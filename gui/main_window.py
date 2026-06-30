"""主窗口 — ADB FPS Monitor 核心 GUI 逻辑

功能:
- 左侧设备信息面板（设备选择 + 信息展示 + 控制按钮）
- 右侧图表区（垂直堆叠，ChartPanel 包裹，十字线联动）
- 底部时间轴导航
- 独立设置面板（传感器选择）
- 启动后等待用户点"开始"才开始监控
"""

import bisect
import logging
import os
import statistics
import time

logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout,
                              QHBoxLayout, QLabel, QScrollArea)
from PyQt6.QtCore import QTimer, Qt
import pyqtgraph as pg

from core.adb import ADBRunner
from core.fps_sources import SmartFPSSource
from core.sensors import TemperatureReader, FreqReader, PowerReader, MemReader, NetReader, batch_prime
from core.app_reader import AppDataReader
from gui.widgets import (COLORS, WINDOW_SECONDS, StatCard, FPSChart,
                          CrosshairChart, TimeAxisWidget,
                          DeviceInfoPanel, ChartPanel, SettingsPanel, HelpDialog)
from gui.worker import FPSWorker, GenericSensorWorker, DeviceInfoWorker, FPSUpdate
from gui.recorder import CSVRecorder

from collections import deque


class _AppReaderAdapter:
    """将 AppDataReader 的方法适配为 Reader 接口（read() → dict）"""
    def __init__(self, app_reader, method_name: str):
        self._reader = app_reader
        self._method = getattr(app_reader, method_name)
    def read(self) -> dict:
        return self._method()

class _MergedMemReader:
    """合并 App + ADB 内存数据：App 提供系统内存，ADB 补充 GPU 显存和 PSS"""
    def __init__(self, app_reader, adb_reader):
        self._app = app_reader
        self._adb = adb_reader
    def read(self) -> dict:
        result = {}
        # ADB 读 GPU 显存 + PSS（App 读不到）
        if self._adb:
            adb_data = self._adb.read()
            if adb_data:
                result.update(adb_data)
        # App 读系统内存（更快更准），但不覆盖 ADB 已有的非零值
        if self._app:
            app_data = self._app.read_memory()
            if app_data:
                for k, v in app_data.items():
                    if v and v > 0 and k not in result:
                        result[k] = v
        return result


def _append_limit(lst: list, val, max_len: int) -> None:
    """向 list 追加元素，超出 max_len 时移除最旧的，替代 deque(maxlen=N)。"""
    if len(lst) >= max_len:
        del lst[0:len(lst) - max_len + 1]
    lst.append(val)


# ─── 卡顿判定常量 ──────────────────────
JANK_THRESHOLD_MS = 33.3       # 帧时间卡顿阈值（30fps）
MAX_SORTED_FPS_SAMPLES = 6000  # FPS 排序样本上限
JANK_MULTIPLIER = 1.5          # 帧时间 > 中位数 × 此倍数 → Jank
BIG_JANK_MULTIPLIER = 2.5      # 帧时间 > 中位数 × 此倍数 → BigJank
FREEZE_MULTIPLIER = 4          # 帧时间 > 中位数 × 此倍数 → Freeze
FREEZE_MIN_MS = 100            # Freeze 最低帧时间阈值（ms）
CONSECUTIVE_JANK_LIMIT = 10    # 连续卡顿次数上限（超过则更新中位数基准）
AUTO_SCROLL_TOLERANCE = 2      # 自动滚动判定容差（秒）


