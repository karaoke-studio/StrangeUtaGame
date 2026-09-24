"""主窗口。

应用主容器，使用 PyQt-Fluent-Widgets 的 MSFluentWindow。
参考 March7thAssistant 的 UI 架构。
"""

from copy import deepcopy

from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QFileDialog

from qfluentwidgets import (
    NavigationItemPosition,
    MSFluentWindow,
    setThemeColor,
    NavigationBarPushButton,
    setTheme,
    Theme,
    FluentIcon as FIF,
)
from qfluentwidgets import InfoBar, InfoBarPosition, StateToolTip

from typing import Optional

from strange_uta_game.backend.application import CommandManager, TimingService
from strange_uta_game.backend.domain import Project
from strange_uta_game.frontend.background_throttle import set_visibility_override
from strange_uta_game.frontend.project_store import ProjectStore
from strange_uta_game.frontend.theme import theme
from strange_uta_game.frontend.dialog_policy import install_non_modal_dialog_policy
from strange_uta_game.frontend.crash_guard import install_crash_guard
from strange_uta_game.frontend.fluent_widgets import message_choice, message_question
from strange_uta_game.frontend.window_sizing import (
    center_on_screen,
    clamp_size,
    fit_min_size,
    fit_to_screen,
)


def _build_axis_plan(project: object) -> "dict[str, object]":
    """从 ``project.axis_groups`` 派生宿主侧 ``axis_plan`` 快照。

    纯 dict / 纯值类型，不引用任何 domain 对象 —— 构建完成即与后续编辑
    隔离（与 ``export_to_next_payload`` 其余键同一契约）。project 允许是
    任意带 ``singers`` / ``axis_groups`` 属性的对象，方便宿主与契约测试
    用简易对象模拟。

    结构::

        {
            "mode": "single" | "split",   # split = axis_groups 非空
            "groups": [
                {
                    "name": "轴1",
                    "singer_ids": [...],
                    "is_primary": True,    # 有且仅有一个主分组（标签信息随主分组导出）
                    "singers": [  # 冗余快照（id/name/color/color_mode/split_colors）
                        {"id": "...", "name": "初音ミク", "color": "#FF6B6B",
                         "color_mode": "solid", "split_colors": [...]},
                    ],
                },
            ],
            "unassigned": [...],  # 未进入任何组的启用演唱者 ID
        }

    每组的内容口径与 Nicokara 导出的演唱者过滤一致：行内任一字符属于组内
    演唱者则保留整行（空行无条件保留），行内组外字符剔除（详见
    docs/EMBEDDING.md §2.1）。组内 singer_ids 为空 = 全部演唱者，payload
    中物化为当前全部启用歌手 id。
    """
    groups_raw = list(getattr(project, "axis_groups", None) or [])
    singers = list(getattr(project, "singers", None) or [])
    by_id = {}
    for s in singers:
        sid = getattr(s, "id", None)
        if sid is not None:
            by_id[sid] = s

    groups: list = []
    primary_assigned = False
    for g in groups_raw:
        raw_ids = getattr(g, "singer_ids", None) or []
        if raw_ids:
            singer_ids = [sid for sid in raw_ids if sid in by_id]
        else:
            # 空 = 全部演唱者（过滤器「不勾选则导出全部」口径）：物化为
            # 当前全部启用歌手，随构建反映最新名单
            singer_ids = [
                sid
                for sid, s in by_id.items()
                if getattr(s, "enabled", True)
            ]
        # 主分组归一化（防御手写数据）：第一个标记为主分组者生效，其后
        # 一律降级；无人标记时首组为主分组
        is_primary = bool(getattr(g, "is_primary", False)) and not primary_assigned
        primary_assigned = primary_assigned or is_primary
        groups.append(
            {
                "name": str(getattr(g, "name", "") or ""),
                "singer_ids": singer_ids,
                "is_primary": is_primary,
                "singers": [
                    {
                        "id": sid,
                        "name": str(getattr(by_id[sid], "name", "") or ""),
                        "color": str(getattr(by_id[sid], "color", "") or ""),
                        "color_mode": str(
                            getattr(by_id[sid], "color_mode", "solid") or "solid"
                        ),
                        "split_colors": list(
                            getattr(by_id[sid], "split_colors", None) or []
                        ),
                    }
                    for sid in singer_ids
                ],
            }
        )
    if groups and not primary_assigned:
        groups[0]["is_primary"] = True

    assigned = set()
    for group in groups:
        assigned.update(group["singer_ids"])
    unassigned = [
        sid
        for sid in by_id
        if sid not in assigned and getattr(by_id[sid], "enabled", True)
    ]

    return {
        "mode": "split" if groups else "single",
        "groups": groups,
        "unassigned": unassigned,
    }


