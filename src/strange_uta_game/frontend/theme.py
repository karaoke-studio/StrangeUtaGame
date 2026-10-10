"""主题管理器 — 支持浅色/深色模式动态切换。

集成 qfluentwidgets 主题框架，监听系统主题变化。

使用方式：
    from strange_uta_game.frontend.theme import theme, ThemeMode

    # 获取颜色
    bg_color = theme.bg_primary
    text_color = theme.text_primary

    # 切换主题
    theme.mode = ThemeMode.DARK
    theme.mode = ThemeMode.AUTO  # 跟随系统
"""

from __future__ import annotations

import sys
from enum import Enum, auto
from typing import Optional

from PyQt6.QtCore import QEvent, Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from strange_uta_game.frontend.background_throttle import background_throttle


class ThemeMode(Enum):
    """主题模式"""
    LIGHT = auto()
    DARK = auto()
    AUTO = auto()  # 跟随系统


class ThemeColors:
    """颜色方案定义"""

    def __init__(self, is_dark: bool):
        self._is_dark = is_dark

    @property
    def is_dark(self) -> bool:
        return self._is_dark

    # ── 背景色 ──

    @property
    def bg_primary(self) -> QColor:
        """主背景色"""
        return QColor("#1E1E1E" if self._is_dark else "#FFFFFF")

    @property
    def bg_secondary(self) -> QColor:
        """次级背景色（面板、卡片）"""
        return QColor("#252526" if self._is_dark else "#F0F0F0")

    @property
    def bg_tertiary(self) -> QColor:
        """三级背景色（输入框、下拉框）"""
        return QColor("#2D2D2D" if self._is_dark else "#FFFFFF")

    @property
    def bg_hover(self) -> QColor:
        """悬停背景色"""
        return QColor("#3E3E3E" if self._is_dark else "#E5E5E5")

    @property
    def bg_selected(self) -> QColor:
        """选中背景色"""
        return QColor("#094771" if self._is_dark else "#CCE4F7")

    # ── 文字色 ──

    @property
    def text_primary(self) -> QColor:
        """主文字色"""
        return QColor("#CCCCCC" if self._is_dark else "#333333")

    @property
    def text_secondary(self) -> QColor:
        """次级文字色"""
        return QColor("#888888")

    @property
    def text_hint(self) -> QColor:
        """提示文字色"""
        return QColor("#666666" if self._is_dark else "#999999")

    @property
    def text_disabled(self) -> QColor:
        """禁用文字色"""
        return QColor("#636363" if self._is_dark else "#CCCCCC")

    # ── 边框色 ──

    @property
    def border_primary(self) -> QColor:
        """主边框色"""
        return QColor("#3E3E3E" if self._is_dark else "#DDDDDD")

    @property
    def border_secondary(self) -> QColor:
        """次级边框色"""
        return QColor("#2D2D2D" if self._is_dark else "#EEEEEE")

    # ── 强调色 ──

    @property
    def accent_primary(self) -> QColor:
        """主强调色（播放头、进度条）"""
        return QColor("#4ECDC4")

    @property
    def accent_secondary(self) -> QColor:
        """次级强调色"""
        return QColor("#5B9BD5")

    @property
    def accent_warning(self) -> QColor:
        """警告色（时间标签、高亮）"""
        return QColor("#FF6B6B")

    @property
    def mode_edit_bg(self) -> QColor:
        """编辑模式指示器背景色"""
        return QColor("#3E3E3E" if self._is_dark else "#E0E0E0")

    @property
    def mode_edit_text(self) -> QColor:
        """编辑模式指示器文字色"""
        return QColor("#E6E6E6" if self._is_dark else "#444444")

    @property
    def mode_timing_bg(self) -> QColor:
        """打轴模式指示器背景色"""
        return QColor("#FFD54F")

    @property
    def mode_timing_text(self) -> QColor:
        """打轴模式指示器文字色"""
        return QColor("#333333")

    @property
    def timetag_nonmonotonic(self) -> QColor:
        """非单调时间戳警告色（时序回退提示）"""
        return QColor("#CC44FF" if self._is_dark else "#9922CC")

    # ── 卡拉OK专用色 ──

    @property
    def karaoke_bg(self) -> QColor:
        """卡拉OK预览背景"""
        return QColor("#1E1E1E" if self._is_dark else "#FFFFFF")

    @property
    def karaoke_text_past(self) -> QColor:
        """已唱文字（当前行之前）"""
        return QColor("#666666" if self._is_dark else "#AAAAAA")

    @property
    def karaoke_text_future(self) -> QColor:
        """未唱文字（当前行之后）"""
        return QColor("#888888" if self._is_dark else "#666666")

    @property
    def karaoke_text_current(self) -> QColor:
        """当前行文字"""
        return QColor("#FFFFFF" if self._is_dark else "#000000")

    @property
    def karaoke_highlight_bg(self) -> QColor:
        """当前字符高亮背景"""
        return QColor("#3E3E3E" if self._is_dark else "#FFE0E0")

    @property
    def karaoke_selection_bg(self) -> QColor:
        """划词选中背景"""
        return QColor("#264F78" if self._is_dark else "#BDE0FE")

    # ── 波形专用色 ──

    @property
    def waveform_bg(self) -> QColor:
        """波形背景"""
        return QColor("#1E1E1E" if self._is_dark else "#F0F0F0")

    @property
    def waveform_fill(self) -> QColor:
        """波形填充色"""
        return QColor("#3A7CA5" if self._is_dark else "#9DC8E8")

    @property
    def waveform_line(self) -> QColor:
        """波形中心线"""
        return QColor("#4A9EC5" if self._is_dark else "#6BA8D4")

    # ── 行号专用色 ──

    @property
    def line_number_current(self) -> QColor:
        """当前行号颜色"""
        return QColor("#FF6B6B")

    @property
    def line_number_normal(self) -> QColor:
        """普通行号颜色"""
        return QColor("#666666" if self._is_dark else "#AAAAAA")

    # ── 全文本编辑器专用色 ──

    @property
    def editor_gutter_bg(self) -> QColor:
        """行号栏背景"""
        return QColor("#2B2B2B" if self._is_dark else "#F0F0F0")

    @property
    def editor_gutter_fg(self) -> QColor:
        """行号栏文字"""
        return QColor("#9B9B9B" if self._is_dark else "#888888")

    @property
    def editor_current_line(self) -> QColor:
        """当前光标行整行高亮背景"""
        return QColor("#323539" if self._is_dark else "#FFF8C4")

    @property
    def syntax_separator(self) -> QColor:
        """语法着色：花括号/分隔符 { } || | ,（偏绿的靛蓝/青蓝色，醒目）"""
        return QColor("#4EC9B0" if self._is_dark else "#0E8C7A")

    @property
    def syntax_singer(self) -> QColor:
        """语法着色：演唱者标签 【名】"""
        return QColor("#C586C0" if self._is_dark else "#AF00DB")

    @property
    def syntax_timestamp(self) -> QColor:
        """语法着色：起始时间戳 [..]"""
        return QColor("#4FC1FF" if self._is_dark else "#0070C1")

    @property
    def syntax_timestamp_end(self) -> QColor:
        """语法着色：停顿点时间戳 [>..]"""
        return QColor("#CE9178" if self._is_dark else "#A31515")

    # ── 进度状态色 ──

    @property
    def status_complete(self) -> QColor:
        """完成状态（绿色）"""
        return QColor("#2ecc71")

    @property
    def status_partial(self) -> QColor:
        """部分完成状态（橙色）"""
        return QColor("#f39c12")

    @property
    def status_none(self) -> QColor:
        """未完成状态（灰色）"""
        return QColor("#666666" if self._is_dark else "#999999")

    # ── 默认高亮色（无演唱者时使用）──

    @property
    def default_highlight(self) -> QColor:
        """默认高亮色（红色）"""
        return QColor("#FF6B6B")

    @property
    def default_highlight_complement(self) -> QColor:
        """默认高亮补色"""
        return QColor("#6BFFB6")

    # ── 颜色调整辅助 ──

    def ensure_contrast(self, color: QColor, bg: Optional[QColor] = None) -> QColor:
        """确保颜色在背景上有足够对比度。"""
        if bg is None:
            bg = self.bg_primary

        def relative_luminance(c: QColor) -> float:
            r, g, b = c.redF(), c.greenF(), c.blueF()
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        l1 = relative_luminance(color)
        l2 = relative_luminance(bg)
        contrast = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)

        if contrast >= 4.5:
            return color

        h, s, v, a = color.getHsvF()
        if relative_luminance(bg) > 0.5:
            v = max(0.0, v * 0.7)
        else:
            v = min(1.0, v * 1.3 + 0.2)

        return QColor.fromHsvF(h, s, v, a)

    # ── 样式表辅助 ──

    def get_hint_style(self) -> str:
        """获取提示文字的样式表"""
        color = self.text_hint.name()
        return f"color: {color};"

    def get_secondary_style(self) -> str:
        """获取次级文字的样式表"""
        color = self.text_secondary.name()
        return f"color: {color};"

    def get_caption_style(self) -> str:
        """获取说明文字的样式表"""
        color = self.text_secondary.name()
        return f"font-size: 13px; color: {color};"


