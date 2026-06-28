"""UI 组件 — StatCard、CrosshairChart、FPSChart、TimeAxisWidget、DeviceInfoPanel、ChartPanel、SettingsPanel"""

import bisect

from PyQt6.QtWidgets import (QLabel, QWidget, QVBoxLayout, QHBoxLayout,
                              QGridLayout, QPushButton, QFrame, QCheckBox,
                              QComboBox, QScrollArea)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QElapsedTimer, QPointF
import pyqtgraph as pg


# ─── 常量 ──────────────────────────────

COLORS = ['#89b4fa', '#a6e3a1', '#f38ba8', '#f9e2af', '#cba6f7', '#fab387', '#94e2d5', '#89dceb']
WINDOW_SECONDS = 60  # 默认时间轴长度（秒）

HOVER_THROTTLE_MS = 33     # 鼠标悬停更新节流（~30fps）
TOOLTIP_OFFSET_X = 15      # 提示框水平偏移（px）
TOOLTIP_OFFSET_Y = 10      # 提示框垂直偏移（px）

pg.setConfigOptions(antialias=True, background='#1e1e2e', foreground='#cdd6f4')


# ─── StatCard ─────────────────────────

class StatCard(QLabel):
    """统计指标卡片"""
    def __init__(self, title: str, color: str = '#89b4fa'):
        super().__init__()
        self.title = title
        self.color = color
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(70)
        self.setStyleSheet("""
            QLabel {
                background: #313244;
                border-radius: 8px;
                padding: 8px;
                color: #cdd6f4;
            }
        """)
        self.update_value("--")

    def update_value(self, val: str) -> None:
        self.setText(
            f'<div style="font-size:11px;color:#a6adc8">{self.title}</div>'
            f'<div style="font-size:22px;font-weight:bold;color:{self.color}">{val}</div>'
        )


# ─── CrosshairChart ──────────────────

