"""Fluent 风格的通用控件 / 对话框封装。

集中存放用于替换原生 Qt 控件的 qfluentwidgets 封装，使其在深色模式下
（尤其 Win10）也能被 qfluentwidgets 主题正确接管，不再退化为系统原生外观。

- ``FluentGroupBox``：替代原生 ``QGroupBox``（qfluentwidgets 无 GroupBox，
  这里用受主题管理的 ``SimpleCardWidget`` + 标题实现）。
- ``RangeSlider``：双柄范围滑块（qfluentwidgets 社区版无此控件，自绘）。
- ``ThemedMenuLineEdit`` / ``ThemedMenuTextEdit``：原生 ``QLineEdit`` /
  ``QTextEdit`` 换用 qfluentwidgets 主题右键菜单（原生菜单不跟主题，
  深色模式下仍是系统白色弹窗）。
- ``themed_get_open_file_name`` 等 4 个文件弹窗包装：Windows 上改用 Qt
  自绘文件弹窗（原生 IFileDialog 无法跟随应用主题），由主题过滤器
  （theme._DialogTitleBarThemeFilter）在显示时套上随主题的样式表；
  其它平台保持原生弹窗。
- ``message_info`` / ``message_warning`` / ``message_error`` / ``message_question``：
  替代 ``QMessageBox`` 的常见用法，内部使用 qfluentwidgets ``MessageBox``。
"""

from __future__ import annotations

import math
import sys
from typing import Callable, Optional, Sequence

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Dialog,
    LineEditMenu,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    FluentIconBase,
    isDarkTheme,
    themeColor,
)
# TextEditMenu 未在包顶层导出，按库内部用法从子模块导入。
from qfluentwidgets.components.widgets.menu import TextEditMenu

from strange_uta_game.frontend.theme import theme


class ThemedMenuLineEdit(QLineEdit):
    """原生 ``QLineEdit``，右键菜单换成 qfluentwidgets 主题菜单。

    适用于必须保持原生外观/布局、只缺主题化右键菜单的输入框
    （qfluentwidgets 自带的 ``LineEdit`` 可直接用时优先用后者）。
    """

    def contextMenuEvent(self, event):
        LineEditMenu(self).exec(event.globalPos())


class ThemedMenuTextEdit(QTextEdit):
    """原生 ``QTextEdit``，右键菜单换成 qfluentwidgets 主题菜单。

    只读预览同样适用（``TextEditMenu`` 会按 ``isReadOnly`` 收窄菜单项）。
    ``QPlainTextEdit`` 场景请直接在子类里用 ``TextEditMenu`` 重写
    ``contextMenuEvent``（见 fulltext_interface.LineNumberPlainTextEdit）。
    """

    def contextMenuEvent(self, event):
        TextEditMenu(self).exec(event.globalPos())


# ── 主题化文件弹窗 ──────────────────────────────────────────────────────────
# Windows 原生文件弹窗（IFileDialog）由系统按系统主题绘制，无 API 强制跟随
# 应用主题；Qt 自绘弹窗（DontUseNativeDialog）靠调色板/QSS 渲染，可被主题
# 接管——QSS 由 theme._DialogTitleBarThemeFilter 在弹窗 Show 时自动套上，
# 标题栏明暗也由同一过滤器同步。macOS 保持原生（file_loader 对 NSOpenPanel
# 有平台特定处理）。


def _themed_file_dialog_options() -> QFileDialog.Option:
    """文件弹窗选项：Windows 自绘（可主题化），其它平台原生。"""
    if sys.platform == "win32":
        return QFileDialog.Option.DontUseNativeDialog
    return QFileDialog.Option(0)


def themed_get_open_file_name(parent=None, caption="", directory="", filter=""):
    return QFileDialog.getOpenFileName(
        parent, caption, directory, filter, "", _themed_file_dialog_options()
    )


def themed_get_open_file_names(parent=None, caption="", directory="", filter=""):
    return QFileDialog.getOpenFileNames(
        parent, caption, directory, filter, "", _themed_file_dialog_options()
    )


