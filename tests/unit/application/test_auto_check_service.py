"""AutoCheckService 测试。"""

import pytest
from strange_uta_game.backend.application import AutoCheckService
from strange_uta_game.backend.domain import Sentence
from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    DummyAnalyzer,
    RubyAnalyzer,
    RubyResult,
)


class _FixedReadingAnalyzer(RubyAnalyzer):
    """按固定映射给单字注音的测试分析器（其余字符自注音）。"""

    def __init__(self, mapping):
        self._mapping = mapping

    def analyze(self, text):
        return [
            RubyResult(
                text=ch,
                reading=self._mapping.get(ch, ch),
                start_idx=i,
                end_idx=i + 1,
            )
            for i, ch in enumerate(text)
        ]

    def get_reading(self, text):
        return "".join(self._mapping.get(ch, ch) for ch in text)


class TestMergeNRubyParts:
    """check_n 关闭时，汉字注音里非起始的 ん/ン 并入前一拍。"""

    _FLAGS_BASE = {"kanji": True, "hiragana": True, "katakana": True}

    def _run(self, text, mapping, check_n):
        flags = dict(self._FLAGS_BASE, check_n=check_n)
        svc = AutoCheckService(
            _FixedReadingAnalyzer(mapping), auto_check_flags=flags
        )
        s = Sentence.from_text(text, "s1")
        svc.apply_to_sentence(s)
        return [
            (c.char, c.check_count, [p.text for p in c.ruby.parts] if c.ruby else None)
            for c in s.characters
        ]

    def test_n_merged_when_check_n_off(self):
        # 険[けん]：check_n 关闭 → ん 并入 け，1 拍 [けん]
        out = self._run("険", {"険": "けん"}, check_n=False)
        assert out[0] == ("険", 1, ["けん"])

    def test_n_kept_when_check_n_on(self):
        # check_n 开启 → ん 单独成拍，2 拍 [け|ん]
        out = self._run("険", {"険": "けん"}, check_n=True)
        assert out[0] == ("険", 2, ["け", "ん"])

    def test_multiple_kanji_each_merged(self):
        # 人間[にん][げん]：各自合并为 1 拍
        out = self._run("人間", {"人": "にん", "間": "げん"}, check_n=False)
        assert out[0] == ("人", 1, ["にん"])
        assert out[1] == ("間", 1, ["げん"])

    def test_non_n_reading_unaffected(self):
        # 康[こう]：长音 う 不是 ん，保持 2 拍
        out = self._run("康", {"康": "こう"}, check_n=False)
        assert out[0] == ("康", 2, ["こ", "う"])

    def test_update_checkpoints_keeps_merge(self):
        # 已合并的 険[けん] 经 update_checkpoints_from_rubies 刷新后仍是 1 拍
        flags = dict(self._FLAGS_BASE, check_n=False)
        svc = AutoCheckService(
            _FixedReadingAnalyzer({"険": "けん"}), auto_check_flags=flags
        )
        s = Sentence.from_text("険", "s1")
        svc.apply_to_sentence(s)
        svc.update_checkpoints_from_rubies(s)
        assert s.characters[0].check_count == 1
        assert [p.text for p in s.characters[0].ruby.parts] == ["けん"]


def _get_sudachi_analyzer():
    """获取跨平台的真实注音分析器。"""
    from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
        SudachiAnalyzer,
    )

    return SudachiAnalyzer()


class TestAutoCheckService:
    """测试自动检查服务"""

    def test_analyze_sentence_simple(self):
        """测试分析简单日文"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("赤い花", "s1")

        results = service.analyze_sentence(sentence)

        assert len(results) == 3
        assert results[0].char == "赤"
        assert results[1].char == "い"
        assert results[2].char == "花"

    def test_apply_to_sentence(self):
        """测试应用自动检查结果"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("赤い花", "s1")

        service.apply_to_sentence(sentence)

        # 验证 chars 被设置
        assert sentence.chars == ["赤", "い", "花"]

        # 验证 characters 被创建
        assert len(sentence.characters) == 3

        # 验证最后一个字符标记为停顿点
        assert sentence.characters[2].is_line_end

    def test_mixed_consecutive_spaces_do_not_shift_ruby_results(self):
        service = AutoCheckService(
            _FixedReadingAnalyzer({"君": "きみ", "届": "とど"})
        )
        text = "magic magic! \u3000君に届くよぅに"
        sentence = Sentence.from_text(text, "s1")

        service.apply_to_sentence(sentence)

        normalized = "magic magic!\u3000君に届くよぅに"
        assert sentence.text == normalized
        kimi = sentence.characters[normalized.index("君")]
        todoku = sentence.characters[normalized.index("届")]
        assert kimi.char == "君"
        assert [part.text for part in kimi.ruby.parts] == ["き", "み"]
        assert todoku.char == "届"
        assert [part.text for part in todoku.ruby.parts] == ["と", "ど"]

    def test_estimate_check_count(self):
        """测试估算节奏点数量"""
        service = AutoCheckService(DummyAnalyzer())

        count = service.estimate_check_count("赤い花")

        # DummyAnalyzer 直接返回字符数
        assert count == 3

    def test_keep_existing_timetags(self):
        """测试保留现有时间标签"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("赤い花", "s1")
        sentence.characters[0].add_timestamp(1000)

        # 应用自动检查（保留时间标签）
        service.apply_to_sentence(sentence, keep_existing_timetags=True)

        # 验证时间标签保留
        assert len(sentence.characters[0].timestamps) == 1

    def test_clear_existing_timetags(self):
        """测试清除现有时间标签"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("赤い花", "s1")
        sentence.characters[0].add_timestamp(1000)

        # 应用自动检查（清除时间标签）
        service.apply_to_sentence(sentence, keep_existing_timetags=False)

        # 验证时间标签被清除
        assert len(sentence.characters[0].timestamps) == 0

    def test_flags_disable_hiragana(self):
        """测试关闭平假名打勾"""
        flags = {"hiragana": False, "kanji": True}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("赤い花", "s1")

        results = service.analyze_sentence(sentence)

        assert results[1].check_count == 0

    def test_flags_check_line_end_disabled(self):
        """测试关闭行尾打勾"""
        flags = {"check_line_end": False}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("赤い花", "s1")

        service.apply_to_sentence(sentence)

        assert not sentence.characters[-1].is_line_end


