"""RhythmicaLyrics 风格内联文本格式 序列化/反序列化。

格式规则:
  {漢字|[N|MM:SS:cc]ruby[MM:SS:cc]ruby}  — Ruby 注音组
  [N|MM:SS:cc]char                         — 普通字符 + checkpoint
  ＋                                       — 多汉字 Ruby 内分隔
  ▨                                        — 休止标记 (is_rest=True)
  [10|...]                                 — 行尾标记 (10 = 1cp + line_end)

N 编码: 数字末尾为 "0" 表示 is_line_end=True，前面的部分为 check_count。
例: "2" → check_count=2, line_end=False
    "10" → check_count=1, line_end=True

时间戳格式: MM:SS:cc (分:秒:厘秒)  例: 00:14:64 = 14640ms
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from strange_uta_game.backend.domain.models import (
    Character,
    Ruby,
    RubyPart,
    TimeTagType,
    get_ruby_pause_char,
)
from strange_uta_game.backend.domain.entities import Sentence
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import ParseError


def _export_timestamps(char: Character) -> List[int]:
    """返回导出用的时间戳列表（带全局偏移，若已计算）。

    若 Character 已通过 set_offset 计算过 global_timestamps（长度匹配 timestamps），
    则返回 global_timestamps；否则回退到原始 timestamps。
    """
    if char.global_timestamps and len(char.global_timestamps) == len(char.timestamps):
        return char.global_timestamps
    return char.timestamps


def _export_sentence_end_ts(char: Character) -> Optional[int]:
    """返回导出用的停顿点时间戳（带全局偏移，若已计算）。"""
    if char.sentence_end_ts is None:
        return None
    if char.global_sentence_end_ts is not None:
        return char.global_sentence_end_ts
    return char.sentence_end_ts



# ──────────────────────────────────────────────
# 时间戳
# ──────────────────────────────────────────────


def format_timestamp(ms: int) -> str:
    """毫秒 → MM:SS:cc"""
    total_cs = ms // 10
    minutes = total_cs // 6000
    seconds = (total_cs % 6000) // 100
    centis = total_cs % 100
    return f"{minutes:02d}:{seconds:02d}:{centis:02d}"


def parse_timestamp(s: str) -> int:
    """MM:SS:cc → 毫秒"""
    parts = s.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"时间戳格式无效: {s!r} (应为 MM:SS:cc)")
    minutes = int(parts[0])
    seconds = int(parts[1])
    centis = int(parts[2])
    return (minutes * 60 + seconds) * 1000 + centis * 10


# ──────────────────────────────────────────────
# N 编码 (checkpoint count + line_end flag)
# ──────────────────────────────────────────────


def encode_check_n(
    check_count: int, is_line_end: bool, is_sentence_end: bool = False
) -> str:
    """编码 check_count 和 is_line_end 到 N 字符串。

    规则: 末尾 "0" 表示 line_end。
    注: is_sentence_end 不编码到 N 中，停顿点时间戳单独处理。
    """
    suffix = ""
    if is_line_end:
        suffix += "0"
    return f"{check_count}{suffix}"


def decode_check_n(n_str: str) -> Tuple[int, bool, bool]:
    """解码 N 字符串到 (check_count, is_line_end, is_sentence_end)。

    健壮化（E4）：容忍空白与 `_TAG_RE` 历史上的 `e` 后缀变体（如 `[1e]`）；
    仍无法解析时抛 ParseError（而非裸 ValueError）。
    """
    is_sentence_end = False
    is_line_end = False
    n_str = n_str.strip().rstrip("e")
    if len(n_str) >= 2 and n_str.endswith("0"):
        is_line_end = True
        n_str = n_str[:-1]
    try:
        check_count = int(n_str)
    except ValueError as exc:
        raise ParseError(f"无效的 N 标记: {n_str!r}") from exc
    return check_count, is_line_end, is_sentence_end


# ──────────────────────────────────────────────
# Mora 分割 (用于 Ruby 文本拆分)
# ──────────────────────────────────────────────

_SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮー")


def split_into_moras(text: str) -> List[str]:
    """将注音文本按拍拆分（假名按モーラ，IPA 按音节）。

    - 小假名（ゃ ゅ ょ っ …）与长音符 ``ー`` 附属前一拍（历史行为）；
    - 组合附加符随基字成簇（``ɛ̃`` 一拍），IPA 修饰符（``ʰ`` ``ː`` ``˥``
      ``ⁿ`` …）附属前一单位，重音符（``ˈ`` ``ˌ``）附属后一单位开头；
    - 含 IPA 特有字符的文本改按**音节**拆分（``ipa_syllables``）——
      IPA 的拍是音节而不是音段；
    - 逗号是读音分隔符，会被跳过。
    """
    if not text:
        return []
    from strange_uta_game.backend.infrastructure.parsers.ipa import (
        has_ipa_text,
        ipa_syllables,
        is_stress_mark,
    )
    from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
        CharType,
        get_char_type,
    )

    if has_ipa_text(text):
        syllables = ipa_syllables(text.replace(",", ""))
        if syllables:
            return syllables
        return [text.replace(",", "")] if text.replace(",", "") else []

    from strange_uta_game.backend.domain.graphemes import split_graphemes

    moras: List[str] = []
    pending_stress = ""
    for cluster in split_graphemes(text):
        if cluster == ",":
            continue  # 跳过逗号分隔符
        if is_stress_mark(cluster):
            pending_stress += cluster
            continue
        unit = pending_stress + cluster
        pending_stress = ""
        attaches = (
            cluster in _SMALL_KANA
            or get_char_type(cluster) == CharType.MODIFIER
        )
        if moras and attaches:
            moras[-1] += unit
        else:
            moras.append(unit)
    if pending_stress:
        if moras:
            moras[-1] += pending_stress
        else:
            moras.append(pending_stress)
    return moras


def split_ruby_units(text: str) -> List[str]:
    """把 ruby 文本切成不可再分的**读音单位**列表（跳过逗号分隔符）。

    单位 = 字素簇（``ɛ̃`` ``t͡ʃ`` 各为一个单位）+ 重音符前置归并。
    供「按字符均分」模式使用——既不会把基字与附加符拆开，也不做假名
    mora / IPA 音节归并（那是 "mora" 模式的 :func:`split_into_moras` 的职责）。
    """
    from strange_uta_game.backend.domain.graphemes import split_graphemes
    from strange_uta_game.backend.infrastructure.parsers.ipa import is_stress_mark

    units: List[str] = []
    pending = ""
    for cluster in split_graphemes(text):
        if cluster == ",":
            continue
        if is_stress_mark(cluster):
            pending += cluster
            continue
        unit = pending + cluster
        pending = ""
        units.append(unit)
    if pending:
        if units:
            units[-1] += pending
        else:
            units.append(pending)
    return units


def distribute_ruby_chars_evenly(chars: List[str], target_count: int) -> List[str]:
    """将字符列表真正均分到 target_count 个组中。

    每组获得 ceil(剩余字符数 / 剩余组数) 个字符。
    前面组可能比后面组多一个字符。
    当字符数 <= target_count 时，逐字符分配，不足补占位符（停顿符）。

    Args:
        chars: 字符列表（已去除逗号）
        target_count: 目标组数

    Returns:
        长度为 target_count 的字符串列表
    """
    import math
    if target_count <= 0:
        return []
    if target_count == 1:
        return ["".join(chars)]
    if len(chars) <= target_count:
        return chars + [get_ruby_pause_char()] * (target_count - len(chars))
    result = []
    remaining = len(chars)
    remaining_parts = target_count
    pos = 0
    while remaining_parts > 0:
        size = math.ceil(remaining / remaining_parts)
        result.append("".join(chars[pos : pos + size]))
        pos += size
        remaining -= size
        remaining_parts -= 1
    return result


def split_ruby_for_checkpoints(ruby_text: str, total_cps: int) -> List[str]:
    """将 ruby 纯读音文本按 checkpoint 数量拆分。

    入参: ruby_text 纯读音串（不再支持 `#` 分组标记）; total_cps 节奏点数量。
    出参: 长度为 total_cps 的读音分段列表；优先按 mora 对齐，否则按字符均分。
    逗号是读音分隔符，会被跳过。

    当 mora/字符数 > total_cps 时，多余部分会合到末段，确保输出长度 = total_cps。
    """
    if total_cps <= 0:
        return [ruby_text.replace(',', '')] if ruby_text else []
    if total_cps == 1:
        return [ruby_text.replace(',', '')]

    moras = split_into_moras(ruby_text)
    if len(moras) == total_cps:
        return moras

    # mora 数量 > total_cps 时，多余 mora 均分到各段
    if len(moras) > total_cps:
        return distribute_ruby_chars_evenly(moras, total_cps)

    # 按读音单位拆分（字素簇，附加符不与基字拆开）时跳过逗号
    units = split_ruby_units(ruby_text)
    if len(units) <= total_cps:
        # 单位数 ≤ cp 数: 每个 cp 分一个单位，多余 cp 分占位符（停顿符）
        return units + [get_ruby_pause_char()] * (total_cps - len(units))

    # 单位数 > cp 数: 均分到各段
    return distribute_ruby_chars_evenly(units, total_cps)


def split_ruby_segments(
    ruby_text: str, check_count: int, mode: str
) -> List[str]:
    """按指定分段方式把读音文本切为 RubyPart 文本列表。

    字符修改/批量变更对话框的**预览与实际写回共用本函数**，保证所见即所得。
    占位规则：缺读音的节奏点一律用占位符（停顿符）补位，禁止空串。

    mode:
      - "direct": 逗号手动分段。空段（连续/首尾逗号）解析为占位符；
        段数 < check_count 时补占位符，> check_count 时合并尾段——
        与 ``Character.set_check_count`` 的不变式收口行为一致。
      - "char" / "mora": 委托 ``distribute_ruby_chars_evenly`` /
        ``split_ruby_for_checkpoints``（check_count >= 2 时恒返回
        check_count 段）；check_count <= 1 时整段不拆。

    Returns:
        分段文本列表；输入为空白时返回 ``[]``（表示无注音）。
        头尾空格与中间空格同为实义字符（导出文件中空格读音有意义），
        只参与空白判断，不做剥离。
    """
    if not ruby_text.strip():
        return []
    text = ruby_text

    if mode == "direct":
        pause = get_ruby_pause_char()
        segs = text.split(",")
        segs = [s if s else pause for s in segs]
        if check_count >= 1 and len(segs) < check_count:
            segs = segs + [pause] * (check_count - len(segs))
        elif check_count >= 1 and len(segs) > check_count:
            segs = segs[: check_count - 1] + ["".join(segs[check_count - 1 :])]
        return segs

    clean = text.replace(",", "")
    if not clean:
        return []
    if check_count <= 1:
        return [clean]
    if mode == "char":
        return distribute_ruby_chars_evenly(split_ruby_units(clean), check_count)
    # mode == "mora"
    return split_ruby_for_checkpoints(clean, check_count)


def align_ruby_parts_to_checkpoints(
    parts: List[str], check_count: int, is_sentence_end: bool = False
) -> List[str]:
    """将 Ruby 分段列表对齐到 check_count。

    入参: parts 原始分段列表; check_count 目标分段数; is_sentence_end 是否为停顿点。
    出参: 对齐后的分段列表；cp<2 或停顿点合并为单段，多余合并到末段，缺失用空格填充。
    """
    joined = "".join(parts)
    if not joined:
        return []

    # cp<2 或停顿点 → 不分组
    if check_count < 2 or is_sentence_end:
        return [joined]

    n = len(parts)
    if n == check_count:
        return [p if p else " " for p in parts]
    if n > check_count:
        # 多余组合并到末组
        head = list(parts[: check_count - 1])
        tail = "".join(parts[check_count - 1 :])
        merged = head + [tail if tail else " "]
        return [p if p else " " for p in merged]
    # 缺失组以空格填充
    padded = [p if p else " " for p in parts]
    return padded + [" "] * (check_count - n)


# ──────────────────────────────────────────────
# 序列化: Sentence → 内联文本
# ──────────────────────────────────────────────

REST_CHAR = "▨"
RUBY_SEP = "＋"  # 全角加号


def _collect_linked_group(chars: List[Character], start: int) -> Tuple[int, int]:
    """收集从 start 开始的连词字符组的范围。

    Returns:
        (group_start, group_end) — 左闭右开
    """
    group_start = start
    group_end = start
    # 向后收集 linked_to_next=True 的字符
    while group_end < len(chars) and chars[group_end].linked_to_next:
        group_end += 1
    # 最后一个 linked 的字符也包含在内
    if group_end < len(chars):
        group_end += 1
    return group_start, group_end


def _is_linked_group(chars: List[Character], start: int) -> bool:
    """判断 start 位置的字符是否是连词组的开始。"""
    return start < len(chars) and chars[start].linked_to_next


def to_inline_text(sentence: Sentence) -> str:
    """将一个 Sentence 序列化为 RhythmicaLyrics 风格内联文本。

    连词字符组（linked_to_next=True）会合并输出：
    - 有注音、或含 cc>0 成员的连词组：合并成一个 Ruby Node（F1：
      Normal Node 表达不了组内多 cp 成员的连词关系，reimport 会拆散；
      Ruby Node 的 ＋ 分段能逐成员还原 cc/时间戳，纯 cc=0 组仍走
      Normal Node、由解析侧 cc=0 连读规则复原）
    - 全员 cc=0 的无注音连词组：合并成一个 Normal Node
    """
    parts: List[str] = []
    chars = sentence.characters
    i = 0

    while i < len(chars):
        char = chars[i]

        # 检查是否是连词组的开始
        if _is_linked_group(chars, i):
            group_start, group_end = _collect_linked_group(chars, i)
            group_chars = chars[group_start:group_end]

            # 检查连词组中是否有注音或 cc>0 成员
            use_ruby_node = any(
                c.ruby or c.check_count > 0 for c in group_chars
            )

            if use_ruby_node:
                # 合并成一个 Ruby Node（含 F1 的多 cp 无注音成员路径）
                parts.append(_linked_group_to_ruby_node(group_chars))
            else:
                # 无注音的连词组：合并成一个 Normal Node
                parts.append(_linked_group_to_normal_node(group_chars))

            i = group_end
        elif char.ruby:
            # 单个有注音的字符：Ruby Node
            parts.append(_single_char_to_ruby_node(char))
            i += 1
        else:
            # 单个普通字符：Normal Node
            parts.append(_single_char_to_normal_node(char))
            i += 1

    return "".join(parts)


def _single_char_to_normal_node(char: Character) -> str:
    """将单个普通字符转换为 Normal Node。

    格式:
    - 无 CP (check_count=0): 直接输出字符本身（如空格）
    - 有 CP 有时间戳: [N|ts]char[ts][ts]...（N=check_count，F1）
    - 有 CP 无时间戳: [N]char
    - 有停顿点 CP 有时间戳: char[10|ts]
    - 有停顿点 CP 无时间戳: char[10]
    """
    display_char = REST_CHAR if char.is_rest else char.char

    # 无 CP 的字符（如空格）直接输出
    if char.check_count == 0:
        # 检查是否有停顿点标记
        if char.is_sentence_end:
            _se = _export_sentence_end_ts(char)
            if _se is not None:
                return f"{display_char}[10|{format_timestamp(_se)}]"
            else:
                return f"{display_char}[10]"
        return display_char

    char_parts: List[str] = []

    # 主时间戳（F1）：N 按 check_count 输出——硬编码 "1" 只取首个时间戳，
    # 多拍字符 round-trip 会丢节奏点。N 与停顿点标记 [10] 冲突，
    # 与 ruby node 路径一致封顶 9。
    _ts_list = _export_timestamps(char)
    n_out = min(char.check_count, 9)
    if _ts_list:
        char_parts.append(f"[{n_out}|{format_timestamp(_ts_list[0])}]")
    else:
        char_parts.append(f"[{n_out}]")

    # 字符本身
    char_parts.append(display_char)

    # 补充 checkpoint（F1）：其余时间戳用无前缀 [ts] 补充标签
    # （解析器本就支持），放在字符之后归属前一字
    if _ts_list:
        for extra_ts in _ts_list[1:n_out]:
            char_parts.append(f"[{format_timestamp(extra_ts)}]")

    # 停顿点标记
    if char.is_sentence_end:
        _se = _export_sentence_end_ts(char)
        if _se is not None:
            char_parts.append(f"[10|{format_timestamp(_se)}]")
        else:
            char_parts.append("[10]")

    return "".join(char_parts)


def _single_char_to_ruby_node(char: Character) -> str:
    """将单个有注音的字符转换为 Ruby Node。

    格式:
    - 有时间戳: {display|[count|ts]ruby[ts]ruby}
    - 无时间戳: {display|[N]ruby}
    - 有停顿点 CP 有时间戳: ...}[10|ts]
    - 有停顿点 CP 无时间戳: ...}[10]

    停顿符占位 part 原样保留：RL 编辑模式面向打轴软件
    （RhythmicaLyrics），占位拍在该侧是有意义的，不剥离。
    """
    assert char.ruby is not None
    display = char.char
    ruby_segments = [p.text for p in char.ruby.parts]

    # Ruby count
    count = min(len(ruby_segments), 9)

    # 有时间戳的情况
    _ts_list = _export_timestamps(char)
    if _ts_list:
        mora_portions: List[str] = []  # 存放每一拍的组合字符串
        for cp_idx in range(count):
            portion_str = ""
            if cp_idx < len(_ts_list):
                ts = _ts_list[cp_idx]
                # 第一个时间戳前没有 [
                if cp_idx == 0:
                    portion_str += f"{format_timestamp(ts)}]"
                else:
                    portion_str += f"[{format_timestamp(ts)}]"
            # ruby 文本始终输出
            if cp_idx < len(ruby_segments):
                portion_str += ruby_segments[cp_idx]

            mora_portions.append(portion_str)

        inner = "".join(mora_portions)
        result = f"{{{display}|[{count}|{inner}}}"
    else:
        # RL 的 [N] 表示整个 ruby 的节奏点数量，只能在读音开头写一次；
        # RubyPart 边界由导入侧按 N 重新拆分，不能把 [N] 当逐 part 分隔符。
        n_out = min(char.check_count, 9)
        ruby_text = "".join(ruby_segments[:9])
        result = f"{{{display}|[{n_out}]{ruby_text}}}"

    # 停顿点标记
    if char.is_sentence_end:
        _se = _export_sentence_end_ts(char)
        if _se is not None:
            result += f"[10|{format_timestamp(_se)}]"
        else:
            result += "[10]"

    return result


def _linked_group_to_normal_node(group: List[Character]) -> str:
    """将无注音的连词字符组转换为 Normal Node。

    每个字符独立输出时间戳和字符。

    格式:
    - 无 CP (check_count=0): 直接输出文本
    - 有 CP 有时间戳: [N|ts]char[ts]...（N=check_count，F1）
    - 有 CP 无时间戳: [N]char
    - 停顿点字符后面加 [10|ts] 或 [10]
    """
    # 无 CP 的字符组直接输出
    if all(c.check_count == 0 for c in group):
        return "".join(REST_CHAR if c.is_rest else c.char for c in group)

    parts: List[str] = []
    for i, c in enumerate(group):
        display_char = REST_CHAR if c.is_rest else c.char

        if c.check_count > 0:
            # 有 CP 的字符：N 按 check_count 输出 + 无前缀 [ts] 补充标签（F1）
            _ts_list = _export_timestamps(c)
            n_out = min(c.check_count, 9)
            if _ts_list:
                parts.append(f"[{n_out}|{format_timestamp(_ts_list[0])}]{display_char}")
                for extra_ts in _ts_list[1:n_out]:
                    parts.append(f"[{format_timestamp(extra_ts)}]")
            else:
                parts.append(f"[{n_out}]{display_char}")
        else:
            # 无 CP 的字符
            parts.append(display_char)

        # 停顿点标记（无论是否有 CP）
        if c.is_sentence_end:
            _se = _export_sentence_end_ts(c)
            if _se is not None:
                parts.append(f"[10|{format_timestamp(_se)}]")
            else:
                parts.append("[10]")

    return "".join(parts)


def _linked_group_to_ruby_node(group: List[Character]) -> str:
    """将有注音的连词字符组转换为 Ruby Node。

    合并所有字符的文本和注音，使用 ＋ 分隔各字符。
    有注音的字符输出注音，无注音的字符输出空的分隔符。

    格式: {display|[count|ts]ruby＋＋＋}[10|ts]

    F1：无注音但 cc>0 的成员同样按 ruby node 路径输出自己的多 cp
    标签（N 按 check_count），reimport 时连词关系与节奏点不丢。
    停顿符占位 part 原样保留（RL 编辑模式面向打轴软件，不剥离）。
    """
    display = "".join(c.char for c in group)

    # 构建 ruby 部分
    ruby_portions: List[str] = []
    for c in group:
        if c.ruby:
            ruby_segments = [p.text for p in c.ruby.parts]
            # 修复核心：针对当前遍历到的字，计算属于它自己的真实 count
            char_count = min(len(ruby_segments), 9)
            # N 按 check_count 输出（不变式下与 len(parts) 相等；
            # cc=0 的无 mora 形态输出 [0]，reimport 能还原 cc）
            n_out = min(c.check_count, 9)

            portion_parts: List[str] = []
            _ts_list = _export_timestamps(c)
            for cp_idx in range(char_count):
                part_str = ""
                if cp_idx < len(_ts_list):
                    ts = _ts_list[cp_idx]
                    # 第一个时间戳前没有 [
                    if cp_idx == 0:
                        # 写入各自正确的 N
                        part_str += f"[{n_out}|{format_timestamp(ts)}]"
                    else:
                        part_str += f"[{format_timestamp(ts)}]"
                elif cp_idx == 0:
                    # 无时间戳时只在该字符读音开头写一次 [N]。
                    part_str += f"[{n_out}]"

                if cp_idx < len(ruby_segments):
                    part_str += ruby_segments[cp_idx]

                portion_parts.append(part_str)

            # 直接拼接，不用逗号
            ruby_portions.append("".join(portion_parts))
        elif c.check_count > 0:
            _ts_list = _export_timestamps(c)
            n_out = min(c.check_count, 9)
            portion_parts: List[str] = []
            for cp_idx in range(n_out):
                if cp_idx < len(_ts_list):
                    ts = _ts_list[cp_idx]
                    if cp_idx == 0:
                        portion_parts.append(f"[{n_out}|{format_timestamp(ts)}]")
                    else:
                        portion_parts.append(f"[{format_timestamp(ts)}]")
                elif cp_idx == 0:
                    # 无时间戳时只在该字符 portion 开头写一次 [N]。
                    portion_parts.append(f"[{n_out}]")
            ruby_portions.append("".join(portion_parts))
        else:
            # 无注音的字符，输出空的分隔符
            ruby_portions.append("")

    # 用 ＋ 分隔各字符
    inner = RUBY_SEP.join(ruby_portions)
    result = f"{{{display}|{inner}}}"

    # 停顿点标记（取最后一个字符的停顿点标记）
    last_char = group[-1]
    if last_char.is_sentence_end:
        _se = _export_sentence_end_ts(last_char)
        if _se is not None:
            result += f"[10|{format_timestamp(_se)}]"
        else:
            result += "[10]"

    return result


def sentences_to_inline_text(sentences: List[Sentence]) -> str:
    """多行序列化，换行分隔。"""
    return "\n".join(to_inline_text(s) for s in sentences)


# 兼容别名
lines_to_inline_text = sentences_to_inline_text


# ──────────────────────────────────────────────
# 反序列化: 内联文本 → Sentence
# ──────────────────────────────────────────────

# 正则: 匹配 [N|MM:SS:cc] 或 [MM:SS:cc] 或 [N]（无时间戳）。
# 分钟位宽 \d{2,}（F15）：format_timestamp 对 ≥100 分钟输出 3 位分钟，
# 旧正则 \d{2} 固定两位会导致长曲时间戳导出后读不回来。
_TAG_RE = re.compile(r"\[(?:(\d+e?)\|)?(\d{2,}:\d{2}:\d{2})\]")
_TAG_NO_TS_RE = re.compile(r"\[(\d+e?)\]")


def _parse_char_tokens(segment: str) -> List[Tuple[Optional[str], Optional[int], str]]:
    """解析一段文本中的 (n_str|None, timestamp_ms|None, following_text) 三元组。

    segment 形如 "[2|00:14:64]や[00:15:61]わ" → [(2,14640,"や"), (None,15610,"わ")]
    也支持无时间戳格式: "[2]や[10]" → [(2,None,"や"), ("10",None,"")]
    """
    tokens: List[Tuple[Optional[str], Optional[int], str]] = []
    pos = 0

    while pos < len(segment):
        # 尝试匹配带时间戳的 tag: [N|MM:SS:cc] 或 [MM:SS:cc]
        m = _TAG_RE.search(segment, pos)
        # 尝试匹配无时间戳的 tag: [N]
        m_no_ts = _TAG_NO_TS_RE.search(segment, pos)

        if m and (not m_no_ts or m.start() <= m_no_ts.start()):
            # 找到带时间戳的 tag
            # 处理 tag 之前的文本
            if m.start() > pos:
                text = segment[pos:m.start()]
                if text and text != RUBY_SEP:
                    # 前面有文本，附加到上一个 token
                    if tokens:
                        n, ts, prev_text = tokens[-1]
                        tokens[-1] = (n, ts, prev_text + text)

            n_str = m.group(1)
            ts_ms = parse_timestamp(m.group(2))
            text_start = m.end()

            # 找下一个 tag 或结尾
            next_m = _TAG_RE.search(segment, text_start)
            next_m_no_ts = _TAG_NO_TS_RE.search(segment, text_start)
            sep_pos = segment.find(RUBY_SEP, text_start)

            if next_m:
                end = next_m.start()
            elif next_m_no_ts:
                end = next_m_no_ts.start()
            else:
                end = len(segment)

            # 在 ＋ 分隔符处截断
            if sep_pos != -1 and sep_pos < end:
                end = sep_pos

            text = segment[text_start:end]
            tokens.append((n_str, ts_ms, text))
            pos = end
        elif m_no_ts:
            # 找到无时间戳的 tag
            # 处理 tag 之前的文本
            if m_no_ts.start() > pos:
                text = segment[pos:m_no_ts.start()]
                if text and text != RUBY_SEP:
                    # 前面有文本，附加到上一个 token
                    if tokens:
                        n, ts, prev_text = tokens[-1]
                        tokens[-1] = (n, ts, prev_text + text)

            n_str = m_no_ts.group(1)
            text_start = m_no_ts.end()

            # 找下一个 tag 或结尾
            next_m = _TAG_RE.search(segment, text_start)
            next_m_no_ts = _TAG_NO_TS_RE.search(segment, text_start)
            sep_pos = segment.find(RUBY_SEP, text_start)

            if next_m:
                end = next_m.start()
            elif next_m_no_ts:
                end = next_m_no_ts.start()
            else:
                end = len(segment)

            # 在 ＋ 分隔符处截断
            if sep_pos != -1 and sep_pos < end:
                end = sep_pos

            text = segment[text_start:end]
            tokens.append((n_str, None, text))
            pos = end
        else:
            # 没有更多 tag
            break

    return tokens


def from_inline_text(text: str, singer_id: str) -> Sentence:
    """解析一行内联文本为 Sentence。"""
    characters: List[Character] = []

    # 提取 ruby 组和普通段
    segments = _split_ruby_groups(text)

    for seg_idx, (seg_type, seg_content) in enumerate(segments):
        if seg_type == "ruby":
            _parse_ruby_group(seg_content, characters, singer_id)
        else:
            # 检查是否是 ruby 组后面的停顿点标记 [10|ts] 或 [10]
            if characters and seg_content.startswith("[10"):
                # 尝试解析为停顿点标记
                m_ts = _TAG_RE.match(seg_content)
                m_no_ts = _TAG_NO_TS_RE.match(seg_content)

                if m_ts and m_ts.group(1) == "10":
                    # [10|ts] 格式
                    ts_ms = parse_timestamp(m_ts.group(2))
                    last_char = characters[-1]
                    last_char.is_sentence_end = True
                    last_char.sentence_end_ts = ts_ms
                    # 继续处理剩余内容
                    remaining = seg_content[m_ts.end():]
                    if remaining:
                        _parse_plain_segment(remaining, characters, singer_id)
                elif m_no_ts and m_no_ts.group(1) == "10":
                    # [10] 格式
                    last_char = characters[-1]
                    last_char.is_sentence_end = True
                    # 继续处理剩余内容
                    remaining = seg_content[m_no_ts.end():]
                    if remaining:
                        _parse_plain_segment(remaining, characters, singer_id)
                else:
                    _parse_plain_segment(seg_content, characters, singer_id)
            else:
                _parse_plain_segment(seg_content, characters, singer_id)

    # 设置 linked_to_next
    # 连词组规则：
    # 1. 有注音或 check_count>0 的字符，如果后面跟着 check_count=0 的字符，则 linked_to_next=True
    # 2. check_count=0 的字符，如果后面也是 check_count=0 的字符，则 linked_to_next=True
    # 3. 但是，is_sentence_end=True 的字符不会被链接到后面的字符
    for i in range(len(characters) - 1):
        curr = characters[i]
        next_char = characters[i + 1]
        # 停顿点字符不链接到后面的字符
        if curr.is_sentence_end:
            continue
        if next_char.check_count == 0 and not curr.linked_to_next:
            curr.linked_to_next = True

    return Sentence(
        singer_id=singer_id,
        characters=characters,
    )


def sentences_from_inline_text(text: str, singer_id: str) -> List[Sentence]:
    """多行解析。"""
    result = []
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        result.append(from_inline_text(stripped, singer_id))
    return result


# 兼容别名
lines_from_inline_text = sentences_from_inline_text


def _split_ruby_groups(text: str) -> List[Tuple[str, str]]:
    """将内联文本拆分为 ("ruby", content) 和 ("plain", content) 段。

    未闭合的 `{`（畸形输入）降级为普通文本段，不再抛裸 ValueError（E4）。
    """
    result: List[Tuple[str, str]] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            # 找匹配的 }（E4：index→find，未闭合时把余文当普通段）
            end = text.find("}", i + 1)
            if end < 0:
                result.append(("plain", text[i:]))
                break
            result.append(("ruby", text[i + 1 : end]))
            i = end + 1
        else:
            # 找下一个 { 或结尾
            next_brace = text.find("{", i)
            if next_brace == -1:
                result.append(("plain", text[i:]))
                break
            else:
                if next_brace > i:
                    result.append(("plain", text[i:next_brace]))
                i = next_brace
    return result


def _parse_ruby_group(
    content: str,
    characters: List[Character],
    singer_id: str,
) -> None:
    """解析 ruby 组内容 (不含花括号)。

    格式: "漢字|[count|ts]ruby[ts]ruby[10|ts]"
    - [count|ts]ruby: count 是 1-9，表示 ruby 分段数
    - [10|ts]: 停顿点时间戳（可选，在末尾）

    畸形输入健壮化（E4）：无 `|` 的组降级为普通文本段；N 标记无法
    解析时按 token 数降级，不抛裸 ValueError。
    """
    pipe_pos = content.find("|")
    if pipe_pos < 0:
        # 无竖线的畸形 ruby 组：按普通文本段还原字符，不抛异常
        _parse_plain_segment(content, characters, singer_id)
        return
    display_text = content[:pipe_pos]
    ruby_body = content[pipe_pos + 1 :]

    # 原文按字素簇拆分（ɛ̃/t͡ʃ 是一个字符）。旧版按码点导出的文件
    # （簇成员各自占一个 ＋ 段）段数对不上簇数时，退回码点拆分保往返。
    from strange_uta_game.backend.domain.graphemes import split_graphemes

    display_chars = split_graphemes(display_text)
    if len(display_chars) != ruby_body.count(RUBY_SEP) + 1 and len(display_text) == ruby_body.count(RUBY_SEP) + 1:
        display_chars = list(display_text)

    # 按 ＋ 分割各字符的 ruby 部分
    portions = ruby_body.split(RUBY_SEP)

    for portion_idx, portion in enumerate(portions):
        # 确定对应的显示字符
        char_text = (
            display_chars[portion_idx] if portion_idx < len(display_chars) else "?"
        )
        tokens = _parse_char_tokens(portion)

        if not tokens:
            # 无 checkpoint 信息
            ruby_text = portion.strip()
            ruby_obj = Ruby(parts=[RubyPart(text=ruby_text)]) if ruby_text else None
            character = Character(
                char=char_text,
                ruby=ruby_obj,
                check_count=0 if not ruby_text else len(split_into_moras(ruby_text)),
                singer_id=singer_id,
            )
            if ruby_obj:
                character.push_to_ruby()
            characters.append(character)
            continue

        # 第一个 token 的 N 是 count（1-9）。畸形 N 降级为按 token 数
        # （E4：不再对 `[1e]` 之类的变体抛裸 ValueError）
        first_n_str = tokens[0][0]
        if first_n_str is not None:
            try:
                count = min(int(first_n_str.rstrip("e")), 9)
            except ValueError:
                count = len(tokens)
        else:
            count = len(tokens)

        # 收集时间戳和 ruby 文本
        all_timestamps: List[int] = []
        ruby_text_parts: List[str] = []
        sentence_end_ts = None
        is_sentence_end = False

        for token_n, ts_ms, seg_text in tokens:
            # 检查是否是停顿点标记 [10|ts] 或 [10]
            if token_n == "10" and not seg_text:
                sentence_end_ts = ts_ms  # ts_ms 可能是 None（无时间戳）
                is_sentence_end = True
            else:
                all_timestamps.append(ts_ms)
                ruby_text_parts.append(seg_text)

        timestamps = [ts for ts in all_timestamps[:count] if ts is not None]

        # 标准 RL 写法只在 ruby 开头写一次 [N]；若后续没有时间标签，
        # 剩余读音会全部落在最后一个 token 中。根据 N 把该尾段重新按
        # mora 拆开，既接受标准格式，也继续兼容旧版重复 [N] 的文件。
        if 0 < len(ruby_text_parts) < count and ruby_text_parts[-1]:
            tail_count = count - len(ruby_text_parts) + 1
            ruby_text_parts = (
                ruby_text_parts[:-1]
                + split_ruby_for_checkpoints(ruby_text_parts[-1], tail_count)
            )

        if ruby_text_parts and not any(ruby_text_parts):
            # 所有拍都无文本 → 非注音的连词组成员（F1）：只还原字符、
            # cc 与时间戳，不伪造停顿符 ruby
            ruby_obj = None
        else:
            # per-char ruby 分段（结构化）。空段（该拍无新文字，如
            # `[2|ts]か[ts]` 的第二个拍）按占位符（停顿符）还原，段数不足
            # N 时补齐——维持 len(parts) == check_count 不变式，与
            # set_check_count 的收口行为一致（外部/手写文件同样适用）。
            pause = get_ruby_pause_char()
            ruby_parts = [
                RubyPart(text=p if p else pause) for p in ruby_text_parts
            ]
            if len(ruby_parts) < count:
                ruby_parts += [RubyPart(text=pause)] * (count - len(ruby_parts))
            ruby_obj = Ruby(parts=ruby_parts) if ruby_parts else None

        character = Character(
            char=char_text,
            ruby=ruby_obj,
            check_count=count,
            timestamps=timestamps,
            sentence_end_ts=sentence_end_ts,
            is_line_end=False,
            is_sentence_end=is_sentence_end,
            singer_id=singer_id,
        )
        character.push_to_ruby()
        characters.append(character)

    # 设置连词组的 linked_to_next（除了最后一个字符）
    if len(portions) > 1:
        for i in range(len(characters) - len(portions), len(characters) - 1):
            if i >= 0:
                characters[i].linked_to_next = True


def _parse_plain_segment(
    content: str,
    characters: List[Character],
    singer_id: str,
) -> None:
    """解析普通段 (非 ruby 组)。

    格式:
    - 无 CP (check_count=0): 直接输出字符本身（如空格）
    - 有 CP 有时间戳: [1|ts]char
    - 有 CP 无时间戳: [1]char
    - 有停顿点 CP 有时间戳: char[10|ts]
    - 有停顿点 CP 无时间戳: char[10]
    """
    pos = 0
    pending_tags: List[Tuple[Optional[str], int]] = []

    while pos < len(content):
        # 尝试匹配带时间戳的 tag: [N|MM:SS:cc] 或 [MM:SS:cc]
        m = _TAG_RE.match(content, pos)
        if m:
            n_str = m.group(1)
            ts_ms = parse_timestamp(m.group(2))
            pos = m.end()

            # 查看紧跟的文本（可能是多个字符，直到下一个 [ 或 {）
            if pos < len(content) and content[pos] not in "[{":
                # 读取所有非 [ 和 { 的字符
                text_start = pos
                while pos < len(content) and content[pos] not in "[{":
                    pos += 1
                text = content[text_start:pos]

                if n_str is not None:
                    # 新字符起始
                    if pending_tags:
                        _flush_pending(pending_tags, characters, singer_id)
                        pending_tags = []
                    pending_tags.append((n_str, ts_ms))
                    # 检查该字符是否还有后续 checkpoint tag (无 N 前缀)
                    while pos < len(content):
                        m2 = _TAG_RE.match(content, pos)
                        if m2 and m2.group(1) is None:
                            ts2 = parse_timestamp(m2.group(2))
                            pending_tags.append((None, ts2))
                            pos = m2.end()
                            # 吃掉可能的文本 (不应该有，但安全处理)
                            if pos < len(content) and content[pos] not in "[{":
                                text_start2 = pos
                                while pos < len(content) and content[pos] not in "[{":
                                    pos += 1
                        else:
                            break

                    is_rest = text == REST_CHAR
                    first_n = pending_tags[0][0]
                    if first_n is not None:
                        check_count, is_line_end, is_sentence_end = decode_check_n(
                            first_n
                        )
                    else:
                        check_count = len(pending_tags)
                        is_line_end = False
                        is_sentence_end = False

                    all_timestamps = [ts for _, ts in pending_tags]
                    timestamps = all_timestamps[:check_count]
                    sentence_end_ts = None
                    if is_sentence_end and len(all_timestamps) > check_count:
                        sentence_end_ts = all_timestamps[check_count]

                    # 为每个字符创建 Character（字素簇，附加符不与基字拆开）
                    from strange_uta_game.backend.domain.graphemes import (
                        split_graphemes,
                    )

                    text_units = split_graphemes(text)
                    for ch_idx, ch in enumerate(text_units):
                        if ch_idx == 0:
                            # 第一个字符：有 CP 和时间戳
                            character = Character(
                                char=ch,
                                check_count=check_count,
                                timestamps=timestamps,
                                sentence_end_ts=sentence_end_ts if len(text_units) == 1 else None,
                                is_line_end=is_line_end if len(text_units) == 1 else False,
                                is_sentence_end=is_sentence_end if len(text_units) == 1 else False,
                                is_rest=is_rest,
                                singer_id=singer_id,
                            )
                        else:
                            # 后续字符：无 CP
                            character = Character(
                                char=ch,
                                check_count=0,
                                is_sentence_end=is_sentence_end if ch_idx == len(text_units) - 1 else False,
                                sentence_end_ts=sentence_end_ts if ch_idx == len(text_units) - 1 else None,
                                singer_id=singer_id,
                            )
                        characters.append(character)
                    pending_tags = []
                else:
                    # 后续 checkpoint（归属前一个字符）
                    pending_tags.append((None, ts_ms))
            else:
                # 没有后续字符
                if n_str is not None and n_str == "10" and characters:
                    # [10|ts] 没有后续字符 → 这是停顿点时间戳
                    last_char = characters[-1]
                    last_char.is_sentence_end = True
                    last_char.sentence_end_ts = ts_ms
                else:
                    pending_tags.append((n_str, ts_ms))
        else:
            # 尝试匹配无时间戳的 tag: [N]
            m_no_ts = _TAG_NO_TS_RE.match(content, pos)
            if m_no_ts:
                n_str = m_no_ts.group(1)
                pos = m_no_ts.end()

                # 查看紧跟的文本字符（整簇消费，附加符不与基字拆开）
                if pos < len(content) and content[pos] not in "[{":
                    from strange_uta_game.backend.domain.graphemes import (
                        next_grapheme_end,
                    )

                    cluster_end = next_grapheme_end(content, pos)
                    ch = content[pos:cluster_end]
                    pos = cluster_end

                    check_count, is_line_end, is_sentence_end = decode_check_n(n_str)

                    character = Character(
                        char=ch,
                        check_count=check_count,
                        timestamps=[],
                        is_line_end=is_line_end,
                        is_sentence_end=is_sentence_end,
                        singer_id=singer_id,
                    )
                    characters.append(character)
                else:
                    # [N] 没有后续字符
                    if n_str == "10" and characters:
                        # [10] 没有后续字符 → 这是停顿点标记
                        last_char = characters[-1]
                        last_char.is_sentence_end = True
            else:
                # 非 tag 文本 — 裸字符（E7）：所有非 tag 字符都保留为
                # 无 CP 字符，与导出侧 cc=0 输出对称。旧实现只保留
                # 半角空格/Tab，"♪"、全角空格等全部丢失，整行无 tag 的
                # 句子 round-trip 会变空句。裸 ▨ 还原为休止符。
                # 按字素簇整簇消费（附加符不与基字拆开）。
                from strange_uta_game.backend.domain.graphemes import (
                    next_grapheme_end,
                )

                cluster_end = next_grapheme_end(content, pos)
                ch = content[pos:cluster_end]
                character = Character(
                    char=ch,
                    check_count=0,
                    is_rest=(ch == REST_CHAR),
                    singer_id=singer_id,
                )
                characters.append(character)
                pos = cluster_end

    if pending_tags:
        _flush_pending(pending_tags, characters, singer_id)


def _flush_pending(
    pending_tags: List[Tuple[Optional[str], int]],
    characters: List[Character],
    singer_id: str,
) -> None:
    """将未消费的 pending_tags 作为无字符 checkpoint 刷出。"""
    if not pending_tags:
        return
    first_n = pending_tags[0][0]
    if first_n is not None:
        check_count, is_line_end, is_sentence_end = decode_check_n(first_n)
    else:
        check_count = len(pending_tags)
        is_line_end = False
        is_sentence_end = False

    all_timestamps = [ts for _, ts in pending_tags]
    timestamps = all_timestamps[:check_count]
    sentence_end_ts = None
    if is_sentence_end and len(all_timestamps) > check_count:
        sentence_end_ts = all_timestamps[check_count]

    character = Character(
        char="?",
        check_count=check_count,
        timestamps=timestamps,
        sentence_end_ts=sentence_end_ts,
        is_line_end=is_line_end,
        is_sentence_end=is_sentence_end,
        singer_id=singer_id,
    )
    characters.append(character)
