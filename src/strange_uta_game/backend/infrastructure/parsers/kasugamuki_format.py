"""春日向注音格式（单注音 / 双注音带罗马音）序列化。

格式一（单注音）:
  [MM:SS:cc]char[MM:SS:cc]char{kanji|[MM:SS:cc]kana[MM:SS:cc]kana}[MM:SS:cc]char

格式二（双注音带罗马音）:
  {kanji|[MM:SS:cc]kana[MM:SS:cc]kana>[MM:SS:cc]romaji[MM:SS:cc]romaji}
  {kana|>[MM:SS:cc]romaji}          （纯假名，无假名注音层）
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

from strange_uta_game.backend.domain.entities import Sentence
from strange_uta_game.backend.domain.models import (
    Character,
    Ruby,
    RubyPart,
    get_ruby_pause_char,
    strip_ruby_pause_chars,
)
from strange_uta_game.backend.infrastructure.parsers.inline_format import (
    format_timestamp,
    parse_timestamp,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import (
    romanize_sentence_to_self_ruby,
)

# 小假名中，拗音（ゃゅょ等）合并到前一拍；促音（っ）和长音（ー）各为一拍
_YOUON = set("ゃゅょャュョ")
_SOKUON = set("っッ")
_LONG_VOWEL = set("ー")

_KRL_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}:\d{2}:\d{2,3})\]")
_KRL_CONFIG_START_RE = re.compile(r"\A\ufeff?\s*config\s*\{", re.IGNORECASE)
# 行首角色标签：【@角色名】，`+` 连接表示合唱（如 【@miku+rin】）。
# 标签不属于歌词正文；未标注的行沿用上一个角色。
_KRL_ROLE_TAG_RE = re.compile(r"\A\s*【@([^【】\r\n]+)】")


def strip_krl_config(content: str) -> str:
    """Remove an optional leading ``config { ... }`` block from KRL text.

    The config payload is JSON-like and may contain nested objects/arrays or
    braces inside quoted strings, so a non-greedy regular expression is not
    sufficient.  Malformed/unclosed blocks are left untouched rather than
    silently discarding the entire file.
    """
    match = _KRL_CONFIG_START_RE.match(content)
    if match is None:
        return content

    brace_start = content.find("{", match.start())
    depth = 0
    in_string = False
    escaped = False
    for index in range(brace_start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[index + 1 :].lstrip(" \t\r\n")
    return content


def is_kasugamuki_content(content: str) -> bool:
    """Return whether text uses Kirakara/Kasugamuki timed-ruby syntax."""
    body = strip_krl_config(content)
    for block in re.findall(r"\{[^{}\r\n]*\}", body):
        if "|" not in block:
            continue
        # Kirakara's second pronunciation layer is unambiguous.  Single-layer
        # exports are identified by their unnumbered [MM:SS:cc] checkpoints;
        # SUG's generic inline format uses [N|MM:SS:cc] instead.
        if ">" in block or _KRL_TIMESTAMP_RE.search(block):
            return True
    return False


def _parse_krl_layer(layer: str) -> Tuple[List[str], List[int]]:
    """Parse one pronunciation layer into displayed parts and checkpoints."""
    matches = list(_KRL_TIMESTAMP_RE.finditer(layer))
    if not matches:
        return ([layer] if layer else []), []

    parts: List[str] = []
    timestamps: List[int] = []
    leading = layer[: matches[0].start()]
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(layer)
        text = layer[match.end() : end]
        if idx == 0 and leading:
            text = leading + text
        timestamps.append(parse_timestamp(match.group(1)))
        parts.append(text)
    return parts, timestamps


def _krl_block_to_characters(block: str, singer_id: str) -> List[Character]:
    """Parse one KRL block while preserving its linked-group boundary.

    KRL concatenates every member's primary-ruby parts inside one block, so the
    original per-character boundary is not recoverable.  Keep that payload on
    the first character and link all display characters together.  The
    exporter concatenates linked members in the same way, which makes the
    primary subtitle/ruby layer lossless without inventing a split.
    """
    display, separator, annotation = block.partition("|")
    if not separator or not display:
        raise ValueError(f"无效的 KRL 注音块: {{{block}}}")
    kana_layer, has_romaji, romaji_layer = annotation.partition(">")
    kana_parts, kana_timestamps = _parse_krl_layer(kana_layer)
    romaji_parts, romaji_timestamps = _parse_krl_layer(
        romaji_layer if has_romaji else ""
    )

    # The kana layer is the source pronunciation.  For self-ruby blocks such
    # as {しょ|>[00:19:77]sho}, only take timing from the romaji layer.
    timestamps = kana_timestamps or romaji_timestamps
    # Empty kana segments are pause-placeholder beats whose char was stripped
    # on export ({漢|[ts]す[ts][ts]}); restore them in position so ruby parts
    # stay aligned with checkpoints (same contract as the Nicokara importer).
    pause = get_ruby_pause_char()
    ruby_parts = [RubyPart(text=text if text else pause) for text in kana_parts]
    ruby = Ruby(parts=ruby_parts) if ruby_parts else None
    check_count = max(len(timestamps), len(ruby_parts), 1)
    character = Character(
        char=display[0],
        ruby=ruby,
        check_count=check_count,
        timestamps=timestamps[:check_count],
        singer_id=singer_id,
    )
    if ruby is not None and len(ruby.parts) != check_count:
        character.set_check_count(check_count, ruby_split_mode="mora")
    character.push_to_ruby()
    characters = [character]
    for display_char in display[1:]:
        # A non-empty primary ruby layer is the only lossless signal that this
        # multi-character block represents a linked word.  Romaji-only blocks
        # can also be generated for kana mora combinations (e.g. きゃ); that
        # discarded secondary layer must not invent project links.
        if ruby is not None:
            characters[-1].linked_to_next = True
        characters.append(
            Character(char=display_char, check_count=0, singer_id=singer_id)
        )
    return characters


def sentence_from_kasugamuki(line: str, singer_id: str) -> Sentence:
    """Parse one Kirakara/Kasugamuki lyric line."""
    characters: List[Character] = []
    pos = 0
    pending_timestamp: Optional[int] = None
    while pos < len(line):
        if line[pos] == "{":
            end = line.find("}", pos + 1)
            if end < 0:
                raise ValueError("未闭合的 KRL 注音块")
            block_characters = _krl_block_to_characters(
                line[pos + 1 : end], singer_id
            )
            first_character = block_characters[0]
            if pending_timestamp is not None and not first_character.timestamps:
                first_character.check_count = max(first_character.check_count, 1)
                first_character.add_timestamp(pending_timestamp)
            pending_timestamp = None
            characters.extend(block_characters)
            pos = end + 1
            continue

        timestamp_match = _KRL_TIMESTAMP_RE.match(line, pos)
        if timestamp_match:
            timestamp = parse_timestamp(timestamp_match.group(1))
            next_pos = timestamp_match.end()
            # A tag at line end or immediately before another block/tag is a
            # release checkpoint for the preceding lyric unit.
            if characters and (
                next_pos == len(line) or line[next_pos] in "{["
            ):
                previous = characters[-1]
                previous.is_sentence_end = True
                previous.set_sentence_end_ts(timestamp)
            else:
                pending_timestamp = timestamp
            pos = next_pos
            continue

        character = Character(
            char=line[pos],
            check_count=1 if pending_timestamp is not None else 0,
            timestamps=[pending_timestamp] if pending_timestamp is not None else [],
            singer_id=singer_id,
        )
        pending_timestamp = None
        characters.append(character)
        pos += 1

    return Sentence(singer_id=singer_id, characters=characters)


def krl_role_names(content: str) -> List[str]:
    """Collect distinct KRL role-tag names in file order (duet labels intact)."""
    names: List[str] = []
    for line in strip_krl_config(content).splitlines():
        match = _KRL_ROLE_TAG_RE.match(line)
        if match is None:
            continue
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def sentences_from_kasugamuki(
    content: str,
    singer_id: str,
    name_to_singer_id: Optional[Dict[str, str]] = None,
) -> List[Sentence]:
    """Parse KRL text, ignoring an optional foreign-exporter config block.

    Leading ``【角色名】`` role tags are metadata, not lyric text: they are
    stripped from the line and, when ``name_to_singer_id`` provides a mapping,
    the line's singer is switched accordingly (duet labels like ``miku+rin``
    map as one name).  Lines without a tag inherit the previous line's role.
    """
    body = strip_krl_config(content)
    if not body:
        return []
    sentences: List[Sentence] = []
    current_singer = singer_id
    for line in body.splitlines():
        match = _KRL_ROLE_TAG_RE.match(line)
        if match is not None:
            name = match.group(1).strip()
            line = line[match.end() :]
            if name and name_to_singer_id and name in name_to_singer_id:
                current_singer = name_to_singer_id[name]
        sentences.append(sentence_from_kasugamuki(line, current_singer))
    return sentences


def _split_kana_moras(text: str) -> List[str]:
    """将假名文本按拍（モーラ）拆分。"""
    if not text:
        return []
    moras: List[str] = []
    for ch in text:
        if ch in _YOUON and moras:
            moras[-1] += ch
        elif ch in _SOKUON or ch in _LONG_VOWEL:
            moras.append(ch)
        else:
            moras.append(ch)
    return moras


def _get_ts_list(char: Character) -> List[int]:
    if char.global_timestamps and len(char.global_timestamps) == len(char.timestamps):
        return char.global_timestamps
    return char.timestamps


def _get_sentence_end_ts(char: Character) -> Optional[int]:
    if char.sentence_end_ts is None:
        return None
    if char.global_sentence_end_ts is not None:
        return char.global_sentence_end_ts
    return char.sentence_end_ts


def _format_sentence_end(char: Character) -> str:
    """如果字符是停顿点，返回停顿点时间标签字符串；否则返回空串。"""
    if not char.is_sentence_end:
        return ""
    se_ts = _get_sentence_end_ts(char)
    if se_ts is not None:
        return f"[{format_timestamp(se_ts)}]"
    return ""


def _is_kana_text(text: str) -> bool:
    if not text:
        return False
    from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
        CharType,
        get_char_type,
    )
    for ch in text:
        if get_char_type(ch) not in (
            CharType.HIRAGANA,
            CharType.KATAKANA,
            CharType.SOKUON,
            CharType.LONG_VOWEL,
        ):
            return False
    return True


# ── 全局 part 映射（用于罗马音粒子检测） ──


def _build_global_part_index(
    chars: List[Character],
) -> Tuple[List[Tuple[int, int, str]], Dict[int, int]]:
    """构建全局 part 列表和每个字符的起始 part 索引。

    Returns:
        part_list: [(char_idx, part_local_idx, text), ...]
        char_start: {char_idx: global_part_start_idx}
    """
    part_list: List[Tuple[int, int, str]] = []
    char_start: Dict[int, int] = {}
    gi = 0
    for ci, ch in enumerate(chars):
        if ch.ruby:
            char_start[ci] = gi
            for pi, part in enumerate(ch.ruby.parts):
                part_list.append((ci, pi, part.text))
                gi += 1
    return part_list, char_start


def _char_particle_indices(
    char_idx: int,
    parts: List[RubyPart],
    char_start: Dict[int, int],
    particle_indices: set,
) -> List[int]:
    """将全局 particle_indices 映射回指定字符的 local part 索引。"""
    start = char_start.get(char_idx)
    if start is None:
        return []
    result: List[int] = []
    for pi in range(len(parts)):
        if (start + pi) in particle_indices:
            result.append(pi)
    return result


# ── 时间标签 + 文本拼接 ──


def _build_tagged_parts(
    parts: List[str],
    ts_list: List[int],
    *,
    keep_empty_ts: bool = False,
) -> str:
    """构造带时间标签的文本串。

    格式: [ts0]text0[ts1]text1[ts2]text2...

    所有 part 文本先剥离停顿符占位（导出契约：占位不泄漏给消费方）。
    剥离后为空的拍：

    - ``keep_empty_ts=True``（假名注音层——占位拍的时间轴必须保留）：
      输出裸时间标签 ``[ts]``，导入侧把空段还原为占位符，roundtrip 不丢拍。
    - ``keep_empty_ts=False``（默认；罗马音层等派生层）：整个拍跳过。
      罗马音层的时间轴以假名层为准，且存在天然的空 part
      （拗音/促音被前导 part 吸收），保持既有跳过行为。
    """
    result: List[str] = []
    for i, text in enumerate(parts):
        text = strip_ruby_pause_chars(text)
        if not text:
            if keep_empty_ts and i < len(ts_list):
                result.append(f"[{format_timestamp(ts_list[i])}]")
            continue
        if i < len(ts_list):
            result.append(f"[{format_timestamp(ts_list[i])}]")
        result.append(text)
    return "".join(result)


# ── 单注音格式 ──


def sentence_to_kasugamuki(sentence: Sentence) -> str:
    """一行 → 春日向单注音格式。"""
    segments: List[str] = []
    chars = sentence.characters
    i = 0
    while i < len(chars):
        char = chars[i]
        if char.linked_to_next and i + 1 < len(chars):
            group, i = _collect_linked(chars, i)
            segments.append(_linked_group_kana(group))
        elif char.ruby:
            segments.append(_char_ruby_kana(char))
            i += 1
        else:
            segments.append(_char_plain(char))
            i += 1
    return "".join(segments)


def sentences_to_kasugamuki(sentences: List[Sentence]) -> str:
    return "\n".join(sentence_to_kasugamuki(s) for s in sentences)


def _char_plain(char: Character) -> str:
    ts_list = _get_ts_list(char)
    base = ""
    if ts_list:
        base = f"[{format_timestamp(ts_list[0])}]{char.char}"
    else:
        base = char.char
    return base + _format_sentence_end(char)


def _char_ruby_kana(char: Character) -> str:
    assert char.ruby is not None
    kana_texts = [p.text for p in char.ruby.parts]
    ts_list = _get_ts_list(char)
    tagged = _build_tagged_parts(kana_texts, ts_list, keep_empty_ts=True)
    if not tagged:
        # 注音全是停顿占位且未打轴 → 退化为普通字符，不输出空注音块
        return _char_plain(char)
    return f"{{{char.char}|{tagged}}}{_format_sentence_end(char)}"


def _linked_group_kana(group: List[Character]) -> str:
    base = "".join(ch.char for ch in group)
    tagged: List[str] = []
    for ch in group:
        if ch.ruby:
            tagged.append(
                _build_tagged_parts(
                    [part.text for part in ch.ruby.parts],
                    _get_ts_list(ch),
                    keep_empty_ts=True,
                )
            )
    annotation = "".join(tagged)
    if not annotation:
        return "".join(_char_plain(ch) for ch in group)
    return f"{{{base}|{annotation}}}{_format_sentence_end(group[-1])}"


# ── 双注音格式（带罗马音） ──


def sentence_to_kasugamuki_romaji(sentence: Sentence) -> str:
    """一行 → 春日向双注音格式（假名 + 罗马音）。"""
    chars = sentence.characters
    # Use the exact same sentence-level path as 注音管理 -> 转罗马音.  Work on
    # a copy because exporting must not modify the open project.  促音独立
    # 成拍（っ|>t、だ|>da），促音与后字各自持有罗马音与时间戳——导出
    # 不合并分块、不改变任何原有连词状态。
    romanized_sentence = deepcopy(sentence)
    romanize_sentence_to_self_ruby(romanized_sentence)
    romaji_by_char = {
        i: [part.text for part in ch.ruby.parts]
        for i, ch in enumerate(romanized_sentence.characters)
        if ch.ruby
    }

    segments: List[str] = []
    i = 0
    while i < len(chars):
        char = chars[i]
        if char.linked_to_next and i + 1 < len(chars):
            group_start = i
            group, i = _collect_linked(chars, i)
            segments.append(
                _linked_group_romaji(group, group_start, romaji_by_char)
            )
        elif char.ruby:
            segments.append(
                _char_ruby_romaji(char, romaji_by_char.get(i, []))
            )
            i += 1
        elif _is_kana_text(char.char):
            if not any(romaji_by_char.get(i, [])):
                # 无独立罗马音的假名（拗音小假名被前导拍吸收等）→ 原样输出，
                # 不与相邻字符合并成块，保持原有连词状态。
                segments.append(_char_plain(char))
                i += 1
                continue
            mora_end = i + 1
            # The shared converter stores a cross-character digraph on its
            # leading part and leaves the consumed small kana empty.  Sokuon
            # is excluded too: it always keeps its own romaji beat and must
            # never be absorbed into the preceding block.
            while (
                mora_end < len(chars)
                and not chars[mora_end].ruby
                and _is_kana_text(chars[mora_end].char)
                and chars[mora_end].char not in ("っ", "ッ")
                and not any(romaji_by_char.get(mora_end, []))
            ):
                mora_end += 1
            if mora_end > i + 1:
                segments.append(
                    _self_kana_group_romaji(
                        chars[i:mora_end], romaji_by_char.get(i, [])
                    )
                )
                i = mora_end
            else:
                segments.append(
                    _char_self_ruby_romaji(char, romaji_by_char.get(i, []))
                )
                i += 1
        else:
            # 非假名无注音 → 回退到单注音格式
            segments.append(_char_plain(char))
            i += 1
    return "".join(segments)


def sentences_to_kasugamuki_romaji(sentences: List[Sentence]) -> str:
    return "\n".join(sentence_to_kasugamuki_romaji(s) for s in sentences)


def _char_ruby_romaji(
    char: Character,
    romaji_list: List[str],
) -> str:
    assert char.ruby is not None
    ts_list = _get_ts_list(char)
    parts = char.ruby.parts
    kana_texts = [p.text for p in parts]
    kana_tagged = _build_tagged_parts(kana_texts, ts_list, keep_empty_ts=True)

    romaji_tagged = _build_tagged_parts(romaji_list, ts_list)

    if not kana_tagged and not romaji_tagged:
        # 两层全是停顿占位且未打轴 → 退化为普通字符，不输出空注音块
        return _char_plain(char)
    return f"{{{char.char}|{kana_tagged}>{romaji_tagged}}}{_format_sentence_end(char)}"


def _char_self_ruby_romaji(char: Character, romaji_list: List[str]) -> str:
    ts_list = _get_ts_list(char)
    romaji_tagged = _build_tagged_parts(romaji_list, ts_list)
    if not romaji_tagged:
        # 罗马音层全是停顿占位且未打轴 → 退化为普通字符，不输出空注音块
        return _char_plain(char)
    return f"{{{char.char}|>{romaji_tagged}}}{_format_sentence_end(char)}"


def _self_kana_group_romaji(group: List[Character], romaji_list: List[str]) -> str:
    base = "".join(ch.char for ch in group)
    romaji_tagged = _build_tagged_parts(romaji_list, _get_ts_list(group[0]))
    if not romaji_tagged:
        # 罗马音层全是停顿占位且未打轴 → 退化为普通字符组
        return "".join(_char_plain(ch) for ch in group)
    return f"{{{base}|>{romaji_tagged}}}{_format_sentence_end(group[-1])}"


def _linked_group_romaji(
    group: List[Character],
    group_start: int,
    romaji_by_char: Dict[int, List[str]],
) -> str:
    base = "".join(ch.char for ch in group)
    kana_tagged: List[str] = []
    romaji_tagged: List[str] = []
    for local_idx, ch in enumerate(group):
        ts_list = _get_ts_list(ch)
        if ch.ruby:
            kana_tagged.append(
                _build_tagged_parts(
                    [part.text for part in ch.ruby.parts],
                    ts_list,
                    keep_empty_ts=True,
                )
            )
        romaji = romaji_by_char.get(group_start + local_idx, [])
        if romaji:
            romaji_tagged.append(_build_tagged_parts(romaji, ts_list))
    kana_annotation = "".join(kana_tagged)
    romaji_annotation = "".join(romaji_tagged)
    if not kana_annotation and not romaji_annotation:
        return "".join(_char_plain(ch) for ch in group)
    return (
        f"{{{base}|{kana_annotation}>{romaji_annotation}}}"
        f"{_format_sentence_end(group[-1])}"
    )


def _linked_group_common(
    group: List[Character],
    *,
    with_romaji: bool,
    char_start: Dict[int, int],
    particle_indices: set,
    group_start: int,
    romaji_by_char: Optional[Dict[int, List[str]]] = None,
) -> str:
    """处理连词组，合并连续假名字符的罗马音。"""
    # 先把 group 分成若干批次：连续的无 ruby 假名字符合并，其他各成一个批次
    batches: List[Tuple[int, List[Character]]] = []  # (start_in_group, chars)
    i = 0
    while i < len(group):
        ch = group[i]
        if not ch.ruby and _is_kana_text(ch.char):
            batch_start = i
            batch_chars = []
            while i < len(group) and not group[i].ruby and _is_kana_text(group[i].char):
                batch_chars.append(group[i])
                i += 1
            batches.append((batch_start, batch_chars))
        else:
            batches.append((i, [ch]))
            i += 1

    result_parts: List[Optional[str]] = [None] * len(group)
    for batch_start, batch_chars in batches:
        ch = batch_chars[0]
        if len(batch_chars) == 1 or ch.ruby or not _is_kana_text(ch.char):
            # 单人批次，走原逻辑
            char_idx = group_start + batch_start
            if ch.ruby:
                if with_romaji:
                    result_parts[batch_start] = _char_ruby_romaji(
                        ch, (romaji_by_char or {}).get(char_idx, []),
                    )
                else:
                    result_parts[batch_start] = _char_ruby_kana(ch)
            elif with_romaji and _is_kana_text(ch.char):
                result_parts[batch_start] = _char_self_ruby_romaji(
                    ch, (romaji_by_char or {}).get(char_idx, []),
                )
            else:
                result_parts[batch_start] = _char_plain(ch)
        else:
            # 连续假名批次：合并全部文本后统一 mora 拆分和罗马化
            _process_kana_batch(
                batch_chars, batch_start, result_parts,
                with_romaji=with_romaji,
                group_start=group_start,
                romaji_by_char=romaji_by_char,
            )

    return "".join(p for p in result_parts if p is not None)


def _process_kana_batch(
    batch: List[Character],
    batch_start: int,
    result_parts: List[Optional[str]],
    *,
    with_romaji: bool,
    group_start: int,
    romaji_by_char: Optional[Dict[int, List[str]]],
) -> None:
    """连续假名字符：按字符粒度传入 romanize_ruby_parts，让其自行处理跨字符拗音拆分。
    然后每个字符各自拿到自己那部分 romaji。"""
    # 收集每个字符的假名文本（按字符粒度，不做 mora 合并）
    char_kana_list = [ch.char for ch in batch]

    if with_romaji:
        char_romaji_list = [
            "".join((romaji_by_char or {}).get(group_start + batch_start + i, []))
            for i in range(len(batch))
        ]
    else:
        char_romaji_list = char_kana_list

    for bi, ch in enumerate(batch):
        ts_list = _get_ts_list(ch)
        rm = char_romaji_list[bi] if bi < len(char_romaji_list) else ""
        rm = strip_ruby_pause_chars(rm)
        km = char_kana_list[bi]

        if not rm:
            result_parts[batch_start + bi] = ""
            continue

        if with_romaji:
            romaji_tagged = _build_tagged_parts([rm], ts_list)
            result_parts[batch_start + bi] = (
                f"{{{ch.char}|>{romaji_tagged}}}{_format_sentence_end(ch)}"
            )
        else:
            kana_tagged = _build_tagged_parts([km], ts_list)
            result_parts[batch_start + bi] = (
                f"{{{ch.char}|{kana_tagged}}}{_format_sentence_end(ch)}"
            )


def _collect_linked(
    chars: List[Character], start: int
) -> Tuple[List[Character], int]:
    group = [chars[start]]
    i = start + 1
    while i < len(chars) and chars[i - 1].linked_to_next:
        group.append(chars[i])
        i += 1
    return group, i