class TestA3RootCauseEmptyRubyGroup:
    """批 17 A.3 根因锁定（集成层红测）。

    用户原报错 verbatim：'Ruby 分组存在空组:'お#お#''
    触发：自动分析"大空" 或 "{大|おお}{空|そら}" 均抛异常。
    新 API 下 # 不再作为分组标记，断言改为验证 parts 结构合法。
    """

    def test_ookusora_raw_no_empty_group_exception(self):
        """A.3 端到端：自动分析"大空" 不应抛异常，且每个 part.text 非空"""
        analyzer = _get_sudachi_analyzer()
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("大空", "s1")

        service.apply_to_sentence(sentence)

        for c in sentence.characters:
            if c.ruby:
                assert c.ruby.parts, f"parts 为空: {c.char}"
                for p in c.ruby.parts:
                    assert p.text, f"part.text 为空: char={c.char!r}"

    def test_ookusora_linked_no_empty_group_exception(self):
        """A.3 端到端：自动分析"{大|おお}{空|そら}" 不应抛异常"""
        analyzer = _get_sudachi_analyzer()
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            from_inline_text,
        )

        sentence = from_inline_text("{大|おお}{空|そら}", "s1")
        service = AutoCheckService(analyzer)

        service.apply_to_sentence(sentence)

        for c in sentence.characters:
            if c.ruby:
                assert c.ruby.parts, f"parts 为空: {c.char}"
                for p in c.ruby.parts:
                    assert p.text, f"part.text 为空: char={c.char!r}"


