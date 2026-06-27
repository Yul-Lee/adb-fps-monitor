# ADB FPS Monitor — 开发文档

## 项目概览

ADB 实时性能监控工具，通过 ADB 采集 Android 设备的 FPS、温度、CPU/GPU 频率、功耗、内存、网络数据，PyQt6 + pyqtgraph 图形化展示。

**技术栈：** Python 3.10+ / PyQt6 / pyqtgraph / ADB

**代码规模：** ~4400 行（核心模块）

---

## 文件结构

```
adb_fps_monitor.py          入口（66 行）：参数解析 → QApplication → MainWindow
core/
  adb.py                    ADB 工具层（172 行）
  fps_sources.py            FPS 数据源 + SmartFPS 状态机（975 行）
  sensors.py                传感器读取 + batch_prime（1124 行）
gui/
  main_window.py            主窗口（~1100 行）
  worker.py                 Worker 线程（246 行）
  widgets.py                UI 组件（735 行）
  recorder.py               CSV 录制（240 行）
tests/
  test_fps_sources.py       SmartFPS 单元测试（292 行）
```

---

## 架构总览

```
┌─────────────────────────────────────────────────────┐
│                    MainWindow                        │
│  ┌──────────┐  ┌──────────────────────────────────┐ │
│  │ Device   │  │  Chart Area (Scroll)              │ │
│  │ Info     │  │  ┌─────────────┐ ┌─────────────┐ │ │
│  │ Panel    │  │  │ FPS Chart   │ │ Frame Time  │ │ │
│  │          │  │  ├─────────────┤ ├─────────────┤ │ │
│  │ [设备▼]  │  │  │ Temp Chart  │ │ Freq Chart  │ │ │
│  │ 设备信息 │  │  ├─────────────┤ ├─────────────┤ │ │
│  │          │  │  │ Core Usage  │ │ Core Freq   │ │ │
│  │ [▶开始]  │  │  └─────────────┘ └─────────────┘ │ │
│  │ [⏹结束]  │  │                                   │ │
│  │ [⏺录制]  │  │  ┌─────────────────────────────┐ │ │
│  │ [⚙设置]  │  │  │ TimeAxisWidget (导航条)      │ │ │
│  └──────────┘  │  └─────────────────────────────┘ │ │
│                 └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
         ↕ signals                    ↕ signals
┌─────────────────┐  ┌─────────────────────────────┐
│  Worker Threads  │  │  SettingsPanel (浮动窗口)    │
│  FPSWorker       │  │  温度 / 频率 / 单核 复选框   │
│  CPUWorker       │  └─────────────────────────────┘
│  TempWorker      │
│  PowerWorker     │
│  MemWorker       │
│  NetWorker       │
└─────────────────┘
         ↕ read()
┌─────────────────┐
│  Reader 层       │
│  SmartFPSSource  │
│  FreqReader      │
│  TemperatureR.   │
│  PowerReader     │
│  MemReader       │
│  NetReader       │
└────────┬────────┘
         ↕ run_shell()
┌─────────────────┐
│  ADBRunner       │
│  adb shell ...   │
└─────────────────┘
```

---

## 核心模块详解

### 1. `core/adb.py` — ADB 工具层

**ADBRunner**：封装 ADB 命令执行。

| 方法 | 说明 |
|------|------|
| `run_shell(cmd, timeout=8)` | 执行 `adb shell <cmd>`，返回 `(stdout, returncode)` |
| `run_shell_retry(cmd, ...)` | 带重试的 run_shell，有输出即返回（不检查 rc） |
| `check_device()` | 列出已连接设备，USB 优先 |
| `select_device(devices)` | 设备选择（终端/GUI 对话框） |
| `get_foreground_package()` | 获取当前前台应用包名 |

**关键设计：**
- `run_shell_retry` 只要有输出就返回，不检查退出码（兼容离线核心等场景）
- Windows 下使用 `CREATE_NO_WINDOW` 隐藏子进程窗口
- ADB 路径优先使用项目内 `platform-tools/adb`，回退系统 PATH

---

### 2. `core/fps_sources.py` — FPS 数据源 + 状态机

**FPSState 枚举**（Source 层返回状态）：

| 状态 | 含义 | 状态机处理 |
|------|------|-----------|
| `READY` | Target 正常出帧 | 锁定源 |
| `NO_FRAME` | Target 在但没帧 | 等待/超时 |
| `WARMUP` | 预热中 | 进入 PENDING |
| `TRANSIENT_FAIL` | Source 临时故障 | 重试/RECOVERING |
| `UNSUPPORTED` | Source 永久不可用 | 拉黑 |
| `TARGET_INVALID` | Target 消失 | 重新 DISCOVERING |

