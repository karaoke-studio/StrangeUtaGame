"""AI 打轴发音计划数据结构。

PronunciationPlan 是 forced alignment 的领域输入：把 Project 中每个可打轴的
checkpoint 展开为一个 PronunciationUnit，记录其原始文字、既有读音、读音来源、
脚本类别和反向映射坐标（行索引、字符索引、checkpoint 索引）。

设计约束（见主仓库 docs/AI自动打轴需求与实施计划.md §4）：

- 项目已有标注（RubyPart / Ruby / 可自读假名）拥有绝对第一优先级；
- 自动注音只能为「完全没有读音的缺口」补足，绝不覆盖或重新切分已有标注；
- 标点、空格等结构单元可以没有读音，但必须保留在 plan 中以维持反向映射；
- annotation_digest 摘要用于执行/应用前检测工程是否已发生变化。
"""

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Collection, Dict, List, Optional, Tuple

from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    CharType,
    get_char_type,
)


class PronunciationSource(Enum):
    """读音来源，严格对应标注优先级（§4.1）。"""

    EXISTING_PART = "existing_part"
    """Character 的 RubyPart 级细粒度标注（最高优先级，含导入/用户确认的读音）。"""

    EXISTING_RUBY = "existing_ruby"
    """整段 Ruby 已知但无法按 checkpoint 对齐时的字符级来源。

    预留值：当前收集路径中 RubyPart 与 checkpoint 不匹配的 unit 保持
    reading=None（缺口语义，阻止执行），不虚构整段读音；保留该枚举值以
    维持与计划文档 §4.3 的来源枚举稳定一致。
    """

    EXISTING_CHARACTER = "existing_character"
    """字符自身即是读音（平假名/片假名/促音/长音的自注音，或已标注的标点读音）。"""

    GENERATED = "generated"
    """由 SUG 自动注音能力为缺口生成的读音（仅存在于执行快照，应用前不写回工程）。"""


class ScriptKind(Enum):
    """脚本类别（决定后续 token 化路径；语言路由在生成阶段完成）。"""

    KANA = "kana"
    """平假名/片假名/促音/长音 —— 日语读音域。"""

    KANJI = "kanji"
    """CJK 表意文字（日文汉字与中文汉字共用码位，语言由生成路径决定）。"""

    LATIN = "latin"
    """拉丁字母（英语等，MMS-Latn 对齐器的原生脚本）。"""

    HANGUL = "hangul"
    """韩文谚文音节（表音文字，字符自身即读音，AI 打轴前转罗马字表音）。"""

    NUMBER = "number"
    """阿拉伯数字（复用 SUG 数字→漢数字→日语读音能力）。"""

    PUNCTUATION = "punctuation"
    """标点符号（不要求读音，可携带用户标注读音）。"""

    SPACE = "space"
    """空白字符（半角/全角空格，纯结构单元）。"""

    OTHER = "other"
    """其他脚本字符；若需读音而无法生成，将阻止执行。"""


# 需要读音才能参与对齐的脚本类别；缺此类读音即构成「缺口」
_TOKEN_SCRIPTS = frozenset(
    {
        ScriptKind.KANA,
        ScriptKind.KANJI,
        ScriptKind.LATIN,
        ScriptKind.HANGUL,
        ScriptKind.NUMBER,
    }
)

# 默认停顿符（含全角变体）。用户自定义停顿符由调用方在构造 token 序列时
# 通过 pause_chars 显式传入（见 pause_char_variants / get_ruby_pause_char），
# 本模块保持纯数据语义、不读取前端设置。
_DEFAULT_PAUSE_CHARS = frozenset({"^", "＾"})

_SCRIPT_BY_CHAR_TYPE: Dict[CharType, ScriptKind] = {
    CharType.HIRAGANA: ScriptKind.KANA,
    CharType.KATAKANA: ScriptKind.KANA,
    CharType.SOKUON: ScriptKind.KANA,
    CharType.LONG_VOWEL: ScriptKind.KANA,
    CharType.KANJI: ScriptKind.KANJI,
    CharType.ALPHABET: ScriptKind.LATIN,
    CharType.HANGUL: ScriptKind.HANGUL,
    CharType.NUMBER: ScriptKind.NUMBER,
    CharType.MODIFIER: ScriptKind.LATIN,  # IPA 修饰符/附加符依附拉丁字母音段
    CharType.SYMBOL: ScriptKind.PUNCTUATION,
    CharType.SPACE: ScriptKind.SPACE,
    CharType.FULL_SPACE: ScriptKind.SPACE,
    CharType.OTHER: ScriptKind.OTHER,
}