class TestPunctuationCheckpointFlag:
    """#5：标点参与节奏点开关。

    默认关闭：()【】[]{}「」!?、， 等标点 check_count=0；
    开关启用：标点 check_count >= 1。
    """

    def test_punctuation_default_off(self):
        """默认关闭：标点 check_count=0"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("ねえ、君", "s2")
        service.apply_to_sentence(sentence)
        for c in sentence.characters:
            if c.char == "、":
                assert c.check_count == 0, "默认情况下标点 check_count 必须为 0"

    def test_punctuation_enabled(self):
        """「記号（符号）」开启：标点（符号）check_count >= 1"""
        service = AutoCheckService(
            DummyAnalyzer(),
            auto_check_flags={"symbol": True},
        )
        sentence = Sentence.from_text("ねえ、君", "s1")
        service.apply_to_sentence(sentence)
        for c in sentence.characters:
            if c.char == "、":
                assert c.check_count >= 1, "开关启用时标点 check_count 必须 >= 1"

    def test_punctuation_full_set_default_off(self):
        """覆盖完整 PUNCTUATION_SET：默认全部为 0"""
        from strange_uta_game.backend.domain import PUNCTUATION_SET

        text = "あ" + "".join(PUNCTUATION_SET) + "い"
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text(text, "s1")
        service.apply_to_sentence(sentence)
        for c in sentence.characters:
            if c.char in PUNCTUATION_SET:
                assert c.check_count == 0, f"标点 {c.char!r} 默认 check_count 必须为 0"


class TestSelectedCheckpointShift:
    """选中 cp 在 check_count 缩减后顺延。"""

    def test_shift_within_char_truncates(self):
        from strange_uta_game.backend.domain import Project, Sentence

        project = Project()
        sentence = Sentence.from_text("赤い花", "s1")
        for c in sentence.characters:
            c.check_count = 3
        project.sentences.append(sentence)
        project.set_selected_checkpoint(0, 0, 2)
        # 缩减
        sentence.characters[0].check_count = 1
        project.shift_selected_checkpoint_if_lost()
        loc = project.get_selected_checkpoint()
        assert loc == (0, 0, 0), f"应截断到 cp 0，实际 {loc}"

    def test_shift_to_next_char_when_zero(self):
        from strange_uta_game.backend.domain import Project, Sentence

        project = Project()
        sentence = Sentence.from_text("、あ", "s1")
        sentence.characters[0].check_count = 1  # 标点先有 cp
        sentence.characters[1].check_count = 1
        project.sentences.append(sentence)
        project.set_selected_checkpoint(0, 0, 0)
        # 模拟开关关闭 → 标点变 0
        sentence.characters[0].check_count = 0
        project.shift_selected_checkpoint_if_lost()
        loc = project.get_selected_checkpoint()
        assert loc == (0, 1, 0), f"应顺延到下一字符 cp 0，实际 {loc}"

    def test_shift_clears_when_all_lost(self):
        from strange_uta_game.backend.domain import Project, Sentence

        project = Project()
        sentence = Sentence.from_text("、", "s1")
        sentence.characters[0].check_count = 1
        project.sentences.append(sentence)
        project.set_selected_checkpoint(0, 0, 0)
        sentence.characters[0].check_count = 0
        project.shift_selected_checkpoint_if_lost()
        assert project.get_selected_checkpoint() is None


class TestBlankLineLineStartEndGuard:
    """Q1：空行（text.strip() 为空）不应被 check_line_start/check_line_end 强制打 CP。

    即使 check_line_start=True / check_line_end=True，只要句子文本 strip 后为空，
    行首 CP 和行尾 CP 都必须被压制。这是"空行不自动 CP"语义相对于 line_start/end 的优先级。
    """

    def test_blank_line_whitespace_line_start_suppressed(self):
        """空行（全是空格）+ check_line_start=True → 不强制 max(count,1)"""
        flags = {"check_line_start": True, "space": False}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("   ", "s1")

        results = service.analyze_sentence(sentence)

        for r in results:
            assert r.check_count == 0, f"空行首字符不应被强制 CP，实际 {r.check_count}"

    def test_blank_line_whitespace_line_end_suppressed(self):
        """空行（全是空格）+ check_line_end=True → is_line_end/is_sentence_end 均为 False"""
        flags = {"check_line_end": True, "space": False}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("   ", "s1")

        service.apply_to_sentence(sentence)

        assert sentence.characters, "空格行应保留字符"
        for c in sentence.characters:
            assert not c.is_line_end, "空行不应有 is_line_end"
            assert not c.is_sentence_end, "空行不应有 is_sentence_end"

    def test_non_blank_line_line_start_still_works(self):
        """非空行 + check_line_start=True → 首字符 check_count >= 1（回归保护）"""
        flags = {"check_line_start": True}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("あい", "s1")

        results = service.analyze_sentence(sentence)

        assert results[0].check_count >= 1, "非空行首字符应被 check_line_start 强制 >=1"

    def test_non_blank_line_line_end_still_works(self):
        """非空行 + check_line_end=True → 末字符 is_line_end=True（回归保护）"""
        flags = {"check_line_end": True}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("あい", "s1")

        service.apply_to_sentence(sentence)

        assert sentence.characters[-1].is_line_end, "非空行末字符应 is_line_end=True"
        assert sentence.characters[-1].is_sentence_end, "非空行末字符应 is_sentence_end=True"

    def test_blank_line_update_checkpoints_from_rubies_line_start_suppressed(self):
        """update_checkpoints_from_rubies：空行 + check_line_start=True → 首字符 check_count=0"""
        flags = {"check_line_start": True, "space": False}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("   ", "s1")
        for c in sentence.characters:
            c.check_count = 0

        service.update_checkpoints_from_rubies(sentence)

        for c in sentence.characters:
            assert c.check_count == 0, f"空行 update 后不应被强制 CP，实际 {c.check_count}"

    def test_blank_line_update_checkpoints_from_rubies_line_end_suppressed(self):
        """update_checkpoints_from_rubies：空行 + check_line_end=True → is_line_end=False"""
        flags = {"check_line_end": True, "space": False}
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("   ", "s1")

        service.update_checkpoints_from_rubies(sentence)

        for c in sentence.characters:
            assert not c.is_line_end, "空行 update 后不应 is_line_end"
            assert not c.is_sentence_end, "空行 update 后不应 is_sentence_end"

    def test_full_width_space_uses_space_checkpoint_flags(self):
        flags = {
            "space": True,
            "space_after_japanese": True,
        }
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text("あ\u3000", "s1")

        results = service.analyze_sentence(sentence)

        assert results[1].char == "\u3000"
        assert results[1].check_count == 1

    def test_space_ruby_type_removes_half_and_full_width_spaces(self):
        from strange_uta_game.backend.application.auto_check_service import (
            delete_rubies_by_type_names,
        )
        from strange_uta_game.backend.domain import Project
        from strange_uta_game.backend.domain.models import Ruby, RubyPart

        sentence = Sentence.from_text(" \u3000", "s1")
        for char in sentence.characters:
            char.set_ruby(Ruby(parts=[RubyPart(text="^")]))
        project = Project(sentences=[sentence])

        removed = delete_rubies_by_type_names(project, ["space"])

        assert removed == 2
        assert all(char.ruby is None for char in sentence.characters)

    def test_alphabet_ruby_removal_keeps_whole_english_word_linked(self):
        """删除英文注音后，全文本仍应把 magic 作为一个完整连词块。"""
        from strange_uta_game.backend.application.auto_check_service import (
            delete_rubies_by_type_names,
        )
        from strange_uta_game.backend.domain import Project
        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            sentence_to_timed_line,
        )

        service = AutoCheckService(
            DummyAnalyzer(),
            auto_check_flags={
                "check_english_word_end": False,
                "symbol": False,
            },
        )
        sentence = Sentence.from_text("magic!", "s1")
        service.apply_to_sentence(sentence)
        project = Project(sentences=[sentence])

        removed = delete_rubies_by_type_names(project, ["alphabet"])

        assert removed == 1
        assert all(char.ruby is None for char in sentence.characters)
        assert [char.linked_to_next for char in sentence.characters[:5]] == [
            True, True, True, True, False,
        ]
        line, _ = sentence_to_timed_line(sentence.characters)
        assert line == "{magic||[T],,,[T],}![>T]"

    def test_kanji_type_removes_rubies_from_linked_kanji_word(self):
        """含汉字的连词块按汉字类型删除，而不是被无条件保护。"""
        from strange_uta_game.backend.application.auto_check_service import (
            delete_rubies_by_type_names,
        )
        from strange_uta_game.backend.domain import Project
        from strange_uta_game.backend.domain.models import Ruby, RubyPart

        sentence = Sentence.from_text("今日", "s1")
        sentence.characters[0].linked_to_next = True
        for text, char in zip(("きょ", "う"), sentence.characters):
            char.set_ruby(Ruby(parts=[RubyPart(text=text)]))
        project = Project(sentences=[sentence])

        removed = delete_rubies_by_type_names(project, ["kanji"])

        assert removed == 2
        assert all(char.ruby is None for char in sentence.characters)
        assert not any(char.linked_to_next for char in sentence.characters)

    def test_non_kanji_type_keeps_mixed_linked_kanji_word(self):
        """含汉字的混合连词块仍不应被平假名类型误删。"""
        from strange_uta_game.backend.application.auto_check_service import (
            delete_rubies_by_type_names,
        )
        from strange_uta_game.backend.domain import Project
        from strange_uta_game.backend.domain.models import Ruby, RubyPart

        sentence = Sentence.from_text("食べ", "s1")
        sentence.characters[0].linked_to_next = True
        for text, char in zip(("た", "べ"), sentence.characters):
            char.set_ruby(Ruby(parts=[RubyPart(text=text)]))
        project = Project(sentences=[sentence])

        removed = delete_rubies_by_type_names(project, ["hiragana"])

        assert removed == 0
        assert all(char.ruby is not None for char in sentence.characters)
        assert sentence.characters[0].linked_to_next


class TestFallbackSplitPeelKana:
    """连词回退：头尾假名剥离策略（_fallback_split_peel_kana）"""

    def test_kana_suffix_preserved(self):
        """可愛い / かわいい → 头剥可=か、尾剥假名い，剩余わい分配给中间的愛。

        pykakasi 提供单字候选（可=か），故能头部剥离；剥离后只剩中间字「愛」时，
        把剩余读音直接给它，而不是把整串读音倒灌首字（旧「首字全吃」语义）。
        """
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("可愛い", "かわいい")
        assert parts == ["か", "わい", "い"], f"应分配到各字，实际 {parts}"

    def test_pure_kanji_no_candidates_first_char_all(self):
        """无候选回退：XYZ/ABC → 首字全吃（保留原语义）"""
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("XYZ", "ABC")
        assert parts == ["ABC", "", ""], f"无候选应首字全吃，实际 {parts}"

    def test_kana_prefix_and_suffix_preserved(self):
        """お可愛い / おかわいい → 头剥お+可、尾剥假名い，剩余わい给中间的愛。"""
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("お可愛い", "おかわいい")
        assert parts == ["お", "か", "わい", "い"], f"应分配到各字，实际 {parts}"

    def test_compound_sokuon_keeps_peeled_suffix(self):
        """逆光 / ぎゃっこう：尾剥光=こう，剩余ぎゃっ给逆（即便字典只有ぎゃく）。

        复合词促音便使「逆」读 ぎゃっ 而非字典单字读音 ぎゃく，此前的「读音不在
        候选中→首字全吃」会把整串 ぎゃっこう 倒灌「逆」、清空「光」。现在应保留
        已成功剥离的 光=こう。
        """
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("逆光", "ぎゃっこう")
        assert parts == ["ぎゃっ", "こう"], f"应保留尾剥 光=こう，实际 {parts}"

    def test_single_char_returns_full_reading(self):
        """单字：直接返回 reading"""
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("漢", "かん")
        assert parts == ["かん"]

    def test_empty_reading_fallback(self):
        """空 reading：全空"""
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("漢字", "")
        assert parts == ["", ""]

    def test_tail_peel_does_not_eat_everything(self):
        """坩堝 / るつぼ：堝 的 kun 字典恰为整词读音 るつぼ（历史标注法），
        尾剥不应一口吃光、把首字清空。

        历史 bug：堝.kun = ['るつぼ'] → tail_peel(るつぼ, 候选='るつぼ') 命中
        → split=["", "るつぼ"]，与「首字吃全部剩余」的兜底语义相反。
        修法：剥离后剩余读音长度需 ≥ 左侧未填字符数，否则拒绝该候选 →
        退回 Step 5 → split=["るつぼ", ""]（兜底首字全吃）。
        """
        service = AutoCheckService(DummyAnalyzer())
        parts = service._fallback_split_peel_kana("坩堝", "るつぼ")
        assert parts == ["るつぼ", ""], (
            f"坩堝 应首字全吃（堝.kun=['るつぼ'] 不应吃光），实际 {parts}"
        )

    def test_head_peel_does_not_eat_everything(self):
        """head_peel 对称防过吃：单测 try_match_prefix 不允许吃光剩余字符额度。

        通过直接调 try_match_prefix 验证约束在最底层生效；
        实链路里 head_peel 通常先被 tail_peel/Step 5 绕开，
        但保留这个内层约束可防止未来回归。
        """
        service = AutoCheckService(DummyAnalyzer())
        # 直接构造 head_peel 场景：3 字词，head 候选若一口吃完整 reading
        # （8 字符），剩余 0 < 还需填的 2 → 应被防过吃约束拒绝。
        # 通过 monkeypatch _try_split_to_chars 屏蔽 Step 5。
        service._try_split_to_chars = lambda *a, **k: None
        original_kanji = service._kanji_dict
        try:
            # 三个汉字，首字字典 kun = 整词读音；后两字字典无任何候选 →
            # tail_peel 失败 → head_peel 触发 → 候选 "こうおつてん" 命中
            # 但剩余 0 < 2 → 拒绝 → Step 4 不触发（left=0, right=2）→
            # Step 5 已被屏蔽 → 首字全吃兜底。
            service._kanji_dict = {
                "甲": {"on": [], "kun": ["こうおつてん"]},
                "乙": {"on": [], "kun": []},
                "丙": {"on": [], "kun": []},
            }

            # 同样 monkeypatch _get_single_char_candidates，让 乙/丙 拿不到候选，
            # 避免 pykakasi 给出 おつ/てん 让尾剥逃逸。
            orig = service._get_single_char_candidates
            service._get_single_char_candidates = lambda ch: [] if ch in ("乙", "丙") else orig(ch)
            try:
                parts = service._fallback_split_peel_kana("甲乙丙", "こうおつてん")
                assert parts == ["こうおつてん", "", ""], (
                    f"head_peel 不得吃光全部读音，实际 {parts}"
                )
            finally:
                service._get_single_char_candidates = orig
        finally:
            service._kanji_dict = original_kanji


class TestLibraryBlockFallbackLinking:
    """library 块走 fallback 路径后同块汉字自动连词（端到端）"""

    def test_kawaii_library_block_links_kanji(self):
        """可愛い：无音读字典条目，回退到 fallback，可+愛 连词"""
        analyzer = _get_sudachi_analyzer()
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("可愛い", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 3
        assert chars[0].char == "可"
        assert chars[1].char == "愛"
        assert chars[2].char == "い"
        # 无音读字典条目，回退到 fallback：可+愛 连词（首字承载读音）
        assert chars[0].linked_to_next, "可→愛 应 linked（fallback 连词）"
        assert not chars[1].linked_to_next, "愛→い 不应 linked（い 是独立假名块）"

    def test_daibouken_dict_path_links_all(self):
        """大冒険（字典逗号分隔路径）：读音已拆分，不连词"""
        analyzer = _get_sudachi_analyzer()
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("大冒険", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 3
        # 读音已拆分到各字，不连词
        assert not chars[0].linked_to_next, "大→冒 不应 linked（读音已拆分）"
        assert not chars[1].linked_to_next, "冒→険 不应 linked（读音已拆分）"
        # 各字有自己的 mora ruby
        assert all(c.ruby is not None and len(c.ruby.parts) == 2 for c in chars)

    def test_gyakkou_compound_splits_but_links(self):
        """逆光（ぎゃっこう）：fallback 拆成 逆=ぎゃっ・光=こう，但保持连词。

        促音便使「逆」读 ぎゃっ（≠ 字典 ぎゃく），单字读音仅在复合词里成立，
        故两字虽各带 ruby 仍需连词，避免被当成可独立复用的单字读音、也避免
        整串读音倒灌「逆」而「光」误落回单字训读 ひかり。
        """
        analyzer = _get_sudachi_analyzer()
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("逆光", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 2
        assert [p.text for p in chars[0].ruby.parts] == ["ぎゃっ"], "逆 应为 ぎゃっ"
        assert [p.text for p in chars[1].ruby.parts] == ["こ", "う"], "光 应为 こう"
        assert chars[0].linked_to_next, "逆→光 应连词（复合词读音）"


class TestPhase5CompoundProtection:
    """Phase 5 用户词典不应部分覆盖已注音的连词组（linked compound）。

    场景：「逆光」被形态素分析拆为 逆=ぎゃっ、光=こう 并保持连词。
    默认词典包含 "光→ひかり" 单字条目，Phase 5 不应用该条目覆盖
    复合词中的「光」（因为它只是连词组的部分子串）。
    """

    class _GyakkouAnalyzer(RubyAnalyzer):
        """把「逆光」整词返回 ぎゃっこう（其余字符自注音）。"""

        def analyze(self, text):
            idx = text.find("逆光")
            if idx >= 0:
                results = []
                for i, c in enumerate(text[:idx]):
                    results.append(RubyResult(text=c, reading=c, start_idx=i, end_idx=i + 1))
                results.append(RubyResult(text="逆光", reading="ぎゃっこう", start_idx=idx, end_idx=idx + 2))
                for i, c in enumerate(text[idx + 2:], start=idx + 2):
                    results.append(RubyResult(text=c, reading=c, start_idx=i, end_idx=i + 1))
                return results
            return [
                RubyResult(text=c, reading=c, start_idx=i, end_idx=i + 1)
                for i, c in enumerate(text)
            ]

        def get_reading(self, text):
            return "".join(r.reading for r in self.analyze(text))

    def test_single_char_dict_entry_does_not_override_compound(self):
        """词典 "光→ひかり" 不应覆盖复合词 逆光 中的 光=こう。"""
        user_dict = [
            {"enabled": True, "word": "光", "reading": "{光||ひ|か|り}"},
        ]
        service = AutoCheckService(self._GyakkouAnalyzer(), user_dictionary=user_dict)
        sentence = Sentence.from_text("逆光", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 2
        assert chars[0].ruby is not None, "逆 应有 ruby"
        # 光 应保持复合词读音 こう，而非被词典覆盖为 ひかり
        assert chars[1].ruby is not None, "光 应有 ruby"
        ruby_text = "".join(p.text for p in chars[1].ruby.parts)
        assert ruby_text == "こう", (
            f"光 应保持复合词读音 こう，实际为 {ruby_text}（被词典覆盖为 ひかり）"
        )
        assert chars[0].linked_to_next, "逆→光 应保持连词"

    def test_full_compound_dict_entry_does_override(self):
        """词典 "逆光→ぎゃっこう" 覆盖整个连词组时应允许。"""
        user_dict = [
            {"enabled": True, "word": "逆光", "reading": "{逆光||ぎゃっ,こ|う}"},
        ]
        service = AutoCheckService(self._GyakkouAnalyzer(), user_dictionary=user_dict)
        sentence = Sentence.from_text("逆光", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 2
        assert chars[0].ruby is not None, "逆 应有 ruby"
        assert chars[1].ruby is not None, "光 应有 ruby"
        ruby_text_1 = "".join(p.text for p in chars[1].ruby.parts)
        assert ruby_text_1 == "こう", f"光 应为 こう，实际为 {ruby_text_1}"

    def test_standalone_char_still_gets_dict_reading(self):
        """独立出现的「光」（不在连词组中）仍应被词典 "光→ひかり" 覆盖。"""
        user_dict = [
            {"enabled": True, "word": "光", "reading": "{光||ひ|か|り}"},
        ]
        service = AutoCheckService(DummyAnalyzer(), user_dictionary=user_dict)
        sentence = Sentence.from_text("光", "s1")
        service.apply_to_sentence(sentence)

        chars = sentence.characters
        assert len(chars) == 1
        assert chars[0].ruby is not None, "光 应有 ruby"
        ruby_text = "".join(p.text for p in chars[0].ruby.parts)
        assert ruby_text == "ひかり", f"独立的光应被词典注为 ひかり，实际为 {ruby_text}"


class TestLinkedMemberNotReAnnotated:
    """连词组成员（参与过复合词注音）不应被 only_noruby 独立再注音。

    场景：分析器把整词读音压在首字（如 今日→今=きょう、日=空、连词承载）。
    「日」自身无 ruby，但属于已注音的连词组，必须视为已注音 —— 否则
    only_noruby 会把它当未注音字符独立再注音，拿到错误单字训读（如 光→ひかり），
    破坏复合词读音。
    """

    class _CompoundAnalyzer(RubyAnalyzer):
        """把「今日」整词返回 きょう（其余字符自注音）。"""

        def analyze(self, text):
            if text == "今日":
                return [RubyResult(text="今日", reading="きょう", start_idx=0, end_idx=2)]
            return [
                RubyResult(text=c, reading=c, start_idx=i, end_idx=i + 1)
                for i, c in enumerate(text)
            ]

        def get_reading(self, text):
            return "".join(r.reading for r in self.analyze(text))

    def _kyou_sentence(self):
        # 今日→きょう 整词：きょう 2 拍无法均分到 2 字 → 压在首字「今」、「日」空 ruby、连词
        service = AutoCheckService(self._CompoundAnalyzer())
        sentence = Sentence.from_text("今日", "s1")
        service.apply_to_sentence(sentence)
        return service, sentence

    def test_empty_linked_member_counts_as_rubied(self):
        service, sentence = self._kyou_sentence()
        chars = sentence.characters
        # 前置条件：今 有 ruby 且连词，日 无自身 ruby
        assert chars[0].ruby is not None and chars[0].linked_to_next
        assert chars[1].ruby is None
        # 连词组成员「日」应被判定为已注音
        assert service._is_char_already_rubied(sentence, 1) is True

    def test_only_noruby_preserves_linked_member(self):
        service, sentence = self._kyou_sentence()
        before = [
            (c.char, None if c.ruby is None else [p.text for p in c.ruby.parts],
             c.linked_to_next)
            for c in sentence.characters
        ]
        service.apply_to_sentence(sentence, only_noruby=True)
        after = [
            (c.char, None if c.ruby is None else [p.text for p in c.ruby.parts],
             c.linked_to_next)
            for c in sentence.characters
        ]
        assert after == before, f"only_noruby 不应改动连词组：{before} -> {after}"


class TestEnglishWordCheckpoints:
    """批 18 #9：英文词组节奏点规则
    - 多字母英文词：每个音节起始字母 cp=1，其余字母 cp=0，末字母标 is_sentence_end
    - 适用于自动分析节奏点路径（apply_to_sentence / analyze_sentence）
    - 同时覆盖 e2k 命中（Hello/Happy/honey/day）和 fallback（Heyyyyy）两条分支
    """

    def _apply(self, text: str):
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text(text, "s1")
        service.apply_to_sentence(sentence)
        return sentence.characters

    def _assert_word_rule(
        self, chars, start: int, end: int, label: str, syllable_offsets=(0,)
    ):
        """断言 chars[start:end] 仅在各音节起点设置 checkpoint。"""
        expected = {start + offset for offset in syllable_offsets}
        for i in range(start, end):
            expected_count = 1 if i in expected else 0
            assert chars[i].check_count == expected_count, (
                f"{label}: 字 '{chars[i].char}' (idx={i}) 应 cp={expected_count}, "
                f"实际 {chars[i].check_count}"
            )
        assert chars[end - 1].is_sentence_end, (
            f"{label}: 末字 '{chars[end - 1].char}' 应 is_sentence_end=True"
        )

    def test_hello_world(self):
        """Hello world：两词均按规则；e2k 命中分支"""
        chars = self._apply("Hello world")
        self._assert_word_rule(chars, 0, 5, "Hello", (0, 3))
        self._assert_word_rule(chars, 6, 11, "world")

    def test_hello_comma_world(self):
        """Hello, world：comma 在 'o' 之后，'o' 仍应 is_sentence_end"""
        chars = self._apply("Hello, world")
        self._assert_word_rule(chars, 0, 5, "Hello", (0, 3))
        # comma idx=5，space idx=6，world idx=7..11
        self._assert_word_rule(chars, 7, 12, "world")

    def test_i_love_you(self):
        """I love you：'I' 单字母词跳过规则；love/you 按规则"""
        chars = self._apply("I love you")
        # love: idx 2..6 ('l','o','v','e')
        self._assert_word_rule(chars, 2, 6, "love")
        # you: idx 7..10
        self._assert_word_rule(chars, 7, 10, "you")

    def test_japanese_mixed_english(self):
        """今日はHappy honey day：日文 + 多个英文词混排"""
        text = "今日はHappy honey day"
        chars = self._apply(text)
        # 'Happy' = idx 3..8
        self._assert_word_rule(chars, 3, 8, "Happy", (0, 3))
        # 'honey' = idx 9..14
        self._assert_word_rule(chars, 9, 14, "honey", (0, 3))
        # 'day' = idx 15..18
        self._assert_word_rule(chars, 15, 18, "day")

    def test_one_apple_one_day(self):
        """One apple one day, doctor go away.：每词的末字应 is_sentence_end"""
        text = "One apple one day, doctor go away."
        chars = self._apply(text)
        self._assert_word_rule(chars, 0, 3, "One")
        self._assert_word_rule(chars, 4, 9, "apple", (0, 2))
        self._assert_word_rule(chars, 10, 13, "one")
        self._assert_word_rule(chars, 14, 17, "day")
        self._assert_word_rule(chars, 19, 25, "doctor", (0, 3))
        self._assert_word_rule(chars, 26, 28, "go")
        self._assert_word_rule(chars, 29, 33, "away")

    def test_heyyyyy_fallback(self):
        """Heyyyyy...：OOV 词走 english_fallback 分支，规则同样应用"""
        chars = self._apply("Heyyyyyyyyyyyyyyy")
        self._assert_word_rule(chars, 0, len(chars), "Heyyyyy")


class TestEnglishWordEndSpaceConflict:
    """英文单词结尾停顿点 vs 空格视为停顿点 冲突修复

    当 check_english_word_end=False 时，英文单词结尾不应被空格规则
    （check_space_as_line_end）间接标记为 is_sentence_end。
    英文单词后面通常有空格，如果空格规则仍然生效，
    则关闭英文单词结尾停顿点的设置形同虚设。
    """

    def _apply_with_flags(self, text: str, flags: dict):
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text(text, "s1")
        service.apply_to_sentence(sentence)
        return sentence.characters

    def _update_with_flags(self, text: str, flags: dict):
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags=flags)
        sentence = Sentence.from_text(text, "s1")
        for c in sentence.characters:
            c.check_count = 0
        service.update_checkpoints_from_rubies(sentence)
        return sentence.characters

    def test_apply_english_word_end_disabled_not_marked_by_space(self):
        """apply_to_sentence: check_english_word_end=False + check_space_as_line_end=True
        → 英文单词末字母不应 is_sentence_end"""
        flags = {
            "check_space_as_line_end": True,
            "check_english_word_end": False,
        }
        chars = self._apply_with_flags("Hello world today", flags)
        # 'o' (idx 4) 是 "Hello" 末字母，后面有空格，但不应被标停顿点
        assert not chars[4].is_sentence_end, (
            f"'o' (idx=4) 不应 is_sentence_end (check_english_word_end=False), "
            f"实际 {chars[4].is_sentence_end}"
        )
        # 'd' (idx 10) 是 "world" 末字母，后面有空格，也不应被标停顿点
        assert not chars[10].is_sentence_end, (
            f"'d' (idx=10) 不应 is_sentence_end (check_english_word_end=False), "
            f"实际 {chars[10].is_sentence_end}"
        )

    def test_apply_english_word_end_enabled_still_works(self):
        """apply_to_sentence: check_english_word_end=True → 英文单词末字母正常标停顿点"""
        flags = {
            "check_space_as_line_end": True,
            "check_english_word_end": True,
        }
        chars = self._apply_with_flags("Hello world today", flags)
        assert chars[4].is_sentence_end, "'o' 应 is_sentence_end (check_english_word_end=True)"
        assert chars[10].is_sentence_end, "'d' 应 is_sentence_end (check_english_word_end=True)"

    def test_update_english_word_end_disabled_not_marked_by_space(self):
        """update_checkpoints_from_rubies: check_english_word_end=False + check_space_as_line_end=True
        → 英文单词末字母不应 is_sentence_end"""
        flags = {
            "check_space_as_line_end": True,
            "check_english_word_end": False,
        }
        chars = self._update_with_flags("Hello world today", flags)
        assert not chars[4].is_sentence_end, (
            f"update: 'o' (idx=4) 不应 is_sentence_end, 实际 {chars[4].is_sentence_end}"
        )
        assert not chars[10].is_sentence_end, (
            f"update: 'd' (idx=10) 不应 is_sentence_end, 实际 {chars[10].is_sentence_end}"
        )

    def test_non_english_before_space_still_marked(self):
        """非英文字符+空格：空格规则仍然正常工作"""
        flags = {
            "check_space_as_line_end": True,
            "check_english_word_end": False,
        }
        chars = self._apply_with_flags("あいう えお", flags)
        # 'う' (idx 2) 后面是空格，应被标停顿点
        assert chars[2].is_sentence_end, "'う' 后有空格，应 is_sentence_end"

    def test_mixed_japanese_english_conflict(self):
        """日文+英文混排：关闭英文单词结尾时，英文词尾不受空格规则影响"""
        flags = {
            "check_space_as_line_end": True,
            "check_english_word_end": False,
        }
        chars = self._apply_with_flags("今日はHello world", flags)
        # 'お' (idx 2) 后面紧跟 'H'（非空格），不标停顿点 — 正常
        # 'o' (idx 7) 是 "Hello" 末字母，后面有空格，但 check_english_word_end=False
        assert not chars[7].is_sentence_end, (
            f"'o' (idx=7) 不应 is_sentence_end, 实际 {chars[7].is_sentence_end}"
        )



class TestAnalyzeUpdateCheckpointsToggle:
    """`update_checkpoints` 开关：注音管线可选是否重算节奏点。

    用于「不更新节奏点的自动注音分析」入口——只刷注音、保留现有节奏点。
    """

    def test_sentence_pipeline_skips_checkpoint_update(self, monkeypatch):
        """update_checkpoints=False 时不应调用 update_checkpoints_from_rubies。"""
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("赤い花", "s1")

        calls = []
        monkeypatch.setattr(
            service, "update_checkpoints_from_rubies",
            lambda *a, **k: calls.append(1),
        )

        service.analyze_and_apply_sentence_pipeline(
            sentence, update_checkpoints=False
        )
        assert calls == [], "关闭节奏点更新时不应重算节奏点"

        service.analyze_and_apply_sentence_pipeline(
            sentence, update_checkpoints=True
        )
        assert calls == [1], "默认仍应重算节奏点"

    def test_project_pipeline_skips_checkpoint_update(self, monkeypatch):
        """update_checkpoints=False 时不应调用 update_checkpoints_for_project。"""
        from strange_uta_game.backend.domain import Project

        service = AutoCheckService(DummyAnalyzer())
        project = Project()
        project.sentences.append(Sentence.from_text("赤い花", "s1"))

        calls = []
        monkeypatch.setattr(
            service, "update_checkpoints_for_project",
            lambda *a, **k: calls.append(1),
        )

        service.analyze_and_apply_pipeline(project, update_checkpoints=False)
        assert calls == [], "关闭节奏点更新时不应重算节奏点"

        service.analyze_and_apply_pipeline(project, update_checkpoints=True)
        assert calls == [1], "默认仍应重算节奏点"

    def test_sentence_pipeline_preserves_check_count_values(self):
        """不更新节奏点：注音照常更新，但 check_count 还原到分析前。"""
        analyzer = _FixedReadingAnalyzer({"桜": "さくら"})  # 3 mora
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("桜", "s1")
        # 人为设定与注音 mora 数不同的节奏点数
        sentence.characters[0].set_check_count(1, force=True)

        service.analyze_and_apply_sentence_pipeline(
            sentence, update_checkpoints=False
        )

        ch = sentence.characters[0]
        # 注音文本已更新为 さくら
        assert ch.ruby is not None
        assert "".join(p.text for p in ch.ruby.parts) == "さくら"
        # 节奏点数保持 1（未被 3 mora 覆盖），parts 也对齐回 1 段
        assert ch.check_count == 1
        assert len(ch.ruby.parts) == 1

    def test_sentence_pipeline_updates_check_count_when_enabled(self):
        """对照：默认模式下 check_count 会被注音 mora 数刷新。"""
        analyzer = _FixedReadingAnalyzer({"桜": "さくら"})
        service = AutoCheckService(analyzer)
        sentence = Sentence.from_text("桜", "s1")
        sentence.characters[0].set_check_count(1, force=True)

        service.analyze_and_apply_sentence_pipeline(
            sentence, update_checkpoints=True
        )
        assert sentence.characters[0].check_count == 3  # さくら 3 拍

    def test_project_pipeline_preserves_check_count_values(self):
        """项目级不更新节奏点：注音更新但 check_count 还原。"""
        from strange_uta_game.backend.domain import Project

        analyzer = _FixedReadingAnalyzer({"桜": "さくら"})
        service = AutoCheckService(analyzer)
        project = Project()
        sentence = Sentence.from_text("桜", "s1")
        sentence.characters[0].set_check_count(1, force=True)
        project.sentences.append(sentence)

        service.analyze_and_apply_pipeline(project, update_checkpoints=False)

        ch = project.sentences[0].characters[0]
        assert ch.ruby is not None
        assert "".join(p.text for p in ch.ruby.parts) == "さくら"
        assert ch.check_count == 1


class TestTimedCheckpointPreservation:
    """已持有时间戳的节奏点不可被截断——沿用原有的不区分分析模式的守卫
    （_apply_english_and_endpoints），日文/中文路径行为完全一致。

    注意：♫ 等音符记号本身仍受「記号」开关控制——无时间戳时照样清零。
    """

    def test_symbol_without_timestamp_still_filtered(self):
        """♫ 无时间戳：記号开关关闭 → 节奏点照常清零（开关语义不变）。"""
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags={"symbol": False})
        sentence = Sentence.from_text("間奏♫", "s1")

        service.apply_to_sentence(sentence)

        assert sentence.characters[1].check_count == 1  # 奏（漢字）不受影响
        assert sentence.characters[2].check_count == 0  # ♫ 被記号开关清零

    def test_symbol_without_timestamp_filtered_without_flags(self):
        service = AutoCheckService(DummyAnalyzer())
        sentence = Sentence.from_text("♫", "s1")

        service.apply_to_sentence(sentence)

        assert sentence.characters[0].check_count == 0

    def test_pipeline_keeps_timed_symbol_checkpoint_both_modes(self):
        """管线（分析 + 节奏点更新）不区分模式：带文件时间戳的 ♫ 节奏点保留。
        对应 analyze_and_apply_pipeline 的 Step 1（apply_to_sentence）+
        Step 2（update_checkpoints_from_rubies）。
        """
        service = AutoCheckService(DummyAnalyzer(), auto_check_flags={"symbol": False})
        sentence = Sentence.from_text("♫", "s1")
        sentence.characters[0].add_timestamp(27836)

        # Step 1：全部重新分析（only_noruby=False，最严苛场景）
        service.apply_to_sentence(sentence, only_noruby=False)
        # Step 2：节奏点更新（守卫所在步骤）
        service.update_checkpoints_from_rubies(sentence)

        ch = sentence.characters[0]
        assert ch.timestamps == [27836]
        assert ch.check_count == 1

    def test_chinese_path_keeps_timed_symbol_checkpoint_both_modes(self):
        """中文/非假名路径与日文路径一致：带时间戳的 ♫ 节奏点保留。"""
        service = AutoCheckService(
            DummyAnalyzer(), auto_check_flags={"symbol": False}, chinese_mode=True
        )
        sentence = Sentence.from_text("♫", "s1")
        sentence.characters[0].add_timestamp(27836)

        service._apply_chinese_to_sentence(sentence, keep_existing_timetags=True)

        ch = sentence.characters[0]
        assert ch.timestamps == [27836]
        assert ch.check_count == 1

    def test_apply_restores_sentence_end_ts_at_original_position(self):
        """行尾释放 ts 绑在非行末字符位（如英文词中段）时也按位恢复。"""
        analyzer = _FixedReadingAnalyzer({})
        service = AutoCheckService(analyzer, auto_check_flags={"symbol": False})
        # 模拟 ASS 卡拉OK导入：ts 在词首字符，行尾释放绑在词尾
        sentence = Sentence.from_text("today", "s1")
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[4].is_sentence_end = True
        sentence.characters[4].sentence_end_ts = 2500

        service.apply_to_sentence(sentence, keep_existing_timetags=True)

        chars = sentence.characters
        assert chars[0].timestamps == [1000]
        # 词尾不是行末规则标记位也必须保留文件里的释放 ts
        assert chars[4].is_sentence_end is True
        assert chars[4].sentence_end_ts == 2500

    def test_chinese_path_restores_end_ts_and_keeps_timed_symbol(self):
        """中文/非假名模式：释放 ts 按位恢复；带时间戳的 ♫ 节奏点保留。"""
        service = AutoCheckService(
            DummyAnalyzer(), auto_check_flags={"symbol": False}, chinese_mode=True
        )
        sentence = Sentence.from_text("♫", "s1")
        sentence.characters[0].add_timestamp(27836)
        sentence.characters[0].is_sentence_end = True
        sentence.characters[0].sentence_end_ts = 42678

        service._apply_chinese_to_sentence(sentence, keep_existing_timetags=True)

        ch = sentence.characters[0]
        assert ch.timestamps == [27836]
        assert ch.check_count == 1
        assert ch.is_sentence_end is True
        assert ch.sentence_end_ts == 42678
