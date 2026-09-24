"""自动检查服务。

分析歌词文本，计算节奏点数量，生成注音。
"""

from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass

from strange_uta_game.backend.domain import (
    Project,
    Sentence,
    Character,
    Ruby,
    RubyPart,
)
from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    split_text,
    SplitConfig,
    get_char_type,
    CharType,
)
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    create_analyzer,
    RubyAnalyzer,
    RubyResult,
    _arabic_to_kanji,
    _arabic_to_kanji_segments,
    _group_reading_for_character,
    is_all_katakana,
    is_english_reading,
)
from strange_uta_game.backend.infrastructure.parsers.inline_format import (
    split_ruby_for_checkpoints,
    split_into_moras,
)
from strange_uta_game.backend.infrastructure.parsers.kanji_reading_split import (
    even_distribute_kana,
)
from strange_uta_game.backend.infrastructure.parsers.english_ruby import (
    EnglishRubyLookup,
    find_english_words,
    get_syllable_start_offsets,
    _TRAILING_COMMA_CHARS,
)
from strange_uta_game.backend.infrastructure.parsers.e2k_engine import (
    EnglishToKanaEngine,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import (
    detect_particle_part_indices,
    is_self_romanizable_kana,
    romanize_sentence_in_place,
)


# 允许自动注音的字符类型白名单（第十批 #5）：
# 英文字符/英文词语、汉字/日汉字、平假名、片假名、阿拉伯数字
_RUBY_ALLOWED_TYPES = {
    CharType.ALPHABET,
    CharType.KANJI,
    CharType.HIRAGANA,
    CharType.KATAKANA,
    CharType.SOKUON,
    CharType.LONG_VOWEL,
    CharType.NUMBER,
}

# 字符类型 → 标志键映射（用于标志过滤器，提取为模块级常量避免循环内重复构造）
# 注意：CharType.SPACE / CharType.FULL_SPACE 不在此表中，空格由 _apply_flags_filter 单独处理
# （需要同时读取 space_after_* 三个子选项，逻辑与其他类型不同）
_TYPE_FLAG_MAP: Dict[CharType, str] = {
    CharType.HIRAGANA: "hiragana",
    CharType.KATAKANA: "katakana",
    CharType.KANJI: "kanji",
    CharType.ALPHABET: "alphabet",
    CharType.NUMBER: "digit",
    CharType.SYMBOL: "symbol",
}

# 小型假名集合（不含促音 っ/ッ，促音由独立的 check_sokuon 标志控制）
_SMALL_KANA_SET = frozenset("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮゕゖ")


# 中文歌词判据用的假名字符集：仅平/片假名 + 小假名，不含 の（中文歌中常见装饰字）、
# 也不含 ーｰ～〜（长音/波浪符号——非真正的假名）。
_KANA_DETECTION_CHARS = frozenset(
    "ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞた"
    "だちぢっつづてでとどなにぬねはばぱひびぴふぶぷへべぺほぼぽ"
    "まみむめもゃやゅゆょよらりるれろゎわゐゑをんゔゕゖ"
    "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタ"
    "ダチヂッツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポ"
    "マミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶ"
)


def is_chinese_lyrics(text: str) -> bool:
    """判断文本是否为「纯中文歌词」（不含任何平/片假名）。

    用于自动注音流程决定是否走中文模式（每字 cc=1、无 ruby、无用户词典、无 LLM）。
    """
    if not text:
        return False
    return not any(c in _KANA_DETECTION_CHARS for c in text)


def _merge_trailing_n_ruby_parts(parts: List[str]) -> List[str]:
    """将非起始位置的「ん/ン」分段并入前一拍。

    用于 check_n 关闭（んン 不单独打节奏点）时收敛字符注音的 ruby 分段：

        ["け", "ん"]       → ["けん"]      # 険[けん]：2 拍 → 1 拍
        ["あ", "ん", "け"] → ["あん", "け"]
        ["ん", "け"]       → ["ん", "け"]   # 起始 ん 无前拍可并，原样保留

    仅对恰好等于「ん」「ン」的整段做合并；其它分段（含 きょ 这类带小假名的拍）
    原样保留。
    """
    merged: List[str] = []
    for p in parts:
        if p in ("ん", "ン") and merged:
            merged[-1] += p
        else:
            merged.append(p)
    return merged


def _has_latin(s: str) -> bool:
    """是否含有 ASCII 英文字母（用于词边界判定）。"""
    return any(c.isascii() and c.isalpha() for c in s)


def _is_word_inner(c: str) -> bool:
    """判断字符是否是英文单词内部字符（字母或撇号）。"""
    return (c.isascii() and c.isalpha()) or c in ("'", "\u2019")


def _split_segmented_english_reading(
    reading: str, expected_parts: int
) -> Optional[List[str]]:
    """解析默认词典风格的片假名英文分段。

    例如 ``com,,bo`` 对应 ``コンボ`` 的三个字符位置。空段表示该片假名
    仍属于同一连词块，但不单独承载英文片段。返回 ``None`` 表示这不是合法的
    英文分段格式；无逗号的 ``computer`` 继续走整词首字承载的兼容路径。
    """
    if expected_parts < 2 or "," not in (reading or ""):
        return None
    parts = [part.strip() for part in reading.split(",")]
    if len(parts) != expected_parts:
        return None
    if not is_english_reading("".join(parts)):
        return None
    return parts


def _parse_dict_reading(reading: str, expected_word: str) -> Optional[
    Tuple[List[List[str]], List[int]]
]:
    """解析用户词典 reading（annotated 行内格式）。

    Args:
        reading: 形如 ``{微笑||ほほ,え}ん`` 的注音文本。
        expected_word: 词条 ``word``，用于校验解析出的原文与 ``word`` 一致。

    Returns:
        ``None`` 解析失败或 raw 与 word 不一致；
        否则返回 ``(per_char_parts, char_block_id)``：

        - ``per_char_parts`` — 每个字符的 RubyPart 文本列表（无 ruby 字符为空列表）；
        - ``char_block_id`` — 每个字符所属的 annotated block 编号，
          块外字符（字面无 ruby 段）一律为 ``-1``；同 block 内字符 id 相同。
          用于设置 ``linked_to_next``：同 block 内相邻字符链接，否则不链接。
    """
    raw_chars: List[str] = []
    per_char_parts: List[List[str]] = []
    char_block_id: List[int] = []
    block_seq = 0

    i = 0
    n = len(reading)
    while i < n:
        ch = reading[i]
        if ch == "{":
            close = reading.find("}", i)
            if close == -1:
                # 未配对 → 整体判定失败
                return None
            content = reading[i + 1 : close]
            if "||" in content:
                text_part, readings_part = content.split("||", 1)
                per_char_readings = readings_part.split(",")
                this_block = block_seq
                block_seq += 1
                for j, c in enumerate(text_part):
                    raw_chars.append(c)
                    if j < len(per_char_readings):
                        parts = [p for p in per_char_readings[j].split("|") if p != ""]
                    else:
                        parts = []
                    per_char_parts.append(parts)
                    char_block_id.append(this_block)
            elif "|" in content:
                # 兼容简短：{text|mora...}（单字多段）
                segs = content.split("|")
                text_part = segs[0]
                if len(text_part) == 1:
                    parts = [p for p in segs[1:] if p != ""]
                    raw_chars.append(text_part)
                    per_char_parts.append(parts)
                    char_block_id.append(block_seq)
                    block_seq += 1
                else:
                    # 不符合 annotated 规范
                    return None
            else:
                # {text} 无 ruby
                this_block = block_seq
                block_seq += 1
                for c in content:
                    raw_chars.append(c)
                    per_char_parts.append([])
                    char_block_id.append(this_block)
            i = close + 1
        else:
            raw_chars.append(ch)
            per_char_parts.append([])
            char_block_id.append(-1)
            i += 1

    if "".join(raw_chars) != expected_word:
        return None
    return per_char_parts, char_block_id


def _build_compound_ranges(
    results: "List[AutoCheckResult]",
) -> "Dict[int, Tuple[int, int]]":
    """从 AutoCheckResult 列表构建「char_idx → (group_start, group_end)」映射。

    同时聚合 origin_morpheme_id（分析器 morpheme_span 来源）和
    compound_group_id（分发块来源，覆盖 clean-split / Step3 均分后被清空
    char_to_block 的多字块）两个维度，取二者的并集，保证所有来自同一多字单元
    的字符都被归入同一保护区间，供 Phase 5 连词组完整性检查使用。
    """
    groups: Dict[int, Tuple[int, int]] = {}
    # 先聚合 origin_morpheme_id，再叠加 compound_group_id（两者可能重叠，幂等）
    for gid_attr in ("origin_morpheme_id", "compound_group_id"):
        partial: Dict[int, Tuple[int, int]] = {}
        for r in results:
            gid = getattr(r, gid_attr)
            if gid < 0:
                continue
            idx = r.char_idx
            grp = partial.get(gid)
            if grp is None:
                partial[gid] = (idx, idx)
            else:
                partial[gid] = (min(grp[0], idx), max(grp[1], idx))
        for grp_start, grp_end in partial.values():
            if grp_end <= grp_start:
                continue  # 单字组，不写入（起不到保护作用）
            for k in range(grp_start, grp_end + 1):
                # 已有更宽的区间时不覆盖（取最宽优先级）
                existing = groups.get(k)
                new_rng = (grp_start, grp_end)
                if existing is None or (
                    existing[1] - existing[0] < new_rng[1] - new_rng[0]
                ):
                    groups[k] = new_rng
    return groups


@dataclass
class AutoCheckResult:
    """自动检查结果"""

    line_idx: int
    char_idx: int
    char: str
    check_count: int
    ruby: Optional[List[str]]  # Stage 0: _group_reading_for_character 返回 List[str]
    origin_block_id: int = -1
    # 注音来源："dict"=用户词典, "e2k"=英语词典, "library"=库函数, "self"=原字符, "none"=无注音
    origin_source: str = "none"
    # 所在 morpheme 的 id（分析器/英文回退给出的语义边界，独立于 block_id）。
    # 与 origin_block_id 的差别：origin_block_id 在 Step 3 mora 均分后会被清空（-1）；
    # origin_morpheme_id 永远保留分析器原始 morpheme 边界，专供 Phase 5 用户词典
    # 「连词组保护」判定，避免单字词条把多字 morpheme 切成两半（如 日→にち 不应
    # 污染 一日/日々/毎日 等复合词）。单字 morpheme 该字段为 -1。
    origin_morpheme_id: int = -1
    # 复合词组 id（独立于 origin_morpheme_id，专供 Phase 5 保护使用）。
    # 来源比 origin_morpheme_id 更广：
    #   - 包含 morpheme_span 给出的分析器复合词边界（与 origin_morpheme_id 重合）；
    #   - 还包含经 is_clean_per_char_split 或 Step 3 均分后被抹掉 char_to_block
    #     的多字分发块（即这些字符来自同一个 multi-char ruby_result 但 block_id
    #     已被清空的情形）。
    # 保证所有「一起从同一个多字块分发出来的字符」都被归入同一 compound_group_id，
    # 无论后续分发路径如何处理，确保 Phase 5 都能做完整覆盖检查。
    # 单字（来自独立 1-char pair）该字段为 -1。
    compound_group_id: int = -1


class AutoCheckService:
    """自动检查服务

    分析歌词文本：
    1. 拆分字符
    2. 分析注音
    3. 计算节奏点数量
    4. 构建 Character 对象
    """

    def __init__(
        self,
        ruby_analyzer: Optional[RubyAnalyzer] = None,
        auto_check_flags: Optional[Dict[str, Any]] = None,
        user_dictionary: Optional[List[Dict[str, Any]]] = None,
        annotate_katakana_with_english: bool = False,
        chinese_mode: bool = False,
        pinyin_analyzer: Optional[RubyAnalyzer] = None,
    ):
        """
        Args:
            ruby_analyzer: 注音分析器（如果为 None 则自动创建）
            auto_check_flags: 自动打勾过滤标志
            user_dictionary: 用户读音词典，格式 [{"enabled": bool, "word": str, "reading": str}, ...]
            annotate_katakana_with_english: 是否根据用户词典给片假名标注英文
            chinese_mode: 中文歌词模式（跳过日文注音分析，每个汉字视为中文单字节奏点）
            pinyin_analyzer: 中文拼音分析器（chinese_mode=True 时，
                若提供则为每个汉字标注带声调拼音 ruby；若为 None 则不产生 ruby）
        """
        self._chinese_mode = chinese_mode
        self._analyzer = ruby_analyzer or (None if chinese_mode else create_analyzer())
        self._pinyin_analyzer = pinyin_analyzer
        self._ruby_analyzer = ruby_analyzer
        self._flags = auto_check_flags or {}
        self._romanize_ruby = bool(self._flags.get("romanize_ruby", False))
        self._annotate_katakana_with_english = annotate_katakana_with_english
        # 用户词典：按 word 长度降序排列（最长匹配优先），
        # 同长度条目保持原始数组顺序。在 apply_to_sentence 末尾
        # 以子串严格匹配方式覆盖 Character[]，
        # 因此本字段仅用于 Phase 5 覆盖，不再参与 analyze_sentence 阶段。
        raw = user_dictionary or []
        _dict_entries = [
            (e["word"], e["reading"])
            for e in raw
            if e.get("enabled", True) and e.get("word") and e.get("reading")
        ]
        # 稳定排序：按 word 长度降序，同长度保持用户定义顺序
        _dict_entries.sort(key=lambda x: len(x[0]), reverse=True)
        self._dict: List[Tuple[str, str]] = _dict_entries
        # pykakasi 用于无约束分区的参考读音
        self._pykakasi_conv = None
        try:
            import pykakasi

            kks = pykakasi.kakasi()
            kks.setMode("J", "H")
            self._pykakasi_conv = kks.getConverter()
        except Exception:
            pass

        # 单字汉字音读字典（KANJIDIC2 派生）
        self._kanji_dict: Dict[str, Dict[str, List[str]]] = {}
        try:
            import json
            from pathlib import Path

            dict_path = Path(__file__).parent.parent.parent / "config" / "kanji_readings.json"
            if dict_path.exists():
                self._kanji_dict = json.loads(dict_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _should_make_romaji_self_ruby(self, char: str) -> bool:
        return self._romanize_ruby and is_self_romanizable_kana(char)

    def _detect_romaji_particles(self, sentence: Sentence) -> set[int]:
        if not self._romanize_ruby:
            return set()
        return detect_particle_part_indices(sentence)

    def _romanize_sentence_ruby(self, sentence: Sentence) -> None:
        if not self._romanize_ruby:
            return
        romanize_sentence_in_place(sentence)

    def _apply_english_dictionary(
        self, text: str, ruby_results: List[RubyResult], dict_covered: set
    ) -> Tuple[List[RubyResult], set]:
        """对英文单词应用自动注音（第十批 #5）。

        用户要求的优先级：
          1. e2k 规则引擎（基于 CMU Pronouncing Dictionary 的音素规则转换）
          2. e2k.txt 词表（EnglishRubyLookup 静态词表）

        用户词典的英文整词回退已被移除：用户词典命中在 Phase 5（apply_to_sentence
        末尾）以子串严格匹配 + Character[] 覆盖的方式处理，优先级最高。

        只覆盖未被用户词典（非英文部分）占用的英文整词范围。
        本函数对命中的英文词以整词为粒度替换 ruby_results，使下游序列化产生
        形如 "{hello|ヘロー}" 的整词 ruby，而不是被 Sudachi 逐字符拆散。

        Returns:
            (合并后的 ruby_results, 被英文注音覆盖的字符索引集合)
        """
        engine = EnglishToKanaEngine.instance()
        lookup = EnglishRubyLookup.instance()
        has_engine = engine.has()
        has_lookup = lookup.has()
        if not has_engine and not has_lookup:
            return ruby_results, set()
        e2k_covered: set[int] = set()
        overrides: List[RubyResult] = []
        for start, end, word in find_english_words(text):
            span = set(range(start, end))
            # 跳过已被用户词典（多字符跨英/非英复合词）占用的范围
            if span & dict_covered:
                continue
            # #11：规范化弯引号，保证 what\u2019s 也能命中 what's 条目
            from strange_uta_game.backend.infrastructure.parsers.english_ruby import (
                normalize_apostrophes,
            )

            normalized_word = normalize_apostrophes(word)
            # 第十批 #5 优先级：e2k → 静态 lookup
            reading = engine.convert(normalized_word) if has_engine else None
            if not reading and has_lookup:
                reading = lookup.lookup(normalized_word)
            if not reading:
                continue
            overrides.append(
                RubyResult(
                    text=word, reading=reading, start_idx=start, end_idx=end,
                    morpheme_span=(start, end) if end - start > 1 else None,
                )
            )
            e2k_covered |= span
        if not overrides:
            return ruby_results, e2k_covered
        # 移除被英文注音覆盖位置上来自 Sudachi 的逐字符结果（防止 hello 被拆成 h/e/l/l/o）
        filtered = [
            r
            for r in ruby_results
            if not (set(range(r.start_idx, r.end_idx)) & e2k_covered)
        ]
        merged = filtered + overrides
        merged.sort(key=lambda r: r.start_idx)
        return merged, e2k_covered

    def _apply_english_fallback(
        self,
        text: str,
        ruby_results: List[RubyResult],
        dict_covered: set,
        e2k_covered: set,
    ) -> Tuple[List[RubyResult], set]:
        """批 17 #1：未命中任何词典的英文连续段作为整词 ruby。

        对 find_english_words 定位到、且未被 user_dict / e2k 覆盖的英文词，
        生成 RubyResult(text=word, reading=word)，整块挂 ruby，
        配合下游 check_counts 覆写（首字=1、其他=0）实现「英文词组首字母
        一个 cp、其他字母无 cp」的需求。

        单字母英文词（end-start <= 1）不视为"词组"，跳过以保留默认逐字 cp。

        Args:
            text: 原句子文本
            ruby_results: 当前已处理的 ruby 结果
            dict_covered: 用户词典已覆盖位置
            e2k_covered: e2k 英语词典已覆盖位置

        Returns:
            (合并后的 ruby_results, 本 fallback 覆盖的字符索引集合)
        """
        covered: set[int] = set()
        overrides: List[RubyResult] = []
        for start, end, word in find_english_words(text):
            if end - start <= 1:
                continue  # 单字母词：无词组概念，保留默认
            span = set(range(start, end))
            if span & dict_covered:
                continue
            if span & e2k_covered:
                continue
            # 整词 fallback：text == reading，下游 check_counts 覆写完成 cp 分配
            overrides.append(
                RubyResult(
                    text=word, reading=word, start_idx=start, end_idx=end,
                    morpheme_span=(start, end) if end - start > 1 else None,
                )
            )
            covered |= span
        if not overrides:
            return ruby_results, covered
        # 移除 Sudachi 在这些位置的逐字符结果，防止残留
        filtered = [
            r
            for r in ruby_results
            if not (set(range(r.start_idx, r.end_idx)) & covered)
        ]
        merged = filtered + overrides
        merged.sort(key=lambda r: r.start_idx)
        return merged, covered

    def _get_digit_on_reading(self, kanji: str) -> str:
        """获取数字汉字对应的音读（优先汉字字典，其次分析器）。

        Args:
            kanji: 单个汉字字符（如 ``"三"``）

        Returns:
            平假名音读，获取失败时返回空字符串
        """
        if self._kanji_dict and kanji in self._kanji_dict:
            on_list = self._kanji_dict[kanji].get("on", [])
            if on_list:
                return self._kata_to_hira(on_list[0])
        # 字典无音读时退回到分析器
        reading = self._analyzer.get_reading(kanji)
        return reading if reading and reading != kanji else ""

    def _distribute_digit_readings(
        self,
        num_str: str,
        seq_start: int,
    ) -> List[RubyResult]:
        """将多位数字的读音分配到各个数字字符上。

        使用 :func:`_arabic_to_kanji_segments` 获取每位数字对应的汉字片段。
        先对完整漢数字串调用分析器获得有上下文的读音，再通过汉字字典拆分为
        逐字读音，最后按段分组到每位原始数字。汉字字典拆分失败时退回到逐段
        独立查询。

        Args:
            num_str: 数字字符串（如 ``"12345"``）
            seq_start: 该数字序列在句子文本中的起始索引

        Returns:
            每位数字的 RubyResult 列表
        """
        kanji, segments = _arabic_to_kanji_segments(num_str)

        # 优先：对完整漢数字串分析，获得有上下文的读音
        full_reading = self._analyzer.get_reading(kanji)
        per_digit: Dict[int, str] = {}

        if full_reading and full_reading != kanji:
            # 尝试用汉字字典将全串读音拆为逐字读音
            char_readings = self._try_split_to_chars(kanji, full_reading)
            if char_readings is not None and len(char_readings) == len(kanji):
                for seg_start, seg_end, orig_idx in segments:
                    reading = "".join(char_readings[seg_start:seg_end])
                    if reading:
                        per_digit[orig_idx] = reading
            else:
                # 拆分失败，退回到逐段独立查询（保留旧行为）
                for seg_start, seg_end, orig_idx in segments:
                    seg_kanji = kanji[seg_start:seg_end]
                    seg_reading = self._analyzer.get_reading(seg_kanji)
                    if seg_reading and seg_reading != seg_kanji:
                        per_digit[orig_idx] = seg_reading
        else:
            # 分析器无法给出有效读音，退回到逐段独立查询
            for seg_start, seg_end, orig_idx in segments:
                seg_kanji = kanji[seg_start:seg_end]
                seg_reading = self._analyzer.get_reading(seg_kanji)
                if seg_reading and seg_reading != seg_kanji:
                    per_digit[orig_idx] = seg_reading

        # 为每位数字创建独立 RubyResult
        results: List[RubyResult] = []
        for i, ch in enumerate(num_str):
            reading = per_digit.get(i, ch)  # 零位（如 105 的 0）无读音，自注音
            results.append(
                RubyResult(
                    text=ch,
                    reading=reading,
                    start_idx=seq_start + i,
                    end_idx=seq_start + i + 1,
                )
            )
        return results

    def _handle_number_readings(
        self,
        text: str,
        ruby_results: List[RubyResult],
    ) -> List[RubyResult]:
        """对文本中的阿拉伯数字序列计算日语读音。

        检测 ruby_results 中读音与原文相同的数字段，将其替换为漢数字表记后
        经注音分析器获取的日语读音（如 ``"999"`` → ``"きゅうひゃくきゅうじゅうきゅう"``）。

        仅当分析器对数字返回自注音（reading == text）时介入；若分析器已给出
        有效读音则保留原结果（如 ``"2024年"`` 被整体分析为 ``"にせんにじゅうよねん"``）。

        多位数字（如 ``"12345"``）会按位拆分读音，每位数字获得独立注音和节奏点。

        Args:
            text: 原句子文本
            ruby_results: 当前已处理的 ruby 结果

        Returns:
            更新后的 ruby_results
        """
        import re

        _digit_re = re.compile(r"\d+")
        new_results = list(ruby_results)

        for m in _digit_re.finditer(text):
            seq_start, seq_end = m.start(), m.end()
            num_str = text[seq_start:seq_end]

            # 找到该数字段覆盖的所有纯数字 RubyResult
            existing = [
                r
                for r in new_results
                if r.start_idx >= seq_start
                and r.end_idx <= seq_end
                and r.text.isdigit()
            ]

            if not existing and len(num_str) <= 1:
                # 单数字可能被包含在复合词结果中（如 "３人" → 分析器返回的
                # RubyResult 覆盖 "三人" 两个字），需要从复合词中拆分出来。
                compound = [
                    r for r in new_results
                    if r.start_idx <= seq_start
                    and r.end_idx > seq_end
                ]
                if compound:
                    new_results = [r for r in new_results if r not in compound]
                    for comp in compound:
                        # 用汉字字典拆分复合词读音
                        split = self._try_split_to_chars(comp.text, comp.reading)
                        for idx in range(comp.start_idx, comp.end_idx):
                            pos = idx - comp.start_idx
                            ch = text[idx] if idx < len(text) else comp.text[pos]
                            if idx == seq_start:
                                # 数字位：用音读替换
                                kanji = _arabic_to_kanji(num_str)
                                on_reading = self._get_digit_on_reading(kanji)
                                new_results.append(
                                    RubyResult(
                                        text=ch,
                                        reading=on_reading or ch,
                                        start_idx=idx,
                                        end_idx=idx + 1,
                                    )
                                )
                            else:
                                # 非数字位：优先用汉字字典音读（数字后的量词通常音读），
                                # 字典无音读时退回分析器拆分结果
                                comp_kanji = comp.text[pos] if pos < len(comp.text) else ch
                                non_digit_reading = self._get_digit_on_reading(comp_kanji)
                                if non_digit_reading:
                                    part_reading = non_digit_reading
                                else:
                                    part_reading = (
                                        split[pos] if split and pos < len(split) and split[pos]
                                        else ch
                                    )
                                new_results.append(
                                    RubyResult(
                                        text=ch,
                                        reading=part_reading,
                                        start_idx=idx,
                                        end_idx=idx + 1,
                                    )
                                )
                continue

            if not existing:
                continue

            # 移除旧的数字位结果
            new_results = [r for r in new_results if r not in existing]

            if len(num_str) <= 1:
                # 单数字：始终用汉字字典音读（数字在日语中应读音读），
                # 避免分析器在缺上下文时返回训读（如 ３→み 而非 さん）。
                kanji = _arabic_to_kanji(num_str)
                on_reading = self._get_digit_on_reading(kanji)
                if on_reading and on_reading != kanji:
                    new_results.append(
                        RubyResult(
                            text=num_str,
                            reading=on_reading,
                            start_idx=seq_start,
                            end_idx=seq_end,
                        )
                    )
                elif existing:
                    # 字典无音读，退回原结果
                    new_results.extend(existing)
                else:
                    new_results.append(
                        RubyResult(
                            text=num_str,
                            reading=num_str,
                            start_idx=seq_start,
                            end_idx=seq_end,
                        )
                    )
            else:
                # 多位数：按位拆分读音
                new_results.extend(
                    self._distribute_digit_readings(num_str, seq_start)
                )

        new_results.sort(key=lambda r: r.start_idx)
        return new_results

    def _try_split_to_chars(self, word: str, reading: str) -> Optional[List[str]]:
        """尝试将多字词的读音拆分到各字符（分析器分词边界 + 汉字音读字典组合匹配）。

        1. 先用注音分析器（WinRT/Sudachi 等，引擎无关）对多字词分词，若各子块
           读音拼接与给定读音完全一致，则按子块边界继续分
        2. 对每个多字子块调用汉字音读字典组合匹配

        注意：不查用户字典（用户字典由上游独立路径处理）。

        Args:
            word: 多字词
            reading: 词的总读音

        Returns:
            各字符读音列表（如果可拆分），否则 None
        """
        if len(word) <= 1:
            return None

        clean_reading = reading.replace(",", "")
        if not clean_reading:
            return None

        # 用分析器分词边界拆分
        seg_result = self._try_analyzer_split(word, clean_reading)
        if seg_result is not None:
            return seg_result

        # 单字音读字典匹配
        result = self._split_by_kanji_dict(word, clean_reading)
        if result is not None:
            return result

        # 匹配失败，不拆分
        return None

    def _try_analyzer_split(self, word: str, reading: str) -> Optional[List[str]]:
        """用注音分析器的分词边界把读音拆到各字符（引擎无关）。

        依赖 ``RubyAnalyzer.analyze`` 产出的 (surface, reading) 块；仅当各块
        读音拼接与给定 reading 完全一致时才采用（避免分析器自带读音与字典/
        用户给定读音冲突时误拆）。多字子块再走汉字音读字典。

        Args:
            word: 多字词
            reading: 词的总读音（平假名，已去逗号）

        Returns:
            各字符读音列表（长度等于 word 长度），如果可拆分，否则 None
        """
        try:
            blocks = self._analyzer.analyze(word)
        except Exception:
            return None

        if len(blocks) <= 1:
            # 分析器未进一步分词
            return None

        # 各子块读音拼接需与给定读音完全一致，否则放弃（交由单字字典）
        combined = "".join(b.reading for b in blocks)
        if combined != reading:
            return None

        final_result: List[str] = []
        for b in blocks:
            surface = b.text
            sub_reading = b.reading
            if len(surface) == 1:
                final_result.append(sub_reading)
            else:
                sub = self._split_by_kanji_dict(surface, sub_reading)
                if sub is not None:
                    final_result.extend(sub)
                else:
                    # 无法拆分：读音给第一个字，其余空
                    final_result.append(sub_reading)
                    final_result.extend([""] * (len(surface) - 1))

        if len(final_result) != len(word):
            return None
        return final_result

    def _get_single_char_candidates(self, ch: str) -> List[str]:
        """收集单个字符的候选读音（仅库：库分析器 + pykakasi）。

        用于连词回退时的「头尾假名剥离」策略。
        注意：不查用户字典、不查 e2k，用户字典/e2k 由上游独立路径处理。
        """
        options: List[str] = []
        # 1. 库分析器
        try:
            results = self._analyzer.analyze(ch)
            for r in results:
                if r.reading and r.reading != ch and r.reading not in options:
                    options.append(r.reading)
        except Exception:
            pass
        # 2. pykakasi 参考读音
        if self._pykakasi_conv is not None:
            try:
                converted = self._pykakasi_conv.do(ch)
                if converted and converted != ch and converted not in options:
                    options.append(converted)
            except Exception:
                pass
        return options

    # ── 连浊清音→浊音映射 ──
    _DAKUTEN_MAP = str.maketrans(
        "かきくけこさしすせそたちつてとはひふへほ",
        "がぎぐげござじずぜぞだぢづでどばびぶべぼ",
    )
    _HANDAKUTEN_MAP = str.maketrans(
        "はひふへほ", "ぱぴぷぺぽ"
    )

    def _split_by_kanji_dict(
        self, word: str, reading: str
    ) -> Optional[List[str]]:
        """用单字音读字典组合匹配拆分读音。

        遍历每个汉字的音读+训读候选，排列组合找到与 reading 完全匹配的拆分。
        处理「々」：继承前一个汉字的候选读音（含连浊变体）。
        """
        if not self._kanji_dict:
            return None

        n = len(word)
        char_options: List[List[str]] = []

        for i, ch in enumerate(word):
            if ch == "\u3005":  # 々: 继承前一个汉字的候选
                if i == 0:
                    # 々 作为块首字（前方汉字已被头尾剥离），无法继承读音。
                    # 若整块全为々，每字应得相同读音 → 均分后各取一份读法即可。
                    # 返回 None 交给调用方做均分，而非错误弃整块。
                    return None
                prev_opts = char_options[-1]
                opts = list(prev_opts)
                # 连浊变体: 清音首字母→浊音
                for opt in prev_opts:
                    dakuten = opt[0].translate(self._DAKUTEN_MAP)
                    if dakuten != opt[0]:
                        variant = dakuten + opt[1:]
                        if variant not in opts:
                            opts.append(variant)
                    handakuten = opt[0].translate(self._HANDAKUTEN_MAP)
                    if handakuten != opt[0]:
                        variant = handakuten + opt[1:]
                        if variant not in opts:
                            opts.append(variant)
                char_options.append(opts)
            elif ch in self._kanji_dict:
                entry = self._kanji_dict[ch]
                # 片假名→平假名
                on = [self._kata_to_hira(r) for r in entry.get("on", [])]
                kun = []
                kun_positional = []  # 带 "-" 标记的位置相关读音
                for r in entry.get("kun", []):
                    hira = self._kata_to_hira(r)
                    # 去掉送假名标记 (如 いた.む → いた)
                    hira = hira.split(".")[0]
                    if not hira:
                        continue
                    if hira.startswith("-"):
                        # 位置相关读音 (如 -ぎ → 接尾)
                        kun_positional.append(hira.lstrip("-"))
                    else:
                        kun.append(hira)
                # 合并: 通用读音 + 位置相关读音
                opts = list(dict.fromkeys(on + kun + kun_positional))
                if not opts:
                    return None
                char_options.append(opts)
            else:
                return None

        # 排列组合匹配
        def _match(idx: int, pos: int) -> Optional[List[str]]:
            if idx == n:
                return [] if pos == len(reading) else None
            for opt in char_options[idx]:
                end = pos + len(opt)
                if end <= len(reading) and reading[pos:end] == opt:
                    rest = _match(idx + 1, end)
                    if rest is not None:
                        return [opt] + rest
            return None

        return _match(0, 0)

    @staticmethod
    def _kata_to_hira(text: str) -> str:
        result = []
        for ch in text:
            code = ord(ch)
            if 0x30A1 <= code <= 0x30F6:
                result.append(chr(code - 0x60))
            else:
                result.append(ch)
        return "".join(result)

    def _fallback_split_peel_kana(
        self, word: str, reading: str
    ) -> List[str]:
        """连词回退策略：从 reading 头尾剥离能匹配的自注音字符。

        当 ``_try_split_to_chars`` 失败时调用。算法：
        1. 每字查候选读音（假名=自身，汉字查字典，其他=自身）。
        2. 从 reading 尾部递归剥离：若末字候选中某读音匹配 reading 尾部 → 扣除。
        3. 从 reading 头部递归剥离：同理。
        4. 对剩余的中间块（reading + 字符），首字承载全部剩余 reading。

        返回长度为 ``len(word)`` 的 split_parts 列表。
        中间连续汉字区域由 ``apply_to_sentence`` 基于 check_count==0 自动连词。

        Example:
            _fallback_split_peel_kana("可愛い", "かわいい")
                → ["かわい", "", "い"]   # 可吃汉字段 + い 自注音
            _fallback_split_peel_kana("明日", "あした")
                → ["あした", ""]         # 纯汉字，首字全吃
            _fallback_split_peel_kana("食べ物", "たべもの")
                → ["た", "べ", "もの"]   # 头剥食(た) + 尾剥物(もの) + べ 自注音
        """
        n = len(word)
        if n <= 1 or not reading:
            return [reading] + [""] * (n - 1)

        # Step 1: 收集每字的候选自注音
        def char_candidates(ch: str) -> List[str]:
            ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
            if ct == CharType.KANJI:
                opts = self._get_single_char_candidates(ch)
                # 补充音读字典的读音
                entry = self._kanji_dict.get(ch)
                if entry:
                    for r in entry.get("on", []):
                        hira = self._kata_to_hira(r)
                        if hira and hira not in opts:
                            opts.append(hira)
                    for r in entry.get("kun", []):
                        hira = self._kata_to_hira(r).split(".")[0].lstrip("-")
                        if hira and hira not in opts:
                            opts.append(hira)
                return opts
            # 非汉字（假名/符号/字母/数字等）→ 自身作为唯一候选
            return [ch]

        candidates: List[List[str]] = [char_candidates(c) for c in word]

        # 选择优先匹配的候选：优先使用与字符本身一致的（假名自匹配），
        # 否则取第一个能匹配的候选
        def try_match_suffix(opts: List[str], s: str) -> Optional[str]:
            for opt in opts:
                if opt and s.endswith(opt):
                    return opt
            return None

        def try_match_prefix(opts: List[str], s: str) -> Optional[str]:
            for opt in opts:
                if opt and s.startswith(opt):
                    return opt
            return None

        split_parts: List[str] = [""] * n
        remaining = reading
        left = 0
        right = n - 1

        # Step 2: 尾部剥离（尝试所有字符，非汉字必须按自身剥；汉字按候选匹配）
        # 防过吃：剥离后剩余读音长度必须 ≥ 「左侧还需填入的字符数」，否则该候选作废。
        # 历史 bug：坩堝/るつぼ 中 堝 的 kun 字典恰好是整词读音 るつぼ（历史标注法），
        # 不加约束就会让尾剥一口吃光全部读音 → split=["","るつぼ"]，
        # 与「首字吃全部剩余」的兜底语义相反；约束后退回 ["るつぼ",""] 正确。
        while right > left:
            ch = word[right]
            ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
            match = try_match_suffix(candidates[right], remaining)
            if match is None:
                # 非汉字无法剥 → 停止（假名/符号在 reading 里位置不对，放弃）
                # 汉字无法剥 → 也停止（候选不匹配）
                break
            # 防过吃：剥后剩余读音长度需 ≥ 剩余字符数（right - left 个：左边未填的）。
            if len(remaining) - len(match) < (right - left):
                break
            split_parts[right] = match
            remaining = remaining[: len(remaining) - len(match)]
            right -= 1

        # Step 3: 头部剥离（同上：防过吃 —— 剩余读音长度 ≥ 右侧未填字符数）。
        while left < right:
            ch = word[left]
            ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
            match = try_match_prefix(candidates[left], remaining)
            if match is None:
                break
            if len(remaining) - len(match) < (right - left):
                break
            split_parts[left] = match
            remaining = remaining[len(match) :]
            left += 1

        # Step 4: 头尾剥离后只剩一个中间字符 → 把剩余读音全部给它，
        # 保留两侧已成功剥离的读音，不再把整串读音倒灌首字。
        # 即便 remaining 不在该字的单字候选读音中也照分：复合词的促音便/连浊
        # 会让某字的读音偏离字典单字读音（如「逆光（ぎゃっこう）」里 逆=ぎゃっ
        # 而字典只有 ぎゃく），此时仍应得到 逆=ぎゃっ・光=こう，而不是 逆=ぎゃっこう。
        # 这类单字读音仅在复合词里成立，连词状态由 apply_to_sentence 维持。
        if left == right:
            split_parts[left] = remaining
            return split_parts

        # Step 5: 中间块 [left..right]，尝试对「纯汉字中间块」再次调用 _try_split_to_chars
        # 若成功则按字分配；失败则首字全吃，其余空（后续 apply_to_sentence 会连词）
        mid_word = word[left : right + 1]
        # 中间块全为々（迭字符）：每个々均继承前字读音，均分 remaining
        if all(c == "\u3005" for c in mid_word) and len(mid_word) > 1:
            dist = even_distribute_kana(remaining, len(mid_word))
            for i, part in enumerate(dist):
                split_parts[left + i] = part
            return split_parts
        all_kanji = all(
            (get_char_type(c) if len(c) == 1 else CharType.OTHER) == CharType.KANJI
            for c in mid_word
        )
        if all_kanji and len(mid_word) > 1:
            sub_split = self._try_split_to_chars(mid_word, remaining)
            if sub_split is not None:
                # 用音读字典校验：每字的分配读音必须在其候选中
                valid = True
                if self._kanji_dict:
                    for ci, part in zip(mid_word, sub_split):
                        if not part:
                            continue
                        entry = self._kanji_dict.get(ci)
                        if not entry:
                            continue
                        on = [self._kata_to_hira(r) for r in entry.get("on", [])]
                        kun = []
                        for r in entry.get("kun", []):
                            hira = self._kata_to_hira(r).split(".")[0].lstrip("-")
                            if hira:
                                kun.append(hira)
                        readings = set(on + kun)
                        if part not in readings:
                            valid = False
                            break
                if valid:
                    for i, part in enumerate(sub_split):
                        split_parts[left + i] = part
                    return split_parts

        # 首字吃全部剩余（保留原回退语义）
        split_parts[left] = remaining
        # 中间块里 left+1..right 保持 ""（由 apply_to_sentence 基于 check_count==0 连词）
        return split_parts

    def _partition_reading(
        self,
        reading: str,
        n: int,
        ref_readings: List[str],
        ki: int = 0,
        ri: int = 0,
    ) -> Optional[List[str]]:
        """递归分区读音到 n 个字符。三级匹配策略：精确 > 前缀 > 无约束。"""
        if ki == n:
            return [] if ri == len(reading) else None
        if ri >= len(reading):
            return None
        remaining_chars = n - ki
        remaining_reading = len(reading) - ri
        if remaining_reading < remaining_chars:
            return None
        max_len = remaining_reading - (remaining_chars - 1)

        ref = ref_readings[ki] if ki < len(ref_readings) else ""
        tried: set = set()

        # 优先精确匹配
        if ref:
            ref_len = len(ref)
            if ref_len <= max_len:
                portion = reading[ri : ri + ref_len]
                if portion == ref:
                    rest = self._partition_reading(
                        reading, n, ref_readings, ki + 1, ri + ref_len
                    )
                    if rest is not None:
                        return [portion] + rest
                    tried.add(ref_len)

        # 前缀匹配
        for try_len in range(1, max_len + 1):
            if try_len in tried:
                continue
            portion = reading[ri : ri + try_len]
            if ref and not ref.startswith(portion):
                continue
            rest = self._partition_reading(
                reading, n, ref_readings, ki + 1, ri + try_len
            )
            if rest is not None:
                return [portion] + rest
            tried.add(try_len)

        # 无约束匹配
        for try_len in range(1, max_len + 1):
            if try_len in tried:
                continue
            rest = self._partition_reading(
                reading, n, ref_readings, ki + 1, ri + try_len
            )
            if rest is not None:
                return [reading[ri : ri + try_len]] + rest
        return None

    def _apply_flags_filter(
        self,
        chars: List[str],
        check_counts: List[int],
        text: str,
    ) -> None:
        """将自动打勾过滤标志应用到 check_counts（原位修改）。

        执行顺序（顺序即优先级，后者可覆盖前者）：
        1. check_line_start —— 首先为行首字符设定"至少 1 cp"基线
        2. 字符类型/特殊字符过滤 —— 可将 check_line_start 的基线覆盖回 0
        3. 括号内字符过滤

        所有符号（含原"标点"）统一由「記号（符号）」类型开关（symbol flag）经
        Step 2 控制，不再有独立的标点最终覆盖；行首符号在 symbol 关闭时由
        Step 2 的类型过滤清零，故 check_line_start 仍须置于类型过滤之前。
        """
        if not self._flags:
            # 无标志时：符号默认不打 CP（等价于 symbol 关闭）
            for i, ch in enumerate(chars):
                if (i < len(check_counts)
                        and len(ch) == 1
                        and get_char_type(ch) == CharType.SYMBOL):
                    check_counts[i] = 0
            return

        # Step 1: check_line_start —— 在类型过滤之前设基线，后续过滤可覆盖
        if self._flags.get("check_line_start", False) and check_counts and text.strip():
            check_counts[0] = max(check_counts[0], 1)

        # Step 2: 逐字符类型/特殊字符过滤（优先级高于 check_line_start 的基线）
        for i, char in enumerate(chars):
            if i >= len(check_counts):
                break

            ct = get_char_type(char) if len(char) == 1 else CharType.OTHER

            # 空格：主开关 + 上下文子选项共同决定是否打 CP（set-to-1 语义）
            # 须先于通用 _TYPE_FLAG_MAP 检查处理，因为空格逻辑与其他类型不同
            if ct in (CharType.SPACE, CharType.FULL_SPACE):
                if not self._flags.get("space", True) or i == 0:
                    # 主开关关闭，或行首空格：不打 CP
                    check_counts[i] = 0
                else:
                    prev_ct = (
                        get_char_type(chars[i - 1])
                        if len(chars[i - 1]) == 1
                        else CharType.OTHER
                    )
                    if prev_ct in (
                        CharType.HIRAGANA,
                        CharType.KATAKANA,
                        CharType.KANJI,
                        CharType.SOKUON,
                        CharType.LONG_VOWEL,
                    ):
                        check_counts[i] = 1 if self._flags.get("space_after_japanese", True) else 0
                    elif prev_ct == CharType.ALPHABET:
                        check_counts[i] = 1 if self._flags.get("space_after_alphabet", True) else 0
                    elif prev_ct in (CharType.SYMBOL, CharType.NUMBER):
                        check_counts[i] = 1 if self._flags.get("space_after_symbol", True) else 0
                    else:
                        check_counts[i] = 0  # 其他上下文（如行首、连续空格等）
                continue

            flag_key = _TYPE_FLAG_MAP.get(ct)
            if flag_key and not self._flags.get(flag_key, True):
                check_counts[i] = 0
                continue

            # fallback 默认值以 UI 显示为基准（check_n / check_sokuon 默认开，
            # check_long_vowel 默认关），避免 _flags 缺键时与界面默认相反。
            if char in ("ん", "ン") and not self._flags.get("check_n", True):
                check_counts[i] = 0
                continue

            if ct == CharType.SOKUON and not self._flags.get("check_sokuon", True):
                check_counts[i] = 0
                continue

            if ct == CharType.LONG_VOWEL and not self._flags.get("check_long_vowel", False):
                check_counts[i] = 0
                continue

            if char in _SMALL_KANA_SET and not self._flags.get("small_kana", False):
                check_counts[i] = 0
                continue

        # Step 3: 括号内字符过滤
        # 支持半/全角圆括号、方括号、花括号、日文引号「」『』、尖括号《》〈〉、
        # 龟甲括号〔〕 等；用深度计数处理嵌套。括号本身不在此清零（由 Step 2
        # 的符号类型过滤决定），只清零括号内的字符。
        if not self._flags.get("check_parentheses", True):
            _OPENERS = "([{（［｛【「『《〈〔"
            _CLOSERS = ")]}）］｝】」』》〉〕"
            depth = 0
            for i, char in enumerate(chars):
                if char in _OPENERS:
                    depth += 1
                elif char in _CLOSERS:
                    if depth > 0:
                        depth -= 1
                elif depth > 0 and i < len(check_counts):
                    check_counts[i] = 0

    def _apply_english_and_endpoints(
        self,
        sentence: Sentence,
        check_counts: List[int],
    ) -> None:
        """英文词音节规则 + 行尾/停顿点标记 + check_count 写入（共用）。

        前置条件：sentence.characters 已建好；check_counts 已经过
        _apply_flags_filter / ruby 计算等前序步骤。
        本方法在 check_counts 基础上叠加英文音节规则、行尾停顿点标记，
        并最终调用 set_check_count 写入字符。
        """
        if not sentence.characters:
            return

        text = sentence.text
        chars = [c.char for c in sentence.characters]
        n = len(sentence.characters)

        # ── 英文词组节奏点规则 ──
        english_sentence_end_idx: set[int] = set()
        english_word_end_idx: set[int] = set()
        english_word_trailing_comma_idx: set[int] = set()
        check_english_word_end = self._flags.get("check_english_word_end", True)
        _english_syllable_check = self._flags.get("english_syllable_check", True)
        for start, end, word in find_english_words(text):
            _is_single = end - start <= 1
            _syllable_starts = (
                get_syllable_start_offsets(word) if _english_syllable_check else {0}
            )
            for idx in range(start, end):
                if idx < len(check_counts):
                    check_counts[idx] = 1 if (idx - start) in _syllable_starts else 0
            if end - 1 < n:
                english_word_end_idx.add(end - 1)
                if not _is_single and check_english_word_end:
                    english_sentence_end_idx.add(end - 1)
            if end < len(text) and text[end] in _TRAILING_COMMA_CHARS:
                english_word_trailing_comma_idx.add(end)

        # ── 行尾 / 空格视为停顿点 ──
        _is_blank_line = not text.strip()
        add_line_end = self._flags.get("check_line_end", True) and not _is_blank_line
        check_space_as_line_end = (
            self._flags.get("check_space_as_line_end", True) and not _is_blank_line
        )

        for i, char in enumerate(sentence.characters):
            is_last = i == n - 1
            is_before_space = (
                not is_last
                and check_space_as_line_end
                and i + 1 < n
                and len(chars[i + 1]) == 1
                and chars[i + 1].isspace()
                and not (i in english_word_end_idx and not check_english_word_end)
                and i not in english_word_trailing_comma_idx
            )

            is_sentence_end = False
            if is_last and add_line_end:
                is_sentence_end = True
            if is_before_space:
                is_sentence_end = True
            if i in english_sentence_end_idx:
                is_sentence_end = True

            # 守卫：已持有 timestamps 的字符不可被截断（n3 加载场景）
            in_ruby_block_follower = (
                i > 0
                and sentence.characters[i - 1].linked_to_next
                and sentence.characters[i - 1].ruby is not None
                and len(sentence.characters[i - 1].ruby.parts) > 0
                and not char.ruby
            )
            if char.timestamps and check_counts[i] < len(char.timestamps) and not in_ruby_block_follower:
                check_counts[i] = len(char.timestamps)

            # 守卫：n3 加载已携带 sentence_end_ts 的字符必须保留
            if char.sentence_end_ts is not None:
                is_sentence_end = True

            if in_ruby_block_follower and check_counts[i] == 0:
                preserved_ts = list(char.timestamps)
                char.set_check_count(check_counts[i], force=True)
                char.timestamps = preserved_ts
            else:
                char.set_check_count(check_counts[i], force=True)

            char.is_line_end = is_last and add_line_end
            char.is_sentence_end = is_sentence_end
            if not char.is_sentence_end:
                char.clear_sentence_end_ts()

    def _apply_chinese_to_sentence(
        self,
        sentence: Sentence,
        keep_existing_timetags: bool = True,
    ) -> None:
        """中文歌词模式：按字符流计算节奏点。

        - 若 ``self._pinyin_analyzer`` 已提供，为每个汉字标注带声调拼音 ruby；
        - 否则全程不产生 ruby（仅节奏点模式）。

        check 规则与日文路径共用 _apply_flags_filter + _apply_english_and_endpoints。
        旧时间戳在过滤/端点规则**之前**回种到新字符上，
        _apply_english_and_endpoints 的「已持有 timestamps 的字符不可被截断」
        守卫（不区分分析模式）因此对本路径同样生效，与日文路径管线行为一致。
        """
        text = sentence.text
        if not text:
            return

        old_timestamps: Dict[int, List[int]] = {}
        old_sentence_end_ts: Dict[int, int] = {}
        old_singer_map: Dict[int, str] = {}
        for i, c in enumerate(sentence.characters):
            if c.timestamps:
                old_timestamps[i] = list(c.timestamps)
            if c.sentence_end_ts is not None:
                old_sentence_end_ts[i] = c.sentence_end_ts
            old_singer_map[i] = c.singer_id

        chars = list(text)
        n = len(chars)
        check_counts: List[int] = [1] * n

        # 拼音注音子步骤：chinese_mode 下若传入了 pinyin_analyzer，则标注拼音
        pinyin_map: Dict[int, str] = {}
        if self._pinyin_analyzer is not None:
            pinyin_results = self._pinyin_analyzer.analyze(text)
            for r in pinyin_results:
                for offset in range(r.end_idx - r.start_idx):
                    idx = r.start_idx + offset
                    if r.reading and r.reading != r.text:
                        pinyin_map[idx] = r.reading

        new_characters: List[Character] = []
        for i, ch in enumerate(chars):
            ruby = None
            py = pinyin_map.get(i, "")
            if py:
                ruby = Ruby(parts=[RubyPart(text=py)])
            character = Character(
                char=ch,
                ruby=ruby,
                check_count=1,
                is_line_end=False,
                is_sentence_end=False,
                singer_id=old_singer_map.get(i, sentence.singer_id),
            )
            new_characters.append(character)
        sentence.characters = new_characters

        # 先回种旧时间戳，再跑过滤/端点规则（见方法 docstring）：
        # 守卫读取的是字符当前持有的 timestamps，回种后中文/非假名路径
        # 与日文路径管线（update_checkpoints_from_rubies）行为完全一致。
        if keep_existing_timetags:
            for i, char in enumerate(sentence.characters):
                if i in old_timestamps:
                    char.timestamps = list(old_timestamps[i])

        self._apply_flags_filter(chars, check_counts, text)
        self._apply_english_and_endpoints(sentence, check_counts)

        if keep_existing_timetags:
            for i, char in enumerate(sentence.characters):
                if i in old_sentence_end_ts:
                    # 停顿点 ts 按原字符位恢复并强制停顿点标记——
                    # 释放点是行尾时刻（时间事实），不随节奏点重算丢弃
                    char.is_sentence_end = True
                    char.sentence_end_ts = old_sentence_end_ts[i]
                    char.push_to_ruby()

    def analyze_sentence(
        self, sentence: Sentence, split_config: Optional[SplitConfig] = None
    ) -> List[AutoCheckResult]:
        """分析句子歌词

        Args:
            sentence: 句子
            split_config: 拆分配置

        Returns:
            分析结果列表
        """
        text = sentence.text
        if not text:
            if self._flags.get("check_empty_lines", False):
                return [
                    AutoCheckResult(
                        line_idx=0,
                        char_idx=0,
                        char="",
                        check_count=1,
                        ruby=None,
                    )
                ]
            return []

        split_config = split_config or SplitConfig()

        # 拆分文本
        chars, check_counts = split_text(text, split_config)

        # split_text 会按既有规则合并连续空白（混有全角空格时保留为一个全角
        # 空格）。后续分析器和所有基于字符索引的处理必须使用同一份规范化文本，
        # 否则分析器按原文位置返回结果、chars 却已少了字符，会导致后半句注音
        # 整体左移。apply_to_sentence 最终也会据 chars 将句子重建为合并后的文本。
        text = "".join(chars)

        # 分析注音
        ruby_results = self._analyzer.analyze(text)

        # 为数字序列计算日语读音（如 "999" → 漢数字 → 注音分析器获取读音）
        ruby_results = self._handle_number_readings(text, ruby_results)

        # #11: 过滤掉符号/括号等非目标字符的注音条目
        # 自动注音仅针对：英文字符、英文单词、汉字、日汉字、平假名、片假名
        def _result_should_keep(r: RubyResult) -> bool:
            if not r.text:
                return False
            # 检查首字符类型即可（ruby_results 的 text 通常是整词或单字符）
            for c in r.text:
                ct = get_char_type(c) if len(c) == 1 else CharType.OTHER
                if ct in _RUBY_ALLOWED_TYPES:
                    return True
            return False

        ruby_results = [r for r in ruby_results if _result_should_keep(r)]

        # 用户词典覆盖已迁移到 Phase 5（apply_to_sentence 末尾，子串严格匹配
        # 覆盖 Character[]），此处不再处理；dict_covered 仅作为占位传给英文阶段。
        dict_covered: set = set()

        # #12: 应用英语词典（e2k）覆盖（用户词典之后，库函数之前的优先级）
        ruby_results, e2k_covered = self._apply_english_dictionary(
            text, ruby_results, dict_covered
        )

        # 批 17 #1: 英文词组 fallback — 未命中任何词典的英文词整块挂 ruby
        # 配合下游 check_counts 覆写实现「首字=1 cp、其他字母=0 cp」
        ruby_results, english_fallback_covered = self._apply_english_fallback(
            text, ruby_results, dict_covered, e2k_covered
        )

        # 记录每个块的来源（用于 #10 连词判定）
        block_source: Dict[int, str] = {}
        for block_id, result in enumerate(ruby_results):
            span = set(range(result.start_idx, result.end_idx))
            if span & dict_covered:
                block_source[block_id] = "dict"
            elif span & e2k_covered:
                block_source[block_id] = "e2k"
            elif span & english_fallback_covered:
                block_source[block_id] = "english_fallback"
            else:
                block_source[block_id] = "library"

        # 永久 morpheme 边界（独立于 char_to_block 和 Step 3 mora 均分）：
        # 用 RubyResult.morpheme_span 登记每个 char 所在的 morpheme 区间 id。
        # 同一 morpheme 派生出的多个 RubyResult（如 ある日→[(あ,あ),(る,る),(日,ひ)]
        # 共享 span=(0,3)）会得到同一 morpheme id；单字 morpheme（surface 长度=1，
        # span=None）不登记，不享受 Phase 5 连词组保护。
        #
        # 这与 char_to_block 的差别：char_to_block 只服务渲染连词（会被 Step 3 mora
        # 均分抹掉、被 clean per-char split 跳过）；char_to_morpheme 始终反映
        # 分析器/英文回退给出的原始 morpheme 边界，专供 Phase 5 单字词典保护用。
        char_to_morpheme: Dict[int, int] = {}
        span_to_id: Dict[Tuple[int, int], int] = {}
        for result in ruby_results:
            # 优先用分析器显式给的 morpheme_span（同一 pair 派生出的多 block 共享同一 span）；
            # 老旧/自定义 analyzer 未设 morpheme_span 时退回 result 自身 span，
            # 仅对覆盖 > 1 字的 RubyResult 起保护作用。
            span = result.morpheme_span
            if span is None:
                if result.end_idx - result.start_idx > 1:
                    span = (result.start_idx, result.end_idx)
                else:
                    continue
            mid = span_to_id.get(span)
            if mid is None:
                mid = len(span_to_id)
                span_to_id[span] = mid
            for idx in range(result.start_idx, result.end_idx):
                if idx < len(chars):
                    char_to_morpheme[idx] = mid
        # 英文 morpheme 边界对齐：e2k/english_fallback 单词整词作为一个 morpheme，
        # 这里通过 ruby_results 的 start/end 已经覆盖正确。
        # 多字非汉字 morpheme（罕见，如 katakana_english）也通过 morpheme_span 自然命中。

        # 片假名外来语 → 英文标注（仅 LLM 注音会产出「片假名 surface + 英文 reading」）。
        # 整词作为单块：首字承载英文读音、整词连词、节奏点首字=1 其余=0；
        # 须豁免后续的「首尾假名剥离 / 中间假名清理 / 第三步均分」逻辑。
        katakana_english_covered: set = set()
        if self._annotate_katakana_with_english:
            for block_id, result in enumerate(ruby_results):
                segmented = _split_segmented_english_reading(
                    result.reading, result.end_idx - result.start_idx
                )
                if is_all_katakana(result.text) and (
                    is_english_reading(result.reading) or segmented is not None
                ):
                    block_source[block_id] = "katakana_english"
                    katakana_english_covered |= set(
                        range(result.start_idx, result.end_idx)
                    )

        # 创建字符到注音的映射（按 mora 分割到每个字符）
        char_to_ruby_raw: Dict[int, str] = {}
        char_to_block: Dict[int, int] = {}
        # 复合词块归组：记录「来自同一个 multi-char ruby_result」的字符，
        # 不受 is_clean_per_char_split / Step 3 清空 char_to_block 的影响。
        # 专供下游 compound_group_id 构建，确保 Phase 5 保护不因拆分路径丢失。
        char_to_dist_block: Dict[int, int] = {}
        for block_id, result in enumerate(ruby_results):
            block_len = result.end_idx - result.start_idx
            # 片假名外来语块：保持一个连词块。默认词典风格的逗号分段把英文
            # 片段放到对应片假名位置；整词英文兼容格式仍由首字承载。
            if block_source.get(block_id) == "katakana_english":
                english_parts = _split_segmented_english_reading(
                    result.reading, block_len
                )
                for idx in range(result.start_idx, result.end_idx):
                    if idx >= len(chars):
                        break
                    pos = idx - result.start_idx
                    if english_parts is not None and english_parts[pos]:
                        char_to_ruby_raw[idx] = english_parts[pos]
                    elif english_parts is None and idx == result.start_idx:
                        char_to_ruby_raw[idx] = result.reading.strip()
                    char_to_block[idx] = block_id
                continue
            # "干净拆分"标记：用户词典 reading 用逗号干净拆成每字独立读音时
            # （每段非空 + 段数 == 字符数），不应强制连词，让每字能被独立使用。
            # 例：`大空 → おお,そら` → 大[おお] 空[そら] 各自独立。
            # 反例：`可愛い → かわい,,い`（中间空段）仍需连词承载 mora。
            is_clean_per_char_split = False
            # 词典条目可能用逗号分隔各字符的读音（如 "だい,ぼう,けん"）
            if "," in (result.reading or "") and block_len > 1:
                parts = [p.strip() for p in result.reading.split(",")]
                # 补齐不足的部分
                while len(parts) < block_len:
                    parts.append("")
                split_parts = parts[:block_len]
                # 劣质拆分检测：仅当「最末尾 part 为空且对应字符是汉字」时，
                # 视为字典条目错漏（尾部汉字无注音承载对象）。走 fallback 重算。
                # 中间空 part 视为用户显式的「首字/前字承载 mora」连词语义，尊重之。
                # 末尾空 part 对应假名属送り仮名模式，由后续首尾剥离处理。
                has_empty_tail_kanji = False
                for pos in range(block_len - 1, -1, -1):
                    if split_parts[pos]:
                        # 从尾往前遇到非空即停（只看真正的尾部空）
                        break
                    idx = result.start_idx + pos
                    if idx >= len(chars):
                        continue
                    ch = chars[idx]
                    ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
                    if ct not in (CharType.HIRAGANA, CharType.KATAKANA):
                        has_empty_tail_kanji = True
                        break
                if has_empty_tail_kanji:
                    # 从字典 reading 中剥离逗号，用完整读音重算 peel_kana
                    full_reading = result.reading.replace(",", "")
                    split_parts = self._fallback_split_peel_kana(
                        result.text, full_reading
                    )
                    # 升级来源让 apply_to_sentence 允许连续汉字间连词
                    block_source[block_id] = "fallback"
                else:
                    # 干净拆分判定：所有 part 非空 + 原始段数 == block_len
                    # （补齐逻辑产生的尾部空段算不干净）
                    if (
                        len(parts) >= block_len
                        and all(p for p in split_parts)
                    ):
                        is_clean_per_char_split = True
            else:
                if block_len > 1:
                    # 尝试按单字读音拆分
                    char_split = self._try_split_to_chars(result.text, result.reading)
                    if char_split is not None:
                        split_parts = char_split
                        # 干净拆分判定：所有 part 非空 + 段数 == 字符数
                        if len(split_parts) == block_len and all(p for p in split_parts):
                            is_clean_per_char_split = True
                    else:
                        # 不可拆分则走「头尾假名剥离」回退
                        split_parts = self._fallback_split_peel_kana(
                            result.text, result.reading
                        )
                        # 升级来源为 "fallback"，让 apply_to_sentence 允许连续汉字间连词
                        block_source[block_id] = "fallback"
                else:
                    split_parts = split_ruby_for_checkpoints(result.reading, block_len)
            for idx in range(result.start_idx, result.end_idx):
                if idx < len(chars):
                    pos = idx - result.start_idx
                    if pos < len(split_parts) and split_parts[pos]:
                        char_to_ruby_raw[idx] = split_parts[pos]
                    # 干净拆分（每段非空 + 段数==字符数）→ 每字独立，
                    # 不写 char_to_block，使 origin_block_id 保持 -1，
                    # 从而跳过 L1094-1100 的连词判定，允许单字独立使用。
                    # 例：大空=おお,そら → 大[おお]+空[そら] 独立；
                    # 大冒険=だい,ぼう,けん → 大/冒/険 各自独立。
                    if not is_clean_per_char_split:
                        char_to_block[idx] = block_id
                    # compound_group_id 追踪：无论 is_clean_per_char_split 如何，
                    # 只要来自同一个多字 block 就记录归组，供 Phase 5 保护使用。
                    if block_len > 1:
                        char_to_dist_block[idx] = block_id

        # 首尾假名剥离：若连词块的首/尾字符是假名（送り仮名/接头假名模式），
        # 将它们从 char_to_block 中移除，使其成为独立自注音字符，
        # 避免 linked_to_next 把送り仮名吸入连词块（如 "可愛い" 字典条目
        # reading="かわい,,い" 使 char_to_block 覆盖全 3 字，导致末尾 い 错误连词）。
        # 剥离条件：对应 split_parts[pos] 为空字符串 或 等于字符本身（即明确表示
        # "该字符由自身注音，不应作为连词成员"）。
        for block_id, result in enumerate(ruby_results):
            block_len = result.end_idx - result.start_idx
            if block_len < 2:
                continue
            # 片假名外来语块：整词连词由首字承载英文，不剥离尾部片假名
            if block_source.get(block_id) == "katakana_english":
                continue
            # 从末尾向前剥离
            for pos in range(block_len - 1, 0, -1):
                idx = result.start_idx + pos
                if idx >= len(chars):
                    continue
                char = chars[idx]
                ct = get_char_type(char) if len(char) == 1 else CharType.OTHER
                if ct not in (CharType.HIRAGANA, CharType.KATAKANA):
                    break
                part = char_to_ruby_raw.get(idx, "")
                if part and part != char:
                    break
                # 剥离：移出 block，让后续自注音兜底
                char_to_block.pop(idx, None)
                char_to_ruby_raw.pop(idx, None)
            # 从首部向后剥离（保留至少 1 个字符在块中）
            for pos in range(0, block_len - 1):
                idx = result.start_idx + pos
                if idx >= len(chars):
                    continue
                # 已被末尾剥离阶段移出的不再处理
                if idx not in char_to_block:
                    continue
                char = chars[idx]
                ct = get_char_type(char) if len(char) == 1 else CharType.OTHER
                if ct not in (CharType.HIRAGANA, CharType.KATAKANA):
                    break
                part = char_to_ruby_raw.get(idx, "")
                if part and part != char:
                    break
                char_to_block.pop(idx, None)
                char_to_ruby_raw.pop(idx, None)

        # 清理：将连词块中无 ruby 的假名从 char_to_block 中移除，使其自注音。
        # 首尾假名剥离只能处理头尾的假名，中间的假名（如「食べ物」的「べ」）需要这里处理。
        cleaned_kana_indices: set = set()
        for idx in list(char_to_block.keys()):
            if idx in char_to_ruby_raw:
                continue  # 有 ruby，保留
            if idx in katakana_english_covered:
                continue  # 片假名外来语块的尾随片假名：保持连词，不自注音
            ch = chars[idx] if idx < len(chars) else ""
            ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
            if ct in (CharType.HIRAGANA, CharType.KATAKANA):
                char_to_block.pop(idx, None)
                cleaned_kana_indices.add(idx)

        # 第三步：对于连为整体的汉字，如果注音 mora 数和汉字数的比例刚好可以整除，
        # 则将其拆分为独立字符。所有来源（除了英文）的汉字都要有这样的逻辑。
        # 例：明日あす：2个字符，2个注音（あ+す），2/2=1，可以平均分配
        # 凛々しい：凛々 りり 是2个字符2个注音，2/2=1，可以平均分配
        # 今日きょう：2个字符，3个注音（きょ+う），3/2=1.5，无法平均分配，保持连词
        step3_handled_blocks: set = set()
        for block_id, result in enumerate(ruby_results):
            block_len = result.end_idx - result.start_idx
            if block_len < 2:
                continue
            # 跳过英文来源（含片假名外来语英文标注）
            if block_source.get(block_id) in ("e2k", "english_fallback", "katakana_english"):
                continue
            # 只收集仍在 char_to_block 中的汉字字符（未被首尾假名剥离的）
            # 假名字符不参与均分，保持自注音
            kanji_indices = []
            kanji_rubies = []
            for pos in range(block_len):
                idx = result.start_idx + pos
                if idx >= len(chars):
                    continue
                if idx not in char_to_block:
                    continue  # 已被剥离，跳过
                ch = chars[idx]
                ct = get_char_type(ch) if len(ch) == 1 else CharType.OTHER
                if ct != CharType.KANJI:
                    continue  # 只处理汉字
                kanji_indices.append(idx)
                kanji_rubies.append(char_to_ruby_raw.get(idx, ""))
            effective_len = len(kanji_indices)
            if effective_len < 2:
                continue
            # 计算汉字注音总字符数（假名字符数，不是mora数）
            total_chars = 0
            for r in kanji_rubies:
                if r:
                    total_chars += len(r)
            # 计算每个汉字应该分配的字符数
            if total_chars == 0 or total_chars % effective_len != 0:
                # 无法平均分配，保持连词
                continue
            chars_per_kanji = total_chars // effective_len
            # 将注音平均分配给所有汉字
            all_chars = []
            for r in kanji_rubies:
                if r:
                    all_chars.extend(list(r))
            # 重新分配注音
            char_idx = 0
            for i, idx in enumerate(kanji_indices):
                # 每个汉字分配 chars_per_kanji 个字符
                assigned = all_chars[char_idx:char_idx + chars_per_kanji]
                char_to_ruby_raw[idx] = "".join(assigned)
                char_idx += chars_per_kanji
                # 从 char_to_block 中移除，使其独立
                char_to_block.pop(idx, None)
                # 更新 check_counts
                if idx < len(check_counts):
                    check_counts[idx] = len(split_into_moras(char_to_ruby_raw[idx]))
            step3_handled_blocks.add(block_id)

        # 未被分析器覆盖的字符使用自注音（保证所有字符都有 ruby）
        # #11：连词块内 split_parts 为空的字符（如 e2k "hello" 的 e/l/l/o 位置）
        # 已归属某个 block（char_to_block 中有记录），不应再 fallback 到自注音，
        # 否则会在导出中出现 {hello|ヘロー,e,l,l,o} 的多余字符残留。
        for idx, char in enumerate(chars):
            if idx in char_to_ruby_raw:
                continue
            if idx in char_to_block:
                # 属于某连词块但自身无拆分读音（连词：读音由首字承载）
                continue
            char_to_ruby_raw[idx] = char

        # 根据注音更新 check_count（汉字按 mora 数分配节奏点）
        for block_id, result in enumerate(ruby_results):
            # 批 17 #1: 英文词组 fallback 块——首字母=1 cp、其他字母=0 cp
            # 必须在 `result.text == result.reading` 短路之前处理
            # （fallback 的 text 与 reading 完全相同，否则会被跳过保留默认每字母=1）
            if block_source.get(block_id) == "english_fallback":
                # 英文词组 fallback：整词首字 = 1 cp，其余字符 = 0。
                for idx in range(result.start_idx, result.end_idx):
                    if idx < len(check_counts):
                        check_counts[idx] = 1 if idx == result.start_idx else 0
                continue
            if block_source.get(block_id) == "katakana_english":
                english_parts = _split_segmented_english_reading(
                    result.reading, result.end_idx - result.start_idx
                )
                for idx in range(result.start_idx, result.end_idx):
                    if idx >= len(check_counts):
                        break
                    pos = idx - result.start_idx
                    check_counts[idx] = (
                        1 if english_parts is not None and english_parts[pos]
                        else 1 if english_parts is None and pos == 0
                        else 0
                    )
                continue
            if result.text == result.reading:
                continue  # 假名/符号/空格等读音与原文相同，不更新

            # 检查这个块是否已经被第三步处理过
            block_len = result.end_idx - result.start_idx
            if block_id in step3_handled_blocks:
                # 已经被第三步处理过，跳过
                continue

            # 词典条目可能用逗号分隔各字符的读音（如 "だい,ぼう,けん"）
            if "," in (result.reading or "") and block_len > 1:
                parts = [p.strip() for p in result.reading.split(",")]
                while len(parts) < block_len:
                    parts.append("")
                split_parts = parts[:block_len]
            else:
                if block_len > 1:
                    # 尝试按单字读音拆分
                    char_split = self._try_split_to_chars(result.text, result.reading)
                    if char_split is not None:
                        split_parts = char_split
                    else:
                        # 不可拆分则走「头尾假名剥离」回退
                        split_parts = self._fallback_split_peel_kana(
                            result.text, result.reading
                        )
                else:
                    split_parts = split_ruby_for_checkpoints(result.reading, block_len)
            for idx in range(result.start_idx, result.end_idx):
                # 跳过被清理的假名（自注音字符）
                if idx in cleaned_kana_indices:
                    continue
                if idx < len(check_counts):
                    pos = idx - result.start_idx
                    if pos < len(split_parts) and split_parts[pos]:
                        check_counts[idx] = len(split_into_moras(split_parts[pos]))
                    else:
                        check_counts[idx] = 0

        # 单一平假名/片假名封顶：单个假名字符最多 1 cp（可以是 0）。
        # 场景：`ロミオ → Ro,me,o` 经 e2k 路径，split_parts 是英文音节，
        # 被 split_into_moras 按字符计数误拿到 2/2/1，应统一封顶为 1/1/1。
        # 汉字/英文字母不受限，允许按 mora 分配。
        for i, ch in enumerate(chars):
            if i >= len(check_counts):
                break
            if len(ch) == 1 and get_char_type(ch) in (
                CharType.HIRAGANA,
                CharType.KATAKANA,
            ):
                if check_counts[i] > 1:
                    check_counts[i] = 1

        # 应用自动打勾过滤规则（含 check_line_start 和标点最终覆盖）
        self._apply_flags_filter(chars, check_counts, text)

        # 批 18 #9：英文词组节奏点规则（按音节首字=1，其余=0；关闭时整词首字=1）
        # 必须放在 e2k mora 分配之后，覆盖 e2k 命中分支的 per-char mora 计数。
        # english_fallback 分支已在前面手动应用过同样规则；此处再次覆盖是幂等的。
        # find_english_words 基于 text 的字符索引，与 chars/check_counts 一一对应。
        _english_syllable_check = self._flags.get("english_syllable_check", True)
        for _start, _end, _word in find_english_words(text):
            _syllable_starts = (
                get_syllable_start_offsets(_word) if _english_syllable_check else {0}
            )
            for _idx in range(_start, _end):
                if _idx < len(check_counts):
                    check_counts[_idx] = 1 if (_idx - _start) in _syllable_starts else 0

        # compound_group_id 构建：在 char_to_morpheme（morpheme_span 来源）基础上，
        # 补入 char_to_dist_block（分发块来源）里未被 morpheme_span 覆盖的多字块。
        # 须在分发循环、Step3 均分全部完成之后执行，确保 char_to_dist_block 已填满。
        # 这样 is_clean_per_char_split / Step3 均分清空 char_to_block 之后，
        # 仍能通过 compound_group_id 知道哪些字符来自同一个多字 block，
        # 保证 Phase 5 做完整覆盖检查时信息不丢失。
        char_to_compound_group: Dict[int, int] = dict(char_to_morpheme)
        _dist_bid_to_cg: Dict[int, int] = {}
        _next_cg_id = len(span_to_id)
        for _idx, _bid in char_to_dist_block.items():
            if _idx in char_to_compound_group:
                continue  # 已被 morpheme_span 覆盖，跳过
            if _bid not in _dist_bid_to_cg:
                _dist_bid_to_cg[_bid] = _next_cg_id
                _next_cg_id += 1
            char_to_compound_group[_idx] = _dist_bid_to_cg[_bid]

        # 构建结果
        # check_n 关闭（「んン 不打节奏点」规则生效）时，把字符注音里非起始的
        # ん/ン 分段并入前一拍，节奏点数同步收敛。例：険[け|ん] 2拍 → [けん] 1拍。
        _merge_n = bool(self._flags) and not self._flags.get("check_n", False)
        results = []
        for i, (char, count) in enumerate(zip(chars, check_counts)):
            block_id = char_to_block.get(i, -1)
            source = block_source.get(block_id, "self")
            # 无注音块时 fallback 为 "self"（由后续 per-char 自注音补上）
            if block_id < 0:
                source = "self"
            ruby_list = (
                _group_reading_for_character(char_to_ruby_raw[i], count)
                if i in char_to_ruby_raw
                else None
            )
            if _merge_n and ruby_list and len(ruby_list) > 1:
                merged = _merge_trailing_n_ruby_parts(ruby_list)
                if len(merged) != len(ruby_list):
                    ruby_list = merged
                    count = len(merged)
            results.append(
                AutoCheckResult(
                    line_idx=0,  # 将在 analyze_project 中设置
                    char_idx=i,
                    char=char,
                    check_count=count,
                    ruby=ruby_list,
                    origin_block_id=block_id,
                    origin_source=source,
                    origin_morpheme_id=char_to_morpheme.get(i, -1),
                    compound_group_id=char_to_compound_group.get(i, -1),
                )
            )

        return results

    def _is_char_already_rubied(
        self, sentence: Sentence, idx: int
    ) -> bool:
        """判断指定位置的字符是否已被注音。

        规则：
        - char.ruby 非 None 视为已注音。
        - 字符属于某个「连词组」且组内任一字符带 ruby（即整组参与过复合词注音）时，
          即便该字符自身无 ruby（读音由组内首字/他字承载，如 今日 的「日」、
          逆光 拆分后的承载关系），也视为已注音 —— 这类连词成员不应被独立再注音，
          否则会因「单字分配不到读音→回退自注音」而拿到错误的单字训读
          （如「光」被单独注成 ひかり），破坏复合词读音。

        Args:
            sentence: 句子
            idx: 字符索引

        Returns:
            是否已注音
        """
        chars = sentence.characters
        if idx < 0 or idx >= len(chars):
            return False
        if chars[idx].ruby is not None:
            return True
        # 定位 idx 所在的连词组 [start, end]（沿 linked_to_next 链双向扩展）。
        start = idx
        while start > 0 and chars[start - 1].linked_to_next:
            start -= 1
        end = idx
        while end < len(chars) - 1 and chars[end].linked_to_next:
            end += 1
        # 组内任一字符已注音 → 整组视为参与过注音，连词成员一并算已注音。
        return any(chars[k].ruby is not None for k in range(start, end + 1))

    def apply_to_sentence(
        self,
        sentence: Sentence,
        split_config: Optional[SplitConfig] = None,
        keep_existing_timetags: bool = True,
        only_noruby: bool = False,
        apply_user_dict: bool = True,
        restrict_indices: Optional[set] = None,
        skip_romanize: bool = False,
    ) -> None:
        """分析并应用自动检查结果到句子

        构建新的 Character 对象列表，每个字符直接携带自己的 Ruby。
        相比旧的多字符 Ruby 合并方式更简洁。

        Args:
            sentence: 句子
            split_config: 拆分配置
            keep_existing_timetags: 是否保留现有时间标签
            only_noruby: 仅对未注音字符应用（已注音字符的 Ruby/check_count/linked_to_next 保留）
            apply_user_dict: 是否在末尾执行 Phase 5 用户词典覆盖（默认 True）。
                传 False 可推迟词典覆盖到删除注音之后，再手动调用
                :meth:`apply_user_dict_to_project`。
            restrict_indices: 仅对这些字符索引应用分析；其余字符的
                Ruby/check_count/linked_to_next/is_sentence_end/is_line_end 原样保留。None 表示作用于整句。
                与 only_noruby 取并集（任一要求保留即保留）。
        """
        # 预先快照需保留字符的状态：
        #   - restrict_indices 给定时，范围外字符全部保留；
        #   - only_noruby 时，已注音字符保留。
        preserved: Dict[int, Tuple[Optional[Ruby], int, bool, bool, bool]] = {}
        for i in range(len(sentence.characters)):
            keep = False
            if restrict_indices is not None and i not in restrict_indices:
                keep = True
            elif only_noruby and self._is_char_already_rubied(sentence, i):
                keep = True
            if keep:
                c = sentence.characters[i]
                preserved[i] = (c.ruby, c.check_count, c.linked_to_next, c.is_sentence_end, c.is_line_end)
        # 全部字符都需保留 → 无事可做
        if preserved and len(preserved) == len(sentence.characters) and sentence.characters:
            return

        results = self.analyze_sentence(sentence, split_config)

        if not results:
            return

        # 保留现有时间标签和演唱者映射
        old_timestamps: Dict[int, List[int]] = {}
        old_sentence_end_ts: Dict[int, int] = {}
        old_singer_map: Dict[int, str] = {}
        for i, char in enumerate(sentence.characters):
            if char.timestamps:
                old_timestamps[i] = list(char.timestamps)
            if char.sentence_end_ts is not None:
                old_sentence_end_ts[i] = char.sentence_end_ts
            old_singer_map[i] = char.singer_id

        # 构建新的 Character 对象列表
        # 空行（text.strip() 为空）不应被 check_line_end/check_space_as_line_end 强制打停顿点 CP
        _is_blank_line = not sentence.text.strip()
        add_line_end = self._flags.get("check_line_end", True) and not _is_blank_line
        check_space_as_line_end = (
            self._flags.get("check_space_as_line_end", True) and not _is_blank_line
        )

        # 批 18 #9：英文词组末字母自动标停顿点。
        # find_english_words 基于 sentence.text 的字符索引，与 results 一一对应
        # （analyze_sentence 内 chars 由 split_text(text) 产出，逐字符英文路径保持索引对齐）。
        english_sentence_end_idx: set = set()
        english_word_end_idx: set = set()  # 所有英文单词结尾索引（不受开关控制）
        english_word_trailing_comma_idx: set = set()  # 英文单词后紧跟的逗号索引
        check_english_word_end = self._flags.get("check_english_word_end", True)
        for _start, _end, _word in find_english_words(sentence.text):
            _is_single = _end - _start <= 1
            if _end - 1 < len(results):
                english_word_end_idx.add(_end - 1)  # 含单字母词，确保空格豁免生效
                if not _is_single and check_english_word_end:
                    english_sentence_end_idx.add(_end - 1)
            # 收集紧跟英文单词的逗号，避免其被空格规则误标停顿点
            if _end < len(sentence.text) and sentence.text[_end] in _TRAILING_COMMA_CHARS:
                english_word_trailing_comma_idx.add(_end)

        new_characters: List[Character] = []
        for i, result in enumerate(results):
            is_last = i == len(results) - 1
            # 空格视为停顿点：当前字符后面紧跟空格时额外+1
            # 当英文单词结尾停顿点关闭时，英文单词结尾不受空格规则影响
            # 英文单词后的逗号也不受空格规则影响（如 "Smile, Love" 中的逗号）
            is_before_space = (
                not is_last
                and check_space_as_line_end
                and i + 1 < len(results)
                and len(results[i + 1].char) == 1
                and results[i + 1].char.isspace()
                and not (i in english_word_end_idx and not check_english_word_end)
                and i not in english_word_trailing_comma_idx
            )
            extra = 0
            is_sentence_end = False
            if is_last and add_line_end:
                is_sentence_end = True
            if is_before_space:
                is_sentence_end = True
            if i in english_sentence_end_idx:
                is_sentence_end = True
            check_count = result.check_count

            # 每个字符直接携带自己的 Ruby（无需跨字符合并）
            # #11：ruby 为空、或与字符本身相同时不生成 Ruby 对象，
            # 避免 Ruby.__post_init__ 触发空文本异常，并避免导出残留 {a|a}。
            # 例外：长音符号 ー（LONG_VOWEL）即使 reading == char，
            # 也保留自注音 Ruby，使其能携带时序信息并可被「按类型删除」覆盖。
            # Stage 0: result.ruby 为 List[str]（来自 _group_reading_for_character），
            # 映射为 Ruby(parts=[RubyPart(text=s), ...])。
            ruby_groups = result.ruby  # List[str] | None
            _is_long_vowel_char = get_char_type(result.char) == CharType.LONG_VOWEL
            if ruby_groups and (
                self._romanize_ruby
                or not (len(ruby_groups) == 1 and ruby_groups[0] == result.char)
                or _is_long_vowel_char
            ):
                # 处理 rubyPart 数量 > checkCount 的情况
                from strange_uta_game.backend.infrastructure.parsers.inline_format import (
                    split_ruby_for_checkpoints,
                )
                full_text = "".join(ruby_groups)
                if check_count > 0 and len(ruby_groups) > check_count:
                    # rubyPart 数量 > checkCount，使用 split_ruby_for_checkpoints 处理
                    aligned_parts = split_ruby_for_checkpoints(full_text, check_count)
                    ruby_obj = Ruby(parts=[RubyPart(text=p) for p in aligned_parts if p])
                else:
                    ruby_obj = Ruby(parts=[RubyPart(text=g) for g in ruby_groups if g])
                if not ruby_obj.parts:
                    ruby_obj = None
            else:
                ruby_obj = None

            character = Character(
                char=result.char,
                ruby=ruby_obj,
                check_count=check_count,
                is_line_end=(is_last and add_line_end),
                is_sentence_end=is_sentence_end,
                singer_id=old_singer_map.get(i, sentence.singer_id),
            )
            new_characters.append(character)

        # 设置 linked_to_next:
        # - 干净拆分（origin_block_id == -1，字典逗号分段每段非空）→ 不连词
        # - 非干净拆分（origin_block_id >= 0，字典有空读音/fallback）→ 后字无 ruby 才连词
        # - 空格字符不参与连词
        _LINKABLE_SOURCES = {
            "dict", "e2k", "english_fallback", "katakana_english", "fallback", "library",
        }
        for i in range(len(new_characters) - 1):
            next_ch = new_characters[i + 1]
            if next_ch.char and next_ch.char.isspace():
                continue
            cur_ch = new_characters[i]
            if cur_ch.char and cur_ch.char.isspace():
                continue
            cur_src = results[i].origin_source if i < len(results) else "self"
            next_src = (
                results[i + 1].origin_source if i + 1 < len(results) else "self"
            )
            # 仅可连词来源且属于同一个注音块时，才考虑连词
            if not (
                cur_src in _LINKABLE_SOURCES
                and next_src in _LINKABLE_SOURCES
                and results[i].origin_block_id >= 0
                and results[i].origin_block_id
                == results[i + 1].origin_block_id
            ):
                continue
            # 英文词组 / 片假名外来语始终整词连词
            if cur_src in ("e2k", "english_fallback", "katakana_english"):
                new_characters[i].linked_to_next = True
                continue
            # 假名不参与汉字连词（送り仮名/接头假名应独立）
            next_ct = get_char_type(next_ch.char) if len(next_ch.char) == 1 else CharType.OTHER
            if next_ct in (CharType.HIRAGANA, CharType.KATAKANA):
                continue
            # fallback 复合词：整词读音被 _fallback_split_peel_kana 拆到各汉字，
            # 但这些单字读音仅在复合时成立（如 逆光=ぎゃっ+こう、促音便使 逆≠字典 ぎゃく），
            # 故各字虽都带 ruby 仍需保持连词，避免被当成可独立复用的单字读音。
            if cur_src == "fallback":
                new_characters[i].linked_to_next = True
                continue
            # 其他来源（library 等）：后字无 ruby 才连词（无法拆分的情况）
            next_has_ruby = (
                next_ch.ruby is not None
                and (
                    isinstance(next_ch.ruby, list) and any(next_ch.ruby)
                    or hasattr(next_ch.ruby, "parts") and next_ch.ruby.parts
                )
            )
            if not next_has_ruby:
                new_characters[i].linked_to_next = True

        # 恢复时间标签
        if keep_existing_timetags:
            for i, char in enumerate(new_characters):
                if i in old_timestamps:
                    char.timestamps = old_timestamps[i]
                    # 节奏点是否保住由管线后续 update_checkpoints_from_rubies
                    # 的「已持有 timestamps 的字符不可被截断」守卫统一处理
                    #（不区分分析模式，与日文/中文路径一致）
                if i in old_sentence_end_ts:
                    # 停顿点 ts 按原字符位恢复并强制停顿点标记——重分析后
                    # 该位可能不再被行尾/空格规则标记（如英文词中段承接了
                    # 文件的行尾释放）；释放点是行尾时刻（时间事实），不随
                    # 节奏点重算丢弃
                    char.sentence_end_ts = old_sentence_end_ts[i]
                    char.is_sentence_end = True
                    char.push_to_ruby()

        sentence.characters = new_characters

        # 对需保留的字符恢复原 Ruby/check_count/linked_to_next
        # /is_sentence_end/is_line_end
        # （only_noruby 已注音字符 / restrict_indices 范围外字符）。
        # 注意：analyze 过程可能改变字符数量时（当前流程下不会），此覆盖按原位置对齐。
        if preserved:
            for i, (old_ruby, old_cc, old_link, old_sent_end, old_line_end) in preserved.items():
                if i < len(sentence.characters):
                    sentence.characters[i].ruby = old_ruby
                    # 已先恢复 ruby，此时 set_check_count 走 force=True 安全
                    # （ruby.parts 与 old_cc 在原 Character 上本就匹配）
                    sentence.characters[i].set_check_count(old_cc, force=True)
                    sentence.characters[i].linked_to_next = old_link
                    sentence.characters[i].is_sentence_end = old_sent_end
                    sentence.characters[i].is_line_end = old_line_end

        # Phase 5: 用户词典覆盖（优先级最高，覆盖一切包括 only_noruby preserved）。
        # 按词典数组顺序逐条扫描，先命中锁定 span，后命中若与已锁定区间重叠则跳过。
        # 子串严格匹配 sentence 字面文本，不跨 Sentence。
        # apply_user_dict=False 时跳过，由调用方在删除注音后手动调用。
        if apply_user_dict and self._dict:
            char_morpheme_range = _build_compound_ranges(results)
            self._apply_user_dictionary_to_sentence(
                sentence, morpheme_ranges=char_morpheme_range
            )

        if not skip_romanize:
            self._romanize_sentence_ruby(sentence)

    def romanize_project_rubies(self, project: Project, progress_callback=None) -> int:
        """对项目所有句子执行罗马音转换（供外部在 delete 之后调用）。

        Args:
            progress_callback: ``(phase, current, total)`` 进度回调。
        """
        if self._chinese_mode:
            return 0
        if not self._romanize_ruby:
            return 0
        changed = 0
        sentences = project.sentences
        total = len(sentences)
        for i, sentence in enumerate(sentences):
            before = sum(len(part.text) for ch in sentence.characters if ch.ruby for part in ch.ruby.parts)
            self._romanize_sentence_ruby(sentence)
            after = sum(len(part.text) for ch in sentence.characters if ch.ruby for part in ch.ruby.parts)
            if before != after:
                changed += 1
            if progress_callback is not None:
                progress_callback("罗马音转换", i + 1, total)
        return changed

    def _apply_user_dictionary_to_sentence(
        self,
        sentence: Sentence,
        morpheme_ranges: Optional[Dict[int, Tuple[int, int]]] = None,
    ) -> None:
        """Phase 5：把用户词典以子串严格匹配方式覆盖到 sentence.characters 上。

        语义：
          - 词典按 word 长度降序排列（最长匹配优先），同长度保持数组顺序；
          - 对每条 ``(word, reading)``，在 ``sentence`` 字面 ``"".join(c.char ...)``
            上扫描所有不重叠的出现位置；
          - 字符位置若已被更高优先级词条锁定，则跳过；
          - 命中后：解析 ``reading``（annotated 行内格式），为该 span 的每个
            ``Character`` 覆盖 ``ruby`` 和 ``linked_to_next``；
            ``timestamps / check_count / singer_id / is_line_end / is_sentence_end /
            sentence_end_ts / is_rest`` 等字段全部保留；
          - 同一 annotated block 内相邻字符设 ``linked_to_next=True``，
            block 末字符 / 块外字符（无 ruby 段）设 ``linked_to_next=False``。

        Args:
            sentence: 目标句子。
            morpheme_ranges: 可选「char_idx → (group_start, group_end)」映射，
                给出分析器/英文回退原始 morpheme 边界。给定时 Phase 5 用它判定
                「单字词条切碎多字 morpheme」并跳过该命中（如 日→にち 不污染
                一日/日々/毎日）。未给时退回旧逻辑（用 linked_to_next 推断
                连词组，对 Step 3 之后的 fallback 块/clean per-char split 失灵）。
        """
        chars = sentence.characters
        if not chars:
            return
        sentence_text = "".join(c.char for c in chars)
        if not sentence_text:
            return

        # 已被任意词条覆盖的字符索引（防止重叠）
        locked: set[int] = set()

        for word, reading in self._dict:
            if not word or not reading:
                continue
            # 不允许跨字符位置不一致：sentence 字符与 word 字符一一对应（按 Python 字符串索引）
            # sentence.characters 每项 char 字段通常是单字符；若为多字符（如空格段），则
            # 子串匹配仍按 Python str 索引，但映射到 characters 列表时需要按累计长度处理。
            # 这里先用最简单方式：要求 sum(len(c.char)) == len(sentence_text)，
            # 且每个 character 严格 1 字符（项目主流路径成立）。若不满足，跳过。
            char_lens = [len(c.char) for c in chars]
            if any(l != 1 for l in char_lens):
                # 极少数情况：character 存了多字符（旧数据迁移可能出现）。
                # 此时退化为不应用 Phase 5，避免索引错乱。
                return

            parsed = _parse_dict_reading(reading, word)
            if parsed is None:
                # reading 解析失败或 raw != word，跳过该条目
                continue
            per_char_parts, char_block_id = parsed
            if len(per_char_parts) != len(word):
                # 解析出的字符数与 word 不符，跳过
                continue

            # 检查是否需要拦截：当 annotate_katakana_with_english 为 False 时
            if not self._annotate_katakana_with_english:
                # 检查条件1：word 是否含有汉字/平假名（含小平假名），有则放行
                has_kanji_or_hira = any(
                    "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u309f"
                    for c in word
                )
                if not has_kanji_or_hira:
                    # word 中无汉字/平假名 → 视为纯片假名词条，拦截
                    # 检查条件2：reading 中的 ruby 部分是否只有英文、空格和结构化修饰符
                    all_ruby_parts = []
                    for parts in per_char_parts:
                        all_ruby_parts.extend(parts)

                    if all_ruby_parts:
                        is_english_only = all(
                            all(c.isascii() and (c.isalpha() or c.isspace() or c == "|") for c in part)
                            for part in all_ruby_parts
                        )
                        if is_english_only:
                            continue  # 拦截英文读音的片假名词条
                    else:
                        continue  # ruby 为空的纯片假名词条也拦截

            # 找所有不重叠出现位置（贪心从左到右）
            search_from = 0
            wlen = len(word)
            while True:
                idx = sentence_text.find(word, search_from)
                if idx == -1:
                    break
                span = range(idx, idx + wlen)
                if any(i in locked for i in span):
                    # 与已锁定区间重叠 → 整段丢弃，继续找下一处
                    search_from = idx + 1
                    continue
                # ----------------------------------------------------------
                # 防止词典条目部分覆盖已注音的连词组（linked compound）。
                # 例如 "逆光" 被形态素分析拆为 逆=ぎゃっ、光=こう 并保持连词，
                # 此时单字词典条目 "光→ひかり" 不应覆盖 "光" 的读音，因为
                # "光" 此处作为复合词成员，其读音仅在复合词上下文中成立。
                # 仅当词典条目恰好覆盖了连词组的完整范围时才允许覆盖。
                #
                # 优先用 morpheme_ranges（分析器原始 morpheme 边界，独立于 Step 3
                # mora 均分和 char_to_block）；未给时退回 linked_to_next 推断
                # （旧逻辑，对 Step 3 抹掉 block_id 后的 fallback 块、以及
                # clean per-char split 失灵 —— 参见 一日/日々 与 日→にち 的冲突案例）。
                # ----------------------------------------------------------
                def _linked_group_range(pos: int) -> tuple:
                    """返回 pos 所在连词组的 [start, end] 闭区间。"""
                    if morpheme_ranges is not None:
                        rng = morpheme_ranges.get(pos)
                        if rng is not None:
                            return rng
                        # 不在任何多字 morpheme 里 → 单字组（自身一格）
                        return (pos, pos)
                    s = pos
                    while s > 0 and chars[s - 1].linked_to_next:
                        s -= 1
                    e = pos
                    while e < len(chars) - 1 and chars[e].linked_to_next:
                        e += 1
                    return (s, e)

                partial_overlap = False
                for i_pos in span:
                    grp_start, grp_end = _linked_group_range(i_pos)
                    grp_len = grp_end - grp_start + 1
                    if grp_len > 1:
                        # 该字符属于一个多字符连词组
                        # 词典条目必须完整覆盖该连词组，否则视为部分覆盖
                        if idx > grp_start or idx + wlen - 1 < grp_end:
                            partial_overlap = True
                            break
                if partial_overlap:
                    search_from = idx + 1
                    continue
                # 命中：覆盖 chars[idx..idx+wlen]
                for k in range(wlen):
                    ch = chars[idx + k]
                    parts = per_char_parts[k]
                    if parts:
                        ch.ruby = Ruby(parts=[RubyPart(text=p) for p in parts])
                        ch.check_count = len(parts)
                    else:
                        ch.ruby = None
                        ct = get_char_type(ch.char) if len(ch.char) == 1 else CharType.OTHER
                        is_linked = k > 0 and chars[idx + k - 1].linked_to_next
                        if ct == CharType.KANJI or is_linked:
                            ch.check_count = 0
                        elif self._should_make_romaji_self_ruby(ch.char):
                            ch.ruby = Ruby(parts=[RubyPart(text=ch.char)])
                            # 经 setter 收口：原 check_count >= 2 时补占位符，
                            # 而非留下 parts(1) != cc(>=2) 的失配
                            ch.set_check_count(max(ch.check_count, 1), force=True)
                    # linked_to_next：同 block 内相邻字符 → True；否则 False。
                    # 词末字符（k == wlen-1）的 linked_to_next 不在 word 内部决定，
                    # 保守置 False（不连到 word 之外的下一字符）。
                    if k < wlen - 1:
                        same_block = (
                            char_block_id[k] >= 0
                            and char_block_id[k] == char_block_id[k + 1]
                        )
                        ch.linked_to_next = same_block
                    else:
                        ch.linked_to_next = False
                    locked.add(idx + k)
                search_from = idx + wlen

        # 后处理：把用户词典覆盖后的读音传播到相邻的々字符。
        # 々 表「同前字」，当前方汉字被用户词典改变读音后，々 也应跟随。
        # 仅当々的当前读音与前方汉字不同、且不是连浊变体时才覆盖，
        # 以保留分析器正确给出的连浊读法（如 日々→ひび 中 々→び）。
        chars = sentence.characters
        for i in range(1, len(chars)):
            if chars[i].char != "\u3005":
                continue
            prev = chars[i - 1]
            if not prev.ruby or not prev.ruby.parts:
                continue
            prev_reading = "".join(p.text for p in prev.ruby.parts)
            if not prev_reading or prev_reading == prev.char:
                continue
            cur_reading = "".join(p.text for p in chars[i].ruby.parts) if chars[i].ruby else ""
            if cur_reading == prev_reading:
                continue  # 已经一致，无需覆盖
            # 检查々的读音是不是 prev_reading 的连浊/半浊变体：
            # 连浊：首假名从清音变浊音；半浊：は行→ぱ行。
            # 若是有效变体则保留，否则覆盖为 prev_reading。
            if cur_reading and len(cur_reading) == len(prev_reading):
                mutated = prev_reading[0].translate(self._DAKUTEN_MAP)
                mutated_h = prev_reading[0].translate(self._HANDAKUTEN_MAP)
                if cur_reading == mutated + prev_reading[1:] or cur_reading == mutated_h + prev_reading[1:]:
                    continue  # 连浊/半浊变体，保留
            chars[i].ruby = Ruby(parts=[RubyPart(text=prev_reading)])
            chars[i].set_check_count(len(split_into_moras(prev_reading)), force=True)

    def analyze_project(
        self, project: Project, split_config: Optional[SplitConfig] = None
    ) -> List[Tuple[int, List[AutoCheckResult]]]:
        """分析整个项目

        Args:
            project: 项目
            split_config: 拆分配置

        Returns:
            (行索引, 分析结果) 列表
        """
        results = []

        for i, sentence in enumerate(project.sentences):
            sent_results = self.analyze_sentence(sentence, split_config)
            # 更新行索引
            for r in sent_results:
                r.line_idx = i
            results.append((i, sent_results))

        return results

    def apply_to_project(
        self,
        project: Project,
        split_config: Optional[SplitConfig] = None,
        keep_existing_timetags: bool = True,
        only_noruby: bool = False,
        apply_user_dict: bool = True,
        progress_callback=None,
        skip_romanize: bool = False,
    ) -> None:
        """分析并应用到整个项目"""
        sentences = project.sentences
        total = len(sentences)
        if self._chinese_mode:
            for i, sentence in enumerate(sentences):
                self._apply_chinese_to_sentence(sentence, keep_existing_timetags)
                if progress_callback is not None:
                    progress_callback("注音分析", i + 1, total)
            project.shift_selected_checkpoint_if_lost()
            return
        for i, sentence in enumerate(sentences):
            self.apply_to_sentence(
                sentence, split_config, keep_existing_timetags, only_noruby,
                apply_user_dict=apply_user_dict, skip_romanize=skip_romanize,
            )
            if progress_callback is not None:
                progress_callback("注音分析", i + 1, total)
        project.shift_selected_checkpoint_if_lost()

    def apply_user_dict_to_project(
        self, project: Project, skip_romanize: bool = False, progress_callback=None
    ) -> None:
        """对整个项目执行 Phase 5 用户词典覆盖。

        每句重新调用 analyze_sentence 拿到最新的复合词归组信息（compound_group_id），
        构建 morpheme_ranges 后传给 Phase 5，确保保护与 apply_to_sentence 路径一致。

        Args:
            progress_callback: ``(phase, current, total)`` 进度回调。
        """
        if self._chinese_mode:
            return
        if not self._dict:
            return
        sentences = project.sentences
        total = len(sentences)
        for i, sentence in enumerate(sentences):
            results = self.analyze_sentence(sentence)
            morpheme_ranges = _build_compound_ranges(results) or None
            self._apply_user_dictionary_to_sentence(sentence, morpheme_ranges=morpheme_ranges)
            if not skip_romanize:
                self._romanize_sentence_ruby(sentence)
            if progress_callback is not None:
                progress_callback("应用用户词典", i + 1, total)

    def update_checkpoints_from_rubies(
        self,
        sentence: Sentence,
        split_config: Optional[SplitConfig] = None,
        *,
        preserve_ruby_segments: bool = False,
    ) -> None:
        """根据现有注音更新节奏点配置（不重新分析注音）

        仅更新 checkpoint 的 check_count，保留现有的 Ruby 不变。
        在新模型中，每个字符直接持有自己的 Ruby，无需跨字符拆分。

        Args:
            sentence: 句子
            split_config: 拆分配置
            preserve_ruby_segments: True 时信任 ruby.parts 已有分段（来自 nicokara
                解析的"连词/非连词"事实），cc 取 len(ruby.parts) 而非按 mora 总数重算，
                从而避免 set_check_count 重切 parts、丢失 offset_ms。
                仅由 nicokara 导入"保留原有注音"路径使用。
        """
        if not sentence.characters:
            return

        split_config = split_config or SplitConfig()

        # 使用 text_splitter 获取默认节奏点数
        _, check_counts = split_text(sentence.text, split_config)

        # 确保长度匹配
        while len(check_counts) < len(sentence.characters):
            check_counts.append(1)
        check_counts = check_counts[: len(sentence.characters)]

        # 根据现有 per-char 注音更新 check_count
        # 规则：汉字/数字的 cp 严格由它自己的 ruby parts 决定
        #   - 无 ruby → cp=0（典型场景：连词块内后字，mora 已压在首字上）
        #   - 有 ruby 且非自注音 → 按 parts 的 mora 总数
        #   - 自注音（ruby==char）→ 保留默认 cp（走下游过滤规则）
        # 假名/字母/符号：保留默认，由下游过滤规则处理。
        for i, char in enumerate(sentence.characters):
            if len(char.char) != 1 or get_char_type(char.char) not in (
                CharType.KANJI,
                CharType.NUMBER,
            ):
                continue  # 只对汉字和数字按 ruby 重算
            if not char.ruby:
                # 空 ruby 的汉字：cp 默认为 0（连词块内后字不打拍）
                # 但若该字符已持有起始 timestamp（n3 加载后），保留 cp=1
                # 以避免 set_check_count 不变式截断 timestamps（丢失原文件时间戳）
                # 例外：若处于连词块内（沿 linked_to_next 链回溯到块首，
                # 块首带 ruby.parts），则保持 cc=0——这是 nicokara 多 kanji 块
                # "首字吞 ruby"的语义，后续字虽有 body timestamps 但不参与 ruby 行输出。
                # 注意：必须沿链回溯，不能只看前一字（前一字本身可能 ruby=None，
                # 如「高揚感」中「感」前驱「揚」ruby=None，需继续回溯到「高」）。
                in_ruby_block = False
                j = i - 1
                while j >= 0 and sentence.characters[j].linked_to_next:
                    prev = sentence.characters[j]
                    if prev.ruby is not None and len(prev.ruby.parts) > 0:
                        in_ruby_block = True
                        break
                    j -= 1
                if in_ruby_block:
                    check_counts[i] = 0
                else:
                    check_counts[i] = 1 if char.timestamps else 0
                continue
            ruby_groups = [p.text for p in char.ruby.parts]
            if len(ruby_groups) == 1 and char.char == ruby_groups[0]:
                continue  # 自注音汉字（罕见），保留默认
            if preserve_ruby_segments or self._romanize_ruby:
                # 保留原 ruby 分段：cc = parts 段数，这样后续 set_check_count
                # 走 new_count == old_count 路径，不会触发 _resplit_ruby
                # 重切 parts、丢失 offset_ms。
                # 连词块首字示例：友 ruby.parts=[ゆう,じょう] → cc=2；
                # 块内后字 ruby 为空，上面已处理为 cc=0。
                check_counts[i] = len(ruby_groups)
            else:
                # check_n 关闭时：把注音里非起始的 ん/ン 并入前一拍后再数拍，
                # 与 analyze_sentence 的合并规则保持一致，避免刷新节奏点时
                # 把已合并的 険[けん] 又按 mora 展开回 2 拍。
                if bool(self._flags) and not self._flags.get("check_n", False):
                    moras = split_into_moras("".join(ruby_groups))
                    check_counts[i] = len(_merge_trailing_n_ruby_parts(moras))
                else:
                    check_counts[i] = sum(
                        len(split_into_moras(group)) for group in ruby_groups
                    )

        # 单一平假名/片假名封顶：最多 1 cp（同 analyze_sentence）
        chars_for_cap = [c.char for c in sentence.characters]
        for i, ch in enumerate(chars_for_cap):
            if i >= len(check_counts):
                break
            if len(ch) == 1 and get_char_type(ch) in (
                CharType.HIRAGANA,
                CharType.KATAKANA,
            ):
                if check_counts[i] > 1:
                    check_counts[i] = 1

        # 应用自动打勾过滤规则（含 check_line_start 和标点最终覆盖）
        chars = [c.char for c in sentence.characters]
        self._apply_flags_filter(chars, check_counts, sentence.text)

        self._apply_english_and_endpoints(sentence, check_counts)

        # #10: 此函数仅更新节奏点，不改变 linked_to_next。
        # linked_to_next 已由 analyze_sentence/apply_to_sentence 根据注音来源
        # （用户词典/e2k/库函数）正确设置，不应被此函数覆盖。
        # （历史：曾有 "next_ch.check_count != 0 时断开 linked" 的清理逻辑，
        #  但新规则允许"连词不强制后字 cc==0；后字继续展示自己的 ruby"，
        #  该清理会错误断开合法连词 [可,愛]→[い] 此类链，已移除。）

    def update_checkpoints_for_project(
        self,
        project: Project,
        split_config: Optional[SplitConfig] = None,
        *,
        preserve_ruby_segments: bool = False,
        progress_callback=None,
    ) -> None:
        """根据现有注音更新整个项目的节奏点配置（不重新分析注音）

        Args:
            project: 项目
            split_config: 拆分配置
            preserve_ruby_segments: 透传到 update_checkpoints_from_rubies。
            progress_callback: ``(phase, current, total)`` 进度回调。
        """
        if self._chinese_mode:
            return
        sentences = project.sentences
        total = len(sentences)
        for i, sentence in enumerate(sentences):
            self.update_checkpoints_from_rubies(
                sentence, split_config, preserve_ruby_segments=preserve_ruby_segments
            )
            if progress_callback is not None:
                progress_callback("更新节奏点", i + 1, total)

    def analyze_and_apply_pipeline(
        self,
        project: Project,
        *,
        only_noruby: bool = False,
        apply_user_dict: bool = True,
        delete_types: Optional[List[str]] = None,
        update_checkpoints: bool = True,
        progress_callback=None,
    ) -> int:
        """注音分析 →（可选）节奏点更新 → 按类型删除 → 用户词典补回 → 罗马音转换。

        统一全项目注音分析的完整管线，保证所有入口（新建项目加载、手动重新
        分析、全文本编辑界面分析）走同一路径，避免遗漏或顺序不一致。

        Args:
            project: 目标项目。
            only_noruby: 仅对未注音字符应用。
            apply_user_dict: 是否应用用户词典（LLM 模式可关闭）。
            delete_types: 按类型删除注音的类型名列表（如 ``["hiragana"]``）。
                为空或 None 时跳过删除步骤。
            update_checkpoints: True=分析后根据注音重算节奏点（默认）；
                False=只更新注音、保留现有节奏点不动。
            progress_callback: ``(phase, current, total)`` 进度回调。

        Returns:
            按类型删除的注音数量（无删除步骤时返回 0）。
        """
        # 不更新节奏点：注音分析本身（apply_to_project/delete/romanize）会改写
        # check_count，故先快照全项目节奏点数，管线末尾再还原。
        # 同时也快照 is_sentence_end / is_line_end，避免 apply_to_sentence 重建
        # 字符时根据 check_line_end / check_space_as_line_end 自动覆盖。
        saved_cc = None
        saved_se = None
        saved_le = None
        if not update_checkpoints:
            saved_cc = [
                [c.check_count for c in s.characters] for s in project.sentences
            ]
            saved_se = [
                [c.is_sentence_end for c in s.characters] for s in project.sentences
            ]
            saved_le = [
                [c.is_line_end for c in s.characters] for s in project.sentences
            ]

        # Step 1: 注音分析（延迟 romaji，delete 之后再转）
        self.apply_to_project(
            project,
            only_noruby=only_noruby,
            apply_user_dict=(not bool(delete_types)) and apply_user_dict,
            progress_callback=progress_callback,
            skip_romanize=True,
        )

        # Step 2: 根据已有注音更新节奏点（统一 check 规则应用）
        if update_checkpoints:
            self.update_checkpoints_for_project(
                project, progress_callback=progress_callback
            )

        # Step 3: 按类型删除注音
        deleted_count = 0
        if delete_types:
            deleted_count = delete_rubies_by_type_names(
                project, delete_types, progress_callback=progress_callback
            )
            if apply_user_dict:
                self.apply_user_dict_to_project(
                    project, skip_romanize=True, progress_callback=progress_callback
                )

        # Step 4: 罗马音转换（在 delete 之后，只转换剩余的假名注音）
        self.romanize_project_rubies(project, progress_callback=progress_callback)

        # Step 5: 还原节奏点（不更新节奏点模式）。在所有改动之后兜底还原，
        # 权威 setter 会把新注音的 ruby.parts 重新对齐回原节奏点数。
        # 同时还原 is_sentence_end / is_line_end，避免「仅注音」意外改变停顿点标记。
        if saved_cc is not None:
            for s, ccs, ses, les in zip(project.sentences, saved_cc, saved_se, saved_le):
                self._restore_sentence_check_counts(s, ccs)
                for i, ch in enumerate(s.characters):
                    if i < len(ses):
                        ch.is_sentence_end = ses[i]
                    if i < len(les):
                        ch.is_line_end = les[i]

        return deleted_count

    def analyze_and_apply_sentence_pipeline(
        self,
        sentence: Sentence,
        *,
        only_noruby: bool = False,
        restrict_indices: Optional[set] = None,
        apply_user_dict: bool = True,
        update_checkpoints: bool = True,
    ) -> None:
        """单句注音分析 →（可选）节奏点更新。

        统一单句/子集注音分析管线，保证 apply_to_sentence 后一定跟
        update_checkpoints_from_rubies（除非显式关闭）。

        Args:
            sentence: 目标句子。
            only_noruby: 仅对未注音字符应用。
            restrict_indices: 仅对这些字符索引应用分析。
            apply_user_dict: 是否应用用户词典。
            update_checkpoints: True=分析后重算节奏点（默认）；
                False=只更新注音、保留现有节奏点不动。
        """
        # 不更新节奏点：apply_to_sentence 内部会按新注音重写 check_count，
        # 故先快照整句节奏点数，分析后还原（覆盖全句，确保节奏点完全不动）。
        # 同时也快照 is_sentence_end / is_line_end。
        saved_cc = None
        saved_se = None
        saved_le = None
        if not update_checkpoints:
            saved_cc = [c.check_count for c in sentence.characters]
            saved_se = [c.is_sentence_end for c in sentence.characters]
            saved_le = [c.is_line_end for c in sentence.characters]

        self.apply_to_sentence(
            sentence,
            only_noruby=only_noruby,
            restrict_indices=restrict_indices,
            apply_user_dict=apply_user_dict,
        )
        if update_checkpoints:
            self.update_checkpoints_from_rubies(sentence)
        elif saved_cc is not None:
            self._restore_sentence_check_counts(sentence, saved_cc)
            for i, ch in enumerate(sentence.characters):
                if i < len(saved_se):
                    ch.is_sentence_end = saved_se[i]
                if i < len(saved_le):
                    ch.is_line_end = saved_le[i]

    @staticmethod
    def _restore_sentence_check_counts(sentence: Sentence, saved: List[int]) -> None:
        """把句中字符的 check_count 还原到 saved（供「不更新节奏点」复用）。

        仅对实际发生变化的字符调用权威 setter——setter 会按旧节奏点数重新对齐
        新注音的 ruby.parts（缩小合并尾段 / 放大补占位），从而做到「注音更新、
        节奏点不动」。未变化的字符跳过，避免无谓重切 parts。
        """
        for i, ch in enumerate(sentence.characters):
            if i < len(saved) and ch.check_count != saved[i]:
                ch.set_check_count(saved[i], force=True)

    def estimate_check_count(self, text: str) -> int:
        """估算文本的节奏点数量

        Args:
            text: 输入文本

        Returns:
            估算的节奏点数量
        """
        if not text:
            return 0

        try:
            results = self._analyzer.analyze(text)

            count = 0
            for result in results:
                # 汉字：注音假名数量
                if self._is_kanji(result.text[0]):
                    count += len(result.reading)
                # 假名：1 个
                elif self._is_kana(result.text[0]):
                    count += 1

            return count

        except Exception:
            # 如果分析失败，返回字符数作为保守估计
            return len(text)

    @staticmethod
    def _is_kanji(char: str) -> bool:
        """检查是否是汉字"""
        code = ord(char)
        return (
            (0x4E00 <= code <= 0x9FFF)
            or (0x3400 <= code <= 0x4DBF)
            or (0xF900 <= code <= 0xFAFF)
        )

    @staticmethod
    def _is_kana(char: str) -> bool:
        """检查是否是假名"""
        code = ord(char)
        return (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF)


# ── 配置类型名 → CharType 映射 ──
_RUBY_TYPE_NAME_MAP: Dict[str, CharType] = {
    "hiragana": CharType.HIRAGANA,
    "katakana": CharType.KATAKANA,
    "kanji": CharType.KANJI,
    "alphabet": CharType.ALPHABET,
    "number": CharType.NUMBER,
    "symbol": CharType.SYMBOL,
    "long_vowel": CharType.LONG_VOWEL,
    "sokuon": CharType.SOKUON,
    "other": CharType.OTHER,
    "space": CharType.SPACE,
    "full_space": CharType.FULL_SPACE,
}

_SMALL_HIRAGANA = set("ぁぃぅぇぉゃゅょゎ")
_SMALL_KATAKANA = set("ァィゥェォャュョヮゕゖ")


def get_kanji_linked_indices(characters: list) -> set:
    """返回"处于含汉字连词链中"的所有字符索引集合。

    连词链由 ``linked_to_next`` 构成：``ch.linked_to_next=True`` 表示该字符与
    下一字符在同一连词块内。对每条连续链，若链内存在任意汉字字符，则链内所有
    字符索引均纳入保护集合——删除注音时这些字符视为汉字，不删除其注音。

    Args:
        characters: ``Sentence.characters`` 列表。

    Returns:
        需要保护的字符索引集合（``set[int]``）。
    """
    n = len(characters)
    if n == 0:
        return set()

    # 先构建连词链：连续的 linked_to_next=True 把相邻字符串联成一组
    protected: set = set()
    i = 0
    while i < n:
        # 找到从 i 开始的连词链尾部
        j = i
        while j < n - 1 and characters[j].linked_to_next:
            j += 1
        # 链覆盖 [i..j]
        if j > i:
            chain = characters[i : j + 1]
            if any(get_char_type(ch.char) == CharType.KANJI for ch in chain):
                protected.update(range(i, j + 1))
        i = j + 1

    return protected


def _ruby_is_all_hiragana(ruby_text: str) -> bool:
    """注音文本是否全为平假名（范围 U+3040-U+309F）。"""
    return bool(ruby_text) and all("぀" <= c <= "ゟ" for c in ruby_text)


def delete_rubies_by_type_names(
    project: "Project", type_names: List[str], progress_callback=None
) -> int:
    """按字符类型名称列表删除注音。

    与 DeleteRubyByTypeDialog 的逻辑保持一致：
    - 勾选 HIRAGANA → 同时移除促音 っ
    - katakana_hiragana_ruby → 删除注音全为平假名的片假名字符（含ッ）
    - katakana_english_ruby  → 删除注音含非平假名内容的片假名字符（含ッ）
    - 与汉字处于同一连词链中的字符视为汉字：选择汉字时删除，选择其他类型时保留。

    Args:
        project: 项目
        type_names: 类型名称列表，如 ["hiragana", "katakana_hiragana_ruby"]
        progress_callback: ``(phase, current, total)`` 进度回调。

    Returns:
        删除的注音数量
    """
    ct_selected = [_RUBY_TYPE_NAME_MAP[n] for n in type_names if n in _RUBY_TYPE_NAME_MAP]
    delete_kata_hira = "katakana_hiragana_ruby" in type_names
    delete_kata_eng = "katakana_english_ruby" in type_names

    if not ct_selected and not delete_kata_hira and not delete_kata_eng:
        return 0

    extended = set(ct_selected)
    if "space" in type_names or "full_space" in type_names:
        # 配置层只展示一个“空格”选项；兼容预发布版本的 full_space 键。
        extended.update((CharType.SPACE, CharType.FULL_SPACE))
    if CharType.HIRAGANA in ct_selected:
        extended.add(CharType.SOKUON)

    removed = 0
    sentences = project.sentences
    total = len(sentences)
    for si, sentence in enumerate(sentences):
        kanji_linked = get_kanji_linked_indices(sentence.characters)
        for idx, ch in enumerate(sentence.characters):
            if not ch.ruby:
                continue
            # 含汉字的连词块应整体按汉字类型匹配。此前这里无条件跳过，导致即使
            # 用户明确选择“汉字”，连词块上的注音也始终无法删除。
            ct = CharType.KANJI if idx in kanji_linked else get_char_type(ch.char)

            # 片假名家族：片假名本体 + 片假名形式的促音 ッ（sokuon 未被显式选中时）
            # 和长音符号 ー（long_vowel 未被显式选中时）。hiragana 等其他类型不应
            # 波及这些片假名性质的字符；若用户显式选 sokuon/long_vowel 类型，
            # 则走后面 ct in extended 的通用路径，确保能被独立删除。
            is_kata_family = (
                ct == CharType.KATAKANA
                or (ct == CharType.SOKUON and ch.char == "ッ" and CharType.SOKUON not in ct_selected)
                or (ct == CharType.LONG_VOWEL and CharType.LONG_VOWEL not in ct_selected)
            )
            if is_kata_family:
                if delete_kata_hira or delete_kata_eng:
                    is_hira = _ruby_is_all_hiragana(ch.ruby.text)
                    if (is_hira and delete_kata_hira) or (not is_hira and delete_kata_eng):
                        ch.set_ruby(None)
                        ch.linked_to_next = False
                        if idx > 0:
                            sentence.characters[idx - 1].linked_to_next = False
                        removed += 1
                continue

            if ct in extended:
                if ct == CharType.SOKUON and ch.char == "っ" and CharType.HIRAGANA not in ct_selected:
                    continue
                ch.set_ruby(None)
                # 英文自动解析把整个单词作为一个连词块；其 ruby 通常只挂在
                # 首字母。删除“英文字母”注音时只移除读音，必须保留单词边界，
                # 否则 ``magic`` 会在全文本中断成 ``[T]m{agic||...}``。
                # 其他字符类型沿用旧行为：删除 ruby 的同时拆开连词。
                if ct != CharType.ALPHABET:
                    ch.linked_to_next = False
                    if idx > 0:
                        sentence.characters[idx - 1].linked_to_next = False
                removed += 1
        if progress_callback is not None:
            progress_callback("删除注音", si + 1, total)

    return removed