def themed_get_save_file_name(parent=None, caption="", directory="", filter=""):
    return QFileDialog.getSaveFileName(
        parent, caption, directory, filter, "", _themed_file_dialog_options()
    )


def themed_get_existing_directory(parent=None, caption="", directory=""):
    return QFileDialog.getExistingDirectory(
        parent, caption, directory, _themed_file_dialog_options()
    )


class _WorkspaceSwitchItem(QAbstractButton):
    """药丸分段切换器中的单个自绘文字项。"""

    def __init__(
        self,
        text: str,
        icon: Optional[FluentIconBase],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._icon = icon
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        width = 36 + self.fontMetrics().horizontalAdvance(self._text)
        if self._icon is not None:
            width += 21
        return QSize(max(96, width), 26)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        checked = self.isChecked()
        foreground = QColor("#FFFFFF") if checked else theme.text_secondary
        if not checked and self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 24 if theme.is_dark else 160))
            painter.drawRoundedRect(QRectF(self.rect()), self.height() / 2, self.height() / 2)
            foreground = theme.text_primary

        text_width = self.fontMetrics().horizontalAdvance(self._text)
        content_width = text_width + (21 if self._icon is not None else 0)
        x = (self.width() - content_width) / 2
        if self._icon is not None:
            rect = QRectF(x, (self.height() - 15) / 2, 15, 15).toRect()
            self._icon.icon(color=foreground).paint(
                painter, rect, Qt.AlignmentFlag.AlignCenter,
                QIcon.Mode.Normal, QIcon.State.On,
            )
            x += 21

        font = QFont(self.font())
        if checked:
            font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(foreground)
        painter.drawText(
            QRectF(x, 0, text_width + 2, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._text,
        )


class WorkspaceSwitcher(QWidget):
    """与工作台同款的浅灰轨道 + 主色药丸分段切换器。"""

    currentItemChanged = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setAccessibleName(self.tr("工作区切换"))
        self._items: dict[str, _WorkspaceSwitchItem] = {}
        self._current_key: Optional[str] = None
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._thumb_from = QRectF()
        self._thumb_to = QRectF()
        self._thumb_progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(180)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_animation_value)
        # 所有颜色均从 SUG ThemeManager 实时读取；主题切换后主动重绘，
        # 保证深色/浅色模式无需再次悬停或切换选项就立即更新。
        theme.changed.connect(self._on_theme_changed)
        try:
            from qfluentwidgets.common.config import qconfig

            qconfig.themeColorChanged.connect(self._on_theme_changed)
        except Exception:
            pass
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

    def addItem(
        self,
        routeKey: str,
        text: str,
        onClick: Optional[Callable[[bool], None]] = None,
        icon: Optional[FluentIconBase] = None,
    ) -> _WorkspaceSwitchItem:
        if routeKey in self._items:
            return self._items[routeKey]
        item = _WorkspaceSwitchItem(text, icon, self)
        if onClick is not None:
            item.clicked.connect(onClick)
        item.clicked.connect(lambda _checked=False, key=routeKey: self.setCurrentItem(key))
        self.layout().addWidget(item)
        self._group.addButton(item)
        self._items[routeKey] = item
        if self._current_key is None:
            self._apply_current(routeKey, animate=False)
        return item

    def setItemText(self, routeKey: str, text: str) -> None:
        if routeKey in self._items:
            self._items[routeKey].setText(text)

    def setCurrentItem(self, routeKey: Optional[str]) -> None:
        if routeKey is None or routeKey not in self._items or routeKey == self._current_key:
            return
        self._apply_current(routeKey, animate=self.isVisible())

    def currentRouteKey(self) -> Optional[str]:
        return self._current_key

    def _apply_current(self, routeKey: str, *, animate: bool) -> None:
        previous_key = self._current_key
        self._current_key = routeKey
        self._items[routeKey].setChecked(True)
        target = QRectF(self._items[routeKey].geometry())
        if animate and previous_key is not None:
            self._thumb_from = self._current_thumb_rect()
            self._thumb_to = target
            self._animation.stop()
            self._animation.start()
        else:
            self._animation.stop()
            self._thumb_to = target
            self._thumb_progress = 1.0
        for item in self._items.values():
            item.update()
        self.update()
        self.currentItemChanged.emit(routeKey)

    def _current_thumb_rect(self) -> QRectF:
        if self._thumb_progress >= 1.0 or not self._thumb_from.isValid():
            return QRectF(self._thumb_to)
        t = self._thumb_progress
        a, b = self._thumb_from, self._thumb_to
        return QRectF(
            a.x() + (b.x() - a.x()) * t,
            a.y() + (b.y() - a.y()) * t,
            a.width() + (b.width() - a.width()) * t,
            a.height() + (b.height() - a.height()) * t,
        )

    def _on_animation_value(self, value) -> None:
        self._thumb_progress = float(value)
        self.update()

    def _on_theme_changed(self) -> None:
        for item in self._items.values():
            item.update()
        self.update()

    def _snap_thumb_to_current(self) -> None:
        if self._animation.state() == QAbstractAnimation.State.Running or self._current_key is None:
            return
        target = QRectF(self._items[self._current_key].geometry())
        if target != self._thumb_to or self._thumb_progress < 1.0:
            self._thumb_to = target
            self._thumb_progress = 1.0
            self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._snap_thumb_to_current()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._snap_thumb_to_current()

    def paintEvent(self, _event) -> None:
        self._snap_thumb_to_current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(theme.border_primary)
        painter.setBrush(theme.bg_secondary)
        painter.drawRoundedRect(track, self.height() / 2, self.height() / 2)
        thumb = self._current_thumb_rect()
        if thumb.isValid() and thumb.width() > 0:
            radius = thumb.height() / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 26))
            painter.drawRoundedRect(thumb.adjusted(0, 1, 0, 1.5), radius, radius)
            # SUG 的 Fluent 控件主题色由 main_window 统一设为 #FF6B6B；
            # 这里跟随 themeColor()，不能误用时间轴专用的青色 accent_primary。
            painter.setBrush(themeColor())
            painter.drawRoundedRect(thumb, radius, radius)


