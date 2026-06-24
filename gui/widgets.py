"""UI 组件 — StatCard、CrosshairChart、FPSChart、TimeAxisWidget、LoadingOverlay"""

import sys
import bisect

from PyQt6.QtWidgets import (QLabel, QWidget, QVBoxLayout, QPushButton)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QElapsedTimer
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


# ─── Loading Overlay ─────────────────

class LoadingOverlay(QWidget):
    """启动加载遮罩层（主窗口内部子组件）"""

    cancelled = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet("background: #1e1e2e;")
        if parent:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title_label = QLabel("ADB FPS Monitor")
        self.title_label.setStyleSheet("color: #89b4fa; font-size: 24px; font-weight: bold;")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)

        self.device_label = QLabel("正在获取设备信息...")
        self.device_label.setStyleSheet("color: #a6adc8; font-size: 14px; padding: 8px;")
        self.device_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.device_label.setWordWrap(True)
        layout.addWidget(self.device_label)

        self.status = QLabel("正在连接设备...")
        self.status.setStyleSheet("color: #a6adc8; font-size: 14px; padding: 20px;")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.dots = QLabel("● ○ ○ ○")
        self.dots.setStyleSheet("color: #89b4fa; font-size: 16px;")
        self.dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.dots)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setFixedSize(100, 36)
        self.btn_cancel.setStyleSheet("""
            QPushButton { background: #45475a; color: #cdd6f4; font-size: 13px;
                          border-radius: 6px; padding: 4px 16px; border: 1px solid #585b70; }
            QPushButton:hover { background: #585b70; }
        """)
        self.btn_cancel.clicked.connect(self.cancelled.emit)
        layout.addWidget(self.btn_cancel, alignment=Qt.AlignmentFlag.AlignCenter)

        self._dot_idx = 0
        self._dot_timer = QTimer()
        self._dot_timer.timeout.connect(self._animate_dots)
        self._dot_timer.start(400)

    def _animate_dots(self) -> None:
        patterns = ["● ○ ○ ○", "○ ● ○ ○", "○ ○ ● ○", "○ ○ ○ ●"]
        self._dot_idx = (self._dot_idx + 1) % len(patterns)
        self.dots.setText(patterns[self._dot_idx])

    def update_device_info(self, info: str) -> None:
        self.device_label.setText(info)

    def update_status(self, msg: str) -> None:
        self.status.setText(msg)

    def finish(self) -> None:
        self._dot_timer.stop()
        self.hide()
        self.deleteLater()