# ── 弹窗原生标题栏跟随应用主题 ─────────────────────────────────────────────
# QDialog 及其子类默认用系统原生标题栏，明暗跟随 Windows 系统设置而非本应用
# 主题（深色应用 + 系统浅色时，弹窗顶部是一条刺眼的白色标题栏）。主窗口
# MSFluentWindow 在 Win11 上通过 DWMWA_USE_IMMERSIVE_DARK_MODE 让系统标题栏
# 跟随 qfluentwidgets 主题，这里对弹窗沿用同一机制。


def _apply_native_titlebar_theme(dialog: QDialog, dark: bool) -> None:
    """把弹窗原生标题栏的明暗设为 ``dark``（仅 Windows）。

    系统不支持该属性（老 Win10 / 非 Windows）时调用失败，静默保持系统
    默认样式，与 qfluentwidgets 自身处理 DWM 调用的策略一致。
    """
    if sys.platform != "win32":
        return
    try:
        hwnd = int(dialog.winId())
        if not hwnd:
            return
        import ctypes

        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        size = ctypes.sizeof(value)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE（19041+）；旧 Win10 曾用未公开
        # 编号 19，先试 20 失败再试 19 兼容老系统。
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), size) != 0:
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(value), size)
    except Exception:
        pass


class _DialogTitleBarThemeFilter(QObject):
    """弹窗显示（或原生窗口重建）时，把原生标题栏明暗同步为当前主题。

    应用级事件过滤器：无需逐个弹窗改造，现有及未来新增的 QDialog 全部
    覆盖（与 dialog_policy 的非模态弹窗策略同一手法）。
    """

    def __init__(self, theme_ref: Theme):
        super().__init__(theme_ref)
        self._theme = theme_ref

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (
            event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange)
            and isinstance(watched, QDialog)
        ):
            _apply_native_titlebar_theme(watched, self._theme.is_dark)
        return False


