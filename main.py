"""StrangeUtaGame 应用程序入口。

启动歌词打轴软件的主入口点。
"""

import sys
from pathlib import Path

# 添加 src 到路径
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

# DPI awareness 必须在 QApplication 创建时确定。先读取纯数据设置，再为 Qt
# 构造独立参数列表；原始 sys.argv 留给下方的文件关联打开逻辑使用。
from strange_uta_game.frontend.dpi_policy import (
    HIGH_DPI_SCALING_KEY,
    build_qt_argv,
)
from strange_uta_game.frontend.settings.app_settings import AppSettings

settings = AppSettings()
_qt_argv = build_qt_argv(
    sys.argv,
    high_dpi_scaling=bool(settings.get(HIGH_DPI_SCALING_KEY, True)),
)

# 设置 Windows 任务栏图标（AppUserModelID）必须在 QApplication 创建之前调用
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "xuancc.strangeutagame.app.1"
        )
    except Exception:
        pass

# 必须先创建 QApplication，再导入任何 QWidget
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QIcon

# 高分屏开启时保留 Qt 6 的分数缩放；DPI Unaware 下比例为 1，此策略无副作用。
QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)


class SUGApplication(QApplication):
    """处理 macOS 双击关联文件（QFileOpenEvent）。

    mac 上双击 ``.sug`` 走 Apple Event（QFileOpenEvent），不进 ``sys.argv``；
    启动期窗口尚未就绪时先缓存路径，``main()`` 设置 file_open_handler 后冲放。
    """

    def __init__(self, argv):
        super().__init__(argv)
        self.file_open_handler = None  # main() 创建窗口后赋值
        self._pending_file = None

    def event(self, e):
        if e.type() == QEvent.Type.FileOpen:
            path = e.file()
            if self.file_open_handler:
                self.file_open_handler(path)
            else:
                self._pending_file = path
            return True
        return super().event(e)


# 创建应用实例
app = SUGApplication(_qt_argv)

# 所有弹窗都作为普通窗口显示：即使调用方使用 QDialog.exec() 等待返回值，
# 也不禁用主窗口或其他弹窗，避免多个模态窗口互相争抢导致无法操作。
from strange_uta_game.frontend.dialog_policy import install_non_modal_dialog_policy

install_non_modal_dialog_policy(app)

# 确定图标路径（后续多次使用）
_icon_path = (
    Path(__file__).parent / "src" / "strange_uta_game" / "resource" / "icon.ico"
)
if not _icon_path.exists():
    # PyInstaller 打包后的路径
    _base = getattr(sys, "_MEIPASS", Path(__file__).parent)
    _icon_path = Path(_base) / "strange_uta_game" / "resource" / "icon.ico"

# 初始化主题管理器（必须在创建主窗口之前）
from strange_uta_game.frontend.theme import theme

# 闪屏早于 MainWindow 创建，因此先按设置准备应用字体；MainWindow 随后安装
# translator 时会再次确认有效语言。这样启动阶段也不会短暂使用中文 UI 字体。
from strange_uta_game.frontend.font_utils import set_ui_font_override, set_ui_language
from strange_uta_game.frontend.localization import (
    AUTO_LANGUAGE_CODE,
    language_by_code,
    resolve_auto_language,
)

_selected_language = language_by_code(settings.get("ui.language", "auto"))
_font_language = (
    resolve_auto_language()
    if _selected_language.code == AUTO_LANGUAGE_CODE
    else _selected_language.code
)
set_ui_font_override(settings.get("ui.interface_font", ""))
set_ui_language(_font_language)

# 翻译器安装迁移到 MainWindow.__init__（在 super().__init__() 之前）——
# 让入口对语言初始化无感、嵌入式场景也能正常工作。

theme_value = settings.get("ui.theme", "auto")
from strange_uta_game.frontend.theme import ThemeMode
mode_map = {
    "light": ThemeMode.LIGHT,
    "dark": ThemeMode.DARK,
    "auto": ThemeMode.AUTO,
}
theme.mode = mode_map.get(theme_value, ThemeMode.AUTO)

# 应用 qfluentwidgets 主题色和明暗模式（在创建任何 qfluentwidgets 控件之前）
from qfluentwidgets import setThemeColor, setTheme, Theme
setThemeColor("#FF6B6B", lazy=True)
setTheme(Theme.DARK if theme.is_dark else Theme.LIGHT, lazy=True)

# 在主题初始化完成后设置应用图标，避免 setTheme 内部重置图标
if _icon_path.exists():
    app.setWindowIcon(QIcon(str(_icon_path)))

