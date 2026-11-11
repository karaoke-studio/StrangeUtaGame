"""字素簇（grapheme cluster）切分 —— 领域层文本原语，纯标准库实现。

为什么需要它
------------
``Character.char`` 的语义是「用户感知的一个字符」。历史实现按 **码点**
（``list(text)``）切字，对日/中/英/韩歌词恰好成立，但对带组合附加符的
文字不成立：国际音标 ``ɛ̃``（U+025B U+0303）、``n̩``（U+006E U+0329）、
``t͡ʃ``（U+0074 U+0361 U+0283），以及带变体选择符 / ZWJ 的 emoji、泰文、
天城文等，都是「多个码点 = 一个字符」。按码点切会把附加符拆成独立
``Character``——它既不能独立排版（孤立附加符没有可锚定的基字），也不该
独立承载节奏点。

本模块按 Unicode 标准附件 #29（UAX #29）的「扩展字素簇」规则切分，
与 Word / 浏览器 / Qt 等文本引擎的光标移动单位一致，并在其上做两处
**裁剪（tailoring）**：

1. **连结线并簇（IPA）**：``◌͡◌`` / ``◌͜◌`` 等「双基字附加符」
   （U+035C–U+0362、U+1DCD、U+1DFC）在逻辑上横跨前后两个基字。标准规则
   只把它并入前一个基字（``t͡`` + ``ʃ``），本模块把后一个字母也并入同一
   簇（``t͡ʃ``）：塞擦音 / 双重调音是一个音段，应是一个 ``Character``，
   且连结线必须与两个基字在同一次排版中才能正确定位。
2. **半角浊点不并簇（兼容）**：半角片假名的浊点 / 半浊点
   （U+FF9E / U+FF9F）按标准属于 Extend，会与前一假名并簇（``ｶﾞ``）。
   既有日文流程（注音分析偏移、节奏点规则与大量测试）一直把它们当作
   独立字符，这里保持不变。

不变式
------
- ``"".join(split_graphemes(text)) == text``（无损）。
- 不含上述可并簇码点的文本（常规日/中/英/韩歌词），
  ``split_graphemes(text) == list(text)``——行为与历史实现逐字符一致。

本模块无第三方依赖（不引入 ``regex`` / ``grapheme`` 等包）：分类表由
``unicodedata.category`` 加少量区间表构成，并在测试中与 Qt 的
``QTextBoundaryFinder`` 交叉验证。

与 Qt 边界查找器的已知无害分歧：连续两个印度系文字元音符号
（SpacingMark × SpacingMark，如天城文 ``ाा``）按 UAX #29 GB9a 不应断簇，
本模块遵循标准；Qt 自带实现在此选择断开。歌词排版不依赖簇内断点，
该分歧无实际影响。
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from typing import Dict, Iterator, List, Sequence, Tuple

__all__ = [
    "split_graphemes",
    "iter_graphemes",
    "grapheme_spans",
    "grapheme_count",
    "grapheme_boundaries",
    "next_grapheme_end",
    "is_single_grapheme",
    "is_grapheme_extender",
    "is_dependent_text",
    "starts_with_extender",
    "grapheme_base",
    "truncate_graphemes",
    "is_double_diacritic",
]


# ──────────────────────────────────────────────
# Grapheme_Cluster_Break 分类
# ──────────────────────────────────────────────

_OTHER = 0
_CR = 1
_LF = 2
_CONTROL = 3
_EXTEND = 4
_ZWJ = 5
_RI = 6
_PREPEND = 7
_SPACING = 8
_L = 9
_V = 10
_T = 11
_LV = 12
_LVT = 13

# 附加标志位（与上面的主分类正交）
_F_EXTPICT = 0x100  # Extended_Pictographic（GB11：emoji ZWJ 序列）
_F_INCB_CONSONANT = 0x200  # Indic_Conjunct_Break=Consonant（GB9c）
_F_INCB_LINKER = 0x400  # Indic_Conjunct_Break=Linker（GB9c）
_F_DOUBLE = 0x800  # 双基字附加符（连结线等，本模块裁剪用）
_CLASS_MASK = 0xFF


def _ranges(*pairs: int) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """把 (lo, hi, lo, hi, ...) 展平为 (starts, ends) 两个有序元组。"""
    starts = tuple(pairs[0::2])
    ends = tuple(pairs[1::2])
    return starts, ends


def _in_ranges(cp: int, table: Tuple[Tuple[int, ...], Tuple[int, ...]]) -> bool:
    starts, ends = table
    i = bisect_right(starts, cp) - 1
    return i >= 0 and cp <= ends[i]


# 类别为 Mc（或其他）但 Grapheme_Extend=Yes 的码点（Other_Grapheme_Extend），
# 以及 Unicode 16 起改判为 Extend 的若干 virama / 元音符号。
_EXTRA_EXTEND = frozenset(
    {
        0x09BE, 0x09D7, 0x0B3E, 0x0B57, 0x0BBE, 0x0BD7, 0x0CC0, 0x0CC2,
        0x0CC7, 0x0CC8, 0x0CCA, 0x0CCB, 0x0CD5, 0x0CD6, 0x0D3E, 0x0D57,
        0x0DCF, 0x0DDF, 0x1715, 0x1734, 0x1B35, 0x1B3B, 0x1B3D, 0x1B43,
        0x1B44, 0x1BAA, 0x1BF2, 0x1BF3, 0x200C, 0x302E, 0x302F, 0xA953,
        0xA9C0, 0x111C0, 0x11235, 0x1133E, 0x1134D, 0x11357, 0x113B8,
        0x113C2, 0x113C5, 0x113C7, 0x113C8, 0x113C9, 0x113CF, 0x114B0,
        0x114BD, 0x115AF, 0x116B6, 0x11930, 0x1193D, 0x11F41, 0x16FF0,
        0x16FF1, 0x1D165, 0x1D166, 0x1D16D, 0x1D16E, 0x1D16F, 0x1D170,
        0x1D171, 0x1D172,
        0x0897,  # ARABIC PIED NO QA：Cf 但 GCB=Extend
    }
)

# 整块视为组合附加符的区间：Python 内置 unicodedata 对块内**已分配**码点
# 本就返回 Mn，这里把已分配尾段并入 Extend 仅为容错（未来 Unicode 版本
# 新增的附加符直接可用）。范围止于 Unicode 17 已分配的 1ADD / 1AEB；
# 1ADE–1ADF、1AEC–1AFF 仍未分配，不能整段并簇。
_MARK_BLOCKS = _ranges(
    0x1AB0, 0x1ADD,  # Combining Diacritical Marks Extended
    0x1AE0, 0x1AEB,  # Combining Diacritical Marks Extended（续）
    0x1DC0, 0x1DFF,  # Combining Diacritical Marks Supplement
)

# 类别为 Mc 但 Grapheme_Cluster_Break=Other 的码点（不并簇）
_MC_NOT_SPACING = frozenset(
    {
        0x102B, 0x102C, 0x1038, 0x1062, 0x1063, 0x1064, 0x1067, 0x1068,
        0x1069, 0x106A, 0x106B, 0x106C, 0x106D, 0x1083, 0x1087, 0x1088,
        0x1089, 0x108A, 0x108B, 0x108C, 0x108F, 0x109A, 0x109B, 0x109C,
        0x1A61, 0x1A63, 0x1A64, 0xAA7B, 0xAA7D, 0x11720, 0x11721,
    }
)

# 类别不是 Mc 但 Grapheme_Cluster_Break=SpacingMark 的码点
_EXTRA_SPACING = frozenset({0x0E33, 0x0EB3})

# Grapheme_Cluster_Break=Prepend
_PREPEND_SET = frozenset(
    {
        0x0600, 0x0601, 0x0602, 0x0603, 0x0604, 0x0605, 0x06DD, 0x070F,
        0x0890, 0x0891, 0x08E2, 0x0D4E, 0x110BD, 0x110CD, 0x111C2, 0x111C3,
        0x113D1, 0x1193F, 0x11941, 0x11A3A, 0x11A84, 0x11A85, 0x11A86,
        0x11A87, 0x11A88, 0x11A89, 0x11D46, 0x11F02,
    }
)

# 半角片假名浊点 / 半浊点：标准属 Extend，本模块刻意不并簇（见模块文档）
_HALFWIDTH_VOICED_MARKS = frozenset({0xFF9E, 0xFF9F})

# 双基字附加符（连结线、双波浪线、双长音线等）：横跨前后两个基字
_DOUBLE_DIACRITICS = frozenset(
    {0x035C, 0x035D, 0x035E, 0x035F, 0x0360, 0x0361, 0x0362, 0x1DCD, 0x1DFC}
)

# Extended_Pictographic（emoji-data.txt；仅用于 GB11 的 ZWJ 序列判定）
_EXTPICT = _ranges(
    0x00A9, 0x00A9, 0x00AE, 0x00AE, 0x203C, 0x203C, 0x2049, 0x2049,
    0x2122, 0x2122, 0x2139, 0x2139, 0x2194, 0x2199, 0x21A9, 0x21AA,
    0x231A, 0x231B, 0x2328, 0x2328, 0x2388, 0x2388, 0x23CF, 0x23CF,
    0x23E9, 0x23F3, 0x23F8, 0x23FA, 0x24C2, 0x24C2, 0x25AA, 0x25AB,
    0x25B6, 0x25B6, 0x25C0, 0x25C0, 0x25FB, 0x25FE, 0x2600, 0x2605,
    0x2607, 0x2612, 0x2614, 0x2685, 0x2690, 0x2705, 0x2708, 0x2712,
    0x2714, 0x2714, 0x2716, 0x2716, 0x271D, 0x271D, 0x2721, 0x2721,
    0x2728, 0x2728, 0x2733, 0x2734, 0x2744, 0x2744, 0x2747, 0x2747,
    0x274C, 0x274C, 0x274E, 0x274E, 0x2753, 0x2755, 0x2757, 0x2757,
    0x2763, 0x2767, 0x2795, 0x2797, 0x27A1, 0x27A1, 0x27B0, 0x27B0,
    0x27BF, 0x27BF, 0x2934, 0x2935, 0x2B05, 0x2B07, 0x2B1B, 0x2B1C,
    0x2B50, 0x2B50, 0x2B55, 0x2B55, 0x3030, 0x3030, 0x303D, 0x303D,
    0x3297, 0x3297, 0x3299, 0x3299, 0x1F000, 0x1F0FF, 0x1F10D, 0x1F10F,
    0x1F12F, 0x1F12F, 0x1F16C, 0x1F171, 0x1F17E, 0x1F17F, 0x1F18E, 0x1F18E,
    0x1F191, 0x1F19A, 0x1F1AD, 0x1F1E5, 0x1F201, 0x1F20F, 0x1F21A, 0x1F21A,
    0x1F22F, 0x1F22F, 0x1F232, 0x1F23A, 0x1F23C, 0x1F23F, 0x1F249, 0x1F3FA,
    0x1F400, 0x1F53D, 0x1F546, 0x1F64F, 0x1F680, 0x1F6FF, 0x1F774, 0x1F77F,
    0x1F7D5, 0x1F7FF, 0x1F80C, 0x1F80F, 0x1F848, 0x1F84F, 0x1F85A, 0x1F85F,
    0x1F888, 0x1F88F, 0x1F8AE, 0x1F8FF, 0x1F90C, 0x1F93A, 0x1F93C, 0x1F945,
    0x1F947, 0x1FAFF, 0x1FC00, 0x1FFFD,
)

# Indic_Conjunct_Break=Linker（virama）与 Consonant（辅音字母），
# 覆盖 UAX #29 GB9c 适用的 6 种文字：天城文、孟加拉文、古吉拉特文、
# 奥里亚文、泰卢固文、马拉雅拉姆文。
_INCB_LINKERS = frozenset({0x094D, 0x09CD, 0x0ACD, 0x0B4D, 0x0C4D, 0x0D4D})
_INCB_CONSONANTS = _ranges(
    0x0915, 0x0939, 0x0958, 0x095F, 0x0978, 0x097F,  # Devanagari
    0x0995, 0x09A8, 0x09AA, 0x09B0, 0x09B2, 0x09B2, 0x09B6, 0x09B9,
    0x09DC, 0x09DD, 0x09DF, 0x09DF, 0x09F0, 0x09F1,  # Bengali
    0x0A95, 0x0AA8, 0x0AAA, 0x0AB0, 0x0AB2, 0x0AB3, 0x0AB5, 0x0AB9,
    0x0AF9, 0x0AF9,  # Gujarati
    0x0B15, 0x0B28, 0x0B2A, 0x0B30, 0x0B32, 0x0B33, 0x0B35, 0x0B39,
    0x0B5C, 0x0B5D, 0x0B5F, 0x0B5F, 0x0B71, 0x0B71,  # Oriya
    0x0C15, 0x0C28, 0x0C2A, 0x0C39, 0x0C58, 0x0C5A,  # Telugu
    0x0D15, 0x0D3A,  # Malayalam
)

# 默认可忽略的未分配码点（Unicode 把它们归入 Control）
_UNASSIGNED_CONTROL = _ranges(
    0x2065, 0x2065, 0xFFF0, 0xFFF8, 0xE0000, 0xE0000, 0xE0002, 0xE001F,
    0xE0080, 0xE00FF, 0xE01F0, 0xE0FFF,
)

_CLASS_CACHE: Dict[str, int] = {}


def _classify(ch: str) -> int:
    """返回单个码点的 (主分类 | 标志位)。结果按码点缓存。"""
    cached = _CLASS_CACHE.get(ch)
    if cached is not None:
        return cached
    cp = ord(ch)
    cls = _classify_uncached(ch, cp)
    _CLASS_CACHE[ch] = cls
    return cls


def _classify_uncached(ch: str, cp: int) -> int:
    if cp == 0x0D:
        return _CR
    if cp == 0x0A:
        return _LF
    if cp == 0x200D:
        return _ZWJ
    if cp < 0x0300:
        # Latin-1 / 拉丁扩展 / IPA 扩展 / 间隔修饰字母：除控制符与少数
        # 图画符号（© ®）外均为 Other。
        if cp < 0x20 or 0x7F <= cp < 0xA0 or cp == 0xAD:
            return _CONTROL
        if cp == 0xA9 or cp == 0xAE:
            return _OTHER | _F_EXTPICT
        return _OTHER

    # Hangul：L / V / T / LV / LVT
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return _L
    if (
        0x1160 <= cp <= 0x11A7
        or 0xD7B0 <= cp <= 0xD7C6
        # Unicode 16 把 Kirat Rai 的元音符号归入 GCB=V（不是 Extend）
        or cp == 0x16D63
        or 0x16D67 <= cp <= 0x16D6A
    ):
        return _V
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return _T
    if 0xAC00 <= cp <= 0xD7A3:
        return _LV if (cp - 0xAC00) % 28 == 0 else _LVT

    if 0x1F1E6 <= cp <= 0x1F1FF:
        return _RI
    if cp in _HALFWIDTH_VOICED_MARKS:
        return _OTHER  # 兼容裁剪：不并簇
    if cp in _PREPEND_SET:
        return _PREPEND
    # emoji 肤色修饰符与标签字符：Extend
    if 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F:
        return _EXTEND

    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me") or cp in _EXTRA_EXTEND or _in_ranges(cp, _MARK_BLOCKS):
        flags = 0
        if cp in _DOUBLE_DIACRITICS:
            flags |= _F_DOUBLE
        if cp in _INCB_LINKERS:
            flags |= _F_INCB_LINKER
        return _EXTEND | flags
    if cat == "Mc":
        return _OTHER if cp in _MC_NOT_SPACING else _SPACING
    if cp in _EXTRA_SPACING:
        return _SPACING
    if cat in ("Cc", "Cf", "Zl", "Zp", "Cs"):
        return _CONTROL
    if cat == "Cn" and _in_ranges(cp, _UNASSIGNED_CONTROL):
        return _CONTROL

    flags = 0
    if _in_ranges(cp, _EXTPICT):
        flags |= _F_EXTPICT
    if 0x0900 <= cp <= 0x0D7F and _in_ranges(cp, _INCB_CONSONANTS):
        flags |= _F_INCB_CONSONANT
    return _OTHER | flags


def _is_letter(ch: str) -> bool:
    """连结线并簇的后一基字判定：只并字母（L*），不并标点 / 空白 / 数字。"""
    return unicodedata.category(ch).startswith("L")


# ──────────────────────────────────────────────
# 边界判定
# ──────────────────────────────────────────────


def _needs_segmentation(text: str) -> bool:
    """快路径判定：全部码点 < U+0300 且不含 CR 时，每个码点自成一簇。"""
    for ch in text:
        if ch >= "\u0300" or ch == "\r":
            return True
    return False


def grapheme_boundaries(text: str) -> List[int]:
    """返回 ``text`` 中每个字素簇的起始偏移（码点下标），外加末尾 ``len(text)``。

    例：``"ɛ̃l"`` → ``[0, 2, 3]``。空串返回 ``[0]``。
    """
    n = len(text)
    if n == 0:
        return [0]
    if not _needs_segmentation(text):
        return list(range(n + 1))

    bounds = [0]
    prev = _classify(text[0])
    prev_cls = prev & _CLASS_MASK
    ri_run = 1 if prev_cls == _RI else 0
    # GB11：当前簇是否形如 ExtPict Extend*（等待 ZWJ）
    pict_seq = bool(prev & _F_EXTPICT)
    after_pict_zwj = False
    # GB9c：当前簇是否形如 Consonant (Extend|Linker)*，以及其中是否已见 Linker
    incb_seq = bool(prev & _F_INCB_CONSONANT)
    incb_linker = False
    # 连结线裁剪：当前簇尾部的附加符串里是否含双基字附加符
    pending_double = False

    for i in range(1, n):
        ch = text[i]
        cur = _classify(ch)
        cur_cls = cur & _CLASS_MASK
        brk = True

        if prev_cls == _CR and cur_cls == _LF:
            brk = False  # GB3
        elif prev_cls in (_CR, _LF, _CONTROL) or cur_cls in (_CR, _LF, _CONTROL):
            brk = True  # GB4 / GB5
        elif prev_cls == _L and cur_cls in (_L, _V, _LV, _LVT):
            brk = False  # GB6
        elif prev_cls in (_LV, _V) and cur_cls in (_V, _T):
            brk = False  # GB7
        elif prev_cls in (_LVT, _T) and cur_cls == _T:
            brk = False  # GB8
        elif cur_cls in (_EXTEND, _ZWJ, _SPACING):
            brk = False  # GB9 / GB9a
        elif prev_cls == _PREPEND:
            brk = False  # GB9b
        elif incb_seq and incb_linker and (cur & _F_INCB_CONSONANT):
            brk = False  # GB9c
        elif after_pict_zwj and (cur & _F_EXTPICT):
            brk = False  # GB11
        elif prev_cls == _RI and cur_cls == _RI and ri_run % 2 == 1:
            brk = False  # GB12 / GB13
        elif pending_double and _is_letter(ch):
            brk = False  # 裁剪：连结线把后一字母并入同一簇

        if brk:
            bounds.append(i)

        # ── 更新状态 ──
        if cur_cls == _RI:
            ri_run = ri_run + 1 if prev_cls == _RI and not brk else 1
        else:
            ri_run = 0

        if cur & _F_EXTPICT:
            # GB11 的 ExtPict 起点：新簇，或紧跟 ZWJ 并入同一簇的后续 emoji
            pict_seq = True
            after_pict_zwj = False
        elif cur_cls == _EXTEND:
            after_pict_zwj = False
        elif cur_cls == _ZWJ:
            after_pict_zwj = pict_seq
            pict_seq = False
        else:
            pict_seq = False
            after_pict_zwj = False

        if cur & _F_INCB_CONSONANT:
            incb_seq = True
            incb_linker = False
        elif incb_seq and cur_cls in (_EXTEND, _ZWJ):
            if cur & _F_INCB_LINKER:
                incb_linker = True
        else:
            incb_seq = False
            incb_linker = False

        if cur_cls == _EXTEND:
            if cur & _F_DOUBLE:
                pending_double = True
            # 其余附加符保持 pending_double 不变（连结线后可再叠附加符）
        elif cur_cls == _ZWJ:
            pass
        else:
            pending_double = False

        prev = cur
        prev_cls = cur_cls

    bounds.append(n)
    return bounds


def grapheme_spans(text: str) -> List[Tuple[int, int]]:
    """返回每个字素簇的 ``(start, end)`` 码点偏移（左闭右开）。"""
    bounds = grapheme_boundaries(text)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def split_graphemes(text: str) -> List[str]:
    """把文本切分为字素簇列表（``"".join(result) == text``）。

    Example:
        >>> split_graphemes("ˈtʰɛ̃ːn̩")
        ['ˈ', 't', 'ʰ', 'ɛ̃', 'ː', 'n̩']
        >>> split_graphemes("t͡ʃ")
        ['t͡ʃ']
        >>> split_graphemes("赤い花")
        ['赤', 'い', '花']
    """
    if not text:
        return []
    if not _needs_segmentation(text):
        return list(text)
    bounds = grapheme_boundaries(text)
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]


def iter_graphemes(text: str) -> Iterator[str]:
    """逐个产出字素簇。"""
    return iter(split_graphemes(text))


def grapheme_count(text: str) -> int:
    """字素簇个数（用户感知的字符数）。"""
    if not text:
        return 0
    if not _needs_segmentation(text):
        return len(text)
    return len(grapheme_boundaries(text)) - 1


def next_grapheme_end(text: str, start: int) -> int:
    """返回从 ``start`` 处开始的那个字素簇的结束偏移（不含）。

    供逐字符扫描的解析器使用：把 ``text[i]`` 换成
    ``text[i:next_grapheme_end(text, i)]`` 即可整簇消费。``start`` 视为簇
    起点（不回看左侧上下文）；越界返回 ``len(text)``。
    """
    n = len(text)
    if start >= n:
        return n
    if start < 0:
        start = 0
    ch = text[start]
    if ch < "\u0300" and ch != "\r":
        # 首码点是普通字符：只有后随附加符 / 连结线时才需要完整判定
        nxt = start + 1
        if nxt >= n or (text[nxt] < "\u0300" and text[nxt] != "\n"):
            return nxt
    # 向后截取一段足够长的窗口做完整判定，避免对整行重复 O(n) 切分
    window_end = min(n, start + 64)
    while True:
        bounds = grapheme_boundaries(text[start:window_end])
        if len(bounds) > 2 or window_end >= n:
            return start + bounds[1]
        window_end = min(n, window_end + 256)


def is_single_grapheme(text: str) -> bool:
    """``text`` 是否恰好是一个字素簇。"""
    return bool(text) and grapheme_count(text) == 1


def is_double_diacritic(ch: str) -> bool:
    """是否为双基字附加符（连结线 ``͡`` ``͜`` 等）。"""
    return len(ch) == 1 and ord(ch) in _DOUBLE_DIACRITICS


def is_grapheme_extender(ch: str) -> bool:
    """单个码点是否为「依附前一基字」的延伸码点。

    含组合附加符（Mn / Me）、依附型元音符号（Mc）、ZWJ、变体选择符、
    emoji 肤色修饰符等——它们不能独立成字，孤立出现时没有可排版的基字。
    半角浊点（U+FF9E / U+FF9F）按兼容裁剪不算。
    """
    if len(ch) != 1:
        return False
    return (_classify(ch) & _CLASS_MASK) in (_EXTEND, _ZWJ, _SPACING)


def starts_with_extender(text: str) -> bool:
    """文本是否以延伸码点开头（即它无法作为簇的起点独立存在）。"""
    return bool(text) and is_grapheme_extender(text[0])


def is_dependent_text(text: str) -> bool:
    """文本是否 **全部** 由延伸码点组成（如孤立的 ``"̃"``、``"️"``）。

    这类文本只可能是按码点切分后的残段：没有基字，Qt 的墨水矩形为空
    哨兵值，绘制时也没有可锚定的位置。
    """
    return bool(text) and all(is_grapheme_extender(ch) for ch in text)


def grapheme_base(text: str) -> str:
    """返回簇的「基字」：第一个非延伸码点；全为延伸码点时返回首码点。

    用于按字符类型分类（``ɛ̃`` → ``ɛ``、``t͡ʃ`` → ``t``、``が`` 的分解形
    → ``か``）。先做 NFC 合成：分解形假名（``か`` + U+3099）合成后是单码点
    ``が``，分类与读音才正确。
    """
    if not text:
        return ""
    if len(text) == 1:
        return text
    composed = unicodedata.normalize("NFC", text)
    if len(composed) == 1:
        return composed
    for ch in composed:
        if not is_grapheme_extender(ch):
            return ch
    return composed[0]


def truncate_graphemes(text: str, max_count: int) -> str:
    """按字素簇截断到至多 ``max_count`` 个字符（不会把附加符与基字截开）。"""
    if max_count <= 0:
        return ""
    if not _needs_segmentation(text):
        return text[:max_count]
    bounds = grapheme_boundaries(text)
    if len(bounds) - 1 <= max_count:
        return text
    return text[: bounds[max_count]]


def chars_to_offsets(chars: Sequence[str]) -> List[int]:
    """各字符串在拼接文本中的起始偏移，外加末尾总长。

    ``["ɛ̃", "l"]`` → ``[0, 2, 3]``。用于「文本偏移 ↔ 字符下标」互转：
    ``Character.char`` 不再保证是单码点后，``sentence.text`` 的偏移不能
    直接当 ``sentence.characters`` 的下标用。
    """
    offsets = [0]
    total = 0
    for s in chars:
        total += len(s)
        offsets.append(total)
    return offsets


def offset_to_index(offsets: Sequence[int], offset: int) -> int:
    """把文本偏移映射到所在字符的下标（``offsets`` 来自 :func:`chars_to_offsets`）。

    偏移落在某字符内部时返回该字符下标；等于总长时返回字符数。
    """
    if offset <= 0:
        return 0
    last = len(offsets) - 1
    if offset >= offsets[last]:
        return last
    return bisect_right(offsets, offset) - 1