class Theme(QObject):
    """主题管理器单例

    集成 qfluentwidgets 主题框架，支持运行时动态切换。
    监听系统主题变化，自动更新颜色方案。
    """

    changed = pyqtSignal()  # 主题变化信号

    _instance: Optional[Theme] = None

    def __new__(cls) -> Theme:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._initialized = True

        self._mode: ThemeMode = ThemeMode.AUTO
        self._colors: Optional[ThemeColors] = None
        self._system_is_dark: bool = False
        self._poll_timer: Optional[QTimer] = None
        self._is_win10: bool = self._detect_windows_version()
        self._refreshing_widgets: bool = False
        self._refresh_widgets_pending: bool = False
        self._titlebar_filter: Optional[_DialogTitleBarThemeFilter] = None

        # 检测初始系统主题
        self._detect_system_theme()

        # 启动时强制同步一次 QApplication palette，使其与解析出的主题一致。
        #
        # 必要性：qfluentwidgets 的 setTheme 只改自有控件的 QSS，不改 QPalette；
        # 而 Win10 上 Qt 平台层不会跟随系统深色模式自动写入暗色 palette（Win11
        # 才会，且通过 colorSchemeChanged → _reapply_win11_appearance 矫正）。
        # 若此处不主动同步，Win10 深色模式下 QDialog / QWidget 等依赖 palette
        # 渲染背景的容器仍保持浅色（Window=#f3f3f3、Base=#fff），而 qfluentwidgets
        # 深色 LineEdit/TextEdit 的背景是 6% 半透明白 + 白色文字，叠在浅色容器上
        # 就呈现「白底白字」无法使用。AUTO 模式下 mode setter 会因 mode 未变而
        # 提前 return，_apply_theme_change（含 _sync_app_palette）不会触发，故必须
        # 在此显式同步一次。
        self._sync_app_palette()

        # 启动时一并写入全局样式表，使无法被 Fluent 接管的原生滚动条
        # 在深色模式下也跟随主题。
        self._apply_global_qss()

        # 监听系统主题变化
        self._setup_system_theme_listener()

        # 弹窗原生标题栏跟随应用主题（应用级事件过滤器）
        self._setup_dialog_titlebar_sync()

    @staticmethod
    def _detect_windows_version() -> bool:
        """检测是否为 Windows 10。

        Returns:
            True 表示 Win10，False 表示 Win11 或非 Windows 系统。
        """
        if sys.platform != "win32":
            return False
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            )
            build_str, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
            winreg.CloseKey(key)
            build = int(build_str)
            # Win10: 10240-22000, Win11: >= 22000
            return build < 22000
        except Exception:
            return False

    def _detect_system_theme(self) -> None:
        """检测系统主题"""
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
                )
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                winreg.CloseKey(key)
                self._system_is_dark = (value == 0)
            except Exception:
                self._system_is_dark = False
        else:
            app = QApplication.instance()
            if app:
                palette = app.palette()
                bg = palette.color(QPalette.ColorRole.Window)
                self._system_is_dark = (bg.lightness() < 128)
            else:
                self._system_is_dark = False

    def _setup_system_theme_listener(self) -> None:
        """设置系统主题变化监听器"""
        app = QApplication.instance()
        if not app:
            return

        # Qt 6.5+ 支持 colorSchemeChanged 信号（Win11 上有效）
        connected = False
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)
            connected = True
        except AttributeError:
            pass

        # Win10 上 colorSchemeChanged 不触发，使用定时器轮询
        if self._is_win10 or not connected:
            self._start_polling()

    def _setup_dialog_titlebar_sync(self) -> None:
        """安装弹窗标题栏主题过滤器（幂等）。

        QApplication 尚未创建（如无 GUI 的测试进程先 import 本模块）时
        本次跳过，待下一次 ``_apply_theme_change`` / ``_sync_dialog_titlebar_theme``
        再补装。
        """
        if self._titlebar_filter is not None:
            return
        app = QApplication.instance()
        if not app:
            return
        self._titlebar_filter = _DialogTitleBarThemeFilter(self)
        app.installEventFilter(self._titlebar_filter)

    def _sync_dialog_titlebar_theme(self) -> None:
        """主题变化后，把所有已存在弹窗的原生标题栏重刷为新主题明暗。"""
        self._setup_dialog_titlebar_sync()
        app = QApplication.instance()
        if not app:
            return
        dark = self.is_dark
        for widget in app.topLevelWidgets():
            if isinstance(widget, QDialog):
                _apply_native_titlebar_theme(widget, dark)

    def _start_polling(self) -> None:
        """启动定时器轮询系统主题（Win10 兼容方案）"""
        if self._poll_timer is not None:
            return
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)  # 每 2 秒检查一次
        self._poll_timer.timeout.connect(self._poll_system_theme)
        self._poll_timer.start()
        # 应用所有窗口都隐藏/最小化时没人看主题，停掉轮询省电；重新可见时
        # 先补一次检测再恢复（窗口可见但失焦不降频——用户可能分屏使用）
        throttle = background_throttle()
        if throttle is not None:
            throttle.visibility_maybe_changed.connect(
                self._on_ui_visibility_maybe_changed
            )
            # 接入时立即校正一次：轮询可能在全部窗口隐藏的状态下启动
            # （如嵌入宿主隐藏 SUG 后才初始化主题监听），不能等下一次
            # 可见性事件才停
            self._on_ui_visibility_maybe_changed()

    def _on_ui_visibility_maybe_changed(self) -> None:
        """应用可见性变化：全部窗口隐藏时暂停系统主题轮询，恢复可见补测。"""
        timer = self._poll_timer
        if timer is None:
            return
        throttle = background_throttle()
        if throttle is not None and throttle.is_visible:
            if not timer.isActive():
                self._poll_system_theme()
            timer.start()
        else:
            timer.stop()

    def _poll_system_theme(self) -> None:
        """轮询检测系统主题变化"""
        if self._mode != ThemeMode.AUTO:
            return

        old_dark = self._system_is_dark
        self._detect_system_theme()

        if old_dark != self._system_is_dark:
            self._apply_theme_change()

    def _on_system_theme_changed(self, scheme: Qt.ColorScheme) -> None:
        """系统主题变化回调（Win11 colorSchemeChanged 信号）

        AUTO 模式：跟随系统切换应用主题。
        LIGHT/DARK 强制模式：qfluentwidgets 内部也连接了 colorSchemeChanged，
        其 lazy 更新会在下一个 singleShot(0) 触发。用 double-singleShot(0)
        确保在那之后再重新强制我们的主题外观（标题栏 + 窗口背景色）。
        """
        if self._mode != ThemeMode.AUTO:
            # double-singleShot(0)：
            #   pass 1 → 排在 qfluentwidgets lazy-singleShot 之后入队
            #   pass 2 → 确保 qfluentwidgets 的延迟更新全部 settle 后再覆盖
            QTimer.singleShot(0, lambda: QTimer.singleShot(0, self._reapply_win11_appearance))
            return

        old_dark = self._system_is_dark
        self._system_is_dark = (scheme == Qt.ColorScheme.Dark)

        if old_dark != self._system_is_dark:
            self._apply_theme_change()

    def _sync_app_palette(self) -> None:
        """强制 QApplication palette 与当前主题一致。

        系统主题改变时，Qt 平台层会自动更新 QApplication.palette()，导致
        所有依赖 palette 渲染的控件（QListWidget、autoFillBackground=True 的
        QWidget 等）跟随系统主题变化。通过强制覆盖 palette，确保强制主题
        模式下外观正确。在 AUTO 模式中也调用，保证 palette 始终与
        qfluentwidgets 主题保持一致。
        """
        app = QApplication.instance()
        if not app:
            return
        palette = QPalette()
        if self.is_dark:
            palette.setColor(QPalette.ColorRole.Window,          QColor(32, 32, 32))
            palette.setColor(QPalette.ColorRole.WindowText,      QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Base,            QColor(25, 25, 25))
            palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(45, 45, 45))
            palette.setColor(QPalette.ColorRole.Text,            QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.BrightText,      QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Button,          QColor(45, 45, 45))
            palette.setColor(QPalette.ColorRole.ButtonText,      QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Highlight,       QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(45, 45, 45))
            palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.Link,            QColor(42, 130, 218))
            palette.setColor(QPalette.ColorRole.Mid,             QColor(60, 60, 60))
            palette.setColor(QPalette.ColorRole.Dark,            QColor(20, 20, 20))
        else:
            palette.setColor(QPalette.ColorRole.Window,          QColor(243, 243, 243))
            palette.setColor(QPalette.ColorRole.WindowText,      QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Base,            QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(233, 231, 227))
            palette.setColor(QPalette.ColorRole.Text,            QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.BrightText,      QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Button,          QColor(240, 240, 240))
            palette.setColor(QPalette.ColorRole.ButtonText,      QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Highlight,       QColor(0, 103, 192))
            palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
            palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(255, 255, 220))
            palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(0, 0, 0))
            palette.setColor(QPalette.ColorRole.Link,            QColor(0, 0, 255))
            palette.setColor(QPalette.ColorRole.Mid,             QColor(160, 160, 160))
            palette.setColor(QPalette.ColorRole.Dark,            QColor(160, 160, 160))
        app.setPalette(palette)

    def _build_global_qss(self) -> str:
        """构建应用级全局样式表，仅针对 qfluentwidgets 无法接管的原生 Qt 控件。

        qfluentwidgets 通过 **逐控件** setStyleSheet 应用样式，其优先级高于
        application 级样式表；因此这里用 ``app.setStyleSheet`` 设置的全局规则
        只会命中没有自管理 QSS 的原生控件，不会污染任何 Fluent 控件。

        QScrollBar：timeline / karaoke 预览把它当作独立数值滑条使用（自定义
        range/value），而 Fluent ScrollBar 必须依附 QAbstractScrollArea，无法直接替换。
        QToolTip / QHeaderView：Windows 10 的原生样式不会可靠采用 ToolTipBase、
        ToolTipText 及表头 palette role，因此需要 QSS 兜底，避免深色模式下仍显示白底。
        其余原生控件（QGroupBox / QMessageBox / QDialogButtonBox 按钮等）已统一改用
        Fluent 控件接管，不在此处理。
        """
        dark = self.colors.is_dark
        # 滚动条滑块：深色用半透明白、浅色用半透明黑，hover 加深
        handle = "rgba(255, 255, 255, 0.28)" if dark else "rgba(0, 0, 0, 0.28)"
        handle_hover = "rgba(255, 255, 255, 0.45)" if dark else "rgba(0, 0, 0, 0.45)"
        tooltip_bg = "#2D2D2D" if dark else "#FFFFDC"
        tooltip_text = "#FFFFFF" if dark else "#000000"
        tooltip_border = "#555555" if dark else "#A0A0A0"
        header_bg = "#2D2D2D" if dark else "#F0F0F0"
        header_hover = "#3E3E3E" if dark else "#E5E5E5"
        header_text = "#CCCCCC" if dark else "#333333"
        header_border = "#3E3E3E" if dark else "#DDDDDD"
        return f"""
        /* ── Win10 原生悬浮提示（palette 的 ToolTipBase/Text 在该平台不可靠）── */
        QToolTip {{
            color: {tooltip_text};
            background-color: {tooltip_bg};
            border: 1px solid {tooltip_border};
            padding: 4px;
        }}

        /* ── 原生表格表头（应用演唱者、按行设置演唱者等对话框）── */
        QHeaderView::section {{
            color: {header_text};
            background-color: {header_bg};
            border: none;
            border-right: 1px solid {header_border};
            border-bottom: 1px solid {header_border};
            padding: 4px;
        }}
        QHeaderView::section:hover {{ background-color: {header_hover}; }}

        /* ── 原生滚动条（波形 timeline / karaoke 预览的独立滑条）── */
        QScrollBar:vertical, QScrollBar:horizontal {{
            background: transparent;
            border: none;
        }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {handle};
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical {{ min-height: 24px; }}
        QScrollBar::handle:horizontal {{ min-width: 24px; }}
        QScrollBar::handle:hover {{ background: {handle_hover}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{
            width: 0px; height: 0px; background: none; border: none;
        }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
        """

    def _apply_global_qss(self) -> None:
        """把全局样式表写入 QApplication（覆盖原生控件，不影响 Fluent 控件）。"""
        app = QApplication.instance()
        if not app:
            return
        app.setStyleSheet(self._build_global_qss())

    def _apply_theme_change(self) -> None:
        """应用主题变更（统一入口）"""
        self._invalidate()
        self._sync_app_palette()
        self._apply_global_qss()
        self._apply_qfluentwidgets_theme(lazy=True)
        self._refresh_all_widgets()
        self._sync_dialog_titlebar_theme()
        self.changed.emit()

    def _reapply_win11_appearance(self) -> None:
        """Win11 专用：重新强制主题外观（全量刷新）。

        在两种场景下通过 double-singleShot(0) 延迟调用：
        1. 强制模式下系统主题改变时 —— Qt 平台层已把系统 QPalette 更新为暗色，
           若不进行全量刷新，用 autoFillBackground 或 QPalette 渲染背景的子界面
           仍会跟着系统暗色走；
        2. 用户手动切换主题时，确保 event loop 完全 settle 后标题栏/背景状态正确。

        与 _apply_theme_change 的区别：
        - _sync_app_palette()：在 setTheme 之前先强制 QPalette，覆盖 Qt 平台层刚
          写入的系统 dark palette，使 autoFillBackground 控件立即呈现正确背景；
        - lazy=False：对 styleSheetManager 中的 **所有** 控件（含隐藏页）立即写入
          正确的 QSS，不依赖 DirtyStyleSheetWatcher 的延迟机制，从根本上消除
          「隐藏子界面切出来时仍显示系统颜色」的问题；
        - _refresh_all_widgets()：unpolish/polish 所有可见控件，使样式重新生效；
        - 不重复调用 _invalidate()，因 _mode 未变，颜色缓存无需重建。
        """
        self._sync_app_palette()
        self._apply_global_qss()
        self._apply_qfluentwidgets_theme(lazy=False)
        self._refresh_all_widgets()
        self._sync_dialog_titlebar_theme()
        self.changed.emit()

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @mode.setter
    def mode(self, value: ThemeMode) -> None:
        if self._mode == value:
            return
        self._mode = value
        self._apply_theme_change()
        # Win11：setTheme(lazy=True) 触发的 DwmAttribute / MSFluentWindow stylesheet
        # 更新可能在 _apply_theme_change 的 processEvents() 之后仍有残留。
        # 用 double-singleShot(0) 在 event loop 完全 settle 后再 re-assert 一次，
        # 确保标题栏颜色和 Mica 覆盖背景色都处于正确状态。
        if not self._is_win10:
            QTimer.singleShot(0, lambda: QTimer.singleShot(0, self._reapply_win11_appearance))

    @property
    def is_dark(self) -> bool:
        """当前是否为深色模式"""
        if self._mode == ThemeMode.AUTO:
            return self._system_is_dark
        return self._mode == ThemeMode.DARK

    @property
    def colors(self) -> ThemeColors:
        """获取当前颜色方案"""
        if self._colors is None:
            self._colors = ThemeColors(self.is_dark)
        return self._colors

    def _invalidate(self) -> None:
        """清除颜色缓存"""
        self._colors = None

    def _apply_qfluentwidgets_theme(self, lazy: bool = True) -> None:
        """同步应用 qfluentwidgets 主题。

        Parameters
        ----------
        lazy : bool
            True（默认）：仅对可见控件立即更新，隐藏控件标记 dirty-qss。
            False：对 styleSheetManager 内所有控件立即写入 QSS（含隐藏控件），
                   消除依赖 DirtyStyleSheetWatcher 时序的隐患。

        Win10 上需要多次调用以确保所有控件正确更新。
        """
        try:
            from qfluentwidgets import setTheme, Theme as QfwTheme
            target = QfwTheme.DARK if self.is_dark else QfwTheme.LIGHT
            setTheme(target, lazy=lazy)
            # Win10 兼容：某些控件需要额外的强制刷新
            if self._is_win10:
                app = QApplication.instance()
                if app:
                    app.processEvents()
                    setTheme(target, lazy=lazy)
        except Exception:
            pass

    def _refresh_all_widgets(self) -> None:
        """强制刷新所有顶层窗口及其子控件的样式表。

        注意：qfluentwidgets 主题的应用（setTheme）已在 _apply_theme_change()
        中调用过一次；这里只负责 unpolish/polish 刷新，不重复调用 setTheme，
        避免多次调用引发控件中间状态渲染异常。
        """
        if self._refreshing_widgets:
            self._refresh_widgets_pending = True
            return
        app = QApplication.instance()
        if not app:
            return

        self._refreshing_widgets = True
        try:
            for _ in range(2):
                self._refresh_widgets_pending = False
                # 先让 setTheme(lazy=True) 排队的事件跑完
                app.processEvents()
                # 遍历所有顶层窗口，强制重新 polish
                for widget in app.topLevelWidgets():
                    self._update_widget_style(widget)
                # 再刷新一次，确保 polish 触发的重绘已入队
                app.processEvents()
                if not self._refresh_widgets_pending:
                    break
        finally:
            self._refreshing_widgets = False

    def _update_widget_style(self, widget) -> None:
        """递归更新控件及其子控件的样式"""
        # 强制重新应用样式表
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()
        # 递归处理子控件
        for child in widget.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)
            child.update()

    def refresh(self) -> None:
        """刷新主题（重新检测系统主题）"""
        self._detect_system_theme()
        self._apply_theme_change()

    def __getattr__(self, name: str):
        """代理 ThemeColors 的属性访问"""
        if name.startswith('_') or name in ('changed', 'mode', 'is_dark', 'colors',
                                              'refresh',
                                              '_invalidate', '_apply_qfluentwidgets_theme',
                                              '_detect_system_theme', '_setup_system_theme_listener',
                                              '_on_system_theme_changed', '_apply_theme_change',
                                              '_start_polling', '_poll_system_theme',
                                              '_detect_windows_version'):
            raise AttributeError(name)
        return getattr(self.colors, name)


