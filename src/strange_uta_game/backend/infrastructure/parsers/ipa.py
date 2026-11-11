"""国际音标（IPA）文本处理 —— 触发判定、语音单位、音节切分。

设计动机
--------
SUG 的注音与主文字需要「完整支持国际音标」（Unicode 见
https://en.wikipedia.org/wiki/International_Phonetic_Alphabet ）。IPA 文本
与普通拉丁字母歌词混在同一编辑器里，必须先回答三个问题：

1. **哪些文本是 IPA？**——只有含「IPA 特有字符」的词才按 IPA 处理
   （音节节奏点、整词连词）；普通英文/法文/拼音歌词保持原有行为，
   逐字节不变。
2. **什么是 IPA 的一个「拍」？**——假名的拍是 mora；IPA 的拍是**音节**。
   带附加符的音段（``tʰ`` ``ɛ̃`` ``n̩``）与修饰符（``ː`` ``ʰ`` ``˥``）
   永远不单独成拍。
3. **音节边界在哪？**——显式标记优先（``.`` 音节点、``ˈ`` ``ˌ`` 重音号
   开新音节）；否则按「元音核心 + 最大音节首」启发式：
   两核心间 1 个辅音 → ``V.CV``（归后音节首），≥2 个辅音 → 首个归前音节
   尾、其余归后音节首（``VC.CCV``，如 ``æktʃən`` → ``æk.tʃən``）。
   连结线塞擦音（``t͡ʃ``）是不可拆的一个辅音单位。

所有函数纯标准库、无异常外抛（被歌词导入与自动注音路径调用）。
"""

from __future__ import annotations

import unicodedata

from strange_uta_game.backend.domain.graphemes import (
    grapheme_base,
    next_grapheme_end,
    split_graphemes,
)

__all__ = [
    "is_ipa_specific_char",
    "is_ipa_word",
    "has_ipa_text",
    "find_ipa_words",
    "is_stress_mark",
    "is_phonetic_modifier",
    "split_phonetic_units",
    "ipa_syllable_onset_offsets",
    "ipa_syllables",
]


# ──────────────────────────────────────────────
# 字符集
# ──────────────────────────────────────────────

def _expand(spec: str) -> frozenset[str]:
    """把 "0250-02AF 1D00" 形式的区间描述展开为字符集合。"""
    out = set()
    for part in spec.split():
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            for cp in range(int(lo_s, 16), int(hi_s, 16) + 1):
                out.add(chr(cp))
        else:
            out.add(chr(int(part, 16)))
    return frozenset(out)


# 「IPA 特有」字符：命中即把所在词判为 IPA 词。
# —— IPA Extensions（U+0250–02AF）：pɸ tɕ ɴ ʑ 等主要音标字母；
# —— 间隔修饰字母中的超音段/修饰符（避开 ʻ ʼ ʽ 等正字法撇号类）；
# —— 语音扩展 U+1D00–1DBF（ʰ ˖ ᵊ 等小型字母）；
# —— 修饰声调字母 U+A700–A71F、拉丁扩展增补(G) U+1DF00–1DFFF（extIPA）、
#    拉丁扩展(F) U+10780–107BF（IPA 专用修饰字母）；
# —— 双基字附加符（连结线）与 IPA 专属组合附加符。
_IPA_TRIGGER = _expand(
    "0250-02AF "  # IPA Extensions
    "02C8 02CC 02D0 02D1 02DE 02E0 02E1 02E2 02E3 02E4 02E5 02E6 02E7 02E8 "
    "02E9 02EC 02ED "
    "1D00-1DBF "  # Phonetic Extensions + Supplement
    "A700-A71F "
    "1DF00-1DFFF "
    "10780-107BF "
    "035C-0362 1DCD 1DFC "
    "0318 0319 031A 031C 031D 031E 031F 0320 0324 0325 0329 032A 032C 032F "
    "0330 0334 0339 033A 033B 033C 033D 0348 0349 034A 034B 034C 034D 034E "
    "0363 0364 0365 0366 1DC8 "
    "2074-2079 "  # 上标 4–9：粤语等声调数字（⁰¹²³ 是常见序号，不作触发）
)

