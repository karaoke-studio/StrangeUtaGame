"""英语单词 → 片假名注音查询。

使用 e2k.txt（英语到片假名词典，CMU-based，BSD-3-Clause）做单词级查询。
优先级：用户自定义词典 → 本模块 → 库函数（Sudachi/pykakasi）。
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class EnglishRubyLookup:
    """英语单词 → 片假名查询。

    数据来源：e2k.txt（TSV 格式：english_word\\tカタカナ），小写键。
    """

    _instance: Optional["EnglishRubyLookup"] = None

    def __init__(self):
        self._dict: Dict[str, str] = {}
        self._loaded = False

    @classmethod
    def instance(cls) -> "EnglishRubyLookup":
        """单例模式，避免重复加载大词典。"""
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._load()
        return cls._instance

    def _load(self) -> None:
        """从 e2k.txt 加载词典（打包环境与开发环境均可用）。"""
        if self._loaded:
            return
        path = self._resolve_path()
        if path is None or not path.exists():
            self._loaded = True
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if not line or "\t" not in line:
                        continue
                    word, reading = line.split("\t", 1)
                    word = word.strip().lower()
                    reading = reading.strip()
                    if word and reading:
                        # 首次出现的读音优先
                        if word not in self._dict:
                            self._dict[word] = reading
        except Exception as e:
            print(f"加载 e2k 英语词典失败: {e}")
        self._loaded = True

    @staticmethod
    def _resolve_path() -> Optional[Path]:
        """解析 e2k.txt 路径（兼容 PyInstaller 打包环境）。"""
        base = getattr(sys, "_MEIPASS", None)
        if base:
            p = Path(base) / "strange_uta_game" / "config" / "e2k.txt"
            if p.exists():
                return p
        # 开发环境 (parsers/ → infrastructure/ → backend/ → strange_uta_game/)
        dev_path = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "config"
            / "e2k.txt"
        )
        if dev_path.exists():
            return dev_path
        return None

    def lookup(self, word: str) -> Optional[str]:
        """查询单词的片假名读音，不存在返回 None。

        #11：先规范化弯引号/全角撇号为 ASCII，避免 `what\u2019s` 之类的输入
        找不到 `what's` 条目。

        批 17：正则会把末尾的 `.` 剪掉（`a.m.` → `a.m`），但词典 key 以
        原始形式 `a.m.` 存储。miss 时自动补尾点重试，命中 `a.m./p.m./b.c./
        a.d./c.o.d.` 等约定俗成含尾点的缩略词条。
        """
        if not word:
            return None
        key = normalize_apostrophes(word).lower()
        hit = self._dict.get(key)
        if hit is not None:
            return hit
        # 尾点重试：仅当 key 含内部点（形如 a.m、b.c、c.o.d）时才尝试
        if "." in key:
            return self._dict.get(key + ".")
        return None

    def has(self) -> bool:
        """词典是否加载成功且非空。"""
        return bool(self._dict)


# 英文单词匹配（连续英文字母，允许内部 ' 和 .）
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['.][A-Za-z]+)*")

# #11：常见 Unicode 弯引号/全角撇号 → ASCII 撇号映射
# 用户从富文本/网页粘贴歌词时常携带这些字符，正则和词典都只识别 ASCII。
_APOSTROPHE_TRANSLATIONS = str.maketrans(
    {
        "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK (’)
        "\u2018": "'",  # LEFT SINGLE QUOTATION MARK (‘)
        "\u02bc": "'",  # MODIFIER LETTER APOSTROPHE (ʼ)
        "\uff07": "'",  # FULLWIDTH APOSTROPHE (＇)
        "\u0060": "'",  # GRAVE ACCENT (`) 常被误用为 apostrophe
    }
)


def normalize_apostrophes(text: str) -> str:
    """将常见弯引号/全角撇号统一为 ASCII 撇号，便于分词与词典查询。"""
    return text.translate(_APOSTROPHE_TRANSLATIONS)


def get_syllable_start_offsets(word: str) -> Set[int]:
    """返回 word 内各音节首字母的偏移集合（始终含 0）。

    含 IPA 特有字符的词按 **IPA 音节规则**切分（``ipa_syllable_onset_offsets``）；
    其余用 CMU 发音词典与离线 G2P，按音素分音节后映射回原词字母位置。
    无可靠发音/字母对应时保留整词；不把排版断字当作发音音节。
    底层任何异常（词典 IO、模型损坏等）同样退化为整词——本入口被歌词
    导入与自动打轴调用，不允许向用户数据路径抛异常。
    """
    try:
        from .ipa import ipa_syllable_onset_offsets, is_ipa_word

        if is_ipa_word(word):
            return ipa_syllable_onset_offsets(word)
    except Exception:
        pass
    try:
        from .english_syllables import analyze_english_word

        return set(analyze_english_word(word).offsets)
    except Exception:
        return {0}


# 各类逗号：英文逗号、中文全角逗号、日文逗号
_TRAILING_COMMA_CHARS = frozenset({',', '\uff0c', '\u3001'})


def _extend_past_trailing_commas(text: str, end: int) -> int:
    """将 end 索引向后扩展到跳过所有尾部逗号。"""
    while end < len(text) and text[end] in _TRAILING_COMMA_CHARS:
        end += 1
    return end


def find_english_words(text: str) -> List[Tuple[int, int, str]]:
    """在文本中定位所有英文单词（**含 IPA 词**）。

    #11：在分词前先把 curly/全角撇号规范化为 ASCII，保证
    `what's`、`don't` 等缩写能被视为一个完整单词（返回的 start/end 索引
    与 normalize 后的字符串对齐——由于 translate 是 1:1 字符映射，
    原字符串的位置也等同有效）。

    IPA 词（含 IPA 特有字符的最大连续语音段，见 ``ipa.find_ipa_words``）
    作为整词加入结果；IPA 词内部的 ASCII 字母片段（如 ``wɜːld`` 里的
    ``ld``）不再单独视为英文词——否则会被 e2k 注上字母名假名读音、
    并在词中段自动打停顿点。

    Returns:
        (start_idx, end_idx, word) 元组列表（end 为 exclusive）。
        需要区分英文词 / IPA 词时用 ``ipa.is_ipa_word(word)`` 判断。
    """
    normalized = normalize_apostrophes(text)
    results = []
    for m in _ENGLISH_WORD_RE.finditer(normalized):
        results.append((m.start(), m.end(), m.group()))

    try:
        from .ipa import find_ipa_words

        ipa_words = find_ipa_words(text)
    except Exception:
        ipa_words = []
    if not ipa_words:
        return results

    filtered = [
        (s, e, w)
        for s, e, w in results
        if not any(s >= a and e <= b for a, b, _ in ipa_words)
    ]
    filtered.extend(ipa_words)
    filtered.sort(key=lambda item: (item[0], item[1]))
    return filtered