class CrosshairChart(pg.PlotWidget):
    """带十字线、悬停数据标签、右侧图例的图表基类"""

    sigMouseXChanged = pyqtSignal(float)
    sigMouseLeft = pyqtSignal()

    def __init__(self, title: str = "", y_label: str = "",
                 color: str = '#89b4fa', parent=None):
        super().__init__(parent)
        self.setTitle(title, color='#cdd6f4', size='13px')
        self.setLabel('left', y_label, color='#a6adc8')
        self.setLabel('bottom', '时间 (s)', color='#a6adc8')
        self.showGrid(x=True, y=True, alpha=0.2)
        left_axis = self.getAxis('left')
        left_axis.setPen('#45475a')
        left_axis.setStyle(
            tickTextWidth=30,
            autoExpandTextSpace=False,
            autoReduceTextSpace=False,
        )
        self.getAxis('bottom').setPen('#45475a')

        # ViewBox 边距（无额外缓冲，精确对齐）
        vb = self.getViewBox()
        vb.setDefaultPadding(0)
        vb.enableAutoRange(x=False, y=True)

        # 裁剪渲染：数据曲线不会超出绘图区域边界
        self.setClipToView(True)

        # PlotItem 右侧固定空白列（15px），避免 60s 刻度紧贴滚动条
        pi = self.getPlotItem()
        pi.setContentsMargins(0, 0, 0, 0)
        pi.layout.setColumnMinimumWidth(2, 0)
        pi.layout.setColumnStretchFactor(2, 0)
        pi.layout.setColumnFixedWidth(3, 15)

        # 右侧图例
        self._legend = self.addLegend(offset=(-5, 5))
        try:
            self._legend.anchor((1, 0), (1, 0))
            self._legend.setLabelTextColor('#a6adc8')
        except Exception:
            pass

        # 十字线（当前图表活跃）
        self._crosshair_v = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen('#585b70', width=1, style=Qt.PenStyle.DashLine)
        )
        self._crosshair_v.setVisible(False)
        self.addItem(self._crosshair_v)

        # 来自其他图表的参考线（淡色）
        self._ref_line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen('#45475a', width=1, style=Qt.PenStyle.DotLine)
        )
        self._ref_line.setVisible(False)
        self.addItem(self._ref_line)

        # 悬停数据标签（使用 QLabel 替代 TextItem，支持完整多行显示）
        self._tooltip_label = QLabel(self)
        self._tooltip_label.setStyleSheet("""
            QLabel {
                background: rgba(49, 50, 68, 220);
                color: #cdd6f4;
                border: 1px solid #585b70;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
                font-family: Consolas, monospace;
            }
        """)
        self._tooltip_label.setWordWrap(True)
        self._tooltip_label.hide()
        self._tooltip_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # 鼠标追踪（节流：每 33ms 最多更新一次，约 30fps）
        self._hover_timer = QElapsedTimer()
        self._hover_timer.start()
        self._last_hover_x = None
        self._cached_plot_items: list | None = None
        self.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # 拖拽平移模式：X 轴拖拽平移，禁用 Y 轴
        self.setMouseEnabled(x=True, y=False)
        self.getViewBox().setMouseMode(pg.ViewBox.PanMode)

    def _get_plot_items(self) -> list:
        """缓存 PlotDataItem 列表，避免每帧重建"""
        if self._cached_plot_items is None:
            self._cached_plot_items = [
                item for item in self.items()
                if isinstance(item, pg.PlotDataItem)
            ]
        return self._cached_plot_items

    def invalidate_items_cache(self) -> None:
        """在添加/移除曲线后调用"""
        self._cached_plot_items = None

    def addItem(self, item, *args, **kwargs):
        """重写 addItem 以在添加项目时自动失效缓存"""
        super().addItem(item, *args, **kwargs)
        if isinstance(item, pg.PlotDataItem):
            self._cached_plot_items = None

    def _on_mouse_moved(self, pos) -> None:
        vb = self.getViewBox()
        if vb is None:
            return

        # 节流：限制更新频率为约 30fps
        if self._hover_timer.elapsed() < HOVER_THROTTLE_MS:
            return
        self._hover_timer.restart()

        mouse_point = vb.mapSceneToView(pos)
        self._last_hover_x = mouse_point.x()

        self._update_crosshair_and_tooltip(vb, mouse_point)

    def _update_crosshair_and_tooltip(self, vb, mouse_point) -> None:
        """更新十字线和 tooltip（供鼠标移动和传感器切换共用）"""
        x = mouse_point.x()

        self._crosshair_v.setPos(x)
        self._crosshair_v.setVisible(True)
        self.sigMouseXChanged.emit(x)

        # 构建悬停提示：直接读取图表中所有可见 PlotDataItem 的数据
        lines = [f"t = {x:.1f}s"]
        for item in self._get_plot_items():
            if not item.isVisible():
                continue
            nm = item.name()
            if not nm:
                continue
            data = item.getData()
            if data is None or data[0] is None or len(data[0]) == 0:
                continue
            xdata, ydata = data
            if len(xdata) == 0:
                continue
            # 二分查找最近的 X 坐标
            idx = bisect.bisect_left(xdata, x)
            best_idx = idx
            if idx > 0:
                if idx >= len(xdata):
                    best_idx = len(xdata) - 1
                elif abs(xdata[idx - 1] - x) <= abs(xdata[idx] - x):
                    best_idx = idx - 1
            if best_idx < len(ydata):
                val = ydata[best_idx]
                if isinstance(val, float):
                    val = f"{val:.1f}"
                lines.append(f"{nm}: {val}")

        if len(lines) > 1:
            # 转换为 HTML 多行显示
            html = '<br>'.join(lines)
            self._tooltip_label.setText(html)
            self._tooltip_label.adjustSize()
            # 将图表坐标转换为 widget 像素坐标定位 QLabel
            scene_pos = vb.mapViewToScene(mouse_point)
            widget_pos = self.mapFromScene(scene_pos)
            # 在鼠标右上方偏移
            label_w = self._tooltip_label.width()
            label_h = self._tooltip_label.height()
            tx = widget_pos.x() + TOOLTIP_OFFSET_X
            ty = widget_pos.y() - label_h - TOOLTIP_OFFSET_Y
            # 确保不超出 widget 边界
            if tx + label_w > self.width():
                tx = widget_pos.x() - label_w - TOOLTIP_OFFSET_X
            if ty < 0:
                ty = widget_pos.y() + 15
            self._tooltip_label.move(tx, ty)
            self._tooltip_label.show()
            self._tooltip_label.raise_()
        else:
            self._tooltip_label.hide()

    def refresh_tooltip(self) -> None:
        """传感器切换后刷新 tooltip（使用上次鼠标位置重新计算）"""
        if self._last_hover_x is None:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        mouse_point = QPointF(self._last_hover_x, 0)
        self._cached_plot_items = None
        self._update_crosshair_and_tooltip(vb, mouse_point)

    def show_ref_at(self, x: float) -> None:
        """显示参考十字线（来自其他图表联动）"""
        self._ref_line.setPos(x)
        self._ref_line.setVisible(True)

    def hide_ref(self) -> None:
        self._ref_line.setVisible(False)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._crosshair_v.setVisible(False)
        self._tooltip_label.hide()
        self.sigMouseLeft.emit()

    def wheelEvent(self, event) -> None:
        """禁用图表滚轮缩放，将事件传递给父容器（QScrollArea）实现滚动翻页"""
        event.ignore()