# 音标词的成员字符（触发后向两侧扩展词边界时的合法成员）。
# 基字 = 拉丁/希腊字母（含 IPA 块）、附加符、修饰字母/声调字母、
# 上标字符、音节点 ``.``、连接符 ``‿``。
_MODIFIER_BACK = _expand(
    "02B0-02BA 02BC-02C1 02C2 02C3 02C4 02C5 02D0 02D1 02D2 02D3 02D4 02D5 "
    "02D6 02D7 02DB 02DD 02DE 02DF 02E0 02E1 02E2 02E3 02E4 02E5 02E6 02E7 "
    "02E8 02E9 02EA 02EB 02EC 02ED 02EE 02EF 02F0 02F1 02F2 02F3 02F4 02F5 "
    "02F6 02F7 02F8 02F9 02FA 02FB 02FC 02FD 02FE 02FF "
    "00B2 00B3 00B9 2070 2071 2074-208E 2090-209C "
    "1D2C-1D6A 1D78 1D9B-1DBF "
    "A700-A716 A717 A718 A719 A71A A71B A71C A71D A71E A71F "
    "02C2-02C5 "
)

# 重音/音节起点标记：依附**后**一个语音单位（构成其后音节的前缀）
_STRESS_MARKS = frozenset({"\u02c8", "\u02cc", "\ua71b", "\ua71c"})

# 元音字母（含拉丁通用与 IPA 专用；æ ø œ 等 Unicode 原样）
# 含带变音符的元音字母（ā ō ū ǎ ǐ ǔ â é í …）——罗马字/拼音/语音学惯例中
# 它们仍是元音核心；只影响已触发 IPA 词内的音节切分，正字法文本
# （café 等）不触发 IPA 路径，不受影响。
_VOWELS = frozenset(
    "aeiouyæɑɒøœɶɐɞʌɤɯɨʉɘɵəɚɝɜɛɪʏʊ"
    "AEIOUYÆØŒ"
    "āēīōūȳ"
    "ǎěǐǒǔ"
    "âêîôû"
    "áéíóúý"
    "àèìòù"
    "äëïöü"
    "ĀĒĪŌŪ"
    "ÂÊÎÔÛ"
    "ÁÉÍÓÚ"
    "ÀÈÌÒÙ"
    "ÄËÏÖÜ"
)

# 音节核心附加符：成音节（◌̩ U+0329 / ◌̍ U+030D）使辅音自成音节；
# 非成音节（◌̯ U+032F / ◌̑ U+0312）使元音失去核心资格。
_SYLLABIC_MARKS = frozenset({"\u0329", "\u030d", "\u0359"})
_NONSYLLABIC_MARKS = frozenset({"\u032f", "\u0312"})

_SYLLABLE_BREAK = "."  # IPA 显式音节点
_LINKING = "\u203f"  # ‿ 连接（前音节尾，不产生新音节）


def is_ipa_specific_char(ch: str) -> bool:
    """单个码点是否为 IPA 特有字符（判断词是否按 IPA 处理的依据）。"""
    return len(ch) == 1 and ch in _IPA_TRIGGER


def is_stress_mark(ch: str) -> bool:
    """是否为重音/上下降阶标记（``ˈ`` ``ˌ`` ``ꜛ`` ``ꜜ``）。"""
    return len(ch) == 1 and ch in _STRESS_MARKS


def is_phonetic_modifier(ch: str) -> bool:
    """是否为「依附前一单位」的语音修饰符（``ʰ`` ``ː`` ``˥`` ``ⁿ`` …）。"""
    return len(ch) == 1 and ch in _MODIFIER_BACK


def _is_letter_cluster(cluster: str) -> bool:
    """簇的基字是否为字母（拉丁/希腊等字母表文字，含 IPA 音标）。"""
    base = grapheme_base(cluster)
    if not base:
        return False
    name = unicodedata.name(base, "")
    return name.startswith(("LATIN ", "GREEK ", "COPTIC ", "CYRILLIC "))