class FluentMessageBox(Dialog):
    """嵌入式兼容的 Fluent 消息对话框。

    改用 qfluentwidgets ``Dialog``（``FramelessDialog``，独立带框普通窗口），而非
    ``MessageBox``（``MaskDialogBase`` 遮罩式）：后者在嵌入式（SUG 作为子 widget
    挂在宿主里）下"对话框可见但点不动、点击只发系统禁止音"——遮罩 + 半透明顶层
    窗口拿不到前台 / 被宿主盖住，点击落到被模态屏蔽的宿主上；其遮罩定位在非最大化
    窗口下也会错位。``Dialog`` 是普通顶层窗口，且原生按父窗口居中。

    与 ``MessageBox`` 共享同一套 ``Ui_MessageBox`` 接口（yesButton / cancelButton /
    hideYesButton / hideCancelButton / setContentCopyable / buttonGroup /
    buttonLayout），故各 ``message_*`` 封装无需改动。
    """

    def __init__(self, title: str, content: str, parent: Optional[QWidget] = None):
        super().__init__(title, content, parent)
        # Dialog 顶部的 windowTitleLabel 与内容区 titleLabel 会重复显示标题，
        # 隐藏前者，外观与 MessageBox 一致。
        self.setTitleBarVisible(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)

    def _ensure_active(self) -> None:
        self.raise_()
        self.activateWindow()

    def showEvent(self, e):
        # QDialog.exec() 会在显示前临时恢复应用级模态，所以在 Show 事件中
        # 再次清除，保证该控件脱离 SUGApplication 使用时也不会屏蔽其他窗口。
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setModal(False)
        super().showEvent(e)
        # 只在首次显示时将新窗口带到前台，不持续争抢焦点。
        self._ensure_active()
        QTimer.singleShot(0, self._ensure_active)