# 显示启动闪屏（在 MainWindow 初始化之前，让用户看到加载进度）
from strange_uta_game.frontend.splash_screen import SplashWindow
_splash_icon = _icon_path.parent / "mascot.png"
if not _splash_icon.exists():
    _splash_icon = _icon_path
_splash = SplashWindow(str(_splash_icon) if _splash_icon.exists() else "")
_splash.show()
app.processEvents()

# 清理上次会话残留的 LLM 请求日志（每次启动从干净状态开始）
try:
    from strange_uta_game.backend.infrastructure.parsers.llm_ruby import clear_llm_logs
    clear_llm_logs()
except Exception:
    pass

# 清理上次更新遗留的 _internal 临时副本（更新器将此目录复制到 TEMP 以解除文件锁，
# 更新完成后主程序启动时清理，避免长期占用磁盘空间）
try:
    import shutil
    from pathlib import Path as _P
    import tempfile as _tp
    _tmp_internal = _P(_tp.gettempdir()) / "StrangeUtaGameUpdater" / "_internal"
    if _tmp_internal.exists():
        shutil.rmtree(str(_tmp_internal), ignore_errors=True)
except Exception:
    pass

# 现在可以安全导入其他模块
from strange_uta_game.frontend.main_window import MainWindow


def _force_taskbar_icon(window, icon_path: Path) -> None:
    """在窗口显示后强制刷新 Windows 任务栏图标。

    Qt 的 setWindowIcon 在 python.exe 宿主进程下有时无法正确更新任务栏，
    需要直接通过 Win32 API 向 HWND 发送 WM_SETICON 并通知 Shell 刷新。
    """
    if sys.platform != "win32" or not icon_path.exists():
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        # 加载图标（大图标 32x32，小图标 16x16）
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        hicon_big = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        hicon_small = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )

        hwnd = int(window.winId())
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)

    except Exception:
        pass


def main():
    """应用入口"""
    # Windows 日文 locale (cp932) 下 stdout 无法输出某些 Unicode 字符（如 U+29F8 ⧸、
    # U+301C 〜），强制切到 UTF-8 与其他入口 (build.py / updater_app) 保持一致。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    # 从命令行参数中提取受支持的文件路径（双击关联打开 / 拖到程序图标时传入）。
    # 除 .sug 项目外也接受歌词（LRC/TXT/KRA/KRL）与音频/视频——启动后由
    # open_initial_files 按「打开项目 / 新建项目并加载对应文件」分流。
    from strange_uta_game.frontend.editor.timing.file_loader import (
        classify_supported_file,
    )

    initial_files = []
    for arg in sys.argv[1:]:
        try:
            if Path(arg).is_file() and classify_supported_file(arg):
                initial_files.append(str(Path(arg).resolve()))
        except (OSError, ValueError):
            continue

    # 创建主窗口（通过回调驱动闪屏进度）
    def _on_splash_progress(value: int, text: str) -> None:
        _splash.set_progress(value, text)

    window = MainWindow(progress_callback=_on_splash_progress)
    window.show()
    window.raise_()
    window.activateWindow()
    _splash.finish()

    from PyQt6.QtCore import QTimer

    # 后台预热字体缓存：本地化字体名的磁盘扫描进后台线程，Qt 侧枚举（字体
    # 选择器条目等）延后到主线程空闲，字体库庞大的用户首次打开字体选择器
    # 不再等待。见 frontend/font_cache.py。
    from strange_uta_game.frontend import font_cache

    font_cache.prewarm_async()

    # 在事件循环启动后强制补设图标：
    # QTimer.singleShot(0, _preload) 会在第一个 tick 运行并可能重置图标，
    # 用 100ms 延迟确保在 _preload 之后再补设一次。
    QTimer.singleShot(100, lambda: _force_taskbar_icon(window, _icon_path))

    # 命令行传入的受支持文件在主窗口构造完成后立即打开——全部界面与音频
    # 服务此时已同步就绪，异步加载 worker 随即启动，结果经事件循环送达，
    # 不用定时器盲等事件循环。
    if initial_files:
        window.open_initial_files(initial_files)

    # macOS：双击关联文件不走 sys.argv，而是 QFileOpenEvent；同样放行所有
    # 受支持格式。启动期窗口就绪前缓存的文件在此一并冲放。
    def _open_dropped_file(path: str) -> None:
        if path and classify_supported_file(path):
            window.open_initial_files([path])

    app.file_open_handler = _open_dropped_file
    if app._pending_file:  # 启动期 handler 未就绪时缓存的文件
        _pending = app._pending_file
        app._pending_file = None
        _open_dropped_file(_pending)

    # 运行应用
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
