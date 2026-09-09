"""歌词文件解析工具。

提供歌词格式检测和解析功能，支持 LRC/SRT/ASS/Nicokara/内联格式等。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QCoreApplication


def _tr(s: str) -> str:
    """模块级 tr 别名（lyric_loader 是纯函数模块、无 QObject self）。"""
    return QCoreApplication.translate("LyricLoader", s)

from strange_uta_game.backend.domain import Character, Ruby, RubyPart, Sentence, Singer
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
    LRCParser,
    NicokaraParser,
    UtatenRubyParser,
    nicokara_result_to_sentences,
    parse_to_sentences,
)
from strange_uta_game.backend.infrastructure.parsers.inline_format import (
    sentences_from_inline_text,
    split_into_moras,
)
from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
    is_kasugamuki_content,
    krl_role_names,
    sentences_from_kasugamuki,
)
from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    CharType,
    get_char_type,
)


# 带内联时间戳的注音文本格式（|| 分隔符，[mm:ss.xx] 时间戳）
_ANNOTATED_PATTERN = re.compile(r"\{[^}]+\|\|[^}]*\}")
# 内联格式检测（包括 inline 和纯 RLF 文本格式）
_INLINE_PATTERN = re.compile(r"\[.*?\|.*?\]|{[^}]+\|[^}]+}")
# LRC 时间标签检测
_LRC_PATTERN = re.compile(r"\[\d{2}:\d{2}\.\d{2,3}\]")
# ASS 格式检测
_ASS_PATTERN = re.compile(r"^\[Script Info\]|^Dialogue:\s*\d+", re.MULTILINE)
# SRT 格式检测
_SRT_PATTERN = re.compile(
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}"
)


def _ruby_text(ch: Character) -> str:
    return "".join(part.text for part in ch.ruby.parts) if ch.ruby else ""


def _is_kana_char(ch: str) -> bool:
    if len(ch) != 1:
        return False
    return get_char_type(ch) in (
        CharType.HIRAGANA,
        CharType.KATAKANA,
        CharType.SOKUON,
        CharType.LONG_VOWEL,
    )


def _utaten_block_ranges(sentence: Sentence) -> list[tuple[int, int, str]]:
    """Return ruby block ranges from the raw Utaten import sentence.

    ``parse_to_sentences(..., utaten_format=False)`` keeps a multi-character
    Utaten ruby as one linked block: the first char carries the full reading and
    ``linked_to_next`` marks the covered span.
    """
    ranges: list[tuple[int, int, str]] = []
    chars = sentence.characters
    i = 0
    while i < len(chars):
        reading = _ruby_text(chars[i])
        if not reading:
            i += 1
            continue
        end = i
        while end < len(chars) - 1 and chars[end].linked_to_next:
            end += 1
        ranges.append((i, end, reading))
        i = end + 1
    return ranges


def _reference_tokens_for_block(
    reference: Sentence,
    start: int,
    end: int,
) -> list[str]:
    tokens: list[str] = []
    for idx in range(start, end + 1):
        if idx >= len(reference.characters):
            tokens.append("")
            continue
        ch = reference.characters[idx]
        ruby = _ruby_text(ch)
        if ruby:
            tokens.append(ruby)
        elif _is_kana_char(ch.char):
            tokens.append(ch.char)
        else:
            tokens.append("")
    return tokens


def _split_reading_by_reference(reading: str, tokens: list[str]) -> Optional[list[str]]:
    """Split a Utaten reading by normal-pipeline reference tokens.

    The split is accepted only when the reference tokens concatenate exactly to
    the Utaten reading, so analyzer/user-dictionary readings never replace the
    source reading. They only provide boundaries.
    """
    if not tokens:
        return None
    if "".join(tokens) == reading:
        return tokens
    return None


def _legacy_utaten_split(word: str, reading: str) -> tuple[list[str], bool]:
    from strange_uta_game.backend.infrastructure.parsers.kanji_reading_split import (
        compute_per_kanji_readings,
    )

    return compute_per_kanji_readings(word, reading)


def _clean_user_dict_split(
    word: str,
    reading: str,
    user_dict: list[dict],
) -> Optional[list[str]]:
    for entry in user_dict:
        if not entry.get("enabled", True):
            continue
        if entry.get("word") != word:
            continue
        dict_reading = str(entry.get("reading") or "")
        if "," not in dict_reading:
            continue
        parts = [p.strip() for p in dict_reading.split(",")]
        if len(parts) == len(word) and all(parts) and "".join(parts) == reading:
            return parts
    return None


def _set_utaten_char_ruby(ch: Character, segment: str, reference: Optional[Character]) -> None:
    ch.ruby = None
    if segment and not (_is_kana_char(ch.char) and segment == ch.char):
        ch.set_ruby(Ruby(parts=[RubyPart(text=segment)]))
    ref_count = reference.check_count if reference is not None else None
    if ref_count is None:
        ref_count = len(split_into_moras(segment)) if segment else 0
    if ch.ruby:
        # cc 必须等于注音段自身的 mora 数：utaten 注音段是权威读音，而
        # reference.check_count 来自对原文汉字跑分析器得到的读音拍数。当て字等
        # 场景下二者 mora 数不一致，若用 ref_count 当目标拍数，set_check_count 会
        # 用停顿符（占位符）把不足的拍补齐，导致字符上凭空多出空拍。
        seg_moras = len(split_into_moras(segment))
        ch.set_check_count(max(1, seg_moras), ruby_split_mode="mora")
    else:
        ch.set_check_count(max(0, ref_count), force=True)


def _align_utaten_sentences_with_auto_check(
    sentences: list[Sentence],
    *,
    setting_iface=None,
    auto_check_flags: Optional[dict] = None,
    user_dict: Optional[list] = None,
    annotate_katakana_with_english: bool = False,
    progress_cb=None,
) -> None:
    """Align Utaten ruby blocks with the normal SUG auto-check pipeline.

    Utaten remains the authority for reading text. The normal pipeline is used
    only to decide per-character boundaries, checkpoint counts, and linked-word
    state, so user dictionary entries affect Utaten imports the same way they
    affect normal LRC imports.

    ``auto_check_flags``, ``user_dict``, ``annotate_katakana_with_english`` can be
    pre-read in the main thread and passed in so this function is safe to call
    from a background worker without touching Qt objects.
    """
    if not sentences:
        return

    _user_dict: list[dict] = user_dict if user_dict is not None else []
    try:
        from strange_uta_game.backend.application import AutoCheckService

        if auto_check_flags is not None and user_dict is not None:
            # Pre-read values provided (worker-mode: avoids touching Qt/setting_iface)
            _flags = auto_check_flags
            _annotate = annotate_katakana_with_english
        else:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            app_settings = (
                setting_iface.get_settings()
                if setting_iface is not None and hasattr(setting_iface, "get_settings")
                else setting_iface
            ) or AppSettings()
            _flags = app_settings.get_all().get("auto_check", {})
            _user_dict = app_settings.load_effective_dictionary()
            _annotate = app_settings.get(
                "ruby_dictionary.annotate_katakana_with_english", False
            )

        auto_check = AutoCheckService(
            auto_check_flags=_flags,
            user_dictionary=_user_dict,
            annotate_katakana_with_english=_annotate,
        )
    except Exception:
        auto_check = None

    total = len(sentences)
    for idx, sentence in enumerate(sentences):
        if progress_cb and total > 0:
            progress_cb(_tr("正在对齐注音 {idx}/{total} 行").format(idx=idx + 1, total=total))
        ranges = _utaten_block_ranges(sentence)
        if not ranges:
            continue

        reference: Optional[Sentence] = None
        if auto_check is not None:
            try:
                reference = Sentence.from_text(sentence.text, sentence.singer_id)
                auto_check.apply_to_sentence(reference, skip_romanize=True)
            except Exception:
                reference = None

        for start, end, reading in ranges:
            if end >= len(sentence.characters):
                continue
            word = "".join(ch.char for ch in sentence.characters[start : end + 1])
            split_parts: Optional[list[str]] = None
            is_ateji = True
            force_unlinked = False
            clean_dict_parts = _clean_user_dict_split(word, reading, _user_dict)
            if clean_dict_parts is not None:
                split_parts = clean_dict_parts
                is_ateji = False
                force_unlinked = True
            if reference is not None and end < len(reference.characters):
                tokens = _reference_tokens_for_block(reference, start, end)
                ref_split = _split_reading_by_reference(reading, tokens)
                if split_parts is None and ref_split is not None:
                    split_parts = ref_split
                    is_ateji = False
            if split_parts is None:
                split_parts, is_ateji = _legacy_utaten_split(word, reading)

            for offset, idx in enumerate(range(start, end + 1)):
                ref_ch = (
                    reference.characters[idx]
                    if reference is not None and idx < len(reference.characters)
                    else None
                )
                segment = split_parts[offset] if offset < len(split_parts) else ""
                _set_utaten_char_ruby(sentence.characters[idx], segment, ref_ch)

            for idx in range(start, end):
                if force_unlinked:
                    sentence.characters[idx].linked_to_next = False
                elif reference is not None and end < len(reference.characters) and not is_ateji:
                    sentence.characters[idx].linked_to_next = bool(
                        reference.characters[idx].linked_to_next
                    )
                else:
                    sentence.characters[idx].linked_to_next = bool(is_ateji)
            sentence.characters[end].linked_to_next = False


def _is_json_content(content: str) -> bool:
    """检测内容是否为 JSON 格式（可能是 SUG 项目文件）。"""
    stripped = content.strip()
    if not stripped:
        return False
    # 快速检查：JSON 必须以 { 或 [ 开头
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    # 尝试解析 JSON
    try:
        data = json.loads(stripped)
        # 检查是否包含 SUG 项目文件的特征字段
        if isinstance(data, dict):
            sug_keys = {"version", "id", "metadata", "singers", "sentences"}
            if sug_keys.intersection(data.keys()):
                return True
        return False
    except (json.JSONDecodeError, ValueError):
        return False


def _sync_nicokara_metadata_to_settings(metadata: dict, *, setting_iface=None) -> None:
    """把 Nicokara 解析出的 @ 元数据写回 AppSettings.nicokara_tags（覆盖式）。

    SHINTA 2025 规格 K：未知 @ 标签需要保留并在导出时原样回写，
    实现跨工具 round-trip 兼容。

    Args:
        metadata: NicokaraParseResult.metadata，{key: value} 扁平字典（key 不含 @）。
        setting_iface: 可选 SettingsInterface 实例。给定时使用其共享 _settings，
            确保后续 _settings.save() 不会用启动期旧内存覆盖磁盘；
            None 时回退到新建 AppSettings()（仅写磁盘，旧内存仍会回滚——
            仅用于无 UI 上下文的纯测试场景）。

    映射：
        Title         → tags["title"]
        Artist        → tags["artist"]
        Album         → tags["album"]
        TaggingBy     → tags["tagging_by"]
        SilencemSec   → tags["silence_ms"] (int)
        Offset        → tags["offset"] (int；解析失败回退 custom 保留原文)
        HeadOffset    → tags["head_offset"] (int；解析失败回退 custom 保留原文)
        其余 *        → tags["custom"]，每项形如 "@Key=Value"

    覆盖式：旧的 nicokara_tags 全部被替换，不做合并——
    用户「每次写入项目都换」的语义。
    """
    if not metadata:
        return
    try:
        from strange_uta_game.frontend.settings.settings_interface import (
            AppSettings,
        )
    except Exception:
        return

    known_map = {
        "Title": "title",
        "Artist": "artist",
        "Album": "album",
        "TaggingBy": "tagging_by",
    }
    int_map = {
        "SilencemSec": "silence_ms",
        "Offset": "offset",
        "HeadOffset": "head_offset",
    }
    tags: dict = {}
    custom: list = []
    for key, value in metadata.items():
        if key in known_map:
            tags[known_map[key]] = value
        elif key in int_map:
            try:
                tags[int_map[key]] = int(value)
            except (TypeError, ValueError):
                custom.append(f"@{key}={value}")
        elif key.startswith("_Emoji"):
            # NicokaraParser 用 _EmojiN 键存储完整 @Emoji 行，还原为 @Emoji=
            custom.append(f"@Emoji={value}")
        else:
            custom.append(f"@{key}={value}")
    if custom:
        tags["custom"] = custom

    try:
        if setting_iface is not None and hasattr(setting_iface, "get_settings"):
            settings = setting_iface.get_settings()
        else:
            settings = AppSettings()
        settings.set("nicokara_tags", tags)
        settings.save()  # 持久化到磁盘——旧实现遗漏 save 导致新建实例的修改根本未落盘
    except Exception:
        # 写入失败不阻断导入；exporter 端 fallback 仍可用旧值
        pass


def detect_lyric_format(content: str) -> str:
    """检测歌词内容的格式。

    Returns:
        格式名称: "sug", "utaten", "krl", "inline", "nicokara", "ass", "srt", "lrc", "text"
    """
    # SUG/JSON 格式检测（最高优先级，避免误解析）
    if _is_json_content(content):
        return "sug"
    if UtatenRubyParser.is_utaten_format(content):
        return "utaten"
    # KRL must precede generic inline detection: both formats use {text|ruby},
    # but KRL checkpoints omit inline's leading N| count and may add >romaji.
    if is_kasugamuki_content(content):
        return "krl"
    # 带内联时间戳的注音文本格式（|| 分隔符）优先于内联格式检测
    if _ANNOTATED_PATTERN.search(content):
        return "annotated"
    # 内联格式检测（包括 inline 和纯 RLF 文本格式）
    if _INLINE_PATTERN.search(content):
        return "inline"
    if NicokaraParser.is_nicokara_format(content):
        return "nicokara"
    if _ASS_PATTERN.search(content):
        return "ass"
    if _SRT_PATTERN.search(content):
        return "srt"
    if _LRC_PATTERN.search(content):
        return "lrc"
    return "text"


def parse_lyric_content(
    content: str,
    default_singer_id: str,
    project_singers: Optional[List[Singer]] = None,
    software_compensation_ms: int = 0,
    *,
    setting_iface=None,
    auto_check_flags: Optional[dict] = None,
    user_dict: Optional[list] = None,
    annotate_katakana_with_english: bool = False,
    skip_settings_sync: bool = False,
    progress_cb=None,
) -> Tuple[List[Sentence], bool, List[Singer], dict]:
    """解析歌词内容，返回解析后的句子列表。

    Args:
        content: 歌词文本内容
        default_singer_id: 默认演唱者 ID
        project_singers: 当前项目的演唱者列表（用于 Nicokara/ASS 格式的演唱者匹配）
        software_compensation_ms: 软件导出补偿（毫秒），导入时减去此值

    Returns:
        (sentences, is_nicokara, new_singers, metadata):
        - sentences: 解析后的句子列表
        - is_nicokara: 是否为 Nicokara 格式
        - new_singers: Nicokara/ASS 格式中需要新增的演唱者列表
        - metadata: 格式元数据字典（如 ASS 的 {"title": ..., "generator": ...}）；
                    其他格式为空字典

    Raises:
        ValueError: 当内容是 SUG 项目文件格式时（由调用方处理为项目加载）
    """

    def _apply_compensation(sentences: List[Sentence]) -> List[Sentence]:
        """应用导入补偿（减去软件导出补偿）"""
        if software_compensation_ms == 0:
            return sentences
        for sentence in sentences:
            for ch in sentence.characters:
                if ch.timestamps:
                    ch.timestamps = [
                        max(0, ts - software_compensation_ms)
                        for ts in ch.timestamps
                    ]
                if ch.sentence_end_ts is not None:
                    ch.sentence_end_ts = max(
                        0, ch.sentence_end_ts - software_compensation_ms
                    )
        return sentences

    fmt = detect_lyric_format(content)
    is_nicokara = False
    new_singers: List[Singer] = []

    # SUG 项目文件格式：抛出异常，由调用方处理为项目加载
    if fmt == "sug":
        raise ValueError("__SUG_PROJECT__")

    if fmt == "utaten":
        if progress_cb:
            progress_cb(_tr("正在解析 UtaTen 格式..."))
        parser = UtatenRubyParser()
        parsed_lines = parser.parse(content)
        sentences = parse_to_sentences(parsed_lines, default_singer_id, utaten_format=False)
        _align_utaten_sentences_with_auto_check(
            sentences,
            setting_iface=setting_iface,
            auto_check_flags=auto_check_flags,
            user_dict=user_dict,
            annotate_katakana_with_english=annotate_katakana_with_english,
            progress_cb=progress_cb,
        )
        return _apply_compensation(sentences), False, [], {"format": "utaten"}

    if fmt == "krl":
        if progress_cb:
            progress_cb(_tr("正在解析 Kirakara 格式..."))
        # 【角色名】 标签 → Singer.id：优先匹配已有同名 singer，否则新建
        # （合唱标签如 miku+rin 作为一个整体名处理）
        singer_colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
            "#C9B1FF", "#F7DC6F", "#82E0AA", "#F1948A", "#85C1E9",
        ]
        singer_name_to_id: dict = {}
        for idx, name in enumerate(krl_role_names(content)):
            existing_id = None
            if project_singers:
                for s in project_singers:
                    if s.name == name:
                        existing_id = s.id
                        break
            if existing_id:
                singer_name_to_id[name] = existing_id
            else:
                color = singer_colors[idx % len(singer_colors)]
                new_singer = Singer(name=name, color=color, is_default=False)
                singer_name_to_id[name] = new_singer.id
                new_singers.append(new_singer)
        sentences = sentences_from_kasugamuki(
            content, default_singer_id, name_to_singer_id=singer_name_to_id
        )
        # 春日向/KRL 自带注音与逐字时间轴 → 交由上层弹「保留原有注音」三选一
        return (
            _apply_compensation(sentences),
            False,
            new_singers,
            {"format": "krl", "prompt_ruby_choice": True},
        )

    # 带内联时间戳的注音文本格式（|| 分隔符）
    if fmt == "annotated":
        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            parse_timed_line,
        )

        raw_lines = content.split("\n")
        inherited = default_singer_id
        sentences = []
        for raw_line in raw_lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            chars, inherited = parse_timed_line(
                stripped,
                name_to_singer_id=None,
                default_singer_id=default_singer_id,
                inherited_singer_id=inherited,
                offset_ms=0,
            )
            if chars:
                sentences.append(Sentence(
                    singer_id=inherited,
                    characters=chars,
                ))
        return _apply_compensation(sentences), False, [], {}

    # 内联格式（包括 inline 和纯 RLF 文本格式）
    if fmt == "inline":
        sentences = sentences_from_inline_text(content, default_singer_id)
        return _apply_compensation(sentences), False, [], {}

    # Nicokara 格式
    if fmt == "nicokara":
        if progress_cb:
            progress_cb(_tr("正在解析 Nicokara 格式..."))
        is_nicokara = True
        parser = NicokaraParser()
        result = parser.parse(content)

        # 为 singer_key 建立映射
        singer_key_to_id: dict = {}
        singer_colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
            "#C9B1FF", "#F7DC6F", "#82E0AA", "#F1948A", "#85C1E9",
        ]

        # 收集所有 singer_key
        all_singer_keys: set = set()
        for singer_key in result.singer_definitions:
            all_singer_keys.add(singer_key)
        for line in result.lines:
            if line.line_singer_key:
                all_singer_keys.add(line.line_singer_key)
            for _, sk in line.char_singer_map.items():
                all_singer_keys.add(sk)

        # 匹配已有演唱者或创建新的
        for idx, singer_key in enumerate(sorted(all_singer_keys)):
            display_name = (
                result.singer_definitions.get(singer_key, singer_key) or singer_key
            )
            # 先查找已有演唱者
            existing_id = None
            if project_singers:
                for s in project_singers:
                    if s.name == display_name:
                        existing_id = s.id
                        break
            if existing_id:
                singer_key_to_id[singer_key] = existing_id
            else:
                color = singer_colors[idx % len(singer_colors)]
                new_singer = Singer(name=display_name, color=color, is_default=False)
                singer_key_to_id[singer_key] = new_singer.id
                new_singers.append(new_singer)

        # 使用 nicokara_result_to_sentences 保留原有注音和时间戳
        sentences = nicokara_result_to_sentences(
            result, singer_key_to_id, default_singer_id,
            progress_cb=progress_cb,
        )

        # SHINTA 2025 规格透明性 (差异 K)：把解析到的 @ 元数据写回 AppSettings.nicokara_tags，
        # 覆盖式（每次导入一个 Nicokara 文件即代表用户切换到新项目）。
        # 已知键 (@Title/@Artist/@Album/@TaggingBy/@SilencemSec) 落到对应字段；
        # 其余未知 @ 标签原样收集到 tags["custom"]，导出器 round-trip 时按行回写。
        if skip_settings_sync:
            # Worker 模式：延迟到主线程回调中用 setting_iface 同步，避免绕过共享实例。
            nicokara_meta_out = {"_nicokara_raw_meta": result.metadata}
        else:
            _sync_nicokara_metadata_to_settings(
                result.metadata, setting_iface=setting_iface
            )
            nicokara_meta_out = {}

        return _apply_compensation(sentences), is_nicokara, new_singers, nicokara_meta_out

    # ASS 格式
    if fmt == "ass":
        if progress_cb:
            progress_cb(_tr("正在解析 ASS 格式..."))
        from strange_uta_game.backend.infrastructure.parsers.ass_parser import (
            ASSParser,
        )

        parser = ASSParser()
        parsed_lines = parser.parse(content)

        # 收集所有 per-char singer 显示名（{\sing_<name>} 解析产物）
        all_singer_names: set = set()
        for pl in parsed_lines:
            for name in pl.char_singer_map.values():
                if name:
                    all_singer_names.add(name)

        # 名字 → Singer.id：优先匹配已有同名 singer，否则新建
        singer_name_to_id: dict = {}
        singer_colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
            "#C9B1FF", "#F7DC6F", "#82E0AA", "#F1948A", "#85C1E9",
        ]
        for idx, name in enumerate(sorted(all_singer_names)):
            existing_id = None
            if project_singers:
                for s in project_singers:
                    if s.name == name:
                        existing_id = s.id
                        break
            if existing_id:
                singer_name_to_id[name] = existing_id
            else:
                color = singer_colors[idx % len(singer_colors)]
                new_singer = Singer(name=name, color=color, is_default=False)
                singer_name_to_id[name] = new_singer.id
                new_singers.append(new_singer)

        sentences = parse_to_sentences(
            parsed_lines, default_singer_id, singer_name_to_id=singer_name_to_id
        )
        meta = parser.parse_metadata()
        meta["format"] = "ass"
        # 含卡拉OK逐字时间轴或 Aegisub 注音语法的 ASS → 上层弹三选一
        if parser.has_karaoke_tags or parser.has_ruby_syntax:
            meta["prompt_ruby_choice"] = True
        return _apply_compensation(sentences), False, new_singers, meta

    # SRT 格式
    if fmt == "srt":
        if progress_cb:
            progress_cb(_tr("正在解析 SRT 格式..."))
        from strange_uta_game.backend.infrastructure.parsers.srt_parser import (
            SRTParser,
        )

        parser = SRTParser()
        parsed_lines = parser.parse(content)
        sentences = parse_to_sentences(parsed_lines, default_singer_id)
        return _apply_compensation(sentences), False, [], {}

    # LRC 格式
    if fmt == "lrc":
        if progress_cb:
            progress_cb(_tr("正在解析 LRC 格式..."))
        lrc_parser = LRCParser()
        parsed_lines = lrc_parser.parse(content)
        sentences = parse_to_sentences(parsed_lines, default_singer_id)
        return _apply_compensation(sentences), False, [], {}

    # 纯文本：按行分割，保留空行作为空 Sentence（维持用户排版）。
    # 仅丢弃文件末尾换行符产生的终止空段，避免无谓追加空行。
    from strange_uta_game.backend.domain import Character

    raw_lines = content.split("\n")
    if len(raw_lines) > 1 and raw_lines[-1] == "" and content.endswith("\n"):
        raw_lines.pop()

    sentences = []
    for raw_line in raw_lines:
        line_text = raw_line.strip()
        if not line_text:
            sentences.append(Sentence(singer_id=default_singer_id, characters=[]))
            continue
        sentence = Sentence(
            singer_id=default_singer_id,
            characters=[Character(char=c) for c in line_text],
        )
        sentences.append(sentence)

    return _apply_compensation(sentences), False, [], {}


def read_lyric_file(path: str) -> Optional[str]:
    """读取歌词文件内容。

    Returns:
        文件内容，读取失败返回 None
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None
