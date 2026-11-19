"""假名→赫本式罗马音转换器（纯打表，零依赖）。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

_VOWELS = frozenset("aeiou")
# \u52a9\u8bcd\u8bfb\u97f3\u8986\u76d6\u8868\uff08\u8d6b\u672c\u5f0f\uff09\uff1a\u306f\u2192wa, \u3078\u2192e, \u3092\u2192o\u3002
# \u3092 \u5728 _KANA \u8868\u4e2d\u4e5f\u5df2\u6620\u5c04\u4e3a "o"\uff0c\u56e0\u6b64 particle_indices \u5bf9 \u3092 \u7684\u5224\u5b9a\u7ed3\u679c\u4e0d\u5f71\u54cd
# \u6700\u7ec8\u8f93\u51fa\uff1b\u6b64\u5904\u4fdd\u7559\u662f\u4e3a\u4e86\u8bed\u4e49\u5b8c\u6574\u6027\uff0c\u82e5\u5c06\u6765 _KANA["\u3092"] \u6539\u56de "wo" \u65f6\u903b\u8f91\u4ecd\u6b63\u786e\u3002
_PARTICLE_ROMAJI = {"\u306f": "wa", "\u3078": "e", "\u3092": "o"}

_KANA: dict[str, str] = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o",
    "ん": "n", "ゔ": "vu",
    "ヷ": "va", "ヺ": "vo",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa",
    "ゕ": "ka", "ゖ": "ke",
}

_SMALL_KANA = frozenset("ぁぃぅぇぉゃゅょゎゕゖ")

_DIGRAPHS: dict[str, str] = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo",
    "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
    "じゃ": "ja", "じゅ": "ju", "じょ": "jo",
    "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho",
    "ぢゃ": "ja", "ぢゅ": "ju", "ぢょ": "jo",
    "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo",
    "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo",
    "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo",
    "うぃ": "wi", "うぇ": "we", "うぉ": "wo",
    "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
    "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo",
    "てぃ": "ti", "でぃ": "di", "とぅ": "tu", "どぅ": "du",
    "しぇ": "she", "じぇ": "je", "ちぇ": "che",
    "つぁ": "tsa", "つぃ": "tsi", "つぇ": "tse", "つぉ": "tso",
    "くぁ": "kwa", "ぐぁ": "gwa",
}

_DIGRAPH_SPLIT: dict[str, Tuple[str, str]] = {
    "kya": ("ky", "a"), "kyu": ("ky", "u"), "kyo": ("ky", "o"),
    "gya": ("gy", "a"), "gyu": ("gy", "u"), "gyo": ("gy", "o"),
    "sha": ("sh", "a"), "shu": ("sh", "u"), "sho": ("sh", "o"),
    "ja": ("j", "a"), "ju": ("j", "u"), "jo": ("j", "o"),
    "cha": ("ch", "a"), "chu": ("ch", "u"), "cho": ("ch", "o"),
    "nya": ("ny", "a"), "nyu": ("ny", "u"), "nyo": ("ny", "o"),
    "hya": ("hy", "a"), "hyu": ("hy", "u"), "hyo": ("hy", "o"),
    "bya": ("by", "a"), "byu": ("by", "u"), "byo": ("by", "o"),
    "pya": ("py", "a"), "pyu": ("py", "u"), "pyo": ("py", "o"),
    "mya": ("my", "a"), "myu": ("my", "u"), "myo": ("my", "o"),
    "rya": ("ry", "a"), "ryu": ("ry", "u"), "ryo": ("ry", "o"),
}

_LINK_DIGRAPH = "digraph"
_LINK_LONG_VOWEL = "long_vowel"
_LINK_SOKUON = "sokuon"
_LINK_SMALL_KANA = "small_kana"
_LINK_VOICING_MARK = "voicing_mark"
HALFWIDTH_VOICING_MARKS = frozenset("ﾞﾟ")


@dataclass(frozen=True)
class RomajiOptions:
    """用户可配置的罗马音显示与分组策略。

    默认值保持历史行为：长音复写为元音，长音与促音不额外创建字符链接，
    输出使用小写。关闭长音复写时，连字符必须依附前一拍，因此长音链接的
    有效值始终为开启。
    """

    repeat_long_vowels: bool = True
    link_long_vowels: bool = False
    link_sokuon: bool = False
    uppercase: bool = False

    @property
    def effective_link_long_vowels(self) -> bool:
        return self.link_long_vowels or not self.repeat_long_vowels

    @classmethod
    def from_mapping(
        cls, values: Optional[Mapping[str, Any]] = None
    ) -> "RomajiOptions":
        values = values or {}
        return cls(
            repeat_long_vowels=bool(
                values.get("romaji_repeat_long_vowels", True)
            ),
            link_long_vowels=bool(values.get("romaji_link_long_vowels", False)),
            link_sokuon=bool(values.get("romaji_link_sokuon", False)),
            uppercase=bool(values.get("romaji_uppercase", False)),
        )


def is_halfwidth_katakana(ch: str) -> bool:
    """半角片假名和长音符。浊点、半浊点单独处理。"""
    return len(ch) == 1 and "\uff66" <= ch <= "\uff9d"


def _kata_to_hira_char(ch: str) -> str:
    if not ch:
        return ""
    if is_halfwidth_katakana(ch):
        ch = unicodedata.normalize("NFKC", ch)
    code = ord(ch)
    if 0x30A1 <= code <= 0x30F6:
        return chr(code - 0x60)
    return ch


def _compose_voicing_mark(base: str, mark: str) -> str:
    if len(base) != 1 or _kata_to_hira_char(base) not in _KANA:
        return ""
    voiced = unicodedata.normalize("NFKC", base + mark)
    if len(voiced) == 1 and _kata_to_hira_char(voiced) in _KANA:
        return voiced
    return ""


def _last_vowel(text: str) -> str:
    for ch in reversed(text.lower()):
        if ch in _VOWELS:
            return ch
    return ""


def _starts_with_vowel_or_y(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return lowered[0] in _VOWELS or lowered.startswith("y")


def _geminate_prefix(next_romaji: str, *, for_alignment: bool = False) -> str:
    lowered = next_romaji.lower()
    if not lowered:
        return ""
    if lowered.startswith("ch"):
        # Display spelling: っち/っちゃ → cchi/ccha (common Hepburn).
        # FA-Kara alignment spelling maps the geminate to t (tchi/tcha).
        return "t" if for_alignment else "c"
    first = lowered[0]
    if first in _VOWELS or first == "n":
        return ""
    return first if first.isalpha() else ""


def _split_digraph(romaji: str) -> Tuple[str, str]:
    if romaji in _DIGRAPH_SPLIT:
        return _DIGRAPH_SPLIT[romaji]
    vowel = _last_vowel(romaji)
    if vowel and romaji.endswith(vowel):
        return romaji[:-1], vowel
    return romaji, ""


class _ReadingChar(NamedTuple):
    part_idx: int
    char: str
    last_part_idx: int


def _flat_chars(
    parts: Sequence[str],
    part_char_indices: Sequence[int] | None,
) -> tuple[list[_ReadingChar], set[tuple[int, int, str]]]:
    """合成有效的半角浊音，保留读音首尾所在的原 part。"""
    flat: list[_ReadingChar] = []
    links: set[tuple[int, int, str]] = set()
    for part_idx, part in enumerate(parts):
        for ch in part:
            if flat and part_char_indices is not None:
                previous = flat[-1]
                if (
                    part_char_indices[part_idx]
                    > part_char_indices[previous.last_part_idx] + 1
                    and (
                        is_halfwidth_katakana(ch)
                        or any(
                            is_halfwidth_katakana(raw)
                            for raw in parts[previous.part_idx]
                        )
                    )
                ):
                    # 未注音的正文字符隔开了两个假名，保留这个边界。
                    flat.append(_ReadingChar(part_idx, "", part_idx))
            if ch in HALFWIDTH_VOICING_MARKS and flat:
                previous = flat[-1]
                last_part = previous.last_part_idx
                # Ruby 列表会跳过没有注音的字，不能隔着正文符号合成浊音。
                if part_idx <= last_part + 1 and (
                    part_char_indices is None
                    or part_char_indices[part_idx] <= part_char_indices[last_part] + 1
                ):
                    voiced = _compose_voicing_mark(previous.char, ch)
                    if voiced:
                        flat[-1] = _ReadingChar(previous.part_idx, voiced, part_idx)
                        if last_part != part_idx:
                            links.add((last_part, part_idx, _LINK_VOICING_MARK))
                        continue
            flat.append(_ReadingChar(part_idx, ch, part_idx))
    return flat, links


def _romaji_mora_at(flat: Sequence[_ReadingChar], index: int) -> str:
    if index >= len(flat):
        return ""
    ch = flat[index].char
    hira = _kata_to_hira_char(ch)
    if not hira or hira in ("っ", "ー") or ch in HALFWIDTH_VOICING_MARKS:
        return ""
    if hira == "ん":
        return "n"
    if index + 1 < len(flat):
        next_ch = flat[index + 1].char
        pair = hira + _kata_to_hira_char(next_ch)
        if pair in _DIGRAPHS:
            return _DIGRAPHS[pair]
    return _KANA.get(hira, ch)


def _romanize_ruby_parts_with_ownership(
    parts: Iterable[str],
    particle_indices: Optional[Iterable[int]] = None,
    *,
    for_alignment: bool = False,
    options: Optional[RomajiOptions] = None,
    part_char_indices: Sequence[int] | None = None,
) -> tuple[list[str], set[tuple[int, int, str]]]:
    """转换 RubyPart，并返回跨 part 的字符链接候选。

    候选中的 ``(lead_part, next_part, kind)`` 直接来自转换器命中的拗音、
    浊音、长音、促音或大小假名规则，供句子级转换结合原歌词 Character 类型决定是否
    链接。它不根据转换后的空字符串反推，因此不会把普通空 part 误认为被吸收
    的字符。
    """
    options = options or RomajiOptions()
    source = list(parts)
    result = ["" for _ in source]
    flat, link_candidates = _flat_chars(source, part_char_indices)
    particle_set = set(particle_indices or ())
    prev_vowel = ""
    index = 0

    while index < len(flat):
        part_idx, ch, last_part_idx = flat[index]
        if not ch or ch in HALFWIDTH_VOICING_MARKS:
            result[part_idx] += ch
            prev_vowel = ""
            index += 1
            continue
        hira = _kata_to_hira_char(ch)

        if (
            part_idx in particle_set
            and len(source[part_idx]) == 1
            and hira in _PARTICLE_ROMAJI
        ):
            romaji = _PARTICLE_ROMAJI[hira]
            result[part_idx] += romaji
            prev_vowel = _last_vowel(romaji)
            index += 1
            continue

        if hira == "っ":
            next_romaji = _romaji_mora_at(flat, index + 1)
            prefix = _geminate_prefix(next_romaji, for_alignment=for_alignment)
            if prefix and index + 1 < len(flat):
                following = flat[index + 1]
                next_part_idx = following.part_idx
                # 促音独立成拍、读后字辅音——ま|っ|て -> ma/t/te。促音
                # part 若只剩空 token，打轴/对齐拿不到独立区间，时间轴
                # 会与后字重合
                result[part_idx] += prefix
                result[next_part_idx] += next_romaji
                if options.link_sokuon and part_idx != next_part_idx:
                    link_candidates.add(
                        (part_idx, next_part_idx, _LINK_SOKUON)
                    )
                prev_vowel = _last_vowel(next_romaji)
                index += 2
                if index < len(flat):
                    small_part_idx, maybe_small, _ = flat[index]
                    pair = (
                        _kata_to_hira_char(following.char)
                        + _kata_to_hira_char(maybe_small)
                    )
                    if pair in _DIGRAPHS:
                        if following.last_part_idx != small_part_idx:
                            link_candidates.add(
                                (
                                    following.last_part_idx,
                                    small_part_idx,
                                    _LINK_DIGRAPH,
                                )
                            )
                        index += 1
            else:
                # 无法连浊（行尾/元音・拨音前）：显示口径回退 xtsu；
                # AI 打轴口径用 FA-Kara 的 tail_pron 弱辅音 h
                result[part_idx] += "h" if for_alignment else "xtsu"
                index += 1
            continue

        if hira == "ー":
            previous = flat[index - 1] if index > 0 else None
            if previous is not None and (
                not previous.char or previous.char in HALFWIDTH_VOICING_MARKS
            ):
                previous = None
            if options.repeat_long_vowels:
                repeated = prev_vowel or "-"
                result[part_idx] += repeated
                prev_vowel = repeated if repeated in _VOWELS else ""
            elif prev_vowel and previous is not None:
                # ``キャ|ー`` 等情况下，紧邻长音的 part 可能是已被拗音吸收的
                # 空 part；连字符仍应附到实际承载该拍罗马音的前导 part。
                anchor_part_idx = previous.part_idx
                for previous_index in range(index - 1, -1, -1):
                    candidate_part_idx = flat[previous_index].part_idx
                    if result[candidate_part_idx]:
                        anchor_part_idx = candidate_part_idx
                        break
                result[anchor_part_idx] += "-"
            else:
                result[part_idx] += "-"
                prev_vowel = ""
            if (
                options.effective_link_long_vowels
                and previous is not None
                and previous.last_part_idx != part_idx
            ):
                link_candidates.add(
                    (previous.last_part_idx, part_idx, _LINK_LONG_VOWEL)
                )
            index += 1
            continue

        if hira == "ん":
            next_romaji = _romaji_mora_at(flat, index + 1)
            romaji = "n'" if _starts_with_vowel_or_y(next_romaji) else "n"
            result[part_idx] += romaji
            prev_vowel = ""
            index += 1
            continue

        if index + 1 < len(flat):
            next_part_idx, next_ch, _ = flat[index + 1]
            pair = hira + _kata_to_hira_char(next_ch)
            digraph = _DIGRAPHS.get(pair)
            if digraph is not None:
                # A digraph is one mora.  Store it on its leading part even
                # when the small kana lives in the next RubyPart.
                result[part_idx] += digraph
                if last_part_idx != next_part_idx:
                    link_candidates.add(
                        (last_part_idx, next_part_idx, _LINK_DIGRAPH)
                    )
                prev_vowel = _last_vowel(digraph)
                index += 2
                continue

            if (
                options.effective_link_long_vowels
                and hira in _KANA
                and hira not in _SMALL_KANA
                and _kata_to_hira_char(next_ch) in _SMALL_KANA
                and last_part_idx != next_part_idx
            ):
                # 非拗音组合也保留小假名自己的罗马音与时间点，仅建立字符链接。
                # 例如 あ|ぁ -> a(link)|a。
                link_candidates.add(
                    (last_part_idx, next_part_idx, _LINK_SMALL_KANA)
                )

        romaji = _KANA.get(hira, ch)
        result[part_idx] += romaji
        prev_vowel = _last_vowel(romaji) or prev_vowel
        index += 1

    if options.uppercase:
        result = [text.upper() for text in result]
    return result, link_candidates


def romanize_ruby_parts(
    parts: Iterable[str],
    particle_indices: Optional[Iterable[int]] = None,
    *,
    for_alignment: bool = False,
    options: Optional[RomajiOptions] = None,
) -> List[str]:
    """将假名 RubyPart 文本逐一转为赫本式罗马音，保持 part 数量不变。

    促音独立成拍、读后字辅音（FA-Kara sokuon_split 口径）：ま|っ|て ->
    ma/t/te，促音与后字各自持有罗马音——编辑器转罗马音与 Kirakara 双注音
    导出均保持每字符原有分块，AI 打轴也拿到独立对齐区间。

    for_alignment（AI 打轴 token 专用，FA-Kara 对齐拼写）：促音遇ち行
    写作 t（tchi 而非 cchi）、无法连浊的促音回退弱辅音 h（tail_pron）
    而非 xtsu。默认 False 维持显示/导出拼写。
    """
    converted, _ = _romanize_ruby_parts_with_ownership(
        parts,
        particle_indices=particle_indices,
        for_alignment=for_alignment,
        options=options,
    )
    return converted


# ──────────────────────────────────────────────
# 句子/项目级罗马音转换（纯逻辑，独立于注音分析流程）
#
# 以下函数被 AutoCheckService（受 romanize_ruby 设置开关）与工具栏的
# 「全部转为罗马字注音」一次性操作共同复用。本层不调用注音引擎、不更新节奏点、
# 不删除注音——仅在转罗马音时记录跨字符拗音归属，并给无 ruby 的单假名补自注音。
# 依赖（domain / text_splitter）按需延迟导入，保持模块加载期零依赖。
# ──────────────────────────────────────────────


def is_self_romanizable_kana(char: str) -> bool:
    """单字符是否为可自注音的假名（平假名/片假名/促音/长音）。"""
    if len(char) != 1 or char in HALFWIDTH_VOICING_MARKS:
        return False
    from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
        CharType,
        get_char_type,
    )

    return get_char_type(char) in (
        CharType.HIRAGANA,
        CharType.KATAKANA,
        CharType.SOKUON,
        CharType.LONG_VOWEL,
    )


def detect_particle_part_indices(sentence) -> set:
    """检测句中作为助词的 は/へ/を 所在的 ruby part 全局索引（罗马音读音覆盖用）。

    part 索引在「按字符、按 part」顺序展开的扁平序列中计数，与
    :func:`romanize_sentence_in_place` 收集 texts 的顺序一致。

    判定规则（保守）：
      - を：恒为助词。
      - は/へ：需前一字符为日文字符；前字为汉字/片假名时直接判为助词，
        前字为假名时仅当后字非假名（或位于句末）才判为助词。
    """
    from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
        CharType,
        get_char_type,
    )

    particle_indices: set = set()
    part_idx = 0
    for char_idx, ch in enumerate(sentence.characters):
        if not ch.ruby:
            continue
        for part in ch.ruby.parts:
            text = part.text
            if len(text) != 1 or text != ch.char:
                part_idx += 1
                continue
            if text == "を":  # を
                particle_indices.add(part_idx)
                part_idx += 1
                continue
            if text not in ("は", "へ"):  # は / へ
                part_idx += 1
                continue
            if char_idx == 0:
                part_idx += 1
                continue
            prev = sentence.characters[char_idx - 1]
            prev_ct = get_char_type(prev.char) if len(prev.char) == 1 else CharType.OTHER
            if prev_ct not in (
                CharType.KANJI,
                CharType.HIRAGANA,
                CharType.KATAKANA,
                CharType.SOKUON,
                CharType.LONG_VOWEL,
            ):
                part_idx += 1
                continue
            # 前一字符是汉字/片假名 → 词边界信号强，直接判定为助词。
            if prev_ct in (CharType.KANJI, CharType.KATAKANA):
                particle_indices.add(part_idx)
                part_idx += 1
                continue
            # 前一字符是假名：后字非假名（或句末）才判为助词，否则保守不判。
            if char_idx + 1 >= len(sentence.characters):
                particle_indices.add(part_idx)
                part_idx += 1
                continue
            next_ch = sentence.characters[char_idx + 1]
            next_ct = (
                get_char_type(next_ch.char) if len(next_ch.char) == 1 else CharType.OTHER
            )
            if next_ct not in (
                CharType.HIRAGANA,
                CharType.KATAKANA,
                CharType.SOKUON,
                CharType.LONG_VOWEL,
            ):
                particle_indices.add(part_idx)
            part_idx += 1
    return particle_indices


def _halfwidth_source_reading(characters, index: int, reading: str) -> str:
    """还原引擎挂在首字的半角假名组合，让各原字符进入转换。"""
    base = characters[index].char
    if not is_halfwidth_katakana(base) or reading == base:
        return reading

    next_idx = index + 1
    hira = _kata_to_hira_char(base)
    if next_idx < len(characters):
        mark = characters[next_idx]
        if mark.char in HALFWIDTH_VOICING_MARKS and (
            mark.ruby is None or [p.text for p in mark.ruby.parts] == [mark.char]
        ):
            voiced = _compose_voicing_mark(base, mark.char)
            if voiced:
                hira = _kata_to_hira_char(voiced)
                next_idx += 1

    normalized = "".join(
        _kata_to_hira_char(ch) for ch in unicodedata.normalize("NFKC", reading)
    )
    if next_idx > index + 1 and normalized == hira:
        return base
    if next_idx < len(characters):
        next_char = characters[next_idx]
        if (
            len(next_char.char) == 1
            and next_char.ruby
            and len(next_char.ruby.parts) == 1
        ):
            next_hira = _kata_to_hira_char(next_char.char)
            pair = hira + next_hira
            next_reading = next_char.ruby.parts[0].text
            if (
                (pair in _DIGRAPHS or (next_hira == "ー" and hira in _KANA))
                and normalized == pair
                and len(next_reading) == 1
                and _kata_to_hira_char(next_reading) == next_hira
            ):
                return base
    return reading


def romanize_sentence_in_place(
    sentence,
    *,
    options: Optional[RomajiOptions] = None,
) -> None:
    """将句中所有假名 ruby part 就地转为赫本式罗马音（含助词/促音上下文）。

    保持每个 ruby 的 part 数量不变；非假名（已是罗马音/英文读音）原样保留。
    转换器识别到跨字符拗音时，在同一次转换中把前导字符链接到被吸收的小假名；
    假名注音阶段本身不创建此链接。
    """
    refs = []
    texts = []
    part_char_indices = []
    for char_idx, ch in enumerate(sentence.characters):
        if not ch.ruby:
            if char_idx and ch.char in HALFWIDTH_VOICING_MARKS:
                # 正文浊点只附在前字的自读音上，不给它创建 RubyPart 或节奏点。
                base = sentence.characters[char_idx - 1]
                if (
                    base.ruby
                    and len(base.ruby.parts) == 1
                    and len(texts[-1]) == 1
                    and _compose_voicing_mark(base.char, ch.char)
                    and _kata_to_hira_char(texts[-1]) == _kata_to_hira_char(base.char)
                ):
                    texts.append(ch.char)
                    part_char_indices.append(char_idx)
            continue
        for part in ch.ruby.parts:
            refs.append((len(texts), part))
            reading = part.text
            if len(ch.ruby.parts) == 1:
                reading = _halfwidth_source_reading(
                    sentence.characters, char_idx, reading
                )
            texts.append(reading)
            part_char_indices.append(char_idx)
    if not refs:
        return
    particle_indices = {refs[i][0] for i in detect_particle_part_indices(sentence)}
    converted, link_candidates = _romanize_ruby_parts_with_ownership(
        texts,
        particle_indices=particle_indices,
        options=options,
        part_char_indices=part_char_indices,
    )
    for part_idx, part in refs:
        part.text = converted[part_idx]
    for lead_part_idx, consumed_part_idx, link_kind in link_candidates:
        lead_char_idx = part_char_indices[lead_part_idx]
        consumed_char_idx = part_char_indices[consumed_part_idx]
        if consumed_char_idx != lead_char_idx + 1:
            continue

        lead_char = sentence.characters[lead_char_idx]
        consumed_char = sentence.characters[consumed_char_idx]
        if link_kind == _LINK_SOKUON:
            from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
                CharType,
                get_char_type,
            )

            # 只允许歌词正文中的 ``っ/ッ`` 链接到下一个假名。汉字 Ruby 内的
            # 促音（如 一体），以及 ``っ`` 后接汉字（如 真っ直ぐ）均不链接。
            if lead_char.char not in ("っ", "ッ", "ｯ"):
                continue
            if len(consumed_char.char) != 1 or get_char_type(
                consumed_char.char
            ) not in (CharType.HIRAGANA, CharType.KATAKANA):
                continue
        elif link_kind == _LINK_LONG_VOWEL and consumed_char.char not in ("ー", "ｰ"):
            # 只认全角、半角日文长音符。
            continue
        elif link_kind == _LINK_VOICING_MARK:
            if consumed_char.char not in HALFWIDTH_VOICING_MARKS or not (
                _compose_voicing_mark(lead_char.char, consumed_char.char)
            ):
                continue
        elif link_kind == _LINK_SMALL_KANA:
            from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
                CharType,
                get_char_type,
            )

            # 只连接歌词正文中的普通假名 + 小假名；不跨汉字 Ruby 建立链接，
            # 促音也仍由独立的「促音链接」选项控制。
            if (
                len(lead_char.char) != 1
                or get_char_type(lead_char.char)
                not in (CharType.HIRAGANA, CharType.KATAKANA)
                or len(consumed_char.char) != 1
                or _kata_to_hira_char(consumed_char.char) not in _SMALL_KANA
            ):
                continue

        sentence.characters[lead_char_idx].linked_to_next = True


def romanize_sentence_to_self_ruby(
    sentence,
    *,
    options: Optional[RomajiOptions] = None,
) -> bool:
    """Apply the editor's one-click romaji conversion to one sentence.

    Bare single kana first receive self-ruby, then every ruby part is converted
    together so cross-character sokuon/youon/long-vowel context is preserved;
    cross-character digraph ownership is linked as part of this conversion.
    Returns whether any self-ruby was added.
    """
    from strange_uta_game.backend.domain.models import Ruby, RubyPart

    added_self_ruby = False
    for ch in sentence.characters:
        if ch.ruby or not is_self_romanizable_kana(ch.char):
            continue
        ch.ruby = Ruby(parts=[RubyPart(text=ch.char)])
        ch.set_check_count(max(ch.check_count, 1), force=True)
        added_self_ruby = True

    romanize_sentence_in_place(sentence, options=options)
    return added_self_ruby


def romanize_project_to_self_ruby(
    project,
    progress_callback=None,
    *,
    options: Optional[RomajiOptions] = None,
) -> int:
    """「全部转为罗马字注音」一次性操作。

    两步：
      1. 给无 ruby 的单假名（平假名/片假名/促音/长音）创建自注音
         （假名本身作为 ruby 文本，保持 check_count 不变、至少为 1）；
      2. 整句假名 ruby 就地转罗马音（含助词/促音上下文），并把转换器实际
         吸收的跨字符小假名链接到前导字符。

    不调用注音引擎、不更新节奏点、不删除注音。对已是罗马音的 part 幂等。

    Args:
        project: 目标项目（就地修改）。
        progress_callback: ``(phase, current, total)`` 进度回调，可选。

    Returns:
        发生变化的句数。
    """
    changed = 0
    sentences = project.sentences
    total = len(sentences)
    for i, sentence in enumerate(sentences):
        def _join(sent) -> str:
            return "".join(
                p.text for c in sent.characters if c.ruby for p in c.ruby.parts
            )

        before = _join(sentence)
        touched = romanize_sentence_to_self_ruby(sentence, options=options)
        if touched or before != _join(sentence):
            changed += 1
        if progress_callback is not None:
            progress_callback("罗马音转换", i + 1, total)
    return changed
