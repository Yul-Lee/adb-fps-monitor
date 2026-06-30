# ADB Monitor Companion

ADB FPS Monitor 的配套 Android App，通过 Android API 直接采集设备数据，比 ADB 更精准。

## 功能

| 数据 | API | 优势 |
|------|-----|------|
| 功耗 | BatteryManager | 直接读电流/电压/功率，无需 charge_counter 差值估算 |
| 内存 | ActivityManager | 实时读取，无 dumpsys meminfo 的 ~3s 延迟 |
| 网络 | TrafficStats | per-app 流量统计（不可用时自动回退系统级） |

## 安装

1. 将 `ADBMonitorCompanion-v0.0.1-alpha.apk` 安装到 Android 设备
2. 启动 `adb_fps_monitor.py`，工具会自动检测并连接 App
3. App 以前台 Service 运行，通知栏显示"监控中: <包名>"

## 工作原理

```
Desktop (Python)                    Android App
┌─────────────────┐   ADB forward   ┌──────────────────┐
│ AppDataReader   │ ──────────────→ │ MonitorHttpServer│
│ (HTTP client)   │  localhost:18765 │ (NanoHTTPD)      │
│                 │                 │                  │
│ GET /api/data   │ ←────────────── │ PowerMonitor     │
│ JSON response   │   JSON          │ MemoryMonitor    │
│                 │                 │ NetworkMonitor   │
└─────────────────┘                 └──────────────────┘
```

- Desktop 通过 `am start` 启动 App，传入目标包名
- App 启动前台 Service + HTTP 服务器（端口 18765）
- Desktop 通过 ADB 端口转发访问 `http://localhost:18765/api/data`
- App 读取数据后返回 JSON，Desktop 解析并显示

## API

**GET /api/data** — 全量数据
```json
{
  "power": {"current_mA": 689, "voltage_V": 4.06, "capacity": 93, "power_mw": 2801},
  "memory": {"total_mb": 11507, "avail_mb": 4779, "target_pss_mb": 245},
  "network": {"rx_bytes": 1234567, "tx_bytes": 234567, "rx_rate_kbps": 456, "tx_rate_kbps": 123}
}
```

**GET /api/data?kind=power** — 按类型查询（power / memory / network）

**GET /api/ping** — 健康检查

## 从源码构建

需要 Android Studio + Android SDK 34。

1. Android Studio 打开 `companion-app/` 目录
2. 等待 Gradle 同步完成
3. Build → Build Bundle(s) / APK(s) → Build APK(s)

## 已知限制

| 场景 | 说明 |
|------|------|
| per-app 网络流量 | 部分设备返回 -1，自动回退到系统级流量 |
| target PSS 内存 | Android 10+ 限制 `getRunningAppProcesses()`，PSS 可能为 0 |
| MIUI 杀后台 | 使用前台 Service 保活，通知栏常驻 |
| 电流读取 | 部分设备不支持 `BATTERY_PROPERTY_CURRENT_NOW`，功耗返回 0 |

## License

MIT
