"""应用设置数据层 — AppSettings 及 RL 字典解析函数。

从 settings_interface.py 拆出，保留公共 API 不变：
- ``AppSettings``：应用设置管理（config.json + dictionary.json + singers.json）
- ``_parse_rl_dictionary``：RL 字典文本解析（模块内私有，被 AppSettings 和 DictionaryEditDialog 复用）

PR #6B 增补：
- ``SettingsProvider``：设置后端 Protocol，允许宿主（如 krok-helper）把
  ``AppSettings`` 的持久化重定向到自己的存储。
- ``AppSettings.__init__`` 增加可选 ``provider`` 参数；不传则保持完整
  standalone 行为（文件读写 + dictionary 升级 + 独立 JSON 文件迁移）。

共享实例（记忆设置丢失修复）：
- ``AppSettings()`` 对同一存储目标（同一 config 文件 / 同一 provider）返回
  **同一个实例**（见类 docstring）。此前各处 ``AppSettings()`` 各持一份内存
  快照而 ``save()`` 整字典写盘，长寿命实例会把短寿命实例刚写入磁盘的键
  回滚——表现为「切换项目后 AI 打轴环境配置、自动删除注音类型等记忆设置
  丢失」。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, Protocol, runtime_checkable
import json
import sys
import threading

from strange_uta_game import app_dirs


@runtime_checkable
class SettingsProvider(Protocol):
    """设置持久化后端协议。

    Standalone 模式下，AppSettings 内部使用一个文件型默认实现读写
    ``config.json``。Embedded 模式下，宿主实现本协议把读写桥接到自己的
    存储（如 krok-helper 的 ``%APPDATA%/Karaoke Helper/settings.json``
    里的 ``lyrics_timing`` namespace）。

    协议覆盖**主 config**（即 ``config.json`` 等价物）的整字典读写，
    以及 dictionary / singers / network 这三类独立数据的通用 extra
    读写。Embedded 模式下，宿主可把它们分别路由到自己的 namespace
    字段（``lyrics_timing_dictionary`` 等）。

    实现示例（host bridge）::

        class KrokHelperConfigProvider:
            def __init__(self, app_settings, save_callback):
                self._app_settings = app_settings
                self._save_callback = save_callback

            def load(self) -> dict:
                return dict(self._app_settings.lyrics_timing)

            def save(self, data: dict) -> None:
                self._app_settings.lyrics_timing = dict(data)
                self._save_callback()
    """

    def load(self) -> Dict[str, Any]:
        """返回当前完整的设置字典；首次启动 / 空存储时返回 ``{}``。"""
        ...

    def save(self, data: Dict[str, Any]) -> None:
        """把完整设置字典持久化到后端存储。"""
        ...

    def load_extra(self, key: str, default: Any) -> Any:
        """读取独立数据 namespace；key 为 dictionary / singers / network。"""
        ...

    def save_extra(self, key: str, data: Any) -> None:
        """保存独立数据 namespace；key 为 dictionary / singers / network。"""
        ...


class AppSettings:
    """应用设置管理

    进程级共享实例：``AppSettings()`` / ``AppSettings(provider=...)`` /
    ``AppSettings(config_path=...)`` 对同一存储目标（同一 config 文件或同一
    provider）返回**同一个实例**。设置修改因此总是直接记忆到 config.json
    （embedded 下经 ``provider.save`` / ``save_partial`` 通知宿主保存），
    不存在会回滚磁盘的"旧内存快照"。

    背景：散落各处的 ``AppSettings()`` 曾各自持有独立内存快照，而 ``save()``
    是整字典写盘——设置页的长寿命实例会在任何一次 save（典型：切换项目时
    经 ``_apply_project_extras`` 重置 nicokara_tags 触发）把短寿命实例
    （AI 打轴弹窗、全文编辑删除注音对话框等）刚写入磁盘的键回滚。

    实例被丢弃前（:meth:`reset_shared_instances`）会先把带未保存修改的
    实例写盘，保证交接不丢内存态。注意：本类不支持 ``copy``/``deepcopy``
    实例本身（与共享机制冲突）；需要副本时拷贝 ``get_all()`` 字典。
    """

    _default_provider: ClassVar[Optional[SettingsProvider]] = None

    # 共享实例缓存：key -> (provider 强引用 | None, instance)。
    # 保留 provider 强引用防止其被 GC 后 id 被新对象复用撞 key。
    _shared_instances: ClassVar[Dict[tuple, tuple]] = {}
    # 独立文件 + 主 config 的文件级锁：maybe_auto_update_network_dictionary
    # 在 daemon 线程写 config.json，与 UI 线程并发 open(w) 在 Windows 上会
    # PermissionError；RLock 保证 init→migrate→save 的重入路径不自我死锁。
    _io_lock: ClassVar["threading.RLock"] = threading.RLock()

    DEFAULT_SETTINGS = {
        "audio": {
            "default_volume": 80,
            "default_speed": 1.0,
            "speed_slider_min": 0.2,
            "speed_slider_max": 1.0,
            "auto_play_on_load": False,
            "pause_on_leave_timing": False,
            # 高质量音频变速：开启用离线 TSM 预渲染（变速不变调、无爆音，占用 .cache
            # 磁盘缓存）；关闭仅用原版 BASS 实时变速（零缓存，但可能爆音）。
            "hq_speed_change": True,
        },
        "timing": {
            "default_check_count": 1,
            "auto_advance_after_tag": True,
            "show_preview_lines": 5,
            "tag_offset_ms": -230,
            "speed_correction": 100,
            "fast_forward_ms": 5000,
            "rewind_ms": 5000,
            "jump_before_ms": 3000,
            "timing_adjust_step_ms": 10,
            "waveform_tag_edit_enabled": True,
            "waveform_tag_char_enabled": True,
            "waveform_tag_ruby_enabled": True,
            "waveform_center_playhead_enabled": False,
            # 波形区显示设置（齿轮对话框）：模式互斥 + 网格 + 频谱参数
            "waveform_visible": True,
            "waveform_display_mode": "waveform",  # waveform / spectrum / dual
            "waveform_grid_mode": "time",         # time / bpm
            "waveform_grid_bpm": 120.0,
            "waveform_grid_offset_ms": 0,         # BPM 网格偏移（±600000ms，拍线相位对齐）
            "waveform_grid_line_width": 2,        # 网格线宽 0~100px；0=不绘制
            "spectrum_overlap": 0.75,             # 窗口重叠（帧距=fft·(1-overlap)）
            "spectrum_fft_size": 8192,
            "spectrum_freq_scale": "log",         # log / linear
            "spectrum_colormap": "inferno",       # 强度配色；保持项目原有方案为默认
            "spectrum_dyn_range_db": 60,
            "spectrum_freq_min_hz": 0,            # 声谱频率钳制（0=自动/全谱）
            "spectrum_freq_max_hz": 0,
            "display_height": 120,                # 旧版共用高度（兼容迁移）
            "waveform_display_height": 120,       # 波形图显示高度
            "spectrum_display_height": 120,       # 声谱图显示高度
            "waveform_rms_enabled": True,         # 双层波形（峰值+RMS）
            "disable_click_jump": False,
            "disable_click_recenter": False,
            "preview_guide_enabled": False,
            "preview_guide_prev_alpha": 100,
            "preview_guide_curr_alpha": 50,
            "preview_guide_next_alpha": 20,
            "preview_guide_prev_enabled": True,
            "preview_guide_curr_enabled": True,
            "preview_guide_next_enabled": True,
            "keysound_enabled": False,
            "keysound_volume": 100,
            "keysound_style": "default",
            # 节拍器（齿轮弹窗「网格与节拍」）：播放期间按 BPM 网格的
            # BPM/偏移触发节拍音，每小节第一拍重音
            "waveform_metronome_enabled": False,
            "waveform_metronome_volume": 100,
            # 拍号分子（每小节拍数）：小节线与节拍器重音共用的循环周期
            "waveform_beats_per_bar": 4,
            "guide_symbol": "",
            "guide_count": 1,
            "guide_duration_ms": 1000,
            "guide_reverse": False,
            "guide_fill_gap": False,
            "scroll_mode": "auto",  # auto / always / never
        },
        "auto_guide": {
            "min_gap_ms": 3000,
            "defaults": None,
            "window_geometry": "",
        },
        "auto_check": {
            "hiragana": True,
            "katakana": True,
            "kanji": True,
            "alphabet": False,
            "digit": False,
            "symbol": False,
            "space": False,
            "auto_on_load": True,
            "check_n": True,
            "check_sokuon": True,
            "check_long_vowel": False,
            "check_parentheses": True,
            "check_empty_lines": False,
            "check_line_start": False,
            "check_line_end": True,
            "space_after_japanese": True,
            "space_after_alphabet": True,
            "space_after_symbol": True,
            "small_kana": False,
            "check_space_as_line_end": True,
            "check_english_word_end": True,
            "chinese_lyrics_detection": True,
            "chinese_pinyin_annotation": False,
            "romanize_ruby": False,
            "delete_ruby_types": [],
        },
        "ui": {
            "theme": "auto",
            "language": "auto",
            # Windows 高分屏适配；关闭时进程按 96 DPI 绘制并由系统缩放整窗。
            # DPI awareness 在 QApplication 创建时确定，因此修改后重启生效。
            "high_dpi_scaling": True,
            # F2 注音编辑器：compact = 字符上方迷你浮窗，classic = 原有大窗口。
            "f2_ruby_editor_mode": "compact",
            # 应用界面字体；空串表示根据界面语言和平台自动选择。
            "interface_font": "",
            # 窗口习惯：启动时读取以恢复上次的窗口大小与最大化状态；
            # 用户改变窗口大小或最大化时实时写回（见 MainWindow）。
            "window_size": [1400, 900],
            "window_maximized": False,
            "font_size": 24,
            "main_font": "Microsoft YaHei",
            "ruby_font": "Microsoft YaHei",
            "lyrics_alignment": "left",
            "alignment_margin": 168,
            "checkpoint_markers": {
                "cp_first_timed": "▶",
                "cp_first_empty": "▷",
                "cp_multi_timed": "▮",
                "cp_multi_empty": "▯",
                "cp_sentence_end_timed": "⬟",
                "cp_sentence_end_empty": "⬠",
            },
            # 导唱待办标记：用户在某字符前需要添加导唱符的提醒
            # symbol — 显示符号；size — 字号
            "needs_guide_symbol": "✚",
            "needs_guide_size": 12,
        },
        "export": {
            "default_format": "Nicokara (带注音)",
            "auto_add_extension": True,
            "last_export_dir": "",
            "default_export_dir": "",
            "offset_ms": 0,
            "software_compensation_ms": 0,
            "nicokara_pause_char": "^",
        },
        "ruby_split_mode": "mora",  # 注音分段方式: "direct", "char", "mora"
        "ruby_dictionary": {
            "enabled": True,
            "annotate_katakana_with_english": False,
        },
        "llm_ruby": {
            "enabled": False,
            "provider": "openai",      # "openai" | "anthropic" | "custom"
            "base_url": "",
            "api_key": "",
            "model": "",
            "apply_user_dict": True,    # LLM 注音后是否仍应用用户词典
            "timeout_sec": 180,
            "total_timeout_sec": 600,
        },
        "nicokara_tags": {
            "title": "",
            "artist": "",
            "album": "",
            "tagging_by": "",
            "silence_ms": 0,
            # @Offset/@HeadOffset 标准栏位（毫秒，可为负；0 = 不输出该标签）
            "offset": 0,
            "head_offset": 0,
            "custom": [],
        },
        "auto_save": {
            "enabled": True,
            "interval_minutes": 5,
            # 触发保存 / 关闭未保存对话框（保存或放弃）时，向 ProjectBackup 目录
            # 写入一份命名备份；按全局上限轮换，超出删最旧。0 表示关闭备份。
            "backup_count": 10,
            # 备份与临时文件（周期保存 / 闪退恢复）的存放目录；留空使用默认
            # 位置（<config_dir>/ProjectBackup）。
            "backup_dir": "",
            # 保存未命名（untitled）项目时优先使用的目录；留空则回退到
            # 已保存项目 / 最近加载目录。见 ProjectStore.save_dir。
            "default_save_dir": "",
        },
        "tools": {
            "ffmpeg_path": "",
        },
        "complete_timestamp": {
            "scope_types": [
                "kanji",
                "hiragana",
                "katakana",
                "sokuon",
                "long_vowel",
                "chon",
                "chisai_kana",
            ],
            "exclude_rules": [
                "linked",
            ],
        },
        "shortcuts": {
            # 以 _SHORTCUT_ACTIONS 为唯一真源，此处需保持一致；
            # 新装用户实际读到的是 config/config.json，两边一起改。
            # 格式: "key:trigger" trigger 为 short 或 long；空字符串表示未绑定
            # 打轴模式：音乐播放时生效（以实时打轴操作为主）
            "timing_mode": {
                "play_pause": "A:short",
                "stop": "S:short",
                "restart_playback": "SHIFT+S:short",
                "speed_down": "Q:short",
                "speed_up": "W:short",
                "speed_reset": "E:short",
                "volume_up": "",
                "volume_down": "",
                "volume_reset": "",
                "nav_prev_line": "UP:short",
                "nav_next_line": "DOWN:short",
                "nav_prev_char": "LEFT:short",
                "nav_next_char": "RIGHT:short",
                "seek_back": "Z:short",
                "seek_forward": "X:short",
                "lock_playback_start": "[:short",
                "lock_playback_end": "]:short",
                "tag_now": "D:short,F:short",
                "tag_now_extra": "SPACE:short",
                "tag_and_delete_next": "",
                "delete_timestamp": "Backspace:short",
                "timestamp_up": "ALT+UP:short",
                "timestamp_down": "ALT+DOWN:short",
                "add_checkpoint": "1:short",
                "remove_checkpoint": "3:short",
                "cycle_checkpoint": "ALT+RIGHT:short",
                "cycle_checkpoint_prev": "ALT+LEFT:short",
                "clear_all_checkpoints": "CTRL+\\:short",
                "toggle_line_end": "2:short",
                "timestamps_to_sentence_end": "P:short",
                "edit_ruby": "F2:short",
                "toggle_word_join": "F3:short",
                "insert_space": "M:short",
                "modify_char": "J:short",
                "modify_line": "CTRL+2:short",
                "bulk_change": "CTRL+H:short",
                "insert_guide": "CTRL+1:short",
                "auto_insert_guide": "",
                "toggle_needs_guide": ";:short",
                "analyze_rubies": "CTRL+3:short",
                "analyze_rubies_by_line": "CTRL+SHIFT+A:short",
                "analyze_rubies_selected": "CTRL+A:short",
                "analyze_rubies_no_cp": "",
                "analyze_rubies_by_line_no_cp": "",
                "analyze_rubies_selected_no_cp": "",
                "romanize_all": "",
                "analyze_pinyin": "",
                "delete_rubies_by_type": "CTRL+4:short",
                "singer_manager": "",
                "apply_singer": "K:short",
                "set_singer_by_line": "CTRL+5:short",
                "complete_timestamp": "",
                "separate_symbol_timestamp": "",
                "adjust_raw_timestamp": "",
                "adjust_raw_timestamp_line": "",
                "adjust_raw_timestamp_selected": "",
                "delete_all_timestamps": "",
                "delete_all_timestamps_keep_head": "",
                "delete_timestamps_selected": "",
                "auto_generate_interlude_guide": "",
                "new_project": "CTRL+0:short",
                "load_project": "",
                "save_as": "",
                "load_audio": "",
                "load_lyrics": "",
                "concat_sug": "",
                "open_fulltext": "CTRL+T:short",
                "quick_export": "CTRL+E:short",
                "undo": "CTRL+Z:short",
                "redo": "CTRL+Y:short",
                "save": "CTRL+S:short",
                "copy_chars": "CTRL+C:short",
                "paste_lyrics": "CTRL+V:short",
                "insert_line_break": "Enter:short",
                "merge_line_up": "Shift+Enter:short",
                "delete_char": "Delete:short",
            },
            # 编辑模式：音乐暂停/停止时生效（以歌词/注音编辑为主）
            "edit_mode": {
                "play_pause": "A:short",
                "stop": "S:short",
                "restart_playback": "SHIFT+S:short",
                "speed_down": "Q:short",
                "speed_up": "W:short",
                "speed_reset": "E:short",
                "volume_up": "",
                "volume_down": "",
                "volume_reset": "",
                "nav_prev_line": "UP:short",
                "nav_next_line": "DOWN:short",
                "nav_prev_char": "LEFT:short",
                "nav_next_char": "RIGHT:short",
                "lock_playback_start": "[:short",
                "lock_playback_end": "]:short",
                "tag_now_editor": "",
                "tag_now_extra_editor": "",
                "timestamp_up": "ALT+UP:short",
                "timestamp_down": "ALT+DOWN:short",
                "add_checkpoint": "Space:short",
                "remove_checkpoint": "BACKSPACE:short",
                "cycle_checkpoint": "ALT+RIGHT:short",
                "cycle_checkpoint_prev": "ALT+LEFT:short",
                "clear_all_checkpoints": "CTRL+\\:short",
                "toggle_line_end": ".:short",
                "timestamps_to_sentence_end": "P:short",
                "edit_ruby": "F2:short",
                "toggle_word_join": "F3:short",
                "insert_space": "M:short",
                "modify_char": "J:short",
                "modify_line": "CTRL+2:short",
                "bulk_change": "CTRL+H:short",
                "insert_guide": "CTRL+1:short",
                "auto_insert_guide": "",
                "toggle_needs_guide": ";:short",
                "analyze_rubies": "CTRL+3:short",
                "analyze_rubies_by_line": "CTRL+SHIFT+A:short",
                "analyze_rubies_selected": "CTRL+A:short",
                "analyze_rubies_no_cp": "",
                "analyze_rubies_by_line_no_cp": "",
                "analyze_rubies_selected_no_cp": "",
                "romanize_all": "",
                "analyze_pinyin": "",
                "delete_rubies_by_type": "CTRL+4:short",
                "singer_manager": "",
                "apply_singer": "K:short",
                "set_singer_by_line": "CTRL+5:short",
                "complete_timestamp": "",
                "separate_symbol_timestamp": "",
                "adjust_raw_timestamp": "",
                "adjust_raw_timestamp_line": "",
                "adjust_raw_timestamp_selected": "",
                "delete_all_timestamps": "",
                "delete_all_timestamps_keep_head": "",
                "delete_timestamps_selected": "",
                "auto_generate_interlude_guide": "",
                "new_project": "CTRL+0:short",
                "load_project": "",
                "save_as": "",
                "load_audio": "",
                "load_lyrics": "",
                "concat_sug": "",
                "open_fulltext": "CTRL+T:short",
                "quick_export": "CTRL+E:short",
                "undo": "CTRL+Z:short",
                "redo": "CTRL+Y:short",
                "save": "CTRL+S:short",
                "copy_chars": "CTRL+C:short",
                "paste_lyrics": "CTRL+V:short",
                "insert_line_break": "Enter:short",
                "merge_line_up": "Shift+Enter:short",
                "delete_char": "Delete:short",
            },
        },
    }

    @classmethod
    def set_default_provider(cls, provider: Optional[SettingsProvider]) -> None:
        """设置当前进程内 ``AppSettings()`` 的默认后端。

        Embedded 宿主会在构造主窗口时设置本值，使深层组件里保留的
        ``AppSettings()`` 调用也能自动走宿主设置存储；standalone 默认
        为 None，继续使用原有文件型配置。
        """
        cls._default_provider = provider

    def __new__(
        cls,
        config_path: Optional[str] = None,
        *,
        provider: Optional[SettingsProvider] = None,
    ) -> "AppSettings":
        # 同一存储目标只建一个实例。key 的解析与 __init__ 的 effective
        # provider 规则保持一致：显式 provider > 显式 config_path > 类默认。
        if provider is not None:
            key = ("provider", id(provider))
        elif config_path is not None:
            key = ("file", str(Path(config_path)))
        elif cls._default_provider is not None:
            key = ("provider", id(cls._default_provider))
        else:
            key = ("file", str(cls.get_config_dir() / "config.json"))
        cached = cls._shared_instances.get(key)
        if cached is not None:
            return cached[1]
        instance = super().__new__(cls)
        cls._shared_instances[key] = (
            provider if provider is not None else cls._default_provider,
            instance,
        )
        return instance

    @classmethod
    def reset_shared_instances(cls) -> None:
        """清空进程级共享实例缓存（丢弃前先把未保存修改写盘）。

        「重置设置为默认值」等需要从头初始化的路径使用：先把带
        ``set()`` 过但未 ``save()`` 的实例 flush 到 config.json /
        provider，再清缓存——之后 ``AppSettings()`` 重建的实例从最新
        磁盘 / provider 状态读起，交接不丢内存态。
        """
        for _pinned_provider, instance in list(cls._shared_instances.values()):
            try:
                if getattr(instance, "_dirty_paths", None):
                    instance.save()
            except Exception:
                pass
        cls._shared_instances.clear()

    @staticmethod
    def get_config_dir() -> Path:
        """获取配置文件目录（已确保存在且可写）。

        解析逻辑见 :mod:`strange_uta_game.app_dirs`：Windows/Linux 默认程序目录并
        支持 ``.config_redirect`` 重定向；macOS 用 ``~/Library/Application Support``；
        目录不可写时回退到 ``~/.strange_uta_game``。
        """
        return app_dirs.config_dir()

    @staticmethod
    def _get_packaged_config_path(filename: str) -> Optional[Path]:
        """获取内嵌配置文件路径（兼容开发环境和 PyInstaller 打包环境）。"""
        # PyInstaller 打包后
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = Path(base) / "strange_uta_game" / "config" / filename
            if p.exists():
                return p
        # 开发环境：相对于本文件的位置（settings/ → frontend/ → strange_uta_game/）
        dev_path = Path(__file__).resolve().parent.parent.parent / "config" / filename
        if dev_path.exists():
            return dev_path
        return None

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        provider: Optional[SettingsProvider] = None,
    ):
        """初始化 AppSettings。

        参数:
            config_path: 显式 config.json 路径（standalone 调试用）。
                仅在 ``provider`` 为 None 时生效。
        provider: 设置后端 Protocol 实现。给定时**完全跳过**所有文件
            路径初始化 —— 不解析 ``get_config_dir()``、不读写
            ``config.json``、不做 dictionary 升级、不做独立文件
            迁移。``self._settings`` 直接从 ``provider.load()`` 获取，
            后续 ``save()`` 写回 provider。
        """
        if getattr(self, "_initialized", False):
            # __new__ 命中共享缓存时 __init__ 仍会被再次调用——直接返回，
            # 避免重复初始化掀掉现役实例的内存态。
            return
        self._dirty_paths: set[str] = set()
        if provider is not None:
            effective_provider = provider
        elif config_path is None:
            effective_provider = self.__class__._default_provider
        else:
            effective_provider = None
        self._provider = effective_provider

        if effective_provider is None:
            # ── standalone 模式：保持原有文件型行为 ──
            if config_path is None:
                # get_config_dir() 已确保目录存在且可写（含 macOS / 只读位置兜底）。
                config_dir = self.get_config_dir()
                self._config_path = config_dir / "config.json"
                # 如果用户配置不存在，从内嵌配置复制
                if not self._config_path.exists():
                    self._copy_packaged_config()
            else:
                self._config_path = Path(config_path)

            self._dict_path = self._config_path.parent / "dictionary.json"
            self._network_dict_path = self._config_path.parent / "network_dictionary.json"
            self._singers_path = self._config_path.parent / "singers.json"
            self._settings = self._load_settings()
            self._migrate_to_separate_files()
            self._ensure_default_dictionary()
            self._force_upgrade_dictionary_if_needed()
        else:
            # ── embedded 模式：通过 provider 桥接，不碰文件系统 ──
            # 字段保留为 None 以保持类型一致；文件型方法（load_dictionary
            # 等）在此模式下会因路径为 None 而返回空集合，由宿主单独
            # 通过自己的 namespace 字段管理词典/演唱者/网络词典。
            self._config_path = None  # type: ignore[assignment]
            self._dict_path = None  # type: ignore[assignment]
            self._network_dict_path = None  # type: ignore[assignment]
            self._singers_path = None  # type: ignore[assignment]
            # 先用内嵌默认值打底（保证 get() 取到合理默认），再用 provider
            # 数据 deep-merge 覆盖。从 provider 接过来时 deepcopy，避免
            # 后续 self.set 误改写到 provider 内部状态（一些朴素 provider
            # 实现可能用浅拷贝传 dict 进来）。
            self._settings = self._load_packaged_defaults()
            try:
                loaded = effective_provider.load() or {}
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                self._migrate_display_heights(loaded)
                self._deep_merge(self._settings, deepcopy(loaded))
            try:
                provider_dictionary_version = int(
                    loaded.get("applied_dictionary_version", 0)
                )
            except (AttributeError, TypeError, ValueError):
                provider_dictionary_version = 0
            self._force_upgrade_dictionary_if_needed(provider_dictionary_version)
        self._initialized = True

    def _force_upgrade_dictionary_if_needed(
        self, user_version_override: Optional[int] = None
    ) -> None:
        """词典版本升级：比较内置词典版本号与用户已应用版本号。

        内置 config.json 含 ``dictionary_version``（整数），用户 config.json 含
        ``applied_dictionary_version``（整数，首次无此字段视为 0）。
        若用户版本 < 内置版本，按以下规则合并词典：
        - word 相同但 reading 不同：替换为内置版本
        - 用户词典中没有的 word：添加
        - 用户词典中有但内置没有的：保留（用户自定义）

        好处：之后只需递增内置 config.json 的 ``dictionary_version``，
        所有用户下次启动即自动升级，无需改代码。
        """
        # 读内置版本号
        packaged_cfg_path = self._get_packaged_config_path("config.json")
        packaged_version = 0
        if packaged_cfg_path:
            try:
                with open(packaged_cfg_path, "r", encoding="utf-8") as f:
                    packaged_cfg = json.load(f)
                packaged_version = int(packaged_cfg.get("dictionary_version", 0))
            except Exception:
                packaged_version = 0

        if packaged_version <= 0:
            return  # 内置没有版本号，跳过

        # 读用户已应用版本号（直接从用户 config.json 原始文件读，避免 merge 干扰）
        user_version = 0
        if user_version_override is not None:
            user_version = user_version_override
        elif self._config_path is not None and self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                user_version = int(user_cfg.get("applied_dictionary_version", 0))
            except Exception:
                user_version = 0

        if user_version >= packaged_version:
            return  # 已是最新版本

        # 加载内置词典和用户词典
        packaged_entries = self._load_packaged_dictionary()
        if not packaged_entries:
            return

        # 加载用户词典（如果存在）
        user_entries = []
        if self._provider is not None or (
            self._dict_path is not None and self._dict_path.exists()
        ):
            try:
                user_entries = self.load_dictionary()
            except Exception:
                user_entries = []

        # 构建用户词典索引：word -> (index, entry)
        user_index = {}
        for i, entry in enumerate(user_entries):
            word = (entry.get("word") or "").strip()
            if word:
                user_index[word] = (i, entry)

        # 合并逻辑：
        # 1. 遍历内置词典，替换 reading 不同的条目，添加新条目
        updated = 0
        added = 0
        for packaged_entry in packaged_entries:
            packaged_word = (packaged_entry.get("word") or "").strip()
            packaged_reading = (packaged_entry.get("reading") or "").strip()
            if not packaged_word:
                continue

            if packaged_word in user_index:
                # word 存在，检查 reading 是否相同
                idx, user_entry = user_index[packaged_word]
                user_reading = (user_entry.get("reading") or "").strip()
                if user_reading != packaged_reading:
                    # reading 不同，替换
                    user_entries[idx] = packaged_entry
                    updated += 1
            else:
                # word 不存在，添加到词典末尾
                user_entries.append(packaged_entry)
                added += 1

        # 保存更新后的用户词典
        try:
            self.save_dictionary(user_entries)
        except Exception as e:
            print(f"Failed to save merged dictionary: {e}")
            return

        # 写入已应用版本号到用户 config.json
        self._settings["applied_dictionary_version"] = packaged_version
        # 清理旧标志位（兼容旧版）
        self._settings.pop("is_dictionary_real_sugdic", None)
        try:
            if self._provider is not None:
                self._dirty_paths.add("applied_dictionary_version")
                self.save()
            else:
                self._save_json(self._config_path, self._settings)
        except Exception as e:
            print(f"Failed to write applied_dictionary_version: {e}")

    def _load_packaged_dictionary(self) -> list:
        """Load built-in entries independently of the active storage backend."""
        packaged = self._get_packaged_config_path("dictionary.json")
        if packaged:
            try:
                with open(packaged, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                if isinstance(entries, list):
                    return entries
            except Exception as e:
                print(f"Failed to read packaged dictionary: {e}")
        try:
            from strange_uta_game.backend.infrastructure.data.default_dictionary import (
                DEFAULT_RL_DICT_TEXT,
            )

            return _parse_rl_dictionary(DEFAULT_RL_DICT_TEXT)
        except Exception as e:
            print(f"Failed to load fallback dictionary: {e}")
            return []

    def _ensure_default_dictionary(self) -> None:
        """首次启动时，将内置 RL 字典固化为默认 dictionary.json。"""
        if self._dict_path.exists():
            return
        # 优先使用打包的 dictionary.json
        packaged = self._get_packaged_config_path("dictionary.json")
        if packaged:
            try:
                import shutil

                shutil.copy2(str(packaged), str(self._dict_path))
                return
            except Exception:
                pass
        # 回退到代码内置的默认字典文本
        try:
            from strange_uta_game.backend.infrastructure.data.default_dictionary import (
                DEFAULT_RL_DICT_TEXT,
            )

            entries = _parse_rl_dictionary(DEFAULT_RL_DICT_TEXT)
            if entries:
                self._save_json(self._dict_path, entries)
        except Exception as e:
            print(f"初始化默认词典失败: {e}")

    def _copy_packaged_config(self) -> None:
        """从内嵌配置文件复制到用户目录。"""
        packaged = self._get_packaged_config_path("config.json")
        if packaged:
            try:
                import shutil
                shutil.copy2(str(packaged), str(self._config_path))
            except Exception as e:
                print(f"复制内嵌配置失败: {e}")

    def _load_settings(self) -> Dict[str, Any]:
        # 从内嵌 config.json 加载默认值
        defaults = self._load_packaged_defaults()

        if self._config_path.exists():
            try:
                with type(self)._io_lock:
                    with open(self._config_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                # 以默认值为基础，用户配置覆盖
                self._migrate_display_heights(loaded)
                self._deep_merge(defaults, loaded)
            except Exception as e:
                print(f"加载设置失败: {e}")
        else:
            # 用户配置不存在，使用内嵌默认配置
            packaged = self._get_packaged_config_path("config.json")
            if packaged:
                try:
                    with open(packaged, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        self._deep_merge(defaults, loaded)
                except Exception:
                    pass

        return defaults

    def _load_packaged_defaults(self) -> Dict[str, Any]:
        """从内嵌 config.json 加载默认配置"""
        packaged = self._get_packaged_config_path("config.json")
        if packaged:
            try:
                with open(packaged, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return self._deep_copy_defaults()

    def _deep_copy_defaults(self) -> Dict[str, Any]:
        """递归深拷贝默认设置"""
        return json.loads(json.dumps(self.DEFAULT_SETTINGS))

    def _deep_merge(self, base: Dict, override: Dict) -> None:
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    @staticmethod
    def _migrate_display_heights(settings: Dict[str, Any]) -> None:
        """旧版共用显示高度迁移为波形/声谱两个独立值。"""
        timing = settings.get("timing")
        if not isinstance(timing, dict) or "display_height" not in timing:
            return
        legacy = timing.get("display_height", 120)
        timing.setdefault("waveform_display_height", legacy)
        timing.setdefault("spectrum_display_height", legacy)

    def save(self) -> None:
        """持久化当前设置。

        Provider 模式委托给 ``provider.save``；standalone 模式写
        ``config.json``。两条路径都吞异常并打印日志，不抛。
        """
        if self._provider is not None:
            try:
                save_partial = getattr(self._provider, "save_partial", None)
                if self._dirty_paths and callable(save_partial):
                    save_partial(
                        {
                            path: deepcopy(self.get(path))
                            for path in sorted(self._dirty_paths)
                        }
                    )
                    self._dirty_paths.clear()
                    return
                # deepcopy 出去：provider 应能持有完全独立的副本，避免
                # 后续 AppSettings.set 改写到 provider 内部状态。
                self._provider.save(deepcopy(self._settings))
                self._dirty_paths.clear()
            except Exception as e:
                print(f"保存设置失败 (provider): {e}")
            return
        try:
            # 文件级锁：网络词典自动更新等 daemon 线程与 UI 线程并发写
            # config.json 时避免 open(w) 竞争（Windows 下会 PermissionError）。
            with type(self)._io_lock:
                with open(self._config_path, "w", encoding="utf-8") as f:
                    json.dump(self._settings, f, indent=2, ensure_ascii=False)
            self._dirty_paths.clear()
        except Exception as e:
            print(f"保存设置失败: {e}")

    def reload(self) -> None:
        """从后端存储重新加载配置（discard 内存内未保存的改动）。

        Provider 模式：以内嵌默认值打底，再 deep-merge provider.load() 结果。
        Standalone 模式：走 ``_load_settings()`` 重读 config.json。
        """
        if self._provider is not None:
            self._settings = self._load_packaged_defaults()
            try:
                loaded = self._provider.load() or {}
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                self._migrate_display_heights(loaded)
                self._deep_merge(self._settings, deepcopy(loaded))
            self._dirty_paths.clear()
            return
        self._settings = self._load_settings()
        self._dirty_paths.clear()

    def get(self, path: str, default=None) -> Any:
        keys = path.split(".")
        value = self._settings
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, path: str, value: Any) -> None:
        keys = path.split(".")
        target = self._settings
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        target[keys[-1]] = value
        # dirty 标记供两条路径消费：provider 模式的 save_partial 增量写、
        # reset_shared_instances 丢弃实例前的 flush。
        self._dirty_paths.add(path)

    def get_all(self) -> Dict[str, Any]:
        return self._settings.copy()

    # ── 独立文件：词典 & 演唱者预设 ──

    def _migrate_to_separate_files(self) -> None:
        """将旧 config.json 中的 ruby_dictionary.entries 和 singer_presets 迁移到独立文件。"""
        dirty = False
        # 迁移词典条目
        dict_data = self._settings.get("ruby_dictionary", {})
        if isinstance(dict_data, dict) and "entries" in dict_data:
            entries = dict_data.pop("entries", [])
            if entries and not self._dict_path.exists():
                self._save_json(self._dict_path, entries)
            dirty = True
        # 迁移演唱者预设
        if "singer_presets" in self._settings:
            presets = self._settings.pop("singer_presets", [])
            if presets and not self._singers_path.exists():
                self._save_json(self._singers_path, presets)
            dirty = True
        if dirty:
            self.save()

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        """写入 JSON 文件。"""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"保存文件失败 {path}: {e}")

    @staticmethod
    def _load_json(path: Path, default: Any = None) -> Any:
        """读取 JSON 文件，不存在时回退到内嵌默认文件。"""
        if not path.exists():
            # 尝试从内嵌配置包回退
            packaged = AppSettings._get_packaged_config_path(path.name)
            if packaged:
                try:
                    with open(packaged, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            return default if default is not None else []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"加载文件失败 {path}: {e}")
            return default if default is not None else []

    def load_dictionary(self) -> list:
        """从 dictionary.json 加载用户词典条目。"""
        if self._provider is not None:
            entries = self._provider.load_extra("dictionary", [])
            return deepcopy(entries) if isinstance(entries, list) else []
        return self._load_json(self._dict_path, [])

    def save_dictionary(self, entries: list) -> None:
        """保存用户词典条目到 dictionary.json。"""
        if self._provider is not None:
            self._provider.save_extra("dictionary", deepcopy(entries))
            return
        self._save_json(self._dict_path, entries)

    def register_dictionary_word(self, word: str, reading: str) -> None:
        """新增单个词条到词典顶部（最高优先级）。

        允许同 word 多条共存：完全相同 (word, reading) 才去重，避免重复点击产生
        无意义副本；其他读音变体并存，由 lookup 顺序（自顶向下首个命中）决定应用。
        """
        word = (word or "").strip()
        reading = (reading or "").strip()
        if not word:
            return
        entries = self.load_dictionary()
        new_entry = {"enabled": True, "word": word, "reading": reading}
        # 仅当存在完全一致 (word, reading) 时跳过添加；不再清除其他读音变体
        for e in entries:
            if (e.get("word") or "").strip() == word and (e.get("reading") or "").strip() == reading:
                return
        entries.insert(0, new_entry)
        self.save_dictionary(entries)

    def delete_dictionary_entry(self, word: str, reading: str) -> int:
        """从本地词典 + 所有网络源缓存中删除完全匹配 ``(word, reading)`` 的条目。

        用于「查询候补字典」窗口的删除功能：让某条读音从所有词典源彻底消失，
        不再被注音 lookup 命中。本地删除持久；网络源条目下次拉取（自动 / 手动
        更新）会被重新写回——删除不持久，这是网络源的固有语义。

        Args:
            word: 原文词（完全相等匹配）。
            reading: annotated 注音（完全相等匹配，去首尾空白后比较）。

        Returns:
            删除的条目总数（本地 + 各网络源累计）。未匹配 / word 为空 → 0。
        """
        word = (word or "").strip()
        reading = (reading or "").strip()
        if not word:
            return 0

        def _match(e: dict) -> bool:
            return (
                (e.get("word") or "").strip() == word
                and (e.get("reading") or "").strip() == reading
            )

        removed = 0

        # 本地词典
        local = self.load_dictionary()
        kept = [e for e in local if not _match(e)]
        if len(kept) != len(local):
            removed += len(local) - len(kept)
            self.save_dictionary(kept)

        # 网络源缓存（遍历每个源的 entries；仅在确有删除时回写，避免无谓落盘）
        doc = self.load_network_dictionary()
        changed = False
        for src in doc.get("sources") or []:
            entries = src.get("entries") or []
            new_entries = [e for e in entries if not _match(e)]
            if len(new_entries) != len(entries):
                removed += len(entries) - len(new_entries)
                src["entries"] = new_entries
                changed = True
        if changed:
            self.save_network_dictionary(doc)

        return removed

    def import_rl_dictionary(self, text: str) -> tuple:
        """导入 RL 字典文本：新条目整体插入到顶部，保持原文件顺序。

        允许同 word 多条共存：仅当 (word, reading) 与已有条目完全一致时跳过（去重），
        其他读音变体并存。lookup 时由顺序（自顶向下首个命中）决定优先级。

        Returns:
            (added, skipped): 新增数量与因完全重复被跳过的数量。
        """
        new_entries = _parse_rl_dictionary(text)
        if not new_entries:
            return (0, 0)
        entries = self.load_dictionary()
        existing_keys = {
            ((e.get("word") or "").strip(), (e.get("reading") or "").strip())
            for e in entries
        }
        to_prepend: list = []
        added = 0
        skipped = 0
        for entry in new_entries:
            word = (entry.get("word") or "").strip()
            reading = (entry.get("reading") or "").strip()
            if not word:
                continue
            key = (word, reading)
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)
            to_prepend.append({"enabled": True, "word": word, "reading": reading})
            added += 1
        # 整批插入到顶部，保留 new_entries 原顺序（首条最优先）
        entries = to_prepend + entries
        self.save_dictionary(entries)
        return (added, skipped)

    # ──────────────────────────────────────────────
    # 网络词典：meta 存 config.json[network_dictionary]，
    #          cache（entries + last_fetched）存 network_dictionary.json
    # ──────────────────────────────────────────────

    def load_network_dictionary(self) -> dict:
        """加载统一形态的网络词典文档（合并 meta + cache）。

        meta（启用/源列表/源排序/URL/名称等设置）从 ``config.json[network_dictionary]``
        读取；cache（每源 entries / last_fetched）从 ``network_dictionary.json`` 读取。
        缺失任一文件用 :data:`DEFAULT_NETWORK_DICTIONARY_META` 兜底；自动补齐内置源。
        旧版（一体式 ``network_dictionary.json``）会被自动迁移：拆分后写回。

        Provider 模式下，meta 仍来自主 config，cache 来自宿主的
        ``lyrics_timing_network_dictionary`` namespace。
        """
        from strange_uta_game.backend.infrastructure.network_dictionary import (
            DEFAULT_NETWORK_DICTIONARY_META,
            ensure_builtin_sources,
            merge_meta_and_cache,
        )

        meta = self.get("network_dictionary", None)
        if not isinstance(meta, dict):
            meta = json.loads(json.dumps(DEFAULT_NETWORK_DICTIONARY_META))

        cache: dict = {}
        if self._provider is not None:
            loaded_cache = self._provider.load_extra("network", {})
            if isinstance(loaded_cache, dict):
                cache = deepcopy(loaded_cache)
        elif self._network_dict_path.exists():
            try:
                with open(self._network_dict_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                raw = None
            # 兼容旧一体式文档：``{"enabled","sources":[{...,"entries":[...]}],
            # "source_order":[...]}`` —— 拆出 meta 部分覆盖到 config.json，cache 留下。
            if isinstance(raw, dict) and "sources" in raw and isinstance(raw["sources"], list):
                if raw["sources"] and "entries" in raw["sources"][0]:
                    # 旧形态：拆分 + 迁移
                    from strange_uta_game.backend.infrastructure.network_dictionary import (
                        split_meta_and_cache,
                    )
                    migrated_meta, cache = split_meta_and_cache(raw)
                    self.set("network_dictionary", migrated_meta)
                    self._save_json(self._network_dict_path, cache)
                    meta = migrated_meta
                else:
                    cache = raw
            elif isinstance(raw, dict):
                cache = raw

        doc = merge_meta_and_cache(meta, cache)
        return ensure_builtin_sources(doc)

    def save_network_dictionary(self, doc: dict) -> None:
        """保存统一文档：meta → ``config.json``，cache → ``network_dictionary.json``。

        Provider 模式下 meta 仍走 self.set / self.save（由 provider 接管），
        cache 写入宿主的 ``lyrics_timing_network_dictionary`` namespace。

        ``network_dictionary`` 下还包含 ``auto_update`` 与
        ``last_auto_update_at`` 等非源配置；保存源元数据时必须保留这些字段。
        """
        from strange_uta_game.backend.infrastructure.network_dictionary import (
            split_meta_and_cache,
        )
        meta, cache = split_meta_and_cache(doc)
        existing_meta = self.get("network_dictionary", {})
        settings_meta = deepcopy(existing_meta) if isinstance(existing_meta, dict) else {}
        settings_meta.update(meta)
        self.set("network_dictionary", settings_meta)
        self.save()  # 立即落盘 meta 到 config.json
        if self._provider is not None:
            self._provider.save_extra("network", deepcopy(cache))
            return
        self._save_json(self._network_dict_path, cache)

    def maybe_auto_update_network_dictionary(self, force: bool = False) -> "tuple[list, list, bool]":
        """检查并按需自动拉取所有启用的网络源。

        触发条件：``network_dictionary.auto_update.enabled=True`` 且距离
        ``last_auto_update_at`` 已超过 ``(interval_value, interval_unit)``。
        ``force=True`` 时无视条件强制执行（"立即同步"按钮可用）。

        Provider 模式与 standalone 使用同一更新流程；cache 分别由 provider
        namespace 与 ``network_dictionary.json`` 持久化。

        非阻塞建议：调用方在后台线程中调用本方法（HTTP 慢，UI 线程不可阻塞）。

        Args:
            force: 是否无视开关与间隔强制拉取。

        Returns:
            ``(ok_msgs, fail_msgs, ran)``：成功/失败消息列表 + 是否真的执行了拉取。
            未到期 / 未启用 / ``enabled=False`` → ``ran=False`` 且 msgs 为空。
        """
        from strange_uta_game.backend.infrastructure.network_dictionary import (
            auto_update_enabled_sources,
            is_auto_update_due,
            resolve_app_proxies,
        )

        au_enabled = bool(self.get("network_dictionary.auto_update.enabled", False))
        if not force and not au_enabled:
            return ([], [], False)

        interval_value = int(self.get("network_dictionary.auto_update.interval_value", 1) or 1)
        interval_unit = str(self.get("network_dictionary.auto_update.interval_unit", "week") or "week")
        last_at = float(self.get("network_dictionary.last_auto_update_at", 0) or 0)
        if not force and not is_auto_update_due(last_at, interval_value, interval_unit):
            return ([], [], False)

        # 网络词典总开关未启用时，自动更新无意义（用户也看不到结果）—— 仍允许拉取以保持
        # entries 最新；但若希望节流可以这里返回。当前选择"拉取"，因为下次启用立即生效。
        doc = self.load_network_dictionary()
        # 拉取走「设置 → 网络与代理」的代理配置，与更新器/模型下载同一套
        proxies = resolve_app_proxies(self)
        ok_msgs, fail_msgs = auto_update_enabled_sources(doc, proxies=proxies)
        self.save_network_dictionary(doc)

        import time as _time
        self.set("network_dictionary.last_auto_update_at", int(_time.time()))
        self.save()
        return (ok_msgs, fail_msgs, True)

    def load_effective_dictionary(self) -> list:
        """加载用于注音 lookup 的完整词典：本地 + 启用的网络源，按全局优先级拼接。

        ``DictionaryEditDialog`` 等编辑场景仍使用 :meth:`load_dictionary`
        （仅本地）；只读消费者（``analyze_sentence`` 等）应改调本方法。
        """
        from strange_uta_game.backend.infrastructure.network_dictionary import (
            flatten_effective_dictionary,
        )
        local = self.load_dictionary()
        net = self.load_network_dictionary()
        return flatten_effective_dictionary(local, net)

    # ──────────────────────────────────────────────
    # LLM 注音
    # ──────────────────────────────────────────────

    def llm_ruby_active(self) -> bool:
        """LLM 注音是否处于激活态（已启用且连接信息齐全）。"""
        from strange_uta_game.backend.infrastructure.parsers.llm_ruby import (
            LLMRubyConfig,
        )

        return LLMRubyConfig.from_settings(self).enabled and LLMRubyConfig.from_settings(
            self
        ).is_complete()

    def llm_apply_user_dict(self) -> bool:
        """LLM 注音时是否仍应用用户词典（默认 True）。"""
        return bool(self.get("llm_ruby.apply_user_dict", True))

    def build_ruby_analyzer(
        self, lines: Optional[list] = None, annotate_katakana_with_english: bool = False
    ):
        """构建注音分析器：LLM 激活时返回 LLMRubyAnalyzer，否则走本地回退链。

        Args:
            lines: 本批需要注音的行文本列表（LLM 一次批量发送所选行；
                「全部」入口传整首行列表）。LLM 未激活时忽略。
            annotate_katakana_with_english: 开启时让 LLM 为英语外来语片假名返回英文读音
                （无法对应英文的片假名放弃标注）。LLM 未激活时忽略。
        """
        from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
            create_analyzer,
        )

        if not self.llm_ruby_active():
            return create_analyzer()

        from strange_uta_game.backend.infrastructure.parsers.llm_ruby import (
            LLMRubyAnalyzer,
            LLMRubyConfig,
            _resolve_proxies,
        )

        cfg = LLMRubyConfig.from_settings(self)
        return LLMRubyAnalyzer(
            cfg,
            list(lines or []),
            fallback=create_analyzer(),
            proxies=_resolve_proxies(self),
            annotate_katakana_with_english=annotate_katakana_with_english,
        )

    def load_singer_presets(self) -> list:
        """从 singers.json 加载演唱者预设。"""
        if self._provider is not None:
            presets = self._provider.load_extra("singers", [])
            return deepcopy(presets) if isinstance(presets, list) else []
        return self._load_json(self._singers_path, [])

    def save_singer_presets(self, presets: list) -> None:
        """保存演唱者预设到 singers.json。"""
        if self._provider is not None:
            self._provider.save_extra("singers", deepcopy(presets))
            return
        self._save_json(self._singers_path, presets)


def build_annotated_reading(
    word: str,
    per_char_ruby: list,
    per_char_linked: "list[bool] | None" = None,
) -> str:
    """把 ``(word, per_char_ruby[, per_char_linked])`` 序列化为 annotated 行内格式。

    Args:
        word: 原文词（N 个字符）。
        per_char_ruby: 长度为 N 的列表，每项是对应字符的 ``Ruby`` 对象或 ``None``。
            ``Ruby.parts`` 中每个 ``RubyPart.text`` 对应一个 mora，用 ``|`` 连接写入。
        per_char_linked: 长度为 N 的布尔列表，``True`` 表示该字符与下一字符连词。
            若为 ``None``，则视所有字符独立（不连词）。

    完全尊重用户输入：不做自注音剥离、不做额外 mora 拆分、不做任何读音变换。

    连词处理：
        ``linked[i]=True`` 表示字符 ``i`` 与 ``i+1`` 在同一连词块内。
        连词链 ``[i..j]`` 合并为单个 annotated 块
        ``{word[i:j+1]||seg_i,seg_{i+1},...,seg_j}``，
        每段 ``seg_k`` 为该字符各 ``RubyPart.text`` 以 ``|`` 连接；无 ruby 的字符用空段。

    例：
        word="大冒険", ruby=[R("だ","い"), R("ぼ","う"), R("け","ん")], linked=[True,True,False]
        → ``{大冒険||だ|い,ぼ|う,け|ん}``

        word="食べ物", ruby=[R("た"), None, R("も","の")], linked=[False,False,False]
        → ``{食||た}べ{物||も|の}``
    """

    def _seg(ruby: "object | None") -> str:
        """把一个字符的 Ruby 对象序列化为段内字符串（mora 用 | 连接）。"""
        if ruby is not None and hasattr(ruby, "parts") and ruby.parts:
            return "|".join(p.text for p in ruby.parts if p.text)
        return ""

    n = len(word)
    linked: "list[bool]" = list(per_char_linked) if per_char_linked else [False] * n

    out: list = []
    i = 0
    while i < n:
        # 找到从 i 开始的连词链末尾 j（linked[i..j-1] 均为 True，linked[j] 为 False 或越界）
        j = i
        while j < n - 1 and j < len(linked) and linked[j]:
            j += 1
        # 链覆盖 [i..j]
        chain_chars = word[i : j + 1]
        chain_rubies = [
            per_char_ruby[k] if k < len(per_char_ruby) else None
            for k in range(i, j + 1)
        ]
        chain_segs = [_seg(r) for r in chain_rubies]

        if j > i:
            # 连词块：合并为 {chars||seg_i,seg_{i+1},...,seg_j}
            segs_str = ",".join(chain_segs)
            out.append(f"{{{chain_chars}||{segs_str}}}")
        else:
            # 单字
            seg = chain_segs[0]
            if seg:
                out.append(f"{{{chain_chars}||{seg}}}")
            else:
                out.append(chain_chars)
        i = j + 1

    return "".join(out)


def _parse_rl_dictionary(text: str) -> list:
    """解析 RL 字典文件格式（薄包装，实现位于后端
    :mod:`strange_uta_game.backend.infrastructure.parsers.rl_dictionary`）。

    保留此函数以兼容 ``dictionary_dialog`` / ``settings_interface`` 的历史导入路径。
    """
    from strange_uta_game.backend.infrastructure.parsers.rl_dictionary import (
        parse_rl_dictionary,
    )

    return parse_rl_dictionary(text)