def _is_word_member(ch: str, cluster: str) -> bool:
    """簇是否可作为 IPA 词的成员（用于词边界扩展）。

    组合附加符（U+0300–036F 等 Extend 码点）是词成员——否则 ``ɑ̃ː`` 会在
    附加符处断词，`̃` 后的 `ː` 变成单字母"词"而凭空获得节奏点。
    """
    if is_ipa_specific_char(ch) or is_phonetic_modifier(ch) or is_stress_mark(ch):
        return True
    if ch in _SYLLABLE_BREAK or ch == _LINKING:
        return True
    base = grapheme_base(cluster)
    if base in _VOWELS:
        return True
    # 字母基字 + 附加符的多码点簇（ɑ̃/n̩）整体是成员——附加符不断词
    return _is_letter_cluster(cluster)


def is_ipa_word(word: str) -> bool:
    """词（连续字母段）是否含 IPA 特有字符。"""
    return any(is_ipa_specific_char(ch) for ch in word)


def has_ipa_text(text: str) -> bool:
    """整段文本是否含 IPA 特有字符。"""
    return any(is_ipa_specific_char(ch) for ch in text)


def _has_word_base(word: str) -> bool:
    """词内是否含至少一个**非修饰符**音段（字母簇或元音）。

    纯修饰符串（`˥˥` 声调条、孤立 `ˈ`、`⁵⁵` 声调数字）依附相邻音段，
    自身不是词——不应作为「IPA 词」获得节奏点/词尾停顿点（审查反馈 #1）。
    """
    for cluster in split_graphemes(word):
        if len(cluster) > 1:
            return True  # 基字+附加符簇
        if not (is_stress_mark(cluster) or is_phonetic_modifier(cluster)):
            return True  # 字母 / 元音 / 数字等非修饰符基字
    return False


def find_ipa_words(text: str) -> list[tuple[int, int, str]]:
    """定位文本中的所有 IPA 词（含 IPA 特有字符的最大连续语音段）。

    Returns:
        ``(start, end, word)`` 列表，``end`` 为排他下标（与
        ``english_ruby.find_english_words`` 同构）。纯 ASCII / 纯正字法
        文本返回空表；纯修饰符串（声调条、孤立重音符）不构成词。
    """
    # 快速出口（审查反馈 #3）：无 IPA 特有字符的文本（常规日/中/英/韩歌词）
    # 一次线性扫描即返回，不进入逐簇分词——`find_english_words` 每句调用约
    # 5 次，CJK 行逐簇 next_grapheme_end 的开销会放大到大项目秒级。
    if not has_ipa_text(text):
        return []
    n = len(text)
    words: list[tuple[int, int, str]] = []
    # 先按「潜在成员」粗分段：字母、附加符、修饰符、音节点、连接符
    def _cluster_at(pos: int) -> tuple[int, str]:
        # 不能按 text[pos] >= U+0300 短路：IPA 区块字母（U+0250–02AF）本身
        # 小于 U+0300，但其后可跟组合附加符——next_grapheme_end 内部有真快路径
        end = next_grapheme_end(text, pos)
        return end - pos, text[pos:end]

    i = 0
    while i < n:
        cluster_len, cluster = _cluster_at(i)
        if _is_word_member(text[i], cluster):
            j = i
            end = i
            while j < n:
                clj, cluster_j = _cluster_at(j)
                if (
                    _is_word_member(text[j], cluster_j)
                    or text[j] == _SYLLABLE_BREAK
                    or text[j] == _LINKING
                ):
                    j += clj
                    end = j
                else:
                    break
            word = text[i:end]
            if is_ipa_word(word) and _has_word_base(word):
                words.append((i, end, word))
            i = max(end, i + 1)
        else:
            i += cluster_len
    return words


# ──────────────────────────────────────────────
# 语音单位（拍内不可拆）
# ──────────────────────────────────────────────


