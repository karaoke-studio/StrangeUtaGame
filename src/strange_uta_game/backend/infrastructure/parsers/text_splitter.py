"""文本拆分器 - 将歌词文本拆分为字符列表。

支持日文（汉字、假名、长音、促音）、韩文（谚文音节）、英文以及**国际音标**
（IPA）的拆分规则。拆分单位是**字素簇**（见 ``domain.graphemes``）——
``ɛ̃`` ``n̩`` ``t͡ʃ`` 这样的多码点音段不会被拆散。

``get_char_type`` 对多码点簇按**基字**分类（``ɛ̃`` → 英文字母、``が`` 的
分解形 → 平假名），不再要求单码点输入。
"""

import re
import unicodedata
from abc import ABC, abstractmethod
from typing import List, Tuple
from enum import Enum, auto

from strange_uta_game.backend.domain.graphemes import (
    grapheme_base,
    split_graphemes,
)


class CharType(Enum):
    """字符类型"""

    KANJI = auto()  # 汉字
    HIRAGANA = auto()  # 平假名
    KATAKANA = auto()  # 片假名
    LONG_VOWEL = auto()  # 长音「ー」
    SOKUON = auto()  # 促音「っ/ッ」
    ALPHABET = auto()  # 英文字母（含 IPA 音标字母）
    HANGUL = auto()  # 韩文谚文音节（U+AC00–U+D7A3 预组音节块）
    NUMBER = auto()  # 数字
    MODIFIER = auto()  # 修饰符：组合附加符 / IPA 修饰字母与超音段（ˈ ˌ ː ʰ …）
    SYMBOL = auto()  # 符号
    SPACE = auto()  # 半角空格（U+0020 等 ASCII 空白）
    FULL_SPACE = auto()  # 全角空格（U+3000，表意文字空格）
    OTHER = auto()  # 其他


# 修饰符区间：**仅限 IPA 语义无歧义的字符**（审查反馈 #2 收紧）——
#   02B0–02B8（ʰ ʱ ʲ ʴ ʵ ʶ ʷ ʸ）、02C0/02C1（喉塞化 ˀ ˁ）、
#   02C8 ˈ、02CC ˌ、02D0/02D1 ː ˑ、02DE ˞、
#   02E0–02EB（ˠ ˡ ˢ ˣ ˤ + 声调条）、02EC ˬ、02ED ˭、
#   10780–107BF（Latin Extended-F 的 IPA 上标字母）。
# 它们依附相邻字母（IPA 修饰符/超音段），永不自动分配节奏点。
# 刻意**不含**：正字法撇号类 ʻ ʼ ʽ ʾ ʿ（02BB–02BF）与 primes（02B9/02BA）
# ——夏威夷/土耳其/西里尔转写里是实义字符；上标 ² ³ 1 ⁿ（00B2/B3/B9、2070+）
# 与语音扩展小型字母（1D2C+）——常规歌词里的序号/数学上标按历史行为保留
# NUMBER/ALPHABET 分类与节奏点（IPA 词内的依附由 ipa.py 的
# `split_phonetic_units` 按上下文处理，与全局分类无关）。
_MODIFIER_RANGES = (
    (0x02B0, 0x02B8),
    (0x02C0, 0x02C1),
    (0x02C8, 0x02C8),
    (0x02CC, 0x02CC),
    (0x02D0, 0x02D1),
    (0x02DE, 0x02DE),
    (0x02E0, 0x02EB),
    (0x02EC, 0x02EC),
    (0x02ED, 0x02ED),
    (0x10780, 0x107BF),  # Latin Extended-F：IPA 专用修饰字母
)


def _in_modifier_range(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _MODIFIER_RANGES)


