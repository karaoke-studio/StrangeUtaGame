"""统一数据中心。

ProjectStore 是整个前端的唯一数据来源，替代之前的信号链同步模式。
所有 UI 模块订阅 ``data_changed`` 信号，根据 change_type 决定是否刷新自身。
所有数据变更后调用 ``store.notify(change_type)``，由 store 统一广播并自动保存。
"""

import re
from datetime import datetime
from time import perf_counter

from PyQt6.QtCore import QObject, QThread, pyqtSignal, QTimer
from typing import Callable, Optional
from pathlib import Path

from strange_uta_game import app_dirs
from strange_uta_game.backend.domain import Project
from strange_uta_game.backend.infrastructure.audio.video_converter import (
    audio_display_stem,
)
from strange_uta_game.backend.infrastructure.persistence.sug_io import (
    SugProjectParser,
)
from strange_uta_game.frontend.perf_log import log_elapsed, log_perf_event, perf_enabled
from strange_uta_game.frontend.workers import ProjectSaveWorker


def _get_cache_dir() -> Path:
    """获取缓存目录（与 video_converter / tsm_cache 同源）。

    解析逻辑见 :mod:`strange_uta_game.app_dirs`：``SUG_CACHE_DIR`` 最高优先，
    macOS 用 ``~/Library/Caches``，其余平台用程序目录下的 ``.cache``。

    注意：本目录仅用于媒体提取等真·缓存；项目临时文件（周期保存 / 闪退恢复）
    已迁移到备份目录下的 :func:`_temp_dir`，旧位置仅做升级兼容扫描。
    """
    return app_dirs.cache_dir()


def _cache_dir() -> Path:
    return _get_cache_dir()


def _backup_root_dir() -> Path:
    """项目备份根目录。读取用户设置 ``auto_save.backup_dir``（留空用默认位置）。"""
    custom = ""
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        custom = AppSettings().get("auto_save.backup_dir", "") or ""
    except Exception:
        custom = ""
    return app_dirs.backup_dir(custom)


def _temp_dir() -> Path:
    """项目临时文件目录：备份根目录下的隐藏子目录 ``.temp``。

    存放周期自动保存与闪退恢复用的 ``.sug.temp`` 文件，与用户可见的命名
    备份文件分开，避免污染备份列表。
    """
    return _backup_root_dir() / ".temp"


def _untitled_temp_path() -> Path:
    return _temp_dir() / ".untitled.sug.temp"