def script_kind_of(char: str) -> ScriptKind:
    """单个字符的脚本类别（无法识别的归入 OTHER）。"""
    try:
        ct = get_char_type(char)
    except ValueError:
        return ScriptKind.OTHER
    return _SCRIPT_BY_CHAR_TYPE.get(ct, ScriptKind.OTHER)


def compute_annotation_digest(project) -> str:
    """计算工程标注摘要（sha256）。

    摘要覆盖对齐所依赖的标注与节奏结构：行数、每字符文本、check_count、
    停顿点标记与全部 RubyPart 文本。刻意不包含时间戳——AI 打轴的目的就是
    覆盖时间戳，时间戳变化不应使快照失效。
    """
    payload: List = []
    for sentence in project.sentences:
        line: List = []
        for ch in sentence.characters:
            line.append(
                [
                    ch.char,
                    ch.check_count,
                    bool(ch.is_sentence_end),
                    [p.text for p in ch.ruby.parts] if ch.ruby else None,
                ]
            )
        payload.append(line)
    serialized = repr(payload).encode("utf-8")
    return sha256(serialized).hexdigest()


@dataclass
class PronunciationUnit:
    """一个可打轴 checkpoint 对应的对齐单元。

    checkpoint_idx 与 Character 的 all_timestamps 域一致：
    普通节奏点为 0..check_count-1；停顿点为虚拟索引 check_count
    （is_sentence_end=True，无读音、不生成 token）。
    """

    line_idx: int
    char_idx: int
    checkpoint_idx: int
    is_sentence_end: bool
    char_text: str
    script: ScriptKind
    reading: Optional[str] = None
    """对齐用读音；None 表示尚未获得（缺口或结构单元）。"""

    source: Optional[PronunciationSource] = None
    """reading 的来源；None 表示尚未获得。"""

    display_text: Optional[str] = None
    """显示用 RubyPart 原文（与 reading 区分：停顿符等占位 part 会保留原文）。"""

    @property
    def location(self) -> Tuple[int, int, int]:
        """反向映射坐标 (line_idx, char_idx, checkpoint_idx)。"""
        return (self.line_idx, self.char_idx, self.checkpoint_idx)

    @property
    def expects_token(self) -> bool:
        """该单元是否需要读音才能参与对齐（缺口语义仅对此类成立）。"""
        return (not self.is_sentence_end) and self.script in _TOKEN_SCRIPTS

    @property
    def is_pending(self) -> bool:
        """是否为缺口：需要读音但尚未获得（填充后仍为 True 表示无法补足）。"""
        return self.expects_token and self.reading is None

    def has_model_token(
        self, *, pause_chars: Collection[str] = _DEFAULT_PAUSE_CHARS
    ) -> bool:
        """是否为对齐器生成模型 token。

        停顿符占位 part、空白读音与停顿点不生成 token，但单元仍保留
        在 plan 中以维持结构映射。pause_chars 由调用方按用户停顿符配置显式
        传入（默认 '^' 的全半角变体）。
        """
        if self.is_sentence_end or not self.reading:
            return False
        if not self.reading.strip():
            return False
        return self.reading not in pause_chars


@dataclass
class PronunciationPlan:
    """整个工程的发音计划（forced alignment 的执行快照输入）。"""

    units: List[PronunciationUnit] = field(default_factory=list)
    annotation_digest: str = ""
    filled_count: int = 0
    """由自动注音补足读音的单元数（GENERATED 来源计数）。"""

    generation_errors: List[str] = field(default_factory=list)
    """生成阶段的中文错误记录（分析器不可用、字符无法注音等），供执行前阻断展示。"""

    chinese_mode: bool = False
    """工程级中文模式（resolver 路由结论）。对齐 token 化据此把汉字的
    拉丁读音按拼音→表音转写（FA-Kara 口径）；不进 annotation_digest。"""

    @property
    def pending_units(self) -> List[PronunciationUnit]:
        """缺口单元（含生成后仍无法补足的）。"""
        return [u for u in self.units if u.is_pending]

    @property
    def is_complete(self) -> bool:
        """所有需要读音的单元均已获得读音。"""
        return not any(u.is_pending for u in self.units)

    def units_for_line(self, line_idx: int) -> List[PronunciationUnit]:
        return [u for u in self.units if u.line_idx == line_idx]

    def unit_at(
        self, line_idx: int, char_idx: int, checkpoint_idx: int
    ) -> Optional[PronunciationUnit]:
        """按反向映射坐标取单元。"""
        for u in self.units:
            if (
                u.line_idx == line_idx
                and u.char_idx == char_idx
                and u.checkpoint_idx == checkpoint_idx
            ):
                return u
        return None


__all__ = [
    "PronunciationSource",
    "ScriptKind",
    "PronunciationUnit",
    "PronunciationPlan",
    "compute_annotation_digest",
    "script_kind_of",
]