def _classify_single(ch: str) -> CharType:
    """按单个码点分类（历史逻辑，未改动）。"""
    # 长音（含半角片假名长音 ｰ U+FF70）
    if ch in ("ー", "ｰ", "－", "～", "〜"):
        return CharType.LONG_VOWEL

    # 促音
    if ch in ("っ", "ッ"):
        return CharType.SOKUON

    # 平假名
    if "\u3040" <= ch <= "\u309f":
        return CharType.HIRAGANA

    # 片假名
    # 片假名块内的标点/分隔符（中点「・」U+30FB、双连字符「゠」U+30A0）归为符号：
    # 它们落在片假名 Unicode 块内，但并不表音，不应被当作可注音/可补轴的假名。
    # 须在片假名块判断之前拦截。注意：长音「ー」U+30FC 已在上方特判为长音；
    # 片假名迭字「ヽヾ」表音，保留为片假名。
    if ch in ("\u30fb", "\u30a0"):
        return CharType.SYMBOL

    if "\u30a0" <= ch <= "\u30ff":
        return CharType.KATAKANA

    # CJK 统一表意文字（汉字）+ 迭字 mark
    if (
        "\u4e00" <= ch <= "\u9fff"
        or "\u3400" <= ch <= "\u4dbf"
        or "\uf900" <= ch <= "\ufaff"
        or ch == "\u3005"  # 々 IDEOGRAPHIC ITERATION MARK
    ):
        return CharType.KANJI

    # 韩文谚文音节块（U+AC00–U+D7A3）。谚文是表音文字、每音节一个
    # 节拍单元，必须在 isalpha() 兜底之前拦截——否则会被误归为英文字母
    # （isalpha() 对谚文返回 True），节奏点语义与按类型删除都会错挂。
    # 兼容 jamo（U+3130–U+318F 等）不在此列：歌词中几乎只出现预组音节。
    if "\uac00" <= ch <= "\ud7a3":
        return CharType.HANGUL

    # 半角片假名字母（U+FF66–U+FF9F，ｦｧｨ…ﾝﾞﾟ）。isalpha() 对它们返回
    # True，不拦截会被下方兜底误归 ALPHABET → 语言误判/romaji 漏判（E11，
    # 昭和系歌词常见）。注意 U+FF61–FF65（｡｢｣､･）是半角标点，仍交给
    # 下方 Unicode 分类兜底归 SYMBOL（与全角中点 U+30FB 同语义）；
    # 半角长音 ｰ U+FF70 已在上方特判为长音。
    if "\uff66" <= ch <= "\uff9f":
        return CharType.KATAKANA

    # 修饰符：IPA 修饰字母 / 超音段符号（ˈ ˌ ː ʰ ˥ ⁿ …）与组合附加符。
    # 间隔修饰字母 isalpha() 为 True，须在 ALPHABET 兜底之前拦截。
    if _in_modifier_range(ord(ch)):
        return CharType.MODIFIER

    # 英文字母（含 IPA 音标字母 ɛ ʃ ŋ θ …）
    if ch.isalpha():
        return CharType.ALPHABET

    # 数字
    if ch.isdigit():
        return CharType.NUMBER

    # 全角空格（U+3000 IDEOGRAPHIC SPACE）与半角空格分开
    if ch == "\u3000":
        return CharType.FULL_SPACE

    # 半角空格（U+0020 等 ASCII 空白）
    if ch.isspace():
        return CharType.SPACE

    # 组合附加符（Mn/Me/Mc）：依附基字，不构成独立音节
    if unicodedata.category(ch) in ("Mn", "Me", "Mc"):
        return CharType.MODIFIER

    # 符号：用 Unicode 分类兜底——标点（P*）与各类符号（数学 Sm、货币 Sc、
    # 修饰 Sk、其他 So）统一归为符号，含常见标点、+ - * & ^ = ~ | 等运算符，
    # 以及 ♪ ♥ ★ 等装饰符号与絵文字。注音是否删除与是否生成节奏点无关，故
    # 装饰符号也算符号。假名/汉字/字母/数字/长音/促音/空格已在上方优先返回。
    if unicodedata.category(ch)[0] in ("P", "S"):
        return CharType.SYMBOL

    return CharType.OTHER


def get_char_type(char: str) -> CharType:
    """获取字符类型。

    Args:
        char: 单个字符（字素簇，可以是多码点，如 ``"ɛ̃"`` ``"t͡ʃ"``；
            也容忍普通字符串——按其中第一个字素簇的基字分类）

    Returns:
        字符类型

    Note:
        多码点输入按**基字**分类：``"ɛ̃"`` → ALPHABET、``"か"+U+3099`` →
        HIRAGANA、``"t͡ʃ"`` → ALPHABET。纯附加符串（如孤立的 ``"̃"``）
        归 MODIFIER。空串仍抛 ``ValueError``（保持历史行为）。
    """
    if not char:
        raise ValueError(f"必须是单个字符: {char}")
    if len(char) == 1:
        return _classify_single(char)
    # 多码点：取基字（先 NFC 合成，分解形假名可得到正确分类）
    base = grapheme_base(char)
    if len(base) == 1:
        return _classify_single(base)
    # 基字本身仍是多码点（罕见：多个字素簇粘在一个 Character 里），
    # 退回第一个字素簇的基字
    first = split_graphemes(base)[0] if base else char
    base2 = grapheme_base(first)
    return _classify_single(base2)


class TextSplitter(ABC):
    """文本拆分器抽象基类"""

    @abstractmethod
    def split(self, text: str) -> List[str]:
        """将文本拆分为字符列表

        Args:
            text: 输入文本

        Returns:
            字符列表
        """
        pass


