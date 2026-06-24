"""主窗口 — ADB FPS Monitor 核心 GUI 逻辑

优化点:
- _on_fps 拆分为多个聚焦方法
- _update_fps_stats 使用 bisect 实现 O(log n) 插入
- CSV 录制逻辑委托给 CSVRecorder
- 垂直单列图表布局 + 滚动
- 十字线联动 + 悬停数据提示
- 底部时间轴导航
"""

import bisect
import statistics
import time

from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout,
                              QHBoxLayout, QLabel, QDockWidget, QCheckBox,
                              QScrollArea, QPushButton)
from PyQt6.QtCore import QTimer, Qt
import pyqtgraph as pg

from core.adb import ADBRunner
from core.fps_sources import SmartFPSSource
from core.sensors import TemperatureReader, FreqReader, PowerReader, MemReader, NetReader
from gui.widgets import (COLORS, WINDOW_SECONDS, StatCard, FPSChart,
                          CrosshairChart, TimeAxisWidget, LoadingOverlay)
from gui.worker import FPSWorker, GenericSensorWorker, DeviceInfoWorker, FPSUpdate
from gui.recorder import CSVRecorder

from collections import deque


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
    TEMP_IMPORTANT = {"CPU", "GPU", "表面", "电池"}

    def __init__(self, adb: ADBRunner, fps_src: SmartFPSSource,
                 temp_reader: TemperatureReader | None,
                 freq_reader: FreqReader | None,
                 power_reader: PowerReader, mem_reader: MemReader,
                 interval: float, package: str | None):
        super().__init__()
        self.adb = adb
        self.fps_src = fps_src
        self.temp_reader = temp_reader
        self.freq_reader = freq_reader
        self.power_reader = power_reader
        self.mem_reader = mem_reader
        self.interval = interval
        self.package = package

        # ─── 数据存储（plain list + _append_limit 手动截断） ───
        self.max_points = 3600  # 1小时数据量
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
        self._x_range = (0, WINDOW_SECONDS)  # 当前显示的 X 轴范围
        self._linked_charts: list = []  # 所有联动图表

        # ─── FPS 统计（bisect 优化） ───
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

        # ─── 预热状态 ───
        self.ready_count = 0
        self.total_workers = 0
        self.monitor_started = False
        self._startup_cancelled = False
        self.device_info_worker = None
        self.workers: list = []

        self._setup_ui()
        self._start_worker()

    # ═══════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════

    def _setup_ui(self) -> None:
        self.setWindowTitle("ADB FPS Monitor")
        self.setMinimumSize(1200, 800)
        self.setStyleSheet("background: #1e1e2e; color: #cdd6f4;")

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # ─── 顶部固定区域（标题 + 卡片） ───
        title = QLabel(f"ADB FPS Monitor - {self.package or '自动检测'}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:16px;font-weight:bold;color:#89b4fa;padding:8px;")
        main_layout.addWidget(title)

        # 卡片行
        main_layout.addLayout(self._build_fps_cards())
        main_layout.addLayout(self._build_system_cards())
        main_layout.addLayout(self._build_stats_cards())

        # ─── 中部滚动区域（图表） ───
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

        # 构建图表
        self._build_all_charts()

        scroll.setWidget(chart_container)
        # 始终显示垂直滚动条，预留滚动条空间，防止图表 X 轴超出可视区域
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        main_layout.addWidget(scroll, stretch=1)

        # ─── 底部时间轴导航 ───
        self.time_axis = TimeAxisWidget()
        self.time_axis.regionChanged.connect(self._on_time_axis_changed)
        main_layout.addWidget(self.time_axis)

        # ─── 十字线联动 ───
        self._setup_crosshair_linkage()

        self._create_settings_dock()

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
        for card in [self.card_gpu_mem, self.card_pss, self.card_jank,
                     self.card_count, self.card_time]:
            row.addWidget(card)

        self.btn_record = QPushButton("⏺ 录制")
        self.btn_record.setCheckable(True)
        self.btn_record.setFixedHeight(70)
        self.btn_record.setStyleSheet("""
            QPushButton { background: #313244; color: #a6e3a1; font-size: 16px; font-weight: bold;
                          border-radius: 8px; padding: 8px 16px; border: 2px solid #45475a; }
            QPushButton:checked { background: #45475a; color: #f38ba8; border-color: #f38ba8; }
        """)
        self.btn_record.clicked.connect(self._toggle_recording)
        row.addWidget(self.btn_record)
        return row

    def _build_all_charts(self) -> None:
        """构建所有图表（垂直单列布局）"""
        CHART_HEIGHT = 280

        # 1. FPS 曲线
        self.fps_chart = FPSChart("FPS 曲线", "FPS", '#89b4fa')
        self.fps_chart.setMinimumHeight(CHART_HEIGHT)
        self.chart_layout.addWidget(self.fps_chart)

        # 2. 帧时间图表
        self.ft_chart = CrosshairChart("帧时间 (ms)", "ms", '#f38ba8')
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
        self.chart_layout.addWidget(self.ft_chart)

        # 3. 温度图表
        if self.temp_reader:
            self.temp_chart = CrosshairChart("温度 (°C)", "°C", '#f38ba8')
            self.temp_chart.setMinimumHeight(CHART_HEIGHT)
            self.temp_curves: dict[str, pg.PlotDataItem | None] = {}
            self.chart_layout.addWidget(self.temp_chart)
        else:
            self.temp_chart = None

        # 4. CPU/GPU 频率图表
        if self.freq_reader:
            self.freq_chart = CrosshairChart("CPU/GPU 频率", "MHz", '#a6e3a1')
            self.freq_chart.setMinimumHeight(CHART_HEIGHT)
            self.freq_curves: dict[str, pg.PlotDataItem] = {}
            self.chart_layout.addWidget(self.freq_chart)
        else:
            self.freq_chart = None

        # 5. 单核 CPU 负载
        self.core_usage_chart = CrosshairChart("单核 CPU 负载 (%)", "%", '#f38ba8')
        self.core_usage_chart.setMinimumHeight(CHART_HEIGHT)
        self.core_usage_chart.setYRange(0, 100)
        self.chart_layout.addWidget(self.core_usage_chart)

        # 6. 单核 CPU 频率
        self.core_freq_chart = CrosshairChart("单核 CPU 频率", "MHz", '#a6e3a1')
        self.core_freq_chart.setMinimumHeight(CHART_HEIGHT)
        self.core_freq_chart.setYRange(0, 1000)
        self.chart_layout.addWidget(self.core_freq_chart)

        # 收集所有联动图表
        self._linked_charts = [
            self.fps_chart, self.ft_chart, self.core_usage_chart,
            self.core_freq_chart
        ]
        if self.temp_chart:
            self._linked_charts.append(self.temp_chart)
        if self.freq_chart:
            self._linked_charts.append(self.freq_chart)

        # 设置 X 轴范围和联动
        for chart in self._linked_charts:
            if chart:
                chart.setLimits(xMin=0, xMax=WINDOW_SECONDS)
                chart.setXRange(0, WINDOW_SECONDS, padding=0)
            chart.getViewBox().sigRangeChanged.connect(self._on_chart_range_changed)

    def _setup_crosshair_linkage(self) -> None:
        """设置十字线跨图表联动"""
        for chart in self._linked_charts:
            chart.sigMouseXChanged.connect(
                lambda x, src=chart: self._on_crosshair_moved(src, x)
            )
            chart.sigMouseLeft.connect(self._on_crosshair_left)

    def _on_crosshair_moved(self, source, x: float) -> None:
        """任意图表鼠标移动 → 其他图表显示参考线"""
        for chart in self._linked_charts:
            if chart is not source:
                chart.show_ref_at(x)

    def _on_crosshair_left(self) -> None:
        """鼠标离开图表 → 隐藏所有参考线"""
        for chart in self._linked_charts:
            chart.hide_ref()

    def _create_settings_dock(self) -> None:
        self._settings_dock = dock = QDockWidget("传感器选择", self)
        dock.setStyleSheet("QDockWidget { color: #cdd6f4; background: #181825; }")
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea { background: #1e1e2e; border: none; }
            QScrollArea > QWidget > QWidget { background: #1e1e2e; }
            QCheckBox { padding-left: 6px; spacing: 6px; }
            QCheckBox::indicator { width: 16px; height: 16px; }
        """)
        panel = QWidget()
        panel.setStyleSheet("background: #1e1e2e;")
        self.dock_layout = QVBoxLayout(panel)
        self.dock_layout.setContentsMargins(4, 8, 8, 8)
        self.dock_layout.setSpacing(4)

        lbl1 = QLabel("🌡 温度传感器")
        lbl1.setStyleSheet("color:#f38ba8; font-weight:bold; font-size:13px; padding:4px 0;")
        self.dock_layout.addWidget(lbl1)
        self._show_all_temp = QCheckBox("显示所有已映射传感器")
        self._show_all_temp.setChecked(False)
        self._show_all_temp.setStyleSheet("color:#6c7086; font-size:11px; padding:2px 0 2px 16px;")
        self._show_all_temp.stateChanged.connect(self._on_show_all_temp_toggle)
        self.dock_layout.addWidget(self._show_all_temp)
        self.temp_checkbox_container = QVBoxLayout()
        self.dock_layout.addLayout(self.temp_checkbox_container)
        self.temp_checkboxes: dict[str, QCheckBox] = {}

        lbl2 = QLabel("⚡ 频率/GPU")
        lbl2.setStyleSheet("color:#a6e3a1; font-weight:bold; font-size:13px; padding:8px 0 4px;")
        self.dock_layout.addWidget(lbl2)
        self.freq_checkbox_container = QVBoxLayout()
        self.dock_layout.addLayout(self.freq_checkbox_container)
        self.freq_checkboxes: dict[str, QCheckBox] = {}

        lbl4 = QLabel("📊 单核 CPU 负载")
        lbl4.setStyleSheet("color:#f38ba8; font-weight:bold; font-size:13px; padding:8px 0 4px;")
        self.dock_layout.addWidget(lbl4)
        self.core_usage_checkbox_container = QVBoxLayout()
        self.dock_layout.addLayout(self.core_usage_checkbox_container)

        lbl5 = QLabel("📈 单核 CPU 频率")
        lbl5.setStyleSheet("color:#a6e3a1; font-weight:bold; font-size:13px; padding:8px 0 4px;")
        self.dock_layout.addWidget(lbl5)
        self.core_freq_checkbox_container = QVBoxLayout()
        self.dock_layout.addLayout(self.core_freq_checkbox_container)

        self.dock_layout.addStretch()
        scroll.setWidget(panel)
        dock.setWidget(scroll)

    # ═══════════════════════════════════════════
    # Worker 启动
    # ═══════════════════════════════════════════

    def _start_worker(self) -> None:
        """立即显示遮罩，后台获取设备信息后启动 Worker"""
        self.loading_overlay = LoadingOverlay(self)
        self.loading_overlay.cancelled.connect(self._on_loading_cancelled)

        self.device_info_worker = DeviceInfoWorker(self.adb)
        self.device_info_worker.finished.connect(self._on_device_info_ready)
        self.device_info_worker.start()

    def _on_device_info_ready(self, info_text: str) -> None:
        """设备信息获取完毕，更新遮罩并启动监控 Worker"""
        self.device_info_worker = None
        if self._startup_cancelled:
            return
        if self.loading_overlay:
            self.loading_overlay.update_device_info(info_text)
        self._create_monitor_workers()

    def _create_monitor_workers(self) -> None:
        """创建并启动所有监控 Worker"""
        self.net_reader = NetReader(self.adb)
        self.workers: list = []

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

        self.power_worker = GenericSensorWorker(self.power_reader, interval=5.0)
        self.power_worker.data_ready.connect(self._on_power)
        self.power_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.power_worker)

        self.mem_worker = GenericSensorWorker(self.mem_reader, interval=5.0)
        self.mem_worker.data_ready.connect(self._on_mem)
        self.mem_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.mem_worker)

        self.net_worker = GenericSensorWorker(self.net_reader, interval=2.0)
        self.net_worker.data_ready.connect(self._on_net)
        self.net_worker.ready.connect(self._on_worker_ready)
        self.workers.append(self.net_worker)

        self.ready_count = 0
        self.total_workers = len(self.workers)
        self.monitor_started = False

        for w in self.workers:
            w.start()

        QTimer.singleShot(500, self._on_init_done)

    def _on_loading_cancelled(self) -> None:
        """用户点击取消按钮"""
        self._startup_cancelled = True
        # 清理 DeviceInfoWorker
        if self.device_info_worker:
            self.device_info_worker.requestInterruption()
            self.device_info_worker.wait(200)
            self.device_info_worker = None
        # 关闭遮罩
        if self.loading_overlay:
            self.loading_overlay.finish()
            self.loading_overlay = None
        self.close()

    # ═══════════════════════════════════════════
    # Checkbox / Toggle
    # ═══════════════════════════════════════════

    def _add_temp_checkbox(self, name: str) -> None:
        if name in self.temp_checkboxes:
            return
        cb = QCheckBox(name)
        is_important = name in self.TEMP_IMPORTANT
        cb.setChecked(is_important)
        cb.setStyleSheet("color:#cdd6f4; font-size:12px; padding:2px 0 2px 16px;")
        cb.stateChanged.connect(lambda state, n=name: self._on_temp_toggle(n, state))
        self.temp_checkboxes[name] = cb
        self.temp_checkbox_container.addWidget(cb)
        if not is_important and not self._show_all_temp.isChecked():
            cb.hide()

    def _on_show_all_temp_toggle(self, state: int) -> None:
        show_all = state == 2
        for name, cb in self.temp_checkboxes.items():
            if name in self.TEMP_IMPORTANT or show_all:
                cb.show()
            else:
                cb.hide()

    def _add_freq_checkbox(self, name: str) -> None:
        if name in self.freq_checkboxes:
            return
        cb = QCheckBox(name)
        cb.setChecked(True)
        cb.setStyleSheet("color:#cdd6f4; font-size:12px; padding:2px 0 2px 16px;")
        cb.stateChanged.connect(lambda state, n=name: self._on_freq_toggle(n, state))
        self.freq_checkboxes[name] = cb
        self.freq_checkbox_container.addWidget(cb)

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

    def _toggle_curve(self, chart, curves_dict: dict,
                      name: str, state: int) -> None:
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

    def _add_core_checkbox(self, name: str, checked: bool,
                           chart, curves: dict,
                           checks: dict, container: QVBoxLayout) -> None:
        cb = QCheckBox(name)
        cb.setChecked(checked)
        cb.setStyleSheet("color:#cdd6f4; font-size:12px; padding:2px 0 2px 16px;")

        def _on_toggle(state: int, n: str = name) -> None:
            checks[n] = (state == 2)
            if n in curves and curves[n] is not None:
                curves[n].setVisible(state == 2)
                self._rebuild_legend(chart, curves)

        cb.stateChanged.connect(_on_toggle)
        container.addWidget(cb)

    # ═══════════════════════════════════════════
    # 图表范围同步 + 时间轴
    # ═══════════════════════════════════════════

    def _on_chart_range_changed(self, vb, ranges) -> None:
        """同步所有图表的 X 轴范围 + 更新底部时间轴"""
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

        # 同步底部时间轴选区
        self.time_axis.set_region(x_start, x_end)

        # 检测是否仍在自动滚动
        if len(self.fps_x) > 0:
            latest_t = self.fps_x[-1]
            self._auto_scroll = (x_end >= latest_t - AUTO_SCROLL_TOLERANCE)
        self._updating_range = False

    def _on_time_axis_changed(self, start: float, end: float) -> None:
        """底部时间轴选区变化 → 更新所有图表 X 轴"""
        if self._updating_range:
            return
        self._updating_range = True
        self._x_range = (start, end)
        # 检测是否仍在自动滚动
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, 'loading_overlay') and self.loading_overlay is not None:
            self.loading_overlay.setGeometry(self.rect())

    def _on_init_done(self) -> None:
        if hasattr(self, 'loading_overlay') and self.loading_overlay is not None:
            self.loading_overlay.update_status(f"预热中... (0/{self.total_workers})")

        if hasattr(self, '_settings_dock'):
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._settings_dock)
            self._settings_dock.show()

    def closeEvent(self, event) -> None:
        # 清理 DeviceInfoWorker（如果还在运行）
        if self.device_info_worker:
            self.device_info_worker.requestInterruption()
            self.device_info_worker.wait(200)
            self.device_info_worker = None
        if hasattr(self.fps_src, '_sources'):
            for _name, src in self.fps_src._sources:
                if hasattr(src, 'cleanup'):
                    src.cleanup()
        elif hasattr(self.fps_src, 'cleanup'):
            self.fps_src.cleanup()
        self.recorder.stop()
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.wait(2000)
        event.accept()

    # ═══════════════════════════════════════════
    # 预热完成回调
    # ═══════════════════════════════════════════

    def _on_worker_ready(self) -> None:
        self.ready_count += 1
        if hasattr(self, 'loading_overlay') and self.loading_overlay is not None:
            self.loading_overlay.update_status(f"预热中... ({self.ready_count}/{self.total_workers})")

        if self.ready_count >= self.total_workers:
            self._start_monitoring()

    def _start_monitoring(self) -> None:
        self.monitor_started = True

        if hasattr(self, 'loading_overlay') and self.loading_overlay is not None:
            self.loading_overlay.finish()
            self.loading_overlay = None

        self._monitor_start = time.monotonic()

        # 清空所有图表数据
        self.fps_x.clear()
        self.fps_y.clear()
        self.ft_x.clear()
        self.ft_y.clear()
        self.ft_jank_x.clear()
        self.ft_jank_y.clear()
        self.temp_x.clear()
        for d in list(self.temp_y.values()):
            d.clear()
        self.freq_x.clear()
        for d in list(self.freq_y.values()):
            d.clear()
        for d in list(self._core_usage_x.values()) + list(self._core_freq_x.values()):
            d.clear()
        for d in list(self._core_usage_y.values()) + list(self._core_freq_y.values()):
            d.clear()

        # 重置统计
        self._fps_sorted.clear()
        self._jank_count = 0
        self._big_jank_count = 0
        self._freeze_count = 0
        self._ft_window.clear()
        self._consecutive_jank = 0
        self._jank_bar_x.clear()
        self._jank_bar_y.clear()
        self._ft_sum = 0.0
        self._ft_count = 0
        self._last_ft = 0.0
        self._ft_ymax = 0.0
        self._temp_ymax = 0.0
        self._temp_ymin = 999.0
        self._freq_ymax = 0.0
        self._core_freq_ymax = 0.0
        self._temp_updated = False
        self._freq_updated = False
        self._freq_legend_dirty = True

        for w in self.workers:
            w.reset_time(self._monitor_start)

        for chart in self._linked_charts:
            if chart:
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
    # 预热数据回放
    # ═══════════════════════════════════════════

    def _replay_warmup_data(self) -> None:
        for w in self.workers:
            data = w.get_warmup_data()
            if data is None:
                continue

            if isinstance(w, FPSWorker):
                fps = data
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

            elif w is getattr(self, 'cpu_worker', None):
                self._on_freq(0.0, data)

            elif w is getattr(self, 'temp_worker', None):
                self._on_temp(0.0, data)

            elif w is getattr(self, 'power_worker', None):
                self._last_power = data

            elif w is getattr(self, 'mem_worker', None):
                self._last_mem = data
                if "GPU显存(MB)" in data:
                    self.card_gpu_mem.update_value(str(data["GPU显存(MB)"]))
                if "PSS内存(MB)" in data:
                    self.card_pss.update_value(str(data["PSS内存(MB)"]))

            elif w is getattr(self, 'net_worker', None):
                self._last_net = data

    # ═══════════════════════════════════════════
    # 信号回调 — FPS
    # ═══════════════════════════════════════════

    def _on_fps(self, update: FPSUpdate) -> None:
        if not self.monitor_started:
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

        if self.recorder.is_recording:
            self.recorder.write_row(update.t, update.fps, self._fps_sorted,
                                    self._jank_count, self._last_temps,
                                    self._last_freqs, self._last_power,
                                    self._last_mem, self._last_net)

        if update.fps > 0.1:
            self._update_frame_time_chart(update.t, update.fps)

        # 更新底部时间轴缩略图
        self.time_axis.update_overview(self.fps_x, self.fps_y, update.t)

        self._update_auto_scroll(update.t)

    # ═══════════════════════════════════════════
    # 温度/频率曲线辅助方法
    # ═══════════════════════════════════════════

    def _ensure_temp_series(self, name: str) -> bool:
        """确保温度传感器 name 有对应的 list、checkbox、curve。返回是否新建了曲线。"""
        if name in self.temp_y:
            return False
        self.temp_y[name] = []
        self._add_temp_checkbox(name)
        if name in self.TEMP_IMPORTANT:
            color = COLORS[len([k for k in self.temp_curves if self.temp_curves[k] is not None]) % len(COLORS)]
            pen = pg.mkPen(color, width=1.5)
            self.temp_curves[name] = self.temp_chart.plot(pen=pen, name=name)
        else:
            self.temp_curves[name] = None
        return True

    def _ensure_freq_series(self, name: str) -> bool:
        """确保频率传感器 name 有对应的 list、curve。返回是否新建了曲线。"""
        if name in self.freq_y:
            return False
        self.freq_y[name] = []
        color = COLORS[len(self.freq_curves) % len(COLORS)]
        pen = pg.mkPen(color, width=1.5)
        self.freq_curves[name] = self.freq_chart.plot(pen=pen, name=name)
        self._add_freq_checkbox(name)
        return True

    # ═══════════════════════════════════════════
    # 温度数据同步
    # ═══════════════════════════════════════════

    def _sync_temp_data(self, t: float) -> None:
        if not (self._temp_updated and self.temp_chart):
            return
        self._temp_updated = False

        _append_limit(self.temp_x, t, self.max_points)
        temp_legend_dirty = False

        for name, val in self._last_temps.items():
            if self._ensure_temp_series(name):
                temp_legend_dirty = True

            if self.temp_curves.get(name) is None:
                if name in self.temp_checkboxes and self.temp_checkboxes[name].isChecked():
                    color = COLORS[len([k for k in self.temp_curves if self.temp_curves[k] is not None]) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self.temp_curves[name] = self.temp_chart.plot(pen=pen, name=name)
                    temp_legend_dirty = True

            _append_limit(self.temp_y[name], val, self.max_points)
            if self.temp_curves.get(name) is not None:
                checked = name in self.temp_checkboxes and self.temp_checkboxes[name].isChecked()
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
            checked = name in self.freq_checkboxes and self.freq_checkboxes[name].isChecked()
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
    # FPS 统计（bisect 优化 O(log n)）
    # ═══════════════════════════════════════════

    def _update_fps_stats(self, fps: float, t: float) -> None:
        bisect.insort(self._fps_sorted, fps)
        # 每 100 次才截断一次，避免每次都创建新 list
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
    # 自动滚动（动态时间轴）
    # ═══════════════════════════════════════════

    def _update_auto_scroll(self, t: float) -> None:
        if not self._auto_scroll or len(self.fps_x) == 0:
            return
        latest_t = self.fps_x[-1]
        # 动态时间轴：数据超过 WINDOW_SECONDS 后自动扩展
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
        if not self.monitor_started:
            return
        self._last_temps = temps
        self._temp_updated = True

    def _on_freq(self, t: float, freqs: dict) -> None:
        if not self.monitor_started:
            return
        self._last_freqs = freqs
        self._freq_updated = True

        if "CPU负载(%)" in freqs:
            self.card_cpu_load.update_value(str(freqs["CPU负载(%)"]))
        if "GPU负载(%)" in freqs:
            self.card_gpu_load.update_value(str(freqs["GPU负载(%)"]))

        self._sync_core_usage(t, freqs)
        self._sync_core_freq(t, freqs)

    def _sync_core_usage(self, t: float, freqs: dict) -> None:
        """同步单核 CPU 负载图表"""
        for k, v in freqs.items():
            if k.startswith("CPU") and k.endswith("(%)") and k != "CPU负载(%)":
                label = k.replace("CPU", "Core").replace("(%)", "")
                if label not in self._core_usage_curves:
                    color = COLORS[len(self._core_usage_curves) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self._core_usage_curves[label] = self.core_usage_chart.plot(pen=pen, name=label)
                    self._core_usage_checkboxes[label] = True
                    self._add_core_checkbox(label, True, self.core_usage_chart,
                                            self._core_usage_curves, self._core_usage_checkboxes,
                                            self.core_usage_checkbox_container)
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
        """同步单核 CPU 频率图表"""
        core_freq_dirty = False
        for k, v in freqs.items():
            if k.startswith("Core") and k.endswith("(MHz)"):
                label = k.replace("(MHz)", "")
                if label not in self._core_freq_curves:
                    color = COLORS[len(self._core_freq_curves) % len(COLORS)]
                    pen = pg.mkPen(color, width=1.5)
                    self._core_freq_curves[label] = self.core_freq_chart.plot(pen=pen, name=label)
                    self._core_freq_checkboxes[label] = True
                    self._add_core_checkbox(label, True, self.core_freq_chart,
                                            self._core_freq_curves, self._core_freq_checkboxes,
                                            self.core_freq_checkbox_container)
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
        if not self.monitor_started:
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
        if not self.monitor_started:
            return
        self._last_net = net
        if "下行(KB/s)" in net:
            self.card_dl.update_value(str(net["下行(KB/s)"]))
        if "上行(KB/s)" in net:
            self.card_ul.update_value(str(net["上行(KB/s)"]))

    def _on_mem(self, t: float, mem: dict) -> None:
        if not self.monitor_started:
            return
        self._last_mem = mem
        if "GPU显存(MB)" in mem:
            self.card_gpu_mem.update_value(str(mem["GPU显存(MB)"]))
        if "PSS内存(MB)" in mem:
            self.card_pss.update_value(str(mem["PSS内存(MB)"]))

    # ═══════════════════════════════════════════
    # 录制
    # ═══════════════════════════════════════════

    def _toggle_recording(self, checked: bool) -> None:
        if checked:
            self.recorder.start(self._last_temps)
            self.btn_record.setText("⏹ 停止")
        else:
            self.recorder.stop()
            self.btn_record.setText("⏺ 录制")