def split_phonetic_units(text: str) -> list[str]:
    """把文本切成「语音单位」：组合附加符并入基字、修饰符并入前一单位、
    重音符并入后一单位。单位串拼接无损。

    Example:
        >>> split_phonetic_units("ˈtʰɛ̃ːn̩")
        ['ˈtʰ', 'ɛ̃ː', 'n̩']
        >>> split_phonetic_units("həˈləʊ")
        ['hə', 'ˈlə', 'ʊ']
    """
    clusters = split_graphemes(text)
    units: list[str] = []
    pending_stress = ""
    dot_break = False  # 音节点后强制断单位（其后的修饰符不再并入前单位）
    for cluster in clusters:
        if cluster and is_stress_mark(cluster):
            # 重音符依附**后**一个单位（构成其后音节的前缀）；行尾悬挂的
            # 归入末单位
            pending_stress += cluster
            continue
        unit = pending_stress + cluster
        pending_stress = ""
        if cluster == _SYLLABLE_BREAK:
            dot_break = True
        if units and _attaches_back(unit) and not (dot_break and cluster != _SYLLABLE_BREAK):
            units[-1] += unit
        else:
            units.append(unit)
            dot_break = False
    if pending_stress:
        if units:
            units[-1] += pending_stress
        else:
            units.append(pending_stress)
    return units


def _attaches_back(unit: str) -> bool:
    """单位是否应并入前一单位：纯附加符，或基字是修饰符/音节点/连接符。"""
    base = grapheme_base(unit)
    if base in (_SYLLABLE_BREAK, _LINKING):
        return True
    return base in _MODIFIER_BACK


# ──────────────────────────────────────────────
# 音节切分
# ──────────────────────────────────────────────

# 单位属性位
_NUCLEUS = 0x1  # 音节核心（元音 / 成音节辅音）
_EXPLICIT_ONSET = 0x2  # 显式开新音节（重音符前缀）
_BOUNDARY_AFTER = 0x4  # 单位尾部带音节点 ``.``（其后开新音节）


def _unit_flags(units: list[str]) -> list[int]:
    """计算每个语音单位的属性位。"""
    stress_prefix = "".join(_STRESS_MARKS)
    flags: list[int] = []
    for u in units:
        f = 0
        if u[:1] in _STRESS_MARKS:
            f |= _EXPLICIT_ONSET
        # 音节点可被后随修饰符并入同单位（`k.ʰ`）——只要出现在单位内任何
        # 非首位位置都算「其后开新音节」（审查反馈 #4：endswith 会漏判）
        dot_pos = u.find(_SYLLABLE_BREAK)
        if dot_pos > 0:
            f |= _BOUNDARY_AFTER
        body = u.strip(stress_prefix + _SYLLABLE_BREAK + _LINKING)
        if body:
            # 单位可能含多个字素簇（连结线并簇 da͡i、附着修饰 tʰ）：
            # 任一簇的基字是元音（或带成音节符）即为核心
            non_syllabic = any(c in _NONSYLLABIC_MARKS for c in body)
            syllabic = any(c in _SYLLABIC_MARKS for c in body)
            if syllabic:
                f |= _NUCLEUS
            elif not non_syllabic:
                for cluster in split_graphemes(body):
                    if grapheme_base(cluster) in _VOWELS:
                        f |= _NUCLEUS
                        break
        flags.append(f)
    return flags


