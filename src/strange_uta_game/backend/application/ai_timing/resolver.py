"""标注优先的发音解析器（AI 打轴阶段 A 领域契约）。

PronunciationResolver 把 Project 展开为 PronunciationPlan，分两步执行：

1. ``collect_existing_annotations``：只读提取项目已有标注（RubyPart 细粒度
   读音 > Ruby 整段读音 > 可自读假名），不做任何推测和写回；
2. ``fill_missing_annotations``：仅为「完全没有读音的缺口字符」调用 SUG
   现有自动注音能力（日语：WinRT/Sudachi/pykakasi 分析器族；中文：拼音
   分析器），生成结果只写入 plan 的执行快照，不触碰工程对象。

任何情况下都不会覆盖、规范化替换或重新切分已有标注——即使自动分析给出
「更标准」的结果（它可能是用户针对唱法手工校正的读音）。
"""

from typing import Dict, List, Optional, Set, Tuple

from strange_uta_game.backend.application.ai_timing.pronunciation import (
    PronunciationPlan,
    PronunciationSource,
    PronunciationUnit,
    ScriptKind,
    compute_annotation_digest,
    script_kind_of,
)
from strange_uta_game.backend.application.auto_check_service import is_chinese_lyrics
from strange_uta_game.backend.domain import DomainError, Project, Sentence
from strange_uta_game.backend.infrastructure.parsers.inline_format import (
    split_ruby_for_checkpoints,
)
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    RubyAnalyzer,
    _group_reading_for_character,
)


class ProjectDriftError(DomainError):
    """collect 与 fill 之间工程的标注摘要发生了变化，执行快照已失效。"""