class RangeSlider(QWidget):
    """双柄范围滑块（qfluentwidgets 社区版无此控件，自绘实现）。

    - 线性值域 ``[min_value, max_value]``（float）；调用方把值取对数即可
      得到对数刻度滑块（声谱「频率范围」：value = ln(Hz)）。
    - 拖动中持续发 ``rangeChanged``（供实时刷新数字），松手才发
      ``rangeCommitted``（供应用设置）——与其他滑条"拖动看数字、松手
      应用"的语义一致。
    - 两柄间保持最小间距（默认值域跨度的 5%），低柄恒 ≤ 高柄；点击
      轨道空白处吸附最近的柄到点击位置并进入拖动。
    - 仅鼠标交互（拖动/点击轨道）；外观：轨道两端 border 色、选中段
      themeColor，圆形手柄；禁用态整体降透明度。
    - 主题（明暗）切换经 ``theme.changed``、强调色变更经 qfluentwidgets
      ``qconfig.themeColorChanged`` 触发重绘——自绘控件不随 QSS 自动刷新，
      颜色在 paintEvent 现取现用。

    线程性：仅在 UI 线程使用。
    """

    rangeChanged = pyqtSignal(float, float)      # 拖动中（low, high）
    rangeCommitted = pyqtSignal(float, float)    # 松手（low, high）

    _HANDLE_R = 9         # 手柄外圈半径（px）；内圈 themeColor 半径 5
    _GROOVE_H = 4         # 轨道厚度（px）
    _PICK_RADIUS = 14     # 按下时判定手柄的命中半径（px）

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._min_value = 0.0
        self._max_value = 1.0
        self._low = 0.0
        self._high = 1.0
        self._dragging: Optional[str] = None
        self.setFixedHeight(22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(False)
        # 自绘控件不随 QSS 自动刷新：主题（明暗）切换与 qfluentwidgets 强调色
        # 变化都要显式重绘，颜色在 paintEvent 里现取现用（与 WaveformDisplay
        # 挂 theme.changed 的模式一致；themeColorChanged 覆盖单独改强调色）
        theme.changed.connect(self.update)
        try:
            from qfluentwidgets.common.config import qconfig

            qconfig.themeColorChanged.connect(self.update)
        except Exception:
            pass

    # ── 值域与取值 ──

    def set_range(self, min_value: float, max_value: float) -> None:
        """设置值域并复位两柄到端点（不发信号）。"""
        if max_value <= min_value:
            return
        self._min_value = float(min_value)
        self._max_value = float(max_value)
        self._low = self._min_value
        self._high = self._max_value
        self.update()

    def _min_span(self) -> float:
        return (self._max_value - self._min_value) * 0.05

    def set_low(self, value: float) -> None:
        self._set_handle("low", value)

    def set_high(self, value: float) -> None:
        self._set_handle("high", value)

    def low(self) -> float:
        return self._low

    def high(self) -> float:
        return self._high

    def set_values(self, low: float, high: float, emit: bool = False) -> None:
        """同时设置两柄（钳制到值域与最小间距）；emit=True 时发两个信号。"""
        lo = max(self._min_value, min(self._max_value, float(low)))
        hi = max(self._min_value, min(self._max_value, float(high)))
        if hi - lo < self._min_span():
            # 间距不足：以中点对撑到最小间距（仍钳在值域内）
            mid = (lo + hi) / 2.0
            half = self._min_span() / 2.0
            lo = max(self._min_value, mid - half)
            hi = min(self._max_value, lo + self._min_span())
            lo = hi - self._min_span()
        if lo == self._low and hi == self._high:
            return
        self._low, self._high = lo, hi
        self.update()
        if emit:
            self.rangeChanged.emit(self._low, self._high)
            self.rangeCommitted.emit(self._low, self._high)

    def _set_handle(self, which: str, value: float) -> None:
        value = max(self._min_value, min(self._max_value, float(value)))
        if which == "low":
            self._low = min(value, self._high - self._min_span())
            self._low = max(self._low, self._min_value)
        else:
            self._high = max(value, self._low + self._min_span())
            self._high = min(self._high, self._max_value)
        self.update()

    # ── 坐标换算 ──

    def _value_to_x(self, value: float) -> float:
        span = self._max_value - self._min_value
        frac = 0.0 if span <= 0 else (value - self._min_value) / span
        margin = self._HANDLE_R + 2.0
        return margin + frac * (self.width() - 2.0 * margin)

    def _x_to_value(self, x: float) -> float:
        margin = self._HANDLE_R + 2.0
        usable = max(1.0, self.width() - 2.0 * margin)
        frac = (x - margin) / usable
        frac = max(0.0, min(1.0, frac))
        return self._min_value + frac * (self._max_value - self._min_value)

    # ── 交互 ──

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        x = float(event.position().x())
        low_x, high_x = self._value_to_x(self._low), self._value_to_x(self._high)
        d_low, d_high = abs(x - low_x), abs(x - high_x)
        if d_low <= self._PICK_RADIUS and d_low <= d_high:
            self._dragging = "low"
            self._set_handle("low", self._x_to_value(x))
        elif d_high <= self._PICK_RADIUS:
            self._dragging = "high"
            self._set_handle("high", self._x_to_value(x))
        else:
            # 点轨道空白：吸附较近的柄到点击处再拖动
            self._dragging = "low" if d_low < d_high else "high"
            self._set_handle(self._dragging, self._x_to_value(x))
        self.rangeChanged.emit(self._low, self._high)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging is None:
            super().mouseMoveEvent(event)
            return
        self._set_handle(self._dragging, self._x_to_value(float(event.position().x())))
        self.rangeChanged.emit(self._low, self._high)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging is not None:
            self._dragging = None
            self.rangeCommitted.emit(self._low, self._high)
            return
        super().mouseReleaseEvent(event)

    # ── 绘制 ──

    def paintEvent(self, _event) -> None:
        """配色逐项对齐 qfluentwidgets Slider / SliderHandle（明暗两套）。

        - 轨道底色：深色 rgba(255,255,255,115) / 浅色 rgba(0,0,0,100)；
        - 选中段：themeColor；
        - 手柄外圈：深色 (69,69,69) / 浅色白（描边黑 90/25）；
          内圈 themeColor 半径 5。
        颜色在 paint 时现取（isDarkTheme/themeColor），主题切换经
        __init__ 挂的 theme.changed / themeColorChanged 触发重绘。
        禁用态整体降透明度（随所在 FluentGroupBox 一起被禁用时与
        qfluentwidgets 控件的变淡观感一致）。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2.0
        low_x, high_x = self._value_to_x(self._low), self._value_to_x(self._high)

        is_dark = isDarkTheme()
        enabled = self.isEnabled()
        groove = QColor(255, 255, 255, 115) if is_dark else QColor(0, 0, 0, 100)
        outer = QColor(69, 69, 69) if is_dark else QColor(255, 255, 255)
        outer_pen = QColor(0, 0, 0, 90 if is_dark else 25)
        accent = themeColor()
        if not enabled:
            groove.setAlpha(max(1, groove.alpha() // 2))
            outer_pen.setAlpha(max(1, outer_pen.alpha() // 2))
            accent = QColor(accent)
            accent.setAlpha(110)

        x_min, x_max = self._value_to_x(self._min_value), self._value_to_x(self._max_value)
        pen_track = QPen(groove, self._GROOVE_H)
        pen_track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_track)
        painter.drawLine(QPointF(x_min, cy), QPointF(x_max, cy))
        painter.setPen(QPen(accent, self._GROOVE_H, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(low_x, cy), QPointF(high_x, cy))

        # 手柄：外圈（同 SliderHandle）+ 内圈 themeColor
        for x in (low_x, high_x):
            painter.setPen(QPen(outer_pen, 1))
            painter.setBrush(outer)
            painter.drawEllipse(QPointF(x, cy), float(self._HANDLE_R), float(self._HANDLE_R))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(x, cy), 5.0, 5.0)


def make_message_box(
    parent: Optional[QWidget], title: str, content: str
) -> FluentMessageBox:
    """构建 Fluent 消息对话框（供各 message_* 封装与 winrt 引导复用）。"""
    return FluentMessageBox(title, content, _resolve_window(parent))


class FluentGroupBox(SimpleCardWidget):
    """受 qfluentwidgets 主题管理的"分组框"，替代原生 ``QGroupBox``。

    qfluentwidgets 不提供 GroupBox，而原生 QGroupBox 在 Win10 深色模式下标题
    会渲染为黑字、边框不跟随主题。``SimpleCardWidget`` 是受主题管理的卡片容器，
    深/浅色自动切换。本类在卡片顶部加一个标题标签，并暴露 ``contentLayout``
    供调用方添加内容。

    迁移方式：把
        gb = QGroupBox(title, parent)
        lay = QVBoxLayout(gb)
    改为
        gb = FluentGroupBox(title, parent)
        lay = gb.contentLayout
    其余 ``lay.addWidget(...)`` 调用保持不变。
    """

    def __init__(self, title: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rootLayout = QVBoxLayout(self)
        self._rootLayout.setContentsMargins(14, 10, 14, 12)
        self._rootLayout.setSpacing(8)

        self._titleLabel = StrongBodyLabel(title, self)
        self._rootLayout.addWidget(self._titleLabel)
        if not title:
            self._titleLabel.hide()

        # 内容布局：调用方往这里加控件（替代原 QVBoxLayout(group_box)）
        self.contentLayout = QVBoxLayout()
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(6)
        self._rootLayout.addLayout(self.contentLayout)

    def setTitle(self, text: str) -> None:
        self._titleLabel.setText(text)
        self._titleLabel.setVisible(bool(text))

    def title(self) -> str:
        return self._titleLabel.text()


def dialog_button_row(
    dialog: QDialog,
    *,
    ok_text: str = "确定",
    cancel_text: str = "取消",
) -> tuple[QLayout, PrimaryPushButton, PushButton]:
    """构建一行 Fluent 的"确定/取消"按钮，替代原生 ``QDialogButtonBox``。

    原生 QDialogButtonBox 内部是原生 QPushButton，在 Win10 深色模式下不跟随
    主题；改用 qfluentwidgets ``PrimaryPushButton`` / ``PushButton`` 受主题管理。

    Returns:
        (按钮行布局, 确定按钮, 取消按钮)。确定/取消已分别连到
        ``dialog.accept`` / ``dialog.reject``，调用方把布局加入对话框即可。
    """
    row = QHBoxLayout()
    row.addStretch(1)
    ok_btn = PrimaryPushButton(ok_text)
    cancel_btn = PushButton(cancel_text)
    ok_btn.clicked.connect(dialog.accept)
    cancel_btn.clicked.connect(dialog.reject)
    row.addWidget(ok_btn)
    row.addWidget(cancel_btn)
    return row, ok_btn, cancel_btn


def _resolve_window(parent: Optional[QWidget]) -> Optional[QWidget]:
    """把传入的父控件解析为其顶层窗口。

    Fluent 对话框需要一个顶层窗口作为定位锚点。这里：
    1. 优先返回传入控件的顶层窗口（让弹窗相对整窗居中）；
    2. parent 为 None 或无有效窗口时，回退到当前活动窗口 / 首个可见顶层窗口，
       避免 ``QMessageBox(None)`` 旧用法迁移后因 None parent 崩溃。
    """
    if parent is not None:
        try:
            win = parent.window()
            if win is not None:
                return win
        except Exception:
            pass

    app = QApplication.instance()
    if app is not None:
        active = app.activeWindow()
        if active is not None:
            return active
        for w in app.topLevelWidgets():
            if w.isVisible():
                return w
    return parent


def message_info(
    parent: Optional[QWidget],
    title: str,
    content: str,
    *,
    ok_text: str = "确定",
    copyable: bool = False,
) -> None:
    """信息提示（单个"确定"按钮）。替代 ``QMessageBox.information``。"""
    w = make_message_box(parent, title, content)
    w.yesButton.setText(ok_text)
    w.hideCancelButton()
    if copyable:
        w.setContentCopyable(True)
    w.exec()


# Fluent MessageBox 无 information/warning/critical 图标区分，三者外观一致；
# 保留独立函数名以表达语义并便于将来差异化。
message_warning = message_info
message_error = message_info


def message_question(
    parent: Optional[QWidget],
    title: str,
    content: str,
    *,
    yes_text: str = "确定",
    no_text: str = "取消",
    default_cancel: bool = False,
    copyable: bool = False,
) -> bool:
    """是/否确认。替代 ``QMessageBox.question``。

    Args:
        default_cancel: True 时把焦点放在"取消"按钮（用于删除等危险操作，
            避免回车误触确定）。

    Returns:
        True 表示用户点击了"是/确定"，False 表示取消或关闭。
    """
    w = make_message_box(parent, title, content)
    w.yesButton.setText(yes_text)
    w.cancelButton.setText(no_text)
    if copyable:
        w.setContentCopyable(True)
    if default_cancel:
        w.cancelButton.setFocus()
    return bool(w.exec())


def message_choice(
    parent: Optional[QWidget],
    title: str,
    content: str,
    buttons: Sequence[str],
    *,
    default: int = 0,
) -> int:
    """多选项（≥3 个按钮）对话框。替代带多个 ``addButton`` 的 ``QMessageBox``。

    第一个按钮使用主按钮样式；最后一个按钮作为取消/次要按钮。点击任意按钮都会
    关闭对话框。

    Args:
        buttons: 按钮文案列表（按显示顺序）。
        default: 默认获得焦点的按钮索引。

    Returns:
        被点击按钮的索引；若通过窗口关闭按钮/Esc 关闭而未点击任何按钮，返回 -1。
    """
    w = make_message_box(parent, title, content)
    state = {"index": -1}

    def _pick(idx: int) -> None:
        state["index"] = idx

    ordered: list = [w.yesButton]
    w.yesButton.setText(buttons[0])
    w.yesButton.clicked.connect(lambda: _pick(0))

    # 中间按钮：插入到取消按钮之前，保持顺序
    for i in range(1, len(buttons) - 1):
        btn = PushButton(buttons[i], w.buttonGroup)
        btn.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)
        btn.clicked.connect(lambda _=False, idx=i: (_pick(idx), w.accept()))
        w.buttonLayout.insertWidget(
            w.buttonLayout.count() - 1, btn, 1, Qt.AlignmentFlag.AlignVCenter
        )
        ordered.append(btn)

    last = len(buttons) - 1
    w.cancelButton.setText(buttons[last])
    # cancelButton 基类已连 reject；这里仅追加记录索引（同步执行，先后无碍）
    w.cancelButton.clicked.connect(lambda: _pick(last))
    ordered.append(w.cancelButton)

    if 0 <= default < len(ordered):
        ordered[default].setFocus()

    w.exec()
    return state["index"]


def message_busy(
    parent: Optional[QWidget],
    title: str,
    content: str,
) -> FluentMessageBox:
    """构建一个无按钮的"忙碌/请稍候"普通弹窗（不在此处 exec）。

    替代 ``QMessageBox`` + ``setStandardButtons(NoButton)`` 的用法：调用方拿到
    返回的弹窗后自行 ``exec()`` 等待，并在后台完成时调用其 ``accept()`` 关闭；
    等待期间其他窗口仍可正常操作。
    """
    w = make_message_box(parent, title, content)
    w.hideYesButton()
    w.hideCancelButton()
    return w