class JapaneseSplitter(TextSplitter):
    """日文文本拆分器

    拆分规则：
    - 汉字：单独拆分
    - 假名：单独拆分
    - 长音「ー」：可配置是否独立拆分
    - 促音「っ/ッ」：可配置是否独立拆分
    - 符号：单独拆分
    - 连续空格：合并为单个空格

    ``by_codepoint=True`` 时按码点拆分（供自动注音的码点视图等按码点
    索引的既有管线使用）；默认按字素簇。
    """

    def __init__(
        self,
        split_long_vowel: bool = True,
        split_sokuon: bool = True,
        merge_spaces: bool = True,
        by_codepoint: bool = False,
    ):
        """
        Args:
            split_long_vowel: 是否将长音「ー」作为独立字符拆分
            split_sokuon: 是否将促音「っ/ッ」作为独立字符拆分
            merge_spaces: 是否合并连续空格
            by_codepoint: 按码点而非字素簇拆分
        """
        self.split_long_vowel = split_long_vowel
        self.split_sokuon = split_sokuon
        self.merge_spaces = merge_spaces
        self.by_codepoint = by_codepoint

    def split(self, text: str) -> List[str]:
        """拆分日文文本（按字素簇，组合附加符不与基字拆开）"""
        if not text:
            return []

        chars = []
        prev_was_space = False
        saw_full_space_in_seq = False

        units = list(text) if self.by_codepoint else split_graphemes(text)
        for char in units:
            char_type = get_char_type(char)

            # 处理空格（半角/全角）
            if char_type in (CharType.SPACE, CharType.FULL_SPACE):
                if self.merge_spaces:
                    if char_type == CharType.FULL_SPACE:
                        saw_full_space_in_seq = True
                    if not prev_was_space:
                        chars.append("\u3000" if saw_full_space_in_seq else " ")
                        prev_was_space = True
                    elif saw_full_space_in_seq and chars and chars[-1] == " ":
                        chars[-1] = "\u3000"
                else:
                    chars.append(char)
                continue

            prev_was_space = False
            saw_full_space_in_seq = False

            # 根据配置决定是否拆分长音
            if char_type == CharType.LONG_VOWEL and not self.split_long_vowel:
                # 将长音合并到前一个字符
                if chars:
                    # 这里只是标记，实际合并逻辑由调用者处理
                    chars.append(char)
                else:
                    chars.append(char)
                continue

            # 根据配置决定是否拆分促音
            if char_type == CharType.SOKUON and not self.split_sokuon:
                # 将促音合并到前一个字符
                if chars:
                    chars.append(char)
                else:
                    chars.append(char)
                continue

            # 其他字符直接添加
            chars.append(char)

        return chars


class EnglishSplitter(TextSplitter):
    """英文文本拆分器

    拆分规则：
    - 字母：单独拆分
    - 数字：单独拆分
    - 标点符号：单独拆分
    - 单词边界：保留空格用于识别
    - 连续空格：合并为单个空格

    ``by_codepoint=True`` 时按码点拆分（语义见 JapaneseSplitter）。
    """

    def __init__(self, merge_spaces: bool = True, by_codepoint: bool = False):
        """
        Args:
            merge_spaces: 是否合并连续空格
            by_codepoint: 按码点而非字素簇拆分
        """
        self.merge_spaces = merge_spaces
        self.by_codepoint = by_codepoint

    def split(self, text: str) -> List[str]:
        """拆分英文文本（按字素簇，组合附加符不与基字拆开）"""
        if not text:
            return []

        chars = []
        prev_was_space = False
        saw_full_space_in_seq = False

        units = list(text) if self.by_codepoint else split_graphemes(text)
        for char in units:
            if char.isspace():
                is_full = char == "\u3000"
                if self.merge_spaces:
                    if is_full:
                        saw_full_space_in_seq = True
                    if not prev_was_space:
                        chars.append("\u3000" if saw_full_space_in_seq else " ")
                        prev_was_space = True
                    elif saw_full_space_in_seq and chars and chars[-1] == " ":
                        chars[-1] = "\u3000"
                else:
                    chars.append(char)
                continue

            prev_was_space = False
            saw_full_space_in_seq = False
            chars.append(char)

        return chars


