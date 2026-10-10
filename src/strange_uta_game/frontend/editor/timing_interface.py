"""编辑器界面（打轴主界面）。

本模块仅包含 ``EditorInterface`` 主类。控件与对话框已拆分到 ``timing/`` 子包：

- ``timing.commands``        : ``_SentenceSnapshotCommand``
- ``timing.transport_bar``   : ``TransportBar``
- ``timing.toolbar``         : ``EditorToolBar``
- ``timing.karaoke_preview`` : ``KaraokePreview``
- ``timing.timeline_widget`` : ``TimelineWidget``
- ``timing.dialogs``         : ``ModifyCharacterDialog`` / ``InsertGuideSymbolDialog`` / ``CharEditDialog``

为保留历史 import 路径（``from ...editor.timing_interface import _SentenceSnapshotCommand`` 等），
本模块对子包内符号进行 re-export。
"""

from __future__ import annotations

import re
import time
from time import perf_counter
from copy import deepcopy
from pathlib import Path
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QEvent, QMimeData, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    StateToolTip,
    setCustomStyleSheet,
)

from strange_uta_game.backend.application import (
    CheckpointPosition,
    TimingService,
)
from strange_uta_game.backend.application.auto_check_service import (
    delete_checkpoints_by_type_names,
    delete_rubies_by_type_names,
)
from strange_uta_game.backend.application.export_service import (
    ExportService,
    sanitize_export_basename,
)
from strange_uta_game.backend.domain import Character, Project, Sentence
from strange_uta_game.backend.infrastructure.audio import AudioLoadError
from strange_uta_game.backend.infrastructure.exporters import get_exporter_by_name
from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    CharType,
    get_char_type,
)
from strange_uta_game.frontend.perf_log import (
    log_elapsed,
    log_perf_event,
    log_slow_method,
    perf_enabled,
    start_ui_watchdog,
)
from strange_uta_game.frontend.background_throttle import (
    background_throttle,
    ui_visible,
)
from strange_uta_game.frontend.theme import theme, ThemeColors
from strange_uta_game.frontend.fluent_widgets import (
    message_choice,
    message_info,
    message_question,
)

from .line_interface import LineDetailDialog
from .timing import (
    CharEditDialog,
    CharacterSnapshotCommand,
    CompleteTimestampDialog,
    EditorToolBar,
    FileLoader,
    InsertGuideSymbolDialog,
    KaraokePreview,
    MiniSingerManager,
    ModifyCharacterDialog,
    PlaybackRangeCommand,
    RubyEditPopup,
    SentenceSnapshotCommand,
    TimelineWidget,
    TransportBar,
    _SentenceSnapshotCommand,
)

__all__ = [
    "EditorInterface",
    # re-exports for backward compatibility
    "_SentenceSnapshotCommand",
    "SentenceSnapshotCommand",
    "TransportBar",
    "EditorToolBar",
    "KaraokePreview",
    "MiniSingerManager",
    "TimelineWidget",
    "ModifyCharacterDialog",
    "InsertGuideSymbolDialog",
    "CharEditDialog",
    "CompleteTimestampDialog",
]

# 「切换波形/声谱/双谱」快捷键的循环顺序：波形图 → 声谱图 → 双谱 → 波形图
_DISPLAY_MODE_CYCLE = {
    "waveform": "spectrum",
    "spectrum": "dual",
    "dual": "waveform",
}

# SUG 内部富文本剪贴板格式。系统剪贴板的 text/plain 保留全文本编辑器
# 内联格式，项目内粘贴则优先根据该自定义 MIME 精确识别，避免单字等
# 未命中启发式时把 ``[mm:ss.cc]`` 当成歌词露出来。
_SUG_INLINE_CLIPBOARD_MIME = "application/x-strange-uta-game-inline-lyrics"


# ──────────────────────────────────────────────
# 编辑器主界面
# ──────────────────────────────────────────────

class EditorInterface(QWidget):
    """编辑器界面主容器"""

    project_saved = pyqtSignal()
    _position_changed_signal = pyqtSignal(int, int, object)
    _checkpoint_moved_signal = pyqtSignal(object)
    _timetag_added_signal = pyqtSignal(int, int, int)  # (line_idx, char_idx, checkpoint_idx)
    _timing_error_signal = pyqtSignal(str, str)
    # 渲染进度：(speed, progress)。内部从音频 worker 线程触发，经此信号
    # 自动 marshal 到 UI 线程（Qt 跨线程默认 queued connection）。
    _render_progress_signal = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._timing_service: Optional[TimingService] = None
        self._audio_file_path: Optional[str] = None
        # 一次性标记：下一次 set_project（项目替换）保留音频引擎/波形状态。
        # 由 FileLoader 在「加载歌词替换无歌词项目」前置位（音频继承），
        # set_project 内消费后立即复位。
        self._preserve_audio_on_project_load = False
        self._current_line_idx = 0
        self._pressed_keys: set[str] = set()  # 当前按下的打轴按键集合（支持多键独立）
        self._last_position_update_time = 0.0  # 60fps UI 节流
        self._fast_forward_ms = 5000
        self._rewind_ms = 5000
        self._f2_ruby_editor_mode = "compact"
        self._playback_range_start_ms: Optional[int] = None
        self._playback_range_end_ms: Optional[int] = None
        self._key_map = {}  # key_string -> action_name, populated by _apply_settings
        self._settings_loaded = False  # 配置是否已加载成功
        self._last_pause_char: str = ""  # 跟踪停顿符变更，用于实时迁移 ruby parts
        # 滚动模式：auto / always / never（由按钮循环切换，持久化到 config）
        # 在创建 UI 前初始化，确保统一主题刷新入口构造期间即可安全调用。
        self._scroll_mode: str = "auto"
        # 长按/短按支持
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(300)
        self._long_press_timer.timeout.connect(self._on_long_press_timeout)
        # A11：长按 pending 按键名保存（key_upper -> (short 动作, long 动作)）。
        # 旧版单槽实现里后按的键会覆盖先按键的 pending，先按键的短按动作被吞。
        self._pending_presses: dict[str, tuple[Optional[str], Optional[str]]] = {}
        # 长按定时器当前跟踪的按键（_pending_presses 中最早的未决键）
        self._long_press_key: Optional[str] = None
        # 当 cp 标记被点击时，沿 _on_checkpoint_clicked → move_to_checkpoint →
        # on_checkpoint_moved (signal) → _handle_checkpoint_moved →
        # _apply_checkpoint_position 链路同步执行；此标志使后者跳过
        # set_current_position，从而不污染"选中字符"光标 (_current_char_idx)。
        # 区分：selected_cp（cp 标记选中态）vs selected_char（光标/选中字符态）。
        self._suppress_cp_cursor_move = False
        self._file_loader = FileLoader(self)
        self._mini_singer_manager: Optional[MiniSingerManager] = None
        self._init_ui()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self._bind_callback_signals()
        start_ui_watchdog(self)

        # 位置主动拉取定时器（UI 线程 60fps，替代旧的回调线程+信号推送）
        self._position_poll_timer = QTimer(self)
        self._position_poll_timer.setInterval(16)  # ~60fps
        self._position_poll_timer.timeout.connect(self._poll_audio_position)
        # 前台轮询间隔跟随 timing.ui_refresh_fps 设置；窗口最小化/隐藏后降到
        # 200ms——仅维持播放结束/锁定终点检测与状态栏。窗口可见但失焦不降频
        # （用户可能一边看预览一边在别的窗口干活）。音频播放不受影响（引擎
        # 独立线程），打轴取时按键时直读引擎也不经过此定时器。
        self._position_poll_fg_interval_ms = 16
        self._position_poll_bg_interval_ms = 200
        _throttle = background_throttle()
        if _throttle is not None:
            _throttle.visibility_maybe_changed.connect(
                self._refresh_position_poll_interval
            )
            # 应用最小化/切后台时同样暂停在途声谱计算（回前台按需恢复）
            _throttle.visibility_maybe_changed.connect(
                self._on_ui_visibility_for_spectrum
            )
        self._last_polled_duration_ms: Optional[int] = None
        # Per-line status snapshots. A timing key only changes one line, so the
        # footer can update in O(chars in line) instead of scanning the project.
        self._status_line_cache: dict[int, tuple[bool, bool, int]] = {}
        self._status_cache_project_id: Optional[int] = None
        self._status_meaningful_total = 0
        self._status_timed_total = 0
        self._status_needs_guide_total = 0

        # 高频打轴时合并 timeline 标签刷新，避免每次按键都全量遍历/排序。
        self._time_tags_update_timer = QTimer(self)
        self._time_tags_update_timer.setSingleShot(True)
        self._time_tags_update_timer.setInterval(33)
        self._time_tags_update_timer.timeout.connect(self._update_time_tags_display)

        # 自动滚动状态机：用户交互挂起 → 播放到达新行 + 3s 无交互后恢复
        self._auto_scroll_suspended: bool = False
        self._auto_scroll_new_line_reached: bool = False
        self._auto_scroll_cooldown_timer = QTimer(self)
        self._auto_scroll_cooldown_timer.setSingleShot(True)
        self._auto_scroll_cooldown_timer.setInterval(6000)
        self._auto_scroll_cooldown_timer.timeout.connect(
            self._on_auto_scroll_cooldown_timeout
        )
        # eventFilter 中鼠标拖拽检测
        self._auto_scroll_mouse_press_pos = None

        # 按键音播放器（低延迟，基于 BASS Sample API）
        self._keysound_player = None
        self._keysound_enabled: bool = False
        # None 表示"尚未加载过任何风格"，确保 _apply_settings 首次调用时强制加载
        self._keysound_style = None
        self._init_keysound()

        # 节拍器（齿轮弹窗「网格与节拍」）：播放期间按 BPM 网格的 BPM/偏移
        # 触发节拍音，每 4 拍一记重音；调度/播放器见 playback_metronome
        self._metronome_player = None
        self._metronome = None
        self._metronome_enabled: bool = False
        self._init_metronome()

    def changeEvent(self, event):
        """切语言时刷新可见 labels/buttons——本 widget 持有音频引擎等重状态，
        不能整体 rebuild。改成精确 retranslate：每个文本独立 setText 一遍。"""
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate_visible_labels()
        super().changeEvent(event)

    def _retranslate_visible_labels(self) -> None:
        """切语言后重新设置所有自有控件的可见文本。

        toolbar / transport / timeline 是独立子 widget，自己有 changeEvent；
        本方法只处理 EditorInterface 直接持有的 labels 与 buttons。
        """
        if hasattr(self, "btn_clear_tags"):
            self.btn_clear_tags.setText(self.tr("清除当前行时间戳"))
        if hasattr(self, "btn_scroll_mode"):
            self.btn_scroll_mode.setToolTip(self.tr(
                "切换歌词预览滚动模式：\n"
                "自动滚动 — 操作后挂起 6 秒自动恢复\n"
                "始终滚动 — 始终跟随播放位置\n"
                "不滚动 — 完全停用自动滚动"
            ))
        # _SCROLL_MODE_LABELS 是类属性，含中文 key——读时走 tr。直接刷新当前文本：
        if hasattr(self, "btn_scroll_mode") and hasattr(self, "_scroll_mode"):
            try:
                self._sync_scroll_mode()
            except Exception:
                pass
        # 模式/状态/行信息：相应 helper 重新设值
        try:
            self._update_mode_indicator()
        except Exception:
            pass
        try:
            self._update_line_info()
        except Exception:
            pass
        try:
            self._update_status()
        except Exception:
            pass
        # lbl_status（"就绪"/"播放中"/...）按当前状态码重新翻译
        if hasattr(self, "lbl_status"):
            try:
                self.lbl_status.setText(self._tr_status(self._status_state))
            except Exception:
                pass
        # 快捷键提示：用最近一次缓存的快捷键映射重新渲染
        if hasattr(self, "_shortcut_actions_timing"):
            try:
                self._update_shortcut_hint(
                    self._shortcut_actions_timing,
                    getattr(self, "_shortcut_actions_edit", None),
                )
            except Exception:
                pass
        # 打轴按钮文字：保留按键名后缀
        if hasattr(self, "btn_tag") and self._settings_loaded:
            try:
                actions = getattr(self, "_shortcut_actions_timing", {})
                tag_key_raw = actions.get("tag_now", "Space") if actions else "Space"
                tag_first = tag_key_raw.split(",")[0].split(":")[0].strip() if tag_key_raw else "Space"
                self.btn_tag.setText(self.tr("打轴 ({key})").format(key=tag_first))
            except Exception:
                pass

    def _tr_status(self, state: str) -> str:
        """按状态码翻译——显式列举源串让 extractor 抓到。"""
        if state == "playing":  return self.tr("播放中")
        if state == "paused":   return self.tr("已暂停")
        if state == "stopped":  return self.tr("已停止")
        if state == "finished": return self.tr("播放完毕")
        if state == "range_finished": return self.tr("已到达锁定终点")
        return self.tr("就绪")

    def _init_keysound(self) -> None:
        """创建播放器并预加载默认风格样本（失败时静默跳过，不影响主功能）。"""
        try:
            from ...backend.infrastructure.audio.keysound_player import create_keysound_player
            self._keysound_player = create_keysound_player()
            self._reload_keysound("default")  # 预热：先加载默认风格
        except Exception as e:
            print(f"[KeySound] 初始化失败: {e}")

    _KEYSOUND_STYLE_FILES = {
        "osu":    ("osu_press.wav",    "osu_release.wav"),
        "arcade": ("arcade_press.wav", "arcade_release.wav"),
        "sci":    ("sci_press.wav",    "sci_release.wav"),
    }

    def _reload_keysound(self, style: str) -> None:
        """按指定风格重新加载按键音样本。"""
        if self._keysound_player is None:
            return
        from pathlib import Path as _Path
        sounds_dir = _Path(__file__).resolve().parent.parent.parent / "resource" / "sounds"
        press_name, release_name = self._KEYSOUND_STYLE_FILES.get(
            style, ("press.wav", "release.wav")
        )
        try:
            self._keysound_player.load(sounds_dir / press_name, sounds_dir / release_name)
        except Exception as e:
            print(f"[KeySound] 样本加载失败: {e}")

    def _reload_keysound_after_audio(self) -> None:
        """音频(重新)加载完成后按当前风格重载按键音样本。

        加载/切换音频会触发 BASS 设备释放+重建：``release_resources()`` 调
        ``BASS_Free`` 并 ``invalidate()`` 归零按键音 handle，随后引擎 ``load()``
        又 ``BASS_Init`` 出一个新会话。旧 sample handle 在新会话里已失效，但
        ``_on_audio_loaded`` 不会再触发 ``_apply_settings``，故仅靠「风格变化 /
        is_loaded()」判定无法在导入新歌后自动重载——表现为必须手动切换一次
        音效才有按键音。这里在音频加载完成后主动按当前风格重载一次。
        """
        if self._keysound_player is None:
            return
        style = self._keysound_style
        if not style:
            setting_iface = self._get_setting_interface()
            if setting_iface is not None:
                try:
                    style = str(
                        setting_iface.get_settings().get("timing.keysound_style", "default")
                    )
                except Exception:
                    style = "default"
        style = style or "default"
        self._keysound_style = style
        self._reload_keysound(style)

    # ── 节拍器（播放期间按 BPM 网格 BPM/偏移触发节拍音） ──

    def _init_metronome(self) -> None:
        """创建节拍音播放器与调度器并预热样本（失败静默，不影响主功能）。"""
        try:
            from ...backend.infrastructure.audio.metronome_player import (
                create_metronome_player,
            )
            from .timing.playback_metronome import PlaybackMetronome

            self._metronome_player = create_metronome_player()
            self._metronome = PlaybackMetronome(
                self._metronome_player,
                position_ms=self._metronome_position_ms,
                is_playing=self._metronome_is_playing,
                speed=self._metronome_speed,
                output_latency_ms=self._metronome_output_latency_ms,
            )
            self._reload_metronome_after_audio()  # 预热：加载节拍音样本
        except Exception as e:
            print(f"[Metronome] 初始化失败: {e}")

    def _metronome_engine(self):
        """节拍器使用的音频引擎（直接读原始位置：延迟补偿后的可听时刻，
        且不触碰 get_display_position_ms 的 UI 单调状态——调度在后台线程）。"""
        service = self._timing_service
        return getattr(service, "_audio_engine", None) if service else None

    def _metronome_position_ms(self) -> float:
        engine = self._metronome_engine()
        return float(engine.get_position_ms()) if engine is not None else 0.0

    def _metronome_is_playing(self) -> bool:
        engine = self._metronome_engine()
        return engine is not None and engine.is_playing()

    def _metronome_speed(self) -> float:
        engine = self._metronome_engine()
        if engine is None:
            return 1.0
        try:
            return float(engine.get_speed())
        except Exception:
            return 1.0

    def _metronome_output_latency_ms(self) -> float:
        engine = self._metronome_engine()
        return float(engine.get_output_latency_ms()) if engine is not None else 0.0

    def _reload_metronome_after_audio(self) -> None:
        """音频(重新)加载后重载节拍音样本——加载/切换音频触发 BASS 设备
        释放+重建，旧 sample handle 失效（与按键音同一套生命周期处理）。"""
        if self._metronome_player is None:
            return
        from ...backend.infrastructure.audio.metronome_player import (
            metronome_sound_paths,
        )

        beat_path, accent_path = metronome_sound_paths()
        try:
            self._metronome_player.load(beat_path, accent_path)
        except Exception as e:
            print(f"[Metronome] 样本加载失败: {e}")

    def _ensure_metronome_samples(self) -> None:
        """节拍音样本自愈：失效（BASS 会话重建后 handle 归零）即重载。

        与按键音 ``_apply_settings`` 里的 samples_invalid 判定同款兜底——
        换项目/切歌/设备恢复等路径中，样本可能在 ``_on_audio_loaded`` 的
        重载之外失效（如项目未附带媒体、加载被防重入拒绝、BASS 设备恢复
        重建会话），没有这层兜底节拍器会永久静默。
        """
        if self._metronome_player is None:
            return
        if not self._metronome_player.is_loaded():
            self._reload_metronome_after_audio()

    def _configure_metronome_from_settings(self) -> None:
        """按 timing.* 设置配置节拍器：参数（BPM/偏移）、开关与音量。

        播放中开启 → 立即开始随播调度；关闭 → 立即停。设置页重应用与齿轮
        弹窗 applied 两条入口共用本方法（值未变化时 configure 内部为空操作）。
        """
        if self._metronome is None or self._metronome_player is None:
            return
        self._ensure_metronome_samples()
        enabled = False
        bpm = 120.0
        offset_ms = 0
        volume = 100
        beats_per_bar = 4
        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            s = setting_iface.get_settings()
            enabled = bool(s.get("timing.waveform_metronome_enabled", False))
            bpm = float(s.get("timing.waveform_grid_bpm", 120.0))
            offset_ms = int(s.get("timing.waveform_grid_offset_ms", 0))
            volume = int(s.get("timing.waveform_metronome_volume", 100))
            beats_per_bar = int(s.get("timing.waveform_beats_per_bar", 4))
        self._metronome_enabled = enabled
        self._metronome_player.set_volume(volume)
        self._metronome.configure(bpm, offset_ms, beats_per_bar)
        if enabled and self._metronome_is_playing():
            self._metronome.start()
        elif not enabled:
            self._metronome.stop()

    def _bind_callback_signals(self):
        self._position_changed_signal.connect(self._handle_position_changed)
        self._checkpoint_moved_signal.connect(self._handle_checkpoint_moved)
        self._timetag_added_signal.connect(self._handle_timetag_added)
        self._timing_error_signal.connect(self._handle_timing_error)
        self._render_progress_signal.connect(self._handle_render_progress)

    def _handle_render_progress(self, speed: float, progress: float) -> None:
        """UI 线程：把进度转交给 TransportBar 显示。"""
        self.transport.set_render_progress(speed, progress)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 5)
        layout.setSpacing(8)

        # 1) 工具栏
        self.toolbar = EditorToolBar(self)
        self.toolbar.save_clicked.connect(self._on_save)
        self.toolbar.save_as_clicked.connect(self._on_save_as)
        self.toolbar.new_project_clicked.connect(self._on_new_project)
        self.toolbar.load_project_clicked.connect(self._on_load_project)
        self.toolbar.recent_project_clicked.connect(self._file_loader.open_recent_project)
        self.toolbar.clear_recent_projects_clicked.connect(self._file_loader.clear_recent_projects)
        self.toolbar.set_recent_projects(self._file_loader.recent_projects())
        self.toolbar.load_audio_clicked.connect(self._on_load_audio)
        self.toolbar.load_lyrics_clicked.connect(self._on_load_lyrics)
        self.toolbar.modify_char_clicked.connect(self._on_modify_char)
        self.toolbar.insert_guide_clicked.connect(self._on_insert_guide)
        self.toolbar.bulk_change_clicked.connect(self._on_bulk_change)
        self.toolbar.modify_line_clicked.connect(self._on_modify_line)
        self.toolbar.analyze_rubies_clicked.connect(self._on_analyze_rubies)
        self.toolbar.analyze_rubies_by_line_clicked.connect(self._on_analyze_rubies_by_line)
        self.toolbar.analyze_rubies_selected_clicked.connect(self._on_analyze_rubies_selected)
        self.toolbar.analyze_rubies_no_cp_clicked.connect(self._on_analyze_rubies_no_cp)
        self.toolbar.analyze_rubies_by_line_no_cp_clicked.connect(self._on_analyze_rubies_by_line_no_cp)
        self.toolbar.analyze_rubies_selected_no_cp_clicked.connect(self._on_analyze_rubies_selected_no_cp)
        self.toolbar.romanize_all_clicked.connect(self._on_romanize_all_rubies)
        self.toolbar.open_fulltext_clicked.connect(self._on_open_fulltext)
        self.toolbar.delete_rubies_by_type_clicked.connect(self._on_delete_rubies_by_type)
        self.toolbar.delete_rubies_line_clicked.connect(self._on_delete_rubies_line)
        self.toolbar.delete_rubies_selected_clicked.connect(self._on_delete_rubies_selected)
        self.toolbar.delete_checkpoints_by_type_clicked.connect(self._on_delete_checkpoints_by_type)
        self.toolbar.set_singer_by_line_clicked.connect(self._on_set_singer_by_line)
        self.toolbar.apply_singer_clicked.connect(self._on_apply_singer)
        self.toolbar.singer_manager_clicked.connect(self._on_singer_manager_clicked)
        self.toolbar.complete_timestamp_clicked.connect(self._on_complete_timestamp)
        self.toolbar.separate_symbol_timestamp_clicked.connect(self._on_separate_symbol_timestamp)
        self.toolbar.adjust_raw_timestamp_clicked.connect(self._on_adjust_raw_timestamp)
        self.toolbar.adjust_raw_timestamp_line_clicked.connect(self._on_adjust_raw_timestamp_line)
        self.toolbar.adjust_raw_timestamp_selected_clicked.connect(self._on_adjust_raw_timestamp_selected)
        self.toolbar.delete_all_timestamps_clicked.connect(self._on_delete_all_timestamps)
        self.toolbar.delete_all_timestamps_keep_head_clicked.connect(self._on_delete_all_timestamps_keep_head)

        self.toolbar.delete_timestamps_selected_clicked.connect(self._on_delete_timestamps_selected)
        self.toolbar.auto_generate_interlude_guide_clicked.connect(self._on_auto_generate_interlude_guide)
        self.toolbar.auto_insert_guide_clicked.connect(self._on_auto_insert_guide)
        self.toolbar.analyze_pinyin_clicked.connect(self._on_analyze_pinyin)
        self.toolbar.analyze_korean_clicked.connect(self._on_analyze_korean)
        self.toolbar.concat_sug_clicked.connect(self._on_concat_sug)
        self.toolbar.ai_timing_clicked.connect(self._on_ai_timing_clicked)
        self.toolbar.offset_changed.connect(self._on_offset_changed)
        layout.addWidget(self.toolbar)

        # 2) 播放控制栏
        self.transport = TransportBar(self)
        self.transport.play_clicked.connect(self._on_play)
        self.transport.pause_clicked.connect(self._on_pause)
        self.transport.stop_clicked.connect(self._on_stop)
        self.transport.seek_requested.connect(self._on_seek)
        self.transport.speed_changed.connect(self._on_speed_changed)
        self.transport.volume_changed.connect(self._on_volume_changed)
        layout.addWidget(self.transport)

        # 3) 时间轴
        self.timeline = TimelineWidget(self)
        self.timeline.seek_requested.connect(self._on_seek)
        self.timeline.waveform_visibility_changed.connect(self._on_waveform_visibility_changed)
        self.timeline.display_settings_changed.connect(
            self._on_timeline_display_settings_changed
        )
        self.timeline.tag_clicked.connect(self._on_timeline_tag_clicked)
        self.timeline.tags_drag_committed.connect(self._on_timeline_tags_drag_committed)
        layout.addWidget(self.timeline)

        # 4) 歌词预览（占主要空间）
        self.preview = KaraokePreview(self)
        # 预览默认 minimumHeight（声谱模式让位后可恢复的原值）
        self._preview_default_min_h = self.preview.minimumHeight()
        self.preview.line_clicked.connect(self._on_line_clicked)
        self.preview.checkpoint_clicked.connect(self._on_checkpoint_clicked)
        self.preview.char_selected.connect(self._on_char_selected)
        self.preview.char_edit_requested.connect(self._on_char_edit_requested)
        self.preview.seek_to_char_requested.connect(self._on_seek_to_char)
        self.preview.seek_to_checkpoint_requested.connect(self._on_seek_to_checkpoint)
        self.preview.singer_change_requested.connect(self._on_singer_change_selection)
        self.preview.delete_chars_requested.connect(self._on_delete_chars_requested)
        self.preview.delete_chars_multi_requested.connect(self._on_delete_chars_multi_requested)
        self.preview.singer_change_multi_requested.connect(self._on_singer_change_multi_requested)
        self.preview.delete_timestamp_requested.connect(self._on_delete_timestamp_requested)
        self.preview.insert_space_before_requested.connect(
            self._on_insert_space_before_requested
        )
        self.preview.insert_space_after_requested.connect(
            self._on_insert_space_after_requested
        )
        self.preview.merge_line_up_requested.connect(self._on_merge_line_up_requested)
        self.preview.delete_line_requested.connect(self._on_delete_line_requested)
        self.preview.insert_blank_line_before_requested.connect(
            self._on_insert_blank_line_before_requested
        )
        self.preview.insert_blank_line_requested.connect(
            self._on_insert_blank_line_requested
        )
        self.preview.add_checkpoint_requested.connect(
            self._on_add_checkpoint_requested
        )
        self.preview.remove_checkpoint_requested.connect(
            self._on_remove_checkpoint_requested
        )
        self.preview.toggle_sentence_end_requested.connect(
            self._on_toggle_sentence_end_requested
        )
        self.preview.toggle_needs_guide_requested.connect(
            self._on_toggle_needs_guide_requested
        )
        self.preview.auto_scroll_line_changed.connect(
            self._on_auto_scroll_line_changed
        )
        self.preview.user_interaction_during_auto_scroll.connect(
            self._on_user_interaction_during_auto_scroll
        )
        self.preview.installEventFilter(self)
        layout.addWidget(self.preview, stretch=1)

        # 5) 底部打轴操作栏
        # 布局：[模式指示器] [打轴按钮] [清除按钮] <stretch> [快捷键提示]
        bottom = QHBoxLayout()
        bottom.setSpacing(10)

        # 左下角模式指示器（#8：区分音乐播放/暂停模式）
        self.lbl_mode = QLabel(self.tr("模式：编辑"))
        bottom.addWidget(self.lbl_mode)

        self.btn_tag = PrimaryPushButton(self.tr("打轴 (Space)"), self)
        self.btn_tag.setIcon(FIF.PIN)
        self.btn_tag.setMinimumHeight(36)
        self.btn_tag.setMinimumWidth(160)
        self.btn_tag.clicked.connect(self._on_tag_now)
        bottom.addWidget(self.btn_tag)

        self.btn_clear_tags = PushButton(self.tr("清除当前行时间戳"), self)
        self.btn_clear_tags.setIcon(FIF.DELETE)
        self.btn_clear_tags.clicked.connect(self._on_clear_current_line_tags)
        bottom.addWidget(self.btn_clear_tags)

        self.btn_scroll_mode = PushButton(self.tr("自动滚动"), self)
        self.btn_scroll_mode.setObjectName("btnScrollMode")
        self.btn_scroll_mode.setIcon(FIF.SYNC)
        self.btn_scroll_mode.setToolTip(self.tr(
            "切换歌词预览滚动模式：\n"
            "自动滚动 — 操作后挂起 6 秒自动恢复\n"
            "始终滚动 — 始终跟随播放位置\n"
            "不滚动 — 完全停用自动滚动"
        ))
        self.btn_scroll_mode.clicked.connect(self._on_cycle_scroll_mode)
        bottom.addWidget(self.btn_scroll_mode)

        bottom.addStretch()

        # 快捷键提示（动态跟随设置）
        self.lbl_shortcut_hint = QLabel("")
        bottom.addWidget(self.lbl_shortcut_hint)

        layout.addLayout(bottom)

        # 6) 状态栏
        # 布局：[播放状态] <stretch> [当前行/字符/时间戳] <stretch> [总体进度]
        status = QHBoxLayout()
        status.setContentsMargins(5, 2, 5, 2)
        # 状态码：'ready' / 'playing' / 'paused' / 'stopped' / 'finished'。
        # 显示文本通过 _render_status_label() 渲染，便于语言切换时按当前
        # 状态重译。
        self._status_state: str = "ready"
        self.lbl_status = QLabel(self.tr("就绪"))
        status.addWidget(self.lbl_status)
        status.addStretch()
        # 行号/字符/时间戳信息（#5：从打轴栏移到此处，与播放状态一同显示）
        self.lbl_line_info = QLabel(self.tr("当前行: -"))
        status.addWidget(self.lbl_line_info)
        status.addStretch()
        self.lbl_progress = QLabel(self.tr("行: 0/0 | 进度: 0%"))
        status.addWidget(self.lbl_progress)
        # 待添加导唱符计数：项目中所有 needs_guide=True 的字符数
        self.lbl_needs_guide = QLabel("")
        status.addWidget(self.lbl_needs_guide)
        layout.addLayout(status)

        # 原生 QLabel 的内联颜色不会被 QApplication palette 或 Fluent 主题
        # 覆盖，因此统一在主题变化时重写全部底部样式。
        theme.changed.connect(self._apply_theme_styles)
        self._apply_theme_styles()

    def _apply_theme_styles(self) -> None:
        """刷新打轴页底部所有依赖主题的内联样式。"""
        self.lbl_shortcut_hint.setStyleSheet(
            f"font-size: 11px; color: {theme.text_hint.name()};"
        )
        common = f"font-size: 12px; color: {theme.text_primary.name()};"
        self.lbl_status.setStyleSheet(common)
        self.lbl_line_info.setStyleSheet(common)
        self.lbl_progress.setStyleSheet(common)
        self.lbl_needs_guide.setStyleSheet(
            f"font-size: 12px; color: {theme.accent_warning.name()};"
        )
        self._update_mode_indicator()
        self._update_scroll_mode_btn_style()

    def set_timing_service(self, timing_service: TimingService):
        """接入 TimingService 并完成全部回调/信号接线。

        本方法被设计为可重复调用（幂等）：切换音频引擎后 MainWindow 会再次调用它，
        以把所有回调重新挂到新引擎与服务上。两处 _global_qt 信号在重连前先断开旧连接，
        避免重复连接导致回调多次触发。
        """
        self._timing_service = timing_service
        self._timing_service.set_callbacks(self)
        # 注册渲染进度回调：经 pyqtSignal 自动 marshal 到 UI 线程。
        self._timing_service.set_render_progress_callback(
            lambda spd, prog: self._render_progress_signal.emit(float(spd), float(prog))
        )
        # 注册timing_servive焦点时间戳改变回调（先断开旧连接保证幂等）
        try:
            self._timing_service._global_qt._focus_moved_signal.disconnect(self._handle_foucus_moved)
        except (TypeError, RuntimeError):
            pass
        self._timing_service._global_qt._focus_moved_signal.connect(self._handle_foucus_moved)
        # 注册当前行居中滚动信号（先断开旧连接保证幂等）
        try:
            self._timing_service._global_qt._center_current_line_signal.disconnect(self._handle_center_current_line)
        except (TypeError, RuntimeError):
            pass
        self._timing_service._global_qt._center_current_line_signal.connect(self._handle_center_current_line)
        # 注册结构性变更信号（节奏点增减后需要刷新歌词显示）
        try:
            self._timing_service._global_qt._structural_change_signal.disconnect(self.refresh_lyric_display)
        except (TypeError, RuntimeError):
            pass
        self._timing_service._global_qt._structural_change_signal.connect(self.refresh_lyric_display)
        # 传音频引擎引用给 preview，使 paintEvent 可主动拉取高精度时间
        self.preview.set_audio_engine(timing_service._audio_engine)

    def set_store(self, store):
        """接入 ProjectStore 统一数据中心。"""
        self._store = store
        self._save_tooltip = None
        store.data_changed.connect(self._on_data_changed)
        # 手动保存（工具栏保存/另存为、Ctrl+S 等）成功后记入最近项目；
        # 编辑器与主窗口共享同一 store，这里一次连接覆盖全部保存入口。
        store.save_finished.connect(self._file_loader._on_store_saved)

    def _get_setting_interface(self):
        """Return SUG's settings interface even when embedded in a host window."""
        widget = self
        while widget is not None:
            setting_iface = getattr(widget, "settingInterface", None)
            if setting_iface is not None:
                return setting_iface
            widget = widget.parentWidget()
        main_window = self.window()
        return getattr(main_window, "settingInterface", None)

    def _on_data_changed(self, change_type: str):
        """响应 ProjectStore 的数据变更。"""
        try:
            if change_type == "project":
                # 项目结构变更，波形选中句柄可能全部失效
                if hasattr(self, "timeline"):
                    self.timeline.clear_tag_selection()
                auto_dialog = getattr(self, "_auto_guide_dialog", None)
                if auto_dialog is not None:
                    auto_dialog.close()
                self.set_project(self._store.project)
                if self._mini_singer_manager is not None:
                    self._mini_singer_manager.set_project(self._store.project)
            elif change_type in ("rubies", "lyrics", "checkpoints"):
                # 注音/歌词/节奏点变更可能使波形选中句柄越界悬空，清空避免误写
                if hasattr(self, "timeline"):
                    self.timeline.clear_tag_selection()
                self.refresh_lyric_display()
                # 导入歌词/项目时可能自带时间戳，波形 timetag 也需刷新（此前只刷了 preview）
                self._update_time_tags_display()
                self._update_status()
                auto_dialog = getattr(self, "_auto_guide_dialog", None)
                if auto_dialog is not None:
                    auto_dialog.mark_stale()
            elif change_type == "timetags":
                self._schedule_time_tags_update()
                self._update_status()
                auto_dialog = getattr(self, "_auto_guide_dialog", None)
                if auto_dialog is not None:
                    auto_dialog.mark_stale()
            elif change_type == "settings":
                self._apply_settings()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[TimingInterface] _on_data_changed(%s) 失败: %s",
                change_type, e, exc_info=True)
            self._store.error_notify.emit("数据刷新异常", str(e))

    @staticmethod
    def _mode_shortcut_defaults(mode_key: str) -> dict:
        """按模式取默认键位，来源为快捷键设置页 _SHORTCUT_ACTIONS 表。

        表中 default_timing / default_edit 两列是双模式默认键位的唯一真相源；
        打轴专属动作（delete_timestamp / tag_now 等）在编辑模式默认为空。
        """
        from strange_uta_game.frontend.settings.sub_interfaces.shortcut import (
            ShortcutSubInterface,
        )

        col = 4 if mode_key == "timing_mode" else 5
        return {
            row[0]: row[col]
            for row in ShortcutSubInterface._SHORTCUT_ACTIONS
            if len(row) > col
        }

    @staticmethod
    def _normalize_trigger(raw: str) -> str:
        """将旧格式快捷键值（无 :short/:long 后缀）标准化为新格式。"""
        if not raw:
            return raw
        parts = []
        needs_update = False
        for k in raw.split(","):
            k = k.strip()
            if k:
                if ":" not in k:
                    parts.append(f"{k}:short")
                    needs_update = True
                else:
                    parts.append(k)
        return ",".join(parts) if needs_update else raw

    @classmethod
    def _collect_shortcut_map(
        cls,
        settings,
        mode_key: str,
        action_names: list,
        fallback_defaults: dict,
    ) -> tuple[dict, dict, dict, list]:
        """构建单个模式的快捷键映射。

        返回 ``(key_map_short, key_map_long, action->key_str, 迁移写入列表)``；
        迁移写入列表为 ``[(设置路径, 标准化值)]``，由调用方写回 settings。

        取值优先级：``shortcuts.{mode}.{action}`` > 旧扁平 ``shortcuts.{action}``
        （两个模式都回填，上游评审：老用户的编辑模式键位不能回落默认值）
        > 设置页 _SHORTCUT_ACTIONS 的模式默认值 > fallback_defaults。

        旧扁平值命中且模式键缺失时，把规范化后的值列入迁移写回
        ``shortcuts.{mode}.{action}``（两个模式各转化一份）——老 schema
        自动转化为双模式并持久化，此后新 schema 为权威来源；旧扁平键
        保留为惰性残留（读取优先级被模式键压制，且 provider 增量保存
        无法表达删除）。

        键冲突规则：**显式绑定（来自设置）不被回退默认值覆盖**。历史上内嵌
        config.json 的 edit_mode 段漏写 delete_timestamp 键时，该动作回退到
        单 schema 时代的打轴默认 "Backspace:short"，在遍历中后写覆盖了显式
        绑定 remove_checkpoint 的 BACKSPACE，导致编辑模式退格键变成「删除
        时间戳」而非「减节奏点」（1.6.1 用户反馈）。同优先级的冲突保留
        后写生效并打印警告，便于排查。
        """
        mode_defaults = cls._mode_shortcut_defaults(mode_key)
        key_map_short: dict[str, str] = {}
        key_map_long: dict[str, str] = {}
        action_to_keys: dict[str, str] = {}
        # key -> (action, 是否显式绑定)；用于冲突时的优先级判定
        bound_short: dict[str, tuple] = {}
        bound_long: dict[str, tuple] = {}
        migrated: list = []

        def _bind(bound: dict, key_map: dict, key: str, action: str, explicit: bool):
            existing = bound.get(key)
            if existing is not None and existing[0] != action:
                if existing[1] and not explicit:
                    return  # 显式绑定优先，回退默认不得覆盖
                if existing[1] == explicit:
                    print(
                        f"[Shortcuts] {mode_key} 键 {key} 同时绑定 "
                        f"{existing[0]} 与 {action}，后者生效"
                    )
            bound[key] = (action, explicit)
            key_map[key] = action

        for action in action_names:
            mode_raw = settings.get(f"shortcuts.{mode_key}.{action}")
            # 兼容旧 schema（无 mode_key 的扁平 shortcuts.xxx）：两个模式都
            # 回填（上游评审——老用户升级后编辑模式键位必须保持原样）。
            # 编辑模式下若同一键同时命中 tag_now 等打轴专属键，按模式各自
            # 的键表独立生效，互不影响。
            flat_raw = settings.get(f"shortcuts.{action}")
            # 命中旧扁平值且模式键缺失 → 本次读值来自老 schema，需把
            # 规范化后的值转化进双模式 schema（连同下方旧格式后缀修正
            # 一起经迁移列表持久化）
            converted_from_flat = mode_raw is None and flat_raw is not None
            if mode_raw is not None:
                raw, explicit = mode_raw, True
            elif flat_raw is not None:
                raw, explicit = flat_raw, True
            else:
                raw = mode_defaults.get(action, fallback_defaults[action])
                explicit = False
            # 旧格式自动更正：无后缀的键名补全为 :short
            normalized = cls._normalize_trigger(raw)
            if converted_from_flat or normalized != raw:
                migrated.append((f"shortcuts.{mode_key}.{action}", normalized))
                raw = normalized
            action_to_keys[action] = raw
            for k in (raw or "").split(","):
                k = k.strip()
                if k:
                    parts = k.split(":")
                    key_name = parts[0].strip()
                    trigger = parts[1].strip().lower() if len(parts) > 1 else "short"
                    if key_name:
                        if trigger == "long":
                            _bind(bound_long, key_map_long, key_name.upper(), action, explicit)
                        else:
                            _bind(bound_short, key_map_short, key_name.upper(), action, explicit)
        return key_map_short, key_map_long, action_to_keys, migrated

    def _apply_settings(self):
        """从 AppSettings 读取设定并应用到编辑器。"""
        try:
            self._apply_settings_inner()
        except Exception as e:
            # 此方法挂在 ProjectStore.data_changed("settings") 信号槽上，
            # 任何未捕获的 Python 异常都可能在 Qt C++ 派发层变为 0xC0000409
            # STATUS_STACK_BUFFER_OVERRUN 原生闪退（参见 commit fccb832）。
            # 兜底打日志，决不让 cascade 击穿到 Qt。
            print(f"[Settings] _apply_settings 失败: {e}")

    @staticmethod
    def _normalize_ui_refresh_fps(value) -> int:
        """Return a supported timing-page refresh rate."""
        try:
            fps = int(value)
        except (TypeError, ValueError):
            return 60
        return 30 if fps <= 30 else 60

    def _apply_settings_inner(self):
        if not self._store:
            return
        # In embedded mode, self.window() is the host window. Walk parents to
        # find SUG's own MainWindow so runtime settings apply immediately.
        setting_iface = self._get_setting_interface()
        if setting_iface is None:
            return
        settings = setting_iface.get_settings()
        f2_ruby_editor_mode = settings.get("ui.f2_ruby_editor_mode", "compact")
        self._f2_ruby_editor_mode = (
            f2_ruby_editor_mode
            if f2_ruby_editor_mode in {"compact", "classic"}
            else "compact"
        )
        self._fast_forward_ms = settings.get("timing.fast_forward_ms", 5000)
        self._rewind_ms = settings.get("timing.rewind_ms", 5000)
        self._jump_before_ms = settings.get("timing.jump_before_ms", 3000)
        # #4：读取时间戳微调步长（默认 10ms）
        self._timing_adjust_step_ms = int(
            settings.get("timing.timing_adjust_step_ms", 10)
        )
        # UI 刷新率只影响播放头、波形和歌词动画，不参与时间戳采样。
        # 打轴仍在按键发生时直接读取音频引擎，因此低性能模式不会降低精度。
        refresh_fps = self._normalize_ui_refresh_fps(
            settings.get("timing.ui_refresh_fps", 60)
        )
        self._position_poll_fg_interval_ms = round(1000 / refresh_fps)
        # 窗口隐藏期间应用新设置时保持降频间隔，回可见状态由信号恢复
        self._refresh_position_poll_interval()
        # 波形时间标签拖拽编辑总开关（默认开启，关闭回退旧的纯显示/seek/pan 模式）
        if hasattr(self, "timeline"):
            # 波形区显示设置（齿轮对话框项）：模式 / 网格 / 频谱参数。
            # 时间标签四开关（拖拽/居中/字符/注音）也并入同一 dict——齿轮弹窗
            # 与设置页「波形时间标签」组经 _apply_display_settings 单路径联动
            self.timeline._apply_display_settings({
                "display_mode": settings.get("timing.waveform_display_mode", "waveform"),
                "grid_mode": settings.get("timing.waveform_grid_mode", "time"),
                "grid_bpm": float(settings.get("timing.waveform_grid_bpm", 120.0)),
                "grid_offset_ms": int(
                    settings.get("timing.waveform_grid_offset_ms", 0)
                ),
                "beats_per_bar": int(
                    settings.get("timing.waveform_beats_per_bar", 4)
                ),
                "auto_bpm_on_load": bool(
                    settings.get("timing.waveform_auto_bpm", False)
                ),
                "grid_line_width": int(
                    settings.get("timing.waveform_grid_line_width", 2)
                ),
                "spectrum_fft_size": int(settings.get("timing.spectrum_fft_size", 8192)),
                "spectrum_overlap": float(
                    settings.get("timing.spectrum_overlap", 0.75)
                ),
                "spectrum_freq_scale": settings.get("timing.spectrum_freq_scale", "log"),
                "spectrum_colormap": settings.get("timing.spectrum_colormap", "inferno"),
                "spectrum_dyn_range_db": int(settings.get("timing.spectrum_dyn_range_db", 60)),
                "spectrum_freq_min_hz": int(
                    settings.get("timing.spectrum_freq_min_hz", 0)
                ),
                "spectrum_freq_max_hz": int(
                    settings.get("timing.spectrum_freq_max_hz", 0)
                ),
                "waveform_display_height": int(
                    settings.get(
                        "timing.waveform_display_height",
                        settings.get("timing.display_height", 120),
                    )
                ),
                "spectrum_display_height": int(
                    settings.get(
                        "timing.spectrum_display_height",
                        settings.get("timing.display_height", 120),
                    )
                ),
                "waveform_rms_enabled": bool(
                    settings.get("timing.waveform_rms_enabled", True)
                ),
                "tag_edit_enabled": settings.get(
                    "timing.waveform_tag_edit_enabled", True
                ),
                "center_playhead_enabled": settings.get(
                    "timing.waveform_center_playhead_enabled", False
                ),
                "tag_char_enabled": settings.get(
                    "timing.waveform_tag_char_enabled", True
                ),
                "tag_ruby_enabled": settings.get(
                    "timing.waveform_tag_ruby_enabled", True
                ),
                "metronome_enabled": settings.get(
                    "timing.waveform_metronome_enabled", False
                ),
                "metronome_volume": int(
                    settings.get("timing.waveform_metronome_volume", 100)
                ),
            })
            # 节拍器参数/开关/音量（值未变时弹窗链不会发信号，须显式配置）
            self._configure_metronome_from_settings()
            # 波形显示开关的持久化状态（齿轮外的既有开关）
            self.timeline.set_waveform_visible(
                bool(settings.get("timing.waveform_visible", True))
            )
            # 显示开关影响声谱模式下的预览让位
            self._apply_preview_spectrum_yield()
        # #8/#11/#13：读取双模式快捷键映射（打轴模式=播放中、编辑模式=未播放）
        # 动作集合（所有动作在两种模式下都存在，读设置时各自取值，互不干扰）
        action_names = [
            "tag_now",
            "tag_now_extra",
            "play_pause",
            "stop",
            "restart_playback",
            "seek_back",
            "seek_forward",
            "lock_playback_start",
            "lock_playback_end",
            "toggle_waveform_spectrum",
            "speed_down",
            "speed_up",
            "speed_reset",
            "edit_ruby",
            "add_checkpoint",
            "remove_checkpoint",
            "toggle_line_end",
            "toggle_word_join",
            "volume_up",
            "volume_down",
            "volume_reset",
            "nav_prev_line",
            "nav_next_line",
            "nav_prev_char",
            "nav_next_char",
            "timestamp_up",
            "timestamp_down",
            "cycle_checkpoint",
            "cycle_checkpoint_prev",
            "delete_timestamp",
            "bulk_change",
            "modify_char",
            "insert_guide",
            "toggle_needs_guide",
            "modify_line",
            "analyze_rubies",
            "analyze_rubies_by_line",
            "analyze_rubies_selected",
            "open_fulltext",
            "delete_rubies_by_type",
            "set_singer_by_line",
            "apply_singer",
            "timestamps_to_sentence_end",
            "quick_export",
            "insert_space",
            "merge_line_up",
            "tag_now_editor",
            "tag_now_extra_editor",
            "clear_all_checkpoints",
            "tag_and_delete_next",
            # 工具栏功能对应的可设置快捷键（默认留空）
            "analyze_rubies_no_cp",
            "analyze_rubies_by_line_no_cp",
            "analyze_rubies_selected_no_cp",
            "romanize_all",
            "singer_manager",
            "complete_timestamp",
            "separate_symbol_timestamp",
            "adjust_raw_timestamp",
            "adjust_raw_timestamp_line",
            "adjust_raw_timestamp_selected",
            "delete_all_timestamps",
            "delete_all_timestamps_keep_head",
            "delete_timestamps_selected",
            "new_project",
            "load_project",
            "save_as",
            "load_audio",
            "load_lyrics",
            "concat_sug",
            "auto_insert_guide",
            "analyze_pinyin",
            "analyze_korean",
            "auto_generate_interlude_guide",
        ]
        # 末级兜底（设置页 _SHORTCUT_ACTIONS 表未收录的动作才会用到；
        # 该表按模式给出默认键位，见 _collect_shortcut_map）
        defaults = {
            "tag_now": "D:short,F:short",
            "tag_now_extra": "SPACE:short",
            "play_pause": "A:short",
            "stop": "S:short",
            "restart_playback": "SHIFT+S:short",
            "seek_back": "Z:short",
            "seek_forward": "X:short",
            "lock_playback_start": "[:short",
            "lock_playback_end": "]:short",
            "toggle_waveform_spectrum": "",
            "speed_down": "Q:short",
            "speed_up": "W:short",
            "speed_reset": "E:short",
            "edit_ruby": "F2:short",
            "add_checkpoint": "1:short",
            "remove_checkpoint": "3:short",
            "toggle_line_end": "2:short",
            "toggle_word_join": "F3:short",
            "volume_up": "",
            "volume_down": "",
            "volume_reset": "",
            "nav_prev_line": "UP:short",
            "nav_next_line": "DOWN:short",
            "nav_prev_char": "LEFT:short",
            "nav_next_char": "RIGHT:short",
            "timestamp_up": "ALT+UP:short",
            "timestamp_down": "ALT+DOWN:short",
            "cycle_checkpoint": "ALT+RIGHT:short",
            "cycle_checkpoint_prev": "ALT+LEFT:short",
            "delete_timestamp": "Backspace:short",
            "bulk_change": "CTRL+H:short",
            "modify_char": "J:short",
            "insert_guide": "CTRL+1:short",
            "toggle_needs_guide": ";:short",
            "modify_line": "CTRL+2:short",
            "analyze_rubies": "CTRL+3:short",
            "analyze_rubies_by_line": "CTRL+SHIFT+A:short",
            "analyze_rubies_selected": "CTRL+A:short",
            "open_fulltext": "CTRL+T:short",
            "delete_rubies_by_type": "CTRL+4:short",
            "set_singer_by_line": "CTRL+5:short",
            "apply_singer": "K:short",
            "timestamps_to_sentence_end": "P:short",
            "quick_export": "CTRL+E:short",
            "insert_space": "M:short",
            "merge_line_up": "Shift+Enter:short",
            "tag_now_editor": "",
            "tag_now_extra_editor": "",
            "clear_all_checkpoints": "CTRL+\\:short",
            "tag_and_delete_next": "",
            # 工具栏功能默认不绑定按键（留空），由用户在快捷键设置中按需配置
            "analyze_rubies_no_cp": "",
            "analyze_rubies_by_line_no_cp": "",
            "analyze_rubies_selected_no_cp": "",
            "romanize_all": "",
            "singer_manager": "",
            "complete_timestamp": "",
            "separate_symbol_timestamp": "",
            "adjust_raw_timestamp": "",
            "adjust_raw_timestamp_line": "",
            "adjust_raw_timestamp_selected": "",
            "delete_all_timestamps": "",
            "delete_all_timestamps_keep_head": "",
            "delete_timestamps_selected": "",
            "new_project": "CTRL+0:short",
            "load_project": "",
            "save_as": "",
            "load_audio": "",
            "load_lyrics": "",
            "concat_sug": "",
            "auto_insert_guide": "",
            "analyze_pinyin": "",
            "analyze_korean": "",
            "auto_generate_interlude_guide": "",
        }

        # 标记是否有旧格式需要持久化
        self._settings_migrated = False

        timing_short, timing_long, timing_actions, migrated_timing = (
            self._collect_shortcut_map(settings, "timing_mode", action_names, defaults)
        )
        edit_short, edit_long, edit_actions, migrated_edit = (
            self._collect_shortcut_map(settings, "edit_mode", action_names, defaults)
        )
        for path, value in migrated_timing + migrated_edit:
            settings.set(path, value)
            self._settings_migrated = True
        # 旧格式迁移后自动保存
        if self._settings_migrated:
            settings.save()
            self._settings_migrated = False
        self._key_map_timing_short = timing_short
        self._key_map_timing_long = timing_long
        self._key_map_edit_short = edit_short
        self._key_map_edit_long = edit_long
        # 当前活动 map（按播放状态切换；初始为编辑模式）
        self._key_map_short = edit_short
        self._key_map_long = edit_long
        # 兼容旧引用
        self._key_map = edit_short
        old_speed_pct = self.transport.get_speed_value()
        new_speed_pct = self.transport.set_speed_range(
            settings.get("audio.speed_slider_min", 0.5),
            settings.get("audio.speed_slider_max", 1.0),
            emit_signal=False,
        )
        if self._timing_service and new_speed_pct != old_speed_pct:
            self._timing_service.set_speed(new_speed_pct / 100.0)
        # 应用渲染偏移（与导出偏移联动）
        render_offset = settings.get("export.offset_ms", 0)
        prev_render_offset = self.preview._global_offset_ms
        self.preview.set_global_offset(render_offset)
        # 同步工具栏偏移控件
        self.toolbar.edit_offset.blockSignals(True)
        self.toolbar.edit_offset.setText(str(render_offset))
        self.toolbar.edit_offset.blockSignals(False)
        # 将偏移量写入所有字符的渲染/导出时间戳
        if self._project:
            self._project.global_offset_ms = render_offset
            for sentence in self._project.sentences:
                for ch in sentence.characters:
                    # 旧版 .sug 升级 / 第三方导入可能在 timestamps 中混入
                    # 非 int（None / 字符串），ch.set_offset 内部的算术会抛
                    # TypeError。单个脏字符不应阻断整次 settings cascade。
                    try:
                        ch.set_offset(render_offset)
                    except Exception as e:
                        print(f"[Settings] set_offset 跳过脏字符: {e}")
        # 设置页改「全局偏移」走本路径：值变化时波形时间标签同样要重刷
        # （标签时间戳带偏移，不重刷会停在旧位置）
        if render_offset != prev_render_offset:
            self._schedule_time_tags_update()
        # 停顿符变更时立即迁移所有 ruby parts（不等重开项目）
        new_pause_char = settings.get("export.nicokara_pause_char", "^") or "^"
        if self._last_pause_char and new_pause_char != self._last_pause_char and self._project:
            from strange_uta_game.backend.domain.models import pause_char_variants
            old_variants = pause_char_variants(self._last_pause_char)
            for sentence in self._project.sentences:
                for ch in sentence.characters:
                    if ch.ruby:
                        for p in ch.ruby.parts:
                            if p.text in old_variants:
                                p.text = new_pause_char
        self._last_pause_char = new_pause_char
        # 应用歌词对齐方式
        lyrics_alignment = settings.get("ui.lyrics_alignment", "center")
        self.preview.set_alignment(lyrics_alignment)
        # 应用左/右对齐页边距
        alignment_margin = settings.get("ui.alignment_margin", 168)
        self.preview.set_alignment_margin(alignment_margin)
        # 应用字体大小设置
        base_font_size = settings.get("ui.font_size", 18)
        current_line_size = settings.get("ui.current_line_font_size", 22)
        ruby_size = settings.get("ui.ruby_size", 10)
        cp_size = settings.get("ui.cp_size", 8)
        line_height_factor = settings.get("ui.line_height_factor", 1.20)
        ruby_spacing = settings.get("ui.ruby_spacing", 4)
        cp_spacing = settings.get("ui.cp_spacing", 4)
        main_font = settings.get("ui.main_font", "Microsoft YaHei")
        ruby_font = settings.get("ui.ruby_font", "Microsoft YaHei")
        self.preview.set_font_sizes(base_font_size, current_line_size, ruby_size, cp_size, line_height_factor, ruby_spacing, main_font=main_font, ruby_font=ruby_font, cp_spacing=cp_spacing)
        # 应用 checkpoint 标记字符
        checkpoint_markers = settings.get("ui.checkpoint_markers", {})
        if checkpoint_markers:
            self.preview.set_checkpoint_markers(checkpoint_markers)
        # 应用导唱待办标记
        self.preview.set_needs_guide_style(
            settings.get("ui.needs_guide_symbol", "✚"),
            settings.get("ui.needs_guide_size", 12),
        )
        # 更新快捷键提示（#6：只保留 9 项核心）
        self._update_shortcut_hint(timing_actions, edit_actions)
        # #7：打轴按钮文字联动 shortcuts.timing_mode.tag_now
        tag_key_raw = timing_actions.get("tag_now", "Space")
        tag_first = tag_key_raw.split(",")[0].split(":")[0].strip() if tag_key_raw else "Space"
        if hasattr(self, "btn_tag"):
            self.btn_tag.setText(self.tr("打轴 ({key})").format(key=tag_first))
        # #8：同步模式指示器（首次应用设置时刷新）
        self._update_mode_indicator()
        # 应用禁用单击跳转设置
        disable_click_jump = settings.get("timing.disable_click_jump", False)
        self.preview.set_disable_click_jump(disable_click_jump)
        # 应用禁用点击时居中设置
        disable_click_recenter = settings.get("timing.disable_click_recenter", False)
        self.preview.set_disable_click_recenter(disable_click_recenter)
        # 应用隐藏 hitbox 高亮设置
        hide_hitbox = settings.get("timing.hide_hitbox_highlights", False)
        self.preview.set_hide_hitbox_highlights(hide_hitbox)
        # 应用打轴指引总开关（正文走字预览 + 注音打轴状态）
        # 默认值与 DEFAULT_SETTINGS（preview_guide_enabled=True）一致
        preview_guide = settings.get("timing.preview_guide_enabled", True)
        self.preview.set_preview_guide_enabled(preview_guide)
        # 应用走字预览指引逐群设置
        self.preview.set_preview_guide_config(
            prev_alpha=settings.get("timing.preview_guide_prev_alpha", 100) / 100.0,
            curr_alpha=settings.get("timing.preview_guide_curr_alpha", 50) / 100.0,
            next_alpha=settings.get("timing.preview_guide_next_alpha", 20) / 100.0,
            prev_enabled=settings.get("timing.preview_guide_prev_enabled", True),
            curr_enabled=settings.get("timing.preview_guide_curr_enabled", True),
            next_enabled=settings.get("timing.preview_guide_next_enabled", True),
        )
        # 应用滚动模式（设置页修改后同步到按钮和 preview）
        scroll_mode = settings.get("timing.scroll_mode", "auto")
        if scroll_mode != self._scroll_mode:
            self._scroll_mode = scroll_mode
            self._sync_scroll_mode()
        # 按键音开关、音量、风格
        # 默认值与 DEFAULT_SETTINGS（keysound_enabled=False）一致：首次启动
        # （config 缺键）时设置页与打轴界面的按键音开关不再互相矛盾
        self._keysound_enabled = bool(settings.get("timing.keysound_enabled", False))
        if self._keysound_player is not None:
            self._keysound_player.set_enabled(self._keysound_enabled)
        keysound_volume = int(settings.get("timing.keysound_volume", 100))
        if self._keysound_player is not None:
            self._keysound_player.set_volume(keysound_volume)
        keysound_style = str(settings.get("timing.keysound_style", "default"))
        samples_invalid = (
            self._keysound_player is not None and not self._keysound_player.is_loaded()
        )
        if keysound_style != self._keysound_style or samples_invalid:
            self._keysound_style = keysound_style
            self._reload_keysound(keysound_style)
        self._settings_loaded = True

    def _update_shortcut_hint(
        self, timing_actions: dict, edit_actions: Optional[dict] = None
    ):
        """根据当前设置的快捷键映射，动态更新底部提示。

        #6：只显示 9 项核心动作（播放/停止/前进/后退/加速/减速/加节奏点/减节奏点/停顿点），
        按当前模式（播放中=打轴模式，否则=编辑模式）取快捷键文本。
        """
        action_labels = [
            ("play_pause", self.tr("播放")),
            ("stop", self.tr("停止")),
            ("seek_back", self.tr("后退")),
            ("seek_forward", self.tr("前进")),
            ("speed_down", self.tr("减速")),
            ("speed_up", self.tr("加速")),
            ("add_checkpoint", self.tr("加节奏点")),
            ("remove_checkpoint", self.tr("减节奏点")),
            ("toggle_line_end", self.tr("停顿点")),
        ]
        playing = bool(self._timing_service and self._timing_service.is_playing())
        active = timing_actions if playing else (edit_actions or timing_actions)
        parts = []
        for action, label in action_labels:
            key = active.get(action, "")
            if key:
                first_key = key.split(",")[0].split(":")[0].strip()
                if first_key:
                    # 用 [key] action 包裹键名，避免 "DPlay" 这类挤在一起难辨
                    parts.append(f"[{first_key}] {label}")
        parts.append(self.tr("Alt+→ 切换字内节奏点"))
        if hasattr(self, "lbl_shortcut_hint"):
            self.lbl_shortcut_hint.setText("  ".join(parts))
        # 缓存以便模式切换时再次调用（无需重读设置）
        self._shortcut_actions_timing = timing_actions
        self._shortcut_actions_edit = edit_actions or timing_actions

    # ==================== 项目 ====================

    def _on_offset_changed(self, offset_ms: int):
        """工具栏偏移控件变更 — 更新设置、字符偏移时间戳和渲染缓存"""
        # 写入设置（与设置页面联动）—— 必须用 settingInterface 的共享实例，
        # 否则 _store.notify("settings") 触发 _apply_settings() 时读到的还是旧值，
        # 会立刻把刚设的偏移回滚掉。
        try:
            setting_iface = self._get_setting_interface()
            if setting_iface:
                app_settings = setting_iface.get_settings()
            else:
                from strange_uta_game.frontend.settings.app_settings import AppSettings
                app_settings = AppSettings()
            app_settings.set("export.offset_ms", offset_ms)
            app_settings.save()
        except Exception:
            pass
        # 同步到Project对象
        if self._project:
            self._project.global_offset_ms = offset_ms
        # 更新所有字符的偏移时间戳（单个脏字符不能阻断整次更新）
        if self._project:
            for sentence in self._project.sentences:
                for ch in sentence.characters:
                    try:
                        ch.set_offset(offset_ms)
                    except Exception as e:
                        print(f"[Offset] set_offset 跳过脏字符: {e}")
        # 更新渲染
        try:
            self.preview.set_global_offset(offset_ms)
        except Exception as e:
            print(f"[Offset] preview.set_global_offset 失败: {e}")
        # 波形时间标签用的也是带偏移的全局时间戳——偏移一变必须重刷，
        # 否则 Tag 停在旧位置不跟随（33ms 防抖与打轴路径一致）
        self._schedule_time_tags_update()
        # 通知 ProjectStore，使 Settings 页面等监听者同步更新
        if hasattr(self, "_store") and self._store:
            self._store.notify("settings")

    def set_project(self, project: Project):
        previous_project = self._project
        self._project = project
        # 支持“先导入音频，再创建/导入歌词”的工作流。首次设置项目或调用方
        # 显式标记继承音频（加载歌词替换无歌词项目时音频跟随带入）时，音频
        # 引擎会被保留，因此也要把引擎中的真实时长带入新项目。
        preserve_audio = (
            previous_project is None
            or getattr(self, "_preserve_audio_on_project_load", False)
        )
        self._preserve_audio_on_project_load = False
        if preserve_audio and self._timing_service:
            self._sync_project_audio_duration(
                self._timing_service.get_duration_ms(),
                mark_dirty=False,
            )
        # 获取AppSettings实例（与_apply_settings使用同一个）
        app_settings = None
        try:
            setting_iface = self._get_setting_interface()
            if setting_iface:
                app_settings = setting_iface.get_settings()
        except Exception:
            pass
        # 从项目读取全局偏移，若为None则使用config中的值（兼容旧版.sug）
        offset = project.global_offset_ms
        if offset is None:
            offset = app_settings.get("export.offset_ms", 0) if app_settings else 0
            # 写入project，保存时旧sug自动升级
            project.global_offset_ms = offset
        else:
            # 项目有偏移量，同步到config.json
            if app_settings:
                app_settings.set("export.offset_ms", offset)
                app_settings.save()
            InfoBar.success(
                title=self.tr("已应用项目全局偏移"),
                content=self.tr("从项目读取到全局偏移: {offset}ms，已同步到设置").format(offset=offset),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        # 通知 ProjectStore，使 Settings 页面等监听者与项目偏移保持同步
        if hasattr(self, "_store") and self._store:
            self._store.notify("settings")

        # 先应用偏移到所有字符，再设置到preview（预渲染缓存会使用global_timestamps）
        for sentence in project.sentences:
            for ch in sentence.characters:
                ch.set_offset(offset)
        # 更新预览和工具栏
        self.preview.set_global_offset(offset)
        self.toolbar.edit_offset.blockSignals(True)
        self.toolbar.edit_offset.setText(str(offset))
        self.toolbar.edit_offset.blockSignals(False)
        # 设置到preview（会触发预渲染，此时global_timestamps已正确）
        self.preview.set_project(project)
        self._apply_checkpoint_position(
            self._timing_service.get_current_position()
            if self._timing_service
            else CheckpointPosition()
        )
        self._update_time_tags_display()
        self._update_status()
        # 重新应用设置（字体大小、行间距、对齐方式等）
        self._apply_settings()
        # 仅在替换已有项目时清除旧音频缓存，避免旧波形/时长/缓存残留。
        # 首次设置项目（self._project 原为 None）或调用方标记继承音频
        # （加载歌词替换无歌词项目）时保留已加载的音频，支持“先导入
        # 音频再导入歌词”的工作流。
        if previous_project is not None and not preserve_audio:
            self._clear_audio_state()

    def release_resources(self):
        """释放音频资源"""
        if self._timing_service:
            self._timing_service.release()
        # timing_service.release() 会调用 BASS_Free，使 keysound sample handle 失效。
        # 在此归零 handle（避免野指针），并重置风格标记，确保下次 _apply_settings 强制重新加载。
        if self._keysound_player is not None:
            self._keysound_player.invalidate()
        self._keysound_style = None
        # 节拍器同理：停调度 + 归零 sample handle（下次音频加载完成后重载）
        if self._metronome is not None:
            self._metronome.stop()
        if self._metronome_player is not None:
            self._metronome_player.invalidate()

    def _clear_audio_state(self):
        """清除音频状态并重置所有音频相关子控件。
        在加载新项目（含新建和打开）且新项目未附带音频时调用，
        避免旧项目的波形/时长/缓存残留造成用户误解。
        """
        self.release_resources()
        self._reset_playback_range()

        self.transport.set_duration(0)
        self.transport.set_position(0)
        self.transport.set_playing(False)

        self.timeline.set_duration(0)
        self.timeline.set_position(0)
        self.timeline.clear_audio_data()

        self.preview.set_duration(0)
        self._last_polled_duration_ms = 0

        self._audio_file_path = None
        self._audio_loading = False

    # ==================== 拖拽加载 ====================

    def dragEnterEvent(self, a0: Optional[QDragEnterEvent]):
        if a0 is None:
            return
        mime = a0.mimeData()
        if mime is not None and mime.hasUrls():
            for url in mime.urls():
                if self._file_loader.can_accept_drop(url.toLocalFile()):
                    a0.acceptProposedAction()
                    return
        a0.ignore()

    def dropEvent(self, a0: Optional[QDropEvent]):
        if a0 is None:
            return
        mime = a0.mimeData()
        if mime is None or not mime.hasUrls():
            a0.ignore()
            return
        for url in mime.urls():
            self._file_loader.handle_drop(url.toLocalFile())
        a0.acceptProposedAction()

    # ==================== 工具栏操作 ====================

    # 判断文本是否为全文本编辑器的内联格式。匹配以下任意一种特征：
    # - [>...] 停顿点 token（我们格式独有）
    # - {原文||...} 双竖线注音块（我们格式独有）
    # - [T] 占位符（我们格式独有）
    # - [ts]X[ 连续逐字时间戳（LRC 每行只有一个起始 token，不会出现此模式）
    _INLINE_TS_DETECT_RE = re.compile(
        r"\[>[^\]]*\]|\{[^{}]+\|\||\[T\]|\[\d+:\d{2}\.\d{2}\].\["
    )

    def _on_paste_lyrics(self):
        """从剪贴板粘贴（Ctrl+V）。

        - 内联格式（含 [>...] 停顿点 token 或 {原文||} 注音块）：调用 _paste_inline_format。
        - 空项目 / 无歌词行：整批加载歌词文本。
        - 已有歌词：在当前光标处插入（富信息 or 纯文本）。
        """
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if not clipboard:
            return

        text = clipboard.text()
        mime = clipboard.mimeData()

        # 本编辑器复制的字符优先读取自定义富文本载荷，不依赖 text/plain
        # 的启发式判断，因此单字时间戳也不会被当作普通歌词插入。
        if mime is not None and mime.hasFormat(_SUG_INLINE_CLIPBOARD_MIME):
            inline_text = bytes(mime.data(_SUG_INLINE_CLIPBOARD_MIME)).decode(
                "utf-8", errors="replace"
            )
            if inline_text:
                self._paste_inline_format(inline_text)
                return

        # 内联时间戳格式优先（来自全文本编辑器的复制内容）
        if text and self._INLINE_TS_DETECT_RE.search(text):
            self._paste_inline_format(text)
            return

        # 空项目 / 无歌词：整批加载
        if self._file_loader.can_load_from_clipboard():
            if not text or not text.strip():
                return
            self._file_loader.load_lyrics_from_text(text)
            return

        # 已有歌词：在光标处插入
        self._paste_chars_at_cursor(text)

    def _paste_inline_format(self, text: str) -> None:
        """将内联时间戳格式文本（来自全文本编辑器）粘贴进项目。

        按行调用 ``parse_timed_line`` 解析，保留时间戳/ruby/演唱者/连词信息。

        - 空项目：解析结果直接作为全部 Sentence 加载。
        - 已有歌词：在当前光标行处插入——首行字符插入当前行光标位置，
          后续行创建新 Sentence 插入到当前行之后；光标后原有字符追加到最后一行末尾。
        """
        if not self._project:
            return

        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            parse_timed_line,
        )

        # 构建 singer 映射（与全文本编辑器一致）
        name_to_id: dict = {}
        default_singer = ""
        for s in self._project.singers:
            name_to_id[s.name] = s.id
            if s.is_default:
                default_singer = s.id
        if not default_singer and self._project.singers:
            default_singer = self._project.singers[0].id

        offset = getattr(self._project, "global_offset_ms", 0) or 0

        # 按行解析
        raw_lines = [seg.strip("\r") for seg in text.split("\n")]
        if len(raw_lines) > 1 and raw_lines[-1] == "" and text.endswith("\n"):
            raw_lines.pop()

        inherited = default_singer
        parsed_lines: list[list] = []
        for ls in raw_lines:
            chars, inherited = parse_timed_line(
                ls,
                name_to_singer_id=name_to_id,
                default_singer_id=default_singer,
                inherited_singer_id=inherited,
                offset_ms=offset,
            )
            parsed_lines.append(chars)

        if not parsed_lines:
            return

        project = self._project

        # ── 空项目：直接整批加载 ──
        if not project.sentences:
            from strange_uta_game.backend.domain import Sentence

            def _load():
                sentences = []
                for chars in parsed_lines:
                    singer = (chars[0].singer_id if chars and chars[0].singer_id
                              else default_singer)
                    sentences.append(Sentence(singer_id=singer, characters=chars))
                project.sentences = sentences
                last = len(sentences) - 1
                return last, 0, 0, "lyrics"

            self._execute_structural_edit("粘贴内联格式", _load)
            return

        # ── 已有歌词：插入光标处 ──
        if self.preview._focus_line_idx >= 0 and self.preview._focus_char_idx >= 0:
            line_idx = self.preview._focus_line_idx
            insert_at = (min(self.preview._focus_char_idx,
                             self.preview._focus_char_range_end)
                         if self.preview._focus_char_range_end >= 0
                         else self.preview._focus_char_idx)
        else:
            line_idx = self._current_line_idx
            insert_at = self.preview._current_char_idx

        if line_idx < 0 or line_idx >= len(project.sentences):
            return

        from strange_uta_game.backend.domain import Sentence

        sentence = project.sentences[line_idx]
        original_len = len(sentence.characters)
        pos = max(0, min(insert_at + 1, original_len))

        def _mutate_inline():
            s = project.sentences[line_idx]
            after_chars = list(s.characters[pos:])
            s.characters = list(s.characters[:pos])

            # 首行字符插入当前行
            for ch in parsed_lines[0]:
                ch.is_line_end = False
                s.characters.append(ch)

            insert_after = line_idx
            if len(parsed_lines) == 1:
                # 单行粘贴：光标后原有字符追加回来
                for ch in after_chars:
                    ch.is_line_end = False
                    s.characters.append(ch)
            else:
                for i, seg_chars in enumerate(parsed_lines[1:]):
                    seg = list(seg_chars)
                    # 最后一段拼接光标后原有字符
                    if i == len(parsed_lines) - 2:
                        seg.extend(after_chars)
                    for ch in seg:
                        ch.is_line_end = False
                    if seg:
                        seg[-1].is_line_end = True
                    singer = (seg[0].singer_id if seg and seg[0].singer_id
                              else sentence.singer_id)
                    new_s = Sentence(singer_id=singer, characters=seg)
                    project.sentences.insert(insert_after + 1, new_s)
                    insert_after += 1

            if s.characters:
                s.characters[-1].is_line_end = True

            last_line = insert_after
            last_char = max(0, len(project.sentences[last_line].characters) - 1)
            return last_line, last_char, 0, "lyrics"

        self._execute_structural_edit("粘贴内联格式", _mutate_inline)

    def _on_copy_chars(self):
        """复制选中字符（Ctrl+C）。

        系统剪贴板的普通文本采用全文本编辑器内联格式；同一内容也写入
        SUG 自定义 MIME，确保 Ctrl+V 时可被精确识别并无损还原。
        跨行复制时各行用 \\n 分隔，保留行边界信息。
        """
        from PyQt6.QtWidgets import QApplication
        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            sentence_to_timed_line,
        )

        if not self._project:
            return

        id_to_name = {s.id: s.name for s in self._project.singers}
        offset = getattr(self._project, "global_offset_ms", 0) or 0

        sel = self.preview.get_normalized_selection()
        if sel is not None and self.preview.is_multi_line_selection():
            start_line, start_char, end_line, end_char = sel
            line_texts = []
            total_chars = 0
            inherited = ""
            for line_idx in range(start_line, end_line + 1):
                if line_idx < 0 or line_idx >= len(self._project.sentences):
                    continue
                sentence = self._project.sentences[line_idx]
                if not sentence.characters:
                    continue
                s = start_char if line_idx == start_line else 0
                e = end_char if line_idx == end_line else len(sentence.characters) - 1
                if s > e:
                    continue
                chars = [deepcopy(sentence.characters[i]) for i in range(s, e + 1)]
                line_text, inherited = sentence_to_timed_line(
                    chars,
                    singer_id_to_name=id_to_name,
                    line_singer_id=sentence.singer_id,
                    default_singer_id=sentence.singer_id,
                    inherited_singer_id=inherited,
                    offset_ms=offset,
                )
                line_texts.append(line_text)
                total_chars += len(chars)
            inline_text = "\n".join(line_texts)
        elif sel is not None:
            start_line, start_char, end_line, end_char = sel
            # 单行划选
            if start_line < 0 or start_line >= len(self._project.sentences):
                return
            sentence = self._project.sentences[start_line]
            if not sentence.characters:
                return
            start_char = max(0, min(start_char, len(sentence.characters) - 1))
            end_char = max(start_char, min(end_char, len(sentence.characters) - 1))
            chars = [deepcopy(sentence.characters[i]) for i in range(start_char, end_char + 1)]
            inline_text, _ = sentence_to_timed_line(
                chars,
                singer_id_to_name=id_to_name,
                line_singer_id=sentence.singer_id,
                default_singer_id=sentence.singer_id,
                offset_ms=offset,
            )
            total_chars = len(chars)
        else:
            line_idx = self._current_line_idx
            start = self.preview._current_char_idx
            if 0 <= line_idx < len(self._project.sentences):
                sentence = self._project.sentences[line_idx]
                if sentence.characters and 0 <= start < len(sentence.characters):
                    chars = [deepcopy(sentence.characters[start])]
                    inline_text, _ = sentence_to_timed_line(
                        chars,
                        singer_id_to_name=id_to_name,
                        line_singer_id=sentence.singer_id,
                        default_singer_id=sentence.singer_id,
                        offset_ms=offset,
                    )
                    total_chars = 1
                else:
                    return
            else:
                return

        clipboard = QApplication.clipboard()
        if clipboard:
            mime = QMimeData()
            mime.setText(inline_text)
            mime.setData(_SUG_INLINE_CLIPBOARD_MIME, inline_text.encode("utf-8"))
            clipboard.setMimeData(mime)

        InfoBar.success(
            title=self.tr("已复制"),
            content=self.tr("已复制 {n} 个字符").format(n=total_chars),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=1500,
            parent=self,
        )

    def _paste_chars_at_cursor(self, clipboard_text: str) -> None:
        """在当前光标处插入字符（Ctrl+V，已有歌词时）。

        富信息粘贴：剪贴板文本与上次 Ctrl+C 一致时插入字符深拷贝（保留注音/
        节奏点/时间戳/演唱者等）。纯文本：逐字构造为新歌词字符。
        纯文本含换行时按行拆分，首段插入当前行，后续段依次新建行；
        光标后的原有字符拼接至最后一段末尾。
        纯文本粘贴后自动对受影响字符范围执行局部注音分析（不影响已有注音）。
        插入经 _execute_structural_edit 包装，纳入 undo/redo。
        """
        if not self._project:
            return

        if (
            self.preview._focus_line_idx >= 0
            and self.preview._focus_char_idx >= 0
        ):
            line_idx = self.preview._focus_line_idx
            if self.preview._focus_char_range_end >= 0:
                insert_at = min(
                    self.preview._focus_char_idx, self.preview._focus_char_range_end
                )
            else:
                insert_at = self.preview._focus_char_idx
        else:
            line_idx = self._current_line_idx
            insert_at = self.preview._current_char_idx

        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]

        buffer = getattr(self, "_char_clipboard", None)
        buffer_text = getattr(self, "_char_clipboard_text", None)
        if buffer and clipboard_text == buffer_text:
            new_chars = []
            for c in buffer:
                ch = deepcopy(c)
                # 插入位非行尾时清理行尾标记与 UI 选中态，避免重复行尾/选中
                ch.is_line_end = False
                ch.selected_checkpoint_idx = None
                new_chars.append(ch)
        else:
            if not clipboard_text or not clipboard_text.strip():
                return
            # 按换行拆分，保留空行作为空行（维持用户排版）；仅丢弃末尾换行符产生的终止空段
            lines = [seg.strip("\r") for seg in clipboard_text.split("\n")]
            if len(lines) > 1 and lines[-1] == "" and clipboard_text.endswith("\n"):
                lines.pop()
            if not lines:
                return

            if len(lines) == 1:
                new_chars = [
                    Character(char=c, singer_id=sentence.singer_id)
                    for c in lines[0]
                ]
                if not new_chars:
                    return

                project = self._project
                original_len = len(sentence.characters)
                pos = max(0, min(insert_at, original_len))
                affected = set(range(pos, pos + len(lines[0])))

                def _mutate():
                    s = project.sentences[line_idx]
                    for off, ch in enumerate(new_chars):
                        s.insert_character(pos + off, ch)
                    return line_idx, pos + len(new_chars) - 1, 0, "lyrics"

                self._execute_structural_edit("粘贴字符", _mutate)
                self._analyze_rubies_specs_async(
                    [(line_idx, affected)], "粘贴字符注音分析",
                    show_winrt_dialog=False,
                )
                return

            # 多行：拆行粘贴
            singer_id = sentence.singer_id
            project = self._project
            original_len = len(sentence.characters)
            pos = max(0, min(insert_at, original_len))
            has_after = pos < original_len

            def _mutate_multi():
                s = project.sentences[line_idx]
                after_chars = list(s.characters[pos:])
                s.characters = s.characters[:pos]

                # 第一段拼入当前行
                for c in lines[0]:
                    s.characters.append(Character(char=c, singer_id=singer_id))
                for ch in s.characters:
                    ch.is_line_end = False
                if s.characters:
                    s.characters[-1].is_line_end = True

                # 后续段逐行插入
                insert_after = line_idx
                for i, seg_text in enumerate(lines[1:]):
                    seg_chars = [
                        Character(char=c, singer_id=singer_id) for c in seg_text
                    ]

                    # 最后一段拼接光标后原有字符
                    if i == len(lines) - 2:
                        seg_chars.extend(after_chars)

                    for ch in seg_chars:
                        ch.is_line_end = False
                    if seg_chars:
                        seg_chars[-1].is_line_end = True

                    new_sentence = Sentence(
                        singer_id=singer_id, characters=seg_chars
                    )
                    project.sentences.insert(insert_after + 1, new_sentence)
                    insert_after += 1

                last_line = insert_after
                last_sentence = project.sentences[last_line]
                last_char = max(0, len(last_sentence.characters) - 1)
                return last_line, last_char, 0, "lyrics"

            self._execute_structural_edit("粘贴字符", _mutate_multi)
            # 收集所有受影响行/范围，合并为一次异步分析，避免多个 InfoBar
            _paste_specs: list = []
            if lines[0]:
                _paste_specs.append((line_idx, set(range(pos, pos + len(lines[0])))))
            for li in range(line_idx + 1, line_idx + len(lines) - 1):
                _paste_specs.append((li, None))
            if len(lines) > 1 and lines[-1]:
                _affected_last = (
                    set(range(0, len(lines[-1]))) if has_after else None
                )
                _paste_specs.append((line_idx + len(lines) - 1, _affected_last))
            if _paste_specs:
                self._analyze_rubies_specs_async(
                    _paste_specs, "粘贴字符注音分析",
                    show_winrt_dialog=False,
                )

    def _on_save(self) -> bool:
        """保存当前项目。

        Returns:
            True  — 已发起保存（已有路径的异步保存，或用户选定了另存路径）；
            False — 无项目，或用户取消了「另存为」对话框。调用方（未保存检测等）
                    据此中止后续流程，避免"选了保存却取消另存为"仍继续操作。
        """
        if not self._project:
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

        store = getattr(self, "_store", None)

        # 已有正式保存路径（非 .cache 临时）→ 异步保存
        if (
            store is not None
            and store.save_path
            and not store.is_temp_save_path()
        ):
            self._connect_save_signals(store)
            store.save()
            return True

        # 无正式保存路径 / 仍是临时项目 → 弹出另存为对话框
        suggested = store.suggested_save_path(".sug") if store else ""
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("保存项目"), suggested,
            self.tr("StrangeUtaGame 项目 (*.sug);;所有文件 (*.*)")
        )
        if not path:
            return False
        if not path.endswith(".sug"):
            path += ".sug"

        if store:
            store.set_working_dir(path)
            self._connect_save_signals(store)
            store.save(path)
        else:
            self._fallback_sync_save(path)

        self._file_loader._notify_main_window_frameless_refresh()
        return True

    def _connect_save_signals(self, store) -> None:
        """一次性连接 store 保存生命周期信号（含进度提示）。"""
        for sig, slot in [
            (store.save_started,  self._on_store_save_started),
            (store.save_progress, self._on_store_save_progress),
            (store.save_finished, self._on_store_save_finished),
            (store.save_error,    self._on_store_save_error),
        ]:
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
            sig.connect(slot)

    def _disconnect_save_signals(self, store) -> None:
        """断开本次保存的所有信号连接。"""
        for sig, slot in [
            (store.save_started,  self._on_store_save_started),
            (store.save_progress, self._on_store_save_progress),
            (store.save_finished, self._on_store_save_finished),
            (store.save_error,    self._on_store_save_error),
        ]:
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    def _on_store_save_started(self, save_path: str) -> None:
        from pathlib import Path as _Path
        from qfluentwidgets import StateToolTip
        from strange_uta_game.frontend.theme import theme
        if self._save_tooltip:
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

    def _on_store_save_progress(self, stage: str) -> None:
        if self._save_tooltip:
            self._save_tooltip.setContent(stage)

    def _on_store_save_finished(self, saved_path: str) -> None:
        store = getattr(self, "_store", None)
        if store:
            self._disconnect_save_signals(store)
        if self._save_tooltip:
            self._save_tooltip.setState(True)
            self._save_tooltip.setContent(self.tr("保存完成"))
            self._save_tooltip.close()
            self._save_tooltip = None
        InfoBar.success(
            title=self.tr("保存成功"),
            content=saved_path,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )
        self.project_saved.emit()

    def _on_store_save_error(self, error_msg: str) -> None:
        store = getattr(self, "_store", None)
        if store:
            self._disconnect_save_signals(store)
        if self._save_tooltip:
            self._save_tooltip.close()
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

    def _fallback_sync_save(self, path: str) -> None:
        """无 store 时的同步保存回退。"""
        try:
            from strange_uta_game.backend.infrastructure.persistence.sug_io import (
                SugProjectParser,
            )
            SugProjectParser.save(self._project, path)
            self._file_loader._record_recent_project(path)
            InfoBar.success(
                title=self.tr("保存成功"),
                content=path,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            self.project_saved.emit()
        except Exception as e:
            InfoBar.error(
                title=self.tr("保存失败"),
                content=str(e),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

    def _on_new_project(self):
        """新建项目（检查当前项目是否需要保存）"""
        if self._project:
            store = getattr(self, "_store", None)
            # 检查是否有未保存的更改
            if store and store.dirty:
                choice = message_choice(
                    self,
                    self.tr("保存当前项目"),
                    self.tr("当前项目有未保存的更改，是否保存？"),
                    [self.tr("保存"), self.tr("放弃"), self.tr("取消")],
                    default=0,
                )
                if choice == 0:  # 保存
                    # 保存被取消（如「另存为」对话框点了取消）→ 中止新建
                    if not self._on_save():
                        return
                elif choice == 2 or choice == -1:  # 取消 / 关闭
                    return
                # choice == 1（放弃）：什么都不做，继续新建

        # 创建新项目
        from strange_uta_game.backend.application import ProjectService

        project_service = ProjectService()
        project = project_service.create_project()

        # 重置 AppSettings 中的 nicokara_tags 为默认值，避免上一个项目残留
        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = setting_iface.get_settings()
            settings.set("nicokara_tags", dict(AppSettings.DEFAULT_SETTINGS.get("nicokara_tags", {})))
            settings.save()

        if self._store:
            self._store.load_project(project)
        else:
            self.set_project(project)

    def _on_save_as(self):
        """项目另存为"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        store = getattr(self, "_store", None)
        suggested = store.suggested_save_path(".sug") if store else ""
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("另存为"), suggested,
            self.tr("StrangeUtaGame 项目 (*.sug);;所有文件 (*.*)")
        )
        if not path:
            return
        if not path.endswith(".sug"):
            path += ".sug"

        # 登记工作目录到 config
        if store:
            store.set_working_dir(path)

        if store:
            self._connect_save_signals(store)
            store.save(path)
        else:
            self._fallback_sync_save(path)

        self._file_loader._notify_main_window_frameless_refresh()

    def _on_load_project(self):
        """加载项目文件"""
        self._file_loader.prompt_load_project()

    def _on_load_audio(self):
        self._file_loader.prompt_load_audio()

    def _on_load_lyrics(self):
        """加载歌词文件到当前项目（替换现有歌词）。"""
        self._file_loader.prompt_load_lyrics()

    def _on_concat_sug(self):
        """拼接多个SUG项目为新的单一项目。"""
        from strange_uta_game.frontend.editor.timing.sug_concat_dialog import SugConcatDialog

        # 弹窗前先复用 FileLoader.check_unsaved_changes 做未保存检测
        # （与「新建项目」「加载项目」完全一致的逻辑）
        if not self._file_loader.check_unsaved_changes():
            return

        dlg = SugConcatDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.was_apply_clicked():
            return

        entries = dlg.get_entries()
        output_name = dlg.get_output_name()
        uniform_offset = dlg.get_uniform_offset()

        self._launch_concat_worker(entries, output_name, uniform_offset)

    def _launch_concat_worker(
        self,
        entries: list,
        output_name: str,
        uniform_offset: int,
    ):
        """在后台线程中执行SUG拼接，主线程显示进度。"""
        from strange_uta_game.frontend.theme import theme
        from strange_uta_game.frontend.editor.timing.sug_concat_worker import (
            SugConcatWorker,
        )

        # 创建并显示进度提示
        self._concat_tooltip = StateToolTip(
            self.tr("正在拼接SUG"), self.tr("准备中..."), self,
        )
        green = theme.status_complete.name()
        self._concat_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        self._concat_tooltip.move(self._concat_tooltip.getSuitablePos())
        self._concat_tooltip.show()

        # 创建 worker + 线程
        self._concat_thread = QThread(self)
        self._concat_worker = SugConcatWorker(entries, output_name, uniform_offset)
        self._concat_worker.moveToThread(self._concat_thread)

        self._concat_thread.started.connect(self._concat_worker.run)
        self._concat_worker.progress.connect(self._on_concat_progress)
        self._concat_worker.finished.connect(self._on_concat_finished)
        self._concat_worker.error.connect(self._on_concat_error)
        self._concat_worker.finished.connect(self._cleanup_concat_thread)
        self._concat_worker.error.connect(self._cleanup_concat_thread)

        self._concat_thread.start()

    def _on_concat_progress(self, stage: str, current: int, total: int):
        if hasattr(self, "_concat_tooltip") and self._concat_tooltip:
            self._concat_tooltip.setContent(
                f"{stage} ({current}/{total})"
            )

    def _on_concat_finished(self, project, entries_count: int):
        if hasattr(self, "_concat_tooltip") and self._concat_tooltip:
            self._concat_tooltip.setState(True)
            self._concat_tooltip.setContent(self.tr("拼接完成"))
            self._concat_tooltip.close()
            self._concat_tooltip = None

        # 重置 nicokara_tags
        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = setting_iface.get_settings()
            settings.set("nicokara_tags", dict(AppSettings.DEFAULT_SETTINGS.get("nicokara_tags", {})))
            settings.save()

        # 加载新项目：load_project 会清零 dirty，拼接产物本身尚未落盘，
        # 需立即标记为未保存，使用户退出/切换项目时会被提示保存。
        if self._store:
            self._store.load_project(project)
            self._store.mark_dirty()
        else:
            self.set_project(project)

        InfoBar.success(
            title=self.tr("拼接完成"),
            content=self.tr("已从 {n} 个SUG文件拼接生成新项目，共 {lines} 行歌词。").format(
                n=entries_count,
                lines=len(project.sentences),
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_concat_error(self, error_msg: str):
        if hasattr(self, "_concat_tooltip") and self._concat_tooltip:
            self._concat_tooltip.close()
            self._concat_tooltip = None

        InfoBar.error(
            title=self.tr("拼接失败"),
            content=error_msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _cleanup_concat_thread(self):
        if hasattr(self, "_concat_thread") and self._concat_thread:
            self._concat_thread.quit()
            self._concat_thread.wait()
            self._concat_thread = None
        if hasattr(self, "_concat_worker") and self._concat_worker:
            self._concat_worker.deleteLater()
            self._concat_worker = None

    # ──────────────────────────────────────────────
    # AI 打轴（阶段 F；宿主能力注入见阶段 G）
    # ──────────────────────────────────────────────

    def _on_ai_timing_clicked(self):
        """AI 打轴一级入口：§3.1 前置阻拦 + 打开完整弹窗。"""
        if self._project is None or not any(
            len(s.characters) for s in self._project.sentences
        ):
            InfoBar.warning(
                title=self.tr("无法使用 AI 打轴"),
                content=self.tr("请先加载或创建包含歌词正文的工程"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return
        audio_path = getattr(self, "_audio_file_path", None)
        if not audio_path or not Path(audio_path).is_file():
            InfoBar.warning(
                title=self.tr("无法使用 AI 打轴"),
                content=self.tr("请先加载音频，AI 打轴需要人声/音频素材"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return

        # 防重入：弹窗非模态，忙碌中重复点击只置前已有窗口，不再起第二个任务
        existing = getattr(self, "_ai_timing_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        from strange_uta_game.frontend.editor.ai_timing_dialog import AiTimingDialog

        parts = self._build_ai_timing_service()
        if parts is None:
            return
        (
            service,
            settings,
            registry,
            runtime,
            download_service,
            proxy,
            managed_runtime,
            open_separation,
            stop_service,
            embedded,
        ) = parts
        duration_ms = 0
        if self._timing_service:
            try:
                duration_ms = int(self._timing_service.get_duration_ms() or 0)
            except Exception:
                duration_ms = 0
        self._ai_timing_dialog = AiTimingDialog(
            project=self._project,
            audio_path=str(audio_path),
            audio_duration_ms=duration_ms,
            service=service,
            settings=settings,
            registry=registry,
            runtime=runtime,
            download_service=download_service,
            on_applied=self._apply_ai_timing_command,
            save_settings=self._save_ai_timing_settings,
            download_proxy=proxy,
            managed_runtime_python=managed_runtime,
            embedded_mode=embedded,
            open_separation_page=open_separation,
            stop_separation_service=stop_service,
            context_checker=lambda: (
                self._project is not None
                and getattr(self, "_audio_file_path", None) == audio_path
            ),
            parent=self,
        )
        self._ai_timing_dialog.show()

    def _build_ai_timing_service(self):
        """构建 AI 打轴服务栈；设置读取失败时提示并返回 None。

        embedded：使用宿主注入的 AiTimingHost 能力（会话人声、分离执行、
        分离身份、缓存目录）；standalone：全部使用 SUG 自身默认配置。
        """
        from strange_uta_game.backend.application.ai_timing import (
            AiCache,
            AiRuntimeManager,
            AiTimingService,
            ModelDownloadService,
            ModelRegistry,
            PronunciationResolver,
            VocalPreparationService,
            load_ai_timing_settings,
            resolve_model_root,
        )
        from strange_uta_game.backend.application.ai_timing.host import (
            is_ai_timing_host,
        )
        from strange_uta_game.backend.application.ai_timing.models import (
            HfHubTransport,
        )
        from strange_uta_game.backend.application.ai_timing.vocals import (
            default_ai_cache_root,
        )
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        try:
            app_settings = AppSettings()
            settings = load_ai_timing_settings(app_settings.get)
        except Exception:
            InfoBar.error(
                title=self.tr("AI 打轴初始化失败"),
                content=self.tr("无法读取应用设置，请重试或检查配置目录权限"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return None

        # 宿主能力查找：沿 parent 链找 MainWindow 上注入的 aiTimingHost
        host = self._resolve_ai_timing_host()

        # 模型根/AI 缓存：embedded 默认用宿主统一目录（与分离模型同源
        # 管理），但**不覆盖用户显式选择**（弹窗里改过路径就以用户为准，
        # 与运行环境注入同口径——此前无条件覆盖导致嵌入模式改路径
        # 永远不生效）；standalone 用 SUG 自己的设置/默认目录
        host_model_root_value = ""
        host_cache_value = ""
        if host is not None:
            host_model_root = getattr(host, "model_root", None)
            if callable(host_model_root):
                try:
                    host_model_root_value = str(host_model_root() or "")
                except Exception:
                    host_model_root_value = ""
            try:
                host_cache_value = str(host.ai_cache_dir() or "")
            except Exception:
                host_cache_value = ""
        from strange_uta_game.backend.application.ai_timing.settings import (
            resolve_effective_cache_root,
            resolve_effective_model_root,
        )

        model_root = resolve_effective_model_root(
            settings, host_model_root_value
        )
        if host is not None:
            # 内存态回填生效根（弹窗路径选择器的起始目录）；
            # 用户显式改过时 chosen≠host，判别不受影响
            settings.model_root = str(model_root)
        cache_root = resolve_effective_cache_root(settings, host_cache_value)
        cache = AiCache(cache_root)
        registry = ModelRegistry(model_root)
        runtime = AiRuntimeManager()
        if host is not None:
            # 方案 B 增量安装会升级/降级宿主清单登记在案的共用包——
            # 装完必须让宿主重扫清单，否则其下次启动校验报损坏
            note_changed = getattr(host, "note_runtime_changed", None)
            if callable(note_changed):
                runtime.set_runtime_changed_hook(note_changed)
        vocal_service = VocalPreparationService(
            cache,
            session_vocal_finder=(
                host.find_session_vocal if host is not None else None
            ),
        )
        # 代理：embedded 用宿主（工作台网络设置）；standalone 用 SUG 自己
        # 「设置 → 网络与代理」的代理模式（updater.proxy.resolve_proxy）。
        # 分离子进程下载 UVR 模型走 GitHub，代理必须一并传入
        proxy = ""
        if host is not None:
            proxy_getter = getattr(host, "http_proxy", None)
            if callable(proxy_getter):
                try:
                    proxy = str(proxy_getter() or "")
                except Exception:
                    proxy = ""
        else:
            try:
                from strange_uta_game.updater.proxy import resolve_proxy
                from strange_uta_game.updater.settings import UpdaterSettings

                net = UpdaterSettings.load()
                info, _ = resolve_proxy(net.proxy_mode, net.proxy_manual_url)
                if info and info.is_valid:
                    proxy = info.url
            except Exception:
                proxy = ""
        # 分离编排：standalone 用共享 Runtime 子进程分离；embedded
        # 宿主优先（会话人声/工作台设置），宿主未配置第 2 步分离环境时
        # 回落 AI Runtime 内置分离（安装器本身携带 audio-separator，
        # 只为 AI 打轴的用户不必强配工作台分离环境）
        follows_host = False
        # 解释器路径惰性读取：安装/修复完成后路径才写回设置，同一弹窗
        # 会话内的分离 prober/执行器必须能立即反映（否则分离环境行
        # 一直显示未安装，重复点「安装 / 修复」也不恢复）
        runtime_python_getter = lambda: settings.runtime_python  # noqa: E731
        if host is not None:
            from strange_uta_game.backend.application.ai_timing.separation import (
                StandaloneVocalSeparator,
                host_first_separation,
            )

            # embedded：宿主优先；宿主未配置第 2 步分离环境时回落
            # 内置分离。embedded=True 让失败提示引导到工作台设置
            # （SUG 自身的 ffmpeg/网络入口在嵌入模式下隐藏）
            standalone_sep = StandaloneVocalSeparator(
                runtime_python_getter, model_root, proxy=proxy, embedded=True
            )
            executor, identity_fn, prober, follows_host = (
                host_first_separation(host, standalone_sep)
            )
        else:
            from strange_uta_game.backend.application.ai_timing.separation import (
                StandaloneVocalSeparator,
            )

            separator = StandaloneVocalSeparator(
                runtime_python_getter, model_root, proxy=proxy
            )
            # 统一返回 (path, identity)：缓存登记跟随实际执行者
            # （与 embedded 宿主回落路径的口径一致）
            executor = separator.separate_with_identity
            identity_fn = separator.identity
            prober = separator.available
        service = AiTimingService(
            settings=settings,
            cache=cache,
            registry=registry,
            runtime=runtime,
            vocal_service=vocal_service,
            resolver=PronunciationResolver(),
            separation_executor=executor,
            separation_identity=identity_fn,
            separation_prober=prober,
            separation_follows_host=follows_host,
        )
        # 方案 B：embedded 复用宿主托管 Runtime——宿主给出可用的
        # python.exe 时，解释器跟随宿主（不另建 venv，安装改为增量）。
        # 例外：用户显式选择过的其他解释器（含原生安装兜底产物）不覆盖；
        # 旧版自建 venv 路径视为过期配置，切到宿主托管值。
        managed_runtime = ""
        if host is not None:
            runtime_getter = getattr(host, "runtime_python", None)
            if callable(runtime_getter):
                try:
                    candidate = str(runtime_getter() or "")
                except Exception:
                    candidate = ""
                if candidate and Path(candidate).is_file():
                    managed_runtime = candidate
                    current = settings.runtime_python or ""
                    legacy = ""
                    try:
                        legacy = str(
                            AiRuntimeManager._venv_python(
                                resolve_model_root(settings).parent / "ai_runtime"
                            )
                        )
                    except Exception:
                        pass
                    if not current or not Path(current).is_file() or current == legacy:
                        settings.runtime_python = managed_runtime
        # 宿主页面跳转（引导去第 2 步装环境）；未提供则为 None
        open_separation = None
        if host is not None:
            page_opener = getattr(host, "open_separation_page", None)
            if callable(page_opener):
                open_separation = page_opener
        # 宿主分离服务停止（方案 B 装前腾出解释器文件锁）；未提供则
        # 为 None（旧宿主，弹窗跳过停服直接增量安装）
        stop_service = None
        if host is not None:
            stopper = getattr(host, "stop_separation_service", None)
            if callable(stopper):
                stop_service = stopper
        download_service = ModelDownloadService(
            registry,
            HfHubTransport(
                endpoint=settings.download_mirror,
                hf_cache_root=model_root / ".hf",
                proxy=proxy,
            ),
        )
        return (
            service,
            settings,
            registry,
            runtime,
            download_service,
            proxy,
            managed_runtime,
            open_separation,
            stop_service,
            host is not None,
        )

    def _save_ai_timing_settings(self, settings) -> None:
        """弹窗「安装对齐环境」等动作后的设置持久化。"""
        from strange_uta_game.backend.application.ai_timing import (
            save_ai_timing_settings,
        )
        from strange_uta_game.frontend.settings.app_settings import AppSettings

        app_settings = AppSettings()
        save_ai_timing_settings(app_settings.set, settings)
        app_settings.save()

    def _resolve_ai_timing_host(self):
        """沿 parent 链查找宿主注入的 AiTimingHost（嵌入式）。"""
        from strange_uta_game.backend.application.ai_timing.host import (
            is_ai_timing_host,
        )

        widget = self
        while widget is not None:
            host = getattr(widget, "aiTimingHost", None)
            if is_ai_timing_host(host):
                return host
            widget = widget.parentWidget()
        return None

    def _apply_ai_timing_command(self, command):
        """弹窗成功回调（主线程）：执行命令入撤销栈并按非结构命令刷新。"""
        command_manager = None
        if self._timing_service:
            command_manager = self._timing_service.command_manager
        if command_manager is not None:
            command_manager.execute(command)
        else:
            command.execute()
        self.refresh_lyric_display()
        self._update_time_tags_display()
        if self._timing_service:
            self._apply_checkpoint_position(
                self._timing_service.get_current_position()
            )
        self._update_status()
        self._sync_focus_from_timing_service()
        if getattr(self, "_store", None):
            self._store.mark_dirty()

    def _on_undo(self):
        if self._timing_service and self._timing_service.can_undo():
            self._timing_service.undo()
            cmd = self._timing_service.command_manager.get_last_undone_command()
            if isinstance(cmd, SentenceSnapshotCommand) and cmd.undo_position:
                self._sync_after_structure_change(
                    change_type="lyrics",
                    focus_line_idx=cmd.undo_position[0],
                    focus_char_idx=cmd.undo_position[1],
                    move_cp=getattr(cmd, "move_cp", True),
                )
            else:
                # Non-structural commands (for example timestamp edits) mutate the
                # project in place.  Refresh the preview as well as the waveform;
                # otherwise its per-sentence render cache keeps showing pre-undo data
                # until an unrelated cursor/line change invalidates the display.
                self.refresh_lyric_display()
                self._update_time_tags_display()
                self._apply_checkpoint_position(self._timing_service.get_current_position())
                self._update_status()
            self._sync_focus_from_timing_service()

    def _on_redo(self):
        if self._timing_service and self._timing_service.can_redo():
            self._timing_service.redo()
            cmd = self._timing_service.command_manager.get_last_redone_command()
            if isinstance(cmd, SentenceSnapshotCommand) and cmd.redo_position:
                self._sync_after_structure_change(
                    change_type="lyrics",
                    focus_line_idx=cmd.redo_position[0],
                    focus_char_idx=cmd.redo_position[1],
                    move_cp=getattr(cmd, "move_cp", True),
                )
            else:
                # Keep redo symmetric with undo so cached timestamp/checkpoint
                # rendering is visible immediately.
                self.refresh_lyric_display()
                self._update_time_tags_display()
                self._apply_checkpoint_position(self._timing_service.get_current_position())
                self._update_status()
            self._sync_focus_from_timing_service()

    def _sync_focus_from_timing_service(self):
        """将 TimingService 当前位置同步到 focus 域。"""
        if self._timing_service:
            pos = self._timing_service.get_current_position()
            self.preview.set_focus_position(pos.line_idx, pos.char_idx)

    def _on_bulk_change(self):
        """Ctrl+H — 打开批量変更对话框，自动填充当前焦点字符的连词或划选区域"""
        if self.preview.is_multi_line_selection():
            InfoBar.warning(
                title=self.tr("暂不允许多行"),
                content=self.tr("批量编辑暂不允许多行选择"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return

        initial_word = ""
        initial_reading = ""
        if self._project:
            line_idx = self.preview._current_line_idx
            char_idx = self.preview._current_char_idx
            if 0 <= line_idx < len(self._project.sentences):
                sentence = self._project.sentences[line_idx]
                text = sentence.text
                chars = sentence.characters

                # 优先使用划选区域（多字符选择）
                sel_line = self.preview._focus_line_idx
                sel_start = self.preview._focus_char_idx
                sel_end = self.preview._focus_char_range_end
                if sel_line >= 0 and sel_start >= 0 and sel_line == line_idx:
                    lo = min(sel_start, sel_end)
                    hi = max(sel_start, sel_end)
                    if lo < len(chars) and hi < len(chars) and hi >= lo:
                        initial_word = text[lo : hi + 1]
                        readings: list[str] = []
                        for ci in range(lo, hi + 1):
                            r = chars[ci].ruby
                            readings.append(r.text if r else "")
                        if any(readings):
                            initial_reading = ",".join(readings)
                elif 0 <= char_idx < len(chars):
                    # 回退到连词逻辑（由领域方法 Sentence.get_word_char_range 计算）
                    start, end = sentence.get_word_char_range(char_idx)
                    initial_word = text[start:end]
                    readings = []
                    for ci in range(start, end):
                        r = chars[ci].ruby
                        readings.append(r.text if r else "")
                    if any(readings):
                        initial_reading = ",".join(readings)

        self._open_bulk_change_dialog(
            initial_word=initial_word,
            initial_reading=initial_reading,
        )

    def _open_bulk_change_dialog(
        self, initial_word: str = "", initial_reading: str = "", initial_state=None
    ) -> None:
        """Open bulk mode and route its mode switch back to selected-char mode."""
        from strange_uta_game.frontend.editor.timing import BulkChangeDialog

        dialog = BulkChangeDialog(
            self._project,
            self,
            initial_word=initial_word,
            initial_reading=initial_reading,
            initial_state=initial_state,
        )
        dialog.exec()
        if dialog.switch_to_normal_requested():
            state = dialog.get_switch_state()
            if initial_state and "normal_range" in initial_state:
                state["normal_range"] = initial_state["normal_range"]
            self._on_modify_char(state)

    def _on_modify_char(self, initial_state=None):
        """打开修改所选字符对话框"""
        if not self._project:
            return
        if not isinstance(initial_state, dict):
            initial_state = None

        if self.preview.is_multi_line_selection():
            InfoBar.warning(
                title=self.tr("暂不允许多行"),
                content=self.tr("修改所选字符暂不允许多行选择"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return

        forced_range = initial_state.get("normal_range") if initial_state else None
        if forced_range:
            use_line, start_idx, end_idx = forced_range
        else:
            # Determine selection range
            line_idx = self.preview._current_line_idx
            sel_line = self.preview._focus_line_idx
            sel_start = self.preview._focus_char_idx
            sel_end = self.preview._focus_char_range_end

            if sel_line >= 0 and sel_start >= 0:
                # Use drag selection
                use_line = sel_line
                start_idx = min(sel_start, sel_end)
                end_idx = max(sel_start, sel_end)
            else:
                # Use single char selection
                use_line = line_idx
                char_idx = self.preview._current_char_idx
                start_idx = char_idx
                end_idx = char_idx

        if use_line < 0 or use_line >= len(self._project.sentences):
            return
        sentence = self._project.sentences[use_line]
        if start_idx < 0 or end_idx >= len(sentence.characters):
            return

        # 快照 before：ModifyCharacterDialog 会原地修改 project.sentences
        before_sentences = deepcopy(self._project.sentences)

        dialog = ModifyCharacterDialog(
            sentence, start_idx, end_idx, self, initial_state=initial_state
        )
        dialog.exec()

        if dialog.switch_to_bulk_requested():
            state = dialog.get_switch_state()
            state["normal_range"] = (use_line, start_idx, end_idx)
            self._open_bulk_change_dialog(
                initial_word=state["search_word"], initial_state=state
            )
            return

        if dialog.was_modified():
            # 将本次修改登记为一次 SentenceSnapshotCommand（支持撤销/重做）
            command_manager = None
            if self._timing_service:
                command_manager = self._timing_service.command_manager
            if command_manager is not None:
                after_sentences = deepcopy(self._project.sentences)
                cmd = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    f"修改字符（第 {use_line + 1} 句 第 {start_idx + 1}-{end_idx + 1} 字）",
                )
                cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
                cmd.undo_position = cursor_pos
                cmd.redo_position = cursor_pos
                command_manager.execute(cmd)

            # Reapply global offset & rebuild global checkpoints
            self._reapply_global_offset()
            if self._timing_service:
                self._timing_service.rebuild_global_checkpoints()
            self.refresh_lyric_display()
            self._update_time_tags_display()
            self._update_status()
            if hasattr(self, "_store") and self._store:
                self._store.notify("rubies")
                self._store.notify("checkpoints")
                self._store.notify("lyrics")

            # 弹窗汇总连词失败项
            failures = dialog.get_linked_failures()
            if failures:
                lines = []
                for abs_idx, ch, reason in failures[:20]:
                    lines.append(
                        f"  第 {use_line + 1} 句 第 {abs_idx + 1} 字「{ch}」：{reason}"
                    )
                more = ""
                if len(failures) > 20:
                    more = f"\n...（还有 {len(failures) - 20} 项未显示）"
                message_info(
                    self,
                    self.tr("部分连词设置未应用"),
                    self.tr("以下位置为末字/行尾，不能设置连词，已自动跳过：\n\n")
                    + "\n".join(lines)
                    + more,
                )

    def _on_modify_line(self):
        """打开修改选中行对话框（复用行编辑界面的 LineDetailDialog）"""
        if not self._project:
            return

        line_idx = self.preview._current_line_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return

        sentence = self._project.sentences[line_idx]
        before_sentences = deepcopy(self._project.sentences)

        dialog = LineDetailDialog(sentence, project=self._project, parent=self)
        dialog.exec()

        if dialog.was_modified():
            command_manager = None
            if self._timing_service:
                command_manager = self._timing_service.command_manager
            if command_manager is not None:
                after_sentences = deepcopy(self._project.sentences)
                cmd = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    f"修改选中行（第 {line_idx + 1} 句）",
                )
                cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
                cmd.undo_position = cursor_pos
                cmd.redo_position = cursor_pos
                command_manager.execute(cmd)

            self._reapply_global_offset()
            if self._timing_service:
                self._timing_service.rebuild_global_checkpoints()
            self.refresh_lyric_display()
            self._update_time_tags_display()
            self._update_status()
            if hasattr(self, "_store") and self._store:
                self._store.notify("rubies")
                self._store.notify("checkpoints")
                self._store.notify("lyrics")

    def _on_delete_rubies_by_type(self):
        """工具栏「按类型删除注音」入口（异步）。

        与全文本编辑界面的同名功能及自动注音管线复用同一后端实现
        （DeleteRubyByTypeDialog + delete_rubies_by_type_names）。删除在
        后台线程对项目副本执行，StateToolTip 显示「删除注音 当前/总数」
        文字进度；完成后经 SentenceSnapshotCommand 纳入撤销/重做栈并自动
        同步 timing_service。

        勾选 HIRAGANA → 同时移除小假名(ぁぃ等)与促音 っ；
        勾选 KATAKANA → 同时移除小假名(ァィ等)与促音 ッ。
        """
        if not self._project:
            return
        # 与异步注音分析互斥，避免并发改写句子结构
        if getattr(self, "_ruby_analyzing", False) or getattr(
            self, "_ruby_subset_analyzing", False
        ):
            InfoBar.warning(
                title=self.tr("注音分析进行中"),
                content=self.tr("请等待当前注音分析完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        # 复用 fulltext_interface 的对话框（CharType 复选 + 默认勾选平假名/片假名）
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.frontend.workers import ProjectTaskWorker

        from .fulltext_interface import DeleteRubyByTypeDialog

        app_settings = AppSettings()
        saved_types = app_settings.get("auto_check.delete_ruby_types", [])

        dlg = DeleteRubyByTypeDialog(self, initial_types=saved_types)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_types()
        type_names = dlg.selected_type_names()

        # 保存用户选择到配置（无论是否有变化）
        app_settings.set("auto_check.delete_ruby_types", type_names)
        app_settings.save()

        if not selected:
            return

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)
        removed_box = [0]

        def _task(proj, progress_cb):
            removed_box[0] = delete_rubies_by_type_names(
                proj, type_names, progress_callback=progress_cb
            )

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在删除注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = ProjectTaskWorker(project_copy, _task)
        thread = QThread(self)
        worker.moveToThread(thread)

        # 保存强引用，防止 PyQt6 弱引用机制在函数返回后回收 worker/thread
        self._ruby_delete_worker = worker
        self._ruby_delete_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_delete_worker = None
            self._ruby_delete_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project) -> None:
            state_tooltip.setState(True)
            _cleanup()

            if removed_box[0] == 0:
                InfoBar.info(
                    title=self.tr("无变化"),
                    content=self.tr("所选类型范围内没有需要删除的注音"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2500,
                    parent=self,
                )
                return

            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project, before_sentences, after_sentences, "按类型删除注音"
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            # 焦点保持在当前位置；ruby 变更使用 "rubies" 通道刷新（与 fulltext 一致）
            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=True,
            )

            labels = ", ".join(
                label for ct, label in DeleteRubyByTypeDialog._TYPE_LABELS if ct in selected
            )
            InfoBar.success(
                title=self.tr("删除完成"),
                content=self.tr("已删除 {n} 个注音（类型: {labels}）").format(
                    n=removed_box[0], labels=labels),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("删除注音失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_delete_checkpoints_by_type(self):
        """工具栏「按类型删除节奏点」入口（同步）。

        复用 fulltext_interface 的 DeleteCheckpointByTypeDialog +
        delete_checkpoints_by_type_names。删除是纯内存操作（不含注音分析），
        直接同步执行，经 SentenceSnapshotCommand 纳入撤销/重做栈，并按
        "checkpoints" 通道刷新（与「清除所有节奏点」一致）。

        勾选 hiragana → 连带 っ；勾选 katakana → 连带 ッ/ー（后端语义）。
        """
        if not self._project:
            return
        # 与异步注音分析互斥，避免并发改写句子结构
        if getattr(self, "_ruby_analyzing", False) or getattr(
            self, "_ruby_subset_analyzing", False
        ):
            InfoBar.warning(
                title=self.tr("注音分析进行中"),
                content=self.tr("请等待当前注音分析完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        from strange_uta_game.frontend.settings.settings_interface import AppSettings

        from .fulltext_interface import DeleteCheckpointByTypeDialog

        app_settings = AppSettings()
        saved_types = app_settings.get("auto_check.delete_checkpoint_types", [])

        dlg = DeleteCheckpointByTypeDialog(self, initial_types=saved_types)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_types()
        type_names = dlg.selected_type_names()

        # 保存用户选择到配置（无论是否有变化）
        app_settings.set("auto_check.delete_checkpoint_types", type_names)
        app_settings.save()

        if not selected:
            return

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        removed = delete_checkpoints_by_type_names(self._project, type_names)
        if removed == 0:
            InfoBar.info(
                title=self.tr("无变化"),
                content=self.tr("所选类型范围内没有需要删除的节奏点"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        after_sentences = deepcopy(self._project.sentences)
        command_manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if command_manager is not None:
            command = SentenceSnapshotCommand(
                self._project, before_sentences, after_sentences, "按类型删除节奏点"
            )
            command.undo_position = undo_pos
            command.redo_position = (focus_line_idx, focus_char_idx)
            command_manager.execute(command)

        self._sync_after_structure_change(
            change_type="checkpoints",
            focus_line_idx=focus_line_idx,
            focus_char_idx=focus_char_idx,
            checkpoint_idx=None,
            move_cp=True,
        )

        labels = ", ".join(
            label for ct, label in DeleteCheckpointByTypeDialog._TYPE_LABELS if ct in selected
        )
        InfoBar.success(
            title=self.tr("删除完成"),
            content=self.tr("已删除 {n} 个字符的节奏点（类型: {labels}）").format(
                n=removed, labels=labels),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_delete_rubies_line(self):
        """工具栏「删除选中行注音」— 删除当前选中行内全部注音。"""
        if not self._project:
            return
        line_idx = self._current_line_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            InfoBar.warning(
                title=self.tr("未选中行"),
                content=self.tr("请先在歌词中选择要删除注音的行"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        sentence = self._project.sentences[line_idx]
        self._delete_rubies_in_ranges(
            [(line_idx, 0, len(sentence.characters) - 1)]
            if sentence.characters
            else [],
            self.tr("删除选中行注音（第 {line} 句）").format(line=line_idx + 1),
        )

    def _on_delete_rubies_selected(self):
        """工具栏「删除所选字符注音」— 删除选中字符范围的注音（支持跨行）。"""
        if not self._project:
            return
        ranges = self._collect_selected_char_ranges()
        if not ranges:
            InfoBar.warning(
                title=self.tr("未选中字符"),
                content=self.tr("请先选择要删除注音的字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        self._delete_rubies_in_ranges(
            ranges,
            self.tr("删除所选字符注音（{label}）").format(
                label=self._format_selected_scope_label(ranges)
            ),
        )

    def _delete_rubies_in_ranges(
        self, ranges: list[tuple[int, int, int]], desc: str
    ) -> None:
        """按 (line_idx, start_char, end_char) 闭区间列表删除字符注音。

        同步执行（无需后台线程），一次快照、一条撤销命令；
        删除仅清掉 Ruby 标注，字符自身节奏点/时间戳保持不变。
        """
        if not self._project:
            return
        # 与异步注音分析互斥，避免并发改写句子结构
        if getattr(self, "_ruby_analyzing", False) or getattr(
            self, "_ruby_subset_analyzing", False
        ):
            InfoBar.warning(
                title=self.tr("注音分析进行中"),
                content=self.tr("请等待当前注音分析完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        removed = 0
        for line_idx, start_char, end_char in ranges:
            if line_idx < 0 or line_idx >= len(self._project.sentences):
                continue
            sentence = self._project.sentences[line_idx]
            end_char = min(end_char, len(sentence.characters) - 1)
            for char_idx in range(max(0, start_char), end_char + 1):
                if sentence.remove_ruby_from_char(char_idx) is not None:
                    removed += 1

        if removed == 0:
            InfoBar.info(
                title=self.tr("无变化"),
                content=self.tr("所选范围内没有需要删除的注音"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        after_sentences = deepcopy(self._project.sentences)
        command_manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if command_manager is not None:
            cmd = SentenceSnapshotCommand(self._project, before_sentences, after_sentences, desc)
            cmd.undo_position = undo_pos
            cmd.redo_position = (focus_line_idx, focus_char_idx)
            command_manager.execute(cmd)

        self._sync_after_structure_change(
            change_type="rubies",
            focus_line_idx=focus_line_idx,
            focus_char_idx=focus_char_idx,
            checkpoint_idx=None,
            move_cp=False,
        )
        InfoBar.success(
            title=self.tr("删除完成"),
            content=self.tr("已删除 {n} 个注音").format(n=removed),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def _on_set_singer_by_line(self):
        """工具栏「按行设置演唱者」入口。

        弹出对话框显示所有行（只读），用户可多选行后批量设置演唱者。
        点击"应用"按钮后不关闭对话框，方便继续设置其他行。
        通过 _execute_structural_edit 包装，支持撤销/重做。
        """
        if not self._project:
            return
        if not self._project.singers:
            InfoBar.warning(
                title=self.tr("无演唱者"),
                content=self.tr("项目中没有演唱者，请先添加演唱者"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        from .timing.dialogs import SetSingerByLineDialog

        dlg = SetSingerByLineDialog(
            self._project.sentences,
            [s for s in self._project.singers if s.enabled],
            self,
            focus_line_idx=self._current_line_idx,
        )
        dlg.apply_requested.connect(self._on_apply_singer_by_line)
        dlg.preview_line_requested.connect(self._on_preview_singer_line)
        dlg.exec()

    def _on_preview_singer_line(self, line_idx: int) -> None:
        """从按行设置演唱者对话框试听指定歌词行。"""
        if (
            not self._project
            or not self._timing_service
            or not 0 <= line_idx < len(self._project.sentences)
        ):
            return

        sentence = self._project.sentences[line_idx]
        start_ms = sentence.global_timing_start_ms
        if start_ms is None:
            return

        # 先同步编辑位置，确保暂停状态下预览也立即居中到目标行；随后统一走
        # seek/play 入口，让音频、进度条、波形与播放状态保持一致。
        self._current_line_idx = line_idx
        self.preview.set_current_position(line_idx, 0)
        if sentence.characters:
            self.preview.set_focus_position(line_idx, 0)
        self._update_line_info()
        self._on_seek(start_ms)
        if not self._timing_service.is_playing():
            self._on_play()

    def _on_apply_singer_by_line(self, result_map: dict):
        """处理按行设置演唱者的应用请求"""
        if not self._project or not result_map:
            return

        def _mutate() -> Optional[tuple[int, int, Optional[int], str]]:
            assert self._project is not None
            changed = 0
            for line_idx, singer_id in result_map.items():
                if 0 <= line_idx < len(self._project.sentences):
                    sentence = self._project.sentences[line_idx]
                    sentence.singer_id = singer_id
                    for ch in sentence.characters:
                        if ch.singer_id != singer_id:
                            ch.singer_id = singer_id
                            if ch.ruby:
                                ch.push_to_ruby()
                    changed += 1
            if changed == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "singers")

        ok = self._execute_structural_edit("按行设置演唱者", _mutate)
        if not ok:
            InfoBar.info(
                title=self.tr("无变化"),
                content=self.tr("所选行的演唱者未发生变化"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        InfoBar.success(
            title=self.tr("设置完成"),
            content=self.tr("已为 {n} 行设置演唱者").format(n=len(result_map)),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_apply_singer(self):
        """工具栏「应用演唱者」入口。

        弹出对话框显示当前选中字符信息，用户可选择演唱者并应用到选中字符。
        通过 _execute_structural_edit 包装，支持撤销/重做。
        支持跨行选择：逐行应用演唱者。
        """
        if not self._project:
            return
        if not self._project.singers:
            InfoBar.warning(
                title=self.tr("无演唱者"),
                content=self.tr("项目中没有演唱者，请先添加演唱者"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        line_idx = self._current_line_idx
        char_idx = self.preview._current_char_idx

        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return

        # 获取选中字符范围（支持跨行）
        sel = self.preview.get_normalized_selection()
        if sel is not None and self.preview.is_multi_line_selection():
            start_line, start_char, end_line, end_char = sel
            all_char_texts = []
            singer_ids = set()
            for li in range(start_line, end_line + 1):
                if li >= len(self._project.sentences):
                    continue
                s = self._project.sentences[li]
                if not s.characters:
                    continue
                sc = start_char if li == start_line else 0
                ec = end_char if li == end_line else len(s.characters) - 1
                if sc > ec:
                    continue
                for ci in range(sc, ec + 1):
                    ch = s.characters[ci]
                    all_char_texts.append(ch.char)
                    if ch.singer_id:
                        singer_ids.add(ch.singer_id)
            char_text = "".join(all_char_texts)
            if len(char_text) > 20:
                char_text = char_text[:17] + "..."
            singer_map = {s.id: s for s in self._project.singers}
            current_singers = [singer_map[sid] for sid in singer_ids if sid in singer_map]
            from .timing.dialogs import ApplySingerDialog
            dlg = ApplySingerDialog(
                char_text,
                current_singers,
                [s for s in self._project.singers if s.enabled],
                self,
            )
            dlg.apply_requested.connect(
                lambda singer_id: self._apply_singer_multi_line(
                    start_line, start_char, end_line, end_char, singer_id
                )
            )
            dlg.exec()
            return

        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        start_idx = char_idx
        end_idx = char_idx
        if (
            self.preview._focus_line_idx == line_idx
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            start_idx = min(self.preview._focus_char_idx, self.preview._focus_char_range_end)
            end_idx = max(self.preview._focus_char_idx, self.preview._focus_char_range_end)

        chars = sentence.characters[start_idx:end_idx + 1]
        char_text = "".join(c.char for c in chars)

        singer_ids = set()
        for ch in chars:
            if ch.singer_id:
                singer_ids.add(ch.singer_id)

        singer_map = {s.id: s for s in self._project.singers}
        current_singers = [singer_map[sid] for sid in singer_ids if sid in singer_map]

        from .timing.dialogs import ApplySingerDialog

        dlg = ApplySingerDialog(
            char_text,
            current_singers,
            [s for s in self._project.singers if s.enabled],
            self,
        )
        dlg.apply_requested.connect(lambda singer_id: self._on_apply_singer_to_chars(line_idx, start_idx, end_idx, singer_id))
        dlg.exec()

    def _apply_singer_multi_line(self, start_line: int, start_char: int, end_line: int, end_char: int, singer_id: str):
        """跨行应用演唱者（批量单次 undo）。"""
        if not self._project:
            return
        project = self._project
        def _mutate():
            changed = False
            for line_idx in range(end_line, start_line - 1, -1):
                if line_idx >= len(project.sentences):
                    continue
                sentence = project.sentences[line_idx]
                if not sentence.characters:
                    continue
                s = start_char if line_idx == start_line else 0
                e = end_char if line_idx == end_line else len(sentence.characters) - 1
                if s > e:
                    continue
                for ci in range(s, e + 1):
                    if 0 <= ci < len(sentence.characters):
                        ch = sentence.characters[ci]
                        if ch.singer_id != singer_id:
                            ch.singer_id = singer_id
                            if ch.ruby:
                                ch.push_to_ruby()
                            changed = True
                if s == 0 and e >= len(sentence.characters) - 1:
                    if sentence.singer_id != singer_id:
                        sentence.singer_id = singer_id
                        changed = True
            if not changed:
                return None
            return (start_line, start_char, None, "singers")
        ok = self._execute_structural_edit("应用演唱者", _mutate)
        if not ok:
            InfoBar.info(
                title=self.tr("无变化"),
                content=self.tr("所选字符的演唱者未发生变化"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        InfoBar.success(
            title=self.tr("设置完成"),
            content=self.tr("已为选中字符设置演唱者"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_apply_singer_to_chars(self, line_idx: int, start_idx: int, end_idx: int, singer_id: str):
        """处理应用演唱者到选中字符的请求"""
        if not self._project:
            return

        def _mutate() -> Optional[tuple[int, int, Optional[int], str]]:
            assert self._project is not None
            sentence = self._project.sentences[line_idx]
            changed = False
            for ci in range(start_idx, end_idx + 1):
                if 0 <= ci < len(sentence.characters):
                    ch = sentence.characters[ci]
                    if ch.singer_id != singer_id:
                        ch.singer_id = singer_id
                        if ch.ruby:
                            ch.push_to_ruby()
                        changed = True
            # 如果整个行都被选中，也更新 sentence.singer_id
            if start_idx == 0 and end_idx >= len(sentence.characters) - 1:
                if sentence.singer_id != singer_id:
                    sentence.singer_id = singer_id
                    changed = True
            if not changed:
                return None
            return (line_idx, start_idx, None, "singers")

        ok = self._execute_structural_edit("应用演唱者", _mutate)
        if not ok:
            InfoBar.info(
                title=self.tr("无变化"),
                content=self.tr("所选字符的演唱者未发生变化"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        InfoBar.success(
            title=self.tr("设置完成"),
            content=self.tr("已为选中字符设置演唱者"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_singer_manager_clicked(self):
        """工具栏「演唱者管理」入口。

        打开一个微型浮动窗口，复用 SingerManagerInterface 的全部功能，
        允许用户在打轴的同时随时编辑演唱者。
        """
        try:
            if self._mini_singer_manager is not None and self._mini_singer_manager.isVisible():
                self._mini_singer_manager.raise_()
                self._mini_singer_manager.activateWindow()
                return
        except RuntimeError:
            self._mini_singer_manager = None

        self._mini_singer_manager = MiniSingerManager(self)
        self._mini_singer_manager.destroyed.connect(self._on_mini_singer_manager_closed)
        if self._project:
            self._mini_singer_manager.set_project(self._project)
        if getattr(self, "_store", None) is not None:
            self._mini_singer_manager.set_store(self._store)
        self._mini_singer_manager.show_at_cursor()

    def _on_mini_singer_manager_closed(self):
        self._mini_singer_manager = None

    def _on_insert_guide(self):
        """打开插入导唱符对话框（针对 focus 字符）。

        分支：
        - focus 字符 check_count==0 或无时间戳 → 转为切换"导唱待办"标记
          （用户此时没法选时间戳锚点，先标记待办，等打了轴再补）
        - 否则 → 弹对话框；若对话框中修改了行（插入导唱符），将本次插入
          与"清除 focus 原字符的 needs_guide 标记"合并进同一个撤销快照
        """
        if not self._project:
            return

        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        ch = sentence.characters[char_idx]
        if ch.check_count == 0 or not ch.all_timestamps:
            # 无时间戳锚点 → 改走切换待办
            self._toggle_needs_guide_at(line_idx, char_idx)
            return

        # 快照 before：InsertGuideSymbolDialog 会原地修改 project.sentences
        before_sentences = deepcopy(self._project.sentences)

        dialog = InsertGuideSymbolDialog(sentence, char_idx, self)
        dialog.exec()

        # 对话框中点击了"清除导唱标记"：走单独的 toggle 路径，不进入插入分支
        if dialog.was_clear_marker_requested() and not dialog.was_modified():
            if ch.needs_guide:
                self._toggle_needs_guide_at(line_idx, char_idx)
            return

        # 对话框中点击了"填充所有导唱待办"：扫描全项目批量插入，独立处理
        if dialog.was_fill_all_requested():
            self._fill_all_todo_guides(dialog.fill_all_params())
            return

        if dialog.was_modified():
            # 对话框已插入导唱符 → 顺手清除"原字符"在新位置上的 needs_guide。
            # 插入后该字符已位于 char_idx + N（N=插入个数），但 sentence.characters
            # 里的对象引用未变；直接遍历找回该对象即可。
            orig_obj = ch
            for new_pos, c in enumerate(sentence.characters):
                if c is orig_obj:
                    orig_obj.needs_guide = False
                    break

            # 将本次修改登记为一次 SentenceSnapshotCommand（支持撤销/重做）
            command_manager = None
            if self._timing_service:
                command_manager = self._timing_service.command_manager
            if command_manager is not None:
                after_sentences = deepcopy(self._project.sentences)
                cmd = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    f"插入导唱符（第 {line_idx + 1} 句 第 {char_idx + 1} 字前）",
                )
                cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
                cmd.undo_position = cursor_pos
                cmd.redo_position = cursor_pos
                command_manager.execute(cmd)

            # Reapply global offset & rebuild global checkpoints
            self._reapply_global_offset()
            if self._timing_service:
                self._timing_service.rebuild_global_checkpoints()
            self.refresh_lyric_display()
            self._update_time_tags_display()
            self._update_status()
            if hasattr(self, "_store") and self._store:
                self._store.notify("lyrics")

    def _fill_all_todo_guides(self, params):
        """扫描整个项目中所有 needs_guide 标记的字符，按给定参数批量插入导唱符。

        - 仅对带时间戳锚点（check_count>0 且有时间戳）的待办字符插入；其余跳过。
        - 同一句内按字符索引从右往左处理，避免插入移位影响后续目标的索引与
          「补足间隔时间」的向前时间戳搜索。
        - 插入成功后清除该字符的 needs_guide 标记。
        - 全部修改合并为一次 SentenceSnapshotCommand，支持撤销/重做。
        """
        if not self._project or not params:
            return

        from .timing.dialogs import insert_guide_before

        # 按句分组收集待办目标字符对象（用对象引用，避免索引在插入后失效）
        by_sentence = {}
        for sentence in self._project.sentences:
            todo_chars = [c for c in sentence.characters if getattr(c, "needs_guide", False)]
            if todo_chars:
                by_sentence[id(sentence)] = (sentence, todo_chars)

        if not by_sentence:
            InfoBar.info(
                title=self.tr("没有导唱待办"),
                content=self.tr("项目中没有导唱待办标记。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        before_sentences = deepcopy(self._project.sentences)

        filled = 0
        skipped = 0
        clamped_any = False
        for sentence, todo_chars in by_sentence.values():
            # 按当前索引从右往左处理
            ordered = []
            for c in todo_chars:
                try:
                    ordered.append((sentence.characters.index(c), c))
                except ValueError:
                    continue
            ordered.sort(key=lambda t: t[0], reverse=True)
            for _, c in ordered:
                idx = sentence.characters.index(c)
                # 无时间戳锚点的待办无法定位导唱符，跳过（保留标记）
                if c.check_count == 0 or not c.all_timestamps:
                    skipped += 1
                    continue
                result = insert_guide_before(
                    sentence, idx, params["symbol"], params["count"],
                    params["duration_ms"], params["reverse"], params["fill_gap"],
                )
                if not result["ok"]:
                    skipped += 1
                    continue
                c.needs_guide = False
                filled += 1
                if result.get("clamped"):
                    clamped_any = True

        if filled == 0:
            InfoBar.warning(
                title=self.tr("未填充导唱"),
                content=self.tr("{n} 个导唱待办均无时间戳锚点或间隔无效，已全部跳过。").format(n=skipped),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return

        command_manager = None
        if self._timing_service:
            command_manager = self._timing_service.command_manager
        if command_manager is not None:
            after_sentences = deepcopy(self._project.sentences)
            cmd = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                f"填充所有导唱待办（{filled} 处）",
            )
            cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
            cmd.undo_position = cursor_pos
            cmd.redo_position = cursor_pos
            command_manager.execute(cmd)

        # Reapply global offset & rebuild global checkpoints
        self._reapply_global_offset()
        if self._timing_service:
            self._timing_service.rebuild_global_checkpoints()
        self.refresh_lyric_display()
        self._update_time_tags_display()
        self._update_status()
        if hasattr(self, "_store") and self._store:
            self._store.notify("lyrics")

        content = self.tr("已填充 {filled} 处导唱。").format(filled=filled)
        if skipped:
            content += self.tr("{n} 处因缺时间戳/间隔无效被跳过。").format(n=skipped)
        if clamped_any:
            content += self.tr("部分时间戳越界已自动设为0ms。")
        InfoBar.success(
            title=self.tr("填充完成"),
            content=content,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_complete_timestamp(self):
        """补全时间戳功能入口"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import CompleteTimestampDialog

        dlg = CompleteTimestampDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.was_apply_clicked():
            return

        scope_types = dlg.get_scope_types()
        exclude_rules = dlg.get_exclude_rules()
        head_offset_ms = dlg.get_head_offset_ms()
        tail_offset_ms = dlg.get_tail_offset_ms()

        if not scope_types:
            InfoBar.warning(
                title=self.tr("未选择适用范围"),
                content=self.tr("请至少选择一种字符类型"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        # 执行补全时间戳
        count = self._execute_complete_timestamp(scope_types, exclude_rules, head_offset_ms, tail_offset_ms)

        if count > 0:
            InfoBar.success(
                title=self.tr("补全完成"),
                content=self.tr("已为 {n} 个字符补全时间戳").format(n=count),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无需补全"),
                content=self.tr("没有找到需要补全时间戳的字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _on_separate_symbol_timestamp(self):
        """分离符号时间戳功能入口"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import SeparateSymbolTimestampDialog

        dlg = SeparateSymbolTimestampDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.was_apply_clicked():
            return

        symbol_chars = dlg.get_symbol_chars()
        pre_comp_ms = dlg.get_pre_comp_ms()
        post_comp_ms = dlg.get_post_comp_ms()
        force_copy = dlg.get_force_copy()

        if not symbol_chars:
            InfoBar.warning(
                title=self.tr("未选择符号分组"),
                content=self.tr("请至少选择一个符号分组"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        post_count, pre_count = self._execute_separate_symbol_timestamp(
            symbol_chars, pre_comp_ms, post_comp_ms, force_copy
        )

        total = post_count + pre_count
        if total > 0:
            InfoBar.success(
                title=self.tr("分离完成"),
                content=self.tr("共处理 {total} 个符号（后补偿 {post} 个，前补偿 {pre} 个）").format(
                    total=total, post=post_count, pre=pre_count),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无需处理"),
                content=self.tr("没有找到符合条件的符号时间戳"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _check_timing_complete_for_guide(self) -> bool:
        """检查打轴是否完毕，未完毕时弹窗确认。

        Returns:
            True — 可以继续（已完成或用户确认）
            False — 用户取消
        """
        if not self._project:
            return True
        stats = self._project.get_timing_statistics()
        total_lines = stats.get("total_lines", 0)
        completed_lines = stats.get("completed_lines", 0)
        if completed_lines >= total_lines:
            return True

        untimed = []
        for i, s in enumerate(self._project.sentences):
            if any(c.total_timing_points > 0 for c in s.characters) and not s.is_fully_timed():
                untimed.append(self.tr("第 {line} 行「{text}」").format(line=i + 1, text=s.text))
                if len(untimed) >= 10:
                    break

        detail = "\n".join(untimed)
        extra = self.tr("\n...另 {n} 行").format(n=total_lines - completed_lines - len(untimed)) \
            if total_lines - completed_lines > len(untimed) else ""

        return message_question(
            self,
            self.tr("打轴尚未完毕"),
            self.tr("仅有 {done}/{total} 行完成打轴，继续可能导致生成结果不准确。\n\n"
                    "是否仍要继续？").format(done=completed_lines, total=total_lines)
            + "\n" + detail + extra,
            yes_text=self.tr("仍要继续"),
            no_text=self.tr("取消"),
        )

    def _on_auto_generate_interlude_guide(self):
        """自动生成间奏指引功能入口"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import AutoGenerateInterludeGuideDialog, execute_auto_interlude_guide

        dlg = AutoGenerateInterludeGuideDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.was_apply_clicked():
            return

        min_guide_time_s = dlg.get_min_guide_time_s()
        format_str = dlg.get_format()
        position_mappings = dlg.get_position_mappings()
        allow_inline = dlg.get_allow_inline()
        new_line = dlg.get_new_line()
        front_margin_ms = dlg.get_front_margin_ms()
        back_margin_ms = dlg.get_back_margin_ms()

        if not format_str:
            InfoBar.warning(
                title=self.tr("格式为空"),
                content=self.tr("请输入间奏指引格式字符串"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        # 打轴完毕检测
        if not self._check_timing_complete_for_guide():
            return

        # 快照 before
        before_sentences = deepcopy(self._project.sentences)

        # 后奏必须以当前媒体的真实结尾为边界。项目字段仅作为没有已加载
        # 媒体时的兼容回退，不能覆盖引擎给出的最新时长。
        live_audio_duration_ms = 0
        if self._timing_service:
            live_audio_duration_ms = self._timing_service.get_duration_ms()
        if live_audio_duration_ms > 0:
            self._sync_project_audio_duration(live_audio_duration_ms)

        result = execute_auto_interlude_guide(
            self._project,
            min_guide_time_s,
            format_str,
            position_mappings,
            allow_inline,
            new_line,
            front_margin_ms,
            back_margin_ms,
            audio_duration_ms=live_audio_duration_ms or None,
        )

        inserted = result.get("inserted", 0)
        if inserted == 0:
            InfoBar.info(
                title=self.tr("无符合条件的间隙"),
                content=self.tr("未找到满足最小间隔时间的 is_sentence_end 字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        # 登记撤销
        command_manager = None
        if self._timing_service:
            command_manager = self._timing_service.command_manager
        if command_manager is not None:
            after_sentences = deepcopy(self._project.sentences)
            cmd = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                self.tr("自动生成间奏指引（{n} 处）").format(n=inserted),
            )
            cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
            cmd.undo_position = cursor_pos
            cmd.redo_position = cursor_pos
            command_manager.execute(cmd)

        self._reapply_global_offset()
        if self._timing_service:
            self._timing_service.rebuild_global_checkpoints()
        self.refresh_lyric_display()
        self._update_time_tags_display()
        self._update_status()
        if hasattr(self, "_store") and self._store:
            self._store.notify("lyrics")

        InfoBar.success(
            title=self.tr("生成完成"),
            content=self.tr("已生成 {n} 处间奏指引").format(n=inserted),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self,
        )

    def _on_auto_insert_guide(self):
        """打开根据时间戳扫描的非模态自动导唱工具窗口。"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return
        try:
            if self._auto_guide_dialog is not None and self._auto_guide_dialog.isVisible():
                self._auto_guide_dialog.raise_()
                self._auto_guide_dialog.activateWindow()
                return
        except (AttributeError, RuntimeError):
            self._auto_guide_dialog = None

        from .timing.auto_guide_dialog import AutoGuideDialog

        dialog = AutoGuideDialog(self._project, self)
        self._auto_guide_dialog = dialog
        dialog.locate_requested.connect(self._locate_auto_guide_candidate)
        dialog.execute_requested.connect(self._execute_auto_guide_candidates)
        dialog.destroyed.connect(lambda: setattr(self, "_auto_guide_dialog", None))
        dialog.show()

    def _locate_auto_guide_candidate(self, line_idx: int, char_idx: int):
        """在预览中定位候选字符，并把音频移到目标前 2 秒。"""
        if not self._project or not (0 <= line_idx < len(self._project.sentences)):
            return
        sentence = self._project.sentences[line_idx]
        if not (0 <= char_idx < len(sentence.characters)):
            return
        self._on_char_selected(line_idx, char_idx)
        self.preview.set_focus_position(line_idx, char_idx)
        self.preview.scroll_current_line_to_center()
        ch = sentence.characters[char_idx]
        if ch.all_global_timestamps:
            self._on_seek(max(0, ch.all_global_timestamps[0] - 2000))

    def _execute_auto_guide_candidates(self, items):
        """预检并批量写入自动导唱；整批操作注册为一次撤销。"""
        if not self._project or not items:
            return
        from .timing.auto_guide import apply_auto_guide_candidates
        from .timing.auto_guide_dialog import AutoGuidePreflightDialog

        preflight = AutoGuidePreflightDialog(self._project, items, self)
        preflight.locate_requested.connect(self._locate_auto_guide_candidate)
        if preflight.has_warnings:
            if (
                preflight.exec() != QDialog.DialogCode.Accepted
                or not preflight.should_continue()
            ):
                return

        before_sentences = deepcopy(self._project.sentences)
        result = apply_auto_guide_candidates(self._project, items)
        if result["inserted"] <= 0:
            InfoBar.warning(
                title=self.tr("未插入导唱"),
                content=self.tr("所选候选均已失效或参数不可执行，请重新扫描。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )
            return

        command_manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if command_manager is not None:
            after_sentences = deepcopy(self._project.sentences)
            cmd = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                self.tr("自动插入导唱符（{n} 处）").format(n=result["inserted"]),
            )
            cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
            cmd.undo_position = cursor_pos
            cmd.redo_position = cursor_pos
            command_manager.execute(cmd)

        dialog = getattr(self, "_auto_guide_dialog", None)
        if dialog is not None:
            dialog.remember_first_executed(items)
            dialog.close()

        self._reapply_global_offset()
        if self._timing_service:
            self._timing_service.rebuild_global_checkpoints()
        self.refresh_lyric_display()
        self._update_time_tags_display()
        self._update_status()
        if hasattr(self, "_store") and self._store:
            self._store.notify("lyrics")

        InfoBar.success(
            title=self.tr("自动导唱完成"),
            content=self.tr(
                "处理 {positions} 处，新增 {chars} 个字符，替换 {replaced} 处已有导唱，"
                "清理 {todos} 个待办，跳过 {skipped} 处。"
            ).format(
                positions=result["inserted"],
                chars=result["inserted_chars"],
                replaced=result["replaced"],
                todos=result["cleared_todos"],
                skipped=result["skipped"],
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _on_adjust_raw_timestamp(self):
        """调整原始时间戳功能入口 — 打开非模态调整窗口，允许边测试边调整"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import AdjustRawTimestampDialog

        if not hasattr(self, "_adjust_ts_dlg") or self._adjust_ts_dlg is None or not self._adjust_ts_dlg.isVisible():
            self._adjust_ts_dlg = AdjustRawTimestampDialog(self)
            self._adjust_ts_dlg.apply_requested.connect(self._on_apply_adjust_raw_timestamp)

        self._adjust_ts_dlg.show()
        self._adjust_ts_dlg.raise_()
        self._adjust_ts_dlg.activateWindow()

    def _on_apply_adjust_raw_timestamp(self, delta_ms: int):
        """处理调整原始时间戳对话框的应用请求"""
        if not self._project:
            return

        project = self._project

        def _mutate():
            modified = 0
            for sentence in project.sentences:
                for ch in sentence.characters:
                    if ch.check_count > 0 and ch.timestamps:
                        ch.timestamps = [max(0, ts + delta_ms) for ts in ch.timestamps]
                        modified += 1
                    if ch.sentence_end_ts is not None:
                        ch.sentence_end_ts = max(0, ch.sentence_end_ts + delta_ms)
                    ch._update_offset_timestamps()
                    ch.push_to_ruby()
            if modified == 0 and all(
                ch.sentence_end_ts is None
                for sentence in project.sentences
                for ch in sentence.characters
            ):
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("调整原始时间戳", _mutate)
        if ok:
            if hasattr(self, "_adjust_ts_dlg") and self._adjust_ts_dlg is not None:
                self._adjust_ts_dlg.set_status(self.tr("已成功偏移 {delta:+d} ms").format(delta=delta_ms))
            InfoBar.success(
                title=self.tr("调整完成"),
                content=self.tr("所有原始时间戳已整体偏移 {delta:+d} ms").format(delta=delta_ms),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            if hasattr(self, "_adjust_ts_dlg") and self._adjust_ts_dlg is not None:
                self._adjust_ts_dlg.set_status(self.tr("无可调整的时间戳"), success=False)

    def _on_adjust_raw_timestamp_line(self):
        """按行调整原始时间戳 — 打开非模态调整窗口，作用于当前行。"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        line_idx = self._current_line_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            InfoBar.warning(
                title=self.tr("未选中行"),
                content=self.tr("请先选中要调整的歌词行"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import AdjustRawTimestampDialog

        self._adjust_ts_line_target = line_idx
        scope_label = self.tr("第 {n} 行").format(n=line_idx + 1)
        if not hasattr(self, "_adjust_ts_line_dlg") or self._adjust_ts_line_dlg is None or not self._adjust_ts_line_dlg.isVisible():
            self._adjust_ts_line_dlg = AdjustRawTimestampDialog(self, scope="line", scope_label=scope_label)
            self._adjust_ts_line_dlg.apply_requested.connect(self._on_apply_adjust_raw_timestamp_line)
        else:
            self._adjust_ts_line_dlg.lbl_scope.setText(self.tr("作用范围：{label}").format(label=scope_label))

        self._adjust_ts_line_dlg.show()
        self._adjust_ts_line_dlg.raise_()
        self._adjust_ts_line_dlg.activateWindow()

    def _on_apply_adjust_raw_timestamp_line(self, delta_ms: int):
        """处理按行调整原始时间戳对话框的应用请求。"""
        if not self._project:
            return

        line_idx = getattr(self, "_adjust_ts_line_target", -1)
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            if hasattr(self, "_adjust_ts_line_dlg") and self._adjust_ts_line_dlg is not None:
                self._adjust_ts_line_dlg.set_status(self.tr("目标行已失效"), success=False)
            return

        sentence = self._project.sentences[line_idx]

        def _mutate():
            modified = 0
            for ch in sentence.characters:
                if ch.check_count > 0 and ch.timestamps:
                    ch.timestamps = [max(0, ts + delta_ms) for ts in ch.timestamps]
                    modified += 1
                if ch.sentence_end_ts is not None:
                    ch.sentence_end_ts = max(0, ch.sentence_end_ts + delta_ms)
                ch._update_offset_timestamps()
                ch.push_to_ruby()
            if modified == 0 and all(ch.sentence_end_ts is None for ch in sentence.characters):
                return None
            return (line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("按行调整原始时间戳", _mutate)
        if ok:
            if hasattr(self, "_adjust_ts_line_dlg") and self._adjust_ts_line_dlg is not None:
                self._adjust_ts_line_dlg.set_status(self.tr("已成功偏移 {delta:+d} ms").format(delta=delta_ms))
            InfoBar.success(
                title=self.tr("调整完成"),
                content=self.tr("第 {line} 行原始时间戳已整体偏移 {delta:+d} ms").format(
                    line=line_idx + 1, delta=delta_ms),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            if hasattr(self, "_adjust_ts_line_dlg") and self._adjust_ts_line_dlg is not None:
                self._adjust_ts_line_dlg.set_status(self.tr("无可调整的时间戳"), success=False)

    def _on_adjust_raw_timestamp_selected(self):
        """调整所选字符原始时间戳 — 打开非模态调整窗口，作用于选中字符范围。"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        ranges = self._collect_selected_char_ranges()
        if not ranges:
            InfoBar.warning(
                title=self.tr("未选中字符"),
                content=self.tr("请先选择要调整的字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        from .timing.dialogs import AdjustRawTimestampDialog

        self._adjust_ts_selected_ranges = ranges
        scope_label = self._format_selected_scope_label(ranges)
        if not hasattr(self, "_adjust_ts_sel_dlg") or self._adjust_ts_sel_dlg is None or not self._adjust_ts_sel_dlg.isVisible():
            self._adjust_ts_sel_dlg = AdjustRawTimestampDialog(self, scope="selected", scope_label=scope_label)
            self._adjust_ts_sel_dlg.apply_requested.connect(self._on_apply_adjust_raw_timestamp_selected)
        else:
            self._adjust_ts_sel_dlg.lbl_scope.setText(self.tr("作用范围：{label}").format(label=scope_label))

        self._adjust_ts_sel_dlg.show()
        self._adjust_ts_sel_dlg.raise_()
        self._adjust_ts_sel_dlg.activateWindow()

    def _collect_selected_char_ranges(self) -> list[tuple[int, int, int]]:
        """收集当前选中字符的 (line_idx, start_char, end_char) 闭区间列表。

        支持跨行选择；若无明确选择，则使用当前光标所在单字符。
        """
        if not self._project:
            return []

        ranges: list[tuple[int, int, int]] = []

        if self.preview.is_multi_line_selection():
            sel = self.preview.get_normalized_selection()
            if sel is None:
                return []
            start_line, start_char, end_line, end_char = sel
            for li in range(start_line, end_line + 1):
                if li < 0 or li >= len(self._project.sentences):
                    continue
                sentence = self._project.sentences[li]
                if not sentence.characters:
                    continue
                s = start_char if li == start_line else 0
                e = end_char if li == end_line else len(sentence.characters) - 1
                s = max(0, s)
                e = min(len(sentence.characters) - 1, e)
                if s > e:
                    continue
                ranges.append((li, s, e))
            return ranges

        line_idx = self._current_line_idx
        char_idx = self.preview._current_char_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return []
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return []

        start_idx = char_idx
        end_idx = char_idx
        if (
            self.preview._focus_line_idx == line_idx
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            start_idx = min(self.preview._focus_char_idx, self.preview._focus_char_range_end)
            end_idx = max(self.preview._focus_char_idx, self.preview._focus_char_range_end)
        ranges.append((line_idx, start_idx, end_idx))
        return ranges

    def _format_selected_scope_label(self, ranges: list[tuple[int, int, int]]) -> str:
        if not ranges:
            return self.tr("所选字符")
        if len(ranges) == 1:
            li, s, e = ranges[0]
            if s == e:
                return self.tr("第 {line} 行 第 {char} 字").format(line=li + 1, char=s + 1)
            return self.tr("第 {line} 行 第 {s}-{e} 字").format(
                line=li + 1, s=s + 1, e=e + 1)
        total = sum(e - s + 1 for _, s, e in ranges)
        return self.tr("{start} - {end} 行，共 {total} 字").format(
            start=ranges[0][0] + 1, end=ranges[-1][0] + 1, total=total)

    def _on_apply_adjust_raw_timestamp_selected(self, delta_ms: int):
        """处理调整所选字符原始时间戳对话框的应用请求。"""
        if not self._project:
            return

        ranges: list[tuple[int, int, int]] = getattr(self, "_adjust_ts_selected_ranges", [])
        if not ranges:
            if hasattr(self, "_adjust_ts_sel_dlg") and self._adjust_ts_sel_dlg is not None:
                self._adjust_ts_sel_dlg.set_status(self.tr("选区已失效"), success=False)
            return

        project = self._project

        def _mutate():
            modified = 0
            touched_end_ts = False
            for li, s, e in ranges:
                if li < 0 or li >= len(project.sentences):
                    continue
                sentence = project.sentences[li]
                for ci in range(s, e + 1):
                    if ci < 0 or ci >= len(sentence.characters):
                        continue
                    ch = sentence.characters[ci]
                    if ch.check_count > 0 and ch.timestamps:
                        ch.timestamps = [max(0, ts + delta_ms) for ts in ch.timestamps]
                        modified += 1
                    if ch.sentence_end_ts is not None:
                        ch.sentence_end_ts = max(0, ch.sentence_end_ts + delta_ms)
                        touched_end_ts = True
                    ch._update_offset_timestamps()
                    ch.push_to_ruby()
            if modified == 0 and not touched_end_ts:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("调整所选字符原始时间戳", _mutate)
        if ok:
            if hasattr(self, "_adjust_ts_sel_dlg") and self._adjust_ts_sel_dlg is not None:
                self._adjust_ts_sel_dlg.set_status(self.tr("已成功偏移 {delta:+d} ms").format(delta=delta_ms))
            InfoBar.success(
                title=self.tr("调整完成"),
                content=self.tr("所选字符原始时间戳已偏移 {delta:+d} ms").format(delta=delta_ms),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            if hasattr(self, "_adjust_ts_sel_dlg") and self._adjust_ts_sel_dlg is not None:
                self._adjust_ts_sel_dlg.set_status(self.tr("无可调整的时间戳"), success=False)

    # ── 删除时间戳 ──

    def _on_delete_all_timestamps(self):
        """删除所有时间戳"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        if not message_question(
            self,
            self.tr("删除所有时间戳"),
            self.tr("确定要删除所有时间戳吗？此操作可撤销。"),
            yes_text=self.tr("删除"),
            no_text=self.tr("取消"),
            default_cancel=True,
        ):
            return

        project = self._project

        def _mutate():
            cleared = 0
            for sentence in project.sentences:
                for ch in sentence.characters:
                    if ch.timestamps or ch.sentence_end_ts is not None:
                        ch.clear_timestamps()
                        cleared += 1
            if cleared == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("删除所有时间戳", _mutate)
        if ok:
            InfoBar.success(
                title=self.tr("删除完成"),
                content=self.tr("已删除所有时间戳"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无时间戳"),
                content=self.tr("当前项目没有需要删除的时间戳"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _on_delete_all_timestamps_keep_head(self):
        """删除所有时间戳（保留行首）"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        if not message_question(
            self,
            self.tr("删除所有时间戳（保留行首）"),
            self.tr("确定要删除所有时间戳（保留行首）吗？此操作可撤销。"),
            yes_text=self.tr("删除"),
            no_text=self.tr("取消"),
            default_cancel=True,
        ):
            return

        project = self._project

        def _mutate():
            cleared = 0
            for sentence in project.sentences:
                for idx, ch in enumerate(sentence.characters):
                    if idx == 0:
                        if ch.sentence_end_ts is not None:
                            ch.sentence_end_ts = None
                            ch._update_offset_timestamps()
                            ch.push_to_ruby()
                            cleared += 1
                        continue
                    if ch.timestamps or ch.sentence_end_ts is not None:
                        ch.clear_timestamps()
                        cleared += 1
            if cleared == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("删除所有时间戳（保留行首）", _mutate)
        if ok:
            InfoBar.success(
                title=self.tr("删除完成"),
                content=self.tr("已删除所有时间戳（保留行首）"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无时间戳"),
                content=self.tr("当前项目没有需要删除的时间戳"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _on_delete_timestamps_selected(self):
        """删除所选范围时间戳"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        ranges = self._collect_selected_char_ranges()
        if not ranges:
            InfoBar.warning(
                title=self.tr("未选中字符"),
                content=self.tr("请先选择要删除时间戳的字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        project = self._project

        def _mutate():
            cleared = 0
            for li, s, e in ranges:
                if li < 0 or li >= len(project.sentences):
                    continue
                sentence = project.sentences[li]
                for ci in range(s, e + 1):
                    if ci < 0 or ci >= len(sentence.characters):
                        continue
                    ch = sentence.characters[ci]
                    if ch.timestamps or ch.sentence_end_ts is not None:
                        ch.clear_timestamps()
                        cleared += 1
            if cleared == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        scope_label = self._format_selected_scope_label(ranges)
        ok = self._execute_structural_edit("删除所选范围时间戳", _mutate)
        if ok:
            InfoBar.success(
                title=self.tr("删除完成"),
                content=self.tr("{scope} 的时间戳已删除").format(scope=scope_label),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无时间戳"),
                content=self.tr("{scope} 没有需要删除的时间戳").format(scope=scope_label),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _execute_complete_timestamp(self, scope_types: set[str], exclude_rules: list[str], head_offset_ms: int = 150, tail_offset_ms: int = 150) -> int:
        """执行补全时间戳的核心逻辑

        Args:
            scope_types: 选中的字符类型集合
            exclude_rules: 选中的排除规则列表
            head_offset_ms: 行首无前方时间戳时，向后找到时间戳后扣除的毫秒数
            tail_offset_ms: 行尾无后方时间戳时，向前找到时间戳后增加的毫秒数

        Returns:
            补全的字符数量
        """
        if not self._project:
            return 0

        from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
            CharType,
            get_char_type,
        )

        # 映射 scope_types 到 CharType
        type_map = {
            "kanji": CharType.KANJI,
            "hiragana": CharType.HIRAGANA,
            "katakana": CharType.KATAKANA,
            "sokuon": CharType.SOKUON,
            "long_vowel": CharType.LONG_VOWEL,
            "alphabet": CharType.ALPHABET,
            "hangul": CharType.HANGUL,
            "number": CharType.NUMBER,
            "symbol": CharType.SYMBOL,
        }

        target_types = set()
        for key in scope_types:
            if key in type_map:
                target_types.add(type_map[key])

        # 捨仮名需要特殊处理（小假名）
        include_chisai_kana = "chisai_kana" in scope_types
        _SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮゕゖ")

        # 拨音需要特殊处理
        include_chon = "chon" in scope_types
        _CHON_CHARS = set("んン")

        exclude_linked = "linked" in exclude_rules

        def _is_target_char(ch_obj, char_idx: int, chars_list) -> bool:
            """判断字符是否为目标类型（check_count=0 且符合适用规则）"""
            char = ch_obj.char
            # 跳过 check_count > 0 的字符（已有节奏点，无需补全）
            if ch_obj.check_count > 0:
                return False
            # 跳过被连词字符（如果启用排除）
            # 连词组中的所有字符都应被排除：当前字符 linked_to_next=True 或前一个字符 linked_to_next=True
            if exclude_linked:
                if ch_obj.linked_to_next:
                    return False
                if char_idx > 0 and chars_list[char_idx - 1].linked_to_next:
                    return False

            # 捨仮名字符只由 chisai_kana 选项控制，不被平假名/片假名覆盖
            if char in _SMALL_KANA:
                return include_chisai_kana

            # 拨音字符只由 chon 选项控制，不被平假名/片假名覆盖
            if char in _CHON_CHARS:
                return include_chon

            # 普通类型检查
            try:
                char_type = get_char_type(char)
                return char_type in target_types
            except (ValueError, IndexError):
                return False

        def _find_prev_timestamp(line_idx: int, char_idx: int) -> Optional[int]:
            """向前逐字查找最近的时间戳（在同一行内）

            同时检查普通时间戳和停顿点时间戳，取较大者（更接近目标字符）。
            """
            sentence = self._project.sentences[line_idx]
            for ci in range(char_idx - 1, -1, -1):
                ch = sentence.characters[ci]
                candidates = []
                if ch.timestamps:
                    candidates.append(ch.timestamps[-1])
                if ch.is_sentence_end and ch.sentence_end_ts is not None:
                    candidates.append(ch.sentence_end_ts)
                if candidates:
                    return max(candidates)
            return None

        def _find_next_timestamp(line_idx: int, char_idx: int) -> Optional[int]:
            """向后逐字查找最近的时间戳（在同一行内）

            同时检查普通时间戳和停顿点时间戳，取较小者（更接近目标字符）。
            """
            sentence = self._project.sentences[line_idx]
            for ci in range(char_idx + 1, len(sentence.characters)):
                ch = sentence.characters[ci]
                candidates = []
                if ch.timestamps:
                    candidates.append(ch.timestamps[0])
                if ch.is_sentence_end and ch.sentence_end_ts is not None:
                    candidates.append(ch.sentence_end_ts)
                if candidates:
                    return min(candidates)
            return None

        total_count = 0

        def _mutate() -> Optional[tuple[int, int, Optional[int], str]]:
            nonlocal total_count
            assert self._project is not None

            for line_idx, sentence in enumerate(self._project.sentences):
                chars = sentence.characters
                total_chars = len(chars)

                # 预处理：停顿点特殊符号（cc=0 + is_sentence_end + sentence_end_ts）。
                # 无论行中还是行尾，都先把停顿点点降级为普通 cp 并把停顿点后推
                # tail_offset，使其成为后续插值的锚点。否则行中的停顿点符号会落入
                # 通用插值分支，丢弃真实停顿点点、也拿不到符号特殊补偿。
                for ci in range(total_chars):
                    ch = chars[ci]
                    if (ch.check_count == 0
                            and _is_target_char(ch, ci, chars)
                            and get_char_type(ch.char) == CharType.SYMBOL
                            and ch.is_sentence_end
                            and ch.sentence_end_ts is not None):
                        original_end_ts = ch.sentence_end_ts
                        ch.timestamps = [original_end_ts]
                        ch.sentence_end_ts = original_end_ts + tail_offset_ms
                        ch.set_check_count(1, force=True)
                        total_count += 1

                i = 0
                while i < total_chars:
                    # 跳过不符合适用条件的字符
                    if not _is_target_char(chars[i], i, chars):
                        i += 1
                        continue

                    # 收集连续的待补全字符段。
                    # 遇到带 sentence_end_ts 的非符号字符时，将其纳入当前段
                    # 但立即结束：停顿点时间戳本身就是天然边界，不应与后文混段。
                    segment_start = i
                    while i < total_chars and _is_target_char(chars[i], i, chars):
                        cur = chars[i]
                        if (cur.is_sentence_end
                                and cur.sentence_end_ts is not None
                                and get_char_type(cur.char) != CharType.SYMBOL):
                            i += 1
                            break
                        i += 1
                    segment_end = i  # 不包含

                    segment_len = segment_end - segment_start

                    # 判断段的位置
                    is_at_start = (segment_start == 0)  # 行首

                    # 查找前后时间戳（先于行尾判断：行尾需参考 next_ts）
                    prev_ts = _find_prev_timestamp(line_idx, segment_start)
                    next_ts = _find_next_timestamp(line_idx, segment_end - 1)

                    # 分段保证了：若段内有停顿点字符，它一定是段的最后一个。
                    # 其 sentence_end_ts 是比 _find_next_timestamp 更紧的上限。
                    last_char = chars[segment_end - 1]
                    if last_char.is_sentence_end and last_char.sentence_end_ts is not None:
                        if next_ts is None or last_char.sentence_end_ts < next_ts:
                            next_ts = last_char.sentence_end_ts

                    # 行尾：段之后若仅剩“多余空格占位符”（停顿点 token 贴轴后被解析
                    # 出来的裸空格），也视为行尾。必须校验 check_count==0，避免误吞
                    # 真正带轴的空格字符。
                    # 停顿点以 is_sentence_end 为准，而非单纯行尾位置：停顿标记可能
                    # 落在段后的尾随空格上，此时 _find_next_timestamp 会命中其
                    # sentence_end_ts（next_ts 非空）。这种情况应按停顿点锚点插值，
                    # 不能当作“无后方锚点的行尾”走 prev_ts+tail 兜底，否则会丢弃
                    # 真实的停顿点时间戳。
                    is_at_end = next_ts is None and all(
                        chars[ci].check_count == 0
                        and get_char_type(chars[ci].char) in (CharType.SPACE, CharType.FULL_SPACE)
                        for ci in range(segment_end, total_chars)
                    )

                    # 根据位置和时间戳决定处理方式
                    if is_at_start and is_at_end:
                        # 整行都是待补全字符，前后都没有时间戳，跳过
                        continue
                    elif is_at_start:
                        # 行首：只有后方时间戳，逐个递减
                        if next_ts is None:
                            continue
                        # 从后往前逐个递减
                        for ci in range(segment_end - 1, segment_start - 1, -1):
                            if ci == segment_end - 1:
                                ts = max(0, next_ts - head_offset_ms)
                            else:
                                ts = max(0, chars[ci + 1].timestamps[0] - head_offset_ms)
                            chars[ci].timestamps = [ts]
                            chars[ci].set_check_count(1, force=True)
                            total_count += 1
                    elif is_at_end:
                        # 行尾：只有前方时间戳。
                        # 停顿点特殊符号已在预处理 pass 中降级为锚点，这里 last_char
                        # 不会再是带 sentence_end_ts 的停顿点符号，故统一走均分。
                        if prev_ts is None:
                            continue
                        last_char = chars[segment_end - 1]
                        # 均分(prev_ts, 停顿点时间戳)；无停顿点时以 prev_ts + tail_offset 兜底
                        end_ts = (last_char.sentence_end_ts
                                  if last_char.is_sentence_end
                                     and last_char.sentence_end_ts is not None
                                  else prev_ts + tail_offset_ms)
                        # A10：脏数据（停顿点时间戳早于前方锚点 / 负 tail_offset）
                        # 会让 end_ts < prev_ts，均分产生递减时间戳；钳到不早于锚点。
                        end_ts = max(end_ts, prev_ts)
                        time_diff = end_ts - prev_ts
                        for idx, ci in enumerate(range(segment_start, segment_end)):
                            ts = prev_ts + time_diff * (idx + 1) // (segment_len + 1)
                            chars[ci].timestamps = [ts]
                            chars[ci].set_check_count(1, force=True)
                            total_count += 1
                    else:
                        # 行中：前后都应该有时间戳
                        if prev_ts is None or next_ts is None:
                            continue
                        if segment_len == 1:
                            avg_ts = (prev_ts + next_ts) // 2
                            chars[segment_start].timestamps = [avg_ts]
                            chars[segment_start].set_check_count(1, force=True)
                            total_count += 1
                        else:
                            time_diff = next_ts - prev_ts
                            for idx, ci in enumerate(range(segment_start, segment_end)):
                                ts = prev_ts + time_diff * (idx + 1) // (segment_len + 1)
                                chars[ci].timestamps = [ts]
                                chars[ci].set_check_count(1, force=True)
                                total_count += 1

            if total_count == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("补全时间戳", _mutate)
        if not ok:
            return 0

        return total_count

    def _execute_separate_symbol_timestamp(
        self,
        symbol_chars: frozenset,
        pre_comp_ms: int,
        post_comp_ms: int,
        force_copy: bool = False,
    ) -> tuple:
        """执行分离符号时间戳的核心逻辑，根据模式分发到子函数。

        Returns:
            (post_count, pre_count)
        """
        if not self._project:
            return 0, 0

        if force_copy:
            return self._separate_symbol_force(symbol_chars, pre_comp_ms, post_comp_ms)
        return self._separate_symbol_normal(symbol_chars, pre_comp_ms, post_comp_ms)

    def _separate_symbol_normal(
        self,
        symbol_chars: frozenset,
        pre_comp_ms: int,
        post_comp_ms: int,
    ) -> tuple:
        """普通模式：分离符号时间戳。

        - Pass 1（后补偿）：符号 cc=0 且 is_sentence_end=True 且 sentence_end_ts 不为空
          → cc 改为 1，timestamps = [old_end_ts]，sentence_end_ts 后移 post_comp_ms
        - Pass 2（前补偿）：符号 cc=1 且 is_sentence_end=False 且紧跟的第一个非符号字符 cc=0
          → 非符号字符 cc 改为 1 并获得符号时间戳，符号时间戳前移 pre_comp_ms
        """
        post_count = 0
        pre_count = 0

        def _mutate():
            nonlocal post_count, pre_count
            assert self._project is not None

            all_chars: list = []
            for sentence in self._project.sentences:
                for ch in sentence.characters:
                    all_chars.append(ch)

            # ── Pass 1: 后补偿 ──────────────────────────
            for ch in all_chars:
                if (
                    ch.char in symbol_chars
                    and ch.check_count == 0
                    and ch.is_sentence_end
                    and ch.sentence_end_ts is not None
                ):
                    old_end_ts = ch.sentence_end_ts
                    ch.timestamps = [old_end_ts]
                    ch.sentence_end_ts = old_end_ts + post_comp_ms
                    ch.set_check_count(1, force=True)
                    post_count += 1

            # ── Pass 2: 前补偿 ──────────────────────────
            for i, ch in enumerate(all_chars):
                if (
                    ch.char not in symbol_chars
                    or ch.check_count != 1
                    or not ch.timestamps
                    or ch.is_sentence_end
                ):
                    continue

                next_non_sym = None
                for j in range(i + 1, len(all_chars)):
                    if all_chars[j].char not in symbol_chars:
                        next_non_sym = all_chars[j]
                        break

                if next_non_sym is None or next_non_sym.check_count != 0:
                    continue

                old_sym_ts = ch.timestamps[0]
                next_non_sym.timestamps = [old_sym_ts]
                next_non_sym.set_check_count(1, force=True)

                ch.timestamps = [max(0, old_sym_ts - pre_comp_ms)]
                ch._update_offset_timestamps()
                ch.push_to_ruby()
                pre_count += 1

            if post_count == 0 and pre_count == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("分离符号时间戳", _mutate)
        if not ok:
            return 0, 0
        return post_count, pre_count

    def _separate_symbol_force(
        self,
        symbol_chars: frozenset,
        pre_comp_ms: int,
        post_comp_ms: int,
    ) -> tuple:
        """强制复制模式：连续符号视为整体，不检测相邻非符号字符状态。

        后补偿（从右向左扫描）：
          连续停顿点符号视为整体 → 符号组全部普通时间戳集中赋予前一字符，
          组首获 sentence_end_ts 作为普通时间戳，其余符号均匀分配后补偿区间，
          组尾保持 sentence_end 标记并后移。

        前补偿（从左向右扫描）：
          连续非停顿点符号视为整体 → 符号组全部时间戳 prepend 至后一字符，
          组首时间戳前移 pre_comp_ms，其余符号均匀分配前补偿区间。
        """
        post_count = 0
        pre_count = 0

        def _mutate():
            nonlocal post_count, pre_count
            assert self._project is not None

            all_chars: list = []
            for sentence in self._project.sentences:
                for ch in sentence.characters:
                    all_chars.append(ch)

            n = len(all_chars)
            processed: set[int] = set()

            # ═══════════════════════════════════════════════
            #  后补偿：连续停顿点符号整体处理（从右向左扫描）
            # ═══════════════════════════════════════════════
            for i in range(n - 1, -1, -1):
                ch = all_chars[i]
                if (
                    ch.char not in symbol_chars
                    or not ch.is_sentence_end
                    or ch.sentence_end_ts is None
                ):
                    continue
                if i in processed:
                    continue

                # 向前找连续符号组的起点
                group_start = i
                while group_start > 0 and all_chars[group_start - 1].char in symbol_chars:
                    group_start -= 1

                group = range(group_start, i + 1)
                processed.update(group)

                # 收集组内全部普通时间戳
                all_ts: list[int] = []
                for j in group:
                    all_ts.extend(all_chars[j].timestamps)
                    all_chars[j].timestamps = []

                # 全部时间戳赋予组前一字符
                prev_idx = group_start - 1
                if prev_idx >= 0:
                    prev_ch = all_chars[prev_idx]
                    prev_ch.set_check_count(prev_ch.check_count + len(all_ts), force=True)
                    if all_ts:
                        prev_ch.timestamps.extend(all_ts)
                        prev_ch._update_offset_timestamps()

                sent_ts = ch.sentence_end_ts
                new_sent_end = sent_ts + post_comp_ms

                # 组首获得 sentence_end_ts 作为普通时间戳
                all_chars[group_start].timestamps = [sent_ts]
                all_chars[group_start].set_check_count(1, force=True)

                # 组内其余符号均匀分配 [sent_ts, new_sent_end] 区间
                rest = [j for j in group if j != group_start]
                m = len(rest)
                for rank, j in enumerate(rest, 1):
                    val = sent_ts + post_comp_ms * rank // (m + 1)
                    all_chars[j].timestamps = [val]
                    all_chars[j].set_check_count(1, force=True)

                # 停顿点符号保持标记并后移
                ch.sentence_end_ts = new_sent_end
                post_count += 1

            # ═══════════════════════════════════════════════
            #  前补偿：连续非停顿点符号整体处理（从左向右扫描）
            # ═══════════════════════════════════════════════
            for i, ch in enumerate(all_chars):
                if ch.char not in symbol_chars or ch.is_sentence_end:
                    continue
                if i in processed:
                    continue
                if ch.check_count < 1 or not ch.timestamps:
                    continue

                # 向后找连续非停顿点符号组的终点
                group_end = i
                while (
                    group_end + 1 < n
                    and all_chars[group_end + 1].char in symbol_chars
                    and not all_chars[group_end + 1].is_sentence_end
                    and all_chars[group_end + 1].check_count >= 1
                    and all_chars[group_end + 1].timestamps
                ):
                    group_end += 1

                group = range(i, group_end + 1)
                processed.update(group)

                # 收集组内全部普通时间戳
                all_ts: list[int] = []
                for j in group:
                    all_ts.extend(all_chars[j].timestamps)
                    all_chars[j].timestamps = []

                # 全部时间戳 prepend 至组后一字符
                next_idx = group_end + 1
                if next_idx < n:
                    next_ch = all_chars[next_idx]
                    next_ch.set_check_count(next_ch.check_count + len(all_ts), force=True)
                    if all_ts:
                        next_ch.timestamps = all_ts + next_ch.timestamps
                        next_ch._update_offset_timestamps()

                first_old_ts = all_ts[0] if all_ts else 0
                new_first = max(0, first_old_ts - pre_comp_ms)

                # 组首时间戳前移
                all_chars[i].timestamps = [new_first]
                all_chars[i].set_check_count(1, force=True)
                all_chars[i]._update_offset_timestamps()
                all_chars[i].push_to_ruby()

                # 组内其余符号均匀分配 [new_first, first_old_ts] 区间
                rest = [j for j in group if j != i]
                m = len(rest)
                for rank, j in enumerate(rest, 1):
                    val = new_first + pre_comp_ms * rank // (m + 1)
                    all_chars[j].timestamps = [val]
                    all_chars[j].set_check_count(1, force=True)

                pre_count += 1

            if post_count == 0 and pre_count == 0:
                return None
            return (self._current_line_idx, self.preview._current_char_idx, None, "timetags")

        ok = self._execute_structural_edit("分离符号时间戳", _mutate)
        if not ok:
            return 0, 0
        return post_count, pre_count

    # ==================== 音频 ====================

    def _on_singer_change_selection(
        self, line_idx: int, start_char: int, end_char: int, singer_id: str
    ):
        """划词选中后，修改选中范围内所有字符的 per-char singer_id"""
        if (
            not self._project
            or line_idx < 0
            or line_idx >= len(self._project.sentences)
        ):
            return

        project = self._project

        def _mutate():
            sentence = project.sentences[line_idx]
            changed = False

            for ci in range(start_char, end_char + 1):
                if ci < len(sentence.characters):
                    ch = sentence.characters[ci]
                    if ch.singer_id != singer_id:
                        ch.singer_id = singer_id
                        ch.push_to_ruby()
                        changed = True

            if start_char == 0 and end_char >= len(sentence.chars) - 1:
                if sentence.singer_id != singer_id:
                    sentence.singer_id = singer_id
                    changed = True

            if not changed:
                return None
            return line_idx, start_char, None, "lyrics"

        ok = self._execute_structural_edit("划选设置演唱者", _mutate)

        if ok:
            InfoBar.success(
                title=self.tr("演唱者已更新"),
                content=self.tr("已将第 {line} 行第 {start}~{end} 字的演唱者更改").format(
                    line=line_idx + 1, start=start_char + 1, end=end_char + 1),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )

    def load_audio(self, file_path: str) -> bool:
        """异步加载音频到引擎。

        引擎 load() 现在包含整轨解码 + TSM 源 MP3 编码 + 预渲染派发等重操作，
        必须放到后台线程，否则会卡死 UI。UI 更新在 finished 回调里完成。
        """
        if not self._timing_service:
            return False

        # 防重入：正在加载时忽略新请求
        if getattr(self, "_audio_loading", False):
            return False
        self._audio_loading = True
        # 提前置位，配合 MainWindow._on_data_changed 的幂等守卫，避免
        # store.set_audio_path → emit("audio") → load_audio 的重入回环。
        self._audio_file_path = file_path

        # 状态提示
        self._audio_state_tooltip = StateToolTip(self.tr("正在加载音频"), self.tr("正在读取音频文件..."), self)
        green = theme.status_complete.name()
        self._audio_state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        self._audio_state_tooltip.move(self._audio_state_tooltip.getSuitablePos())
        self._audio_state_tooltip.show()

        # 后台线程加载
        from strange_uta_game.frontend.workers import AudioLoadWorker

        engine = self._timing_service._audio_engine
        self._audio_load_thread = QThread(self)
        self._audio_load_worker = AudioLoadWorker(engine, file_path)
        self._audio_load_worker.moveToThread(self._audio_load_thread)

        self._audio_load_thread.started.connect(self._audio_load_worker.run)
        self._audio_load_worker.progress.connect(self._on_audio_load_progress)
        self._audio_load_worker.finished.connect(lambda: self._on_audio_loaded(file_path))
        self._audio_load_worker.error.connect(self._on_audio_load_error)
        self._audio_load_worker.finished.connect(self._cleanup_audio_load_thread)
        self._audio_load_worker.error.connect(self._cleanup_audio_load_thread)

        self._audio_load_thread.start()
        return True

    def _on_audio_load_progress(self, stage: str, value: float) -> None:
        if getattr(self, "_audio_state_tooltip", None):
            self._audio_state_tooltip.setContent(stage)

    def _sync_project_audio_duration(
        self,
        duration_ms: int,
        *,
        mark_dirty: bool = True,
    ) -> bool:
        """将音频引擎探测到的真实时长同步到当前项目。

        所有受支持的音频格式最终都经过音频引擎；视频也会先提取音轨再由
        同一引擎加载。因此这里是格式无关的统一同步点。
        """
        if not self._project:
            return False
        duration_ms = max(0, int(duration_ms or 0))
        if duration_ms <= 0 or self._project.audio_duration_ms == duration_ms:
            return False
        self._project.audio_duration_ms = duration_ms
        if mark_dirty and getattr(self, "_store", None):
            self._store.mark_dirty()
        return True

    def _on_audio_loaded(self, file_path: str) -> None:
        """音频后台加载完成（UI 线程）：刷新时长/波形/默认音量速度。"""
        if getattr(self, "_audio_state_tooltip", None):
            self._audio_state_tooltip.setState(True)
            self._audio_state_tooltip.setContent(self.tr("加载完成"))
            self._audio_state_tooltip.close()
            self._audio_state_tooltip = None

        info = self._timing_service.get_audio_info() if self._timing_service else None
        if info:
            self._sync_project_audio_duration(info.duration_ms)
            self.transport.set_duration(info.duration_ms)
            self.timeline.set_duration(info.duration_ms)
            self.preview.set_duration(info.duration_ms)
            self._last_polled_duration_ms = info.duration_ms
            self.transport.set_position(0)
            self.timeline.set_position(0)

            samples = self._timing_service.get_original_samples()
            if samples is not None:
                # mono 来自引擎加载线程的预混（P1-1）：UI 线程不再降混立体声
                self.timeline.set_audio_data(
                    samples,
                    info.sample_rate,
                    info.channels,
                    mono=self._timing_service.get_mono_samples(),
                )

        self._audio_file_path = file_path
        self.timeline.set_audio_name(Path(file_path).name)

        # 应用设置中的默认音量和速度
        if self._timing_service:
            setting_iface = self._get_setting_interface()
            if setting_iface is not None:
                settings = setting_iface.get_settings()
                default_volume = int(settings.get("audio.default_volume", 80))
                self.transport.slider_volume.setValue(default_volume)
                self.transport.set_default_volume(default_volume)
                speed_min = settings.get("audio.speed_slider_min", 0.5)
                speed_max = settings.get("audio.speed_slider_max", 1.0)
                self.transport.set_speed_range(
                    speed_min,
                    speed_max,
                    emit_signal=False,
                )
                default_speed = settings.get("audio.default_speed", 1.0)
                speed_pct = self.transport.set_speed_value(
                    int(default_speed * 100), emit_signal=False
                )
                self.transport.set_default_speed(speed_pct)
                self._timing_service.set_speed(speed_pct / 100.0)
                # 用实际滑块范围重新触发预渲染，过滤掉用户不会用到的速度档。
                # 已渲染/已入队的速度幂等跳过，无重复开销。
                self._timing_service.prewarm_speeds(
                    speed_min=speed_min,
                    speed_max=speed_max,
                )

        # 与 Home 页加载音频的动作对称：广播 audio 变更，使导出页等订阅者同步
        # 先设 original_media_path（可能标 dirty），再 emit "audio"，
        # 确保 _update_title() 读到正确的 dirty 状态。
        if hasattr(self, "_store") and self._store:
            self._store.set_original_media_path(file_path)
            self._store.set_audio_path(file_path)

        InfoBar.success(
            title=self.tr("音频已加载"),
            content=Path(file_path).name,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )
        # 音频加载会(重)初始化 BASS 设备，使按键音样本失效；在此重载，
        # 确保导入新歌后无需手动切换音效即有按键音。
        self._reload_keysound_after_audio()
        # 节拍音样本同理（同一 BASS 会话重建）
        self._reload_metronome_after_audio()
        self._audio_loading = False
        # 加载完成后自动识别 BPM（后台线程，结果写回 BPM 网格/节拍器）
        self._auto_detect_bpm()

    # ── 加载完成后自动 BPM 检测（task_runner 自回收；旧音源迟到结果丢弃） ──

    def _auto_detect_bpm(self) -> None:
        """音频加载完成后后台自动检测 BPM。

        复用引擎加载线程预混的单声道样本（get_mono_samples）；检测走
        BpmDetectWorker，不阻塞 UI。换歌/重载时取消旧任务，is_current
        按身份过滤迟到结果。仅当齿轮弹窗「加载后自动检测」开关
        （timing.waveform_auto_bpm，默认关）开启时执行。
        """
        setting_iface = self._get_setting_interface()
        if setting_iface is not None and not bool(
            setting_iface.get_settings().get("timing.waveform_auto_bpm", False)
        ):
            return
        if self._timing_service is None:
            return
        mono = self._timing_service.get_mono_samples()
        info = self._timing_service.get_audio_info()
        if (
            mono is None
            or not hasattr(mono, "__len__")
            or len(mono) == 0
            or info is None
            or info.sample_rate <= 0
        ):
            return

        old = getattr(self, "_bpm_detect_worker", None)
        if old is not None:
            old.request_cancel()
        from strange_uta_game.frontend.workers import BpmDetectWorker
        from strange_uta_game.frontend.editor.timing import task_runner

        worker = BpmDetectWorker(mono, int(info.sample_rate))
        self._bpm_detect_worker = worker
        task_runner.start_task(
            self,
            worker,
            on_finished=self._on_auto_bpm_result,
            on_error=lambda _msg: None,  # 静默失败：自动检测非关键路径
            is_current=lambda w: w is getattr(self, "_bpm_detect_worker", None),
        )

    def _on_auto_bpm_result(self, result: dict) -> None:
        """自动检测完成：置信度足够则写回 BPM 网格/节拍器并持久化，
        同时把 BPM 网格偏移一键对齐到首音（b=0 落在歌声起点）。"""
        self._bpm_detect_worker = None
        if not isinstance(result, dict):
            return
        bpm = result.get("bpm")
        confidence = float(result.get("confidence", 0.0))
        if bpm is None or confidence < 0.05 or not 10.0 <= float(bpm) <= 600.0:
            return
        bpm = float(bpm)

        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            s = setting_iface.get_settings()
            if float(s.get("timing.waveform_grid_bpm", 120.0)) != bpm:
                s.set("timing.waveform_grid_bpm", bpm)
                s.save()
        if hasattr(self, "timeline"):
            self.timeline.set_grid_bpm(bpm)
        # 节拍器 BPM 复用同一设置键，重读配置使其立即生效
        self._configure_metronome_from_settings()

        # 自动对齐首音：偏移 = 开头静音结束、首个有声信号的位置（整曲
        # 毫秒级 numpy 计算，无需后台线程）。写回偏移设置并刷新网格。
        offset_msg = ""
        mono = (
            self._timing_service.get_mono_samples()
            if self._timing_service
            else None
        )
        info = (
            self._timing_service.get_audio_info() if self._timing_service else None
        )
        if (
            mono is not None
            and hasattr(mono, "__len__")
            and len(mono) > 0
            and info is not None
            and info.sample_rate > 0
        ):
            from strange_uta_game.backend.infrastructure.audio.spectrum import (
                first_sound_ms,
            )

            ms = first_sound_ms(mono, int(info.sample_rate))
            if ms is not None:
                offset_ms = int(round(max(-600000, min(600000, ms))))
                if setting_iface is not None:
                    s = setting_iface.get_settings()
                    if int(s.get("timing.waveform_grid_offset_ms", 0)) != offset_ms:
                        s.set("timing.waveform_grid_offset_ms", offset_ms)
                        s.save()
                if hasattr(self, "timeline"):
                    self.timeline.set_grid_offset(offset_ms)
                self._configure_metronome_from_settings()
                offset_msg = f' · {self.tr("偏移已对齐首音")}{offset_ms}ms'

        level = self.tr("高") if confidence > 0.66 else (
            self.tr("中") if confidence > 0.33 else self.tr("低")
        )
        InfoBar.success(
            title=self.tr("BPM 自动识别"),
            content=f'{bpm:g} BPM · {self.tr("置信度")}{level}（{confidence:.0%}）{offset_msg}',
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _on_audio_load_error(self, error_msg: str) -> None:
        if getattr(self, "_audio_state_tooltip", None):
            self._audio_state_tooltip.close()
            self._audio_state_tooltip = None
        # 加载失败，复位以允许重试
        self._audio_file_path = None
        self._audio_loading = False
        InfoBar.error(
            title=self.tr("加载失败"),
            content=error_msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _cleanup_audio_load_thread(self) -> None:
        thread = getattr(self, "_audio_load_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait()
            self._audio_load_thread = None
        worker = getattr(self, "_audio_load_worker", None)
        if worker is not None:
            worker.deleteLater()
            self._audio_load_worker = None

    def _update_mode_indicator(self, playing: Optional[bool] = None):
        """#8：根据播放状态更新左下角模式指示器与激活的 key_map。

        - 播放中 → "模式：打轴"，使用 _key_map_timing_short/long
        - 未播放 → "模式：编辑"，使用 _key_map_edit_short/long
        同步刷新底部快捷键提示（因为两模式文本可能不同）。
        """
        if playing is None:
            playing = bool(self._timing_service and self._timing_service.is_playing())
        else:
            playing = bool(playing)
        self._shortcut_mode_playing = playing
        if not hasattr(self, "lbl_mode"):
            return
        if playing:
            self.lbl_mode.setText(self.tr("模式：打轴"))
            self.lbl_mode.setStyleSheet(
                "font-size: 12px; padding: 2px 8px; border-radius: 4px;"
                f"background-color: {theme.mode_timing_bg.name()};"
                f" color: {theme.mode_timing_text.name()}; font-weight: bold;"
            )
            if hasattr(self, "_key_map_timing_short"):
                self._key_map_short = self._key_map_timing_short
                self._key_map_long = self._key_map_timing_long
                self._key_map = self._key_map_timing_short
        else:
            self.lbl_mode.setText(self.tr("模式：编辑"))
            self.lbl_mode.setStyleSheet(
                "font-size: 12px; padding: 2px 8px; border-radius: 4px;"
                f"background-color: {theme.mode_edit_bg.name()};"
                f" color: {theme.mode_edit_text.name()};"
            )
            if hasattr(self, "_key_map_edit_short"):
                self._key_map_short = self._key_map_edit_short
                self._key_map_long = self._key_map_edit_long
                self._key_map = self._key_map_edit_short
        # 刷新快捷键提示（按新模式取文本）
        if hasattr(self, "_shortcut_actions_timing"):
            self._update_shortcut_hint(
                self._shortcut_actions_timing,
                getattr(self, "_shortcut_actions_edit", None),
            )

    def sync_playback_mode(self) -> None:
        """按音频服务的实际状态重新同步播放模式与活动快捷键表。

        宿主隐藏整个嵌入窗口时，SUG 自己的页面切换和位置回调可能都不会
        执行。这个入口让嵌入生命周期可以主动修复播放状态与快捷键模式的
        偏差，而不需要在每次按键时临时判断。
        """
        self._update_mode_indicator()

    # ==================== 播放控制 ====================

    def _apply_playback_range(
        self, start_ms: Optional[int], end_ms: Optional[int]
    ) -> None:
        """Apply transient playback bounds and refresh both range displays."""
        self._playback_range_start_ms = start_ms
        self._playback_range_end_ms = end_ms
        self.transport.set_playback_range(start_ms, end_ms)
        self.timeline.set_playback_range(start_ms, end_ms)
        # 锁定终点决定后台轮询是否保持高频（音频终点检测），区间变化
        # （含 undo/redo，都走本方法）后立即重估
        self._refresh_position_poll_interval()

    def _reset_playback_range(self) -> None:
        self._apply_playback_range(None, None)

    def _execute_playback_range_change(
        self,
        start_ms: Optional[int],
        end_ms: Optional[int],
        description: str,
    ) -> None:
        old_state = (
            self._playback_range_start_ms,
            self._playback_range_end_ms,
        )
        new_state = (start_ms, end_ms)
        if old_state == new_state:
            return
        command = PlaybackRangeCommand(
            self._apply_playback_range, old_state, new_state, description
        )
        manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if manager is not None:
            manager.execute(command)
        else:
            command.execute()

    def _toggle_playback_range_start(self) -> None:
        if not self._timing_service:
            return
        if self._playback_range_start_ms is not None:
            self._execute_playback_range_change(
                None,
                self._playback_range_end_ms,
                self.tr("取消播放区间起点"),
            )
            return
        position_ms = self._timing_service.get_position_ms()
        if (
            self._playback_range_end_ms is not None
            and position_ms >= self._playback_range_end_ms
        ):
            self._show_invalid_playback_range()
            return
        self._execute_playback_range_change(
            position_ms,
            self._playback_range_end_ms,
            self.tr("锁定播放区间起点"),
        )

    def _toggle_playback_range_end(self) -> None:
        if not self._timing_service:
            return
        if self._playback_range_end_ms is not None:
            self._execute_playback_range_change(
                self._playback_range_start_ms,
                None,
                self.tr("取消播放区间终点"),
            )
            return
        position_ms = self._timing_service.get_position_ms()
        if (
            self._playback_range_start_ms is not None
            and position_ms <= self._playback_range_start_ms
        ):
            self._show_invalid_playback_range()
            return
        self._execute_playback_range_change(
            self._playback_range_start_ms,
            position_ms,
            self.tr("锁定播放区间终点"),
        )

    def _show_invalid_playback_range(self) -> None:
        InfoBar.warning(
            title=self.tr("无法锁定播放区间"),
            content=self.tr("终点必须晚于起点"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def _on_play(self):
        if self._timing_service:
            try:
                # 播放完毕或当前位置在锁定区间之外时，从有效区间起点播放。
                # 此时 _last_reported_ms == _duration_ms，get_position_ms 也返回 duration。
                # 需要先 seek(0) 重置 _last_reported_ms 和 BASS 位置，再 play()；
                # 否则 get_display_position_ms 的单调性保护会让位置卡在 duration，
                # 导致所有控件一直显示末尾、不滚动，直到用户手动 seek 才解除。
                if not self._timing_service.is_playing():
                    dur = self._timing_service.get_duration_ms()
                    pos = self._timing_service.get_position_ms()
                    start = self._playback_range_start_ms
                    end = self._playback_range_end_ms
                    outside_range = (
                        (start is not None and pos < start)
                        or (end is not None and pos >= end)
                    )
                    if dur > 0 and (pos >= dur or outside_range):
                        target = start if start is not None else 0
                        self._timing_service.seek(target)
                        self.transport.set_position(target)
                        self.timeline.set_position(target)
                        self.preview.set_current_time_ms(target)
                self._timing_service.play()
                self.transport.set_playing(True)
                self.preview.set_playing(True)
                self.timeline.set_playing(True)
                self._status_state = "playing"
                self.lbl_status.setText(self.tr("播放中"))
                self._update_mode_indicator(True)
                self.preview._last_auto_scroll_line_idx = -1
                # 无论鼠标点击还是键盘快捷键触发播放，都无条件恢复自动滚动
                self._auto_scroll_suspended = False
                self._auto_scroll_new_line_reached = False
                self._auto_scroll_cooldown_timer.stop()
                self.preview._auto_scroll_suspended = False
                # 启动位置主动拉取定时器
                self._position_poll_timer.start()
                # 节拍器随播放启动（开关/参数在齿轮弹窗「网格与节拍」）；
                # 播放是节拍发声的关键动作，样本失效在此自愈重载。
                # getattr 防御：最小化假对象测试不携带节拍器属性（同
                # _timetags_dirty_while_hidden 的防御访问先例）
                metronome = getattr(self, "_metronome", None)
                if getattr(self, "_metronome_enabled", False) and metronome is not None:
                    self._ensure_metronome_samples()
                    metronome.start()
            except Exception as e:
                self._show_runtime_error(str(e))

    def _on_pause(self):
        if self._timing_service:
            self._timing_service.pause()
            self.transport.set_playing(False)
            self.preview.set_playing(False)
            self.timeline.set_playing(False)
            self._status_state = "paused"
            self.lbl_status.setText(self.tr("已暂停"))
            self._update_mode_indicator(False)
            # 重置自动滚动状态
            self._auto_scroll_suspended = False
            self._auto_scroll_new_line_reached = False
            self._auto_scroll_cooldown_timer.stop()
            # 节拍器随暂停停止（调度线程自退，不阻塞 UI）
            metronome = getattr(self, "_metronome", None)
            if metronome is not None:
                metronome.stop()
            # 停止位置拉取定时器
            self._position_poll_timer.stop()
            # A7：清掉仍按住的打轴键，悬挂键会吞掉下次播放同键的 press
            self._pressed_keys.clear()
            # 切换到编辑模式时校验所有行时间戳
            self._validate_all_timestamps()

    def _on_stop(self):
        if self._timing_service:
            self._timing_service.stop()
            self.transport.set_playing(False)
            self.preview.set_playing(False)
            self.timeline.set_playing(False)
            self.transport.set_position(0)
            self.timeline.set_position(0)
            self._status_state = "stopped"
            self.lbl_status.setText(self.tr("已停止"))
            self._update_mode_indicator(False)
            # 重置自动滚动状态
            self._auto_scroll_suspended = False
            self._auto_scroll_new_line_reached = False
            self._auto_scroll_cooldown_timer.stop()
            # 节拍器随停止停响
            metronome = getattr(self, "_metronome", None)
            if metronome is not None:
                metronome.stop()
            # 停止位置拉取定时器
            self._position_poll_timer.stop()
            # A7：清掉仍按住的打轴键，悬挂键会吞掉下次播放同键的 press
            self._pressed_keys.clear()
            # 切换到编辑模式时校验所有行时间戳
            self._validate_all_timestamps()

    def _clamp_seek_to_playback_range(self, ms: int) -> int:
        """锁定播放区间时把 seek 目标钳制到 [起点, 终点] 内。

        点击波形窗/进度条上 A 之前的位置 → 落到区间起点（以 A 为起始点播放）；
        点击 B 之后的位置 → 落到区间终点（播放中由位置轮询触发「已到达锁定
        终点」暂停，停留在 B）。未锁定时不限制。
        """
        if self._playback_range_start_ms is not None:
            ms = max(self._playback_range_start_ms, ms)
        if self._playback_range_end_ms is not None:
            ms = min(self._playback_range_end_ms, ms)
        return ms

    @log_slow_method("editor.seek", 20, lambda self, args, kwargs: {"target_ms": args[0] if args else kwargs.get("ms")})
    def _on_seek(self, ms: int):
        log_perf_event("editor.seek.start", target_ms=ms, line=getattr(self, "_current_line_idx", -1))
        self._suspend_auto_scroll()
        if self._timing_service:
            ms = self._clamp_seek_to_playback_range(ms)
            self._timing_service.seek(ms)
            self.transport.set_position(ms)
            self.timeline.set_position(ms)
            self.preview.set_current_time_ms(ms)
            # 播放中 seek：节拍器按新位置重新对齐（不补响）
            metronome = getattr(self, "_metronome", None)
            if metronome is not None:
                metronome.resync()
        log_perf_event("editor.seek.end", target_ms=ms, line=getattr(self, "_current_line_idx", -1))

    def _on_speed_changed(self, speed: float):
        if self._timing_service:
            self._timing_service.set_speed(speed)

    def _on_volume_changed(self, vol: int):
        if self._timing_service:
            self._timing_service.set_volume(vol)

    def _on_waveform_visibility_changed(self, visible: bool):
        self.timeline.updateGeometry()
        self.preview.updateGeometry()
        self.updateGeometry()
        # 隐藏期间累积的 timetag 变更在重新显示时补刷一次
        if visible and getattr(self, "_timetags_dirty_while_hidden", False):
            self._timetags_dirty_while_hidden = False
            self._update_time_tags_display()
        # 显示开关影响声谱模式下的预览让位
        if hasattr(self, "preview"):
            self._apply_preview_spectrum_yield()
        # 开关状态持久化（齿轮高级设置之外的既有开关）；值未变时不写盘
        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            s = setting_iface.get_settings()
            if bool(s.get("timing.waveform_visible", True)) != bool(visible):
                s.set("timing.waveform_visible", bool(visible))
                s.save()

    def _on_timeline_display_settings_changed(self, settings: dict):
        """齿轮对话框的显示设置变化 → 持久化到 timing.* 键。"""
        # 模式/期望高度变化 → 立即重新协商预览让位（布局协商不依赖设置接口，
        # 无 settingInterface 的嵌入/测试环境同样生效）
        self._apply_preview_spectrum_yield()
        setting_iface = self._get_setting_interface()
        if setting_iface is None:
            return
        s = setting_iface.get_settings()
        s.set("timing.waveform_display_mode", settings.get("display_mode", "waveform"))
        s.set("timing.waveform_grid_mode", settings.get("grid_mode", "time"))
        s.set("timing.waveform_grid_bpm", float(settings.get("grid_bpm", 120.0)))
        s.set(
            "timing.waveform_grid_offset_ms",
            int(settings.get("grid_offset_ms", 0)),
        )
        s.set(
            "timing.waveform_beats_per_bar",
            int(settings.get("beats_per_bar", 4)),
        )
        s.set(
            "timing.waveform_auto_bpm",
            bool(settings.get("auto_bpm_on_load", False)),
        )
        s.set(
            "timing.waveform_grid_line_width",
            int(settings.get("grid_line_width", 2)),
        )
        s.set(
            "timing.spectrum_overlap",
            float(settings.get("spectrum_overlap", 0.75)),
        )
        s.set("timing.spectrum_fft_size", int(settings.get("spectrum_fft_size", 8192)))
        s.set("timing.spectrum_freq_scale", settings.get("spectrum_freq_scale", "log"))
        s.set("timing.spectrum_colormap", settings.get("spectrum_colormap", "inferno"))
        s.set(
            "timing.spectrum_dyn_range_db",
            int(settings.get("spectrum_dyn_range_db", 60)),
        )
        s.set(
            "timing.spectrum_freq_min_hz",
            int(settings.get("spectrum_freq_min_hz", 0)),
        )
        s.set(
            "timing.spectrum_freq_max_hz",
            int(settings.get("spectrum_freq_max_hz", 0)),
        )
        s.set(
            "timing.waveform_display_height",
            int(settings.get("waveform_display_height", 120)),
        )
        s.set(
            "timing.spectrum_display_height",
            int(settings.get("spectrum_display_height", 120)),
        )
        s.set(
            "timing.waveform_rms_enabled",
            bool(settings.get("waveform_rms_enabled", True)),
        )
        # 时间标签四开关：与设置页「波形时间标签」组共用同一组键（两处联动）
        s.set(
            "timing.waveform_tag_edit_enabled",
            bool(settings.get("tag_edit_enabled", True)),
        )
        s.set(
            "timing.waveform_center_playhead_enabled",
            bool(settings.get("center_playhead_enabled", False)),
        )
        s.set(
            "timing.waveform_tag_char_enabled",
            bool(settings.get("tag_char_enabled", True)),
        )
        s.set(
            "timing.waveform_tag_ruby_enabled",
            bool(settings.get("tag_ruby_enabled", True)),
        )
        # 节拍器：开关 + 音量（BPM/偏移复用 waveform_grid_* 两键）
        s.set(
            "timing.waveform_metronome_enabled",
            bool(settings.get("metronome_enabled", False)),
        )
        s.set(
            "timing.waveform_metronome_volume",
            int(settings.get("metronome_volume", 100)),
        )
        s.save()
        # 即时生效：播放中开关/改 BPM/偏移/音量都立即反映到调度器
        self._configure_metronome_from_settings()

    def _toggle_waveform_spectrum(self) -> None:
        """快捷键：波形图 → 声谱图 → 双谱 → 波形图 三模式循环，走既有持久化链。"""
        if not hasattr(self, "timeline"):
            return
        settings = dict(self.timeline.display_settings())
        current = settings.get("display_mode", "waveform")
        settings["display_mode"] = _DISPLAY_MODE_CYCLE.get(current, "spectrum")
        self.timeline._apply_display_settings(settings)

    # ── 声谱高度协商 / 应用可见性 ──

    # 声谱模式下歌词预览让位后的最低可操作高度（KaraokePreview 默认
    # minimumHeight=400，不让位则窗口内放不下声谱期望高度）。
    _SPECTRUM_PREVIEW_YIELD_H = 160

    def _apply_preview_spectrum_yield(self) -> None:
        """按显示模式让歌词预览可缩到低位（同步 minimumHeight），隐藏时恢复。

        只调 maximumHeight 无法突破 preview 既有的 minimumHeight=400——
        必须同步下调 min，父布局才能把空间分给时间轴（Qt 原生协商）。
        波形模式下若窗口空间不足（总 min > 窗口高）同样让位，防止顶层
        窗口被 min 撑大（P1：曾实测请求 713px 得到 855px）。

        双谱模式独享更深的让位：时间轴上下两条 lane 占用约两倍纵向空间，
        **仅该模式**允许把预览压缩到只剩 1 句高度（minimumHeight = 单行
        行高，可见行数下限同步放宽到 1）；其余模式保持 160 / 3 行下限。
        """
        if not hasattr(self, "timeline") or not hasattr(self, "preview"):
            return
        # 波形/声谱可见时预览恒让位到低位——显示高度经 minimumHeight 领取
        # 空间，预览的 400px 固有 min 会阻止空间分配并撑大窗口
        if not self.timeline.is_waveform_visible():
            self.preview.set_min_visible_lines(3)
            if self.preview.minimumHeight() != self._preview_default_min_h:
                self.preview.setMinimumHeight(self._preview_default_min_h)
            return
        if self.timeline.display_mode() == "dual":
            self.preview.set_min_visible_lines(1)
            target = max(40, int(self.preview.single_line_height()))
        else:
            self.preview.set_min_visible_lines(3)
            target = self._SPECTRUM_PREVIEW_YIELD_H
        if self.preview.minimumHeight() != target:
            self.preview.setMinimumHeight(target)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 显示后确保预览让位（显示高度经 min 领取空间需要预览配合）
        if hasattr(self, "timeline") and hasattr(self, "preview"):
            self._apply_preview_spectrum_yield()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    def _on_ui_visibility_for_spectrum(self) -> None:
        """应用/窗口可见性变化 → 暂停或恢复在途声谱计算。"""
        if not hasattr(self, "timeline"):
            return
        self.timeline.set_app_visible(not self._position_poll_hidden())

    # ==================== 打轴 ====================

    def _on_tag_now(self):
        """打轴按钮点击：与键盘打轴按键（tag_now）完全同源的打轴入口。

        旧实现直连 on_timing_key_pressed/released，服务 shim 内的裸
        engine.play() 会绕过 _on_play：音频在响而界面仍停留在编辑模式
        （不切打轴模式、无位置轮询/时间同步、无按键音），属遗留 BUG。

        现与键盘 tag_now 路径逐项对齐：
        1. 未播放 → 先走 _on_play() 正常启播（锁定区间回退 seek、
           「模式：打轴」切换、位置轮询（时间同步）、节拍器、自动滚动
           全部开启）；启播失败（如未加载音频）则不打点；
        2. 按键音路由同键盘：普通 cp 在 press 响、停顿点 cp 在 release 响；
        3. 时间戳同源同宿：queue_delay_ms 以 time.monotonic() 补偿
           handler 入口到读位置的耗时，经 on_timing_key_pressed/released
           写入当前 checkpoint——与键盘打轴键拿同一个位置、写同一个位置。
        """
        if not self._timing_service:
            return

        # A4：注音分析完成后会整体替换 sentences，分析期间打轴会被静默回滚
        if self._reject_during_ruby_analysis():
            return

        handler_entry_s = time.monotonic()
        keysound = getattr(self, "_keysound_player", None)
        key_name = self._tag_now_button_key_name()
        try:
            if not self._timing_service.is_playing():
                self._on_play()
                if not self._timing_service.is_playing():
                    # 启播失败：错误提示已由 _on_play 弹出，不再打点
                    return
            # press 语义：普通 cp 写时间戳并推进（停顿点 cp 由服务层过滤）
            if keysound is not None and not self._timing_service.is_current_cp_sentence_end_tail():
                keysound.play_press()
            queue_delay_ms = self._clamp_queue_delay(
                max(0, int((time.monotonic() - handler_entry_s) * 1000))
            )
            self._timing_service.on_timing_key_pressed(key_name, queue_delay_ms)
            # release 语义：停顿点 cp 在"抬起"时写入（模拟一次完整敲击）
            if keysound is not None and self._timing_service.is_current_cp_sentence_end_tail():
                keysound.play_release()
            queue_delay_ms = self._clamp_queue_delay(
                max(0, int((time.monotonic() - handler_entry_s) * 1000))
            )
            self._timing_service.on_timing_key_released(key_name, queue_delay_ms)
        except Exception as e:
            self._show_runtime_error(str(e))

    def _tag_now_button_key_name(self) -> str:
        """打轴按钮对应的按键名：打轴模式 tag_now 首个绑定键（与按钮文案一致）。"""
        actions = getattr(self, "_shortcut_actions_timing", None) or {}
        raw = actions.get("tag_now", "") or ""
        first = raw.split(",")[0].split(":")[0].strip()
        return first or "SPACE"

    def _on_clear_current_line_tags(self):
        if not self._timing_service:
            return

        self._timing_service.clear_timetags_for_current_line()
        self._update_time_tags_display()
        self._update_status()
        self.preview._update_display()

    def _on_line_clicked(self, idx: int):
        # 切换行前，校验上一行的时间戳
        if self._project and 0 <= self._current_line_idx < len(self._project.sentences):
            self._validate_line_timestamps(self._current_line_idx)
        self._current_line_idx = idx
        self._update_line_info()

    def _validate_line_timestamps(self, line_idx: int) -> None:
        """校验指定行的所有字符时间戳，确保不超过允许的数量。

        规则：
        - 每个字符允许的时间戳数量 = check_count + (1 if is_sentence_end else 0)
        - timestamps 列表长度不允许超过 check_count
        - 如果有冗余时间戳，截断并推送至 ruby；A9：截断会改写（导入）轴，
          经 SentenceSnapshotCommand 入撤销栈，用户可以撤销这次自动截断，
          不再是静默丢失。
        """
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        before_sentences = None
        for ch in sentence.characters:
            max_timestamps = ch.check_count
            if len(ch.timestamps) > max_timestamps:
                # 快照须在第一次截断前拍，只对确实发生截断的行入栈
                if before_sentences is None:
                    before_sentences = deepcopy(self._project.sentences)
                ch.timestamps = ch.timestamps[:max_timestamps]
                ch._update_offset_timestamps()
                ch.push_to_ruby()
        if before_sentences is None:
            return
        command_manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if command_manager is not None:
            undo_pos = (self._current_line_idx, self.preview._current_char_idx)
            cmd = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                self._project.sentences,  # execute() 内会自行 deepcopy
                self.tr("校验时间戳（截断超长 timestamps）"),
            )
            cmd.undo_position = undo_pos
            cmd.redo_position = undo_pos
            command_manager.execute(cmd)
            # 句子列表被整体替换，刷新预览与波形时间标签
            self.refresh_lyric_display()
            self._update_time_tags_display()
            if hasattr(self, "_store") and self._store:
                self._store.notify("timetags")

    def _validate_all_timestamps(self) -> None:
        """校验项目中所有行的时间戳（切换到编辑模式时调用）"""
        if not self._project:
            return
        for line_idx in range(len(self._project.sentences)):
            self._validate_line_timestamps(line_idx)

    def _resolve_target_char(self) -> tuple[int, int]:
        """解析字符级操作的目标 (line_idx, char_idx)。

        双域设计：
        - focus 域 (`preview._focus_*`)：用户视觉/操作真理，由点击/拖选/纯←→/打轴驱动，
          不被 cp 自动跳跃污染。字符级操作的优先来源。
        - current 域 (`self._current_line_idx` + `preview._current_char_idx`)：
          后台 TimingService 反馈的合法 cp 位置，会被 cp 跳跃污染。打轴模式
          (TimingService.is_playing()) 下字符级操作目标 — 因为打轴时 TimingService
          自动推进，focus 是用户上次点的位置，可能不是当前正在打的字符。

        Returns:
            (line_idx, char_idx)：目标字符。无 focus 时回退 current；
            两域都无效时返回 (-1, -1)。
        """
        # focus 域优先（line + char 一起取，避免 cp 跳跃后
        # _current_line_idx 与 _focus_line_idx 不一致导致目标错位）
        if (
            self.preview._focus_line_idx >= 0
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            line_idx = self.preview._focus_line_idx
            char_idx = min(
                self.preview._focus_char_idx,
                self.preview._focus_char_range_end,
            )
            return line_idx, char_idx
        # focus 无效：回退 current
        return self._current_line_idx, self.preview._current_char_idx

    def _on_checkpoint_clicked(self, line_idx: int, char_idx: int, cp_idx: int):
        """点击 checkpoint 标记：仅切换 selected_cp 与音频跳转，不移动光标。

        selected_cp（Character.selected_checkpoint_idx + preview._current_checkpoint_idx）
        与 selected_char（preview._current_char_idx + _focus_*）是两个独立状态：
        - 点击 cp 标记 → 仅 selected_cp 改变；selected_char（光标）保持
        - 点击字符文本 / 方向键 → selected_char（光标）改变
        - F4/F5/F6/Alt+←→ 等编辑/循环操作 → 作用于 selected_char

        通过临时设置 _suppress_cp_cursor_move 阻止
        _apply_checkpoint_position 调用 set_current_position。
        """
        if not self._timing_service:
            return
        self._suppress_cp_cursor_move = True
        try:
            self._timing_service.move_to_checkpoint(line_idx, char_idx, cp_idx)
        finally:
            self._suppress_cp_cursor_move = False
        # 同步 focus 和 current 字符到 cp 对应的字符
        self.preview.set_current_position(line_idx, char_idx)
        self.preview.set_focus_position(line_idx, char_idx)
        self._update_line_info()

    def _sync_preview_to_handle(self, line_idx: int, char_idx: int, cp_idx: int):
        """把 preview 选中（current + focus）同步到指定 checkpoint 对应的字符。

        与 ``_on_checkpoint_clicked`` 同义：移动打轴位置但不污染光标域
        （_suppress_cp_cursor_move），再把 preview 选中落到该字符。供波形时间标签
        单击选中、拖拽提交后同步使用。
        """
        if not self._timing_service:
            return
        self._suppress_cp_cursor_move = True
        try:
            self._timing_service.move_to_checkpoint(line_idx, char_idx, cp_idx)
        finally:
            self._suppress_cp_cursor_move = False
        self.preview.set_current_position(line_idx, char_idx)
        self.preview.set_focus_position(line_idx, char_idx)
        self._update_line_info()

    def _on_timeline_tag_clicked(self, line_idx: int, char_idx: int, cp_idx: int, is_sentence_end: bool):
        """单击波形时间标签把手：同步 preview 选中到该字符（seek 由 seek_requested 处理）。"""
        _ = is_sentence_end  # 停顿点 cp_idx == check_count，move_to_checkpoint 可解析
        self._sync_preview_to_handle(line_idx, char_idx, cp_idx)

    def _on_timeline_tags_drag_committed(self, handles, delta_ms: int):
        """波形时间标签拖拽提交：按字符分组写入 raw 时间戳，再做完整刷新。

        handles 为 (line, char, cp, is_sentence_end) 列表，首项为锚点（拖动锚字符）。
        delta_ms 在 raw / global 两域恒等（项目级统一偏移在差分中抵消），直接施加。
        """
        if not self._project or not handles or not delta_ms:
            return
        from collections import defaultdict
        groups = defaultdict(list)  # (line, char) -> list[(cp, is_end)]
        for line_idx, char_idx, cp_idx, is_end in handles:
            groups[(line_idx, char_idx)].append((cp_idx, is_end))

        # 拖拽可撤销：先快照，再直接写入，最后注册 SentenceSnapshotCommand（与删除时间戳同范式）
        before_sentences = deepcopy(self._project.sentences)

        for (line_idx, char_idx), items in groups.items():
            if not (0 <= line_idx < len(self._project.sentences)):
                continue
            sentence = self._project.sentences[line_idx]
            if not (0 <= char_idx < len(sentence.characters)):
                continue
            ch = sentence.characters[char_idx]
            touched_normal = False
            for cp_idx, is_end in items:
                if is_end:
                    if ch.is_sentence_end and ch.sentence_end_ts is not None:
                        # set_sentence_end_ts 内部已 _update_offset_timestamps + push_to_ruby
                        ch.set_sentence_end_ts(max(0, ch.sentence_end_ts + delta_ms))
                else:
                    if 0 <= cp_idx < len(ch.timestamps):
                        ch.timestamps[cp_idx] = max(0, ch.timestamps[cp_idx] + delta_ms)
                        touched_normal = True
            if touched_normal:
                ch._update_offset_timestamps()
                ch.push_to_ruby()

        # 注册撤销命令（after = 当前已改写的 sentences；execute 内部深拷贝接管）
        a_line, a_char, a_cp, _a_end = handles[0]
        self._register_timestamp_undo(before_sentences, a_line, a_char, self.tr("拖动时间标签"))

        # 副作用四件套（notify 不会刷新 preview / line_info，需显式调）
        self._update_time_tags_display()
        self.refresh_lyric_display()
        self._update_line_info()
        if hasattr(self, "_store") and self._store:
            self._store.notify("timetags")
        # preview 选中同步到锚点字符（handles 首项）
        self._sync_preview_to_handle(a_line, a_char, a_cp)

    def _on_char_selected(self, line_idx: int, char_idx: int):
        """点击字符选中 — 移动到该字符的第一个 checkpoint。

        若字符无 checkpoint（check_count=0 且非停顿点），保持视觉焦点在
        该字符上，方便用户通过 F4 添加节奏点；内部打轴位置仍移到最近的
        下一个有效 checkpoint，确保按空格时能正确赋时间戳。
        """
        # #9: 单一 set_current_position 入口，避免 timing_service 回调在
        # 同帧内反复覆盖 _scroll_center_line 造成空白行抖动。仅当字符无
        # checkpoint 时由本地直接居中；否则交给 _apply_checkpoint_position
        # 统一处理。
        self._current_line_idx = line_idx

        # 判断当前字符是否有 checkpoint
        no_checkpoint = True
        if self._project and line_idx < len(self._project.sentences):
            sentence = self._project.sentences[line_idx]
            if char_idx < len(sentence.characters):
                ch = sentence.characters[char_idx]
                no_checkpoint = ch.check_count == 0 and not ch.is_sentence_end

        if no_checkpoint:
            # 无 checkpoint：直接把视觉焦点定到被点击字符
            self.preview.set_current_position(line_idx, char_idx)
        else:
            # 有 checkpoint：由 timing_service 回调经 _apply_checkpoint_position
            # 统一调用 set_current_position，避免双写 _scroll_center_line
            if self._timing_service:
                self._timing_service.move_to_checkpoint(line_idx, char_idx, 0)
            else:
                self.preview.set_current_position(line_idx, char_idx)
            self._update_line_info()
            self._update_time_tags_display()
            self._update_status()
            return

        # 无 checkpoint 分支也触发 timing_service 移动（便于随后空格赋时间戳）
        # 优先向前查找最近的CP，找不到再向后找。
        # 抑制 _apply_checkpoint_position 的居中滚动：用户操作的是 focus 域，
        # 视口应留在被点击字符所在行，不跳到 cp 所在行。
        if self._timing_service:
            self._suppress_cp_cursor_move = True
            try:
                self._timing_service.move_to_checkpoint(
                    line_idx, char_idx, 0, prefer_backward=True
                )
            finally:
                self._suppress_cp_cursor_move = False
            self._current_line_idx = line_idx
            pos = self._timing_service.get_current_position()
            self.preview._current_char_idx = pos.char_idx

        self._update_line_info()

    def _on_char_edit_requested(self, line_idx: int, char_idx: int):
        """F2: show the configured compact or classic ruby editor."""
        if not self._project or not 0 <= line_idx < len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if not 0 <= char_idx < len(sentence.chars):
            return

        if self._f2_ruby_editor_mode == "classic":
            self._show_classic_char_editor(line_idx, char_idx, sentence)
            return

        before_character = deepcopy(sentence.characters[char_idx])

        def _toggle_link_from_popup() -> None:
            nonlocal before_character
            self._toggle_word_join_single(line_idx, char_idx)
            # The F3-compatible action owns its own undo command. If ruby is
            # saved afterwards, its undo snapshot must preserve this new link.
            before_character = deepcopy(
                self._project.sentences[line_idx].characters[char_idx]
            )

        dialog = RubyEditPopup(
            sentence.characters[char_idx],
            can_link_next=char_idx < len(sentence.characters) - 1,
            parent=self,
            link_toggle_callback=_toggle_link_from_popup,
        )
        anchor = self.preview.character_global_rect(line_idx, char_idx)
        if anchor is None:
            self.preview.repaint()
            anchor = self.preview.character_global_rect(line_idx, char_idx)
        if anchor is None:
            preview_top_left = self.preview.mapToGlobal(self.preview.rect().topLeft())
            anchor = QRect(preview_top_left, self.preview.rect().size())
        dialog.show_above(anchor)
        if dialog.was_modified():
            command_manager = (
                self._timing_service.command_manager
                if self._timing_service
                else None
            )
            if command_manager is not None:
                desc = f"编辑注音（第 {line_idx + 1} 句 第 {char_idx + 1} 字）"
                cmd = CharacterSnapshotCommand(
                    self._project,
                    line_idx,
                    char_idx,
                    before_character,
                    sentence.characters[char_idx],
                    desc,
                )
                command_manager.execute(cmd)

            InfoBar.success(
                title=self.tr("注音已更新"),
                content=self.tr("已应用「{char}」的注音").format(
                    char=sentence.chars[char_idx]
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=1400,
                parent=self,
            )
            QTimer.singleShot(
                0, lambda li=line_idx: self._finish_inline_ruby_edit(li)
            )

    def _show_classic_char_editor(
        self, line_idx: int, char_idx: int, sentence: Sentence
    ) -> None:
        """Open the legacy full character editor and commit its mutations."""
        before_sentences = deepcopy(self._project.sentences)

        dialog = CharEditDialog(sentence, char_idx, self)
        dialog.exec()
        if dialog.switch_to_bulk_requested():
            state = dialog.get_switch_state()
            state["normal_range"] = (
                line_idx,
                dialog._word_start,
                dialog._word_end - 1,
            )
            self._open_bulk_change_dialog(
                initial_word=state["search_word"], initial_state=state
            )
            return
        if not dialog.was_modified():
            return

        command_manager = (
            self._timing_service.command_manager
            if self._timing_service
            else None
        )
        if command_manager is not None:
            after_sentences = deepcopy(self._project.sentences)
            word_start, word_end = sentence.get_word_char_range(char_idx)
            if word_end - word_start > 1:
                desc = (
                    f"编辑连词（第 {line_idx + 1} 句 "
                    f"第 {word_start + 1}-{word_end} 字）"
                )
            else:
                desc = f"编辑字符（第 {line_idx + 1} 句 第 {char_idx + 1} 字）"
            cmd = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                desc,
            )
            cursor_pos = (self._current_line_idx, self.preview._current_char_idx)
            cmd.undo_position = cursor_pos
            cmd.redo_position = cursor_pos
            command_manager.execute(cmd)

        self._reapply_global_offset()
        if self._timing_service:
            self._timing_service.rebuild_global_checkpoints()
        self.preview._update_display()
        self._update_time_tags_display()
        self._update_status()
        if hasattr(self, "_store") and self._store:
            self._store.notify("rubies")
            self._store.notify("checkpoints")
            self._store.notify("lyrics")

    def _finish_inline_ruby_edit(self, line_idx: int) -> None:
        """Refresh only ruby consumers after the popup has visually closed."""
        if not self._project or not 0 <= line_idx < len(self._project.sentences):
            return
        self.preview._update_display()
        self._update_time_tags_display()
        if hasattr(self, "_store") and self._store:
            self._store.notify("rubies")

    def _add_checkpoint(self):
        """F4 增加当前字符节奏点 (+1)。"""
        self._change_checkpoint(delta=1)

    def _remove_checkpoint(self):
        """F5 删除当前字符节奏点 (-1)，最小为 0。"""
        self._change_checkpoint(delta=-1)

    def _adjust_current_timestamp(self, delta_ms: int):
        """Alt+↑/↓ 微调当前选中 checkpoint 的时间戳。

        批 18 #8：委托给 TimingService.adjust_current_timestamp 统一处理，
        由服务层保证 _update_offset_timestamps + push_to_ruby 双同步。
        """
        if not self._project or not self._timing_service:
            return
        if not self._timing_service.adjust_current_timestamp(delta_ms):
            return
        self._update_time_tags_display()
        self.refresh_lyric_display()
        self._update_line_info()
        if hasattr(self, "_store") and self._store:
            self._store.notify("timetags")

    def _cycle_current_checkpoint(self, direction: int = 1):
        """#2：Alt+→/Alt+← 循环切换"当前选中字符"的 checkpoint 索引。

        目标字符优先级：
        1. 若 KaraokePreview 存在有效选中范围，使用选中字符的起点
           (line = _focus_line_idx, char = min(sel_start, sel_end))。
        2. 否则回退到 TimingService.get_current_position()（播放/打轴上下文）。

        停顿点字符若带 is_sentence_end，则停顿点 checkpoint 也在循环序列内
        （位置为 check_count）。

        Args:
            direction: +1 表示下一个 checkpoint（Alt+→），-1 表示上一个（Alt+←）。
        """
        if not self._project or not self._timing_service:
            return
        # 优先用选中字符
        if (
            self.preview._focus_line_idx >= 0
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            line_idx = self.preview._focus_line_idx
            char_idx = min(self.preview._focus_char_idx, self.preview._focus_char_range_end)
            # 以 TimingService 当前 checkpoint_idx 为起点（若行/字匹配），
            # 否则从 0 起。
            pos = self._timing_service.get_current_position()
            base_idx = (
                pos.checkpoint_idx
                if (pos.line_idx == line_idx and pos.char_idx == char_idx)
                else 0
            )
        else:
            pos = self._timing_service.get_current_position()
            line_idx = pos.line_idx
            char_idx = pos.char_idx
            base_idx = pos.checkpoint_idx
        if line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx >= len(sentence.characters):
            return
        ch = sentence.characters[char_idx]
        total = ch.check_count + (1 if ch.is_sentence_end else 0)
        if total <= 0:
            return
        step = 1 if direction >= 0 else -1
        next_idx = (base_idx + step) % total
        self._timing_service.move_to_checkpoint(line_idx, char_idx, next_idx)
        self._update_line_info()
        self.refresh_lyric_display()

    def _rebuild_checkpoints(self):
        if self._timing_service:
            if hasattr(self._timing_service, "rebuild_global_checkpoints"):
                self._timing_service.rebuild_global_checkpoints()
            else:
                self._timing_service.rebuild_global_checkpoints()

    def _reapply_global_offset(self) -> None:
        """将当前全局偏移重新应用到所有字符。

        结构编辑（修改字符、插入导唱符等）会创建新的 Character 对象，
        其 _global_offset_ms 默认为 0。此方法从 preview 读取当前偏移值
        并写入所有字符，确保 global_timestamps 与渲染/导出一致。
        """
        if not self._project:
            return
        offset = self.preview._global_offset_ms
        self._project.global_offset_ms = offset
        for sentence in self._project.sentences:
            for ch in sentence.characters:
                ch.set_offset(offset)

    def _sync_after_structure_change(
        self,
        change_type: str = "lyrics",
        focus_line_idx: Optional[int] = None,
        focus_char_idx: Optional[int] = None,
        checkpoint_idx: Optional[int] = None,
        move_cp: bool = True,
    ):
        if not self._project:
            return

        self._reapply_global_offset()
        self._rebuild_checkpoints()

        total_lines = len(self._project.sentences)
        if total_lines == 0:
            self._current_line_idx = 0
            self.preview._current_line_idx = 0
            self.preview._current_char_idx = 0
            self.preview._current_checkpoint_idx = None
            self.refresh_lyric_display()
            self._update_time_tags_display()
            self._update_status()
            return

        line_idx = focus_line_idx if focus_line_idx is not None else self._current_line_idx
        line_idx = max(0, min(line_idx, total_lines - 1))
        sentence = self._project.sentences[line_idx]

        if sentence.characters:
            char_idx = focus_char_idx if focus_char_idx is not None else self.preview._current_char_idx
            char_idx = max(0, min(char_idx, len(sentence.characters) - 1))
        else:
            char_idx = 0

        self._update_selected_checkpoint(line_idx, char_idx, checkpoint_idx)
        self.preview.set_current_position(line_idx, char_idx)
        self.preview.set_focus_position(line_idx, char_idx)
        self._current_line_idx = line_idx

        if move_cp and self._timing_service and sentence.characters:
            target_cp = checkpoint_idx if checkpoint_idx is not None else 0
            self._timing_service.move_to_checkpoint(line_idx, char_idx, target_cp, prefer_backward=True)

        self.refresh_lyric_display()
        self._update_time_tags_display()
        self._update_status()
        if hasattr(self, "_store") and self._store:
            self._store.notify(change_type)

    def _ruby_analysis_in_progress(self) -> bool:
        """注音分析（含按类型删除注音等异步任务）是否进行中。

        五个异步分析站点均以 ``_ruby_analyzing`` / ``_ruby_subset_analyzing``
        作为忙碌标志（见 _on_delete_rubies_by_type 等），此处与既有互斥机制
        保持同一判定。
        """
        return bool(
            getattr(self, "_ruby_analyzing", False)
            or getattr(self, "_ruby_subset_analyzing", False)
        )

    def _reject_during_ruby_analysis(self) -> bool:
        """A4：注音分析期间拒绝打轴/结构编辑输入。

        异步分析完成后会以 SentenceSnapshotCommand 整体替换
        ``project.sentences``，分析期间写入的打轴/编辑会被静默回滚；
        这里复用既有忙碌/互斥机制提前拒绝并提示，返回 True 表示已拒绝。
        """
        if not self._ruby_analysis_in_progress():
            return False
        InfoBar.warning(
            title=self.tr("注音分析进行中"),
            content=self.tr("请等待当前注音分析完成后再试"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )
        return True

    def _execute_structural_edit(
        self,
        description: str,
        mutator: Callable[[], Optional[tuple[int, int, Optional[int], str]]],
        move_cp: bool = True,
    ) -> bool:
        if not self._project:
            return False

        # A4：分析完成后整体替换 sentences，期间的结构编辑会被回滚
        if self._reject_during_ruby_analysis():
            return False

        undo_pos = (self._current_line_idx, self.preview._current_char_idx)

        before_sentences = deepcopy(self._project.sentences)
        result = mutator()
        if result is None:
            return False

        after_sentences = deepcopy(self._project.sentences)
        command_manager = None
        if self._timing_service:
            command_manager = self._timing_service.command_manager
        if command_manager is not None:
            command = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                description,
            )
            command.undo_position = undo_pos
            focus_line_idx, focus_char_idx, checkpoint_idx, change_type = result
            command.redo_position = (focus_line_idx, focus_char_idx)
            command.move_cp = move_cp
            command_manager.execute(command)

        focus_line_idx, focus_char_idx, checkpoint_idx, change_type = result
        self._sync_after_structure_change(
            change_type=change_type,
            focus_line_idx=focus_line_idx,
            focus_char_idx=focus_char_idx,
            checkpoint_idx=checkpoint_idx,
            move_cp=move_cp,
        )
        return True

    def _register_timestamp_undo(
        self,
        before_sentences: list,
        focus_line_idx: int,
        focus_char_idx: int,
        description: str,
    ) -> None:
        """手动注册撤销命令（不走 _sync_after_structure_change）。"""
        if not self._project:
            return
        # after_sentences 不深拷贝 —— execute() 内会自行 deepcopy，省去一次全量拷贝
        after_sentences = self._project.sentences
        command_manager = None
        if self._timing_service:
            command_manager = self._timing_service.command_manager
        if command_manager is not None:
            command = SentenceSnapshotCommand(
                self._project,
                before_sentences,
                after_sentences,
                description,
            )
            undo_pos = (self._current_line_idx, self.preview._current_char_idx)
            command.undo_position = undo_pos
            command.redo_position = (focus_line_idx, focus_char_idx)
            command_manager.execute(command)

    def _delete_char_range(
        self, line_idx: int, start_idx: int, end_idx: int
    ) -> Optional[tuple[int, int, Optional[int], str]]:
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return None

        sentence = self._project.sentences[line_idx]
        if not sentence.characters:
            return None

        start = max(0, min(start_idx, len(sentence.characters) - 1))
        end = max(start + 1, min(end_idx, len(sentence.characters)))
        delete_count = end - start
        for _ in range(delete_count):
            became_empty = sentence.delete_character(start)
            if became_empty:
                break

        if not sentence.characters:
            self._project.delete_line(line_idx)
            if not self._project.sentences:
                return 0, 0, None, "lyrics"
            new_line_idx = max(0, min(line_idx, len(self._project.sentences) - 1))
            new_sentence = self._project.sentences[new_line_idx]
            new_char_idx = 0 if not new_sentence.characters else min(start, len(new_sentence.characters) - 1)
            return new_line_idx, new_char_idx, 0, "lyrics"

        new_char_idx = min(start, len(sentence.characters) - 1)
        return line_idx, new_char_idx, 0, "lyrics"
    
    def _delete_timestamp(self, line_idx: int, char_idx: int) -> bool:
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return False

        sentence = self._project.sentences[line_idx]
        char = sentence.get_character(char_idx)
        if char is None or not char.all_timestamps:
            return False

        sentence.clear_one_timestamps(char_idx)
        # 退格删除不经过 on_timetag_added 回调，只需刷新 Ruby 的已打轴底色。
        # 走字仍使用删除前的时间轴、锚点和遮罩缓存，不由退格重算。
        self.preview._refresh_ruby_timing_state(line_idx)
        return True

    def _insert_line_break_at_current(self):
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        project = self._project

        self._execute_structural_edit(
            "插入换行",
            lambda: (
                project.insert_line_break(line_idx, char_idx)
                or (line_idx + 1, 0, 0, "lyrics")
            ),
            move_cp=False,
        )

    def _delete_current_selection_or_char(self):
        if not self._project:
            return

        # Del 仅在编辑模式触发（keyPressEvent 路由）。focus 域为真理：
        # 用户拖选范围 → 删整段；单点 focus → 删该字符；focus 无效 → 删 current。
        if self.preview.is_multi_line_selection():
            sel = self.preview.get_normalized_selection()
            if sel is None:
                return
            start_line, start_char, end_line, end_char = sel
            def _multi_delete():
                for line_idx in range(end_line, start_line - 1, -1):
                    if line_idx < 0 or line_idx >= len(self._project.sentences):
                        continue
                    sentence = self._project.sentences[line_idx]
                    if not sentence.characters:
                        continue
                    s = start_char if line_idx == start_line else 0
                    e = end_char if line_idx == end_line else len(sentence.characters) - 1
                    if s > e:
                        continue
                    self._delete_char_range(line_idx, s, e + 1)
                total = len(self._project.sentences)
                fl = max(0, min(start_line, total - 1)) if total > 0 else 0
                return (fl, min(start_char, len(self._project.sentences[fl].characters) - 1) if self._project.sentences[fl].characters else 0, 0, "lyrics")
            self._execute_structural_edit("删除字符", _multi_delete)
            return

        if (
            self.preview._focus_line_idx >= 0
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            line_idx = self.preview._focus_line_idx
            start = min(self.preview._focus_char_idx, self.preview._focus_char_range_end)
            end = max(self.preview._focus_char_idx, self.preview._focus_char_range_end) + 1
        else:
            line_idx = self._current_line_idx
            start = self.preview._current_char_idx
            end = start + 1

        self._execute_structural_edit(
            "删除字符",
            lambda: self._delete_char_range(line_idx, start, end),
        )

    def _toggle_sentence_end_at_current(self):
        if not self._project:
            return
        # `.` (编辑模式) / F4 (打轴模式) 共用入口；目标字符由 `_resolve_target_char()`
        # 按模式分流：编辑模式 focus 优先，打轴模式 current。
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        self._execute_structural_edit(
            "切换停顿点",
            lambda: (
                sentence.toggle_sentence_end(char_idx)
                or (line_idx, char_idx, 0, "checkpoints")
            ),
        )

    def _convert_timestamps_to_sentence_end(self):
        """取消当前字符所有节奏点、清除时间戳并标记为停顿点。"""
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        def _mutate():
            char = sentence.characters[char_idx]
            char.clear_timestamps()
            char.set_check_count(0, force=True)
            if not char.is_sentence_end:
                char.is_sentence_end = True
            return line_idx, char_idx, 0, "checkpoints"

        self._execute_structural_edit("时间戳转停顿点", _mutate)

    def _tag_at_current_position_in_edit_mode(self):
        """编辑模式下打轴：读取当前进度条位置并写入当前节奏点，不启动音频。"""
        if not self._timing_service:
            return
        try:
            self._timing_service.on_edit_mode_tag()
        except Exception as e:
            self._show_runtime_error(str(e))

    def _clear_all_checkpoints(self):
        """清除选中字符的全部节奏点：cc=0，is_sentence_end=False，清空时间戳。

        支持拖选范围：多行选区 / 单行划选范围内的所有字符都会被清除，并拼成
        一个 undo/redo；无选区时回退到当前单字符。仅产生一次结构变更（无逐字通知）。
        """
        if not self._project:
            return

        def _clear_one(line_idx: int, ch_idx: int) -> None:
            sentence = self._project.sentences[line_idx]
            if ch_idx < 0 or ch_idx >= len(sentence.characters):
                return
            char = sentence.characters[ch_idx]
            char.clear_timestamps()
            char.set_check_count(0, force=True)
            char.is_sentence_end = False

        # 多行选区：清除选区内每一行的字符
        if self.preview.is_multi_line_selection():
            sel = self.preview.get_normalized_selection()
            if sel is None:
                return
            start_line, start_char, end_line, end_char = sel

            def _mutate_multi():
                for line_idx in range(start_line, end_line + 1):
                    if line_idx < 0 or line_idx >= len(self._project.sentences):
                        continue
                    sentence = self._project.sentences[line_idx]
                    if not sentence.characters:
                        continue
                    s = start_char if line_idx == start_line else 0
                    e = end_char if line_idx == end_line else len(sentence.characters) - 1
                    for ch_idx in range(s, e + 1):
                        _clear_one(line_idx, ch_idx)
                fl = max(0, min(start_line, len(self._project.sentences) - 1))
                chars = self._project.sentences[fl].characters
                fc = min(start_char, len(chars) - 1) if chars else 0
                return fl, fc, 0, "checkpoints"

            self._execute_structural_edit("清除所有节奏点", _mutate_multi)
            return

        # 单行划选 / 单字符回退（与「连词」等窗口一致：focus 域优先）
        sel_line = self.preview._focus_line_idx
        sel_start = self.preview._focus_char_idx
        sel_end = self.preview._focus_char_range_end
        if sel_line >= 0 and sel_start >= 0:
            use_line = sel_line
            start_idx = min(sel_start, sel_end)
            end_idx = max(sel_start, sel_end)
        else:
            use_line, char_idx = self._resolve_target_char()
            start_idx = end_idx = char_idx

        if use_line < 0 or use_line >= len(self._project.sentences):
            return
        sentence = self._project.sentences[use_line]
        if start_idx < 0 or end_idx >= len(sentence.characters):
            return

        def _mutate():
            for ch_idx in range(start_idx, end_idx + 1):
                _clear_one(use_line, ch_idx)
            return use_line, start_idx, 0, "checkpoints"

        self._execute_structural_edit("清除所有节奏点", _mutate)

    def _change_checkpoint(self, delta: int):
        """增加或减少"当前选中字符"的节奏点数量。

        通过 `_resolve_target_char()` 解析目标：编辑/编辑模式下都 focus 域优先
        （用户点击/拖选/纯←→设置的字符，不被 cp 自动跳跃污染）；打轴模式
        """
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        def _mutate():
            if delta > 0:
                from strange_uta_game.frontend.editor.timing.dialogs import (
                    _get_ruby_split_mode,
                )
                mode = _get_ruby_split_mode()
                sentence.add_checkpoint(char_idx, ruby_split_mode=mode)
            else:
                # 减到 0 时自动退化为 Nicokara 无 mora 格式（注音文本保留）
                sentence.remove_checkpoint(char_idx, force=True)
            cp_idx = self.preview._current_checkpoint_idx
            if cp_idx is not None and delta < 0:
                cp_idx = min(cp_idx, sentence.characters[char_idx].check_count)
            return line_idx, char_idx, cp_idx if cp_idx is not None else 0, "checkpoints"

        self._execute_structural_edit("调整节奏点", _mutate)

    def _toggle_line_end(self):
        """F6 切换当前字符的停顿点标记 (is_line_end)。

        停顿点标记独立于普通 checkpoint 数量。
        """
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        self._execute_structural_edit(
            "切换停顿点",
            lambda: (
                sentence.toggle_sentence_end(char_idx)
                or (line_idx, char_idx, 0, "checkpoints")
            ),
        )

    def _toggle_word_join(self):
        """F3 连词/取消连词。

        单选：toggle 当前字符的 linked_to_next（保持旧行为）。
        多选（划选多个字符）：沿用"快速连词/取消连词"逻辑 ——
        若选区内所有字符的 linked_to_next 均为 False，则把除最后一个被选中字符外
        的 linked_to_next 都置为 True（将选区连成一个词）；否则把选区内所有字符的
        linked_to_next 都置为 False。
        """
        if not self._project:
            return

        if self.preview.is_multi_line_selection():
            InfoBar.warning(
                title=self.tr("暂不允许多行"),
                content=self.tr("F3连词暂不允许多行选择"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return

        # 解析选择范围（与 _on_modify_char 一致：优先划选区域，回退单字符）
        sel_line = self.preview._focus_line_idx
        sel_start = self.preview._focus_char_idx
        sel_end = self.preview._focus_char_range_end
        if sel_line >= 0 and sel_start >= 0:
            use_line = sel_line
            start_idx = min(sel_start, sel_end)
            end_idx = max(sel_start, sel_end)
        else:
            use_line, char_idx = self._resolve_target_char()
            start_idx = end_idx = char_idx

        if use_line < 0 or use_line >= len(self._project.sentences):
            return
        sentence = self._project.sentences[use_line]
        if start_idx < 0 or end_idx >= len(sentence.characters):
            return

        if start_idx == end_idx:
            self._toggle_word_join_single(use_line, start_idx)
        else:
            self._toggle_word_join_range(use_line, start_idx, end_idx)

    def _toggle_word_join_single(self, line_idx: int, char_idx: int):
        """单字符连词：toggle 该字符的 linked_to_next。"""
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        # 不能在最后一个字符上连词
        if char_idx >= len(sentence.characters) - 1:
            InfoBar.warning(
                title=self.tr("无法连词"),
                content=self.tr("已是最后一个字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )
            return

        ch = sentence.characters[char_idx]
        new_linked = not ch.linked_to_next
        before_character = deepcopy(ch)
        ch.linked_to_next = new_linked
        command_manager = (
            self._timing_service.command_manager if self._timing_service else None
        )
        if command_manager is not None:
            command_manager.execute(
                CharacterSnapshotCommand(
                    self._project,
                    line_idx,
                    char_idx,
                    before_character,
                    ch,
                    "连词" if new_linked else "取消连词",
                )
            )
        self.preview._update_display()
        if hasattr(self, "_store") and self._store:
            self._store.notify("rubies")

        InfoBar.success(
            title=self.tr("连词") if new_linked else self.tr("取消连词"),
            content=(
                self.tr("已连接「{a}」与「{b}」") if new_linked
                else self.tr("已断开「{a}」与「{b}」")
            ).format(a=sentence.chars[char_idx], b=sentence.chars[char_idx + 1]),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def _toggle_word_join_range(self, line_idx: int, start_idx: int, end_idx: int):
        """多字符连词：选区全未连词 → 除末字外全部连词；否则全部取消连词。"""
        sentence = self._project.sentences[line_idx]
        chars = sentence.characters
        selected = chars[start_idx : end_idx + 1]
        # 选区内全部 linked_to_next 为 False → 连词；否则 → 全部取消
        link = all(not c.linked_to_next for c in selected)

        def _mutate():
            if link:
                # 除最后一个被选中字符外，全部置为连词（末字保持 False，避免连到选区外）
                for i in range(start_idx, end_idx):
                    chars[i].linked_to_next = True
            else:
                for i in range(start_idx, end_idx + 1):
                    chars[i].linked_to_next = False
            return (line_idx, start_idx, 0, "checkpoints")

        self._execute_structural_edit(
            "连词" if link else "取消连词",
            _mutate,
            move_cp=False,
        )

        InfoBar.success(
            title=self.tr("连词") if link else self.tr("取消连词"),
            content=(
                self.tr("已将第 {line} 句 第 {s}-{e} 字连为一个词")
                if link
                else self.tr("已断开第 {line} 句 第 {s}-{e} 字的连词")
            ).format(line=line_idx + 1, s=start_idx + 1, e=end_idx + 1),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def _on_nav_line(self, delta: int):
        """方向键导航：上一行 (delta=-1) 或下一行 (delta=+1)。

        编辑模式：focus 域为真理来源（与 ←→/Space/Backspace/`.` 一致）。
        起点取 focus 行（无效则 current），目标行落在第一个字符 (char_idx=0)。
        使用 :py:meth:`Project.find_prev_line_with_characters` /
        :py:meth:`Project.find_next_line_with_characters` 跳过空行（无字符的行）。
        到达项目首尾时停止。

        打轴模式：保持原 cp 跳跃语义（focus 不跟随，current 由 TimingService 推进）。
        """
        if not self._project or not self._timing_service:
            return
        sentences = self._project.sentences

        # playing = bool(self._timing_service.is_playing())
        # if playing:
        #     # 打轴模式：原行为不变（基于 current 行 + cp 跳跃）
        #     if delta < 0:
        #         cand = self._project.find_prev_line_with_checkpoints(self._current_line_idx)
        #         if cand < 0:
        #             return
        #         new_idx = cand
        #     else:
        #         new_idx = self._current_line_idx + delta
        #         if new_idx < 0 or new_idx >= len(sentences):
        #             return
        #     self._timing_service.move_to_checkpoint(new_idx, 0, 0)
        #     self._update_time_tags_display()
        #     self._update_status()
        #     return

        # 编辑模式：focus 起点 + 跳空行 + 写 focus + 驱动 current
        if self.preview._focus_line_idx >= 0:
            line_idx = self.preview._focus_line_idx
        else:
            line_idx = self._current_line_idx
        if delta < 0:
            cand = self._project.find_prev_line_with_characters(line_idx)
        else:
            cand = self._project.find_next_line_with_characters(line_idx)
        if cand < 0:
            return
        # 继承当前 char_idx，越界则 clamp 到目标行行尾
        cur_char = self.preview._focus_char_idx if self.preview._focus_char_idx >= 0 else self.preview._current_char_idx
        target_chars = sentences[cand].characters
        if target_chars:
            new_char = min(cur_char, len(target_chars) - 1)
        else:
            new_char = 0
        new_line = cand
        # 行切换前校验当前行的时间戳
        if new_line != line_idx:
            self._validate_line_timestamps(line_idx)
        # 直接写 focus 域（与 _on_nav_char 同款，不依赖 cp 回调链污染）
        self.preview._focus_line_idx = new_line
        self.preview._focus_char_idx = new_char
        self.preview._focus_line_range_end = new_line
        self.preview._focus_char_range_end = new_char
        # 驱动 current 跟随：找最近 cp 反馈到 current。
        # 抑制 _apply_checkpoint_position 的居中滚动，以 focus 域为基准。
        self._suppress_cp_cursor_move = True
        try:
            self._timing_service.move_to_checkpoint(
                new_line, new_char, 0, prefer_backward=True
            )
        finally:
            self._suppress_cp_cursor_move = False
        self._current_line_idx = new_line
        pos = self._timing_service.get_current_position()
        self.preview._current_char_idx = pos.char_idx
        self.preview._current_line_idx = new_line
        self.preview.scroll_current_line_to_center()
        self._update_line_info()
        self._update_time_tags_display()
        self._update_status()
        self.preview.update()

    def _on_nav_char(self, delta: int):
        """方向键左右导航：上一字符 (delta=-1) 或下一字符 (delta=+1)。

        字符级操作 → 读 focus 域（用户视觉真理），不读被 cp 跳跃污染的
        current 域。同时直接更新 focus 域字段，并驱动 current 跟随
        (move_to_checkpoint 让 TimingService 找最近 cp 反馈到 current)。

        行内移动：在当前 focus 行的字符序列内 ±1。
        跨行边界：
        - delta=-1 且 focus 已在首字符 (char_idx == 0)：跳到上一行的末字符。
        - delta=+1 且 focus 已在末字符：跳到下一行的首字符 (char_idx = 0)。
        跨行使用 :py:meth:`Project.find_prev_line_with_characters` /
        :py:meth:`Project.find_next_line_with_characters` 跳过空行。
        到达项目首尾时停止（不循环）。

        Args:
            delta: -1 表示左移 (LEFT)，+1 表示右移 (RIGHT)。
        """
        if not self._project or not self._timing_service:
            return
        sentences = self._project.sentences
        # focus 域作为真理来源；focus 无效则回退 current 一次
        if self.preview._focus_line_idx >= 0 and self.preview._focus_char_idx >= 0:
            line_idx = self.preview._focus_line_idx
            char_idx = min(
                self.preview._focus_char_idx,
                self.preview._focus_char_range_end
                if self.preview._focus_char_range_end >= 0
                else self.preview._focus_char_idx,
            )
        else:
            line_idx = self._current_line_idx
            char_idx = self.preview._current_char_idx
        if line_idx < 0 or line_idx >= len(sentences):
            return
        chars = sentences[line_idx].characters
        if delta < 0:
            if char_idx > 0:
                new_line, new_char = line_idx, char_idx - 1
            else:
                cand = self._project.find_prev_line_with_characters(line_idx)
                if cand < 0:
                    return
                prev_chars = sentences[cand].characters
                new_line, new_char = cand, max(0, len(prev_chars) - 1)
        else:
            if char_idx < len(chars) - 1:
                new_line, new_char = line_idx, char_idx + 1
            else:
                cand = self._project.find_next_line_with_characters(line_idx)
                if cand < 0:
                    return
                new_line, new_char = cand, 0
        # 直接更新 focus 域（不依赖 cp 回调链）
        self.preview._focus_line_idx = new_line
        self.preview._focus_char_idx = new_char
        self.preview._focus_line_range_end = new_line
        self.preview._focus_char_range_end = new_char
        # 驱动 current 跟随：让 TimingService 找最近 cp，
        # 反馈经 _apply_checkpoint_position 更新 current 域。
        # 抑制居中滚动，以 focus 域为基准。
        self._suppress_cp_cursor_move = True
        try:
            self._timing_service.move_to_checkpoint(
                new_line, new_char, 0, prefer_backward=True
            )
        finally:
            self._suppress_cp_cursor_move = False
        self._current_line_idx = new_line
        pos = self._timing_service.get_current_position()
        self.preview._current_char_idx = pos.char_idx
        self.preview._current_line_idx = new_line
        self.preview.scroll_current_line_to_center()
        self._update_line_info()
        self._update_time_tags_display()
        self._update_status()

    def _find_previous_timestamp(self, line_idx: int, char_idx: int) -> Optional[int]:
        """向前查找最近的时间戳（可能在上一行）

        从指定位置向前搜索，返回找到的第一个时间戳。
        """
        if not self._project:
            return None

        # 从当前行往前找
        for li in range(line_idx, -1, -1):
            sentence = self._project.sentences[li]
            # 确定本行搜索的字符范围
            end_char = char_idx if li == line_idx else len(sentence.characters) - 1

            for ci in range(end_char, -1, -1):
                char = sentence.get_character(ci)
                if not char:
                    continue
                tags = char.all_global_timestamps
                if tags:
                    return tags[-1]  # 返回该字符最后一个时间戳（最近的）
        return None

    def _find_previous_timestamp_with_position(
        self, line_idx: int, char_idx: int
    ) -> Optional[tuple[int, int, int]]:
        """向前查找最近的时间戳，同时返回该时间戳所在的字符位置

        Args:
            line_idx: 当前行索引
            char_idx: 当前字符索引

        Returns:
            找到的 (timestamp, line_idx, char_idx) 或 None
        """
        if not self._project:
            return None

        # 从当前行往前找
        for li in range(line_idx, -1, -1):
            sentence = self._project.sentences[li]
            # 确定本行搜索的字符范围
            end_char = char_idx if li == line_idx else len(sentence.characters) - 1

            for ci in range(end_char, -1, -1):
                char = sentence.get_character(ci)
                if not char:
                    continue
                tags = char.all_global_timestamps
                if tags:
                    return (tags[-1], li, ci)
        return None

    def _resolve_cp_idx_for_timestamp(
        self, line_idx: int, char_idx: int, timestamp: int
    ) -> int:
        """根据时间戳值反查所属的 checkpoint 索引。

        在字符的 all_global_timestamps 中找到与 timestamp 匹配的索引；
        找不到时回退到最后一个 cp。
        """
        if not self._project or line_idx >= len(self._project.sentences):
            return 0
        sentence = self._project.sentences[line_idx]
        if char_idx >= len(sentence.characters):
            return 0
        char = sentence.get_character(char_idx)
        if not char:
            return 0
        tags = char.all_global_timestamps
        if not tags:
            return 0
        # 精确匹配
        for i, t in enumerate(tags):
            if t == timestamp:
                return i
        # 找不到精确匹配，回退到最后一个 cp
        return len(tags) - 1

    def _find_prev_char_with_cp(
        self, line_idx: int, char_idx: int
    ) -> Optional[Tuple[int, int, int]]:
        """向前查找最近一个有CP的字符（check_count > 0）

        Args:
            line_idx: 当前行索引
            char_idx: 当前字符索引

        Returns:
            找到的 (line_idx, char_idx, cp_idx) 或 None
        """
        if not self._project:
            return None

        # 从当前行往前找
        for li in range(line_idx, -1, -1):
            sentence = self._project.sentences[li]
            # 当前行从 char_idx - 1 开始（跳过当前字符），其他行从末尾开始
            end_char = char_idx - 1 if li == line_idx else len(sentence.characters) - 1

            for ci in range(end_char, -1, -1):
                char = sentence.get_character(ci)
                if not char:
                    continue
                if char.check_count > 0:
                    return (li, ci, 0)

        return None

    def _on_seek_to_char(self, line_idx: int, char_idx: int):
        """双击字符 → 跳转到该字符的时间戳（无时间戳则向前查找）

        对于无CP字符：
        - 有时间戳：跳转到该时间戳，CP挪到该字符
        - 无时间戳但找到前一个时间戳：跳转到前一个时间戳，CP挪到时间戳所在的字符
        - 完全没有时间戳：跳转到歌曲开头(0)，CP挪到全文键第一个CP
        不动focus域的字符选中。
        """
        if not self._project or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx >= len(sentence.chars):
            return

        char = sentence.get_character(char_idx)
        if not char:
            return

        # 判断当前字符是否有 checkpoint
        no_checkpoint = char.check_count == 0 and not char.is_sentence_end

        tags = char.all_global_timestamps
        if tags:
            # 有时间戳：跳转到该时间戳
            self._on_seek(tags[0])
            # CP挪到当前字符
            if self._timing_service:
                self._timing_service.move_to_checkpoint(line_idx, char_idx, 0)
        elif no_checkpoint:
            # 无CP字符且无时间戳：向前查找最近的时间戳
            result = self._find_previous_timestamp_with_position(line_idx, char_idx)
            if result is not None:
                prev_ts, ts_line_idx, ts_char_idx = result
                self._on_seek(prev_ts)
                # CP挪到时间戳所在的字符的对应 cp_idx（而非固定 0）
                if self._timing_service:
                    cp_idx = self._resolve_cp_idx_for_timestamp(
                        ts_line_idx, ts_char_idx, prev_ts
                    )
                    self._timing_service.move_to_checkpoint(
                        ts_line_idx, ts_char_idx, cp_idx
                    )
            else:
                # 完全没有时间戳：跳转到歌曲开头
                self._on_seek(0)
                # CP挪到全文键第一个CP
                if self._timing_service:
                    self._timing_service.move_to_checkpoint(0, 0, 0)
        else:
            # 有CP但无时间戳：向前查找最近的时间戳
            result = self._find_previous_timestamp_with_position(line_idx, char_idx)
            if result is not None:
                prev_ts, ts_line_idx, ts_char_idx = result
                self._on_seek(prev_ts)
                # CP挪到时间戳所在的字符的对应 cp_idx（而非固定 0）
                if self._timing_service:
                    cp_idx = self._resolve_cp_idx_for_timestamp(
                        ts_line_idx, ts_char_idx, prev_ts
                    )
                    self._timing_service.move_to_checkpoint(
                        ts_line_idx, ts_char_idx, cp_idx
                    )
            else:
                # 完全没有时间戳：跳转到歌曲开头
                self._on_seek(0)
                # CP挪到全文键第一个CP
                if self._timing_service:
                    self._timing_service.move_to_checkpoint(0, 0, 0)

        self._update_line_info()

    def _on_seek_to_checkpoint(self, line_idx: int, char_idx: int, cp_idx: int):
        """双击 checkpoint → 跳转到该 checkpoint 的时间戳（无时间戳则向前查找）"""
        if not self._project or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx >= len(sentence.chars):
            return

        char = sentence.get_character(char_idx)
        if not char:
            return

        tags = char.all_global_timestamps
        if tags:
            target_idx = min(cp_idx, len(tags) - 1)
            self._on_seek(tags[target_idx])
        else:
            # 向前查找最近的时间戳，仅跳转音频
            prev_ts = self._find_previous_timestamp(line_idx, char_idx)
            if prev_ts is not None:
                self._on_seek(prev_ts)

        # 移动打轴位置到当前双击的 checkpoint
        if self._timing_service:
            self._timing_service.move_to_checkpoint(line_idx, char_idx, cp_idx)
        # 同步 focus 字符到 cp 对应的字符
        self.preview.set_focus_position(line_idx, char_idx)

    def _on_delete_chars_requested(self, line_idx: int, start: int, end: int):
        self._execute_structural_edit(
            "删除字符",
            lambda: self._delete_char_range(line_idx, start, end),
            move_cp=False,
        )

    def _on_delete_chars_multi_requested(self, start_line: int, start_char: int, end_line: int, end_char: int):
        def _mutate():
            for line_idx in range(end_line, start_line - 1, -1):
                if line_idx < 0 or line_idx >= len(self._project.sentences):
                    continue
                sentence = self._project.sentences[line_idx]
                if not sentence.characters:
                    continue
                s = start_char if line_idx == start_line else 0
                e = end_char if line_idx == end_line else len(sentence.characters) - 1
                if s > e:
                    continue
                self._delete_char_range(line_idx, s, e + 1)
            total = len(self._project.sentences)
            fl = max(0, min(start_line, total - 1)) if total > 0 else 0
            return (fl, min(start_char, len(self._project.sentences[fl].characters) - 1) if self._project.sentences[fl].characters else 0, 0, "lyrics")
        self._execute_structural_edit("删除字符", _mutate, move_cp=False)

    def _on_singer_change_multi_requested(self, start_line: int, start_char: int, end_line: int, end_char: int, singer_id: str):
        if not self._project:
            return
        project = self._project
        def _mutate():
            changed = False
            for line_idx in range(end_line, start_line - 1, -1):
                if line_idx >= len(project.sentences):
                    continue
                sentence = project.sentences[line_idx]
                if not sentence.characters:
                    continue
                s = start_char if line_idx == start_line else 0
                e = end_char if line_idx == end_line else len(sentence.characters) - 1
                if s > e:
                    continue
                for ci in range(s, e + 1):
                    if ci < len(sentence.characters):
                        ch = sentence.characters[ci]
                        if ch.singer_id != singer_id:
                            ch.singer_id = singer_id
                            ch.push_to_ruby()
                            changed = True
                if s == 0 and e >= len(sentence.characters) - 1:
                    if sentence.singer_id != singer_id:
                        sentence.singer_id = singer_id
                        changed = True
            if not changed:
                return None
            return (start_line, start_char, None, "singers")
        self._execute_structural_edit("划选设置演唱者", _mutate)
    def _on_delete_timestamp_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx >= len(sentence.chars):
            return

        jump_before_raw = getattr(self, "_jump_before_ms", 3000)
        speed = self._timing_service.get_speed() if self._timing_service else 1.0
        jump_before = int(jump_before_raw * speed)
        char = sentence.get_character(char_idx)

        before_sentences = deepcopy(self._project.sentences)

        if char and char.all_global_timestamps:
            # 当前字符有时间戳：删除当前字符时间戳，音频回退3秒，结束
            seek_ms = max(0, char.all_global_timestamps[0] - jump_before)
            self._delete_timestamp(line_idx, char_idx)
            self._register_timestamp_undo(before_sentences, line_idx, char_idx, "删除时间戳")
            if self._timing_service:
                self._timing_service.move_to_checkpoint(line_idx, char_idx, 0, prefer_backward=True)
                self._update_time_tags_display()
                self._update_status()
                if self._scroll_mode != "always":
                    self.preview.scroll_current_line_to_center()
            self._on_seek(seek_ms)
        else:
            # 当前字符没有时间戳：找前一个有节奏点的字符
            prev_char = self._find_prev_char_with_cp(line_idx, char_idx)
            if not prev_char:
                return
            prev_line, prev_char_idx, prev_cp_idx = prev_char
            prev = self._project.sentences[prev_line].get_character(prev_char_idx)
            seek_ms = max(0, prev.all_global_timestamps[0] - jump_before) if prev and prev.all_global_timestamps else None
            self._delete_timestamp(prev_line, prev_char_idx)
            self._register_timestamp_undo(before_sentences, prev_line, prev_char_idx, "删除时间戳")
            if self._timing_service:
                self._timing_service.move_to_checkpoint(prev_line, prev_char_idx, prev_cp_idx)
                self._update_time_tags_display()
                self._update_status()
                if self._scroll_mode != "always":
                    self.preview.scroll_current_line_to_center()
            self.preview.set_focus_position(prev_line, prev_char_idx)
            if seek_ms is not None:
                self._on_seek(seek_ms)

    def _on_insert_space_before_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        def _mutate():
            sentence = project.sentences[line_idx]
            if not sentence.characters:
                # 空行：直接在行首插入
                new_char = Character(char=" ", check_count=0, singer_id=sentence.singer_id)
                sentence.insert_character(0, new_char)
                return line_idx, 0, 0, "lyrics"
            if char_idx < 0 or char_idx >= len(sentence.characters):
                return None
            ref_char = sentence.characters[char_idx]
            new_char = Character(
                char=" ",
                check_count=0,
                singer_id=ref_char.singer_id or sentence.singer_id,
            )
            sentence.insert_character(char_idx, new_char)
            return line_idx, char_idx, 0, "lyrics"

        self._execute_structural_edit("在前插入空格", _mutate, move_cp=False)

    def _on_insert_space_after_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        def _mutate():
            sentence = project.sentences[line_idx]
            if not sentence.characters:
                # 空行：直接在行首插入
                new_char = Character(char=" ", check_count=0, singer_id=sentence.singer_id)
                sentence.insert_character(0, new_char)
                return line_idx, 0, 0, "lyrics"
            if char_idx < 0 or char_idx >= len(sentence.characters):
                return None
            ref_char = sentence.characters[char_idx]
            new_char = Character(
                char=" ",
                check_count=0,
                singer_id=ref_char.singer_id or sentence.singer_id,
            )
            sentence.insert_character(char_idx + 1, new_char)
            return line_idx, char_idx + 1, 0, "lyrics"

        self._execute_structural_edit("插入空格", _mutate, move_cp=False)

    def _insert_space_at_current(self):
        """在当前字符后插入空格（快捷键入口）。"""
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        def _mutate():
            sentence = project.sentences[line_idx]
            if not sentence.characters:
                new_char = Character(char=" ", check_count=0, singer_id=sentence.singer_id)
                sentence.insert_character(0, new_char)
                return line_idx, 0, 0, "lyrics"
            if char_idx < 0 or char_idx >= len(sentence.characters):
                return None
            ref_char = sentence.characters[char_idx]
            new_char = Character(
                char=" ",
                check_count=0,
                singer_id=ref_char.singer_id or sentence.singer_id,
            )
            sentence.insert_character(char_idx + 1, new_char)
            return line_idx, char_idx + 1, 0, "lyrics"

        self._execute_structural_edit("插入空格", _mutate, move_cp=False)

    def _merge_line_up_at_current(self):
        """将当前行合并到上一行（快捷键触发）。"""
        if not self._project:
            return
        line_idx = self._current_line_idx
        if line_idx <= 0 or line_idx >= len(self._project.sentences):
            return
        self._on_merge_line_up_requested(line_idx)

    def _on_merge_line_up_requested(self, line_idx: int):
        if not self._project:
            return
        project = self._project
        self._execute_structural_edit(
            "合并上一行",
            lambda: (
                (
                    line_idx - 1,
                    max(0, len(project.sentences[line_idx - 1].characters) - 1),
                    0,
                    "lyrics",
                )
                if project.merge_line_into_previous(line_idx)
                else None
            ),
            move_cp=False,
        )

    def _on_delete_line_requested(self, line_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        def _mutate():
            project.delete_line(line_idx)
            if not project.sentences:
                return 0, 0, None, "lyrics"
            new_line_idx = max(0, min(line_idx, len(project.sentences) - 1))
            return new_line_idx, 0, 0, "lyrics"

        self._execute_structural_edit("删除本行", _mutate, move_cp=False)

    def _on_insert_blank_line_before_requested(self, line_idx: int):
        if not self._project:
            return
        project = self._project

        singer_id = ""
        if 0 <= line_idx < len(project.sentences):
            sentence = project.sentences[line_idx]
            if sentence.characters:
                singer_id = sentence.characters[-1].singer_id

        self._execute_structural_edit(
            "在前插入空行",
            lambda: ((project.insert_blank_line(line_idx - 1, singer_id=singer_id), 0, None, "lyrics")),
            move_cp=False,
        )

    def _on_insert_blank_line_requested(self, line_idx: int):
        if not self._project:
            return
        project = self._project

        singer_id = ""
        if 0 <= line_idx < len(project.sentences):
            sentence = project.sentences[line_idx]
            if sentence.characters:
                singer_id = sentence.characters[-1].singer_id

        self._execute_structural_edit(
            "插入空行",
            lambda: ((project.insert_blank_line(line_idx, singer_id=singer_id), 0, None, "lyrics")),
            move_cp=False,
        )

    def _on_add_checkpoint_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        def _mutate():
            from strange_uta_game.frontend.editor.timing.dialogs import (
                _get_ruby_split_mode,
            )
            mode = _get_ruby_split_mode()
            project.sentences[line_idx].add_checkpoint(
                char_idx, ruby_split_mode=mode
            )
            return line_idx, char_idx, 0, "checkpoints"

        self._execute_structural_edit("增加节奏点", _mutate)

    def _on_remove_checkpoint_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project
        sentence = project.sentences[line_idx]

        def _mutate():
            # 减到 0 时自动退化为 Nicokara 无 mora 格式（注音文本保留）
            sentence.remove_checkpoint(char_idx, force=True)
            return line_idx, char_idx, 0, "checkpoints"

        self._execute_structural_edit("减少节奏点", _mutate)

    def _on_toggle_sentence_end_requested(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        project = self._project

        self._execute_structural_edit(
            "切换停顿点",
            lambda: (
                project.sentences[line_idx].toggle_sentence_end(char_idx)
                or (line_idx, char_idx, 0, "checkpoints")
            ),
        )

    def _on_toggle_needs_guide_requested(self, line_idx: int, char_idx: int):
        """右键菜单触发：切换指定字符的导唱待办标记。"""
        self._toggle_needs_guide_at(line_idx, char_idx)

    def _toggle_needs_guide(self):
        """快捷键触发：切换当前 focus 字符的导唱待办标记。"""
        if not self._project:
            return
        line_idx, char_idx = self._resolve_target_char()
        if line_idx < 0:
            return
        self._toggle_needs_guide_at(line_idx, char_idx)

    def _toggle_needs_guide_at(self, line_idx: int, char_idx: int):
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            return

        def _mutate():
            ch = sentence.characters[char_idx]
            ch.needs_guide = not ch.needs_guide
            return line_idx, char_idx, None, "lyrics"

        self._execute_structural_edit("切换导唱待办", _mutate, move_cp=False)
        self._update_status()

    # ==================== 键盘 ====================

    def _execute_action(self, action: str, key: int):
        """执行指定的快捷键动作。"""
        if action == "play_pause":
            if self._timing_service and self._timing_service.is_playing():
                self._on_pause()
            else:
                self._on_play()
        elif action == "stop":
            self._on_stop()
        elif action == "restart_playback":
            self._on_stop()
            self._on_play()
        elif action == "seek_back":
            if self._timing_service and self._timing_service.is_playing():
                cur = self._timing_service.get_position_ms()
                speed = self._timing_service.get_speed()
                self._on_seek(max(0, cur - int(self._rewind_ms * speed)))
        elif action == "seek_forward":
            if self._timing_service and self._timing_service.is_playing():
                cur = self._timing_service.get_position_ms()
                dur = self._timing_service.get_duration_ms()
                speed = self._timing_service.get_speed()
                self._on_seek(min(dur, cur + int(self._fast_forward_ms * speed)))
        elif action == "lock_playback_start":
            self._toggle_playback_range_start()
        elif action == "lock_playback_end":
            self._toggle_playback_range_end()
        elif action == "speed_down":
            v = self.transport.get_speed_value()
            self.transport.set_speed_value(v - 5)
        elif action == "speed_up":
            v = self.transport.get_speed_value()
            self.transport.set_speed_value(v + 5)
        elif action == "speed_reset":
            self.transport.reset_speed()
        elif action == "volume_up":
            v = self.transport.slider_volume.value()
            self.transport.slider_volume.setValue(min(100, v + 5))
        elif action == "volume_down":
            v = self.transport.slider_volume.value()
            self.transport.slider_volume.setValue(max(0, v - 5))
        elif action == "volume_reset":
            self.transport.reset_volume()
        elif action == "nav_prev_line":
            self._on_nav_line(-1)
        elif action == "nav_next_line":
            self._on_nav_line(1)
        elif action == "nav_prev_char":
            self._on_nav_char(-1)
        elif action == "nav_next_char":
            self._on_nav_char(1)
        elif action == "timestamp_up":
            self._adjust_current_timestamp(self._timing_adjust_step_ms)
        elif action == "timestamp_down":
            self._adjust_current_timestamp(-self._timing_adjust_step_ms)
        elif action == "cycle_checkpoint":
            self._cycle_current_checkpoint(1)
        elif action == "cycle_checkpoint_prev":
            self._cycle_current_checkpoint(-1)
        elif action == "edit_ruby":
            if self._project:
                # 与「修改所选字符」等窗口统一：优先使用 focus 域（拖选/聚焦），
                # 无 focus 选择时回退到 current 域。
                sel_line = self.preview._focus_line_idx
                sel_start = self.preview._focus_char_idx
                if sel_line >= 0 and sel_start >= 0:
                    line_idx = sel_line
                    char_idx = sel_start
                else:
                    line_idx = self._current_line_idx
                    char_idx = self.preview._current_char_idx
                self._on_char_edit_requested(line_idx, char_idx)
        elif action == "add_checkpoint":
            if self._project:
                self._add_checkpoint()
        elif action == "remove_checkpoint":
            if self._project:
                self._remove_checkpoint()
        elif action == "toggle_word_join":
            if self._project:
                self._toggle_word_join()
        elif action == "toggle_waveform_spectrum":
            self._toggle_waveform_spectrum()
        elif action == "toggle_line_end":
            if self._project:
                line_idx, char_idx = self._resolve_target_char()
                if line_idx >= 0 and char_idx >= 0:
                    self.preview.toggle_sentence_end_requested.emit(line_idx, char_idx)
                else:
                    self._toggle_sentence_end_at_current()
        elif action == "delete_timestamp":
            if self._project:
                line_idx = self._current_line_idx
                char_idx = self.preview._current_char_idx
                self._on_delete_timestamp_requested(line_idx, char_idx)
        elif action == "bulk_change":
            self._on_bulk_change()
        elif action == "modify_char":
            self._on_modify_char()
        elif action == "insert_guide":
            self._on_insert_guide()
        elif action == "toggle_needs_guide":
            self._toggle_needs_guide()
        elif action == "modify_line":
            self._on_modify_line()
        elif action == "analyze_rubies":
            self._on_analyze_rubies()
        elif action == "analyze_rubies_by_line":
            self._on_analyze_rubies_by_line()
        elif action == "analyze_rubies_selected":
            self._on_analyze_rubies_selected()
        elif action == "open_fulltext":
            self._on_open_fulltext()
        elif action == "delete_rubies_by_type":
            self._on_delete_rubies_by_type()
        elif action == "set_singer_by_line":
            self._on_set_singer_by_line()
        elif action == "apply_singer":
            self._on_apply_singer()
        elif action == "timestamps_to_sentence_end":
            self._convert_timestamps_to_sentence_end()
        elif action in ("tag_now_editor", "tag_now_extra_editor"):
            self._tag_at_current_position_in_edit_mode()
        elif action == "clear_all_checkpoints":
            self._clear_all_checkpoints()
        elif action == "quick_export":
            self._on_quick_export()
        elif action == "insert_space":
            self._insert_space_at_current()
        elif action == "merge_line_up":
            self._merge_line_up_at_current()
        # ── 工具栏功能（默认无快捷键，可在设置中绑定）──
        elif action == "analyze_rubies_no_cp":
            self._on_analyze_rubies_no_cp()
        elif action == "analyze_rubies_by_line_no_cp":
            self._on_analyze_rubies_by_line_no_cp()
        elif action == "analyze_rubies_selected_no_cp":
            self._on_analyze_rubies_selected_no_cp()
        elif action == "romanize_all":
            self._on_romanize_all_rubies()
        elif action == "singer_manager":
            self._on_singer_manager_clicked()
        elif action == "complete_timestamp":
            self._on_complete_timestamp()
        elif action == "separate_symbol_timestamp":
            self._on_separate_symbol_timestamp()
        elif action == "adjust_raw_timestamp":
            self._on_adjust_raw_timestamp()
        elif action == "adjust_raw_timestamp_line":
            self._on_adjust_raw_timestamp_line()
        elif action == "adjust_raw_timestamp_selected":
            self._on_adjust_raw_timestamp_selected()
        elif action == "delete_all_timestamps":
            self._on_delete_all_timestamps()
        elif action == "delete_all_timestamps_keep_head":
            self._on_delete_all_timestamps_keep_head()
        elif action == "delete_timestamps_selected":
            self._on_delete_timestamps_selected()
        elif action == "new_project":
            self._on_new_project()
        elif action == "load_project":
            self._on_load_project()
        elif action == "save_as":
            self._on_save_as()
        elif action == "load_audio":
            self._on_load_audio()
        elif action == "load_lyrics":
            self._on_load_lyrics()
        elif action == "concat_sug":
            self._on_concat_sug()
        elif action == "auto_insert_guide":
            self._on_auto_insert_guide()
        elif action == "analyze_pinyin":
            self._on_analyze_pinyin()
        elif action == "analyze_korean":
            self._on_analyze_korean()
        elif action == "auto_generate_interlude_guide":
            self._on_auto_generate_interlude_guide()

    def _prompt_if_needs_guide_pending(self) -> bool:
        """若项目中仍有 needs_guide 标记，弹窗让用户决定是否继续导出。

        Returns:
            True — 可以继续导出（无标记或用户确认继续）
            False — 用户取消
        """
        if not self._project:
            return True
        marks: list[tuple[int, int]] = [
            (line_idx, char_idx)
            for line_idx, s in enumerate(self._project.sentences)
            for char_idx, c in enumerate(s.characters)
            if c.needs_guide
        ]
        if not marks:
            return True
        preview = [
            self.tr("第 {line} 行 第 {char} 字").format(line=l + 1, char=c + 1)
            for l, c in marks[:10]
        ]
        extra = self.tr("\n...另 {n} 处").format(n=len(marks) - 10) if len(marks) > 10 else ""
        return message_question(
            self,
            self.tr("仍有导唱待办未处理"),
            self.tr("还剩 {n} 个标记点未添加导唱符。").format(n=len(marks))
            + "\n\n"
            + "\n".join(preview)
            + extra,
            yes_text=self.tr("继续导出"),
            no_text=self.tr("取消"),
        )

    def _on_quick_export(self):
        """快捷导出：使用默认导出格式弹出保存对话框并导出。"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        if not self._prompt_if_needs_guide_pending():
            return

        from strange_uta_game.frontend.settings.app_settings import AppSettings

        settings = AppSettings()
        format_name = settings.get("export.default_format", "Nicokara (带注音)")

        try:
            exporter = get_exporter_by_name(format_name)
        except ValueError:
            InfoBar.error(
                title=self.tr("导出失败"),
                content=self.tr("未知的导出格式: {fmt}").format(fmt=format_name),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        ext = exporter.file_extension
        file_filter = exporter.file_filter

        store = getattr(self, "_store", None)
        audio_path = getattr(store, "audio_path", None) if store else None
        if audio_path:
            base_name = Path(audio_path).stem
        elif self._project.metadata.title:
            base_name = self._project.metadata.title
        else:
            base_name = "untitled"
        base_name = sanitize_export_basename(base_name)
        suggested_dir = ""
        if store:
            suggested_dir = store.export_dir
        if not suggested_dir:
            suggested_dir = settings.get("export.last_export_dir", "")
        suggested_path = str(Path(suggested_dir) / (base_name + ext)) if suggested_dir else base_name + ext

        file_path, _ = QFileDialog.getSaveFileName(
            self, self.tr("快捷导出"), suggested_path, file_filter
        )
        if not file_path:
            return

        if not Path(file_path).suffix:
            file_path += ext

        export_service = ExportService()
        result = export_service.export(
            self._project,
            format_name,
            file_path,
            offset_ms=settings.get("export.offset_ms", 0),
            software_compensation_ms=settings.get("export.software_compensation_ms", 0),
        )
        if result.success:
            settings.set("export.last_export_dir", str(Path(file_path).parent))
            settings.save()
            InfoBar.success(
                title=self.tr("导出成功"),
                content=result.file_path,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
        else:
            InfoBar.error(
                title=self.tr("导出失败"),
                content=result.error_message or self.tr("未知错误"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        self._file_loader._notify_main_window_frameless_refresh()

    def _on_long_press_timeout(self):
        """长按定时器超时，执行 long 动作。"""
        key_name = self._long_press_key
        # 清除被跟踪键的 pending 状态（标记为已处理长按）
        pending = self._pending_presses.pop(key_name, None) if key_name else None
        self._long_press_key = None
        if pending is not None and pending[1]:
            self._execute_action(pending[1], 0)
        # A11：还有其他未决按键时，为下一个键重启长按判定窗口
        if self._pending_presses:
            self._long_press_key = next(iter(self._pending_presses))
            self._long_press_timer.start()

    def eventFilter(self, obj, event):
        """捕获 preview 子控件的键盘和鼠标交互，触发自动滚动挂起。"""
        if obj is self.preview:
            etype = event.type()
            if etype == QEvent.Type.KeyPress:
                self._suspend_auto_scroll()
            elif etype == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._auto_scroll_mouse_press_pos = (
                        int(event.position().x()),
                        int(event.position().y()),
                    )
                self._suspend_auto_scroll()
            elif etype == QEvent.Type.MouseMove:
                if self._auto_scroll_mouse_press_pos is not None:
                    dx = int(event.position().x()) - self._auto_scroll_mouse_press_pos[0]
                    dy = int(event.position().y()) - self._auto_scroll_mouse_press_pos[1]
                    if dx * dx + dy * dy > 100:  # 10px threshold
                        self._suspend_auto_scroll()
            elif etype == QEvent.Type.MouseButtonRelease:
                self._auto_scroll_mouse_press_pos = None
                self._suspend_auto_scroll()
        return False

    def _clamp_queue_delay(self, queue_delay_ms: int) -> int:
        """A12：卡顿补偿值钳制到 500ms 上限。

        超过 500ms 说明 UI 线程发生了严重卡顿——旧实现直接清零，会让时间戳
        比可听时刻晚一整个卡顿时长；这里改为钳制到 500ms，并弹一次 InfoBar
        提醒（按时间节流，防止连续按键刷屏）。
        """
        if queue_delay_ms <= 500:
            return queue_delay_ms
        now = time.monotonic()
        if now >= getattr(self, "_queue_delay_warn_after_s", 0.0):
            self._queue_delay_warn_after_s = now + 10.0
            InfoBar.warning(
                title=self.tr("界面卡顿"),
                content=self.tr("检测到界面卡顿超过 500ms，打轴时间可能存在偏差"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        return 500

    def keyPressEvent(self, a0: Optional[QKeyEvent]):
        if a0 is None:
            return
        # 所有键盘操作挂起自动滚动（Play 按钮走 _on_play，不经过这里）
        self._suspend_auto_scroll()
        self._action_from_keyboard = True
        try:
            self._keyPressEvent_impl(a0)
        finally:
            self._action_from_keyboard = False

    def _keyPressEvent_impl(self, a0: QKeyEvent):
        # 记录 handler 进入时刻（time.monotonic 同一时钟源）。
        # 注意：这里测的是“handler 入口 → 读取音频位置”之间的同步处理耗时，
        # 不是事件在 Qt 队列里排队等待的时间（旧版 a0.timestamp() 那种语义已废弃，
        # 因其与 QPC 跨时钟会引入稳定的固定偏移）。UI 卡顿导致的排队等待不在此补偿范围内。
        handler_entry_s = time.monotonic()
        key = a0.key()
        modifiers = a0.modifiers()
        playing = bool(self._timing_service and self._timing_service.is_playing())

        # Ctrl 快捷键（系统级，优先处理）
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z:
                self._on_undo()
                a0.accept()
                return
            elif key == Qt.Key.Key_Y:
                self._on_redo()
                a0.accept()
                return
            elif key == Qt.Key.Key_S:
                self._on_save()
                a0.accept()
                return
            elif key == Qt.Key.Key_V:
                self._on_paste_lyrics()
                a0.accept()
                return
            elif key == Qt.Key.Key_C:
                self._on_copy_chars()
                a0.accept()
                return
            # 其他 Ctrl 组合键：不直接 return，继续走 key_map 查找

        # Convert Qt key to string name for mapping lookup
        key_name = self._qt_key_to_name(key, modifiers, a0.nativeVirtualKey(), a0.nativeScanCode())
        if not key_name:
            super().keyPressEvent(a0)
            return

        key_upper = key_name.upper()
        action_short = self._key_map_short.get(key_upper)
        action_long = self._key_map_long.get(key_upper)
        # Fallback to default key map only if settings not loaded yet
        if not self._settings_loaded and action_short is None and action_long is None:
            action_short = self._default_key_action(key, modifiers, playing=playing)

        # tag_now / tag_now_extra 使用 press/release 语义，立即执行，不走长按检测
        if action_short in ("tag_now", "tag_now_extra") or action_long in ("tag_now", "tag_now_extra"):
            # A3：auto-repeat 过滤必须先于 not playing 分支——否则未播放时
            # 长按一次会连发几十次 _add_checkpoint 结构编辑
            if a0.isAutoRepeat():
                a0.ignore()
                return
            # A4：注音分析完成后整体替换 sentences，分析期间拒绝打轴
            if self._reject_during_ruby_analysis():
                a0.accept()
                return
            if not playing:
                self._add_checkpoint()
                a0.accept()
                return
            if self._timing_service and key_name not in self._pressed_keys:
                # 按键音：普通 cp → 按下时播放 press；停顿点 cp → 忽略（等 release）
                if self._keysound_player is not None:
                    if not self._timing_service.is_current_cp_sentence_end_tail():
                        self._keysound_player.play_press()
                try:
                    self._pressed_keys.add(key_name)
                    # handler 入口到此刻的同步处理耗时（非 Qt 队列等待时间）
                    queue_delay_ms = max(0, int((time.monotonic() - handler_entry_s) * 1000))
                    queue_delay_ms = self._clamp_queue_delay(queue_delay_ms)
                    self._timing_service.on_timing_key_pressed(key_name, queue_delay_ms)
                except Exception as e:
                    self._pressed_keys.discard(key_name)
                    self._show_runtime_error(str(e))
            a0.accept()
            return

        # tag_and_delete_next：同样使用 press/release 语义，不走长按检测
        if action_short == "tag_and_delete_next" or action_long == "tag_and_delete_next":
            # A3：同上，auto-repeat 过滤先于 not playing 分支
            if a0.isAutoRepeat():
                a0.ignore()
                return
            # A4：注音分析期间拒绝打轴
            if self._reject_during_ruby_analysis():
                a0.accept()
                return
            if not playing:
                self._add_checkpoint()
                a0.accept()
                return
            if self._timing_service and key_name not in self._pressed_keys:
                if self._keysound_player is not None:
                    if not self._timing_service.is_current_cp_sentence_end_tail():
                        self._keysound_player.play_press()
                try:
                    self._pressed_keys.add(key_name)
                    queue_delay_ms = max(0, int((time.monotonic() - handler_entry_s) * 1000))
                    queue_delay_ms = self._clamp_queue_delay(queue_delay_ms)
                    self._timing_service.on_tag_and_delete_next_pressed(key_name, queue_delay_ms)
                except Exception as e:
                    self._pressed_keys.discard(key_name)
                    self._show_runtime_error(str(e))
            a0.accept()
            return

        # 同一物理按下产生的系统自动重复事件不得重新开始长按手势。
        # Qt 在自动重复时会成对发送 release/press；若重启定时器，
        # release 会被误判为短按，并且长按动作也可能被重复触发。
        if action_long is not None and a0.isAutoRepeat():
            a0.ignore()
            return

        # 只有 short 绑定：立即执行，保留 isAutoRepeat 行为
        if action_short is not None and action_long is None:
            self._execute_action(action_short, key)
            a0.accept()
            return

        # 有 long 绑定（可能同时有 short）：启动定时器等待区分
        if action_long is not None:
            # A11：pending 按键名保存，多键快速连按时互不覆盖；定时器始终
            # 跟踪最早未决的键，首键短按不再被后续按键吞掉。
            if not self._long_press_timer.isActive():
                self._long_press_key = key_upper
                self._long_press_timer.start()
            self._pending_presses[key_upper] = (action_short, action_long)
            a0.accept()
            return

        # 无绑定的按键
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # 如果焦点在 QLineEdit 上（如偏移输入框），不拦截回车
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit):
                return
            self._insert_line_break_at_current()
            a0.accept()
            return
        elif key == Qt.Key.Key_Delete:
            self._delete_current_selection_or_char()
            a0.accept()
            return
        else:
            super().keyPressEvent(a0)

    def keyReleaseEvent(self, a0: Optional[QKeyEvent]):
        if a0 is None:
            return
        # handler 进入时刻；queue_delay_ms 测的是入口→读位置的同步处理耗时，
        # 非 Qt 队列等待（详见 _keyPressEvent_impl 处说明）。
        handler_entry_s = time.monotonic()
        key = a0.key()
        modifiers = a0.modifiers()
        key_name = self._qt_key_to_name(key, modifiers, a0.nativeVirtualKey(), a0.nativeScanCode())
        if not key_name:
            super().keyReleaseEvent(a0)
            return

        key_upper = key_name.upper()

        # tag_now / tag_now_extra 释放处理
        action_short = self._key_map_short.get(key_upper)
        action_long = self._key_map_long.get(key_upper)
        if action_short in ("tag_now", "tag_now_extra") or action_long in ("tag_now", "tag_now_extra"):
            if not (self._timing_service and self._timing_service.is_playing()):
                # A7：提前返回前无条件摘键——按住打轴键期间停止/暂停播放时
                # release 会走到这里，键若留在 _pressed_keys 会吞掉下次播放
                # 同键的 press（表现成"莫名丢一拍"）
                self._pressed_keys.discard(key_name)
                a0.accept()
                return
            if a0.isAutoRepeat():
                a0.ignore()
                return
            if self._timing_service and key_name in self._pressed_keys:
                # 按键音：停顿点 cp → 抬起时播放 release；普通 cp → 忽略
                if self._keysound_player is not None:
                    if self._timing_service.is_current_cp_sentence_end_tail():
                        self._keysound_player.play_release()
                try:
                    # handler 入口到此刻的同步处理耗时（非 Qt 队列等待时间）
                    queue_delay_ms = max(0, int((time.monotonic() - handler_entry_s) * 1000))
                    queue_delay_ms = self._clamp_queue_delay(queue_delay_ms)
                    self._timing_service.on_timing_key_released(key_name, queue_delay_ms)
                except Exception as e:
                    self._show_runtime_error(str(e))
                finally:
                    self._pressed_keys.discard(key_name)
            a0.accept()
            return

        # tag_and_delete_next 释放处理
        if action_short == "tag_and_delete_next" or action_long == "tag_and_delete_next":
            if not (self._timing_service and self._timing_service.is_playing()):
                # A7：同上，提前返回前无条件摘键
                self._pressed_keys.discard(key_name)
                a0.accept()
                return
            if a0.isAutoRepeat():
                a0.ignore()
                return
            if self._timing_service and key_name in self._pressed_keys:
                if self._keysound_player is not None:
                    if self._timing_service.is_current_cp_sentence_end_tail():
                        self._keysound_player.play_release()
                try:
                    queue_delay_ms = max(0, int((time.monotonic() - handler_entry_s) * 1000))
                    queue_delay_ms = self._clamp_queue_delay(queue_delay_ms)
                    self._timing_service.on_tag_and_delete_next_released(key_name, queue_delay_ms)
                except Exception as e:
                    self._show_runtime_error(str(e))
                finally:
                    self._pressed_keys.discard(key_name)
            a0.accept()
            return

        # Qt 自动重复的中间 release 不是用户真正抬键，必须在
        # 短按判定之前过滤，否则长按期间会连续执行 short 动作。
        if a0.isAutoRepeat():
            a0.ignore()
            return

        # 长按/短按释放处理
        pending = self._pending_presses.pop(key_upper, None)
        if pending is not None and self._long_press_timer.isActive():
            # 定时器仍在运行 = 短按（300ms 内释放）
            if self._long_press_key == key_upper:
                self._long_press_timer.stop()
                self._long_press_key = None
                # A11：其余键还在 pending 时，为下一个键重启短按判定窗口
                if self._pending_presses:
                    self._long_press_key = next(iter(self._pending_presses))
                    self._long_press_timer.start()
            action = pending[0]
            if action:
                self._execute_action(action, key)
            a0.accept()
            return

        # 长按已超时，pending 已被 _on_long_press_timeout 清除，忽略释放
        super().keyReleaseEvent(a0)

    def _qt_key_to_name(
        self,
        key,
        modifiers=Qt.KeyboardModifier.NoModifier,
        native_virtual_key: int = 0,
        native_scan_code: int = 0,
    ) -> Optional[str]:
        """Convert Qt key enum to string name for shortcut mapping.

        支持组合键，如 CTRL+F4、ALT+A、SHIFT+Z 等。
        macOS 上 ``QKeyEvent.key()`` 对符号按键可能返回 0，
        此时通过 ``native_virtual_key`` + Carbon kVK 映射做 fallback。
        """
        parts = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("CTRL")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("ALT")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("SHIFT")

        _key_names = {
            Qt.Key.Key_Space: "SPACE",
            Qt.Key.Key_Escape: "ESCAPE",
            Qt.Key.Key_F1: "F1",
            Qt.Key.Key_F2: "F2",
            Qt.Key.Key_F3: "F3",
            Qt.Key.Key_F4: "F4",
            Qt.Key.Key_F5: "F5",
            Qt.Key.Key_F6: "F6",
            Qt.Key.Key_F7: "F7",
            Qt.Key.Key_F8: "F8",
            Qt.Key.Key_F9: "F9",
            Qt.Key.Key_F10: "F10",
            Qt.Key.Key_F11: "F11",
            Qt.Key.Key_F12: "F12",
            Qt.Key.Key_Up: "UP",
            Qt.Key.Key_Down: "DOWN",
            Qt.Key.Key_Left: "LEFT",
            Qt.Key.Key_Right: "RIGHT",
            Qt.Key.Key_Return: "ENTER",
            Qt.Key.Key_Enter: "ENTER",
            Qt.Key.Key_Tab: "TAB",
            Qt.Key.Key_Backspace: "BACKSPACE",
            Qt.Key.Key_Delete: "DELETE",
            Qt.Key.Key_Home: "HOME",
            Qt.Key.Key_End: "END",
            Qt.Key.Key_PageUp: "PAGEUP",
            Qt.Key.Key_PageDown: "PAGEDOWN",
            Qt.Key.Key_Insert: "INSERT",
            # 标点键（#11 修复：支持字面量键名，与 _KeyCaptureButton 保持一致）
            # 逗号用占位名 COMMA，必须与 _KeyCaptureButton._build_key_name 一致，
            # 否则配置里存的 "COMMA" 与按键事件解析结果匹配不上。
            Qt.Key.Key_Comma: "COMMA",
            Qt.Key.Key_Period: ".",
            Qt.Key.Key_Slash: "/",
            Qt.Key.Key_Semicolon: ";",
            Qt.Key.Key_Apostrophe: "'",
            Qt.Key.Key_BracketLeft: "[",
            Qt.Key.Key_BracketRight: "]",
            Qt.Key.Key_Backslash: "\\",
            Qt.Key.Key_Minus: "-",
            Qt.Key.Key_Equal: "=",
            Qt.Key.Key_QuoteLeft: "`",
        }
        if key in _key_names:
            parts.append(_key_names[key])
        elif Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            parts.append(chr(key))
        elif Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            parts.append(chr(key))
        elif native_virtual_key:
            from strange_uta_game.frontend.macos_keymap import (
                macos_vk_to_key_name,
            )
            vk_name = macos_vk_to_key_name(native_virtual_key, native_scan_code)
            if vk_name:
                parts.append(vk_name)
            else:
                return None
        else:
            return None
        return "+".join(parts) if parts else None

    def _default_key_action(
        self,
        key,
        modifiers=Qt.KeyboardModifier.NoModifier,
        *,
        playing: bool = False,
    ) -> Optional[str]:
        """Return the current mode's default short-press action.

        This path is used only before settings finish loading.  Build it from
        the same per-mode defaults as the settings page so startup behavior
        cannot drift from the shipped shortcut layout.
        """
        key_name = self._qt_key_to_name(key, modifiers)
        if not key_name:
            return None
        mode_key = "timing_mode" if playing else "edit_mode"
        target = key_name.upper()
        for action, raw in self._mode_shortcut_defaults(mode_key).items():
            for binding in (raw or "").split(","):
                binding = binding.strip()
                if not binding:
                    continue
                parts = binding.rsplit(":", 1)
                bound_key = parts[0].strip().upper()
                trigger = parts[1].strip().lower() if len(parts) > 1 else "short"
                if trigger == "short" and bound_key == target:
                    return action
        return None

    # ==================== TimingService 回调 ====================

    def on_timetag_added(
        self,
        singer_id: str,
        line_idx: int,
        char_idx: int,
        checkpoint_idx: int,
        timestamp_ms: int,
    ) -> None:
        _ = singer_id, timestamp_ms
        log_perf_event(
            "editor.timetag_added",
            line=line_idx,
            char=char_idx,
            cp=checkpoint_idx,
            ts=timestamp_ms,
        )
        # 透传 (line, char, cp) 给增量追加路径（用全局时间戳从模型回读，避免偏移换算）
        self._timetag_added_signal.emit(line_idx, char_idx, checkpoint_idx)

    def on_position_changed(
        self, position_ms: int, duration_ms: int, singer_positions
    ) -> None:
        self._position_changed_signal.emit(position_ms, duration_ms, singer_positions)

    def on_singer_changed(self, new_singer_id: str, prev_singer_id: str) -> None:
        _ = new_singer_id, prev_singer_id

    def on_checkpoint_moved(self, position: CheckpointPosition) -> None:
        self._checkpoint_moved_signal.emit(position)

    def on_timing_error(self, error_type: str, message: str) -> None:
        self._timing_error_signal.emit(error_type, message)

    def pause_poll_for_page_animation(self, duration_ms: int = 350) -> None:
        """页面切换动画期间暂停位置拉取定时器。

        停止 timer 后，transport/timeline/preview 不会收到新的 setValue/update 调用，
        qfluentwidgets Slider 的内部动画也因此没有新目标追逐，会迅速收敛并停止重绘。
        页面动画结束后（duration_ms 毫秒）自动恢复，不影响打轴精度。
        """
        if not self._position_poll_timer.isActive():
            return
        self._position_poll_timer.stop()
        QTimer.singleShot(
            duration_ms,
            self._resume_poll_after_page_animation,
        )

    def _resume_poll_after_page_animation(self) -> None:
        """动画结束后恢复轮询，或补做暂停期间遗漏的结束同步。"""
        if not self._timing_service:
            return
        if self._timing_service.is_playing():
            self._position_poll_timer.start()
            return

        # 音频可能在页面动画暂停轮询的这段时间内自然结束。旧逻辑只在仍播放时
        # 重启 timer，导致结束分支永远没有机会运行，模式指示器和快捷键表会一直
        # 留在打轴模式。显式 pause/stop 已经同步过完整 UI，只需刷新模式即可。
        if self._status_state == "playing":
            self._poll_audio_position()
        else:
            self._update_mode_indicator()

    def _position_poll_hidden(self) -> bool:
        """播放头轮询的"隐藏态"：窗口最小化/隐藏，或宿主隐藏了 SUG 区域。

        嵌入式模式下 self.window() 是宿主顶层窗口，宿主隐藏 SUG 区域时宿主
        窗口仍可见，因此除自身窗口外还参考节流器的全局判定（含宿主经
        on_host_visibility_changed / set_visibility_override 的显式通知）。
        """
        window = self.window()
        return (
            window is None
            or window.isMinimized()
            or not window.isVisible()
            or not ui_visible()
        )

    def _refresh_position_poll_interval(self) -> None:
        """按窗口可见性与锁定区间调整播放头轮询频率。

        窗口可见（哪怕失焦——用户可能一边看歌词预览一边在别的窗口干活）→
        用户设置的刷新率；窗口最小化/隐藏且无锁定终点 → 200ms 仅维持播放
        结束检测与状态栏。锁定终点是音频行为：越过终点后 seek+暂停的时机
        由本定时器决定，隐藏期间也必须保持高频检测，否则音频会越过终点约
        一个后台轮询间隔（200ms），违背"音频服务不降频"。setInterval 对
        运行中的定时器即时生效。
        """
        interval_ms = (
            self._position_poll_bg_interval_ms
            if self._position_poll_hidden() and self._playback_range_end_ms is None
            else self._position_poll_fg_interval_ms
        )
        self._position_poll_timer.setInterval(interval_ms)

    @log_slow_method(
        "editor.poll_audio_position",
        20,
        lambda self, args, kwargs: {
            "position_ms": self._timing_service.get_position_ms() if self._timing_service else None,
            "line": self._current_line_idx,
        },
    )
    def _poll_audio_position(self) -> None:
        """UI 线程 QTimer 主动拉取音频位置（替代旧的回调线程+信号推送）。

        直接从音频引擎获取基于 perf_counter 外推的高精度位置，
        消除多层异步排队带来的延迟和抖动。
        """
        if not self._timing_service:
            return
        if perf_enabled():
            now_s = perf_counter()
            last_s = getattr(self, "_perf_last_poll_s", None)
            if last_s is not None:
                gap_ms = (now_s - last_s) * 1000.0
                if gap_ms >= 80:
                    log_perf_event(
                        "editor.poll_gap",
                        gap_ms=f"{gap_ms:.1f}",
                        line=self._current_line_idx,
                    )
            self._perf_last_poll_s = now_s
        engine = self._timing_service._audio_engine
        position_ms = self._timing_service.get_position_ms()
        duration_ms = self._timing_service.get_duration_ms()
        reached_locked_end = (
            engine.is_playing()
            and self._playback_range_end_ms is not None
            and position_ms >= self._playback_range_end_ms
        )
        if reached_locked_end:
            position_ms = self._playback_range_end_ms
            self._timing_service.seek(position_ms)

        # 页面切换动画期间（self.y() != 0）跳过 UI 重绘，避免与动画争抢导致
        # 控件抖动；窗口隐藏时没人看得见，同样跳过（锁定终点生效时轮询保持
        # 高频，但 tick 内只做检测不刷新控件）。位置读取和播放结束检测不受
        # 影响，不影响打轴精度。
        if self.y() == 0 and not self._position_poll_hidden():
            if duration_ms != self._last_polled_duration_ms:
                self.transport.set_duration(duration_ms)
                self.timeline.set_duration(duration_ms)
                self.preview.set_duration(duration_ms)
                self._last_polled_duration_ms = duration_ms
            self.transport.set_position(position_ms)
            self.timeline.set_position(position_ms)
            self.preview.set_current_time_ms(position_ms)

        if reached_locked_end:
            self._on_pause()
            self._status_state = "range_finished"
            self.lbl_status.setText(self.tr("已到达锁定终点"))
            return

        # 检测播放结束（位置到达末尾或引擎已停止）
        if not engine.is_playing():
            self.transport.set_playing(False)
            self.preview.set_playing(False)
            self.timeline.set_playing(False)
            self._status_state = "finished"
            self.lbl_status.setText(self.tr("播放完毕"))
            self._update_mode_indicator()
            # 重置自动滚动状态
            self._auto_scroll_suspended = False
            self._auto_scroll_new_line_reached = False
            self._auto_scroll_cooldown_timer.stop()
            # 节拍器随播放完毕停响（调度线程自退，不阻塞 UI）
            metronome = getattr(self, "_metronome", None)
            if metronome is not None:
                metronome.stop()
            # 停止位置拉取定时器
            self._position_poll_timer.stop()
            # A7：播放结束（引擎自然停止）时清掉仍按住的打轴键，
            # 悬挂键会吞掉下次播放同键的 press
            self._pressed_keys.clear()
            # 切换到编辑模式时校验所有行时间戳
            self._validate_all_timestamps()

    # ==================== 自动滚动状态机 ====================

    def _on_cycle_scroll_mode(self):
        """按钮点击：循环切换滚动模式 auto → always → never → auto，并持久化。"""
        modes = ["auto", "always", "never"]
        self._scroll_mode = modes[(modes.index(self._scroll_mode) + 1) % len(modes)]
        self._sync_scroll_mode()
        # 持久化到 config
        setting_iface = self._get_setting_interface()
        if setting_iface is not None:
            s = setting_iface.get_settings()
            s.set("timing.scroll_mode", self._scroll_mode)
            s.save()

    _SCROLL_MODE_LABELS = {"auto": "自动滚动", "always": "始终滚动", "never": "从不滚动"}

    def _scroll_mode_label(self, mode: str) -> str:
        # 显式 tr 调用，让 .ts 提取器抓到这三个源串
        if mode == "always":
            return self.tr("始终滚动")
        if mode == "never":
            return self.tr("从不滚动")
        return self.tr("自动滚动")

    def _sync_scroll_mode(self):
        """将当前 _scroll_mode 同步到按钮文字、颜色和 preview。"""
        self.btn_scroll_mode.setText(self._scroll_mode_label(self._scroll_mode))
        self._update_scroll_mode_btn_style()
        self.preview.set_scroll_mode(self._scroll_mode)
        # 切换到 always / auto 时：重置挂起状态并立刻滚动到当前播放行
        if self._scroll_mode in ("always", "auto"):
            self._auto_scroll_suspended = False
            self._auto_scroll_new_line_reached = False
            self._auto_scroll_cooldown_timer.stop()
            self.preview.resume_auto_scroll()

    def _update_scroll_mode_btn_style(self):
        """根据当前滚动模式刷新按钮颜色。

        使用 qfluentwidgets setCustomStyleSheet 将颜色 QSS 追加到按钮原有样式之后，
        不覆盖 border / padding 等布局属性，避免图标错位。
        浅色和深色两套 QSS 分别传入，qfluentwidgets 主题切换时自动选用对应版本。
        """
        tc_l = ThemeColors(is_dark=False)
        tc_d = ThemeColors(is_dark=True)
        _bgs = {
            "auto":   (tc_l.accent_secondary, tc_d.accent_secondary),  # 蓝
            "always": (tc_l.status_complete,  tc_d.status_complete),   # 绿
            "never":  (tc_l.status_none,      tc_d.status_none),       # 灰
        }
        bg_l, bg_d = _bgs.get(self._scroll_mode, _bgs["auto"])

        def make_qss(bg) -> str:
            lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
            text = "#1a1a1a" if lum > 150 else "#ffffff"
            return (
                f"#btnScrollMode {{"
                f" background-color: {bg.name()};"
                f" color: {text};"
                f"}}"
                f" #btnScrollMode:hover {{"
                f" background-color: {bg.lighter(115).name()};"
                f"}}"
                f" #btnScrollMode:pressed {{"
                f" background-color: {bg.darker(110).name()};"
                f"}}"
            )

        setCustomStyleSheet(self.btn_scroll_mode, make_qss(bg_l), make_qss(bg_d))

    def _suspend_auto_scroll(self):
        """挂起自动滚动：重置冷却状态，通知 preview 暂停。"""
        if self._scroll_mode == "always":
            return
        self._auto_scroll_suspended = True
        self._auto_scroll_new_line_reached = False
        self._auto_scroll_cooldown_timer.stop()
        self.preview._suspend_auto_scroll()

    def _on_user_interaction_during_auto_scroll(self):
        """preview 用户交互信号的槽：同步挂起状态并停止冷却计时器。"""
        self._auto_scroll_suspended = True
        self._auto_scroll_new_line_reached = False
        self._auto_scroll_cooldown_timer.stop()

    def _on_auto_scroll_line_changed(self):
        """preview 自动滚动换行信号的槽：标记新行已到达，启动 3s 冷却。"""
        if self._auto_scroll_suspended:
            self._auto_scroll_new_line_reached = True
            if not self._auto_scroll_cooldown_timer.isActive():
                self._auto_scroll_cooldown_timer.start()

    def _on_auto_scroll_cooldown_timeout(self):
        """冷却超时：若播放已到达新行，恢复自动滚动。"""
        if self._auto_scroll_suspended and self._auto_scroll_new_line_reached:
            self._auto_scroll_suspended = False
            self._auto_scroll_new_line_reached = False
            self.preview.resume_auto_scroll()

    # ========================================================

    def _handle_position_changed(
        self, position_ms: int, duration_ms: int, singer_positions
    ):
        playing = bool(self._timing_service and self._timing_service.is_playing())
        if playing != getattr(self, "_shortcut_mode_playing", None):
            # 音频引擎回调本身就是播放状态发生变化时最早、最可靠的 UI 通知。
            # 模式同步必须放在 60fps 位置节流之前，否则自然结束的最后一次回调
            # 可能只刷新播放控件，快捷键表却仍停留在打轴模式。
            self._update_mode_indicator()

        # 60fps UI 节流：跳过间隔 < 16ms 的更新
        now = time.monotonic()
        if now - self._last_position_update_time < 0.016:
            return
        self._last_position_update_time = now

        _ = singer_positions
        if duration_ms != self._last_polled_duration_ms:
            self.transport.set_duration(duration_ms)
            self.timeline.set_duration(duration_ms)
            self.preview.set_duration(duration_ms)
            self._last_polled_duration_ms = duration_ms
        self.transport.set_position(position_ms)
        self.timeline.set_position(position_ms)
        self.preview.set_current_time_ms(position_ms)
        if self._timing_service:
            self.transport.set_playing(playing)
            self.preview.set_playing(playing)

    def _handle_checkpoint_moved(self, position: CheckpointPosition):
        self._apply_checkpoint_position(position)
        self._render_cached_status()
    
    def _handle_foucus_moved(self, line_idx: int, char_idx: int):
        self.preview.set_focus_position(line_idx, char_idx)

    def _handle_center_current_line(self):
        self.preview.scroll_current_line_to_center()

    @log_slow_method(
        "editor.handle_timetag_added",
        12,
        lambda self, args, kwargs: {"line": args[0] if args else kwargs.get("line_idx")},
    )
    def _handle_timetag_added(self, line_idx: int, char_idx: int = -1, cp_idx: int = -1):
        if self._project:
            self.preview._invalidate_line_and_dependents(line_idx)
        # 波形隐藏时只标脏（连增量都省）；显示时优先走顺序打轴增量追加，失败再回退全量
        if hasattr(self, "timeline") and not self.timeline.is_waveform_visible():
            self._timetags_dirty_while_hidden = True
        elif not self._try_incremental_append(line_idx, char_idx, cp_idx):
            self._schedule_time_tags_update()
        if hasattr(self, "_status_line_cache"):
            EditorInterface._update_status_line(self, line_idx)
        else:
            # Compatibility for embedded/lightweight callers that construct the
            # editor callback surface without running QWidget.__init__.
            self._update_status()

    def _try_incremental_append(self, line_idx: int, char_idx: int, cp_idx: int) -> bool:
        """顺序打轴：把新增的单个 checkpoint 直接增量插入波形，绕过 collect + 全量重建。

        从模型回读全局时间戳/字符/注音（与 collect 同源），交给 WaveformDisplay.try_append_tag
        做"文件序末尾"判定；非末尾/已存在/信息不全则返回 False，由调用方回退全量。
        """
        if not self._project or char_idx < 0 or cp_idx < 0:
            return False
        if not (0 <= line_idx < len(self._project.sentences)):
            return False
        sentence = self._project.sentences[line_idx]
        if not (0 <= char_idx < len(sentence.characters)):
            return False
        ch = sentence.characters[char_idx]
        is_end = ch.is_sentence_end and cp_idx == ch.check_count
        if is_end:
            global_ts = ch.global_sentence_end_ts
            ruby = None
        else:
            if cp_idx >= len(ch.global_timestamps):
                return False
            global_ts = ch.global_timestamps[cp_idx]
            ruby = ch.ruby.parts[cp_idx].text if ch.ruby and cp_idx < len(ch.ruby.parts) else None
        if global_ts is None:
            return False
        return self.timeline.try_add_tag(
            global_ts, ch.char, line_idx, char_idx, cp_idx, is_end, ruby
        )

    def _handle_timing_error(self, error_type: str, message: str):
        InfoBar.warning(
            title=error_type,
            content=message,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    # ==================== 辅助 ====================

    def _update_selected_checkpoint(
        self,
        line_idx: int,
        char_idx: int,
        cp_idx: Optional[int],
    ) -> None:
        """统一入口：更新 cp 选中态（UI 状态 + domain 选中状态）。

        Issue #9 第十六批架构性修复：
        - UI 侧 preview._current_checkpoint_idx 仍维持（用于渲染判断兼容旧路径）
        - Domain 侧 Project.set_selected_checkpoint 维持全局单选不变量 I1
        - 渲染时 paintEvent 直接读 char.selected_checkpoint_idx → singer.complement_color
          单管道上色，不再需要"选中分支 + HSV 运行时补色 + 额外 drawText"

        调用点覆盖所有 cp 切换事件（除 F5/F6 增减 cp 外，按用户约定不触发）：
        - _apply_checkpoint_position（TimingService 主通路）
        - _sync_after_structure_change（结构编辑后）
        - _on_char_selected 无 cp 分支的直接 set_current_position
        """
        self.preview._current_checkpoint_idx = cp_idx
        if self._project is None or cp_idx is None:
            # cp_idx=None 时不清 project 选中态：保持旧选中直到下次有效切换。
            # 这是因为某些路径（空项目、无 cp 字符）传 None 只代表"当前字符没 cp"，
            # 不代表"用户想取消选中"。
            return
        self._project.set_selected_checkpoint(line_idx, char_idx, cp_idx)

    def _apply_checkpoint_position(self, position: CheckpointPosition):
        if not self._project or not self._project.sentences:
            self._current_line_idx = 0
            self.preview._current_checkpoint_idx = None
            self._update_line_info()
            return

        new_line_idx = max(0, min(position.line_idx, len(self._project.sentences) - 1))
        # 行切换时校验上一行的时间戳
        if new_line_idx != self._current_line_idx:
            if 0 <= self._current_line_idx < len(self._project.sentences):
                self._validate_line_timestamps(self._current_line_idx)
        self._current_line_idx = new_line_idx
        self._update_selected_checkpoint(new_line_idx, position.char_idx, position.checkpoint_idx)
        # cp 标记点击路径：跳过光标移动，保持 selected_char 不被污染。
        # 仍需要刷新 preview 显示以反映新的 selected_cp 高亮。
        if self._suppress_cp_cursor_move:
            self.preview.request_repaint()
        else:
            self.preview.set_current_position(new_line_idx, position.char_idx)
        self._update_line_info()

    def _show_runtime_error(self, message: str):
        InfoBar.error(
            title=self.tr("操作失败"),
            content=message,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _update_line_info(self):
        if self._project and self._project.sentences:
            total = len(self._project.sentences)
            idx = min(self._current_line_idx, total - 1)
            text = self._project.sentences[idx].text
            preview = text[:30] + "..." if len(text) > 30 else text
            # 显示选中字符的时间戳信息
            char_info = ""
            char_idx = self.preview._current_char_idx
            sentence = self._project.sentences[idx]
            if 0 <= char_idx < len(sentence.characters):
                ch = sentence.characters[char_idx]
                total_chars = len(sentence.characters)
                # 使用带 global_offset 的时间戳，与实际渲染/导出预览一致
                ts_parts = []
                for ts in ch.global_timestamps:
                    m, s = divmod(ts // 1000, 60)
                    ms = ts % 1000
                    ts_parts.append(f"{m:02d}:{s:02d}.{ms:03d}")
                if ch.is_sentence_end and ch.global_sentence_end_ts is not None:
                    ets = ch.global_sentence_end_ts
                    m, s = divmod(ets // 1000, 60)
                    ms = ets % 1000
                    ts_parts.append(self.tr("停顿点{m:02d}:{s:02d}.{ms:03d}").format(m=m, s=s, ms=ms))
                if ts_parts:
                    char_info = self.tr(" | 字 {n}/{total} | 「{ch}」 {tags}").format(
                        n=char_idx + 1, total=total_chars, ch=ch.char, tags=', '.join(ts_parts))
                else:
                    char_info = self.tr(" | 字 {n}/{total} | 「{ch}」 未打轴").format(
                        n=char_idx + 1, total=total_chars, ch=ch.char)
            self.lbl_line_info.setText(self.tr("行 {idx}/{total}: {preview}{char_info}").format(
                idx=idx + 1, total=total, preview=preview, char_info=char_info))
        else:
            self.lbl_line_info.setText(self.tr("当前行: -"))

    def _update_time_tags_display(self):
        if not self._project:
            return
        # 波形隐藏时跳过 collect + set_time_tags 全量重建（隐藏控件 Qt 也不会绘制），
        # 只标脏；重新显示时在 _on_waveform_visibility_changed 里补一次。既省 CPU，
        # 也减少打轴主线程占用 → 间接降低时间戳抖动。
        if hasattr(self, "timeline") and not self.timeline.is_waveform_visible():
            self._timetags_dirty_while_hidden = True
            return
        _perf_start = perf_counter() if perf_enabled() else None
        if self._time_tags_update_timer.isActive():
            self._time_tags_update_timer.stop()
        # 使用渲染时间戳（带偏移），与波形显示对齐
        tags = self._project.collect_all_global_timestamp_ms_with_chars()
        self.timeline.set_time_tags(tags)
        if _perf_start is not None:
            log_elapsed(
                "editor.update_time_tags_display",
                _perf_start,
                12,
                tags=len(tags),
                lines=len(self._project.sentences),
            )

    def _schedule_time_tags_update(self, delay_ms: int = 33):
        if not self._project:
            return
        if delay_ms <= 0:
            self._update_time_tags_display()
            return
        if not self._time_tags_update_timer.isActive():
            self._time_tags_update_timer.start(delay_ms)

    def _update_status(self):
        self._rebuild_status_cache()
        self._render_cached_status()

    @staticmethod
    def _status_for_sentence(sentence) -> tuple[bool, bool, int]:
        meaningful = any(c.total_timing_points > 0 for c in sentence.characters)
        timed = meaningful and sentence.has_timetags
        needs_guide = sum(1 for c in sentence.characters if c.needs_guide)
        return meaningful, timed, needs_guide

    def _rebuild_status_cache(self) -> None:
        self._status_line_cache = {}
        self._status_meaningful_total = 0
        self._status_timed_total = 0
        self._status_needs_guide_total = 0
        if not self._project:
            self._status_cache_project_id = None
            return
        self._status_cache_project_id = id(self._project)
        for line_idx, sentence in enumerate(self._project.sentences):
            state = self._status_for_sentence(sentence)
            self._status_line_cache[line_idx] = state
            self._status_meaningful_total += int(state[0])
            self._status_timed_total += int(state[1])
            self._status_needs_guide_total += state[2]

    def _status_cache_is_valid(self) -> bool:
        return bool(
            self._project
            and self._status_cache_project_id == id(self._project)
            and len(self._status_line_cache) == len(self._project.sentences)
        )

    def _update_status_line(self, line_idx: int) -> None:
        if not self._project:
            self._render_cached_status()
            return
        if not self._status_cache_is_valid():
            self._rebuild_status_cache()
        if not (0 <= line_idx < len(self._project.sentences)):
            self._render_cached_status()
            return
        old = self._status_line_cache.get(line_idx, (False, False, 0))
        new = self._status_for_sentence(self._project.sentences[line_idx])
        self._status_line_cache[line_idx] = new
        self._status_meaningful_total += int(new[0]) - int(old[0])
        self._status_timed_total += int(new[1]) - int(old[1])
        self._status_needs_guide_total += new[2] - old[2]
        self._render_cached_status()

    def _render_cached_status(self) -> None:
        if not self._project:
            self.lbl_progress.setText(self.tr("行: 0/0 | 进度: 0%"))
            if hasattr(self, "lbl_needs_guide"):
                self.lbl_needs_guide.setText("")
            return
        if not self._status_cache_is_valid():
            self._rebuild_status_cache()
        total = self._status_meaningful_total
        timed = self._status_timed_total
        pct = int(timed / total * 100) if total > 0 else 0
        self.lbl_progress.setText(
            self.tr("行: {total} | 已打轴: {timed}/{total_again} ({pct}%)").format(
                total=total, timed=timed, total_again=total, pct=pct
            )
        )
        # 待添加导唱符计数：>0 显示，=0 隐藏（避免视觉噪音）
        if hasattr(self, "lbl_needs_guide"):
            n = self._status_needs_guide_total
            self.lbl_needs_guide.setText(
                self.tr("待添加导唱符：{n}").format(n=n) if n > 0 else ""
            )

    def refresh_lyric_display(self):
        self.preview._update_display()

    def _auto_analyze_rubies(self, only_noruby: bool = False, auto_detect_chinese: bool = False,
                             update_checkpoints: bool = True):
        """执行注音分析（核心逻辑，供多处复用）。

        分析在后台 QThread 中进行，不阻塞 UI。分析结果通过信号回调到主线程，
        再手动构建 SentenceSnapshotCommand 纳入 undo/redo 栈。

        Args:
            only_noruby: True=仅分析未注音字符，False=全部重新分析
            auto_detect_chinese: True=自动检测纯中文歌词并走中文模式（跳过注音）。
                仅导入歌词后的自动触发应传 True；用户手动按"注音分析"按钮明确表达了
                注音意图，应传 False，避免纯汉字日文行被误判为中文。
            update_checkpoints: True=分析后重算节奏点（默认）；
                False=只刷注音、保留现有节奏点不动。
        """
        if not self._project:
            return
        if getattr(self, "_ruby_analyzing", False):
            return

        from strange_uta_game.backend.application import (
            AutoCheckService,
            is_chinese_lyrics,
            is_korean_lyrics,
        )
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.frontend.workers import RubyAnalyzeWorker

        app_settings = AppSettings()
        llm_active = app_settings.llm_ruby_active()
        auto_check_flags = app_settings.get_all().get("auto_check", {})
        user_dict = app_settings.load_effective_dictionary()
        annotate_katakana_with_english = app_settings.get(
            "ruby_dictionary.annotate_katakana_with_english", False
        )
        delete_types = auto_check_flags.get("delete_ruby_types", [])

        # 中文歌词检测：仅导入歌词的自动触发阶段启用；用户主动触发注音分析时
        # 视为明确需要注音，不再检测中文（避免纯汉字日文行被误判）。
        chinese_mode = (
            auto_detect_chinese
            and auto_check_flags.get("chinese_lyrics_detection", True)
            and is_chinese_lyrics("".join(s.text for s in self._project.sentences))
        )
        # 韩文歌词检测：含谚文即韩文（无纯汉字歧义），注音分析与导入均启用；
        # 「韩文歌词检测」设置关闭时回落日文注音路径。
        korean_mode = (
            auto_check_flags.get("korean_lyrics_detection", True)
            and is_korean_lyrics("".join(s.text for s in self._project.sentences))
        )

        # LLM 注音激活时不需要本地日语 IME，跳过 WinRT 安装引导。中文模式同样跳过。
        if not chinese_mode and not korean_mode and not llm_active:
            from strange_uta_game.frontend.winrt_japanese_guide import (
                ensure_winrt_japanese,
            )
            if not ensure_winrt_japanese(self):
                return

        # AutoCheckService（含 WinRTAnalyzer / LLMRubyAnalyzer）在主线程创建，
        # 确保 WinRT STA apartment 正确；LLM 整首一次发送需传入全部行文本。
        if korean_mode:
            analyzer = None
            llm_apply_user_dict = True
            from strange_uta_game.backend.infrastructure.parsers.korean_reading import (
                create_korean_reading_analyzer,
            )

            auto_check = AutoCheckService(
                auto_check_flags=auto_check_flags,
                user_dictionary=user_dict,
                annotate_katakana_with_english=annotate_katakana_with_english,
                korean_mode=True,
                korean_analyzer=create_korean_reading_analyzer(
                    auto_check_flags.get("korean_annotation_style", "katakana")
                ),
            )
        elif chinese_mode:
            analyzer = None
            llm_apply_user_dict = True
            pinyin_analyzer = None
            if auto_check_flags.get("chinese_pinyin_annotation", False):
                try:
                    from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                        create_pinyin_analyzer,
                    )
                    pinyin_analyzer = create_pinyin_analyzer()
                except ImportError:
                    pass
            auto_check = AutoCheckService(
                auto_check_flags=auto_check_flags,
                user_dictionary=user_dict,
                annotate_katakana_with_english=annotate_katakana_with_english,
                chinese_mode=True,
                pinyin_analyzer=pinyin_analyzer,
            )
        else:
            lines = [s.text for s in self._project.sentences]
            analyzer = app_settings.build_ruby_analyzer(
                lines, annotate_katakana_with_english=annotate_katakana_with_english
            )
            llm_apply_user_dict = app_settings.llm_apply_user_dict() if llm_active else True
            auto_check = AutoCheckService(
                ruby_analyzer=analyzer,
                auto_check_flags=auto_check_flags,
                user_dictionary=user_dict,
                annotate_katakana_with_english=annotate_katakana_with_english,
            )

        # 在主线程提前快照 before 状态和光标位置（worker 运行期间不能读 self._project）
        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在分析注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = RubyAnalyzeWorker(
            project_copy, auto_check, only_noruby, delete_types,
            llm_apply_user_dict=llm_apply_user_dict,
            update_checkpoints=update_checkpoints,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        # 保存强引用，防止 PyQt6 弱引用机制在函数返回后回收 worker/thread
        self._ruby_analyze_worker = worker
        self._ruby_analyze_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_analyze_worker = None
            self._ruby_analyze_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project, deleted_count: int) -> None:
            state_tooltip.setState(True)
            _cleanup()

            # LLM 注音失败时已回退本地引擎，提示用户。
            if getattr(analyzer, "llm_failed", False):
                InfoBar.warning(
                    title=self.tr("LLM 注音失败，已回退本地引擎"),
                    content=str(getattr(analyzer, "last_error", "") or ""),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )

            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    "注音分析",
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=False,
            )

            if deleted_count > 0:
                InfoBar.success(
                    title=self.tr("注音分析完成"),
                    content=self.tr("已重新分析注音，并自动删除了 {n} 个注音").format(n=deleted_count),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )
            else:
                InfoBar.success(
                    title=self.tr("注音分析完成"),
                    content=self.tr("已重新分析注音"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("注音分析失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        def _on_llm_waiting() -> None:
            state_tooltip.setContent(self.tr("正在等待 LLM 返回…（整首歌词一次性发送，请稍候）"))

        def _on_llm_progress(msg: str) -> None:
            state_tooltip.setContent(msg)

        thread.started.connect(worker.run)
        worker.llm_waiting.connect(_on_llm_waiting)
        worker.llm_progress.connect(_on_llm_progress)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_analyze_rubies(self, update_checkpoints: bool = True):
        """工具栏「注音分析」— 弹三选项对话框

        update_checkpoints=False 时只刷注音、保留现有节奏点不动。
        """
        if not self._project:
            return

        cp_note = (
            self.tr("（分析后会重算节奏点）")
            if update_checkpoints
            else self.tr("（保留现有节奏点不动）")
        )
        choice = message_choice(
            self,
            self.tr("自动分析全部注音"),
            self.tr("请选择分析范围：")
            + cp_note
            + "\n\n"
            + self.tr(
                "「全部重新分析」会覆盖现有注音。\n"
                "「仅分析未注音字符」会保留已有的人工/字典注音。"
            ),
            [self.tr("全部重新分析"), self.tr("仅分析未注音字符"), self.tr("取消")],
            default=1,
        )
        if choice in (-1, 2):
            return
        only_noruby = choice == 1
        self._auto_analyze_rubies(
            only_noruby=only_noruby, update_checkpoints=update_checkpoints
        )

    def _on_analyze_rubies_no_cp(self):
        """工具栏「注音分析（不更新节奏点）」— 只刷注音、保留节奏点。"""
        self._on_analyze_rubies(update_checkpoints=False)

    def _on_analyze_pinyin(self):
        """工具栏「中文拼音注音」— 为全项目汉字标注带声调拼音。"""
        self._auto_analyze_pinyin()

    def _auto_analyze_pinyin(self):
        """中文拼音注音入口（工具栏 + 自动检测共用）。

        下发进度为 "拼音注音" 的 StateToolTip，
        分析完成后通过 SentenceSnapshotCommand 纳入 undo 堆栈。
        """
        if not self._project:
            return
        if getattr(self, "_ruby_analyzing", False):
            return

        from strange_uta_game.backend.application import AutoCheckService
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
            create_pinyin_analyzer,
        )
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.frontend.workers import RubyAnalyzeWorker
        from qfluentwidgets import InfoBar, InfoBarPosition, StateToolTip
        from PyQt6.QtCore import Qt, QThread
        from copy import deepcopy

        app_settings = AppSettings()
        auto_check_flags = app_settings.get_all().get("auto_check", {})

        try:
            pinyin_analyzer = create_pinyin_analyzer()
        except ImportError as e:
            InfoBar.error(
                title=self.tr("缺少依赖"),
                content=str(e),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
            return

        auto_check = AutoCheckService(
            auto_check_flags=auto_check_flags,
            chinese_mode=True,
            pinyin_analyzer=pinyin_analyzer,
        )

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在拼音注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = RubyAnalyzeWorker(
            project_copy, auto_check, only_noruby=False, delete_types=[],
            llm_apply_user_dict=True, update_checkpoints=False,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        self._ruby_analyze_worker = worker
        self._ruby_analyze_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_analyze_worker = None
            self._ruby_analyze_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project, deleted_count: int) -> None:
            state_tooltip.setState(True)
            _cleanup()

            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    "拼音注音",
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=False,
            )

        def _on_error(error_msg: str) -> None:
            _cleanup()
            state_tooltip.setState(False)
            InfoBar.error(
                title=self.tr("拼音注音失败"),
                content=error_msg or "",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        worker.llm_progress.connect(lambda _: None)  # pinyin 无 LLM，忽略
        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_analyze_korean(self):
        """工具栏「韩文注音」— 为全项目韩文字/汉字按设置风格标注读音。"""
        self._auto_analyze_korean()

    def _auto_analyze_korean(self):
        """韩文注音入口：片假名/平假名/罗马音（风格 = 韩文注音风格设置）。

        镜像 _auto_analyze_pinyin 的结构（进度提示 / 后台 worker /
        SentenceSnapshotCommand 撤销 / 结构变更同步）；韩文注音纯查表，
        无依赖缺失分支，也无需 WinRT 日语组件。
        """
        if not self._project:
            return
        if getattr(self, "_ruby_analyzing", False):
            return

        from strange_uta_game.backend.application import AutoCheckService
        from strange_uta_game.backend.infrastructure.parsers.korean_reading import (
            create_korean_reading_analyzer,
        )
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.frontend.workers import RubyAnalyzeWorker
        from qfluentwidgets import InfoBar, InfoBarPosition, StateToolTip
        from PyQt6.QtCore import Qt, QThread
        from copy import deepcopy

        app_settings = AppSettings()
        auto_check_flags = app_settings.get_all().get("auto_check", {})
        style = auto_check_flags.get("korean_annotation_style", "katakana")

        auto_check = AutoCheckService(
            auto_check_flags=auto_check_flags,
            korean_mode=True,
            korean_analyzer=create_korean_reading_analyzer(style),
        )

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在韩文注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = RubyAnalyzeWorker(
            project_copy, auto_check, only_noruby=False, delete_types=[],
            llm_apply_user_dict=True, update_checkpoints=False,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        self._ruby_analyze_worker = worker
        self._ruby_analyze_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_analyze_worker = None
            self._ruby_analyze_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project, deleted_count: int) -> None:
            state_tooltip.setState(True)
            _cleanup()

            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project,
                    before_sentences,
                    after_sentences,
                    "韩文注音",
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=False,
            )

        def _on_error(error_msg: str) -> None:
            _cleanup()
            state_tooltip.setState(False)
            InfoBar.error(
                title=self.tr("韩文注音失败"),
                content=error_msg or "",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        worker.llm_progress.connect(lambda _: None)  # 韩文注音无 LLM，忽略
        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _auto_analyze_all_rubies(self):
        """自动分析全部注音（用于歌词导入后重新注音，覆盖已有）"""
        self._auto_analyze_rubies(only_noruby=False)

    def _analyze_rubies_subset(
        self,
        line_idx: int,
        restrict_indices: Optional[set],
        label: str,
        *,
        show_winrt_dialog: bool = True,
        update_checkpoints: bool = True,
    ) -> None:
        """对单行（restrict_indices=None）或行内选定字符执行注音分析（异步）。"""
        self._analyze_rubies_specs_async(
            [(line_idx, restrict_indices)], label,
            show_winrt_dialog=show_winrt_dialog,
            update_checkpoints=update_checkpoints,
        )

    def _analyze_rubies_specs_async(
        self,
        specs: list,
        label: str,
        *,
        show_winrt_dialog: bool = True,
        update_checkpoints: bool = True,
    ) -> None:
        """对多个指定行/范围批量执行注音分析（后台 QThread，不阻塞 UI）。

        Args:
            specs: list of (line_idx, restrict_indices | None)
            label: 用于 InfoBar 标题和 undo 描述
            show_winrt_dialog: False 时 WinRT 不可用则静默跳过（粘贴触发时用）
            update_checkpoints: True=分析后重算节奏点（默认）；
                False=只刷注音、保留现有节奏点不动。
        """
        if not self._project or not specs:
            return
        if getattr(self, "_ruby_subset_analyzing", False):
            if not hasattr(self, "_ruby_analysis_queue"):
                self._ruby_analysis_queue = []
            self._ruby_analysis_queue.append(
                (list(specs), label, show_winrt_dialog, update_checkpoints)
            )
            return

        from strange_uta_game.backend.application import AutoCheckService
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.frontend.workers import RubySubsetAnalyzeWorker

        app_settings = AppSettings()
        llm_active = app_settings.llm_ruby_active()

        # LLM 注音激活时不依赖本地日语 IME，跳过 WinRT 检查/引导。
        # 用户主动触发的按行/按选定字符分析：不做中文检测——按下"注音分析"按钮
        # 即表示需要注音，避免纯汉字日文行被误判为中文跳过。
        if not llm_active:
            import sys

            from strange_uta_game.__version__ import VARIANT

            # 与 ensure_winrt_japanese 的放行判据保持一致：非 Windows 平台或
            # 非 main 变体（noWinIME/mac 打包版）以 Sudachi 为主引擎、不带
            # winrt 包，无需也不应检查 WinRT——否则队列里除首行外的任务会因
            # winrt_japanese_status() 恒为不可用而被静默丢弃。
            if not VARIANT and sys.platform == "win32":
                from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                    winrt_japanese_status,
                )
                from strange_uta_game.frontend.winrt_japanese_guide import (
                    ensure_winrt_japanese,
                )

                if show_winrt_dialog:
                    if not ensure_winrt_japanese(self):
                        return
                else:
                    available, _ = winrt_japanese_status()
                    if not available:
                        return

        auto_check_flags = app_settings.get_all().get("auto_check", {})
        user_dict = app_settings.load_effective_dictionary()
        annotate_katakana_with_english = app_settings.get(
            "ruby_dictionary.annotate_katakana_with_english", False
        )
        # LLM 只发本批需要注音的行（去重保序）：所选/按行不再每次发整首，
        # 避免小范围操作承担整首请求的超长生成时间（600s 总时限超时后
        # 回退本地引擎）；跨行所选的多行合并进同一次批量请求。
        # 「全部」入口走 RubyAnalyzeWorker，仍整首一次发送。
        _seen_line_texts = set()
        lines = []
        for _li, _ri in specs:
            _text = self._project.sentences[_li].text
            if _text not in _seen_line_texts:
                _seen_line_texts.add(_text)
                lines.append(_text)
        analyzer = app_settings.build_ruby_analyzer(
            lines, annotate_katakana_with_english=annotate_katakana_with_english
        )
        llm_apply_user_dict = app_settings.llm_apply_user_dict() if llm_active else True
        auto_check = AutoCheckService(
            ruby_analyzer=analyzer,
            auto_check_flags=auto_check_flags,
            user_dictionary=user_dict,
            annotate_katakana_with_english=annotate_katakana_with_english,
        )

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = specs[0][0]
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)
        self._ruby_subset_analyzing = True

        # 分析中显示忙碌提示器（本地引擎 / LLM 通用）。
        subset_tooltip = StateToolTip(self.tr("正在分析注音"), self.tr("正在初始化…"), self)
        if llm_active:
            green = theme.status_complete.name()
            subset_tooltip.setStyleSheet(f"""
                StateToolTip {{
                    background-color: {green};
                    border: 1px solid {green};
                    border-radius: 8px;
                }}
                StateToolTip QLabel {{
                    color: white;
                }}
            """)
        subset_tooltip.move(subset_tooltip.getSuitablePos())
        subset_tooltip.show()

        worker = RubySubsetAnalyzeWorker(
            project_copy, auto_check, specs, apply_user_dict=llm_apply_user_dict,
            update_checkpoints=update_checkpoints,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        self._ruby_subset_analyze_worker = worker
        self._ruby_subset_analyze_thread = thread

        def _cleanup() -> None:
            self._ruby_subset_analyze_worker = None
            self._ruby_subset_analyze_thread = None
            self._ruby_subset_analyzing = False
            if hasattr(self, "_ruby_analysis_queue") and self._ruby_analysis_queue:
                ns, nl, nswd, nuc = self._ruby_analysis_queue.pop(0)
                self._analyze_rubies_specs_async(
                    ns, nl, show_winrt_dialog=nswd, update_checkpoints=nuc
                )

        def _close_tooltip() -> None:
            if subset_tooltip is not None:
                subset_tooltip.setState(True)

        def _on_finished(analyzed_project) -> None:
            _close_tooltip()
            if getattr(analyzer, "llm_failed", False):
                InfoBar.warning(
                    title=self.tr("LLM 注音失败，已回退本地引擎"),
                    content=str(getattr(analyzer, "last_error", "") or ""),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )
            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project, before_sentences, after_sentences, label
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=False,
            )
            InfoBar.success(
                title=self.tr("{label}完成").format(label=label),
                content=self.tr("已分析所选范围的注音"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            # 本轮结果落盘后再消费队列：下一轮的 before/copy 深拷贝才能包含
            # 本轮分析结果，否则快照整体替换会把它回滚掉（跨行所选交替丢行）。
            _cleanup()

        def _on_error(err: str) -> None:
            _close_tooltip()
            InfoBar.warning(
                title=self.tr("{label}失败").format(label=label),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            _cleanup()

        def _on_llm_waiting() -> None:
            subset_tooltip.setContent(self.tr("正在等待 LLM 返回…（本批内容一次性发送，请稍候）"))

        def _on_llm_progress(msg: str) -> None:
            subset_tooltip.setContent(msg)

        thread.started.connect(worker.run)
        worker.progress.connect(lambda phase, cur, tot: subset_tooltip.setContent(f"{phase} {cur}/{tot}"))
        worker.llm_waiting.connect(_on_llm_waiting)
        worker.llm_progress.connect(_on_llm_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_analyze_rubies_by_line(self, update_checkpoints: bool = True):
        """工具栏「按行注音分析」— 仅分析当前行。

        update_checkpoints=False 时只刷注音、保留现有节奏点不动。
        """
        if not self._project:
            return
        line_idx = self._current_line_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            InfoBar.warning(
                title=self.tr("未选中行"),
                content=self.tr("请先在歌词中选择要分析的行"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        label = "按行注音分析" if update_checkpoints else "按行注音分析（仅注音）"
        self._analyze_rubies_subset(
            line_idx, None, label, update_checkpoints=update_checkpoints
        )

    def _on_analyze_rubies_by_line_no_cp(self):
        """工具栏「按行注音分析（不更新节奏点）」— 只刷注音、保留节奏点。"""
        self._on_analyze_rubies_by_line(update_checkpoints=False)

    def _on_analyze_rubies_selected(self, update_checkpoints: bool = True):
        """工具栏「注音分析所选字符」— 分析选中字符范围（支持跨行逐行排队）。

        update_checkpoints=False 时只刷注音、保留现有节奏点不动。
        """
        if not self._project:
            return

        label = "注音分析所选字符" if update_checkpoints else "注音分析所选字符（仅注音）"

        # 跨行选中：收集每行的字符范围，整批提交（worker 内部逐行顺序分析）
        if self.preview.is_multi_line_selection():
            sel = self.preview.get_normalized_selection()
            if sel is None:
                return
            start_line, start_char, end_line, end_char = sel
            specs = []
            for line_idx in range(start_line, end_line + 1):
                if line_idx < 0 or line_idx >= len(self._project.sentences):
                    continue
                sentence = self._project.sentences[line_idx]
                if not sentence.characters:
                    continue
                s = start_char if line_idx == start_line else 0
                e = end_char if line_idx == end_line else len(sentence.characters) - 1
                if s > e:
                    continue
                ri = set(range(s, e + 1))
                specs.append((line_idx, ri))
            if specs:
                # 整批一次提交：一次深拷贝、一次快照应用、一条撤销命令；
                # worker 内部逐行顺序调用句子级管线（跨行顺序处理语义不变），
                # 并通过 progress 信号显示「注音分析 i/total」进度。拆成
                # 前端逐行排队是 ad30734 时代旧管线的规避手段，句子级管线
                # （analyze_and_apply_sentence_pipeline）重构后已无必要。
                self._analyze_rubies_specs_async(
                    specs, label, update_checkpoints=update_checkpoints
                )
            return

        line_idx = self._current_line_idx
        char_idx = self.preview._current_char_idx
        if line_idx < 0 or line_idx >= len(self._project.sentences):
            return
        sentence = self._project.sentences[line_idx]
        if char_idx < 0 or char_idx >= len(sentence.characters):
            InfoBar.warning(
                title=self.tr("未选中字符"),
                content=self.tr("请先选择要分析的字符"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        start_idx = char_idx
        end_idx = char_idx
        if (
            self.preview._focus_line_idx == line_idx
            and self.preview._focus_char_idx >= 0
            and self.preview._focus_char_range_end >= 0
        ):
            start_idx = min(
                self.preview._focus_char_idx, self.preview._focus_char_range_end
            )
            end_idx = max(
                self.preview._focus_char_idx, self.preview._focus_char_range_end
            )
        self._analyze_rubies_subset(
            line_idx, set(range(start_idx, end_idx + 1)), label,
            update_checkpoints=update_checkpoints,
        )

    def _on_analyze_rubies_selected_no_cp(self):
        """工具栏「注音分析所选字符（不更新节奏点）」— 只刷注音、保留节奏点。"""
        self._on_analyze_rubies_selected(update_checkpoints=False)

    def _on_romanize_all_rubies(self):
        """工具栏「全部转为罗马字注音」（异步）。

        读取现有全部注音结构、保留结构把假名 ruby 转为赫本式罗马音；无 ruby 的
        单假名（平假名/片假名/促音/长音）补自注音再转罗马音。罗马音读音随上下文
        变化（促音双写、は/へ/を 助词读音）。

        不调用注音引擎、不更新节奏点、不删除注音；转换在后台线程对项目副本
        执行，StateToolTip 显示「罗马字转换 当前/总数」文字进度，并与正在
        进行的异步注音分析互斥。变更经 SentenceSnapshotCommand 纳入撤销/重做
        栈，ruby 通道刷新（move_cp=False，不触碰节奏点）。
        """
        if not self._project:
            return
        # 与异步注音分析互斥，避免并发改写句子结构
        if getattr(self, "_ruby_analyzing", False) or getattr(
            self, "_ruby_subset_analyzing", False
        ):
            InfoBar.warning(
                title=self.tr("注音分析进行中"),
                content=self.tr("请等待当前注音分析完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        from strange_uta_game.backend.infrastructure.parsers.romaji import (
            RomajiOptions,
            romanize_project_to_self_ruby,
        )
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        from strange_uta_game.frontend.workers import ProjectTaskWorker

        before_sentences = deepcopy(self._project.sentences)
        undo_pos = (self._current_line_idx, self.preview._current_char_idx)
        focus_line_idx = self._current_line_idx
        focus_char_idx = self.preview._current_char_idx

        project_copy = deepcopy(self._project)
        changed_box = [0]
        romaji_options = RomajiOptions.from_mapping(
            AppSettings().get("auto_check", {})
        )

        def _task(proj, progress_cb):
            changed_box[0] = romanize_project_to_self_ruby(
                proj,
                progress_callback=progress_cb,
                options=romaji_options,
            )

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在转换罗马字注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = ProjectTaskWorker(project_copy, _task)
        thread = QThread(self)
        worker.moveToThread(thread)

        self._romanize_worker = worker
        self._romanize_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._romanize_worker = None
            self._romanize_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project) -> None:
            state_tooltip.setState(True)
            _cleanup()

            if changed_box[0] == 0:
                InfoBar.info(
                    title=self.tr("无变化"),
                    content=self.tr("没有可转为罗马字的注音或单假名"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=2500,
                    parent=self,
                )
                return

            after_sentences = analyzed_project.sentences
            command_manager = (
                self._timing_service.command_manager if self._timing_service else None
            )
            if command_manager is not None:
                command = SentenceSnapshotCommand(
                    self._project, before_sentences, after_sentences, "全部转为罗马字注音"
                )
                command.undo_position = undo_pos
                command.redo_position = (focus_line_idx, focus_char_idx)
                command.move_cp = False
                command_manager.execute(command)
            else:
                self._project.sentences = deepcopy(after_sentences)

            self._sync_after_structure_change(
                change_type="rubies",
                focus_line_idx=focus_line_idx,
                focus_char_idx=focus_char_idx,
                checkpoint_idx=None,
                move_cp=False,
            )
            InfoBar.success(
                title=self.tr("已转为罗马字注音"),
                content=self.tr("共处理 {n} 行").format(n=changed_box[0]),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("罗马字转换失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_open_fulltext(self):
        """工具栏「全文本编辑」— 以对话框打开全文本注音编辑界面。"""
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return
        from .fulltext_interface import FullTextEditDialog

        line_idx = max(0, self._current_line_idx)
        char_idx = max(0, self.preview._current_char_idx)
        dlg = FullTextEditDialog(
            self._store, self, current_line=line_idx, current_char=char_idx
        )
        dlg.exec()