class ProjectStore(QObject):
    """统一数据中心 — 替代信号链的集中式数据管理。

    Change types:
        "project"      — 项目加载/创建（全量刷新）
        "audio"        — 音频路径变更
        "rubies"       — 注音变更
        "singers"      — 演唱者变更
        "lyrics"       — 歌词文本/字符变更
        "timetags"     — 时间标签变更
        "checkpoints"  — 节奏点变更
        "settings"     — 应用设置变更
    """

    # 单一变更通知信号
    data_changed = pyqtSignal(str)  # change_type

    # 手动保存生命周期信号
    save_started = pyqtSignal(str)    # save_path（线程启动前同步触发）
    save_progress = pyqtSignal(str)   # stage description（来自 worker 线程）
    save_finished = pyqtSignal(str)   # saved_path
    save_error = pyqtSignal(str)      # error_msg

    # 全局错误通知（供 _on_data_changed 等信号处理器在兜底时发射）
    error_notify = pyqtSignal(str, str)  # title, message

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)

        self._project: Optional[Project] = None
        self._save_path: Optional[str] = None
        self._audio_path: Optional[str] = None
        # 用户加载的原始媒体文件路径（音频直接路径 或 视频原始路径）。
        # 与 _audio_path 的区别：视频加载后 _audio_path 为 .cache 提取音频，
        # 而此字段始终存储原始文件路径，用于持久化到 .sug。
        self._original_media_path: Optional[str] = None
        # 最近一次被加载/导入的歌词文件所在目录（不持久化，仅运行时使用，
        # 优先级介于 audio 与 last_export_dir 之间）
        self._last_lyric_dir: Optional[str] = None
        self._dirty = False
        # 每次 load_project 递增，用于防止异步保存回调在项目被替换后覆盖 _save_path。
        self._load_count: int = 0
        # 编辑代数：任何把 _dirty 置 True 的修改都会递增。手动保存发起时记录
        # 当前值，完成回调仅在代数未变（保存窗口期没有新编辑）时清 dirty，
        # 避免异步写盘期间的新编辑被误标"已保存"而静默丢失。
        self._edit_epoch: int = 0

        # 防抖保存到 .sug.temp（2 秒无操作后写，用于闪退恢复）
        self._periodic_save_timer = QTimer(self)
        self._periodic_save_timer.setSingleShot(True)
        self._periodic_save_timer.setInterval(2000)
        self._periodic_save_timer.timeout.connect(self._do_periodic_save)

        # 定时 auto-save（周期性保存到 .autosave）
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setInterval(5 * 60 * 1000)  # 默认 5 分钟
        self._auto_save_timer.timeout.connect(self._do_auto_save)
        self._periodic_save_enabled = True
        self._crash_recovery_enabled = True
        self._auto_save_defer_predicate: Optional[Callable[[], bool]] = None

        # 异步保存线程管理
        self._save_thread: Optional[QThread] = None
        self._save_worker: Optional[ProjectSaveWorker] = None
        self._bg_save_thread: Optional[QThread] = None
        self._bg_save_worker: Optional[ProjectSaveWorker] = None

    # ── 属性 ──────────────────────────────────────

    @property
    def project(self) -> Optional[Project]:
        return self._project

    @property
    def save_path(self) -> Optional[str]:
        return self._save_path

    @property
    def audio_path(self) -> Optional[str]:
        return self._audio_path

    def set_audio_path(self, path: Optional[str]) -> None:
        """设置音频路径并广播变更。路径未变则不广播，避免回环。"""
        if self._audio_path == path:
            return
        self._audio_path = path
        # 音频是用户当前工作上下文 → 同步刷新默认目录到 config
        if path:
            self._persist_last_export_dir(str(Path(path).parent))
        self.data_changed.emit("audio")

    @property
    def original_media_path(self) -> Optional[str]:
        return self._original_media_path

    def set_original_media_path(self, path: Optional[str]) -> None:
        """设置原始媒体文件路径，值有变化时标记 dirty。

        用于用户手动加载音频/视频的场景。路径未变化（含自动恢复后的二次调用）则跳过。
        """
        if self._original_media_path == path:
            return
        self._original_media_path = path
        if self._project:
            self._dirty = True
            self._edit_epoch += 1
            self._schedule_auto_save()

    def restore_media_path(self, path: Optional[str]) -> None:
        """静默恢复媒体路径，不标记 dirty。

        专用于从 .sug 文件自动恢复媒体路径的场景。恢复后再次调用
        set_original_media_path() 传入相同路径时会被当作 no-op，不会触发 dirty。
        """
        self._original_media_path = path

    def mark_dirty(self) -> None:
        """手动标记项目为已修改，并广播通知以刷新标题栏等订阅者。

        用于不经过 notify() 的外部变更场景（如 nicokara_tags 修改）。
        """
        if not self._project:
            return
        self._dirty = True
        self._edit_epoch += 1
        self._schedule_auto_save()
        self.data_changed.emit("dirty")

    def get_saveable_media_path(self) -> Optional[str]:
        """返回可持久化的媒体路径，排除 .cache 临时路径。"""
        path = self._original_media_path
        if path and not self._is_in_cache_dir(path):
            return path
        return None

    # ── 工作目录（默认保存/导出位置） ─────────────

    @staticmethod
    def _is_in_cache_dir(path: Optional[str]) -> bool:
        """判断路径是否位于 .cache 临时目录下。"""
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(_cache_dir().resolve())
        except (ValueError, OSError):
            return False

    @staticmethod
    def _is_in_temp_dir(path: Optional[str]) -> bool:
        """判断路径是否位于备份目录下的 .temp 临时目录中。"""
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(_temp_dir().resolve())
        except (ValueError, OSError):
            return False

    def is_temp_save_path(self, path: Optional[str] = None) -> bool:
        """判断给定路径（或当前 _save_path）是否为临时位置（.cache 或备份 .temp）。

        临时项目的 save_path 不应作为默认保存目录返回给用户。
        """
        target = path if path is not None else self._save_path
        return self._is_in_cache_dir(target) or self._is_in_temp_dir(target)

    @property
    def working_dir(self) -> str:
        """派生：当前工作目录（"已保存项目 > 上次加载目录"的通用基准）。

        优先级：
          1. 已正式保存的项目目录（排除 .cache 临时项目）
          2. 音频文件所在目录
          3. 最近加载/导入的歌词文件所在目录
          4. settings["export.last_export_dir"]（系统自动记录的最后操作目录）
          5. ""（让 Qt 用系统默认）

        注：用户在设置中显式指定的「默认导出目录 / SUG默认保存目录」**不在**此链中，
        分别由 :pyattr:`export_dir` / :pyattr:`save_dir` 叠加在最前面。文件加载、
        主页等通用场景使用本属性。
        """
        if self._save_path and not self.is_temp_save_path(self._save_path):
            parent = str(Path(self._save_path).parent)
            if parent and Path(parent).is_dir():
                return parent
        if self._audio_path and not self._is_in_cache_dir(self._audio_path):
            parent = str(Path(self._audio_path).parent)
            if parent and Path(parent).is_dir():
                return parent
        if self._last_lyric_dir and Path(self._last_lyric_dir).is_dir():
            return self._last_lyric_dir
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            last = AppSettings().get("export.last_export_dir", "") or ""
        except Exception:
            last = ""
        if last and Path(last).is_dir():
            return last
        return ""

    @staticmethod
    def _user_default_dir(key: str) -> str:
        """读取并校验用户在设置中显式配置的某个默认目录；无效则返回 ""。"""
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            d = AppSettings().get(key, "") or ""
        except Exception:
            return ""
        if d and Path(d).is_dir():
            return d
        return ""

    @property
    def export_dir(self) -> str:
        """派生：导出预填目录（仅供导出界面使用，区别于 working_dir）。

        优先级：**默认导出目录 > 已保存项目 > 上次加载目录**。即用户在设置中
        显式指定的 ``export.default_export_dir`` 最优先；未设置时回退到
        ``working_dir``（已保存项目 / 音频 / 歌词 / last_export_dir）。
        保存场景不受此影响。
        """
        return self._user_default_dir("export.default_export_dir") or self.working_dir

    @property
    def save_dir(self) -> str:
        """派生：项目保存预填目录（供 untitled / 另存场景使用）。

        优先级：**SUG默认保存目录 > 已保存项目 > 上次加载目录**。即用户在设置中
        显式指定的 ``auto_save.default_save_dir`` 最优先；未设置时回退到
        ``working_dir``。
        """
        return self._user_default_dir("auto_save.default_save_dir") or self.working_dir

    def suggested_save_path(self, ext: str = ".sug") -> str:
        """根据 save_dir + 项目标题/音频名生成建议的保存全路径。

        目录取 :pyattr:`save_dir`（SUG默认保存目录 > 已保存项目 > 上次加载目录），
        用于 untitled / 另存场景。若无可用目录则只返回建议文件名。
        """
        if not ext.startswith("."):
            ext = "." + ext
        # 选 base name
        base = ""
        if self._project and getattr(self._project, "metadata", None):
            title = getattr(self._project.metadata, "title", "") or ""
            if title.strip():
                base = title.strip()
        if not base and self._audio_path:
            # 视频提取的缓存产物名带指纹后缀，派生保存名时去掉
            base = audio_display_stem(self._audio_path)
        if not base:
            base = "untitled"

        wd = self.save_dir
        if wd:
            return str(Path(wd) / f"{base}{ext}")
        return f"{base}{ext}"

    def set_working_dir(self, file_or_dir: str) -> None:
        """登记一个用户刚操作过的文件/目录，并持久化到 config。

        - 传入文件路径 → 取其 parent
        - 同时记录为最近歌词目录（用于歌词类型时的派生）
        - 写入 ``settings["export.last_export_dir"]`` 并立刻 save()
        """
        if not file_or_dir:
            return
        p = Path(file_or_dir)
        parent = str(p.parent) if p.suffix or p.is_file() else str(p)
        if not parent:
            return
        if not Path(parent).is_dir():
            return
        self._last_lyric_dir = parent
        self._persist_last_export_dir(parent)

    @staticmethod
    def _persist_last_export_dir(parent: str) -> None:
        """把目录写入 config.json 的 export.last_export_dir 并立即持久化。

        .cache 目录（含临时音频/临时项目）一律不写入，避免污染默认路径。
        """
        if not parent:
            return
        # 过滤临时目录（.cache 临时提取的音频、备份 .temp 下的临时项目都在这里）
        if ProjectStore._is_in_cache_dir(parent) or ProjectStore._is_in_temp_dir(parent):
            return
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = AppSettings()
            current = settings.get("export.last_export_dir", "")
            if current == parent:
                return
            settings.set("export.last_export_dir", parent)
            settings.save()
        except Exception:
            pass  # 持久化失败不影响主流程

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ── 项目生命周期 ─────────────────────────────

    def load_project(
        self,
        project: Project,
        save_path: Optional[str] = None,
        audio_path: Optional[str] = None,
    ) -> None:
        """加载（或替换）当前项目。

        所有 UI 模块应在收到 ``data_changed("project")`` 后全量刷新。
        """
        # 先停掉旧项目的在途保存线程：否则其完成回调可能在 cleanup 之后
        # 把旧项目数据写回临时文件，"复活"刚删掉的 .temp，污染闪退恢复。
        self._stop_save_threads()
        # 清理旧项目的临时文件
        if self._project:
            self.cleanup_temp_files()

        self._load_count += 1
        self._project = project
        self._save_path = save_path
        self._audio_path = audio_path
        self._original_media_path = None
        self._dirty = False
        self._start_periodic_save()
        self.data_changed.emit("project")

    def close_project(self) -> None:
        """关闭当前项目。"""
        # 与 load_project 同理：先停在途保存线程，再清理临时文件，
        # 避免线程完成回调把旧项目写回刚删掉的 .temp。
        self._stop_save_threads()
        self._auto_save_timer.stop()
        self._periodic_save_timer.stop()
        self.cleanup_temp_files()
        self._project = None
        self._save_path = None
        self._dirty = False

    def _stop_save_threads(self) -> None:
        """等待并清理所有正在运行的保存线程。"""
        for thread in (self._save_thread, self._bg_save_thread):
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait(3000)
        self._save_thread = None
        self._save_worker = None
        self._bg_save_thread = None
        self._bg_save_worker = None

    # ── 变更通知 ─────────────────────────────────

    def notify(self, change_type: str) -> None:
        """通知数据已变更 — 广播 + 调度 auto-save。

        各 UI 模块在修改 domain 对象后调用此方法，
        而非自行发射独立信号。
        """
        # 设置和音频路径变更不算项目内容修改
        if change_type not in ("settings", "audio"):
            self._dirty = True
            self._edit_epoch += 1
            self._schedule_auto_save()
        self.data_changed.emit(change_type)

    # ── 保存 ─────────────────────────────────────

    def _get_nicokara_tags_for_save(self) -> Optional[dict]:
        """读取当前 AppSettings 中的 nicokara_tags 用于持久化。"""
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            tags = AppSettings().get("nicokara_tags")
            if tags:
                return dict(tags)
        except Exception:
            pass
        return None

    def save(self, path: Optional[str] = None) -> bool:
        """异步保存项目到指定路径。

        完成后通过 save_finished / save_error 信号通知。
        返回值仅表示是否成功发起保存（非保存结果）。

        Args:
            path: 保存路径。如果为 None 使用上次路径。

        Returns:
            是否成功发起保存。False 表示未能发起（无项目 / 无路径 /
            同槽位已有保存在进行中被跳过——调用方不应把跳过当作成功）。
        """
        if not self._project:
            return False

        target = path or self._save_path
        if not target:
            return False

        # 捕获当前版本号：若保存期间 load_project 被调用（项目被替换），
        # 回调中版本号不匹配时不覆盖新项目的 _save_path。
        load_count_at_save = self._load_count
        # 捕获当前编辑代数：序列化与本回调之间的窗口期若有新编辑
        # （edit_epoch 递增），完成回调不清 dirty，避免误标"已保存"。
        edit_epoch_at_save = self._edit_epoch

        def _on_finished(saved_path: str) -> None:
            self._on_manual_save_finished(
                saved_path, load_count_at_save, edit_epoch_at_save
            )

        return self._launch_save(
            target,
            on_finished=_on_finished,
            on_error=self._on_manual_save_error,
            is_background=False,
        )

    def _launch_save(
        self,
        file_path: str,
        *,
        on_finished: Callable[[str], None],
        on_error: Callable[[str], None],
        is_background: bool,
    ) -> bool:
        """在后台线程执行一次保存。

        is_background=True 使用 _bg_save 槽位（auto/periodic），
        is_background=False 使用 _save 槽位（手动保存）。
        同槽位有保存正在进行时跳过本次并返回 ``False``，否则启动线程
        返回 ``True``。
        """
        if is_background:
            if self._bg_save_thread is not None and self._bg_save_thread.isRunning():
                return False
        else:
            if self._save_thread is not None and self._save_thread.isRunning():
                return False

        # 主线程构建纯 dict 快照（几 ms），取代旧方案 deepcopy（大项目几十 ms 冻结）。
        # dict 不引用 domain 对象，后台写盘期间 UI 继续编辑不影响快照内容。
        if not is_background:
            self.save_progress.emit("正在序列化项目数据...")
        data = SugProjectParser.serialize(
            self._project,
            nicokara_tags=self._get_nicokara_tags_for_save(),
            media_path=self.get_saveable_media_path(),
        )

        thread = QThread(self)
        worker = ProjectSaveWorker(data, file_path)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)

        # 手动保存：转发进度信号供 UI 显示进度提示
        if not is_background:
            worker.progress.connect(self.save_progress.emit)

        def cleanup():
            thread.quit()
            thread.wait()
            if is_background:
                self._bg_save_thread = None
                self._bg_save_worker = None
            else:
                self._save_thread = None
                self._save_worker = None

        worker.finished.connect(cleanup)
        worker.error.connect(cleanup)

        if is_background:
            self._bg_save_thread = thread
            self._bg_save_worker = worker
        else:
            self._save_thread = thread
            self._save_worker = worker

        # 手动保存：线程启动前同步广播"已开始"，UI 可在此时显示进度提示
        if not is_background:
            self.save_started.emit(file_path)

        thread.start()
        return True

    def _on_manual_save_finished(
        self,
        saved_path: str,
        load_count_at_save: int = -1,
        edit_epoch_at_save: int = -1,
    ) -> None:
        if load_count_at_save >= 0 and self._load_count != load_count_at_save:
            # 保存期间 load_project 被调用（项目已被替换），跳过 _save_path 更新，
            # 避免新项目误继承旧项目的保存路径。仍然广播保存成功信号供 UI 展示提示。
            self.save_finished.emit(saved_path)
            return
        old_path = self._save_path
        self._save_path = saved_path
        # 仅当保存窗口期（序列化快照之后）没有新编辑时才清 dirty：
        # 若编辑代数已变，说明有改动未落入本次保存的快照，必须保持
        # dirty 让关闭提示 / 周期保存兜底继续生效，否则这些改动会被
        # 误标"已保存"而静默丢失。
        if edit_epoch_at_save < 0 or self._edit_epoch == edit_epoch_at_save:
            self._dirty = False
        if old_path and old_path != saved_path:
            self._cleanup_temp_for_path(old_path)
        self.save_finished.emit(saved_path)

    def _on_manual_save_error(self, error_msg: str) -> None:
        self.save_error.emit(error_msg)

    def wait_for_manual_save(self, timeout_ms: int = 10000) -> None:
        """阻塞等待在途手动保存线程写盘完成（仅供应用退出路径调用）。

        closeEvent 选"保存"后立即 ``QApplication.quit()`` 会把仍在写盘的
        保存线程一并杀死，留下截断的 .sug；退出前必须等它自然结束。
        无在途保存时立即返回。超时（磁盘卡死等）则放弃等待继续退出，
        宁可保留 .temp 兜底也不无限卡住退出流程。
        """
        thread = self._save_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(timeout_ms)

    # ── 定时 auto-save 配置 ──────────────────────

    def set_periodic_save_config(self, enabled: bool, interval_minutes: int) -> None:
        """配置定时自动保存参数。

        Args:
            enabled: 是否启用定时自动保存。
            interval_minutes: 保存间隔（分钟），范围 1~60。
        """
        self._periodic_save_enabled = enabled
        interval_ms = max(1, min(60, interval_minutes)) * 60 * 1000
        self._auto_save_timer.setInterval(interval_ms)
        if self._project:
            self._start_periodic_save()

    def set_crash_recovery_enabled(self, enabled: bool) -> None:
        """启用或禁用闪退恢复防抖保存（.sug.temp）。

        Args:
            enabled: 是否启用闪退恢复。
        """
        self._crash_recovery_enabled = enabled
        if not enabled:
            self._periodic_save_timer.stop()

    def set_auto_save_defer_predicate(
        self, predicate: Optional[Callable[[], bool]]
    ) -> None:
        """Set a runtime predicate that temporarily defers automatic saves."""
        self._auto_save_defer_predicate = predicate

    def _should_defer_auto_save(self) -> bool:
        predicate = self._auto_save_defer_predicate
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:
            return False

    # ── auto-save（内部） ────────────────────────

    def _schedule_auto_save(self) -> None:
        """重置防抖定时器，触发 .sug.temp 保存。"""
        if self._project and self._crash_recovery_enabled:
            self._periodic_save_timer.start()

    def _do_auto_save(self) -> None:
        """异步执行定时保存到 ``<原路径>.autosave``。"""
        if not self._project or not self._save_path:
            return
        if self._should_defer_auto_save():
            log_perf_event("project.auto_save.deferred", reason="predicate")
            return

        autosave_path = self._save_path + ".autosave"
        _perf_start = perf_counter() if perf_enabled() else None

        def _on_done(path: str) -> None:
            if _perf_start is not None:
                log_elapsed("project.auto_save", _perf_start, 20)

        self._launch_save(
            autosave_path,
            on_finished=_on_done,
            on_error=lambda _: None,
            is_background=True,
        )

    # ── 定时 auto-save（内部） ───────────────────

    def _start_periodic_save(self) -> None:
        """启动或重启定时 .autosave。"""
        self._auto_save_timer.stop()
        if self._periodic_save_enabled and self._project:
            self._auto_save_timer.start()

    def _do_periodic_save(self) -> None:
        """异步执行防抖保存到 .sug.temp 文件，用于闪退恢复。

        所有临时文件统一存放在备份目录下的隐藏 .temp 子目录中：
        - 已保存项目 → ``<备份目录>/.temp/.项目名.sug.temp``
        - 未保存项目 → ``<备份目录>/.temp/.untitled.sug.temp``
        """
        if not self._project:
            return
        if self._should_defer_auto_save():
            log_perf_event("project.periodic_save.deferred", reason="predicate")
            self._periodic_save_timer.start()
            return

        _temp_dir().mkdir(parents=True, exist_ok=True)
        temp_path = str(self.get_temp_path())
        _perf_start = perf_counter() if perf_enabled() else None

        def _on_done(path: str) -> None:
            if _perf_start is not None:
                log_elapsed("project.periodic_save", _perf_start, 20)

        self._launch_save(
            temp_path,
            on_finished=_on_done,
            on_error=lambda _: None,
            is_background=True,
        )

    def save_sync_for_exit(self) -> None:
        """同步保存到 .sug.temp，仅用于强制退出兜底。"""
        if not self._project:
            return
        _temp_dir().mkdir(parents=True, exist_ok=True)
        temp_path = str(self.get_temp_path())
        try:
            SugProjectParser.save(
                self._project,
                temp_path,
                nicokara_tags=self._get_nicokara_tags_for_save(),
                media_path=self.get_saveable_media_path(),
            )
        except Exception:
            pass

    def get_temp_path(self) -> Path:
        """返回当前项目的临时保存路径（存放在备份目录下的 .temp 子目录）。"""
        if self._save_path:
            p = Path(self._save_path)
            # 使用项目文件名作为临时文件名，存放在 .temp 目录
            temp_filename = "." + p.name + ".temp"
            return _temp_dir() / temp_filename
        return _untitled_temp_path()

    def _cleanup_temp_for_path(self, save_path: str) -> None:
        """删除指定保存路径关联的临时文件（.temp/.xxx.sug.temp 与 autosave）。

        兼容旧版位置：同名 .sug.temp 也可能残留在程序目录 .cache 下。
        """
        sp = Path(save_path)
        temp_name = "." + sp.name + ".temp"
        for temp_path in (_temp_dir() / temp_name, _cache_dir() / temp_name):
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

        for name in (
            str(sp.parent / ("." + sp.name + ".autosave")),
            save_path + ".autosave",
            save_path + ".autosave.sug",
        ):
            try:
                fp = Path(name)
                if fp.exists():
                    fp.unlink()
            except Exception:
                pass

    def cleanup_temp_files(self) -> None:
        """删除当前项目关联的临时文件（含 .temp 与 .autosave，兼容旧命名）。"""
        temp = self.get_temp_path()
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass

        # 删除 autosave 文件（仅已保存项目才有）；兼容旧命名
        if self._save_path:
            p = Path(self._save_path)
            for name in (
                str(p.parent / ("." + p.name + ".autosave")),
                self._save_path + ".autosave",
                self._save_path + ".autosave.sug",
            ):
                try:
                    fp = Path(name)
                    if fp.exists():
                        fp.unlink()
                except Exception:
                    pass

    @staticmethod
    def _legacy_temp_dirs() -> list[Path]:
        """历史遗留的 .temp 目录（更改配置位置后未迁移成功的旧位置）。

        「设置 → 关于/语言 → 更改位置」把迁移失败的旧 .temp 目录记录在
        ``auto_save.legacy_temp_dirs``（见 about._change_config_dir），
        恢复扫描纳入这些目录，避免旧位置的 .sug.temp 成为孤儿。
        """
        dirs: list[Path] = []
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            raw = AppSettings().get("auto_save.legacy_temp_dirs", []) or []
        except Exception:
            return dirs
        if not isinstance(raw, list):
            return dirs
        for item in raw:
            try:
                d = Path(str(item))
                if d.is_dir() and d not in dirs:
                    dirs.append(d)
            except (OSError, ValueError):
                continue
        return dirs

    @staticmethod
    def _crash_recovery_dirs() -> list[Path]:
        """闪退恢复文件可能所在的目录。

        扫描（去重）：
        1. 当前 .temp（受用户自定义备份目录影响）
        2. 默认 .temp（用户从默认位置改为自定义路径后，旧恢复文件仍可被检测/清理）
        3. 旧版 .cache（升级兼容）
        4. 历史遗留位置（更改配置位置后未迁移成功的旧 .temp）
        """
        dirs: list[Path] = []
        for d in (
            _temp_dir(),
            app_dirs.default_backup_dir() / ".temp",
            _cache_dir(),
            *ProjectStore._legacy_temp_dirs(),
        ):
            if d not in dirs:
                dirs.append(d)
        return dirs

    @staticmethod
    def has_crash_recovery() -> bool:
        """检查是否有闪退恢复文件（.temp 目录及旧版 .cache 下的 .sug.temp 文件）。"""
        for d in ProjectStore._crash_recovery_dirs():
            try:
                if any(d.glob(".*.sug.temp")):
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def load_crash_recovery() -> Optional[tuple[Project, str, dict]]:
        """加载闪退恢复文件（优先加载未命名项目的恢复文件）。

        扫描当前 .temp 目录及旧版 .cache（升级兼容）。

        Returns:
            (project, temp_file_path, extras) — temp_file_path 是 .sug.temp 实际
            文件路径，extras 含 nicokara_tags / media_path（单次解析一并产出）。
            失败时返回 None。
        """
        # 优先检查未命名项目的恢复文件
        untitled_name = _untitled_temp_path().name
        for d in ProjectStore._crash_recovery_dirs():
            untitled_temp = d / untitled_name
            if untitled_temp.exists():
                try:
                    project, extras = SugProjectParser.load_with_extras(str(untitled_temp))
                    return project, str(untitled_temp), extras
                except Exception:
                    pass

        # 检查其他项目的恢复文件：按修改时间降序（最新的优先），
        # 多项目恢复文件并存时恢复最近编辑的那个，而不是 glob 顺序第一个。
        candidates: list[Path] = []
        for d in ProjectStore._crash_recovery_dirs():
            try:
                candidates.extend(d.glob(".*.sug.temp"))
            except OSError:
                continue
        candidates.sort(
            key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True
        )
        for temp_file in candidates:
            try:
                project, extras = SugProjectParser.load_with_extras(str(temp_file))
                return project, str(temp_file), extras
            except Exception:
                continue
        return None

    @staticmethod
    def delete_crash_recovery() -> None:
        """删除闪退恢复文件（.temp 目录及旧版 .cache 下的所有 .sug.temp 文件）。"""
        for d in ProjectStore._crash_recovery_dirs():
            for temp_file in d.glob(".*.sug.temp"):
                try:
                    temp_file.unlink()
                except Exception:
                    pass

    # ── 项目备份（ProjectBackup） ─────────────────

    @staticmethod
    def _backup_count() -> int:
        """读取「自动备份项目个数」设置，clamp 到 0~99999。0 表示不备份。"""
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            raw = AppSettings().get("auto_save.backup_count", 10)
            n = int(raw)
        except Exception:
            n = 10
        return max(0, min(99999, n))

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """把任意文本规整成安全的文件名片段（去非法字符、折叠空白、限长）。"""
        # Windows 非法字符 <>:"/\|?* 与控制字符一律替换为下划线
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        cleaned = cleaned.strip().strip(".")  # 结尾的点/空白在 Windows 上不合法
        cleaned = re.sub(r"\s+", " ", cleaned)
        if len(cleaned) > 80:
            cleaned = cleaned[:80].rstrip()
        return cleaned

    def _backup_base_name(self) -> str:
        """备份文件名前缀：项目标题 > 音频文件名 > untitled。"""
        base = ""
        if self._project and getattr(self._project, "metadata", None):
            title = getattr(self._project.metadata, "title", "") or ""
            base = self._sanitize_filename(title)
        if not base and self._audio_path:
            base = self._sanitize_filename(audio_display_stem(self._audio_path))
        return base or "untitled"

    def create_backup(self) -> Optional[str]:
        """在 ProjectBackup 目录写入一份命名备份，并按全局上限轮换删除最旧。

        命名规则：``项目名-YYYYMMDD-HHMMSS.sug.bak``（无项目名时退用音频文件名，
        再退用 untitled）。备份个数为 0 时直接跳过。同步写盘（.sug 为纯 JSON，
        不内嵌媒体，开销很小），失败时静默返回 None，不影响主保存流程。

        Returns:
            实际写入的备份文件路径；未备份（无项目 / 个数为 0 / 写入失败）时为 None。
        """
        if not self._project:
            return None
        count = self._backup_count()
        if count <= 0:
            return None

        root = _backup_root_dir()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        base = self._backup_base_name()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = root / f"{base}-{stamp}.sug.bak"
        # 同一秒内多次备份的去重保护
        dedup = 1
        while target.exists():
            target = root / f"{base}-{stamp}_{dedup}.sug.bak"
            dedup += 1

        try:
            SugProjectParser.save(
                self._project,
                str(target),
                nicokara_tags=self._get_nicokara_tags_for_save(),
                media_path=self.get_saveable_media_path(),
            )
        except Exception:
            return None

        # 仅当用户使用默认备份位置（未自定义 backup_dir）时，才兼容清理旧格式
        # .sug 备份。若用户自定义了备份目录（可能与项目存放目录相同），
        # 则只清理 .sug.bak 新格式文件，避免误删用户的普通 .sug 项目。
        using_default_dir = False
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            custom = AppSettings().get("auto_save.backup_dir", "") or ""
            using_default_dir = not bool(custom.strip())
        except Exception:
            using_default_dir = True

        self._prune_backups(root, count, include_legacy=using_default_dir)
        return str(target)

    @staticmethod
    def _prune_backups(root: Path, count: int, *, include_legacy: bool = False) -> None:
        """保留 root 下最新的 count 个备份，按修改时间删除多余的最旧文件。

        仅扫描 root 顶层（不递归），因此隐藏的 .temp 子目录不受影响。
        仅删除可明确识别为备份的文件：.sug.bak 始终识别；旧格式 .sug
        （带时间戳后缀）仅在 ``include_legacy=True`` 时识别，避免自定义
        备份目录与项目目录相同时误删用户的普通 .sug 文件。
        """
        legacy_backup_re = re.compile(r"-\d{8}-\d{6}(?:_\d+)?\.sug$")

        def _is_backup(path: Path) -> bool:
            """判断是否为可清理的备份文件。"""
            name = path.name
            if name.endswith(".sug.bak"):
                return True
            if include_legacy and name.endswith(".sug") and legacy_backup_re.search(name):
                return True
            return False

        try:
            backups = [p for p in root.iterdir() if p.is_file() and _is_backup(p)]
        except OSError:
            return
        if len(backups) <= count:
            return
        backups.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        for stale in backups[: len(backups) - count]:
            try:
                stale.unlink()
            except Exception:
                pass