class PronunciationResolver:
    """把 Project 解析为标注优先的 PronunciationPlan。

    Args:
        analyzer: 日语注音分析器；None 时懒创建（WinRT → Sudachi →
            pykakasi → Dummy 的现有降级链）。
        chinese_analyzer: 中文拼音分析器；None 时懒创建（依赖
            pypinyin + jieba，缺失时记录错误而不抛出）。
        korean_analyzer: 韩文汉字（韩音）分析器；None 时懒创建
            （纯查 hanja_korean.json 表，无外部依赖）。
        chinese_mode: 中文歌词模式（工程级，与 SUG 自动检查口径一致）。
            True/False 显式指定；None 时按整工程文本自动检测
            （is_chinese_lyrics：全工程不含假名即视为中文）。
        korean_mode: 韩文歌词模式（仅影响 AI 打轴的缺口注音路由，
            不触碰 is_chinese_lyrics / 自动检查语义）。None 时自动检测：
            全工程无假名且含谚文即视为韩文。优先级：假名→日语注音；
            无假名含谚文→汉字按韩音；无假名无谚文→汉字按拼音。
    """

    def __init__(
        self,
        analyzer: Optional[RubyAnalyzer] = None,
        chinese_analyzer: Optional[RubyAnalyzer] = None,
        korean_analyzer: Optional[RubyAnalyzer] = None,
        *,
        chinese_mode: Optional[bool] = None,
        korean_mode: Optional[bool] = None,
    ):
        self._analyzer = analyzer
        self._chinese_analyzer = chinese_analyzer
        self._korean_analyzer = korean_analyzer
        self._chinese_mode = chinese_mode
        self._korean_mode = korean_mode

    # ── 对外入口 ──

    def resolve_project(
        self, project: Project, *, fill_missing: bool = False
    ) -> PronunciationPlan:
        """解析工程：先收集既有标注，可选为缺口补足自动注音。"""
        plan = self.collect_existing_annotations(project)
        if fill_missing:
            self.fill_missing_annotations(plan, project)
        return plan

    def collect_existing_annotations(self, project: Project) -> PronunciationPlan:
        """只读提取既有标注，生成 PronunciationPlan（缺口保持 reading=None）。"""
        plan = PronunciationPlan(
            annotation_digest=compute_annotation_digest(project),
            chinese_mode=self._effective_chinese_mode(project),
        )
        for line_idx, sentence in enumerate(project.sentences):
            if getattr(sentence, "aux_role", ""):
                # 翻译/音译/和声不参与主唱 AI 对齐。
                continue
            for char_idx, ch in enumerate(sentence.characters):
                script = script_kind_of(ch.char)
                parts = ch.ruby.parts if ch.ruby else []
                for cp_idx in range(ch.check_count):
                    unit = PronunciationUnit(
                        line_idx=line_idx,
                        char_idx=char_idx,
                        checkpoint_idx=cp_idx,
                        is_sentence_end=False,
                        char_text=ch.char,
                        script=script,
                    )
                    self._assign_existing(unit, ch.char, parts, ch.check_count, script)
                    plan.units.append(unit)
                if ch.is_sentence_end:
                    # 停顿点：无读音、不生成 token，仅保留结构映射
                    plan.units.append(
                        PronunciationUnit(
                            line_idx=line_idx,
                            char_idx=char_idx,
                            checkpoint_idx=ch.check_count,
                            is_sentence_end=True,
                            char_text=ch.char,
                            script=script,
                        )
                    )
        return plan

    def fill_missing_annotations(
        self, plan: PronunciationPlan, project: Project
    ) -> None:
        """仅为缺口字符补足自动注音（原地更新 plan，不修改工程）。

        Raises:
            ProjectDriftError: 工程标注摘要与 plan 创建时不一致。
        """
        current = compute_annotation_digest(project)
        if plan.annotation_digest != current:
            raise ProjectDriftError(
                "工程标注在生成执行快照后发生了变化，请重新执行 AI 打轴"
            )

        chinese = self._effective_chinese_mode(project)
        korean = self._effective_korean_mode(project)
        for line_idx, sentence in enumerate(project.sentences):
            if getattr(sentence, "aux_role", ""):
                continue
            gap_char_idxs = self._gap_chars_for_line(plan, line_idx, sentence)
            if not gap_char_idxs:
                continue
            readings = self._generate_readings(
                sentence, gap_char_idxs, plan, chinese, line_idx, korean
            )
            readings = self._merge_latin_linked_words(
                sentence, readings
            )
            self._apply_generated(plan, line_idx, sentence, readings)

    def _effective_chinese_mode(self, project: Project) -> bool:
        """解析生效的中文模式：显式指定优先，否则按主歌词文本自动检测。"""
        if self._chinese_mode is not None:
            return self._chinese_mode
        return is_chinese_lyrics(self._main_text(project))

    def _effective_korean_mode(self, project: Project) -> bool:
        """解析生效的韩文模式：显式指定优先，否则自动检测（无假名+含谚文）。

        仅路由 AI 打轴缺口注音的分析器选择，不影响 chinese_mode 本身
        （韩文工程无假名，is_chinese_lyrics 仍为 True——汉字读音按内容
        分流：谚文读音走 hangul_to_phonetic，见 alignment 侧）。
        """
        if self._korean_mode is not None:
            return self._korean_mode
        text = self._main_text(project)
        if is_chinese_lyrics(text):
            # 无假名：含谚文即韩文（否则纯中文，走拼音）
            return any("\uac00" <= c <= "\ud7a3" for c in text)
        return False

    @staticmethod
    def _main_text(project: Project) -> str:
        """主歌词文本；翻译/音译/和声等副轴行不参与语言检测。"""
        return "".join(
            s.text
            for s in project.sentences
            if not getattr(s, "aux_role", "")
        )

    # ── 既有标注收集 ──

    @staticmethod
    def _assign_existing(
        unit: PronunciationUnit,
        char_text: str,
        parts: list,
        check_count: int,
        script: ScriptKind,
    ) -> None:
        """按 §4.1 优先级为单个 checkpoint 单元填充既有读音。"""
        if parts:
            if unit.checkpoint_idx < len(parts):
                if (
                    unit.checkpoint_idx == check_count - 1
                    and len(parts) > check_count
                ):
                    # 与 set_check_count 的修复语义一致：多余尾段并入最后一个
                    # checkpoint，保证既有读音完整保留且不丢失。
                    unit.reading = "".join(
                        p.text for p in parts[unit.checkpoint_idx :]
                    )
                else:
                    unit.reading = parts[unit.checkpoint_idx].text
                unit.display_text = parts[unit.checkpoint_idx].text
                unit.source = PronunciationSource.EXISTING_PART
            # part 数量少于 checkpoint 数（旧档失配）：不虚构读音、不重新切分，
            # 该单元保持缺口；此类字符已有部分 Ruby，不属于自动补注音的范围。
            return
        if script in (ScriptKind.KANA, ScriptKind.HANGUL):
            # 假名/促音/长音自身即读音（与 romaji.is_self_romanizable_kana 同义）；
            # 谚文是表音文字，音节块自身同样即读音（罗马化在 token 阶段进行）
            unit.reading = char_text
            unit.display_text = char_text
            unit.source = PronunciationSource.EXISTING_CHARACTER
        # 其余脚本（汉字/拉丁/数字/标点/空白）无既有读音 → 缺口

    # ── 缺口生成 ──

    @staticmethod
    def _gap_chars_for_line(
        plan: PronunciationPlan, line_idx: int, sentence: Sentence
    ) -> List[int]:
        """收集该行需要自动注音的缺口字符索引（已有任何 Ruby 的字符排除）。"""
        gap_chars: Set[int] = set()
        for u in plan.units_for_line(line_idx):
            if not u.is_pending:
                continue
            if u.char_idx >= len(sentence.characters):
                continue
            if sentence.characters[u.char_idx].ruby is not None:
                # 已有 Ruby 但 checkpoint 与 part 失配：不重新切分，保持缺口
                continue
            gap_chars.add(u.char_idx)
        return sorted(gap_chars)

    def _generate_readings(
        self,
        sentence: Sentence,
        gap_char_idxs: List[int],
        plan: PronunciationPlan,
        chinese: bool,
        line_idx: int,
        korean: bool = False,
    ) -> Dict[int, str]:
        """为一行内的缺口字符生成读音，返回 char_idx → 读音。"""
        text = sentence.text
        analyzer = self._resolve_analyzer(chinese, plan, korean)
        if analyzer is None:
            return {}
        try:
            results = analyzer.analyze(text)
        except Exception as exc:  # 分析器故障不应中断整个 plan 的收集
            plan.generation_errors.append(
                f"第 {line_idx + 1} 行自动注音失败：{exc}"
            )
            return {}

        offsets = self._char_offsets(sentence)
        gap_set = set(gap_char_idxs)
        readings: Dict[int, str] = {}

        for result in results:
            if not result.reading:
                continue
            block_len = result.end_idx - result.start_idx
            if block_len <= 0:
                continue
            if block_len == 1:
                split_parts = [result.reading]
            else:
                split_parts = split_ruby_for_checkpoints(
                    result.reading, block_len
                )
            for char_idx in gap_set:
                start, end = offsets[char_idx]
                if start == end:
                    continue
                # 字符区间必须完整落在同一分析块内（多字符 Character 不跨块拆分）
                if start < result.start_idx or end > result.end_idx:
                    continue
                rel = start - result.start_idx
                piece = "".join(split_parts[rel : rel + (end - start)]).strip(",")
                if not piece:
                    continue
                ch = sentence.characters[char_idx]
                script = script_kind_of(ch.char)
                if piece == ch.char and script not in (
                    ScriptKind.LATIN,
                    ScriptKind.NUMBER,
                ):
                    # 汉字/其他脚本拿到自身作读音 = 分析器未解析成功，
                    # 保持缺口（执行前阻断）；拉丁字母与数字的自读音本身
                    # 即 Latn 对齐器可用的读音。
                    continue
                readings[char_idx] = piece
        return readings

    @staticmethod
    def _merge_latin_linked_words(sentence: Sentence, readings: Dict[int, str]) -> Dict[int, str]:
        """拉丁连词整词化（SUG 英文词约定：首字母 1cp、其余 0cp）。

        逐字分析会把 "Take" 的首字母注成 "T"，对齐 transcript 退化成
        单字母序列。此处把 linked_to_next 连成的纯拉丁区间合并为整词，
        读音挂到区间首个带节奏点的字符上（即 SUG 约定的承载字符）。
        假名/汉字连词不受影响（仅纯拉丁区间合并）。
        """
        if not readings:
            return readings
        merged = dict(readings)
        n = len(sentence.characters)

        def _is_latin(ch: str) -> bool:
            return bool(ch) and all(c.isascii() and c.isalpha() for c in ch)

        idx = 0
        while idx < n:
            ch = sentence.characters[idx]
            if not (_is_latin(ch.char) and ch.linked_to_next):
                idx += 1
                continue
            start = idx
            end = idx + 1
            while end < n and sentence.characters[end - 1].linked_to_next:
                end += 1
            span = sentence.characters[start:end]
            if all(_is_latin(c.char) for c in span):
                word = "".join(c.char for c in span)
                # 读音挂到区间内首个出现在 readings 的字符（SUG 约定：
                # 首字母承载节奏点；防御性处理任意成员带点的情况）
                for k in range(start, end):
                    if k in merged:
                        merged[k] = word
                        break
            idx = end
        return merged

    @staticmethod
    def _char_offsets(sentence: Sentence) -> List[Tuple[int, int]]:
        """每个 Character 在 sentence.text 中的 [start, end) 区间。"""
        offsets: List[Tuple[int, int]] = []
        pos = 0
        for ch in sentence.characters:
            n = len(ch.char)
            offsets.append((pos, pos + n))
            pos += n
        return offsets

    def _resolve_analyzer(
        self, chinese: bool, plan: PronunciationPlan, korean: bool = False
    ) -> Optional[RubyAnalyzer]:
        """按工程级语言模式路由分析器（与 SUG 自动检查的 chinese_mode 同义）。

        - 韩文模式（无假名且含谚文）：汉字缺口用韩音查表分析器（纯
          hanja_korean.json 表，词首두음법칙在表值上动态应用）；
        - 中文模式：中文拼音分析器（纯汉字歌词逐字注音）；
        - 非中文模式：日语分析器（形态素上下文；纯汉字日文行不会被误判，
          与「全部注音」按钮不做中文检测的语义一致）。
        """
        if korean:
            if self._korean_analyzer is None:
                try:
                    from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                        create_korean_analyzer,
                    )

                    self._korean_analyzer = create_korean_analyzer()
                except ImportError as exc:
                    plan.generation_errors.append(
                        f"韩文汉字分析器不可用（{exc}），无法为汉字缺口补注音"
                    )
                    return None
            return self._korean_analyzer
        if chinese:
            if self._chinese_analyzer is None:
                try:
                    from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                        create_pinyin_analyzer,
                    )

                    self._chinese_analyzer = create_pinyin_analyzer()
                except ImportError as exc:
                    plan.generation_errors.append(
                        f"中文拼音分析器不可用（{exc}），无法为汉字缺口补注音"
                    )
                    return None
            return self._chinese_analyzer
        if self._analyzer is None:
            from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
                create_analyzer,
            )

            self._analyzer = create_analyzer()
        return self._analyzer

    @staticmethod
    def _apply_generated(
        plan: PronunciationPlan,
        line_idx: int,
        sentence: Sentence,
        readings: Dict[int, str],
    ) -> None:
        """把生成的读音按 checkpoint 分组写入 plan 单元（GENERATED 来源）。"""
        if not readings:
            return
        line_units = plan.units_for_line(line_idx)
        for char_idx, reading in readings.items():
            ch = sentence.characters[char_idx]
            grouped = _group_reading_for_character(reading, ch.check_count)
            for unit in line_units:
                if unit.char_idx != char_idx or unit.is_sentence_end:
                    continue
                if unit.reading is not None:
                    continue
                cp = unit.checkpoint_idx
                if cp < len(grouped) and grouped[cp]:
                    unit.reading = grouped[cp]
                    unit.source = PronunciationSource.GENERATED
                    plan.filled_count += 1
                # 分段不足（如整词英文读音只有一段）时剩余单元保持缺口


__all__ = ["PronunciationResolver", "ProjectDriftError"]