# ─── FPSChart ─────────────────────────

class FPSChart(CrosshairChart):
    """FPS 曲线图（带十字线 + Jank 标记）"""

    def __init__(self, title: str = "FPS", y_label: str = "FPS",
                 color: str = '#89b4fa', jank_threshold: int = 25):
        super().__init__(title, y_label, color)
        self.jank_threshold = jank_threshold

        self.curve = self.plot(pen=pg.mkPen(color, width=2), name='FPS')
        self.avg_curve = self.plot(
            pen=pg.mkPen('#f9e2af', width=1, style=Qt.PenStyle.DashLine), name='平均'
        )
        self.jank_bars = pg.BarGraphItem(
            x=[], height=[], width=0.8, pen=pg.mkPen('#f38ba8', width=0.5),
            brush=pg.mkBrush('#f38ba880'), name='Jank'
        )
        self.addItem(self.jank_bars)
        self.fill = None

    def update_data(self, x: list, y: list, avg: float,
                    jank_x: list | None = None, jank_y: list | None = None) -> None:
        self.curve.setData(x, y)
        self.avg_curve.setData(x, [avg] * len(x) if avg else [])
        if jank_x is not None and jank_y is not None:
            bx, by = jank_x, jank_y
        else:
            bx = [x[i] for i in range(len(y)) if y[i] < self.jank_threshold]
            by = [y[i] for i in range(len(y)) if y[i] < self.jank_threshold]
        if bx:
            self.jank_bars.setOpts(x=bx, height=by, width=0.8)
        else:
            self.jank_bars.setOpts(x=[], height=[], width=0.8)
        if len(y) > 0:
            self.setYRange(0, max(max(y) + 10, 30))


# ─── TimeAxisWidget ───────────────────