**4 个 FPS Source**（按优先级）：

| 优先级 | Source | 原理 | 适用场景 |
|--------|--------|------|---------|
| 1 | `TimeStatsFPS` | `dumpsys SurfaceFlinger --timestats` | Android 12+，最准确 |
| 2 | `SFLatencyFPS` | `dumpsys SurfaceFlinger --latency` | 通用，兼容旧格式 |
| 3 | `GfxInfoFPS` | `dumpsys gfxinfo` | 需要包名 |
| 4 | `SFBuffFPS` | SurfaceFlinger buffer frame count | 保底 |

**SmartFPS 状态机（6 态）：**

```
UNINITIALIZED → DISCOVERING → PENDING → ACTIVE
                                    ↘ PAUSED → DISCOVERING (重试)
                 ACTIVE → RECOVERING → ACTIVE
                       → PAUSED (NO_FRAME ×30)
                       → DISCOVERING (UNSUPPORTED/TARGET_INVALID)
```

**关键常量：**
- `PENDING_TIMEOUT`: timestats=3.0s, sf_latency=1.2s, gfxinfo=1.5s, sf_buffer=1.0s
- `NO_FRAME_THRESHOLD = 30`（~6s @ 5Hz）
- `FAIL_THRESHOLD = 10`
- `PAUSED_RETRY_INTERVAL = 3.0s`

**SFLatencyFPS 特殊处理：**
- 兼容旧格式 `SurfaceView - pkg/act`（Android 7 等）
- 多候选窗口：逐个尝试，锁定有帧数据的窗口
- 无帧时返回 `READY + 0 FPS`（源已锁定，等待渲染）

**TimeStatsFPS 特殊处理：**
- `_parse_output` 跳过 Legacy 全局统计（`seen_layer` 标记）
- 跟踪 `_target_layer`，layer 消失时返回 `TARGET_INVALID`
- 全局帧计数器作为 per-layer delta 的回退

---

### 3. `core/sensors.py` — 传感器读取

**5 个 Reader 类：**

| Reader | 数据源 | 采集间隔 |
|--------|--------|---------|
| `TemperatureReader` | sysfs thermal_zone → thermalservice → battery | 2s |
| `FreqReader` | cpufreq policies + /proc/stat + GPU sysfs | 1s |
| `PowerReader` | sysfs battery → charge_counter → batterystats | 5s |
| `MemReader` | dumpsys meminfo | 5s |
| `NetReader` | /proc/net/dev | 2s |

**温度传感器三层映射：**
1. 用户自定义 `temp_map.json`（最高优先级）
2. `SPECIAL_MAP`（精确匹配，14 条）
3. `TEMP_RULES`（正则匹配，~80 条，覆盖骁龙/天玑/Exynos/通用）

**FreqReader 特殊处理：**
- cpufreq policy 为空时回退到 per-core sysfs
- 同名传感器取最高值（多个 tsens 映射到 SoC 时显示最热点）
- GPU 频率/负载：4 级降级（骁龙 kgsl → Mali legacy → Mali devfreq → 通用回退）

**batch_prime()：**
- 一次 ADB 调用预读温度/功耗/CPU/网络
- 设置 `_warmup_cache`，Worker 首次 `read()` 直接返回缓存
- 启动延迟从 ~3.7s 降到 ~0.5s

**GPU 名称映射：**
```python
gpu_map = {"qcom": "Adreno", "adreno": "Adreno", "mali": "Mali", "powervr": "PowerVR", "radeon": "AMD RDNA"}
```

---

### 4. `gui/main_window.py` — 主窗口

**启动流程：**
```
__init__ → _setup_ui() → QTimer → _detect_devices()
                                    ↓
                        DeviceInfoWorker → 更新面板
                                    ↓
                        用户点"开始" → _start_monitoring()
                                    ↓
                        get_foreground_package()
                        创建 Reader + batch_prime
                        _create_monitor_workers()
                        Worker 预热 → _on_all_workers_ready()
                        _replay_warmup_data() → 正式监控
```

**状态管理：**
- `monitor_started`: FPS 数据是否在处理
- `paused`: 是否暂停（Worker 继续运行）
- `workers_running`: Worker 是否在运行（含预热）