def _unit_onsets(units: list[str]) -> set[int]:
    """返回「开新音节」的单位下标集合（恒含 0）。空白分词，词内切音节。"""
    n = len(units)
    if n <= 1:
        return {0}
    flags = _unit_flags(units)
    onsets = {0}
    # 词边界：空白之后开新「音节」（空间隔不并入后词的辅音串）
    for i in range(n):
        if i > 0 and units[i - 1].isspace():
            onsets.add(i)
        # 显式边界：重音符标记的单位 / 音节点 ``.`` 之后的单位
        if i > 0 and flags[i] & _EXPLICIT_ONSET:
            onsets.add(i)
        if (flags[i] & _BOUNDARY_AFTER) and i + 1 < n:
            onsets.add(i + 1)

    # 相邻核心间的辅音归属（仅在**同一词内**配对）：1 个辅音归后（V.CV），
    # ≥2 个时首个归前（VC.CCV）
    word_bounds = [0] + [
        i for i in range(1, n) if i in onsets and units[i - 1].isspace()
    ]

    def _word_of(i: int) -> int:
        w = 0
        for b in word_bounds:
            if b <= i:
                w = b
            else:
                break
        return w

    clean_nuclei = [i for i in range(n) if flags[i] & _NUCLEUS]
    # 显式边界快照（音节点/重音符/空白）：两核心间**或终点上**已有显式边界
    # 时启发式让位——`æk.ə` 的 k. 属前一音节（æk.），不能按 V.CV 拆去后音节
    explicit_onsets = frozenset(onsets)
    for a, b in zip(clean_nuclei, clean_nuclei[1:]):
        if b - a <= 1:
            continue  # 相邻核心：复合元音，同一音节
        if _word_of(a) != _word_of(b):
            continue  # 跨词不配对（词首已由空白边界处理）
        mid = list(range(a + 1, b))
        if any(i in explicit_onsets for i in range(a + 1, b + 1)):
            continue
        boundary = mid[0] if len(mid) == 1 else mid[1]
        onsets.add(boundary)
    return onsets


def ipa_syllable_onset_offsets(word: str) -> set[int]:
    """返回 ``word`` 内各音节起始的**码点偏移**集合（恒含 0）。

    与 ``english_ruby.get_syllable_start_offsets`` 同构，供节奏点分配
    （音节首 = 1 个节奏点，其余 0）。无核心（纯辅音/纯修饰）时返回 {0}。

    前导重音符（``ˈ`` ``ˌ``）不承载节奏点——音节首指向其后的第一个
    实际音段（重音符本身是 MODIFIER，cc 恒 0）。
    """
    units = split_phonetic_units(word)
    if len(units) <= 1:
        # 单位词：跳过前导重音符/修饰符
        return {_skip_leading_modifiers(word, 0)}
    onsets = _unit_onsets(units)
    offsets = []
    pos = 0
    for u in units:
        offsets.append(pos)
        pos += len(u)
    result: set[int] = set()
    for i in onsets:
        k = _skip_leading_modifiers(units[i], offsets[i])
        if k is not None:
            result.add(k)
        elif i + 1 < len(units):
            # 整个单位都是重音符/修饰符（词首声调串）：音节首取下一单位
            result.add(offsets[i + 1])
    return result if result else {0}


def _skip_leading_modifiers(unit: str, offset: int) -> "int | None":
    """单位内第一个**实际音段**的偏移；单位全为重音符/修饰符时返回 None。

    前导重音符（ˈ ˌ）与前导修饰符（声调条 ˥、ʰ 等）都不承载节奏点——
    词首声调串（`˥˥ne`）的音节首应落在其后的元音/辅音音段上
    （审查反馈 #1 收尾）。
    """
    k = 0
    while k < len(unit) and (
        is_stress_mark(unit[k]) or is_phonetic_modifier(unit[k])
    ):
        k += 1
    if k >= len(unit):
        return None
    return offset + k


def ipa_syllables(text: str) -> list[str]:
    """把 IPA 文本切成音节串列表（拼接无损），供注音分段使用。

    Example:
        >>> ipa_syllables("həˈləʊ")
        ['hə', 'ˈləʊ']
        >>> ipa_syllables("æk.tʃən")
        ['æk.', 'tʃən']
        >>> ipa_syllables("a.i")
        ['a.', 'i']
    """
    units = split_phonetic_units(text)
    if not units:
        return []
    if len(units) == 1:
        return [text]
    onsets = _unit_onsets(units)
    syllables: list[str] = []
    current: list[str] = []
    for i, u in enumerate(units):
        if i in onsets and current:
            syllables.append("".join(current))
            current = []
        current.append(u)
    if current:
        syllables.append("".join(current))
    # 防御：切分必须无损，否则整段返回
    return syllables if "".join(syllables) == text else ([text] if text else [])