class TimeAxisWidget(pg.PlotWidget):
    """底部时间轴导航条 — LinearRegionItem 拖拽选择时间范围"""

    regionChanged = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("时间轴导航", color='#585b70', size='10px')
        self.setLabel('bottom', '时间 (s)', color='#a6adc8')
        self.setFixedHeight(120)
        self.showGrid(x=True, y=False, alpha=0.1)
        self.getAxis('left').setVisible(False)
        self.getAxis('bottom').setPen('#45475a')
        self.setMouseEnabled(x=False, y=False)
        self.enableAutoRange(x=False, y=True)  # Y 轴自动调整以显示曲线

        # 缩略 FPS 曲线
        self._mini_curve = self.plot(pen=pg.mkPen('#585b70', width=1))

        # 拖拽选区
        self._region = pg.LinearRegionItem(
            values=[0, WINDOW_SECONDS],
            brush=pg.mkBrush(137, 180, 250, 40),
            pen=pg.mkPen('#89b4fa', width=1),
            movable=True
        )
        self._region.sigRegionChanged.connect(self._on_region_changed)
        self.addItem(self._region)

        self._updating = False
        self._total_seconds = WINDOW_SECONDS

        # 初始范围
        self.enableAutoRange(x=False, y=False)
        self.setXRange(0, WINDOW_SECONDS, padding=0)
        self.setYRange(0, 1)

    def _on_region_changed(self) -> None:
        if self._updating:
            return
        mn, mx = self._region.getRegion()
        self.regionChanged.emit(mn, mx)

    def set_region(self, start: float, end: float) -> None:
        """更新选区（来自图表同步）"""
        if self._updating:
            return
        self._updating = True
        self._region.setRegion([start, end])
        self._updating = False

    def update_overview(self, x: list, y: list, total: float) -> None:
        """更新缩略曲线"""
        if total > self._total_seconds:
            self._total_seconds = total
        new_range = max(self._total_seconds, WINDOW_SECONDS)
        # 先设数据
        self._mini_curve.setData(x, y)
        # 更新选区边界
        self._region.setBounds([0, new_range])
        # 设置 X 轴范围，Y 轴自动适配曲线高度
        self.enableAutoRange(x=False, y=True)
        self.setXRange(0, new_range, padding=0)
        # 更新限制
        self.setLimits(xMin=0, xMax=new_range)



# ─── DeviceInfoPanel ────────────────