class MainWindow(QMainWindow):
    def __init__(self, serial: str | None = None,
                 package: str | None = None,
                 interval: float = 1.0,
                 no_temp: bool = False,
                 no_freq: bool = False):
        super().__init__()
        self.package = package
        self.interval = interval
        self.no_temp = no_temp
        self.no_freq = no_freq

        # ─── ADB / FPS 源（启动时创建，切换设备时重建） ───
        self.serial = serial
        self.adb = ADBRunner(serial=serial)
        self.fps_src: SmartFPSSource | None = None
        self.temp_reader: TemperatureReader | None = None
        self.freq_reader: FreqReader | None = None
        self.power_reader: PowerReader | None = None
        self.mem_reader: MemReader | None = None

        # ─── 监控状态 ───
        self.monitor_started = False
        self.paused = False
        self.workers: list = []
        self.device_info_worker = None

        # ─── 数据存储 ───
        self.max_points = 3600
        self.fps_x: list[float] = []
        self.fps_y: list[float] = []
        self.temp_x: list[float] = []
        self.temp_y: dict[str, list[float]] = {}
        self.freq_x: list[float] = []
        self.freq_y: dict[str, list[float]] = {}
        self.ft_x: list[float] = []
        self.ft_y: list[float] = []
        self.ft_jank_x: list[float] = []
        self.ft_jank_y: list[float] = []

        # ─── 传感器快照 ───
        self._last_temps: dict[str, float] = {}
        self._last_freqs: dict[str, float] = {}
        self._last_power: dict[str, float] = {}
        self._last_mem: dict[str, float] = {}
        self._last_net: dict[str, float] = {}
        self._temp_updated = False
        self._freq_updated = False
        self._freq_legend_dirty = True

        # ─── Y 轴增量追踪 ───
        self._temp_ymax = 0.0
        self._temp_ymin = 999.0
        self._freq_ymax = 0.0
        self._core_freq_ymax = 0.0
        self._ft_ymax = 0.0

        # ─── 滚动/图表同步 ───
        self._auto_scroll = True
        self._updating_range = False
        self._x_range = (0, WINDOW_SECONDS)
        self._linked_charts: list = []

        # ─── FPS 统计 ───
        self._fps_sorted: list[float] = []
        self._jank_count = 0
        self._last_ft = 0.0
        self._ft_sum = 0.0
        self._ft_count = 0
        self._ft_window: deque[float] = deque(maxlen=30)
        self._big_jank_count = 0
        self._freeze_count = 0
        self._consecutive_jank = 0
        self._jank_bar_x: list[float] = []
        self._jank_bar_y: list[float] = []

        # ─── Per-core 图表数据 ───
        self._core_usage_curves: dict[str, pg.PlotDataItem] = {}
        self._core_usage_x: dict[str, list[float]] = {}
        self._core_usage_y: dict[str, list[float]] = {}
        self._core_usage_checkboxes: dict[str, bool] = {}
        self._core_freq_curves: dict[str, pg.PlotDataItem] = {}
        self._core_freq_x: dict[str, list[float]] = {}
        self._core_freq_y: dict[str, list[float]] = {}
        self._core_freq_checkboxes: dict[str, bool] = {}

        # ─── CSV 录制 ───
        self.recorder = CSVRecorder()

        # ─── 帮助对话框（单例） ───
        self._help_dialog: HelpDialog | None = None

        # ─── 预热状态 ───
        self.ready_count = 0
        self.total_workers = 0

        self._setup_ui()
        QTimer.singleShot(100, self._detect_devices)

    # ═══════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════

    def _setup_ui(self) -> None:
        self.setWindowTitle("ADB FPS Monitor")
        self.setMinimumSize(1200, 800)
        self.setStyleSheet("background: #1e1e2e; color: #cdd6f4;")

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ─── 左侧：设备信息面板 ───
        self.device_info_panel = DeviceInfoPanel()
        self.device_info_panel.device_selected.connect(self._on_device_selected)
        self.device_info_panel.start_pause_clicked.connect(self._on_start_clicked)
        self.device_info_panel.stop_clicked.connect(self._on_stop_clicked)
        self.device_info_panel.save_clicked.connect(self._on_save_clicked)
        self.device_info_panel.btn_refresh.clicked.connect(self._detect_devices)
        self.device_info_panel.btn_settings.clicked.connect(self._toggle_settings_panel)
        self.device_info_panel.btn_help.clicked.connect(self._show_help)
        self.device_info_panel.btn_install_app.clicked.connect(self._install_companion_app)
        root_layout.addWidget(self.device_info_panel)

        # ─── 右侧：主内容区 ───
        right = QWidget()
        right.setStyleSheet("background: #1e1e2e;")
        main_layout = QVBoxLayout(right)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # 顶部标题
        self.title_label = QLabel(f"ADB FPS Monitor - {self.package or '自动检测'}")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("font-size:16px;font-weight:bold;color:#89b4fa;padding:8px;")
        main_layout.addWidget(self.title_label)

        # 卡片行
        main_layout.addLayout(self._build_fps_cards())
        main_layout.addLayout(self._build_system_cards())
        main_layout.addLayout(self._build_stats_cards())

        # 中部滚动区域（图表）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { background: #1e1e2e; border: none; }
            QScrollArea > QWidget > QWidget { background: #1e1e2e; }
        """)
        chart_container = QWidget()
        chart_container.setStyleSheet("background: #1e1e2e;")
        self.chart_layout = QVBoxLayout(chart_container)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        self.chart_layout.setSpacing(12)

        self._build_all_charts()

        scroll.setWidget(chart_container)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        main_layout.addWidget(scroll, stretch=1)

        # 底部时间轴导航
        self.time_axis = TimeAxisWidget()
        self.time_axis.regionChanged.connect(self._on_time_axis_changed)
        main_layout.addWidget(self.time_axis)

        root_layout.addWidget(right, stretch=1)

        # ─── 十字线联动 ───
        self._setup_crosshair_linkage()

        # ─── 设置面板（独立浮动窗口，默认隐藏） ───
        self.settings_panel = SettingsPanel()
        self.settings_panel.checkbox_changed.connect(self._on_sensor_toggle)

    def _build_fps_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.card_fps = StatCard("当前 FPS", '#89b4fa')
        self.card_avg = StatCard("平均 FPS", '#f9e2af')
        self.card_min = StatCard("最低 FPS", '#f38ba8')
        self.card_max = StatCard("最高 FPS", '#a6e3a1')
        self.card_1low = StatCard("1% Low", '#f38ba8')
        self.card_01low = StatCard("0.1% Low", '#f38ba8')
        for card in [self.card_fps, self.card_avg, self.card_min, self.card_max,
                     self.card_1low, self.card_01low]:
            row.addWidget(card)
        return row

    def _build_system_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.card_cpu_load = StatCard("CPU负载(%)", '#f38ba8')
        self.card_gpu_load = StatCard("GPU负载(%)", '#a6e3a1')
        self.card_power = StatCard("功率(mW)", '#f9e2af')
        self.card_battery = StatCard("电量(%)", '#fab387')
        self.card_dl = StatCard("下行(KB/s)", '#89b4fa')
        self.card_ul = StatCard("上行(KB/s)", '#a6e3a1')
        for card in [self.card_cpu_load, self.card_gpu_load, self.card_power,
                     self.card_battery, self.card_dl, self.card_ul]:
            row.addWidget(card)
        return row

    def _build_stats_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.card_gpu_mem = StatCard("GPU Mem(MB)", '#89dceb')
        self.card_pss = StatCard("PSS内存(MB)", '#cba6f7')
        self.card_jank = StatCard("J/BJ/F", '#fab387')
        self.card_count = StatCard("采样次数", '#cba6f7')
        self.card_time = StatCard("监控时长", '#fab387')
        self.card_fps_source = StatCard("FPS源", '#585b70')
        for card in [self.card_gpu_mem, self.card_pss, self.card_jank,
                     self.card_count, self.card_time, self.card_fps_source]:
            row.addWidget(card)
        return row

    def _build_all_charts(self) -> None:
        CHART_HEIGHT = 280

        self.fps_chart = FPSChart("", "FPS", '#89b4fa')
        self.fps_chart.setMinimumHeight(CHART_HEIGHT)
        self.chart_layout.addWidget(ChartPanel("FPS 曲线", self.fps_chart))

        self.ft_chart = CrosshairChart("", "ms", '#f38ba8')
        self.ft_chart.setMinimumHeight(CHART_HEIGHT)
        self.ft_chart.setYRange(0, 100)
        self.ft_line_60 = pg.InfiniteLine(
            pos=16.67, angle=0,
            pen=pg.mkPen('#a6e3a1', width=1, style=Qt.PenStyle.DashLine),
            label='60fps', labelOpts={'color': '#a6e3a1', 'position': 0.05}
        )
        self.ft_chart.addItem(self.ft_line_60)
        self.ft_line_30 = pg.InfiniteLine(
            pos=JANK_THRESHOLD_MS, angle=0,
            pen=pg.mkPen('#f9e2af', width=1, style=Qt.PenStyle.DashLine),
            label='30fps', labelOpts={'color': '#f9e2af', 'position': 0.05}
        )
        self.ft_chart.addItem(self.ft_line_30)
        self.ft_curve = self.ft_chart.plot(pen=pg.mkPen('#f38ba8', width=1.5), name='帧时间')
        self.ft_jank = pg.ScatterPlotItem(
            size=8, pen=pg.mkPen('#f38ba8'), brush=pg.mkBrush('#f38ba8')
        )
        self.ft_chart.addItem(self.ft_jank)
        self.chart_layout.addWidget(ChartPanel("帧时间", self.ft_chart))

        self.temp_chart = CrosshairChart("", "°C", '#f38ba8')
        self.temp_chart.setMinimumHeight(CHART_HEIGHT)
        self.temp_curves: dict[str, pg.PlotDataItem | None] = {}
        self.chart_layout.addWidget(ChartPanel("温度", self.temp_chart))

        self.freq_chart = CrosshairChart("", "MHz", '#a6e3a1')
        self.freq_chart.setMinimumHeight(CHART_HEIGHT)
        self.freq_curves: dict[str, pg.PlotDataItem] = {}
        self.chart_layout.addWidget(ChartPanel("CPU/GPU 频率", self.freq_chart))

        self.core_usage_chart = CrosshairChart("", "%", '#f38ba8')
        self.core_usage_chart.setMinimumHeight(CHART_HEIGHT)
        self.core_usage_chart.setYRange(0, 100)
        self.chart_layout.addWidget(ChartPanel("单核 CPU 负载", self.core_usage_chart))

        self.core_freq_chart = CrosshairChart("", "MHz", '#a6e3a1')
        self.core_freq_chart.setMinimumHeight(CHART_HEIGHT)
        self.core_freq_chart.setYRange(0, 1000)
        self.chart_layout.addWidget(ChartPanel("单核 CPU 频率", self.core_freq_chart))

        self._linked_charts = [
            self.fps_chart, self.ft_chart, self.temp_chart, self.freq_chart,
            self.core_usage_chart, self.core_freq_chart
        ]

        for chart in self._linked_charts:
            chart.setLimits(xMin=0, xMax=WINDOW_SECONDS)
            chart.setXRange(0, WINDOW_SECONDS, padding=0)
            chart.getViewBox().sigRangeChanged.connect(self._on_chart_range_changed)

    def _setup_crosshair_linkage(self) -> None:
        for chart in self._linked_charts:
            chart.sigMouseXChanged.connect(
                lambda x, src=chart: self._on_crosshair_moved(src, x)
            )
            chart.sigMouseLeft.connect(self._on_crosshair_left)

    def _on_crosshair_moved(self, source, x: float) -> None:
        for chart in self._linked_charts:
            if chart is not source:
                chart.show_ref_at(x)

    def _on_crosshair_left(self) -> None:
        for chart in self._linked_charts:
            chart.hide_ref()

    def _toggle_settings_panel(self) -> None:
        if self.settings_panel.isVisible():
            self.settings_panel.hide()
        else:
            geo = self.geometry()
            self.settings_panel.move(geo.right() - self.settings_panel.width() - 20,
                                     geo.top() + 80)
            self.settings_panel.show()

    def _show_help(self) -> None:
        if self._help_dialog is None:
            self._help_dialog = HelpDialog(self)
        self._help_dialog.show()
        self._help_dialog.raise_()
        self._help_dialog.activateWindow()

    def _install_companion_app(self) -> None:
        """通过 ADB 推送配套 App APK 到设备"""
        import glob as glob_mod
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "companion-app")
        apks = glob_mod.glob(os.path.join(base, "ADBMonitorCompanion-*.apk"))
        if not apks:
            self.device_info_panel.btn_install_app.setText("❌ 无APK")
            QTimer.singleShot(2000, lambda: self.device_info_panel.btn_install_app.setText("📱 安装App"))
            return
        apk = apks[0]
        self.device_info_panel.btn_install_app.setText("安装中...")
        self.device_info_panel.btn_install_app.setEnabled(False)

        def do_install():
            out, rc = self.adb.run_host("install", "-r", apk, timeout=30)
            if rc == 0:
                self.device_info_panel.btn_install_app.setText("✅ 已安装")
            else:
                self.device_info_panel.btn_install_app.setText("❌ 失败")
            self.device_info_panel.btn_install_app.setEnabled(True)
            QTimer.singleShot(2000, lambda: self.device_info_panel.btn_install_app.setText("📱 安装App"))

        import threading
        threading.Thread(target=do_install, daemon=True).start()

    def _on_sensor_toggle(self, kind: str, name: str, state: int) -> None:
        if kind == "temp":
            self._on_temp_toggle(name, state)
            self.temp_chart.refresh_tooltip()
        elif kind == "freq":
            self._on_freq_toggle(name, state)
            self.freq_chart.refresh_tooltip()
        elif kind == "core_usage":
            if name in self._core_usage_curves and self._core_usage_curves[name] is not None:
                self._core_usage_curves[name].setVisible(state == 2)
                self._rebuild_legend(self.core_usage_chart, self._core_usage_curves)
                self.core_usage_chart.refresh_tooltip()
        elif kind == "core_freq":
            if name in self._core_freq_curves and self._core_freq_curves[name] is not None:
                self._core_freq_curves[name].setVisible(state == 2)
                self._rebuild_legend(self.core_freq_chart, self._core_freq_curves)
                self.core_freq_chart.refresh_tooltip()

    # ═══════════════════════════════════════════
    # 设备检测 + 选择
    # ═══════════════════════════════════════════

    def _detect_devices(self) -> None:
        self.device_info_panel.btn_refresh.setEnabled(False)
        self.device_info_panel.btn_start.setEnabled(False)
        self.device_info_panel._lbl_device.setText("正在检测设备...")
        devices = self.adb.check_device()
        self.device_info_panel.set_devices(devices)
        self.device_info_panel.btn_refresh.setEnabled(True)
        if devices:
            if self.serial and self.serial in devices:
                idx = devices.index(self.serial)
                self.device_info_panel.device_combo.setCurrentIndex(idx)
            else:
                self.serial = devices[0]
                self.adb.serial = devices[0]
                self.adb._base_cmd = [self.adb._base_cmd[0], "-s", devices[0]]
                self.device_info_panel.device_combo.blockSignals(True)
                self.device_info_panel.device_combo.setCurrentIndex(0)
                self.device_info_panel.device_combo.blockSignals(False)
                self._on_device_selected(0)
        else:
            self.device_info_panel._lbl_device.setText("未检测到设备")

    def _on_device_selected(self, index: int) -> None:
        serial = self.device_info_panel.device_combo.currentText()
        if not serial:
            return
        # 切换设备前清理旧监控
        if self.workers:
            self._cleanup_monitoring()
        self._reset_all_data()
        self.serial = serial
        self.adb.serial = serial
        self.adb._base_cmd = [self.adb._base_cmd[0], "-s", serial]
        self.package = None
        self.title_label.setText(f"ADB FPS Monitor - {serial}")
        self.device_info_panel.btn_start.setEnabled(False)
        self.device_info_panel._lbl_device.setText("正在获取设备信息...")
        if self.device_info_worker:
            self.device_info_worker.requestInterruption()
            self.device_info_worker.wait(200)
        self.device_info_worker = DeviceInfoWorker(self.adb)
        self.device_info_worker.finished.connect(self._on_device_info_ready)
        self.device_info_worker.start()

    def _on_device_info_ready(self, info: dict) -> None:
        self.device_info_worker = None
        self.device_info_panel.update_info(info)
        self.device_info_panel.btn_start.setEnabled(True)
        self.device_info_panel.set_start_state("ready")

    # ═══════════════════════════════════════════
    # 开始 / 暂停 / 录制
    # ═══════════════════════════════════════════

    def _on_start_clicked(self) -> None:
        if not self.monitor_started:
            self._start_monitoring()
        elif self.paused:
            self.paused = False
            self.device_info_panel.set_start_state("running")
        else:
            self.paused = True
            self.device_info_panel.set_start_state("paused")

    def _on_stop_clicked(self) -> None:
        self._cleanup_monitoring()
        self.device_info_panel.set_start_state("stopped")

    def _on_save_clicked(self) -> None:
        if not self.monitor_started:
            # 停止状态：保存全部数据快照
            fname = self.recorder.save_snapshot(
                self.fps_x, self.fps_y,
                self._last_temps, self._last_freqs,
                self._last_power, self._last_mem, self._last_net)
            if fname:
                self.device_info_panel.btn_save.setText("✅ 已保存")
                self.device_info_panel.btn_save.setEnabled(False)
            return
        if self.recorder.is_recording:
            self.recorder.stop()
            self.device_info_panel.btn_save.setText("⏺ 录制")
        else:
            self.recorder.start(self._last_temps)
            self.device_info_panel.btn_save.setText("⏹ 停止")

    def _cleanup_monitoring(self) -> None:
        """停止监控但保留数据（供用户查看/保存）"""
        if self.recorder.is_recording:
            self.recorder.stop()
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.wait(2000)
        if self.fps_src and hasattr(self.fps_src, '_sources'):
            for _name, src in self.fps_src._sources:
                if hasattr(src, 'cleanup'):
                    src.cleanup()
        elif self.fps_src and hasattr(self.fps_src, 'cleanup'):
            self.fps_src.cleanup()
        # 停止配套 App 的 Service
        if hasattr(self, '_app_connected') and self._app_connected:
            self.adb.run_shell(
                "am stopservice -n com.adbmonitor.companion/.MonitorService",
                timeout=3)
            self.app_reader = None
            self._app_connected = False
        self.workers = []
        self.monitor_started = False
        self.paused = False

    # ═══════════════════════════════════════════
    # 启动监控
    # ═══════════════════════════════════════════

    def _start_monitoring(self) -> None:
        # 自动检测前台应用（未指定包名时）
        if not self.package:
            self.package = self.adb.get_foreground_package()
            if self.package:
                self.title_label.setText(f"ADB FPS Monitor - {self.package}")
        self.fps_src = SmartFPSSource(self.adb, package=self.package)
        if not self.no_temp:
            self.temp_reader = TemperatureReader(self.adb)
        if not self.no_freq:
            self.freq_reader = FreqReader(self.adb)
        self.power_reader = PowerReader(self.adb)
        self.mem_reader = MemReader(self.adb, package=self.package)
        self.net_reader = NetReader(self.adb)

        # 尝试启动配套 App，可用时替代功耗/内存/网络 Reader
        self.app_reader = AppDataReader(self.adb)
        self.app_reader.start_app(self.package or "")
        import time; time.sleep(1.0)
        if self.app_reader.probe():
            self._app_connected = True
            logger.info("Companion app connected, using app data for power/memory/network")
        else:
            self._app_connected = False
            logger.info("Companion app not available, falling back to ADB readers")

        # 批量 prime：一次 ADB 调用预读所有传感器，预热连接 + 缓存首次读数
        batch_prime(self.adb,
                    temp_reader=self.temp_reader,
                    freq_reader=self.freq_reader,
                    power_reader=self.power_reader if not self._app_connected else None,
                    net_reader=self.net_reader if not self._app_connected else None)

        self._reset_all_data()
        self._create_monitor_workers()
        self.device_info_panel.set_start_state("running")

    def _reset_all_data(self) -> None:
        self.fps_x.clear(); self.fps_y.clear()
        self.ft_x.clear(); self.ft_y.clear()
        self.ft_jank_x.clear(); self.ft_jank_y.clear()
        self.temp_x.clear()
        for d in list(self.temp_y.values()): d.clear()
        self.freq_x.clear()
        for d in list(self.freq_y.values()): d.clear()
        for d in list(self._core_usage_x.values()) + list(self._core_freq_x.values()): d.clear()
        for d in list(self._core_usage_y.values()) + list(self._core_freq_y.values()): d.clear()

        self._fps_sorted.clear()
        self._jank_count = 0; self._big_jank_count = 0; self._freeze_count = 0
        self._ft_window.clear(); self._consecutive_jank = 0
        self._jank_bar_x.clear(); self._jank_bar_y.clear()
        self._ft_sum = 0.0; self._ft_count = 0; self._last_ft = 0.0; self._ft_ymax = 0.0
        self._temp_ymax = 0.0; self._temp_ymin = 999.0
        self._freq_ymax = 0.0; self._core_freq_ymax = 0.0
        self._temp_updated = False; self._freq_updated = False; self._freq_legend_dirty = True

        self.card_fps.update_value("--"); self.card_avg.update_value("--")
        self.card_min.update_value("--"); self.card_max.update_value("--")
        self.card_1low.update_value("--"); self.card_01low.update_value("--")
        self.card_fps_source.update_value("--"); self.card_time.update_value("--")

    def _create_monitor_workers(self) -> None:
        self.workers = []
        self.ready_count = 0
        self.monitor_started = False
        self.paused = False

        self.fps_worker = FPSWorker(self.fps_src, interval=max(self.interval, 0.2))
        self.fps_worker.fps_ready.connect(self._on_fps)
        self.fps_worker.ready.connect(self._on_worker_ready)
        self.fps_worker.status_update.connect(self._on_fps_status)
        self.workers.append(self.fps_worker)

        if self.freq_reader:
            self.cpu_worker = GenericSensorWorker(self.freq_reader, interval=1.0)
            self.cpu_worker.data_ready.connect(self._on_freq)
            self.cpu_worker.ready.connect(self._on_worker_ready)
            self.workers.append(self.cpu_worker)

        if self.temp_reader:
            self.temp_worker = GenericSensorWorker(self.temp_reader, interval=2.0)
            self.temp_worker.data_ready.connect(self._on_temp)
            self.temp_worker.ready.connect(self._on_worker_ready)
            self.workers.append(self.temp_worker)

        # 功耗/内存/网络：App 可用时用 AppDataReader，否则用 ADB Reader
        power_src = _AppReaderAdapter(self.app_reader, "read_power") if self._app_connected else self.power_reader
        mem_src = _MergedMemReader(self.app_reader, self.mem_reader) if self._app_connected else self.mem_reader
        net_src = _AppReaderAdapter(self.app_reader, "read_network") if self._app_connected else self.net_reader

        self.power_worker = GenericSensorWorker(power_src, interval=5.0)
        self.power_worker.data_ready.connect(self._on_power)
        self.power_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.power_worker)

        self.mem_worker = GenericSensorWorker(mem_src, interval=5.0)
        self.mem_worker.data_ready.connect(self._on_mem)
        self.mem_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.mem_worker)

        self.net_worker = GenericSensorWorker(net_src, interval=2.0)
        self.net_worker.data_ready.connect(self._on_net)
        self.net_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.net_worker)

        self.total_workers = len(self.workers)

        for w in self.workers:
            w.start()

    def _on_worker_ready(self) -> None:
        self.ready_count += 1
        if self.ready_count >= self.total_workers:
            self._on_all_workers_ready()

    def _on_all_workers_ready(self) -> None:
        self.monitor_started = True
        self._monitor_start = time.monotonic()

        for w in self.workers:
            w.reset_time(self._monitor_start)

        for chart in self._linked_charts:
            chart.setLimits(xMin=0, xMax=WINDOW_SECONDS)
            chart.setXRange(0, WINDOW_SECONDS, padding=0)

        self.card_time.update_value("0s")
        self._replay_warmup_data()

    def _on_fps_status(self, status: str) -> None:
        if status == "disconnected":
            self._original_title = self.windowTitle()
            self.setWindowTitle(f"⚠️ 设备断连/息屏 — {self._original_title}")
        elif status == "reconnected":
            self.setWindowTitle(getattr(self, '_original_title', "ADB FPS Monitor"))

    # ═══════════════════════════════════════════
    # 预热数据回放（monitor_started=True 后调用）
    # ═══════════════════════════════════════════

    def _replay_warmup_data(self) -> None:
        """回放所有 Worker 的 warmup 数据"""
        for w in self.workers:
            data = w.get_warmup_data()
            if data is None:
                continue

            if isinstance(w, FPSWorker):
                self._replay_fps_warmup(data)
            elif w is getattr(self, 'cpu_worker', None):
                self._on_freq(0.0, data)
                self._sync_freq_data(0.0)
                self._sync_core_usage(0.0, data)
                self._sync_core_freq(0.0, data)
            elif w is getattr(self, 'temp_worker', None):
                self._on_temp(0.0, data)
                self._sync_temp_data(0.0)
            elif w is getattr(self, 'power_worker', None):
                self._on_power(0.0, data)
            elif w is getattr(self, 'mem_worker', None):
                self._on_mem(0.0, data)
            elif w is getattr(self, 'net_worker', None):
                self._on_net(0.0, data)

    def _replay_fps_warmup(self, fps: float) -> None:
        avg = round(fps, 1)
        _append_limit(self.fps_x, 0.0, self.max_points)
        _append_limit(self.fps_y, fps, self.max_points)
        self.fps_chart.update_data(self.fps_x, self.fps_y, avg,
                                   self._jank_bar_x, self._jank_bar_y)
        self.card_fps.update_value(str(fps))
        self.card_avg.update_value(str(avg))
        self.card_min.update_value(str(fps))
        self.card_max.update_value(str(fps))
        self.card_count.update_value("1")
        if fps > 0.1:
            ft_ms = round(1000.0 / fps, 1)
            _append_limit(self.ft_x, 0.0, self.max_points)
            _append_limit(self.ft_y, ft_ms, self.max_points)
            self.ft_curve.setData(self.ft_x, self.ft_y)
            self.card_1low.update_value(str(fps))
            self.card_01low.update_value(str(fps))

    # ═══════════════════════════════════════════
    # Checkbox / Toggle
    # ═══════════════════════════════════════════

    def _rebuild_legend(self, chart, curves_dict: dict) -> None:
        legend = chart.plotItem.legend
        if legend is None:
            chart.addLegend(offset=(-10, 10))
            legend = chart.plotItem.legend
        if legend:
            legend.clear()
            for n, c in curves_dict.items():
                if c is not None and c.isVisible():
                    legend.addItem(c, n)

    def _toggle_curve(self, chart, curves_dict: dict, name: str, state: int) -> None:
        if name not in curves_dict:
            return
        curve = curves_dict[name]
        if curve is None:
            return
        curve.setVisible(state == 2)
        self._rebuild_legend(chart, curves_dict)

    def _on_temp_toggle(self, name: str, state: int) -> None:
        if state == 2 and name in self.temp_curves and self.temp_curves[name] is None:
            color = COLORS[len([k for k in self.temp_curves if self.temp_curves[k] is not None]) % len(COLORS)]
            pen = pg.mkPen(color, width=1.5)
            self.temp_curves[name] = self.temp_chart.plot(pen=pen, name=name)
            if name in self.temp_y and self.temp_y[name]:
                ty = self.temp_y[name]
                x = self.temp_x[-len(ty):]
                self.temp_curves[name].setData(x, ty)
        self._toggle_curve(self.temp_chart, self.temp_curves, name, state)

    def _on_freq_toggle(self, name: str, state: int) -> None:
        self._toggle_curve(self.freq_chart, self.freq_curves, name, state)

    # ═══════════════════════════════════════════
    # 图表范围同步 + 时间轴
    # ═══════════════════════════════════════════

    def _on_chart_range_changed(self, vb, ranges) -> None:
        if self._updating_range:
            return
        self._updating_range = True
        x_range = ranges[0]
        if x_range[0] < 0:
            x_start = 0
            x_end = x_range[1] - x_range[0]
            vb.setXRange(x_start, x_end)
        else:
            x_start, x_end = x_range
            for chart in self._linked_charts:
                if chart and chart.getViewBox() is not vb:
                    chart.setLimits(xMin=0, xMax=x_end)
                    chart.setXRange(x_start, x_end, padding=0)

        self._x_range = (x_start, x_end)
        self.time_axis.set_region(x_start, x_end)

        if len(self.fps_x) > 0:
            latest_t = self.fps_x[-1]
            self._auto_scroll = (x_end >= latest_t - AUTO_SCROLL_TOLERANCE)
        self._updating_range = False

    def _on_time_axis_changed(self, start: float, end: float) -> None:
        if self._updating_range:
            return
        self._updating_range = True
        self._x_range = (start, end)
        if len(self.fps_x) > 0:
            latest_t = self.fps_x[-1]
            self._auto_scroll = (end >= latest_t - AUTO_SCROLL_TOLERANCE)
        for chart in self._linked_charts:
            if chart:
                chart.setLimits(xMin=0, xMax=end)
                chart.setXRange(start, end, padding=0)
        self._updating_range = False

    # ═══════════════════════════════════════════
    # 窗口事件
    # ═══════════════════════════════════════════

    def closeEvent(self, event) -> None:
        if self.device_info_worker:
            self.device_info_worker.requestInterruption()
            self.device_info_worker.wait(200)
            self.device_info_worker = None
        if self.fps_src and hasattr(self.fps_src, '_sources'):
            for _name, src in self.fps_src._sources:
                if hasattr(src, 'cleanup'):
                    src.cleanup()
        elif self.fps_src and hasattr(self.fps_src, 'cleanup'):
            self.fps_src.cleanup()
        self.recorder.stop()
        if hasattr(self, 'settings_panel'):
            self.settings_panel.close()
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.wait(2000)
        event.accept()

    # ═══════════════════════════════════════════
    # 信号回调 — FPS
    # ═══════════════════════════════════════════

    def _on_fps(self, update: FPSUpdate) -> None:
        if not self.monitor_started or self.paused:
            return

        _append_limit(self.fps_x, update.t, self.max_points)
        _append_limit(self.fps_y, update.fps, self.max_points)
        self.fps_chart.update_data(self.fps_x, self.fps_y, update.avg,
                                   self._jank_bar_x, self._jank_bar_y)

        self._sync_temp_data(update.t)
        self._sync_freq_data(update.t)

        if update.fps > 0.1:
            self._update_fps_stats(update.fps, update.t)

        self.card_fps.update_value(str(update.fps))
        self.card_avg.update_value(str(update.avg))
        self.card_min.update_value(str(update.fps_min))
        self.card_max.update_value(str(update.fps_max))
        self.card_count.update_value(str(update.count))
        self.card_time.update_value(f"{int(update.t)}s")
        if update.source_name:
            self.card_fps_source.update_value(update.source_name)

        if self.recorder.is_recording:
            self.recorder.write_row(update.t, update.fps, self._fps_sorted,
                                    self._jank_count, self._last_temps,
                                    self._last_freqs, self._last_power,
                                    self._last_mem, self._last_net)

        if update.fps > 0.1:
            self._update_frame_time_chart(update.t, update.fps)

        self.time_axis.update_overview(self.fps_x, self.fps_y, update.t)
        self._update_auto_scroll(update.t)

    # ═══════════════════════════════════════════
    # 温度/频率曲线辅助方法
    # ═══════════════════════════════════════════

    def _ensure_temp_series(self, name: str) -> bool:
        if name in self.temp_y:
            return False
        self.temp_y[name] = []
        self.settings_panel.add_temp_checkbox(name)
        if name in self.settings_panel.TEMP_IMPORTANT:
            color = COLORS[len([k for k in self.temp_curves if self.temp_curves[k] is not None]) % len(COLORS)]
            pen = pg.mkPen(color, width=1.5)
            self.temp_curves[name] = self.temp_chart.plot(pen=pen, name=name)
        else:
            self.temp_curves[name] = None
        return True

    def _ensure_freq_series(self, name: str) -> bool:
        if name in self.freq_y:
            return False
        self.freq_y[name] = []
        color = COLORS[len(self.freq_curves) % len(COLORS)]
        pen = pg.mkPen(color, width=1.5)
        self.freq_curves[name] = self.freq_chart.plot(pen=pen, name=name)
        self.settings_panel.add_freq_checkbox(name)
        return True

    # ═══════════════════════════════════════════
    # 温度数据同步
    # ═══════════════════════════════════════════

    def _sync_temp_data(self, t: float) -> None:
        if not self.temp_chart:
            return

        _append_limit(self.temp_x, t, self.max_points)
        temp_legend_dirty = False

        for name, val in self._last_temps.items():
            if self._ensure_temp_series(name):
                temp_legend_dirty = True

            if self.temp_curves.get(name) is None:
                if self.settings_panel.is_temp_checked(name):
                    color = COLORS[len([k for k in self.temp_curves if self.temp_curves[k] is not None]) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self.temp_curves[name] = self.temp_chart.plot(pen=pen, name=name)
                    temp_legend_dirty = True

            _append_limit(self.temp_y[name], val, self.max_points)
            if self.temp_curves.get(name) is not None:
                checked = self.settings_panel.is_temp_checked(name)
                self.temp_curves[name].setVisible(checked)
                if checked:
                    if val > self._temp_ymax:
                        self._temp_ymax = val
                    if val < self._temp_ymin:
                        self._temp_ymin = val
                    dlen = len(self.temp_y[name])
                    self.temp_curves[name].setData(
                        self.temp_x[-dlen:], self.temp_y[name]
                    )

        if self._temp_ymax > 0:
            self.temp_chart.setYRange(self._temp_ymin - 5, self._temp_ymax + 5)
        if temp_legend_dirty:
            self._rebuild_legend(self.temp_chart, self.temp_curves)

    # ═══════════════════════════════════════════
    # 频率数据同步
    # ═══════════════════════════════════════════

    def _sync_freq_data(self, t: float) -> None:
        if not (self._freq_updated and self.freq_chart):
            return
        self._freq_updated = False

        _append_limit(self.freq_x, t, self.max_points)

        for name, val in self._last_freqs.items():
            if "(%)" in name or name.startswith("Core"):
                continue
            if self._ensure_freq_series(name):
                self._freq_legend_dirty = True
            _append_limit(self.freq_y[name], val, self.max_points)
            checked = self.settings_panel.is_freq_checked(name)
            self.freq_curves[name].setVisible(checked)
            if checked:
                if val > self._freq_ymax:
                    self._freq_ymax = val
                dlen = len(self.freq_y[name])
                self.freq_curves[name].setData(
                    self.freq_x[-dlen:], self.freq_y[name]
                )

        if self._freq_ymax > 0:
            self.freq_chart.setYRange(0, self._freq_ymax + 500)
        if self._freq_legend_dirty:
            self._rebuild_legend(self.freq_chart, self.freq_curves)
            self._freq_legend_dirty = False

    # ═══════════════════════════════════════════
    # FPS 统计
    # ═══════════════════════════════════════════

    def _update_fps_stats(self, fps: float, t: float) -> None:
        bisect.insort(self._fps_sorted, fps)
        if len(self._fps_sorted) > MAX_SORTED_FPS_SAMPLES + 100:
            self._fps_sorted = self._fps_sorted[-MAX_SORTED_FPS_SAMPLES:]

        ft = round(1000.0 / fps, 1)
        self._ft_sum += ft
        self._ft_count += 1

        if len(self._ft_window) < 5:
            self._ft_window.append(ft)
            return

        target_ft = statistics.median(self._ft_window)

        is_jank = False
        if ft > target_ft * JANK_MULTIPLIER:
            self._jank_count += 1
            _append_limit(self._jank_bar_x, t, self.max_points)
            _append_limit(self._jank_bar_y, fps, self.max_points)
            is_jank = True
        if ft > target_ft * BIG_JANK_MULTIPLIER:
            self._big_jank_count += 1
        if ft > max(target_ft * FREEZE_MULTIPLIER, FREEZE_MIN_MS):
            self._freeze_count += 1
        if is_jank:
            self._consecutive_jank += 1
        else:
            self._consecutive_jank = 0
        if not is_jank or self._consecutive_jank >= CONSECUTIVE_JANK_LIMIT:
            self._ft_window.append(ft)
        self._last_ft = ft

        n = len(self._fps_sorted)
        if n < 2:
            return
        count_1 = max(1, int(n * 0.01))
        count_01 = max(1, int(n * 0.001))
        avg_1low = round(sum(self._fps_sorted[:count_1]) / count_1, 1)
        avg_01low = round(sum(self._fps_sorted[:count_01]) / count_01, 1)
        self.card_1low.update_value(str(avg_1low))
        self.card_01low.update_value(str(avg_01low))
        self.card_jank.update_value(f"{self._jank_count}/{self._big_jank_count}/{self._freeze_count}")

    # ═══════════════════════════════════════════
    # 帧时间图表更新
    # ═══════════════════════════════════════════

    def _update_frame_time_chart(self, t: float, fps: float) -> None:
        ft_ms = round(1000.0 / fps, 1)
        _append_limit(self.ft_x, t, self.max_points)
        _append_limit(self.ft_y, ft_ms, self.max_points)
        self.ft_curve.setData(self.ft_x, self.ft_y)
        if ft_ms > JANK_THRESHOLD_MS:
            _append_limit(self.ft_jank_x, t, self.max_points)
            _append_limit(self.ft_jank_y, ft_ms, self.max_points)
            self.ft_jank.setData(self.ft_jank_x, self.ft_jank_y)

        if ft_ms > self._ft_ymax:
            self._ft_ymax = ft_ms
            self.ft_chart.setYRange(0, max(self._ft_ymax * 1.2, 50))

    # ═══════════════════════════════════════════
    # 自动滚动
    # ═══════════════════════════════════════════

    def _update_auto_scroll(self, t: float) -> None:
        if not self._auto_scroll or len(self.fps_x) == 0:
            return
        latest_t = self.fps_x[-1]
        if latest_t <= WINDOW_SECONDS:
            x_start = 0
            x_end = WINDOW_SECONDS
        else:
            x_end = latest_t
            x_start = max(0, x_end - WINDOW_SECONDS)
        self._updating_range = True
        self._x_range = (x_start, x_end)
        for chart in self._linked_charts:
            if chart:
                chart.setLimits(xMin=0, xMax=x_end)
                chart.setXRange(x_start, x_end, padding=0)
        self.time_axis.set_region(x_start, x_end)
        self._updating_range = False

    # ═══════════════════════════════════════════
    # 信号回调 — 传感器
    # ═══════════════════════════════════════════

    def _on_temp(self, t: float, temps: dict) -> None:
        if not self.monitor_started or self.paused:
            return
        self._last_temps = temps
        self._temp_updated = True
        self._sync_temp_data(t)

    def _on_freq(self, t: float, freqs: dict) -> None:
        if not self.monitor_started or self.paused:
            return
        self._last_freqs = freqs
        self._freq_updated = True

        if "CPU负载(%)" in freqs:
            self.card_cpu_load.update_value(str(freqs["CPU负载(%)"]))
        if "GPU负载(%)" in freqs:
            self.card_gpu_load.update_value(str(freqs["GPU负载(%)"]))

        self._sync_freq_data(t)
        self._sync_core_usage(t, freqs)
        self._sync_core_freq(t, freqs)

    def _sync_core_usage(self, t: float, freqs: dict) -> None:
        for k, v in freqs.items():
            if k.startswith("CPU") and k.endswith("(%)") and k != "CPU负载(%)":
                label = k.replace("CPU", "Core").replace("(%)", "")
                if label not in self._core_usage_curves:
                    color = COLORS[len(self._core_usage_curves) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self._core_usage_curves[label] = self.core_usage_chart.plot(pen=pen, name=label)
                    self._core_usage_checkboxes[label] = True
                    self.settings_panel.add_core_checkbox(
                        label, True, "core_usage",
                        lambda state, n=label: self._on_sensor_toggle("core_usage", n, state))
                _append_limit(self._core_usage_x.setdefault(label, []), t, self.max_points)
                _append_limit(self._core_usage_y.setdefault(label, []), v, self.max_points)
                dlen = len(self._core_usage_y[label])
                if self._core_usage_checkboxes.get(label, False):
                    self._core_usage_curves[label].setVisible(True)
                    self._core_usage_curves[label].setData(
                        self._core_usage_x[label][-dlen:], self._core_usage_y[label]
                    )
                else:
                    self._core_usage_curves[label].setVisible(False)

    def _sync_core_freq(self, t: float, freqs: dict) -> None:
        core_freq_dirty = False
        for k, v in freqs.items():
            if k.startswith("Core") and k.endswith("(MHz)"):
                label = k.replace("(MHz)", "")
                if label not in self._core_freq_curves:
                    color = COLORS[len(self._core_freq_curves) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self._core_freq_curves[label] = self.core_freq_chart.plot(pen=pen, name=label)
                    self._core_freq_checkboxes[label] = True
                    self.settings_panel.add_core_checkbox(
                        label, True, "core_freq",
                        lambda state, n=label: self._on_sensor_toggle("core_freq", n, state))
                    core_freq_dirty = True
                _append_limit(self._core_freq_x.setdefault(label, []), t, self.max_points)
                _append_limit(self._core_freq_y.setdefault(label, []), v, self.max_points)
                dlen = len(self._core_freq_y[label])
                if self._core_freq_checkboxes.get(label, False):
                    self._core_freq_curves[label].setVisible(True)
                    self._core_freq_curves[label].setData(
                        self._core_freq_x[label][-dlen:], self._core_freq_y[label]
                    )
                    if v > self._core_freq_ymax:
                        self._core_freq_ymax = v
                else:
                    self._core_freq_curves[label].setVisible(False)

        if self._core_freq_ymax > 0:
            self.core_freq_chart.setYRange(0, self._core_freq_ymax + 500)
        if core_freq_dirty:
            self._rebuild_legend(self.core_freq_chart, self._core_freq_curves)

    def _on_power(self, t: float, power: dict) -> None:
        if not self.monitor_started or self.paused:
            return
        self._last_power = power
        is_charging = power.get("充电中", False)

        power_tooltip = f"电压: {power.get('电压(V)', '?')}V"
        if is_charging:
            power_tooltip += "\n(充电中，暂不测量功耗)"
            self.card_power.update_value("充电中")
        else:
            if "功率(mW)" in power:
                self.card_power.update_value(str(int(power["功率(mW)"])))
            if "电流(mA)" in power:
                power_tooltip += f"\n电流: {int(power['电流(mA)'])}mA"
        self.card_power.setToolTip(power_tooltip)

        if "电量(%)" in power:
            self.card_battery.update_value(str(power["电量(%)"]))
        battery_tooltip = f"电压: {power.get('电压(V)', '?')}V"
        if "电流(mA)" in power:
            battery_tooltip += f"\n电流: {int(power['电流(mA)'])}mA"
        self.card_battery.setToolTip(battery_tooltip)

    def _on_net(self, t: float, net: dict) -> None:
        if not self.monitor_started or self.paused:
            return
        self._last_net = net
        if "下行(KB/s)" in net:
            self.card_dl.update_value(str(net["下行(KB/s)"]))
        if "上行(KB/s)" in net:
            self.card_ul.update_value(str(net["上行(KB/s)"]))

    def _on_mem(self, t: float, mem: dict) -> None:
        if not self.monitor_started or self.paused:
            return
        self._last_mem = mem
        if "GPU显存(MB)" in mem:
            self.card_gpu_mem.update_value(str(mem["GPU显存(MB)"]))
        if "PSS内存(MB)" in mem:
            self.card_pss.update_value(str(mem["PSS内存(MB)"]))