def _patch_round_menu() -> None:
    """统一修正所有 RoundMenu（含 ComboBoxMenu 子类）的阴影与动画行为。

    qfluentwidgets 默认：
    - blurRadius=30、offset=(0,8)、layout margins=(12,8,12,20)
      → 菜单周围形成明显的 12-20px 透明光晕
    - exec() 默认 aniType=DROP_DOWN，菜单弹出时带滑入动画

    修正为：
    - blurRadius=2、offset=(0,1)、margins=(4,4,4,5)  → 紧凑阴影
    - exec() 统一改用 aniType=NONE                   → 无动画即时弹出

    同时修复 MSFluentWindow 页面切换后点击下拉菜单时 Qt 报警
    "QWidgetWindow(StackedWidgetClassWindow) must be a top level window"。
    根因：PopUpAniStackedWidget 在页面切换动画期间可能获得杂散原生窗口句柄，
    导致 RoundMenu 弹窗解析 transient parent 时走到错误的 QWidgetWindow。
    修复：在 exec 前对按钮所在顶层窗口调用 winId()，强制 Qt 理顺窗口父子链。

    详见 qfluentwidgets PopUpAniStackedWidget.setCurrentIndex 中对子 widget
    pos 属性的 QPropertyAnimation，该动画可能在页面显隐切换后使中间层
    QStackedWidget 持有不应存在的原生窗口句柄。
    """
    try:
        from qfluentwidgets import RoundMenu, MenuAnimationType
        _orig_init = RoundMenu.__init__
        _orig_exec = RoundMenu.exec

        def _slim_init(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            self.setShadowEffect(blurRadius=2, offset=(0, 1))
            self.hBoxLayout.setContentsMargins(1, 1, 1, 2)

        def _no_ani_exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
            # 页面切换后 QStackedWidget 可能持有杂散原生窗口句柄，
            # 导致弹窗 transient parent 检查时 Qt 报警。
            # 提前 pin 顶层窗口的原生句柄，强制理顺窗口父子链。
            try:
                parent = self.parent()
                if parent is not None:
                    top = parent.window()
                    if top is not None:
                        top.winId()
            except Exception:
                pass
            _orig_exec(self, pos, ani=False, aniType=MenuAnimationType.FADE_IN_DROP_DOWN)

        RoundMenu.__init__ = _slim_init
        RoundMenu.exec = _no_ani_exec
    except Exception:
        pass


def _patch_tool_tip() -> None:
    """统一修正 Fluent ToolTip（fluent_tooltips 悬停提示）的透明光晕。

    qfluentwidgets 默认（与 RoundMenu 默认光晕同源）：
    - shadowEffect blurRadius=25、offset=(0,5)
    - 窗口布局 margins=(12,8,12,12)
      → 提示框周围形成 8-12px 的透明光晕区域

    修正为与 _patch_round_menu 一致的紧凑阴影：
    - blurRadius=2、offset=(0,1)、margins=(1,1,1,2)

    直接 patch ToolTip.__init__：ToolTipFilter（含 ItemViewToolTip 子类）
    在运行时才创建提示实例，改类本身即可让全应用生效，
    与 _patch_round_menu patch RoundMenu.__init__ 的方式对称。
    """
    try:
        from qfluentwidgets.components.widgets.tool_tip import ToolTip
        _orig_init = ToolTip.__init__

        def _slim_init(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            self.shadowEffect.setBlurRadius(2)
            self.shadowEffect.setOffset(0, 1)
            self.layout().setContentsMargins(1, 1, 1, 2)

        ToolTip.__init__ = _slim_init
    except Exception:
        pass


def _patch_editable_combobox_disconnect() -> None:
    """修复 EditableComboBox 的通配符 disconnect 警告。

    原始 __init__ 末尾调用 self.clearButton.disconnect()（无参数 = 通配符），
    会把 StyleSheetManager 在 clearButton.destroyed 上注册的清理 lambda 也断开，
    触发 Qt 的「wildcard call disconnects from destroyed signal」警告。
    改为只断开 clicked→self.clear 这条具体连接即可。
    """
    try:
        from qfluentwidgets import EditableComboBox
        from qfluentwidgets.components.widgets.line_edit import LineEdit, LineEditButton
        from qfluentwidgets import FluentIcon as FIF
        from PyQt6.QtCore import Qt

        def _fixed_init(self, parent=None):
            # 等价于原始 super().__init__(parent=parent)：
            # 经 MRO 链依次触发 LineEdit / QLineEdit / QWidget / ComboBoxBase 的初始化
            LineEdit.__init__(self, parent=parent)

            self.dropButton = LineEditButton(FIF.ARROW_DOWN, self)
            self.setTextMargins(0, 0, 29, 0)
            self.dropButton.setFixedSize(30, 25)
            self.hBoxLayout.addWidget(self.dropButton, 0, Qt.AlignmentFlag.AlignRight)

            self.dropButton.clicked.connect(self._toggleComboMenu)
            self.textChanged.connect(self._onComboTextChanged)
            self.returnPressed.connect(self._onReturnPressed)

            # FIX：只断开 clicked→clear 这条连接，避免通配符误伤 destroyed 信号
            try:
                self.clearButton.clicked.disconnect(self.clear)
            except TypeError:
                pass
            self.clearButton.clicked.connect(self._onClearButtonClicked)

        EditableComboBox.__init__ = _fixed_init
    except Exception:
        pass


_patch_round_menu()
_patch_tool_tip()
_patch_editable_combobox_disconnect()

# 全局单例
theme = Theme()