class DeviceInfoPanel(QWidget):
    """左侧设备信息面板 — 设备选择 + 信息展示 + 控制按钮"""

    device_selected = pyqtSignal(int)
    start_pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    save_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(200)
        self.setStyleSheet("background: #181825;")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(0)

        # ─── 设备选择 ───
        self._lbl_title = QLabel(" 设备")
        self._lbl_title.setStyleSheet("color: #89b4fa; font-size: 14px; font-weight: bold; padding: 4px 0;")
        self._layout.addWidget(self._lbl_title)
        self._add_sep()

        row = QHBoxLayout()
        row.setSpacing(4)
        self.device_combo = QComboBox()
        self.device_combo.setStyleSheet("""
            QComboBox { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                        border-radius: 4px; padding: 4px 8px; font-size: 12px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #313244; color: #cdd6f4;
                                          selection-background-color: #45475a; }
        """)
        self.device_combo.currentIndexChanged.connect(self.device_selected.emit)
        row.addWidget(self.device_combo, stretch=1)
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedSize(28, 28)
        self.btn_refresh.setStyleSheet("""
            QPushButton { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                          border-radius: 4px; font-size: 14px; }
            QPushButton:hover { background: #45475a; }
        """)
        row.addWidget(self.btn_refresh)
        self._layout.addLayout(row)
        self._layout.addSpacing(8)

        # ─── 设备信息 ───
        self._lbl_device = self._add_section("设备", "等待选择...")
        self._lbl_android = self._add_section("系统", "—")
        self._lbl_soc = self._add_section("SoC", "—")
        self._lbl_cpu = self._add_section("CPU", "—")
        self._lbl_gpu = self._add_section("GPU", "—")
        self._lbl_ram = self._add_section("内存", "—")

        self._layout.addStretch()

        # ─── 控制按钮 ───
        self._add_sep()
        self._layout.addSpacing(6)

        self.btn_start = QPushButton("▶ 开始")
        self.btn_start.setFixedHeight(36)
        self.btn_start.setStyleSheet("""
            QPushButton { background: #a6e3a1; color: #1e1e2e; font-size: 13px; font-weight: bold;
                          border-radius: 6px; padding: 4px 12px; }
            QPushButton:hover { background: #94e2d5; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
        """)
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self.start_pause_clicked.emit)
        self._layout.addWidget(self.btn_start)

        self._layout.addSpacing(4)

        self.btn_stop = QPushButton("⏹ 结束")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setStyleSheet("""
            QPushButton { background: #313244; color: #f38ba8; font-size: 13px; font-weight: bold;
                          border-radius: 6px; padding: 4px 12px; border: 2px solid #45475a; }
            QPushButton:hover { background: #45475a; }
            QPushButton:disabled { background: #313244; color: #6c7086; border-color: #313244; }
        """)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)
        self._layout.addWidget(self.btn_stop)

        self._layout.addSpacing(4)

        self.btn_save = QPushButton("⏺ 录制")
        self.btn_save.setFixedHeight(36)
        self.btn_save.setStyleSheet("""
            QPushButton { background: #313244; color: #a6e3a1; font-size: 13px; font-weight: bold;
                          border-radius: 6px; padding: 4px 12px; border: 2px solid #45475a; }
            QPushButton:hover { background: #45475a; }
            QPushButton:disabled { background: #313244; color: #6c7086; border-color: #313244; }
        """)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_clicked.emit)
        self._layout.addWidget(self.btn_save)

        self._layout.addSpacing(4)

        self.btn_settings = QPushButton("⚙ 设置")
        self.btn_settings.setFixedHeight(36)
        self.btn_settings.setStyleSheet("""
            QPushButton { background: #313244; color: #cdd6f4; font-size: 13px; font-weight: bold;
                          border-radius: 6px; padding: 4px 12px; border: 2px solid #45475a; }
            QPushButton:hover { background: #45475a; }
        """)
        self._layout.addWidget(self.btn_settings)

    def _add_sep(self) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #313244;")
        self._layout.addWidget(line)

    def _add_section(self, title: str, value: str) -> QLabel:
        t = QLabel(title)
        t.setStyleSheet("color: #585b70; font-size: 11px; padding: 0;")
        self._layout.addWidget(t)
        v = QLabel(value)
        v.setStyleSheet("color: #cdd6f4; font-size: 13px; padding: 0 0 2px 0;")
        v.setWordWrap(True)
        self._layout.addWidget(v)
        self._layout.addSpacing(10)
        return v

    def set_devices(self, devices: list[str]) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in devices:
            self.device_combo.addItem(d)
        self.device_combo.blockSignals(False)
        self.btn_start.setEnabled(len(devices) > 0)

    def update_info(self, info: dict) -> None:
        brand = info.get("brand", "")
        model = info.get("model", "")
        device = info.get("device", "")
        display_name = f"{brand} {model}".strip()
        if device and device != model:
            display_name += f"\n({device})"
        self._lbl_device.setText(display_name or "未知")
        android = info.get("android", "?")
        sdk = info.get("sdk", "")
        self._lbl_android.setText(f"Android {android}" + (f" (API {sdk})" if sdk else ""))
        self._lbl_soc.setText(info.get("soc", "") or info.get("platform", "") or "未知")
        self._lbl_cpu.setText(info.get("cpu_text", "") or "未知")
        self._lbl_gpu.setText(info.get("gpu", "") or "未知")
        self._lbl_ram.setText(info.get("ram_text", "") or "未知")

    def set_start_state(self, state: str) -> None:
        """state: 'ready' / 'running' / 'paused' / 'stopped'"""
        if state == "ready":
            self.btn_start.setText("▶ 开始")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_save.setText("⏺ 录制")
            self.btn_save.setEnabled(False)
            self.device_combo.setEnabled(True)
            self.btn_refresh.setEnabled(True)
        elif state == "running":
            self.btn_start.setText("⏸ 暂停")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(True)
            self.btn_save.setEnabled(True)
            self.device_combo.setEnabled(False)
            self.btn_refresh.setEnabled(False)
        elif state == "paused":
            self.btn_start.setText("▶ 继续")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(True)
            self.btn_save.setEnabled(True)
        elif state == "stopped":
            self.btn_start.setText("▶ 开始")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.btn_save.setText("💾 保存数据")
            self.btn_save.setEnabled(True)
            self.device_combo.setEnabled(True)
            self.btn_refresh.setEnabled(True)