class AutoSplitter(TextSplitter):
    """自动文本拆分器

    根据文本内容自动选择合适的拆分策略。
    """

    def __init__(
        self,
        japanese_splitter: JapaneseSplitter = None,
        english_splitter: EnglishSplitter = None,
        by_codepoint: bool = False,
    ):
        self.japanese_splitter = japanese_splitter or JapaneseSplitter(
            by_codepoint=by_codepoint
        )
        self.english_splitter = english_splitter or EnglishSplitter(
            by_codepoint=by_codepoint
        )
        self.by_codepoint = by_codepoint

    def detect_language(self, text: str) -> str:
        """检测文本主要语言

        Args:
            text: 输入文本

        Returns:
            语言代码: "ja" (日文), "en" (英文), "mixed" (混合), "other" (其他)
        """
        if not text:
            return "other"

        ja_chars = 0
        en_chars = 0
        other_chars = 0

        for char in split_graphemes(text):
            char_type = get_char_type(char)

            if char_type in (
                CharType.KANJI,
                CharType.HIRAGANA,
                CharType.KATAKANA,
                CharType.LONG_VOWEL,
                CharType.SOKUON,
            ):
                ja_chars += 1
            elif char_type in (CharType.ALPHABET, CharType.HANGUL, CharType.MODIFIER):
                # 谚文按 en 桶计数：纯韩文行保持 en 检测 → EnglishSplitter
                # 的逐字 + 空格合并行为（否则退化为 other 的裸 list(text)，
                # 连续空格不再合并）。IPA 修饰符同理（依附拉丁字母）。
                en_chars += 1
            elif not char.isspace():
                other_chars += 1

        total = ja_chars + en_chars + other_chars
        if total == 0:
            return "other"

        ja_ratio = ja_chars / total
        en_ratio = en_chars / total

        if ja_ratio > 0.5:
            return "ja"
        elif en_ratio > 0.5:
            return "en"
        elif ja_ratio > 0.2 or en_ratio > 0.2:
            return "mixed"
        else:
            return "other"

    def split(self, text: str) -> List[str]:
        """自动拆分文本"""
        lang = self.detect_language(text)

        if lang == "ja":
            return self.japanese_splitter.split(text)
        elif lang == "en":
            return self.english_splitter.split(text)
        else:
            # 混合或其他语言，使用通用拆分（字素簇；码点模式退回 list）
            return list(text) if self.by_codepoint else split_graphemes(text)


class SplitConfig:
    """拆分配置

    用于 AutoCheckService 的字符拆分配置。
    """

    def __init__(
        self,
        split_long_vowel: bool = True,
        split_sokuon: bool = True,
        count_sokuon: bool = True,
        count_long_vowel: bool = True,
    ):
        """
        Args:
            split_long_vowel: 是否拆分长音「ー」
            split_sokuon: 是否拆分促音「っ/ッ」
            count_sokuon: 促音是否计入节奏点数量
            count_long_vowel: 长音是否计入节奏点数量
        """
        self.split_long_vowel = split_long_vowel
        self.split_sokuon = split_sokuon
        self.count_sokuon = count_sokuon
        self.count_long_vowel = count_long_vowel


def split_text(
    text: str, config: SplitConfig = None, *, by_codepoint: bool = False
) -> Tuple[List[str], List[int]]:
    """拆分文本并返回字符列表和建议的节奏点数量

    Args:
        text: 输入文本
        config: 拆分配置
        by_codepoint: 按码点而非字素簇拆分（供自动注音的码点视图等
            按码点索引的既有管线使用；默认按字素簇）

    Returns:
        (字符列表, 每个字符的建议节奏点数量)
    """
    if not text:
        return [], []

    config = config or SplitConfig()

    splitter = AutoSplitter(
        japanese_splitter=JapaneseSplitter(
            split_long_vowel=config.split_long_vowel,
            split_sokuon=config.split_sokuon,
            by_codepoint=by_codepoint,
        ),
        by_codepoint=by_codepoint,
    )

    chars = splitter.split(text)
    check_counts = []

    for char in chars:
        char_type = get_char_type(char)

        # 计算节奏点数量
        if char_type == CharType.SOKUON:
            check_counts.append(1 if config.count_sokuon else 0)
        elif char_type == CharType.LONG_VOWEL:
            check_counts.append(1 if config.count_long_vowel else 0)
        elif char_type == CharType.MODIFIER:
            # 修饰符（IPA ˈ ˌ ː ʰ … 与组合附加符）依附相邻字母，不打拍
            check_counts.append(0)
        elif char_type in (CharType.KANJI, CharType.HIRAGANA, CharType.KATAKANA):
            # 假名通常 1 个节奏点，汉字根据注音确定（这里默认 1）
            check_counts.append(1)
        elif char_type in (CharType.ALPHABET, CharType.HANGUL):
            # 英文字母通常 1 个；谚文每音节固定 1 个（表音节拍单元）
            check_counts.append(1)
        elif char_type in (CharType.SPACE, CharType.FULL_SPACE):
            # 半角/全角空格 0 个
            check_counts.append(0)
        else:
            # 其他默认 1 个
            check_counts.append(1)

    return chars, check_counts
