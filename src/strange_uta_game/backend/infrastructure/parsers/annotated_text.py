"""带注音标注的行级文本格式 解析/序列化。

用于全文本编辑（已废弃不建议使用）界面（``frontend/editor/fulltext_interface``）在 ``Sentence.characters``
与单行 / 多行字符串之间互转。

格式约定
--------
``{大冒険||だ|い,ぼ|う,け|ん}``

- ``{ ... }``：一个注音块，内部由 ``||`` 把原文与读音分开。
- ``||``：分隔 "原文字符串" 与 "读音区"。
- ``,``：分隔多个字符，各自对应一组读音。
- ``|``：分隔同一字符内多个 ``RubyPart``（mora）。

兼容格式
--------
- ``{漢|か|ん|じ}``：单字多段 mora（``||`` 缺省）。
- ``{赤|あか}``：单字单段 reading。
- ``{text}``：无读音，等价于纯文本（右括号闭合）。

不支持的旧格式（已在 0.x 弃用）
- ``漢字{かんじ}``：后置格式。

Public API
----------
- :func:`parse_annotated_line` — 单行文本 → (raw_text, raw_chars, ruby_map)。
- :func:`sentence_to_annotated_line` — ``Sequence[Character]`` → 单行带注音字符串，
  连词组（``linked_to_next`` 链）合并为一个 ``{...||...}`` 块。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from strange_uta_game.backend.domain.graphemes import (
    next_grapheme_end,
    split_graphemes,
)

if TYPE_CHECKING:
    # 仅用于类型注解，避免运行时 import 产生循环。
    from strange_uta_game.backend.domain.models import Character


def parse_annotated_line(
    line_text: str,
) -> Tuple[str, List[str], Dict[int, List[str]]]:
    """解析带注音标注的文本行为 (原文, 字符列表, ruby_map)。

    Args:
        line_text: 单行文本（不含换行符）。

    Returns:
        ``(raw_text, raw_chars, ruby_map)``：

        - ``raw_text`` — 剥离标注后的纯原文；
        - ``raw_chars`` — 与 ``raw_text`` 字符一一对应的列表；
        - ``ruby_map`` — ``char_idx → [RubyPart.text, ...]``，无读音的字符不出现在键中。

    Note:
        未配对的 ``{`` 按普通字符处理，保持与 0.2 行为一致。
    """
    raw_chars: List[str] = []
    ruby_map: Dict[int, List[str]] = {}
    i = 0
    n = len(line_text)

    while i < n:
        if line_text[i] == "{":
            close = line_text.find("}", i)
            if close == -1:
                # 无配对右括号，当普通字符处理
                raw_chars.append(line_text[i])
                i += 1
                continue

            content = line_text[i + 1 : close]

            if "||" in content:
                # 主格式：text||mora|mora,mora|mora
                text_part, readings_part = content.split("||", 1)
                per_char_readings = readings_part.split(",")
                start_idx = len(raw_chars)
                for ch in split_graphemes(text_part):
                    raw_chars.append(ch)

                for j, reading_group in enumerate(per_char_readings):
                    # reading_group 内部用 "|" 分 mora；空串代表无 ruby
                    parts = [p for p in reading_group.split("|") if p != ""]
                    if parts and (start_idx + j) < len(raw_chars):
                        ruby_map[start_idx + j] = parts
            elif "|" in content:
                # 兼容简短：{text|mora|mora|mora}（单字多段）或 {text|reading}（单字单段）
                text_part, _, readings_part = content.partition("|")
                parts = [p for p in readings_part.split("|") if p != ""]
                clusters = split_graphemes(text_part)

                start_idx = len(raw_chars)
                raw_chars.extend(clusters)

                if len(clusters) == 1 and parts:
                    ruby_map[start_idx] = parts
                elif len(clusters) > 1 and parts:
                    # 歧义：多字只给一个 reading，兜底当作首字全吃
                    ruby_map[start_idx] = parts
            else:
                # {text} 无 ruby → 纯文本
                for ch in split_graphemes(content):
                    raw_chars.append(ch)

            i = close + 1
        else:
            raw_chars.append(line_text[i])
            i += 1

    raw_text = "".join(raw_chars)
    return raw_text, raw_chars, ruby_map


def sentence_to_annotated_line(characters: "Sequence[Character]") -> str:
    """把一个 Sentence 的 ``characters`` 序列化为单行带注音文本。

    序列化规则：

    - 连续的 ``linked_to_next`` 链合并为一个 ``{原文||mora|...,mora|...}`` 块，
      同一字的多 ``RubyPart`` 用 ``|`` 相连，不同字之间用 ``,``。
    - 非连词且带 ``ruby`` 的字符输出为 ``{字||mora|mora|...}``（单字块）。
    - 无 ``ruby`` 的字符按 ``character.char`` 原样输出。

    Args:
        characters: ``Sentence.characters`` 序列。

    Returns:
        单行字符串（不含换行符）；空输入返回空串。
    """
    buf: List[str] = []
    i = 0
    n = len(characters)
    while i < n:
        ch = characters[i]
        if ch.ruby:
            # 收集连词组（linked_to_next 链）
            group_start = i
            while i < n - 1 and characters[i].linked_to_next:
                i += 1
            i += 1  # 包含链中最后一个字符
            group = characters[group_start:i]
            text_part = "".join(c.char for c in group)
            readings = ",".join(
                "|".join(p.text for p in c.ruby.parts) if c.ruby else ""
                for c in group
            )
            buf.append(f"{{{text_part}||{readings}}}")
        else:
            buf.append(ch.char)
            i += 1
    return "".join(buf)


# ══════════════════════════════════════════════════════════════════════
# 带内联时间戳的行级格式（全文本编辑器专用，无损往返）
# ----------------------------------------------------------------------
# 在 {原文||读音} 基础上，把每个 checkpoint（= rubypart / 纯假名的单点）
# 的起始时间戳内联进去，并把停顿点(释放)时间戳贴在字符后方，使整行文本
# 自带完整时间轴 —— 编辑器逐行独立解码，行的增删/重排/文本撞车都不会
# 丢失或错配时间戳。
#
# Token：
#   [mm:ss.xx]       某 checkpoint 的起始时间戳（2 位厘秒，轴主流消费精度）
#   [T]              该 checkpoint 应有但尚无时间戳（占位，T = todo）
#   [>mm:ss.xx]      该字符的停顿点(释放)时间戳，贴字符后方
#   [>T]             该字符是停顿点但时间戳尚无
#   【演唱者名】       演唱者切换标签（与 Nicokara 一致，出现在切换处）
#   【>Guide】          导唱待办标记（字符 needs_guide=True）
#   【>GuideBlock】     紧随其后的字符/连词块为实际导唱字符（is_guide=True）
#
# 容错：任何 [...] / [>...] 若内部不是合法 mm:ss.xx，则一律按占位 [T] /
# [>T] 处理（用户手输的非法时间戳不会丢字符，只是该位轴清空待补）。
#
# 编码约定：
#   - 有 ruby 的字（在 {…||…} 块内）：每个 mora 前缀其 [起始ts]，
#     mora 间用 |，字间用 ,；停顿点 token 贴在该字读音段末尾。
#   - 无 ruby 的字：按 check_count 个 [起始ts] 前缀 + 字符；停顿点 token 贴后。
#   - check_count==0 的字：裸字符，无任何 token。
#   - 连词(linked_to_next) 由 {…} 块归组表达（块内相邻字 linked，块尾不 linked）。
# ══════════════════════════════════════════════════════════════════════

# 起始 token：``[...]`` 且不以 > 开头（容错：任意内容都当 token，非法→占位）
_START_TOKEN_RE = re.compile(r"\[(?!>)[^\]]*\]")
# 行内末尾的停顿点 token：``[>...]`` 贴在读音段最后
_END_TOKEN_AT_END_RE = re.compile(r"\[>[^\]]*\]$")
_TODO = "T"
# 默认演唱者（或无具名 singer）切换回来时使用的统一标签，
# 避免写出冗长/未知的默认演唱者名；解码时映射回 default_singer_id。
DEFAULT_SINGER_LABEL = "默认演唱者"


def _format_ms(ms: int) -> str:
    """毫秒 → ``mm:ss.xx``（2 位厘秒；分钟补零可超 99）。"""
    if ms < 0:
        ms = 0
    m = ms // 60000
    s = (ms // 1000) % 60
    cc = (ms % 1000) // 10  # 厘秒
    return f"{m:02d}:{s:02d}.{cc:02d}"


def _parse_ts_value(value: str) -> Optional[int]:
    """token 内部文本（已去 ``[]`` 和可选 ``>``） → 毫秒。

    合法 ``mm:ss.xx`` 返回毫秒；``todo`` 或任何非法内容返回 None（占位）。
    """
    m = re.fullmatch(r"(\d+):(\d{2})\.(\d{2})", value)
    if not m:
        return None
    return int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + int(m.group(3)) * 10


def _start_token(ms: Optional[int]) -> str:
    return f"[{_format_ms(ms)}]" if ms is not None else f"[{_TODO}]"


def _end_token(ms: Optional[int]) -> str:
    return f"[>{_format_ms(ms)}]" if ms is not None else f"[>{_TODO}]"


def _add_off(ms: Optional[int], offset_ms: int) -> Optional[int]:
    """编码时把内部原始时间戳加上全局偏移，与打轴界面显示一致。"""
    return None if ms is None else ms + offset_ms


def _sub_off(ms: Optional[int], offset_ms: int) -> Optional[int]:
    """解码时去掉全局偏移补偿，还原内部原始时间戳（不小于 0）。"""
    return None if ms is None else max(0, ms - offset_ms)


def _char_start_ts(ch: "Character", idx: int) -> Optional[int]:
    """返回字符第 idx 个 checkpoint 的起始时间戳；缺失返回 None。"""
    return ch.timestamps[idx] if idx < len(ch.timestamps) else None


def _effective_singer(ch: "Character", line_singer: str, default_singer: str) -> str:
    return ch.singer_id or line_singer or default_singer


def _is_singer_block_whitespace(
    characters: "Sequence[Character]",
    start_idx: int,
    singer_id: str,
    line_singer_id: str,
    default_singer_id: str,
) -> bool:
    """检查从 start_idx 开始的同一演唱者连续片段是否全是空白字符。"""
    n = len(characters)
    for j in range(start_idx, n):
        ch = characters[j]
        eff = _effective_singer(ch, line_singer_id, default_singer_id)
        if eff != singer_id:
            break
        if ch.char.strip():
            return False
    return True


def sentence_to_timed_line(
    characters: "Sequence[Character]",
    *,
    singer_id_to_name: Optional[Dict[str, str]] = None,
    line_singer_id: str = "",
    default_singer_id: str = "",
    inherited_singer_id: str = "",
    offset_ms: int = 0,
) -> Tuple[str, str]:
    """把 Sentence.characters 序列化为带内联时间戳的单行文本。

    ``offset_ms``：编码出的时间戳 = 内部原始时间戳 + 该全局偏移，使其与打轴
    界面显示一致；:func:`parse_timed_line` 用同值反向补偿还原。

    Args:
        characters: 一条 Sentence 的字符序列。
        singer_id_to_name: singer_id → 显示名；用于插入 ``【名】`` 切换标签。
            为 None 或空时不输出演唱者标签。
        line_singer_id: 该行的行级 singer（用于 ``ch.singer_id`` 为空时回退）。
        default_singer_id: 项目默认 singer（最终回退）。
        inherited_singer_id: 上一非空行末尾延续下来的 singer；决定行首是否
            需要补 ``【名】`` 标签（与 Nicokara 导出 prev_singer 行为一致）。

    Returns:
        ``(line_text, last_singer_id)``：行文本，以及本行末尾的有效 singer
        （供下一行计算 inherited）。空字符序列返回 ``("", inherited_singer_id)``。
    """
    use_singer = bool(singer_id_to_name)
    current = inherited_singer_id or default_singer_id
    buf: List[str] = []
    i = 0
    n = len(characters)

    def _emit_singer(sid: str, force: bool = False) -> None:
        nonlocal current
        if not use_singer:
            return
        should_emit = force or (sid != current)
        if not should_emit:
            return
        if not force and _is_singer_block_whitespace(
            characters, i, sid, line_singer_id, default_singer_id
        ):
            current = sid
            return
        if not sid or sid == default_singer_id:
            name = DEFAULT_SINGER_LABEL
        else:
            name = singer_id_to_name.get(sid, "")
        if name:
            buf.append(f"【{name}】")
        current = sid

    while i < n:
        ch = characters[i]
        eff = _effective_singer(ch, line_singer_id, default_singer_id)
        _emit_singer(eff, force=ch.force_singer_tag)

        if ch.is_guide:
            buf.append("【>GuideBlock】")

        if ch.needs_guide:
            buf.append("【>Guide】")

        if ch.ruby or ch.linked_to_next:
            # 连词组（linked_to_next 链）合并为一个块
            group_start = i
            while i < n - 1 and characters[i].linked_to_next:
                i += 1
            i += 1
            group = characters[group_start:i]
            text_part = "".join(c.char for c in group)
            segs: List[str] = []
            for c in group:
                if c.ruby:
                    pieces: List[str] = []
                    for k, part in enumerate(c.ruby.parts):
                        ts = _add_off(_char_start_ts(c, k), offset_ms)
                        if ts is not None:
                            pieces.append(f"[{_format_ms(ts)}]" + part.text)
                        elif k < c.check_count:
                            pieces.append(f"[{_TODO}]" + part.text)
                        else:
                            pieces.append(part.text)
                    seg = "|".join(pieces)
                elif c.check_count > 0:
                    pieces = [
                        _start_token(_add_off(_char_start_ts(c, k), offset_ms))
                        for k in range(c.check_count)
                    ]
                    seg = "|".join(pieces)
                else:
                    seg = ""
                if c.is_sentence_end:
                    seg += _end_token(_add_off(c.sentence_end_ts, offset_ms))
                segs.append(seg)
            buf.append("{" + text_part + "||" + ",".join(segs) + "}")
        else:
            if ch.check_count > 1:
                pieces = [
                    _start_token(_add_off(_char_start_ts(ch, k), offset_ms))
                    for k in range(ch.check_count)
                ]
                seg = "|".join(pieces)
                if ch.is_sentence_end:
                    seg += _end_token(_add_off(ch.sentence_end_ts, offset_ms))
                buf.append("{" + ch.char + "||" + seg + "}")
            elif ch.check_count == 1:
                prefix = _start_token(_add_off(_char_start_ts(ch, 0), offset_ms))
                piece = prefix + ch.char
                if ch.is_sentence_end:
                    piece += _end_token(_add_off(ch.sentence_end_ts, offset_ms))
                buf.append(piece)
            else:
                piece = ch.char
                if ch.is_sentence_end:
                    piece += _end_token(_add_off(ch.sentence_end_ts, offset_ms))
                buf.append(piece)
            i += 1

    return "".join(buf), current


def timed_line_columns(
    characters: "Sequence[Character]",
    *,
    singer_id_to_name: Optional[Dict[str, str]] = None,
    line_singer_id: str = "",
    default_singer_id: str = "",
    inherited_singer_id: str = "",
    offset_ms: int = 0,
) -> List[int]:
    """复算 :func:`sentence_to_timed_line` 的输出，返回每个字符的"字形列号"。

    列号 = 该字符的可见字（块内为原文汉字、块外为字符本身）在渲染行中的
    列位置（0 基）。供编辑器把光标定位到某字符使用，与编码逻辑严格对齐。
    """
    use_singer = bool(singer_id_to_name)
    current = inherited_singer_id or default_singer_id
    columns: List[int] = [0] * len(characters)
    col = 0
    i = 0
    n = len(characters)

    def _singer_len(sid: str) -> int:
        nonlocal current
        if not use_singer or sid == current:
            return 0
        if not sid or sid == default_singer_id:
            name = DEFAULT_SINGER_LABEL
        else:
            name = singer_id_to_name.get(sid, "")
        current = sid
        return len(f"【{name}】") if name else 0

    while i < n:
        ch = characters[i]
        col += _singer_len(_effective_singer(ch, line_singer_id, default_singer_id))

        if ch.ruby or ch.linked_to_next:
            group_start = i
            while i < n - 1 and characters[i].linked_to_next:
                i += 1
            i += 1
            group = characters[group_start:i]
            # 块字形列：'{' 之后是原文部分；多码点簇成员按其码点宽度占列
            _gcol = col + 1
            for g, gc in enumerate(group):
                columns[group_start + g] = _gcol
                _gcol += len(gc.char)
            # 推进 col 到块串末尾
            segs: List[str] = []
            for c in group:
                if c.ruby:
                    pieces: List[str] = []
                    for k, part in enumerate(c.ruby.parts):
                        ts = _add_off(_char_start_ts(c, k), offset_ms)
                        if ts is not None:
                            pieces.append(f"[{_format_ms(ts)}]" + part.text)
                        elif k < c.check_count:
                            pieces.append(f"[{_TODO}]" + part.text)
                        else:
                            pieces.append(part.text)
                    seg = "|".join(pieces)
                elif c.check_count > 0:
                    seg = "|".join(
                        _start_token(_add_off(_char_start_ts(c, k), offset_ms))
                        for k in range(c.check_count)
                    )
                else:
                    seg = ""
                if c.is_sentence_end:
                    seg += _end_token(_add_off(c.sentence_end_ts, offset_ms))
                segs.append(seg)
            text_part = "".join(c.char for c in group)
            col += len("{" + text_part + "||" + ",".join(segs) + "}")
        else:
            if ch.check_count > 1:
                pieces = [
                    _start_token(_add_off(_char_start_ts(ch, k), offset_ms))
                    for k in range(ch.check_count)
                ]
                seg = "|".join(pieces)
                if ch.is_sentence_end:
                    seg += _end_token(_add_off(ch.sentence_end_ts, offset_ms))
                block = "{" + ch.char + "||" + seg + "}"
                columns[i] = col + 1
                col += len(block)
            elif ch.check_count == 1:
                prefix = _start_token(_add_off(_char_start_ts(ch, 0), offset_ms))
                columns[i] = col + len(prefix)
                piece = prefix + ch.char
                if ch.is_sentence_end:
                    piece += _end_token(_add_off(ch.sentence_end_ts, offset_ms))
                col += len(piece)
            else:
                columns[i] = col
                extra = len(ch.char)
                if ch.is_sentence_end:
                    extra += len(_end_token(_add_off(ch.sentence_end_ts, offset_ms)))
                col += extra
            i += 1

    return columns


def _consume_leading_start(slot: str) -> Tuple[Optional[int], bool, str]:
    """从 slot 头部消费一个起始 token。

    Returns: ``(ms, had_token, rest)``——ms 为时间戳（占位/无 token 时 None），
    had_token 表示是否确实有 ``[...]`` 前缀，rest 为去掉 token 后的文本。
    """
    m = _START_TOKEN_RE.match(slot)
    if not m:
        return None, False, slot
    ms = _parse_ts_value(slot[1 : m.end() - 1])
    return ms, True, slot[m.end():]


def _build_timestamps(slot_ms: List[Optional[int]]) -> List[int]:
    """把每 checkpoint 的 ms（可能含 None 占位）压成 timestamps 列表。

    timestamps 在 domain 中按 index 紧凑存储（不支持中间空洞），故取最长的
    非占位前缀；遇到第一个 None 即停止（其后视为未打轴的尾部 checkpoint）。
    """
    out: List[int] = []
    for ms in slot_ms:
        if ms is None:
            break
        out.append(ms)
    return out


def is_valid_block_content(content: str) -> bool:
    """``{...}`` 块内容是否合规（结构化字符有效）。

    规则：必须含分隔符 ``||`` 且原文部分（``||`` 之前）非空。不合规的块
    （如缺 ``||``、原文为空）应按普通字符解析/着色，而非结构化解释。
    """
    if "||" not in content:
        return False
    return content.split("||", 1)[0] != ""


def _parse_block(content: str, singer_id: str, offset_ms: int = 0):
    """解析 ``{原文||读音段...}`` 块内容为 Character 列表（块内相邻字 linked）。"""
    from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart

    if "||" in content:
        text_part, readings = content.split("||", 1)
    else:
        text_part, readings = content, ""
    # 原文按字素簇拆分（ɛ̃/t͡ʃ 是一个字符）；旧版按码点导出的行
    # 段数对不上簇数时退回码点拆分，保证旧数据往返。
    text_chars = split_graphemes(text_part)
    if (
        readings != ""
        and len(text_chars) != readings.count(",") + 1
        and len(text_part) == readings.count(",") + 1
    ):
        text_chars = list(text_part)
    segs = readings.split(",") if readings != "" else []

    chars: List[Character] = []
    for idx, chc in enumerate(text_chars):
        seg = segs[idx] if idx < len(segs) else ""

        # 行尾停顿点 token（贴在读音段末尾）
        is_end = False
        end_ms: Optional[int] = None
        m_end = _END_TOKEN_AT_END_RE.search(seg)
        if m_end:
            is_end = True
            end_ms = _sub_off(
                _parse_ts_value(seg[m_end.start() + 2 : m_end.end() - 1]), offset_ms
            )
            seg = seg[: m_end.start()]

        mora_slots = seg.split("|") if seg != "" else []
        parts: List[RubyPart] = []
        slot_ms: List[Optional[int]] = []
        any_token = False
        for slot in mora_slots:
            ms, had, text = _consume_leading_start(slot)
            if had:
                any_token = True
            parts.append(RubyPart(text=text))
            slot_ms.append(_sub_off(ms, offset_ms))

        ch = Character(
            char=chc,
            check_count=len(parts) if any_token else 0,
            timestamps=_build_timestamps(slot_ms),
            singer_id=singer_id or "",
            is_sentence_end=is_end,
            sentence_end_ts=end_ms,
        )
        if parts and any(p.text for p in parts):
            ch.set_ruby(Ruby(parts=parts))
        ch.push_to_ruby()
        chars.append(ch)

    # 块内相邻字连词，块尾不连词
    for k in range(len(chars) - 1):
        chars[k].linked_to_next = True
    return chars


def parse_timed_line(
    line_text: str,
    *,
    name_to_singer_id: Optional[Dict[str, str]] = None,
    default_singer_id: str = "",
    inherited_singer_id: str = "",
    offset_ms: int = 0,
):
    """把一行带内联时间戳的文本解码为 ``(characters, last_singer_id)``。

    与 :func:`sentence_to_timed_line` 互逆。逐行独立解码，不依赖其他行。

    Args:
        line_text: 单行文本（不含换行符）。
        name_to_singer_id: 演唱者名 → singer_id；解析 ``【名】`` 标签用。
        default_singer_id: 行首默认 singer。
        inherited_singer_id: 上一行延续下来的 singer（行首未显式标签时使用）。
        offset_ms: 文本里的时间戳含此全局偏移，解析时统一减去还原内部原始值。

    Returns:
        ``(characters, last_singer_id)``。空行返回 ``([], inherited_singer_id or default)``。
    """
    from strange_uta_game.backend.domain.models import Character

    name_map = name_to_singer_id or {}
    current = inherited_singer_id or default_singer_id
    chars: List[Character] = []
    pending_starts: List[Optional[int]] = []
    pending_guide = False
    pending_guide_block = False
    pending_force_singer = False
    i = 0
    n = len(line_text)

    while i < n:
        c = line_text[i]

        if c == "【":
            close = line_text.find("】", i)
            # inner 含另一个 '【' 时，当前 '【' 是多余的字面字符，真正的标签从
            # 内层 '【' 开始。放行当前 '【' 为普通字符，循环在下一个 '【' 重试
            # （如 "【【名】" → '【' + 【名】 标签）。
            if close != -1 and "【" not in line_text[i + 1 : close]:
                name = line_text[i + 1 : close]
                if name == ">Guide":
                    pending_guide = True
                    i = close + 1
                    continue
                if name == ">GuideBlock":
                    pending_guide_block = True
                    i = close + 1
                    continue
                if name == DEFAULT_SINGER_LABEL:
                    current = default_singer_id
                    i = close + 1
                    continue
                if name in name_map:
                    if name_map[name] == current:
                        pending_force_singer = True
                    else:
                        current = name_map[name]
                    i = close + 1
                    continue
                # 未知演唱者名（不在项目设置中）→ 整个 【名】 当普通字符，
                # 不静默丢弃（与不合规 [xxx] 的容错处理一致）。
                for literal_ch in split_graphemes(line_text[i : close + 1]):
                    ch = Character(char=literal_ch, singer_id=current or "")
                    if pending_starts:
                        ch.check_count = len(pending_starts)
                        ch.timestamps = _build_timestamps(pending_starts)
                        pending_starts = []
                    else:
                        ch.check_count = 0
                    if pending_guide:
                        ch.needs_guide = True
                        pending_guide = False
                    if pending_guide_block:
                        ch.is_guide = True
                        pending_guide_block = False
                    if pending_force_singer:
                        ch.force_singer_tag = True
                        pending_force_singer = False
                    chars.append(ch)
                i = close + 1
                continue

        if c == "[":
            close = line_text.find("]", i)
            # inner 含另一个 '[' 时，当前 '[' 不是合法 token 起点（token 内部
            # 绝不含 '['）——真正的 token 从内层 '[' 开始。此处放行当前 '[' 为
            # 普通字符，由循环在下一个 '[' 重新尝试解析（如 "[[T]" → '[' + [T]）。
            if close != -1 and "[" not in line_text[i + 1 : close]:
                inner = line_text[i + 1 : close]
                is_end_tok = inner.startswith(">")
                val_str = inner[1:] if is_end_tok else inner
                # 合法 token：占位符 [T] / [>T] 或标准时间戳 [mm:ss.xx] / [>mm:ss.xx]
                is_valid_tok = (val_str == _TODO) or (_parse_ts_value(val_str) is not None)
                if is_valid_tok:
                    if is_end_tok:
                        # 停顿点 token → 贴到上一个字符
                        ms = _sub_off(_parse_ts_value(val_str), offset_ms)
                        if chars:
                            chars[-1].is_sentence_end = True
                            chars[-1].sentence_end_ts = ms
                    else:
                        # 起始 token
                        pending_starts.append(_sub_off(_parse_ts_value(val_str), offset_ms))
                    i = close + 1
                    continue
                # 不合规 [xxx] → 逐字当普通字符（首字正常消耗 pending_starts）
                for literal_ch in split_graphemes(line_text[i : close + 1]):
                    ch = Character(char=literal_ch, singer_id=current or "")
                    if pending_starts:
                        ch.check_count = len(pending_starts)
                        ch.timestamps = _build_timestamps(pending_starts)
                        pending_starts = []
                    else:
                        ch.check_count = 0
                    if pending_guide:
                        ch.needs_guide = True
                        pending_guide = False
                    if pending_force_singer:
                        ch.force_singer_tag = True
                        pending_force_singer = False
                    chars.append(ch)
                i = close + 1
                continue
            # 未配对 [ → 当普通字符处理

        if c == "{":
            close = line_text.find("}", i)
            if close != -1:
                content = line_text[i + 1 : close]
                # content 含另一个 '{' 时，当前 '{' 是多余的字面字符，真正的块从
                # 内层 '{' 开始。放行当前 '{' 为普通字符，循环在下一个 '{' 重试
                # （如 "{{原文||…}" → '{' + {原文||…} 块）。
                if "{" not in content and is_valid_block_content(content):
                    before_len = len(chars)
                    chars.extend(_parse_block(content, current, offset_ms))
                    if pending_guide_block and len(chars) > before_len:
                        for parsed_ch in chars[before_len:]:
                            parsed_ch.is_guide = True
                        pending_guide_block = False
                    if pending_guide and len(chars) > before_len:
                        chars[before_len].needs_guide = True
                        pending_guide = False
                    if pending_force_singer and len(chars) > before_len:
                        chars[before_len].force_singer_tag = True
                        pending_force_singer = False
                    pending_starts = []
                    i = close + 1
                    continue
            # 未配对或块内容不合规 → 当普通字符

        # 普通字符（整簇消费：附加符不与基字拆开；语法字符 [ { 【 永不成簇尾，
        # 簇会在它们之前断开）
        cluster_end = next_grapheme_end(line_text, i)
        cluster = line_text[i:cluster_end]
        ch = Character(char=cluster, singer_id=current or "")
        if pending_starts:
            ch.check_count = len(pending_starts)
            ch.timestamps = _build_timestamps(pending_starts)
        else:
            # 裸字符：无 checkpoint
            ch.check_count = 0
        if pending_guide:
            ch.needs_guide = True
            pending_guide = False
        if pending_guide_block:
            ch.is_guide = True
            pending_guide_block = False
        if pending_force_singer:
            ch.force_singer_tag = True
            pending_force_singer = False
        pending_starts = []
        chars.append(ch)
        i = cluster_end

    if chars:
        chars[-1].is_line_end = True
    return chars, current