# ─── ChartPanel ─────────────────────

class ChartPanel(QFrame):
    """带居中标题的图表面板容器"""

    def __init__(self, title: str, chart: QWidget, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(0)

        # 居中标题
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("color: #cdd6f4; font-size: 13px; font-weight: bold; border: none;")
        layout.addWidget(title_label)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #313244; border: none;")
        layout.addWidget(sep)

        # 图表
        layout.addWidget(chart, stretch=1)


# ─── SettingsPanel ──────────────────

class SettingsPanel(QWidget):
    """独立传感器选择面板 — 非模态浮动窗口，通过工具栏按钮 toggle"""

    TEMP_IMPORTANT = {"CPU", "GPU", "表面", "电池", "CPU大核", "CPU中核", "CPU小核"}
    TEMP_GRID_COLS = 3  # 温度复选框网格列数

    # 信号：(类型, 名称, 状态)  类型: "temp"/"freq"/"core_usage"/"core_freq"
    checkbox_changed = pyqtSignal(str, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("设置")
        self.setMinimumSize(360, 500)
        self.resize(400, 700)
        self.setStyleSheet("background: #181825; color: #cdd6f4;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(0)

        # 标题
        self._add_label(outer, "⚙ 传感器选择",
                        "color: #89b4fa; font-size: 14px; font-weight: bold; padding: 4px 0;")
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #313244;"); outer.addWidget(sep)
        outer.addSpacing(8)

        # 可滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: #181825; border: none; }")
        scroll_widget = QWidget()
        scroll_widget.setStyleSheet("background: #181825;")
        self._layout = QVBoxLayout(scroll_widget)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # ─── 温度 ───
        self._add_label(self._layout, "🌡 温度传感器",
                        "color: #f38ba8; font-weight: bold; font-size: 13px; padding: 4px 0;")
        self._temp_grid = QGridLayout()
        self._temp_grid.setSpacing(2)
        self._layout.addLayout(self._temp_grid)
        self._temp_checkboxes: dict[str, QCheckBox] = {}
        self._layout.addSpacing(8)

        # ─── CPU/GPU 频率 ───
        self._add_label(self._layout, "⚡ CPU/GPU 频率",
                        "color: #a6e3a1; font-weight: bold; font-size: 13px; padding: 8px 0 4px;")
        self._freq_grid = QGridLayout()
        self._freq_grid.setSpacing(2)
        self._layout.addLayout(self._freq_grid)
        self._freq_checkboxes: dict[str, QCheckBox] = {}
        self._layout.addSpacing(8)

        # ─── 单核 CPU 负载 ───
        self._add_label(self._layout, "📊 单核 CPU 负载",
                        "color: #f38ba8; font-weight: bold; font-size: 13px; padding: 8px 0 4px;")
        self._core_usage_grid = QGridLayout()
        self._core_usage_grid.setSpacing(2)
        self._layout.addLayout(self._core_usage_grid)
        self._layout.addSpacing(8)

        # ─── 单核 CPU 频率 ───
        self._add_label(self._layout, "📈 单核 CPU 频率",
                        "color: #a6e3a1; font-weight: bold; font-size: 13px; padding: 8px 0 4px;")
        self._core_freq_grid = QGridLayout()
        self._core_freq_grid.setSpacing(2)
        self._layout.addLayout(self._core_freq_grid)

        self._layout.addStretch()
        scroll.setWidget(scroll_widget)
        outer.addWidget(scroll, stretch=1)

    def _add_label(self, layout, text: str, style: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet(style)
        layout.addWidget(lbl)

    # ─── 排序辅助 ───

    @staticmethod
    def _temp_sort_key(name: str) -> tuple:
        """温度传感器排序：CPU → GPU → SoC → NPU → 电池 → 表面 → 内存 → WiFi → 相机 → PMIC → 充电 → 射频 → 其他"""
        prefix_order = [
            ("CPU", 0), ("GPU", 1), ("SoC", 2), ("AOSS", 2), ("NPU", 3),
            ("电池", 4), ("表面", 5), ("内存", 6), ("WiFi", 7),
            ("相机", 8), ("PMIC", 9), ("充电", 10), ("射频", 11),
        ]
        for prefix, priority in prefix_order:
            if name.startswith(prefix):
                return (priority, name)
        return (99, name)

    @staticmethod
    def _freq_sort_key(name: str) -> tuple:
        """频率传感器排序：CPU 策略 → GPU → 其他"""
        if name.startswith("CPU"):
            return (0, name)
        if name.startswith("GPU"):
            return (1, name)
        return (99, name)

    def _rebuild_grid(self, grid: QGridLayout, checkboxes: dict, sort_key) -> None:
        """清空网格并按排序重新添加所有复选框"""
        for name in sorted(checkboxes, key=sort_key):
            grid.removeWidget(checkboxes[name])
        for i, name in enumerate(sorted(checkboxes, key=sort_key)):
            row, col = divmod(i, self.TEMP_GRID_COLS)
            grid.addWidget(checkboxes[name], row, col)

    # ─── 温度复选框（3 列网格） ───

    def add_temp_checkbox(self, name: str) -> QCheckBox:
        if name in self._temp_checkboxes:
            return self._temp_checkboxes[name]
        cb = QCheckBox(name)
        cb.setChecked(name in self.TEMP_IMPORTANT)
        cb.setStyleSheet("color: #cdd6f4; font-size: 12px; padding: 2px 0 2px 8px;")
        cb.stateChanged.connect(lambda state, n=name: self.checkbox_changed.emit("temp", n, state))
        self._temp_checkboxes[name] = cb
        self._rebuild_grid(self._temp_grid, self._temp_checkboxes, self._temp_sort_key)
        return cb

    def is_temp_checked(self, name: str) -> bool:
        return name in self._temp_checkboxes and self._temp_checkboxes[name].isChecked()

    # ─── 频率复选框（3 列网格） ───

    def add_freq_checkbox(self, name: str) -> QCheckBox:
        if name in self._freq_checkboxes:
            return self._freq_checkboxes[name]
        cb = QCheckBox(name)
        cb.setChecked(True)
        cb.setStyleSheet("color: #cdd6f4; font-size: 12px; padding: 2px 0 2px 8px;")
        cb.stateChanged.connect(lambda state, n=name: self.checkbox_changed.emit("freq", n, state))
        self._freq_checkboxes[name] = cb
        self._rebuild_grid(self._freq_grid, self._freq_checkboxes, self._freq_sort_key)
        return cb

    def is_freq_checked(self, name: str) -> bool:
        return name in self._freq_checkboxes and self._freq_checkboxes[name].isChecked()

    # ─── 单核复选框（CPU 负载 / CPU 频率） ───

    def add_core_checkbox(self, name: str, checked: bool,
                          kind: str, on_toggle) -> QCheckBox:
        """kind: 'core_usage' 或 'core_freq'"""
        cb = QCheckBox(name)
        cb.setChecked(checked)
        cb.setStyleSheet("color: #cdd6f4; font-size: 12px; padding: 2px 0 2px 8px;")
        cb.stateChanged.connect(lambda state, n=name: on_toggle(n, state))
        grid = self._core_usage_grid if kind == "core_usage" else self._core_freq_grid
        count = grid.count()
        row = count // self.TEMP_GRID_COLS
        col = count % self.TEMP_GRID_COLS
        grid.addWidget(cb, row, col)
        return cb