class MainWindow(MSFluentWindow):
    """主窗口 - MSFluentWindow 侧边栏导航架构。

    支持两种模式：

    - **standalone**（默认，``embedded=False``）：作为顶层窗口跑，
      自管窗口几何 / Ctrl+S / 启动期 updater / 闪退恢复弹窗等。
      ``python main.py`` 时使用这种模式。
    - **embedded**（``embedded=True``）：作为宿主程序（如 krok-helper）
      子组件嵌入。跳过所有跟"顶层窗口"绑定的行为，由宿主统一管理。
      用 :meth:`for_embedding` 工厂方法创建即可。
    """

    # 类级别 fallback：万一 super().__init__() 通过事件触发
    # （如 resizeEvent / changeEvent → _schedule_geometry_save）在
    # self._embedded 实例属性写入前就访问它，会落到这个 False，保留
    # standalone 行为。
    _embedded: bool = False
    export_to_next_requested = pyqtSignal()
    project_save_finished = pyqtSignal(str)
    project_save_failed = pyqtSignal(str)

    def __init__(self, embedded: bool = False, settings_provider=None, progress_callback=None, ai_timing_host=None):
        # 嵌入模式不经过仓库根目录的 main.py，因此在主窗口入口也安装一次。
        # 安装函数可重复调用，确保 standalone / embedded 的所有弹窗行为一致。
        app = QApplication.instance()
        if app is not None:
            install_non_modal_dialog_policy(app)
            # 全局异常兜底同样装在主窗口入口（而非 main.py）：嵌入宿主
            # （krok-helper 等）不经过 main.py，宿主进程里逃逸的未处理异常
            # 也由此兜住；宿主自装的 excepthook 会被链式转交、不被剥夺。
            install_crash_guard(app)

        # ⚠ 必须在 super().__init__() 之前赋值！MSFluentWindow 初始化
        # 过程中可能触发 resizeEvent / changeEvent，那些 handler 会调
        # _schedule_geometry_save / _save_window_geometry / _restore_window_geometry
        # 等等，里面都读 self._embedded —— 若此时还未赋值会
        # AttributeError，而 Qt C++ 事件分发无法捕获 Python 异常，进程
        # 直接以 0xC0000409 stack-buffer-overrun 崩掉（已实测）。
        self._embedded = embedded
        self._settings_provider = settings_provider
        self._progress_cb = progress_callback if not embedded else None
        # AI 打轴宿主能力（阶段 G 契约，见 backend/.../ai_timing/host.py）：
        # embedded 时由工作台注入（分离状态/会话人声/分离执行/缓存目录）；
        # standalone 为 None，AI 打轴使用自身默认配置。
        self.aiTimingHost = ai_timing_host

        self._report_progress(5, self.tr("正在初始化翻译器..."))

        # 翻译器安装：必须在 super().__init__() 与任何 widget 构造之前——
        # 否则首次构造的 widget 用源串渲染，再依赖 LanguageChange 事件二次
        # 刷新，时序上既慢又脆（被 retranslate 漏到的字符串就永远是源串）。
        # embedded 模式语言由宿主独占（见 EMBEDDING.md §5），不自行装载。
        if not self._embedded:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            from strange_uta_game.frontend.font_utils import set_ui_font_override
            from strange_uta_game.frontend.localization import install_translators
            try:
                _startup_settings = AppSettings()
                _lang = _startup_settings.get("ui.language", "auto")
                _interface_font = _startup_settings.get("ui.interface_font", "")
            except Exception:
                _lang = "zh_CN"
                _interface_font = ""
            set_ui_font_override(_interface_font)
            install_translators(_lang)

        self._report_progress(10, self.tr("正在初始化窗口框架..."))
        super().__init__()

        if self._embedded and self._settings_provider is not None:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            AppSettings.set_default_provider(self._settings_provider)

        self._report_progress(20, self.tr("正在初始化音频引擎..."))
        # 引擎按"启用高质量音频变速"设置选择：
        #   开（默认）→ BassTsmEngine：离线 TSM 预渲染，变速不变调、无爆音；
        #   关        → BassEngine：原版 BASS 实时变速，零缓存但可能爆音。
        self._audio_engine = self._make_audio_engine()

        self._report_progress(30, self.tr("正在初始化核心服务..."))
        self._command_manager = CommandManager()
        self._command_manager.set_on_state_changed(self._on_command_state_changed)
        self._timing_service = TimingService(self._audio_engine, self._command_manager)
        self._store = ProjectStore(self)
        self._store.set_auto_save_defer_predicate(self._timing_service.is_playing)
        self._store.save_finished.connect(self.project_save_finished.emit)
        self._store.save_error.connect(self.project_save_failed.emit)

        # 跟踪当前界面（用于 switchTo 自动应用修改，必须在 _init_navigation 之前）
        self._current_interface = None

        # 窗口大小/最大化记忆：AppSettings 现为进程级共享实例（见
        # app_settings），此处仅保留启动时恢复几何用的读取引用；写盘走
        # _save_window_geometry 每次重新解析。embedded 模式由宿主管理
        # 几何，跳过这一整套。
        if not self._embedded:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            self._win_settings = AppSettings()
            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.setInterval(400)
            self._geometry_save_timer.timeout.connect(self._save_window_geometry)
        else:
            self._win_settings = None
            self._geometry_save_timer = None

        self._report_progress(40, self.tr("正在初始化窗口..."))
        self._init_window()
        self._report_progress(45, self.tr("正在加载界面组件..."))
        self._init_interfaces()
        self._report_progress(88, self.tr("正在配置导航栏..."))
        self._init_navigation()
        self._apply_embedded_ui_policy()

        # 中央响应：store 的 project 变更 → 同步 timing_service 等
        self._store.data_changed.connect(self._on_data_changed)

        # 全局兜底错误可见化：信号处理器中的异常通过此信号通知用户
        self._store.error_notify.connect(self._on_error_notify)
        self._error_notify_timer = QTimer(self)
        self._error_notify_timer.setSingleShot(True)
        self._error_notify_timer.setInterval(3000)
        self._error_notify_timer.timeout.connect(self._show_pending_error)
        self._pending_error_title: Optional[str] = None
        self._pending_error_msg: Optional[str] = None

        # 监听主题变化，更新 Win10 兜底背景色
        theme.changed.connect(self._on_theme_changed)

        # 启动时清空上一次会话的 LLM 注音日志（.cache/llm_ruby/）。
        # 历史方案是「启动 + 退出双清」，但退出清会把崩溃前的请求/响应一并删掉，
        # 调试时拿不到现场。现改成仅启动清 —— 上次会话日志保留到下次启动前，
        # 用户随时可以打开 .cache/llm_ruby/ 复盘。
        self._clear_llm_logs()

        # 全局 Ctrl+S 保存快捷键 —— standalone 专用；embedded 模式宿主可在
        # 自己的顶层窗口注册并转发到 :meth:`trigger_save`。
        if not self._embedded:
            self._save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
            self._save_shortcut.activated.connect(self._on_global_save)
        else:
            self._save_shortcut = None

        # 初始化自动保存配置
        self._apply_auto_save_settings()

        self._report_progress(95, self.tr("正在完成初始化..."))

        # 启动期定时器：
        # - 闪退恢复弹窗会抢宿主焦点，embedded 跳过；
        # - 应用 updater 由宿主统一管理，embedded 跳过；
        # - 网络词典更新只读写 SettingsProvider 管理的 namespace，embedded
        #   可安全保留；HTTP 拉取仍在 daemon 线程执行，不阻塞宿主 UI。
        if not self._embedded:
            # 延迟启动期检查：先闪退恢复，完成后再检查更新/词典更新，
            # 避免两个模态弹窗同时弹出导致无法操作。
            QTimer.singleShot(500, self._startup_checks)
        else:
            QTimer.singleShot(500, self._schedule_network_dict_auto_update)

        # 由 updater 流程主动设置；closeEvent 检测到此标志即 bypass dirty 弹窗，
        # 走"兜底保存 + 直接退出"路径。
        self._force_quitting = False
        # 本次会话是否已通过 open_initial_files 打开拖入/关联文件。为真时
        # 启动链跳过闪退恢复提示——用户拖入文件的意图明确，恢复弹窗反而会
        # 覆盖刚加载的内容；恢复数据保留在磁盘，留待下次正常启动再询问。
        self._opened_initial_files = False

        self._report_progress(100, self.tr("准备就绪"))
        self._progress_cb = None

    def _report_progress(self, value: int, text: str = "") -> None:
        """向闪屏回调报告初始化进度（无回调时静默跳过）。"""
        cb = getattr(self, "_progress_cb", None)
        if cb:
            try:
                cb(value, text)
            except Exception:
                pass

    @staticmethod
    def _find_icon_path() -> Optional[str]:
        """查找应用图标路径（兼容开发环境和 PyInstaller 打包环境）。"""
        import sys
        from pathlib import Path

        candidates = []
        # PyInstaller 打包后
        base = getattr(sys, "_MEIPASS", None)
        if base:
            candidates.append(Path(base) / "strange_uta_game" / "resource" / "icon.ico")
        # 开发环境：相对于本文件
        candidates.append(
            Path(__file__).resolve().parent.parent / "resource" / "icon.ico"
        )
        # 项目根目录
        candidates.append(Path(sys.argv[0]).resolve().parent / "icon.ico")

        for p in candidates:
            if p.exists():
                return str(p)
        return None

    def _init_window(self):
        """初始化窗口属性。

        embedded 模式跳过所有"顶层窗口"操作（主题、标题、尺寸、图标、居中、
        几何恢复）—— 这些全部由宿主统一管理；唯一保留的是 Win10 兜底背景
        样式表，因为它只影响本 widget 内部渲染。
        """
        if self._embedded:
            # 嵌入模式：宿主控制主题与几何。仅保留 widget 本地背景兜底，
            # 防止 Mica 失败时透出宿主背景与子界面冲突。
            self._remove_embedded_title_bar()
            self._apply_win10_fallback_bg()
            return

        setThemeColor("#FF6B6B", lazy=True)
        # 使用主题管理器的设置，而不是硬编码
        if theme.is_dark:
            setTheme(Theme.DARK, lazy=True)
        else:
            setTheme(Theme.LIGHT, lazy=True)

        # Win10 兜底：Mica 材质不可用时，强制设置纯深色背景
        # 避免白底白字的灾难性渲染
        self._apply_win10_fallback_bg()

        self.setWindowTitle(self.tr("StrangeUtaGame - 歌词打轴工具 Bilibili@不会说话的呆轩cc"))
        # 最小尺寸与默认尺寸都按当前屏幕可用区域裁剪：在 1080p/高缩放屏上不会
        # 溢出，也不会出现「最小尺寸大于屏幕、窗口缩不进可视区」的情况。
        fit_min_size(self, 1200, 800)
        fit_to_screen(self, 1400, 900)

        # 设置窗口图标（左上角 + 任务栏）
        from PyQt6.QtGui import QIcon

        icon_path = self._find_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))

        # 居中到光标所在屏（多显示器下不会强制跳回主屏）
        center_on_screen(self)

        # 应用用户上次的窗口大小/最大化习惯（覆盖上面的默认尺寸）
        self._restore_window_geometry()

    def _remove_embedded_title_bar(self) -> None:
        """Remove MSFluentWindow's top title-bar space for embedded mode."""
        try:
            self.setResizeEnabled(False)
        except Exception:
            pass
        try:
            self.hBoxLayout.setContentsMargins(0, 0, 0, 0)
        except Exception:
            pass
        try:
            self.setSystemTitleBarButtonVisible(False)
        except Exception:
            pass
        try:
            title_bar = getattr(self, "titleBar", None)
            if title_bar is not None:
                title_bar.hide()
                title_bar.setFixedHeight(0)
        except RuntimeError:
            pass
        except Exception:
            pass

    def _apply_embedded_ui_policy(self) -> None:
        """Apply host-owned UI behavior that only exists in embedded mode."""
        if not self._embedded:
            return
        navigation = getattr(self, "navigationInterface", None)
        if navigation is not None:
            width = max(72, navigation.minimumSizeHint().width())
            navigation.setFixedWidth(width)
        timeline = getattr(getattr(self, "editorInterface", None), "timeline", None)
        if timeline is not None and hasattr(timeline, "set_zoom_enabled"):
            timeline.set_zoom_enabled(False)

    def _restore_window_geometry(self):
        """启动时从 config.json 恢复窗口大小与最大化状态（仅读取一次）。

        读取 ``ui.window_size`` / ``ui.window_maximized``；字段缺失时维持
        :meth:`_init_window` 设定的默认尺寸（即不做任何改动）。embedded 模式
        由宿主控制窗口几何，直接返回。
        """
        if self._embedded or self._win_settings is None:
            return
        try:
            size = self._win_settings.get("ui.window_size", None)
            if isinstance(size, (list, tuple)) and len(size) == 2:
                # 上次保存的尺寸可能是在更大的屏幕上记录的，恢复时同样裁剪到
                # 当前屏幕，避免换到小屏后窗口超出可视范围。
                w, h = clamp_size(self, int(size[0]), int(size[1]))
                self.resize(w, h)
                # 改变尺寸后重新居中到所在屏
                center_on_screen(self)
            if self._win_settings.get("ui.window_maximized", False):
                self.showMaximized()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("恢复窗口几何状态失败", exc_info=True)

    def _save_window_geometry(self):
        """把当前窗口大小与最大化状态实时写入 config.json。

        AppSettings 为进程级共享实例，内存即最新状态，直接 set+save 即可
        （旧版需先 reload 合并磁盘，否则会覆盖设置页刚保存的其它字段——
        双实例快照问题已随共享实例消失）。最大化/全屏时不覆盖
        ``window_size``，以便退出最大化后仍能恢复用户习惯的普通窗口尺寸。
        embedded 模式下几何由宿主管理，本方法直接返回。
        """
        if self._embedded:
            return
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings

            # 每次保存时重新解析共享实例：「重置设置」会清缓存重建，
            # 持有旧实例引用会在新旧实例间重新制造整字典写盘的回滚窗口。
            win_settings = AppSettings()
        except Exception:
            return
        try:
            maximized = self.isMaximized() or self.isFullScreen()
            win_settings.set("ui.window_maximized", maximized)
            if not maximized:
                win_settings.set("ui.window_size", [self.width(), self.height()])
            win_settings.save()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("保存窗口几何状态失败", exc_info=True)

    def _schedule_geometry_save(self):
        """触发防抖保存（拖拽缩放过程中合并多次事件，停止后才写盘）。

        embedded 模式下 ``_geometry_save_timer`` 为 None，这里的 guard 也
        保证 resizeEvent / changeEvent 触发本方法时不会 NPE。
        """
        if self._embedded:
            return
        timer = getattr(self, "_geometry_save_timer", None)
        if timer is not None:
            timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._schedule_geometry_save()
        elif event.type() == QEvent.Type.LanguageChange:
            # 切语言时（installTranslator 自动派发），刷新窗口标题；导航条 label
            # 走 _refresh_navigation_labels（NavigationItem 持有的是 setText 后的
            # 静态字符串，必须显式重设）。
            self.setWindowTitle(self.tr("StrangeUtaGame - 歌词打轴工具 Bilibili@不会说话的呆轩cc"))
            self._refresh_navigation_labels()

    def hideEvent(self, event):
        """在宿主未显式转发生命周期时为 embedded 模式提供兜底。"""
        if self._embedded:
            self.on_host_visibility_changed(False)
        super().hideEvent(event)

    def showEvent(self, event):
        """embedded 窗口重新显示时按音频实际状态恢复快捷键模式。"""
        super().showEvent(event)
        if self._embedded:
            self.on_host_visibility_changed(True)

    def _refresh_frameless(self) -> None:
        """刷新无边框窗口状态，修复导入后无法拖拽边缘调整窗口大小。

        原生文件对话框（Windows/macOS 的 QFileDialog）接管窗口事件循环；
        关闭后窗口的边框/阴影/边缘命中检测可能丢失，表现为无法拖拽缩放。

        本方法应由各加载路径在项目导入完成后调用。
        """
        import sys
        if sys.platform != "darwin":
            return
        self._do_refresh_frameless()

    def _do_refresh_frameless(self, _index=None) -> None:
        """刷新无边框状态，恢复窗口边缘拖拽缩放能力。

        加载流程中，原生文件对话框会接管窗口事件循环；
        关闭后 ``WM_NCCALCSIZE`` 等消息可能导致 DWM 阴影/边框丢失，
        使 ``WindowsFramelessWindow.nativeEvent`` 的 ``WM_NCHITTEST``
        边缘命中检测失效。macOS 同理，``NSResizableWindowMask`` 可能丢失。

        ``updateFrameless()`` 会重建无边框结构（Windows 恢复 DWM 阴影、
        macOS 重建 NSWindow styleMask），``show()`` 确保 ``setWindowFlags``
        改变时窗口不被隐藏。
        """
        try:
            self.updateFrameless()
            self.show()

            import sys
            if sys.platform != "darwin":
                return

            from ctypes import c_void_p
            import Cocoa
            import objc

            view = objc.objc_object(c_void_p=c_void_p(self.winId().__int__()))
            ns_window = view.window()
            if ns_window is None:
                return

            mask = ns_window.styleMask()
            # NSResizableWindowMask = 1 << 3
            if not (mask & 8):
                ns_window.setStyleMask_(mask | 8)
        except Exception:
            pass

    def _refresh_navigation_labels(self) -> None:
        """切语言后重置侧边栏 4 个 NavigationItem 的 label。

        MSFluentWindow.addSubInterface 把 text 存进 NavigationItem.setText，
        Qt 不会自动重译。走 navigationInterface.widget(routeKey) 拿到底层
        NavigationItem 再 setText。
        """
        nav = getattr(self, "navigationInterface", None)
        if nav is None:
            return
        mapping = {
            getattr(self, "editorInterface", None): self.tr("打轴"),
            getattr(self, "exportInterface", None): self.tr("导出"),
            getattr(self, "singerInterface", None): self.tr("演唱者"),
            getattr(self, "settingInterface", None): self.tr("设置"),
        }
        for iface, label in mapping.items():
            if iface is None:
                continue
            try:
                item = nav.widget(iface.objectName())
                if item is not None and hasattr(item, "setText"):
                    item.setText(label)
            except Exception:
                pass

    def _on_theme_changed(self):
        """主题变化时更新 Win10 兜底背景色"""
        try:
            self._apply_win10_fallback_bg()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[MainWindow] _on_theme_changed 失败: %s",
                e, exc_info=True)
            self._store.error_notify.emit("主题切换异常", str(e))

    def _on_error_notify(self, title: str, message: str):
        """全局兜底错误通知：攒批 3 秒，只展示最新一条，避免信号风暴弹出多个 toasts。"""
        self._pending_error_title = title
        self._pending_error_msg = message
        if not self._error_notify_timer.isActive():
            self._error_notify_timer.start()

    def _show_pending_error(self):
        title = self._pending_error_title or ""
        msg = self._pending_error_msg or ""
        self._pending_error_title = None
        self._pending_error_msg = None
        from qfluentwidgets import InfoBar, InfoBarPosition
        from PyQt6.QtCore import Qt
        InfoBar.error(
            title=title,
            content=msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=7000,
            parent=self,
        )

    def _apply_win10_fallback_bg(self):
        """强制覆盖窗口背景色，确保强制主题模式下 Win11 Mica 不漏色。

        Win11 的 Mica 材质跟随 OS 系统主题，与应用内强制的浅/深色无关。
        若不显式覆盖背景，「强制浅色 + 系统深色」时 Mica 透出深色背景，
        导致整个窗口看起来仍是深色。深色模式同理。

        解决方案：无论深浅色均设置纯色背景，完全覆盖 Mica，保证
        强制模式和跟随系统模式下视觉一致。
        """
        if theme.is_dark:
            bg = theme.bg_primary.name()
            self.setStyleSheet(f"MSFluentWindow {{ background-color: {bg}; }}")
        else:
            # 浅色背景：用 #F9F9F9 而非空字符串，确保覆盖掉任何残留的
            # 深色 Mica 材质（强制浅色 + 系统深色的场景）。
            self.setStyleSheet("MSFluentWindow { background-color: #F9F9F9; }")

    def _init_interfaces(self):
        """初始化所有子界面"""
        from .home.home_interface import HomeInterface
        from .editor.timing_interface import EditorInterface
        from .export.export_interface import ExportInterface
        from .singer.singer_interface import SingerManagerInterface
        from .editor.fulltext_interface import RubyInterface
        from .settings.settings_interface import SettingsInterface
        from .editor.line_interface import EditInterface
        from .online.online_interface import OnlineQueryInterface

        self._report_progress(48, self.tr("正在加载主页..."))
        self.homeInterface = HomeInterface(self)
        self.homeInterface.setObjectName("homeInterface")
        self.homeInterface.project_created.connect(self._on_project_created)
        self.homeInterface.project_opened.connect(self._on_project_opened)
        self.homeInterface.project_save_requested.connect(self._on_save_project)
        self.homeInterface.hide()  # 已废弃，仅保留信号连接

        self._report_progress(55, self.tr("正在加载编辑器..."))
        self.editorInterface = EditorInterface(self)
        self.editorInterface.setObjectName("editorInterface")
        self.editorInterface.set_timing_service(self._timing_service)
        self.editorInterface.project_saved.connect(self._update_title)

        self._report_progress(63, self.tr("正在加载导出界面..."))
        self.exportInterface = ExportInterface(self, embedded=self._embedded)
        self.exportInterface.setObjectName("exportInterface")
        self.exportInterface.export_to_next_requested.connect(
            self.export_to_next_requested.emit
        )

        self._report_progress(68, self.tr("正在加载演唱者管理..."))
        self.singerInterface = SingerManagerInterface(self)
        self.singerInterface.setObjectName("singerInterface")

        self._report_progress(72, self.tr("正在加载注音界面..."))
        self.rubyInterface = RubyInterface(self)
        self.rubyInterface.setObjectName("rubyInterface")
        self.rubyInterface.hide()  # 已废弃，仅保留与 Timing 共用的功能

        self._report_progress(77, self.tr("正在加载设置界面..."))
        self.settingInterface = SettingsInterface(
            self,
            settings_provider=self._settings_provider,
            embedded=self._embedded,
        )
        self.settingInterface.setObjectName("settingInterface")

        self._report_progress(82, self.tr("正在加载编辑视图..."))
        self.editViewInterface = EditInterface(self)
        self.editViewInterface.setObjectName("editViewInterface")
        self.editViewInterface.hide()  # 已废弃，仅保留与 Timing 共用的功能

        self.onlineInterface = OnlineQueryInterface(self)
        self.onlineInterface.setObjectName("onlineInterface")
        self.onlineInterface.hide()  # 已废弃，占位界面

        self._report_progress(85, self.tr("正在连接数据存储..."))
        # 将 store 传递给所有子界面
        self.homeInterface.set_store(self._store)
        self.editorInterface.set_store(self._store)
        self.editViewInterface.set_store(self._store)
        self.exportInterface.set_store(self._store)
        self.singerInterface.set_store(self._store)
        self.rubyInterface.set_store(self._store)
        self.settingInterface.set_store(self._store)

        # 初始广播设置，确保 EditorInterface 等在启动时就能读取设置值
        self._store.notify("settings")

    def _init_navigation(self):
        """初始化侧边栏导航"""
        # 以下 interface 已废弃，仅保留初始化（部分功能被 Timing 界面复用），不注册到侧边栏
        self.addSubInterface(self.editorInterface, FIF.PLAY, self.tr("打轴"))
        self.addSubInterface(self.exportInterface, FIF.SHARE, self.tr("导出"))
        self.addSubInterface(self.singerInterface, FIF.PEOPLE, self.tr("演唱者"))

        # 底部
        self.addSubInterface(
            self.settingInterface,
            FIF.SETTING,
            self.tr("设置"),
            position=NavigationItemPosition.BOTTOM,
        )

        # 默认打轴页面
        self.switchTo(self.editorInterface)

    # ==================== 标签页切换 ====================

    def switchTo(self, interface):
        """切换标签页"""
        self._pause_playback_when_leaving(interface)
        # 重置目标页面的 y 坐标，防止动画被打断时 widget 残留偏移，导致下次动画越来越快
        interface.move(interface.x(), 0)
        # 切换到打轴界面时暂停位置拉取，避免 60fps 刷新与切换动画竞争导致控件抖动
        if hasattr(self, "editorInterface") and interface is self.editorInterface:
            self.editorInterface.pause_poll_for_page_animation()
        # 切换到设置界面时从磁盘重新加载配置
        if hasattr(self, "settingInterface") and interface is self.settingInterface:
            self.settingInterface.reload_from_disk()
        # 切换到导出界面时同步默认格式和输出目录
        if hasattr(self, "exportInterface") and interface is self.exportInterface:
            self.exportInterface._sync_default_format()
            self.exportInterface._sync_default_output_dir()
        self._current_interface = interface
        super().switchTo(interface)

    def _pause_playback_when_leaving(self, interface) -> None:
        """按设置在离开打轴页时暂停正在播放的音乐。"""
        # 只在确实离开打轴页时处理，避免重复切换或进入打轴页时误暂停。
        if (
            hasattr(self, "editorInterface")
            and self._current_interface is self.editorInterface
            and interface is not self.editorInterface
            and self._pause_on_leave_timing_enabled()
            and self._timing_service.is_playing()
        ):
            self.editorInterface._on_pause()

    def _pause_on_leave_timing_enabled(self) -> bool:
        """读取“离开打轴界面时暂停”设置（默认开启）。"""
        try:
            setting_iface = getattr(self, "settingInterface", None)
            if setting_iface is not None:
                return bool(
                    setting_iface.get_settings().get(
                        "audio.pause_on_leave_timing", True
                    )
                )
            from strange_uta_game.frontend.settings.app_settings import AppSettings

            return bool(AppSettings().get("audio.pause_on_leave_timing", True))
        except Exception:
            return True

    # ==================== 项目操作 ====================

    def _on_project_created(self, project: Project, audio_path: str = ""):
        """项目创建完成"""
        self._store.load_project(project, audio_path=audio_path if audio_path else None)

        # 自动加载主页选择的音频
        if audio_path:
            self.editorInterface.load_audio(audio_path)

        self.switchTo(self.editorInterface)

        InfoBar.success(
            title=self.tr("项目创建成功"),
            content=self.tr("共 {n} 行歌词").format(n=len(project.sentences)),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _on_project_opened(self, project: Project, file_path: str = "", extras: dict = None):
        """项目打开完成"""
        self._store.load_project(project, save_path=file_path if file_path else None)
        self.switchTo(self.editorInterface)

        if file_path:
            self._apply_project_extras(extras or {})

        InfoBar.success(
            title=self.tr("项目打开成功"),
            content=self.tr("共 {n} 行歌词").format(n=len(project.sentences)),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

        self._refresh_frameless()

    def _apply_project_extras(self, extras: dict) -> None:
        """应用 .sug 的附加字段（nicokara_tags、media_path）。

        extras 由加载侧（ProjectLoadWorker / 崩溃恢复）单次解析一并带回，
        主线程不再二次解析文件。
        """
        from pathlib import Path

        from strange_uta_game.backend.infrastructure.audio.video_converter import (
            is_video_file,
        )
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        # extras 可能为空（旧版 sug 无 extras 字段），但仍需重置 nicokara_tags，
        # 否则上一个项目残留的 tags 会在保存时回写到当前 sug，造成跨项目污染。

        # nicokara_tags：始终覆盖到 AppSettings；sug 内缺失则 reset 为默认值。
        # 必须写到 SettingsInterface 共享的 _settings 实例（而非新建 AppSettings()），
        # 否则共享实例内存中的旧值会在后续任何 self._settings.save() 时回滚磁盘。
        nicokara_tags = extras.get("nicokara_tags")
        if nicokara_tags is None:
            nicokara_tags = AppSettings.DEFAULT_SETTINGS.get("nicokara_tags", {})
        try:
            setting_iface = getattr(self, "settingInterface", None)
            settings = setting_iface.get_settings() if setting_iface else AppSettings()
            settings.set("nicokara_tags", nicokara_tags)
            settings.save()
        except Exception:
            pass

        media_path = extras.get("media_path", "")
        if not media_path:
            return

        if not Path(media_path).exists():
            InfoBar.warning(
                title=self.tr("媒体文件未找到"),
                content=self.tr("上次关联的媒体文件不存在：{name}").format(name=Path(media_path).name),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        self._store.restore_media_path(media_path)

        if is_video_file(media_path):
            self.editorInterface._file_loader._load_video_as_audio(media_path)
        else:
            self.editorInterface.load_audio(media_path)

    def _update_title(self):
        """刷新窗口标题栏：拼接存档文件名与保存状态。"""
        from pathlib import Path
        project = self._store.project if self._store else None
        if not project:
            self.setWindowTitle(self.tr("StrangeUtaGame - 歌词打轴工具 Bilibili@不会说话的呆轩cc"))
            return
        save_path = self._store.save_path if self._store else None
        if save_path and not self._store.is_temp_save_path(save_path):
            name = Path(save_path).name  # 如 "mysong.sug"
        else:
            name = self.tr("未命名")
        dirty_mark = self.tr("[未保存]") if (self._store and self._store.dirty) else ""
        self.setWindowTitle(self.tr(
            "StrangeUtaGame - {name}{dirty_mark} //Bilibili@不会说话的呆轩cc"
        ).format(name=name, dirty_mark=dirty_mark))

    def _on_data_changed(self, change_type: str):
        """响应 store 的数据变更 — 同步非 UI 组件。"""
        try:
            if change_type == "project":
                project = self._store.project
                self._command_manager.clear()
                if project:
                    self._timing_service.set_project(project)
            elif change_type == "settings":
                # 同步打轴偏移到 TimingService
                settings = self.settingInterface.get_settings()
                offset_ms = settings.get("timing.tag_offset_ms", -230)
                self._timing_service.set_timing_offset(offset_ms)
                # 同步自动保存配置到 ProjectStore
                self._apply_auto_save_settings()
                # 按"高质量音频变速"开关切换引擎（仅在实际变化时重建+重载）
                self._apply_audio_engine_setting()
            elif change_type in ("lyrics", "checkpoints"):
                # 歌词/轴点变更后重建全局 checkpoint 列表
                self._timing_service.rebuild_global_checkpoints()
            elif change_type == "audio":
                # 音频路径变更 → 全局加载到 editor
                # 幂等守卫：editor 已加载相同路径时跳过，避免
                # Editor.load_audio → store.set_audio_path → emit("audio")
                # → MainWindow → Editor.load_audio 的重入回环导致 UI 卡死。
                audio_path = self._store.audio_path
                if audio_path and getattr(self.editorInterface, "_audio_file_path", None) != audio_path:
                    self.editorInterface.load_audio(audio_path)
            self._update_title()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[MainWindow] _on_data_changed(%s) 失败: %s",
                change_type, e, exc_info=True)
            self._store.error_notify.emit("数据刷新异常", str(e))

    def _on_command_state_changed(self) -> None:
        try:
            if self._store:
                self._store.mark_dirty()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[MainWindow] _on_command_state_changed 失败: %s",
                e, exc_info=True)
            self._store.error_notify.emit("命令状态异常", str(e))

    # ==================== 音频引擎选择 ====================

    def _hq_speed_enabled(self) -> bool:
        """读取"启用高质量音频变速"设置（默认开启）。

        优先用 settingInterface 的实时值（反映用户刚改的设置）；启动期
        settingInterface 尚未创建时回退到磁盘 AppSettings。
        """
        try:
            setting_iface = getattr(self, "settingInterface", None)
            if setting_iface is not None:
                return bool(setting_iface.get_settings().get("audio.hq_speed_change", True))
            from strange_uta_game.frontend.settings.app_settings import AppSettings

            return bool(AppSettings().get("audio.hq_speed_change", True))
        except Exception:
            return True

    def _make_audio_engine(self):
        """按设置与平台能力创建音频引擎。

        mac（BASS 不可用）始终返回 SoundDeviceEngine；Windows 按 HQ 开关在
        BassTsmEngine / BassEngine 间选择。
        """
        from strange_uta_game.backend.infrastructure.audio import select_audio_engine

        return select_audio_engine(self._hq_speed_enabled())

    def _apply_audio_engine_setting(self):
        """设置变更时按"高质量音频变速"开关切换引擎。

        仅在引擎类型实际改变时重建：释放旧引擎、接入新引擎，并重载当前曲目
        （位置重置为 0）。切换是用户在设置里的低频操作，可接受短暂中断。
        """
        from strange_uta_game.backend.infrastructure.audio import bass_available

        # mac（BASS 不可用）：SoundDeviceEngine 始终走离线 TSM，HQ 开关无效，直接返回，
        # 否则下方 isinstance(x, None) 会抛 TypeError。
        if not bass_available:
            return

        from strange_uta_game.backend.infrastructure.audio import BassEngine, BassTsmEngine

        desired = BassTsmEngine if self._hq_speed_enabled() else BassEngine
        if isinstance(self._audio_engine, desired):
            return

        editor = getattr(self, "editorInterface", None)
        audio_path = getattr(editor, "_audio_file_path", None) if editor else None

        new_engine = desired()
        self._timing_service.swap_audio_engine(new_engine)
        self._audio_engine = new_engine
        # 仅换引擎实例（swap_audio_engine）只迁移了位置/渲染回调，编辑器侧的其它
        # 接线（服务回调、焦点/居中信号、preview 引擎引用）不会自动跟到新引擎，
        # 表现为切换后回调丢失、跳转不准、数据未重新初始化。这里重走一遍完整接线
        # （set_timing_service 已做成幂等）并完整重载当前曲目，等价于一次干净的加载。
        if editor is not None:
            editor.set_timing_service(self._timing_service)
            # 重载当前曲目到新引擎（清空守卫与路径以放行重载）
            if audio_path:
                editor._audio_loading = False
                editor._audio_file_path = None
                editor.load_audio(audio_path)

        InfoBar.success(
            title=self.tr("音频引擎已切换"),
            content=(self.tr("已启用高质量变速（离线预渲染）") if desired is BassTsmEngine
                     else self.tr("已关闭高质量变速（实时变速，可能爆音）")),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=3000, parent=self,
        )

    # ==================== 自动保存配置 ====================

    def _apply_auto_save_settings(self):
        """从 AppSettings 读取自动保存配置并应用到 ProjectStore。"""
        settings = self.settingInterface.get_settings()
        enabled = settings.get("auto_save.enabled", True)
        interval = settings.get("auto_save.interval_minutes", 5)
        self._store.set_periodic_save_config(enabled, interval)
        crash_recovery = settings.get("auto_save.crash_recovery_enabled", True)
        self._store.set_crash_recovery_enabled(crash_recovery)

    # ==================== 闪退恢复 ====================

    @staticmethod
    def has_pending_crash_recovery() -> bool:
        """是否存在待恢复的闪退临时文件（公开 API，供宿主在弹窗前查询）。"""
        return ProjectStore.has_crash_recovery()

    def _startup_checks(self) -> None:
        """启动期检查入口：检查更新 → 更新解决后闪退恢复 → 链尾清缓存。

        闪退恢复弹窗和更新检测弹窗都是模态的，必须串行。更新优先：
        用户确认更新则直接 force quit（不弹恢复，保留 temp 供更新后重启使用）；
        用户跳过/取消/无更新时，再做闪退恢复。缓存清理延后到启动链最末
        （见 :meth:`_maybe_clear_audio_cache`）。
        """
        self._check_for_app_update()

    @staticmethod
    def _clear_all_audio_cache() -> None:
        """启动时清理所有缓存的音频文件（视频提取 mp3、TSM 缓存、fallback wav 等）。

        所有缓存均可安全删除 — 项目 .sug 保存的是原始媒体路径，打开时会重新提取；
        TSM 渲染缓存引擎会在下一首歌加载时自动重建。
        """
        try:
            from strange_uta_game import app_dirs
            cache_dir = app_dirs.cache_dir()
            if not cache_dir.exists():
                return
            # 1. 清除 extracted/ 子目录（视频提取的 mp3 + 引擎 fallback wav）
            extracted = cache_dir / "extracted"
            if extracted.exists():
                for f in extracted.glob("*"):
                    try:
                        if f.is_file():
                            f.unlink()
                    except Exception:
                        pass
            # 2. 清除 cache 根目录下的音频缓存（TSM source / render / fallback wav）
            for f in cache_dir.glob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() in (".mp3", ".wav"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    def check_crash_recovery(self, dialog_parent: "Optional[object]" = None) -> bool:
        """检查是否有闪退恢复文件，并询问用户是否加载。

        standalone 模式由 :meth:`__init__` 启动期定时器自动触发，embedded 模式
        由宿主在窗口显示后显式调用，并传入自己的顶层窗口作为弹窗 parent，
        避免 QMessageBox 出现在尚未可见的子 widget 上。

        Returns:
            True 表示用户选择恢复且成功加载了项目；其余情况均为 False
            （包括没有恢复文件、用户拒绝、加载失败）。
        """
        if not ProjectStore.has_crash_recovery():
            return False

        parent = dialog_parent if dialog_parent is not None else self
        if message_question(
            parent,
            self.tr("恢复未保存的项目"),
            self.tr("检测到上次异常退出时的未保存项目数据。\n是否加载恢复？"),
            yes_text=self.tr("是"),
            no_text=self.tr("否"),
        ):
            recovered = ProjectStore.load_crash_recovery()
            if recovered:
                project, temp_path, extras = recovered
                self._store.load_project(project)
                # 从 .sug.temp 回灌 nicokara_tags / media_path 到 AppSettings 与 store。
                # 不把 temp_path 当 save_path，以免用户误存到 .cache。
                self._apply_project_extras(extras)
                self.switchTo(self.editorInterface)
                InfoBar.success(
                    title=self.tr("恢复成功"),
                    content=self.tr("已恢复 {n} 行歌词").format(n=len(project.sentences)),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )
                # 恢复后删除临时文件
                ProjectStore.delete_crash_recovery()
                self._refresh_frameless()
                return True
            else:
                InfoBar.error(
                    title=self.tr("恢复失败"),
                    content=self.tr("无法读取恢复文件，文件可能已损坏"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )
                ProjectStore.delete_crash_recovery()
                return False
        else:
            # 用户拒绝恢复 → 删除临时文件
            ProjectStore.delete_crash_recovery()
            return False

    # ==================== 自动更新检查 ====================

    def _check_for_app_update(self) -> None:
        """启动时的轻量自动更新检查。

        实际逻辑全部委托给 ``strange_uta_game.updater``；任何异常均吞掉并仅写日志，
        以确保更新模块的故障不会影响主程序使用。

        更新检查完成且无需退出时，会通过 ``_on_update_check_done`` 继续闪退恢复流程。
        """
        try:
            from strange_uta_game.updater.settings import UpdaterSettings
            from strange_uta_game.updater.worker import UpdateChecker

            settings = UpdaterSettings.load(self.settingInterface.get_settings())
            if not (settings.enabled and settings.check_on_startup):
                self._on_update_check_done()
                return

            # 启动期检查：受 ``min_check_interval_hours`` 防抖限制
            self._update_checker = UpdateChecker(settings, manual=False, parent=self)
            self._update_checker.finished.connect(self._on_startup_update_check)
            self._update_checker.start()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "启动更新检查失败，已忽略", exc_info=True
            )
            self._on_update_check_done()

    def _on_update_check_done(self) -> None:
        """更新检查已解决（无更新 / 用户跳过 / 取消 / 出错），继续闪退恢复。"""
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(500, self._on_update_check_done)
            return
        # 启动即拖入/关联文件打开时跳过闪退恢复（见 _opened_initial_files 注释）。
        if not self._opened_initial_files:
            self.check_crash_recovery()
        QTimer.singleShot(500, self._schedule_network_dict_auto_update)
        # 缓存清理放启动链最末：所有可能加载内容的环节（拖入加载、恢复
        # 加载、更新弹窗）都已结束，仅当本会话无音频在用时执行。
        self._maybe_clear_audio_cache()

    def _maybe_clear_audio_cache(self) -> None:
        """启动链末尾清缓存；音频在用（已载/在载/视频提取在途）则本次跳过。

        清理会删掉 cache/extracted/ 与缓存根下的全部 mp3/wav，只有「本会话
        尚未加载任何音频」时才安全；跳过仅意味着顺延到下一次启动。恢复带
        媒体的项目、拖入音频/视频启动等场景均由此守卫覆盖。
        """
        if self._audio_in_use():
            return
        self._clear_all_audio_cache()

    def _audio_in_use(self) -> bool:
        """本会话是否已有音频加载完成、正在加载或视频提取在途。"""
        editor = self.editorInterface
        loader = getattr(editor, "_file_loader", None)
        return bool(
            self._store.audio_path
            or getattr(editor, "_audio_loading", False)
            or (loader is not None and loader._loading_thread is not None)
        )

    def _on_startup_update_check(self, result_obj: object) -> None:
        """处理启动期 UpdateChecker 的回调。"""
        # 如果当前已有其他模态组件活跃（如用户正在 QFileDialog 保存文件），
        # 延迟 500ms 重试整个回调，避免两个模态弹窗叠加导致 UI 死锁。
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(500, lambda: self._on_startup_update_check(result_obj))
            return

        try:
            from strange_uta_game.__version__ import __version__
            from strange_uta_game.updater.settings import UpdaterSettings
            from strange_uta_game.updater.sources import SOURCE_LABELS
            from strange_uta_game.updater.ui.update_dialog import UpdateAvailableDialog
            from strange_uta_game.updater import installer as upd_installer

            result = result_obj  # type: ignore[assignment]
            if not getattr(result, "ok", False) or not getattr(result, "has_update", False):
                self._on_update_check_done()
                return
            release = getattr(result, "release", None)
            if release is None:
                self._on_update_check_done()
                return

            # 用户曾经点击「跳过此版本」 → 静默忽略
            settings = UpdaterSettings.load(self.settingInterface.get_settings())
            if settings.skipped_version and settings.skipped_version == release.version:
                self._on_update_check_done()
                return

            # 记录最近一次发现的远端版本（仅用于以后扩展，例如"侧栏红点"）
            settings.last_seen_version = release.version
            try:
                settings.save(self.settingInterface.get_settings())
            except Exception:
                pass

            primary_source = getattr(result, "primary_source", "")
            source_label = SOURCE_LABELS.get(primary_source, "") if primary_source else ""

            dlg = UpdateAvailableDialog(
                release,
                local_version=__version__,
                primary_source_label=source_label,
                all_releases=getattr(result, "all_releases", None) or [],
                parent=self,
            )
            accepted = dlg.exec()
            choice = dlg.user_choice

            if choice == "skip":
                settings.skipped_version = release.version
                try:
                    settings.save(self.settingInterface.get_settings())
                except Exception:
                    pass
                self._on_update_check_done()
                return
            if not accepted or choice == "later":
                self._on_update_check_done()
                return

            # 用户确认更新 → 启动 Updater.exe 并退出
            if not upd_installer.is_updater_available():
                import sys

                if sys.platform == "darwin":
                    # mac 无自动安装 handoff：直接打开 release 页面供手动下载。
                    from PyQt6.QtCore import QUrl
                    from PyQt6.QtGui import QDesktopServices
                    from strange_uta_game.__version__ import REPO_OWNER, REPO_NAME

                    QDesktopServices.openUrl(
                        QUrl(
                            f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/latest"
                        )
                    )
                    InfoBar.success(
                        title=self.tr("已打开下载页面"),
                        content=self.tr(
                            "macOS 暂不支持自动更新，已在浏览器打开最新版本下载页。"
                        ),
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=6000,
                        parent=self,
                    )
                else:
                    InfoBar.warning(
                        title=self.tr("更新器未就绪"),
                        content=self.tr("未找到 Updater.exe。请到 GitHub 手动下载完整安装包。"),
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=6000,
                        parent=self,
                    )
                self._on_update_check_done()
                return

            from strange_uta_game.updater.proxy import resolve_proxy
            info, _ = resolve_proxy(settings.proxy_mode, settings.proxy_manual_url)
            proxy_url = info.url if info and info.is_valid else ""

            plan = upd_installer.LaunchPlan(
                app_dir=upd_installer.find_app_dir(),
                app_exe_name=upd_installer.find_app_exe_name(),
                target_version=release.version,
                target_tag=release.tag,
                asset_name=result.primary_asset_name,
                download_urls=list(result.download_candidates),
                proxy_url=proxy_url,
                locale=upd_installer._get_current_locale(),
            )

            # launch_updater 内部会调用 _update_updater_from_remote 发起 HTTP 请求，
            # 同步调用会冻结 UI；改为在后台线程中执行（与 update_card.py 保持一致）。
            from strange_uta_game.updater.ui.update_card import _LaunchUpdaterWorker
            from strange_uta_game.updater.ui.update_progress_window import UpdateProgressWindow

            progress_win = UpdateProgressWindow()
            progress_win.show()

            worker = _LaunchUpdaterWorker(plan, parent=None)
            self._startup_update_launch_worker = worker  # 防 GC
            self._startup_update_progress_win = progress_win  # 防 GC

            worker.progress.connect(
                progress_win.update_from_text, Qt.ConnectionType.QueuedConnection,
            )
            progress_win.cancelled.connect(worker.request_cancel)

            def _on_launch_done(launch_result: object) -> None:
                from strange_uta_game.updater import installer as _inst
                lr: _inst.LaunchResult = launch_result  # type: ignore[assignment]

                if progress_win._cancelled_by_user:
                    return

                progress_win.finish()

                if lr.reason == "用户取消更新":
                    InfoBar.info(
                        title=self.tr("更新已取消"),
                        content=self.tr("您可以稍后在设置中重新检查更新"),
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=4000,
                        parent=self,
                    )
                    self._on_update_check_done()
                    return

                if not lr.launched:
                    InfoBar.error(
                        title=self.tr("启动 Updater 失败"),
                        content=lr.reason or self.tr("未知错误"),
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=6000,
                        parent=self,
                    )
                    self._on_update_check_done()
                    return

                InfoBar.success(
                    title=self.tr("更新已启动"),
                    content=self.tr("即将退出应用，由 Updater 完成替换并自动重启…"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3500,
                    parent=self,
                )
                # 用强制退出而非 QApplication.quit()
                # 注意：force quit 路径会保存 .sug.temp，不调 _on_update_check_done，
                # 保留闪退恢复数据供更新后重启使用。
                QTimer.singleShot(1200, self.request_force_quit)

            worker.done.connect(_on_launch_done, Qt.ConnectionType.QueuedConnection)
            worker.start()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "处理启动更新回调时异常，已忽略", exc_info=True
            )
            self._on_update_check_done()

    def _schedule_network_dict_auto_update(self) -> None:
        """启动期网络词典自动更新调度（独立于应用版本检查）。

        由 ``__init__`` 中 ``QTimer.singleShot(3000, ...)`` 触发；HTTP 拉取
        放在 daemon 线程，绝不阻塞 UI。是否真的拉取由
        ``AppSettings.maybe_auto_update_network_dictionary`` 内部根据
        ``network_dictionary.auto_update.{enabled, interval_value, interval_unit}``
        与 ``last_auto_update_at`` 判断。
        """
        import threading

        def _worker() -> None:
            try:
                from strange_uta_game.frontend.settings.app_settings import AppSettings
                AppSettings().maybe_auto_update_network_dictionary()
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "网络词典自动更新失败，已忽略", exc_info=True
                )

        try:
            threading.Thread(
                target=_worker, daemon=True, name="net-dict-auto-update"
            ).start()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "网络词典自动更新调度失败，已忽略", exc_info=True
            )

    # ==================== 启动/关联文件打开 ====================

    def _require_ready_file_loader(self):
        """取打轴页 FileLoader；未就绪时记日志并返回 None（不重试）。

        主窗口构造完成后 file_loader 与 timing_service 恒已就绪
        （``set_timing_service`` 在 ``__init__`` 内同步完成），本守卫仅作
        契约兜底——调用方必须在构造完成后才发起加载，禁止定时器盲等。
        """
        file_loader = getattr(self.editorInterface, "_file_loader", None)
        if file_loader is not None and file_loader._timing_service is not None:
            return file_loader
        import logging

        logging.getLogger(__name__).error(
            "[MainWindow] 加载请求到达时 file_loader/timing_service 未就绪，已丢弃"
        )
        return None

    def open_initial_files(self, file_paths: list) -> None:
        """打开拖入/关联的受支持文件（异步加载）。

        独立运行时把文件拖到程序图标（或双击关联文件）经命令行参数传入；
        运行期 macOS 关联文件经 QFileOpenEvent 到达这里。分流规则：
        - ``.sug`` → 走 :meth:`open_initial_project` 直接打开项目；
        - 歌词 / 音频 / 视频 → 新建项目并直接加载对应文件；同时拖入
          歌词与音频时合并进同一个新项目。

        分类与加载均复用打轴页 FileLoader（拖入窗口的同一套路径）。必须在
        主窗口构造完成后调用：届时全部界面与 timing_service 已同步就绪，
        加载 worker 立即启动，其结果经事件循环送达，无需任何延迟。
        """
        from pathlib import Path

        from strange_uta_game.frontend.editor.timing.file_loader import (
            classify_supported_file,
        )

        files = [path for path in file_paths if Path(path).is_file()]
        if not files:
            InfoBar.error(
                title=self.tr("无法打开文件"),
                content=self.tr("文件不存在: {path}").format(
                    path=file_paths[0] if file_paths else ""
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        supported = [
            (path, kind) for path, kind in
            ((path, classify_supported_file(path)) for path in files)
            if kind
        ]
        if not supported:
            InfoBar.warning(
                title=self.tr("不支持的文件类型"),
                content=self.tr(
                    "仅支持 .sug 项目、歌词（LRC/TXT/KRA/KRL/SRT/ASS）及音频/视频文件"
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        file_loader = self._require_ready_file_loader()
        if file_loader is None:
            return

        # .sug 优先：直接作为项目打开，其余拖入文件忽略。
        project_file = next(
            (path for path, kind in supported if kind == "project"), None
        )
        if project_file is not None:
            self._opened_initial_files = True
            self.open_initial_project(project_file)
            return

        lyric_file = next(
            (path for path, kind in supported if kind == "lyric"), None
        )
        media_file = next(
            (path for path, kind in supported if kind in ("audio", "video")), None
        )

        self.switchTo(self.editorInterface)
        self._opened_initial_files = True

        if lyric_file is not None:
            # load_lyrics 自带「新建项目 + 装入歌词」语义，内含未保存检测
            # （启动阶段尚无项目，检测自动直通）。
            self._store.set_working_dir(lyric_file)
            file_loader.load_lyrics(lyric_file)
        elif media_file is not None:
            # 纯音频/视频：先做未保存检测（启动阶段直通），再新建空项目
            # 加载（与工具栏「新建项目」一致）。
            if not file_loader.check_unsaved_changes():
                return
            file_loader.create_fresh_project()

        if media_file is not None:
            self._store.set_working_dir(media_file)
            file_loader.load_media(media_file)

        self._refresh_frameless()

    def open_initial_project(self, file_path: str, check_unsaved: bool = True) -> None:
        """打开 .sug 项目文件（异步）。

        委托给 editorInterface._file_loader.load_project，与其他加载路径使用同一函数。
        启动时由 open_initial_files 分流到达；运行期 macOS 双击关联文件亦经此
        进入（当前项目有未保存更改且 check_unsaved=True 时会先询问）。
        必须在主窗口构造完成后调用，无需任何延迟重试。
        """
        from pathlib import Path

        if not Path(file_path).is_file():
            InfoBar.error(
                title=self.tr("无法打开文件"),
                content=self.tr("文件不存在: {path}").format(path=file_path),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        file_loader = self._require_ready_file_loader()
        if file_loader is None:
            return

        def _on_success(project: Project, loaded_path: str) -> None:
            self.switchTo(self.editorInterface)
            InfoBar.success(
                title=self.tr("项目打开成功"),
                content=self.tr("共 {n} 行歌词").format(n=len(project.sentences)),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            self._refresh_frameless()

        file_loader.load_project(
            file_path, check_unsaved=check_unsaved, on_success=_on_success
        )

    # ==================== 窗口事件 ====================

    def _on_save_project(self):
        """从任意页面触发保存"""
        self._on_global_save()

    def _on_global_save(self) -> bool:
        """全局 Ctrl+S 保存（异步，委托给 ProjectStore）"""
        if not self._store.project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return False

        self._connect_store_save_signals()

        if self._store.save_path and not self._store.is_temp_save_path():
            return self._store.save(self._store.save_path)
        else:
            suggested = self._store.suggested_save_path(".sug")
            path, _ = QFileDialog.getSaveFileName(
                self,
                self.tr("保存项目"),
                suggested,
                self.tr("StrangeUtaGame 项目 (*.sug);;所有文件 (*.*)"),
            )
            if not path:
                return False
            if not path.endswith(".sug"):
                path += ".sug"

            old_temp = self._store.get_temp_path()
            try:
                if old_temp.exists():
                    old_temp.unlink()
            except Exception:
                pass

            self._store.set_working_dir(path)
            started = self._store.save(path)
            self._refresh_frameless()
            return started

    def _connect_store_save_signals(self) -> None:
        for sig, slot in [
            (self._store.save_started,  self._on_save_started),
            (self._store.save_progress, self._on_save_progress),
            (self._store.save_finished, self._on_save_success),
            (self._store.save_error,    self._on_save_error),
        ]:
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
            sig.connect(slot)

    def _disconnect_store_save_signals(self) -> None:
        for sig, slot in [
            (self._store.save_started,  self._on_save_started),
            (self._store.save_progress, self._on_save_progress),
            (self._store.save_finished, self._on_save_success),
            (self._store.save_error,    self._on_save_error),
        ]:
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    def _on_save_started(self, save_path: str) -> None:
        from pathlib import Path as _Path
        from qfluentwidgets import StateToolTip
        from strange_uta_game.frontend.theme import theme
        if getattr(self, "_save_tooltip", None):
            self._save_tooltip.close()
        self._save_tooltip = StateToolTip(
            self.tr("正在保存"),
            _Path(save_path).name,
            self,
        )
        green = theme.status_complete.name()
        self._save_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        self._save_tooltip.move(self._save_tooltip.getSuitablePos())
        self._save_tooltip.show()

    def _on_save_progress(self, stage: str) -> None:
        tooltip = getattr(self, "_save_tooltip", None)
        if tooltip:
            tooltip.setContent(stage)

    def _on_save_success(self, file_path: str) -> None:
        self._disconnect_store_save_signals()
        tooltip = getattr(self, "_save_tooltip", None)
        if tooltip:
            tooltip.setState(True)
            tooltip.setContent(self.tr("保存完成"))
            tooltip.close()
            self._save_tooltip = None
        # 保存成功后写一份命名备份到 ProjectBackup（受"自动备份项目个数"控制）
        self._store.create_backup()
        self._store.cleanup_temp_files()
        InfoBar.success(
            title=self.tr("保存成功"),
            content=file_path,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )
        self._update_title()

    def _on_save_error(self, error_msg: str) -> None:
        self._disconnect_store_save_signals()
        tooltip = getattr(self, "_save_tooltip", None)
        if tooltip:
            tooltip.close()
            self._save_tooltip = None
        InfoBar.error(
            title=self.tr("保存失败"),
            content=error_msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def closeEvent(self, e):
        """关闭窗口时检查未保存变更并退出。

        ``self._force_quitting=True`` 时由 :meth:`request_force_quit` 设置，
        表示当前流程是 updater 触发的硬退出 —— 不弹"未保存"对话框，改为
        把脏数据兜底写到临时文件（next 启动会触发"闪退恢复"机制），然后立刻退出。

        embedded 模式下，本 widget 的 closeEvent 通常意味着宿主在销毁它，
        宿主自己会管整个应用的退出。这里只把脏数据兜底到临时文件、释放
        音频引擎等资源，**绝不调 ``QApplication.quit()``**（会连带杀掉宿主）。
        宿主应在自己的 closeEvent 里先调 :meth:`flush_unsaved` 做用户级
        交互的脏检查；走到这里时认为脏数据兜底即可。
        """
        if self._embedded:
            try:
                self.flush_unsaved()
            except Exception:
                pass
            if hasattr(self, "editorInterface"):
                try:
                    self.editorInterface.release_resources()
                except Exception:
                    pass
            if self._settings_provider is not None:
                try:
                    from strange_uta_game.frontend.settings.app_settings import AppSettings
                    if AppSettings._default_provider is self._settings_provider:
                        AppSettings.set_default_provider(None)
                except Exception:
                    pass
            # LLM 日志不在退出时清，保留到下次启动以便复盘（启动时再清）。
            e.accept()
            return

        # 退出前确保窗口大小/最大化状态已落盘（防抖定时器可能来不及触发）
        self._save_window_geometry()

        if self._force_quitting:
            # 兜底保存（脏数据写到 .cache/.untitled.sug.temp 或 .项目名.sug.temp）
            try:
                if self._store.dirty:
                    self._store.save_sync_for_exit()
            except Exception:
                pass
            # 释放编辑器资源（音频引擎等）
            if hasattr(self, "editorInterface"):
                try:
                    self.editorInterface.release_resources()
                except Exception:
                    pass
            # LLM 日志保留（启动时清）
            QApplication.quit()
            e.accept()
            return

        if self._store.dirty:
            choice = message_choice(
                self,
                self.tr("未保存的更改"),
                self.tr("项目有未保存的更改，是否在退出前保存？"),
                [self.tr("保存"), self.tr("放弃"), self.tr("取消")],
                default=0,
            )
            if choice == 0:  # 保存
                self._on_save_project()
                # 异步保存可能赶不及在退出前完成 _on_save_success，这里同步补一份
                # 命名备份到 ProjectBackup，确保关闭即有备份兜底。
                self._store.create_backup()
                # 保存后清理临时文件
                self._store.cleanup_temp_files()
            elif choice == 1:  # 放弃
                # 用户主动放弃保存 → 仍写一份备份兜底，再删除临时文件
                self._store.create_backup()
                self._store.cleanup_temp_files()
            else:
                # 取消或关闭对话框
                e.ignore()
                return
        else:
            # 无脏数据，正常退出 → 清理临时文件
            self._store.cleanup_temp_files()

        # 释放编辑器资源
        if hasattr(self, "editorInterface"):
            self.editorInterface.release_resources()
        # LLM 日志保留（启动时清）
        QApplication.quit()
        e.accept()

    @staticmethod
    def _clear_llm_logs() -> None:
        """启动时清除上次会话遗留的 LLM 请求日志。

        历史方案是「启动 + 退出双清」，但退出清会带走崩溃前的请求/响应，
        无法事后复盘。现仅在启动时清，让上次会话日志在 .cache/llm_ruby/
        持续保留到下次启动。
        """
        try:
            from strange_uta_game.backend.infrastructure.parsers.llm_ruby import (
                clear_llm_logs,
            )
            clear_llm_logs()
        except Exception:
            pass

    def request_force_quit(self) -> None:
        """立即退出主程序（由 updater 流程调用）。

        步骤：

        1. 置位 ``_force_quitting`` 标志（影响 :meth:`closeEvent` 行为）；
        2. 调 :meth:`close` 触发 closeEvent —— 由于 ``_force_quitting=True``，会
           bypass "未保存更改"对话框，自动把脏数据兜底写到 .cache 下的临时文件
           （主程序下次启动会触发"闪退恢复"流程）；
        3. closeEvent 内部已经 `QApplication.quit()`；
        4. 兜底：调度一个 250ms 后的 ``os._exit(0)`` 硬退出，防止 Qt 事件循环
           因为残留 QThread / Modal 未处理事件而拒绝退出 —— 那会导致 Updater
           备份 ``_internal`` 时拿不到写权限。
        """
        import os as _os
        self._force_quitting = True
        try:
            self.close()
        except Exception:
            pass
        # 给 Qt 一点时间走完 close 流程；超时强制退出
        QTimer.singleShot(250, lambda: _os._exit(0))

    # ==================== 嵌入式宿主接口 ====================
    #
    # 以下方法在 standalone 与 embedded 两种模式下都可用，
    # 主要供宿主程序（krok-helper）在自己的快捷键/closeEvent/标题栏
    # 等流程里调用，以接管打轴模块的"保存 / 未保存检查"等用户交互。

    def trigger_save(self) -> bool:
        """请求保存当前项目（公开 API）。

        宿主可把自己顶层的 Ctrl+S 快捷键转发到本方法；行为等同于
        standalone 模式下用户按 Ctrl+S。返回值仅表示是否成功发起异步
        保存；真正完成或失败分别通过 ``project_save_finished`` 与
        ``project_save_failed`` 信号通知。
        """
        return self._on_global_save()

    def export_to_next_payload(self) -> "Optional[dict[str, object]]":
        """返回供嵌入式宿主送往下一模块的完整项目快照。

        项目对象和 Nicokara 标签均深拷贝，避免宿主后续处理意外修改
        SUG 当前编辑状态。媒体路径同时包含原始素材与当前播放音频，宿主可
        优先使用原始素材，并在其缺失时回退到音频。

        ``axis_plan`` 为分色分轴计划（纯 dict 快照）：导出页「进入下一步」
        在过滤器启用时经轴分组对话框写回 ``project.axis_groups``，本方法
        派生成宿主可直接消费的结构；空分组列表 = 单轴（mode=single）。
        """
        project = self._store.project if self._store is not None else None
        if project is None:
            return None

        tags: object = {}
        try:
            settings = self.settingInterface.get_settings()
            tags = settings.get("nicokara_tags") or {}
        except Exception:
            pass

        media_path = None
        try:
            media_path = self._store.get_saveable_media_path()
        except Exception:
            media_path = self._store.original_media_path

        media_kind = None
        if media_path:
            try:
                from strange_uta_game.backend.infrastructure.audio.video_converter import (
                    VIDEO_EXTENSIONS,
                )
                from pathlib import Path

                suffix = Path(media_path).suffix.lower()
                media_kind = (
                    "video"
                    if suffix in VIDEO_EXTENSIONS and suffix != ".dts"
                    else "audio"
                )
            except Exception:
                media_kind = None

        return {
            "project": deepcopy(project),
            "nicokara_tags": deepcopy(tags) if isinstance(tags, dict) else {},
            "source_path": self._store.save_path,
            "media_path": media_path,
            "media_kind": media_kind,
            "audio_path": self._store.audio_path,
            # 分色分轴计划（嵌入「进入下一步」经轴分组对话框写回
            # project.axis_groups；空 = 单轴）。.sug 落盘后宿主也可从
            # 文件读到同一份数据，这里冗余一份纯 dict 快照便于即时消费。
            "axis_plan": _build_axis_plan(project),
        }

    def import_lyrics_from_text(self, content: str) -> bool:
        """从宿主传入的文本直接导入歌词（公开 API）。

        krok-helper 的歌词检索页会先按用户当前的预览选项筛掉介绍、
        选择原文/译文和逐行/按字格式，再把最终文本交给本方法。
        """
        if not content or not content.strip():
            InfoBar.warning(
                title=self.tr("歌词为空"),
                content=self.tr("没有可导入的歌词内容"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return False

        file_loader = getattr(getattr(self, "editorInterface", None), "_file_loader", None)
        if file_loader is None:
            InfoBar.error(
                title=self.tr("无法导入"),
                content=self.tr("打轴编辑器尚未准备好"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return False

        self.switchTo(self.editorInterface)
        file_loader.load_lyrics_from_text(content)
        return True

    def has_unsaved_changes(self) -> bool:
        """报告当前是否有未保存的脏数据（公开 API）。

        宿主可在自己的 closeEvent 里调用，决定是否弹"未保存"对话框。
        """
        return bool(self._store and self._store.dirty)

    def flush_unsaved(self) -> None:
        """把脏数据兜底写到闪退恢复临时文件（公开 API）。

        宿主在销毁本 widget 前调用，等价于 standalone 的硬退出兜底路径。
        失败静默，绝不抛异常。
        """
        try:
            if self._store and self._store.dirty:
                self._store.save_sync_for_exit()
        except Exception:
            pass

    def on_host_visibility_changed(self, visible: bool) -> None:
        """接收宿主对整个 SUG 控件的可见性生命周期通知（公开 API）。

        宿主应在切出 SUG 前传入 ``False``，在切入 SUG 后传入 ``True``。
        离开意味着整个 SUG 控件被宿主隐藏，只要音频仍在播放就立即暂停，
        不依赖“离开打轴界面时暂停”设置——那项设置只约束 SUG 内部的
        页面切换；进入和离开都会从音频服务重新同步编辑器模式，避免
        音频已暂停但快捷键仍处于打轴模式。

        本方法可重复调用：只有实际仍在播放时才会触发暂停。

        可见性同时转发给前后台节流器（background_throttle）：嵌入式下编辑器
        按 ``self.window()`` 自动判定解析到的是宿主顶层窗口，宿主隐藏 SUG
        区域而宿主窗口仍可见时，只有这条显式通知能让非音频服务降频。
        """
        set_visibility_override(visible)
        editor = getattr(self, "editorInterface", None)
        timing_service = getattr(self, "_timing_service", None)
        if editor is None:
            return

        if (
            not visible
            and timing_service is not None
            and timing_service.is_playing()
        ):
            editor._on_pause()

        sync_mode = getattr(editor, "sync_playback_mode", None)
        if callable(sync_mode):
            sync_mode()

    @staticmethod
    def for_embedding(
        parent: "Optional[object]" = None,
        settings_provider=None,
        ai_timing_host=None,
    ) -> "MainWindow":
        """创建一个可作为子 widget 嵌入宿主程序的 MainWindow 实例。

        本方法走轻量嵌入路径：构造时传 ``embedded=True`` 跳过所有顶层
        窗口行为；再把窗口标志改成 :class:`Qt.WindowType.Widget`，并
        ``setParent(parent)`` 让它成为宿主子 widget 而不是独立窗口。

        宿主接收后只需把返回的实例 ``addWidget`` 到自己的 layout / stacked
        widget 即可。

        参数:
            parent: 宿主中将容纳本 widget 的父 widget。可以为 None
                （后续宿主自己 ``setParent``）。
            settings_provider: 宿主设置桥（读写 SUG 设置节）。
            ai_timing_host: AI 打轴宿主能力（阶段 G 契约，
                :class:`...ai_timing.host.AiTimingHost`）；None 表示
                宿主未提供，AI 打轴回落 standalone 默认配置。

        返回:
            一个 ``MainWindow`` 实例，``_embedded=True``，无顶层
            窗口装饰，可以直接 ``addWidget`` 到任意 layout 中。
        """
        instance = MainWindow(
            embedded=True,
            settings_provider=settings_provider,
            ai_timing_host=ai_timing_host,
        )
        instance.setWindowFlags(Qt.WindowType.Widget)
        instance._remove_embedded_title_bar()
        instance._apply_embedded_ui_policy()
        if parent is not None:
            instance.setParent(parent)  # type: ignore[arg-type]
        instance.winId()
        return instance