**数据流：**
```
Worker.poll() → reader.read() → data_ready signal
    → _on_freq/_on_temp/_on_power/_on_net/_on_mem
    → 更新卡片 + _sync_*_data() → 更新图表
    → _on_fps → _sync_temp_data + _sync_freq_data + _update_fps_stats
    → CSVRecorder.write_row()
```

**图表联动：**
- 6 个图表共享 X 轴范围（`_linked_charts`）
- 十字线跨图表联动（`sigMouseXChanged`）
- 底部 TimeAxisWidget 导航条
- 自动滚动：数据超过 WINDOW_SECONDS 后自动扩展

**控制按钮状态：**

| 状态 | 开始 | 结束 | 录制 | 下拉框 |
|------|------|------|------|--------|
| ready | ▶ 开始 | 禁用 | ⏺ 录制（禁用） | 启用 |
| running | ⏸ 暂停 | 启用 | ⏺ 录制 | 禁用 |
| paused | ▶ 继续 | 启用 | ⏺ 录制 | 禁用 |
| stopped | ▶ 开始 | 禁用 | 💾 保存数据 | 启用 |

---

### 5. `gui/worker.py` — Worker 线程

**BaseWorker**：带 sleep 循环的基础 Worker。
- 预热阶段 0.3s 间隔，就绪后恢复正常间隔
- 预热超时 5s 后自动标记就绪
- `_stop_event.wait()` 替代 `time.sleep()`，支持即时退出

**FPSWorker**：5Hz，统计 min/max/avg。
- `FPSUpdate` dataclass：fps, avg, fps_min, fps_max, t, count, source_name
- 连续 30 次失败 → 发射 "disconnected" 信号

**GenericSensorWorker**：通用 dict 返回型 Reader。
- 首次成功数据缓存为 warmup，不发射信号
- 后续数据通过 `data_ready` 信号发射

**DeviceInfoWorker**：后台获取设备信息（品牌/型号/Android/SoC/CPU/GPU/内存）。

---

### 6. `gui/widgets.py` — UI 组件

| 组件 | 说明 |
|------|------|
| `StatCard` | 统计指标卡片（HTML 富文本） |
| `CrosshairChart` | 带十字线 + 悬停 tooltip + 右侧图例的图表基类 |
| `FPSChart` | FPS 曲线（继承 CrosshairChart，带 Jank 标记条） |
| `TimeAxisWidget` | 底部时间轴导航（LinearRegionItem 拖拽选区） |
| `DeviceInfoPanel` | 左侧面板（设备下拉框 + 信息 + 控制按钮） |
| `ChartPanel` | 图表容器（居中标题 + 分隔线 + 图表） |
| `SettingsPanel` | 浮动传感器选择窗口（3 列网格复选框） |

**颜色方案（Catppuccin Mocha）：**
- 背景：`#1e1e2e`（主）、`#181825`（面板）
- 卡片：`#313244`
- 文字：`#cdd6f4`（主）、`#a6adc8`（次要）
- 强调：`#89b4fa`（蓝）、`#a6e3a1`（绿）、`#f38ba8`（红）、`#f9e2af`（黄）

---

### 7. `gui/recorder.py` — CSV 录制

**两种模式：**
- **实时录制**：`start()` → `write_row()` 逐行写入 → `stop()`
- **快照保存**：`save_snapshot()` 一次性写入全部 FPS + 传感器数据

**动态列：** 温度/频率/单核列在运行时按需添加，首次写入时生成表头。

---

## 已知限制

| 场景 | 行为 | 原因 |
|------|------|------|
| AOD 息屏 | 可能统计 5-15 FPS | SFBuffFPS 无法区分 AOD Surface |
| 旧设备 Android 7 | 部分 sysfs 不可用 | shell 兼容性差异 |
| SELinux 封死 GPU sysfs | GPU 负载留空 | 需要 root |
| WiFi 连接 | 传感器读取延迟增加 | ADB subprocess 开销被网络放大 |
| 多个 SurfaceView | 可能选错 layer | 按优先级选择，非按活跃度 |

---

## 测试

```bash
python -m pytest tests/ -v
```

测试覆盖：SmartFPS 状态机 6 态转换、PENDING 超时、UNSUPPORTED 拉黑、PAUSED 重试、Sticky Source。

未覆盖：传感器 Reader、Recorder、UI 组件。

---

## 构建

```bash
pip install -r requirements.txt
python adb_fps_monitor.py          # 开发运行
pyinstaller --onefile adb_fps_monitor.py  # 打包
```
