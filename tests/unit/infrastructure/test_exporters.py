"""导出器测试。"""

import re
import pytest
import tempfile
import os
from pathlib import Path

from strange_uta_game.backend.domain import (
    Project,
    Sentence,
    Singer,
    Character,
    Ruby,
    TimeTagType,
)
from strange_uta_game.backend.infrastructure.exporters import (
    LRCExporter,
    KRAExporter,
    TXTExporter,
    Txt2AssExporter,
    ASSDirectExporter,
    NicokaraExporter,
    get_exporter_by_name,
    get_all_exporters,
    ExportError,
)
from strange_uta_game.backend.application import ExportService
from strange_uta_game.backend.application.export_service import sanitize_export_basename


class TestLRCExporter:
    """测试 LRC 导出器"""

    def test_export_simple(self):
        """测试简单导出"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = LRCExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 增强型 LRC: 行级 [mm:ss.xxx] + 逐字 <mm:ss.xxx>
            assert "[00:12.345]" in content
            assert "<00:12.345>测" in content
            assert "试歌词" in content
        finally:
            os.unlink(temp_path)

    def test_export_with_metadata(self):
        """测试带元数据的导出"""
        project = Project()
        project.metadata.title = "测试歌曲"
        project.metadata.artist = "测试艺术家"

        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = LRCExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "[ti:测试歌曲]" in content
            assert "[ar:测试艺术家]" in content
        finally:
            os.unlink(temp_path)

    def test_export_empty_project_raises_error(self):
        """测试空项目导出报错"""
        project = Project()
        # 不添加歌词行

        exporter = LRCExporter()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".lrc", delete=False) as f:
            temp_path = f.name

        try:
            with pytest.raises(ExportError):
                exporter.export(project, temp_path)
        finally:
            os.unlink(temp_path)


class TestKRAExporter:
    """测试 KRA 导出器"""

    def test_export(self):
        """测试 KRA 导出"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = KRAExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kra", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            assert os.path.exists(temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "测试歌词" in content
        finally:
            os.unlink(temp_path)


class TestTXTExporter:
    """测试 TXT 导出器"""

    def test_export(self):
        """测试 TXT 导出"""
        project = Project()
        project.metadata.title = "测试歌曲"
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = TXTExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "# 测试歌曲" in content
            assert "[001]" in content
            assert "测试歌词" in content
        finally:
            os.unlink(temp_path)


class TestTxt2AssExporter:
    """测试 txt2ass 导出器"""

    def test_export(self):
        """测试 txt2ass 导出"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = Txt2AssExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "# Format: [mm:ss.xx]Lyrics" in content
            assert "[00:12.34]测试歌词" in content
        finally:
            os.unlink(temp_path)


class TestASSDirectExporter:
    """测试 ASS 直接导出器"""

    def test_export(self):
        """测试 ASS 导出"""
        project = Project()
        project.metadata.title = "测试歌曲"
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = ASSDirectExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ass", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "[Script Info]" in content
            assert "Title: 测试歌曲" in content
            assert "[V4+ Styles]" in content
            assert "[Events]" in content
            assert "Dialogue:" in content
        finally:
            os.unlink(temp_path)

    def test_generate_karaoke_text_compound_ruby(self):
        """连词 ruby 导出：{合言葉||あ|い,こ|と,ば} 场景。

        "合" 拥有所有 5 个 ruby part 和 5 个时间戳；"言"、"葉" check_count=0 无时间戳。
        期望输出：合言葉|<あ 出现在首个 \\k 块，言葉不作为 tail 追加到末尾。
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("合言葉", singer.id)
        # "合": 5 个 ruby part，5 个 checkpoint
        ch0 = sentence.characters[0]
        ch0.check_count = 5
        ch0.set_ruby(Ruby(parts=[
            RubyPart(text="あ"),
            RubyPart(text="い"),
            RubyPart(text="こ"),
            RubyPart(text="と"),
            RubyPart(text="ば"),
        ]))
        for i, ts in enumerate([1000, 1130, 1260, 1390, 1520]):
            ch0.add_timestamp(ts, checkpoint_idx=i)
        ch0.linked_to_next = True

        # "言": check_count=0，无时间戳，linked_to_next=True
        ch1 = sentence.characters[1]
        ch1.check_count = 0
        ch1.linked_to_next = True

        # "葉": check_count=0，无时间戳，连词末尾
        ch2 = sentence.characters[2]
        ch2.check_count = 0

        ch2.is_sentence_end = True
        ch2.sentence_end_ts = 1650
        ch2._update_offset_timestamps()
        project.add_sentence(sentence)

        exporter = ASSDirectExporter()
        line_start_ms = sentence.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sentence)
        text = exporter._generate_karaoke_text(sentence, line_start_ms, line_end_ms)

        # "合言葉" 应整体出现在首个 \k 块的注音前
        assert "合言葉|<あ" in text, f"连词 kanji 未整体输出: {text}"

    def test_generate_karaoke_text_strips_placeholder_parts(self):
        """占位 part（停顿符）在 ASS 注音中剥离，该拍输出纯时长段 `#|`。"""
        from strange_uta_game.backend.domain import Ruby, RubyPart

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("寿", singer.id)
        ch0 = sentence.characters[0]
        ch0.check_count = 3
        ch0.set_ruby(
            Ruby(parts=[
                RubyPart(text="す"),
                RubyPart(text="^"),
                RubyPart(text="^"),
            ])
        )
        for i, ts in enumerate([5000, 5150, 5300]):
            ch0.add_timestamp(ts, checkpoint_idx=i)
        project.add_sentence(sentence)

        exporter = ASSDirectExporter()
        line_start_ms = sentence.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sentence)
        text = exporter._generate_karaoke_text(sentence, line_start_ms, line_end_ms)

        assert "寿|<す" in text, text
        # 占位拍退化为纯时长段（#| 后无文字），停顿符不泄漏
        assert "^" not in text, text
        assert text.count("#|") == 2, text
        # "言葉" 不应出现在末尾（tail_text bug）
        assert not text.endswith("言葉{\\k0}"), f"言葉 被错误地追加到末尾: {text}"
        assert "ば言葉" not in text, f"言葉 混入续段尾部: {text}"

    # ── 句中停顿（断句轴点）导出 ──
    # 用户反馈的 bug：句中断句的轴点被合到前一个音，例如
    #   [00:34.43]て[>00:35.01]　{不||[00:35.25]ふ}
    # 旧逻辑输出 {\k82}て　{\k20}不（吞掉轴点），正确输出 {\k58}て{\k24}　{\k20}不。

    @staticmethod
    def _make_pause_space_sentence(singer_id: str) -> "Sentence":
        """て(轴点3501) + 全角空格 + 不(ふ) + 意(い) + に(行尾释放3622)。"""
        from strange_uta_game.backend.domain import Ruby, RubyPart

        sent = Sentence.from_text("て　不意に", singer_id)
        c_te = sent.characters[0]
        c_te.add_timestamp(34430)
        c_te.is_sentence_end = True
        c_te.set_sentence_end_ts(35010)

        sent.characters[2].check_count = 1
        sent.characters[2].set_ruby(Ruby(parts=[RubyPart(text="ふ")]))
        sent.characters[2].add_timestamp(35250)
        sent.characters[3].check_count = 1
        sent.characters[3].set_ruby(Ruby(parts=[RubyPart(text="い")]))
        sent.characters[3].add_timestamp(35450)
        sent.characters[4].add_timestamp(35860)
        sent.characters[4].set_sentence_end_ts(36220)
        return sent

    def test_midline_pause_gap_goes_to_following_space(self):
        r"""轴点后的空格独立成段吃掉间隙：{\k58}て{\k24}　{\k20}不|<ふ。"""
        project = Project()
        singer = project.singers[0]
        sentence = self._make_pause_space_sentence(singer.id)
        for ch in sentence.characters:
            ch.set_offset(0)
        project.add_sentence(sentence)

        exporter = ASSDirectExporter()
        line_start_ms = sentence.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sentence)
        text = exporter._generate_karaoke_text(
            sentence, line_start_ms, line_end_ms, singer_map=None
        )

        expected = (
            "{\\k0}{\\k58}て{\\k24}　{\\k20}不|<ふ{\\k41}意|<い{\\k36}に{\\k0}"
        )
        assert text == expected, f"轴点间隙未切给空格:\n  got: {text}\n  exp: {expected}"

    @staticmethod
    def _make_pause_punct_sentence(singer_id: str) -> "Sentence":
        """奇(き) 跡(せ|き) ？(轴点2620，无 ts) 縁(え|に|し，行尾释放2750)。"""
        from strange_uta_game.backend.domain import Ruby, RubyPart

        sent = Sentence.from_text("奇跡？縁", singer_id)

        sent.characters[0].check_count = 1
        sent.characters[0].set_ruby(Ruby(parts=[RubyPart(text="き")]))
        sent.characters[0].add_timestamp(25680)

        ch_seki = sent.characters[1]
        ch_seki.check_count = 2
        ch_seki.set_ruby(
            Ruby(parts=[RubyPart(text="せ"), RubyPart(text="き")])
        )
        ch_seki.add_timestamp(25910, checkpoint_idx=0)
        ch_seki.add_timestamp(26090, checkpoint_idx=1)

        c_q = sent.characters[2]
        c_q.is_sentence_end = True
        c_q.set_sentence_end_ts(26200)

        ch_en = sent.characters[3]
        ch_en.check_count = 3
        ch_en.set_ruby(
            Ruby(
                parts=[
                    RubyPart(text="え"),
                    RubyPart(text="に"),
                    RubyPart(text="し"),
                ]
            )
        )
        ch_en.add_timestamp(26540, checkpoint_idx=0)
        ch_en.add_timestamp(26710, checkpoint_idx=1)
        ch_en.add_timestamp(26870, checkpoint_idx=2)
        ch_en.set_sentence_end_ts(27500)
        return sent

    def test_midline_pause_punct_even_split_and_gap_block(self):
        r"""轴点前标点与前一音均分时间，间隙输出空文本段：
        {\k6}#|き{\k5}？{\k34}（不再把 ？ 并进注音段吞掉轴点）。
        """
        project = Project()
        singer = project.singers[0]
        sentence = self._make_pause_punct_sentence(singer.id)
        for ch in sentence.characters:
            ch.set_offset(0)
        project.add_sentence(sentence)

        exporter = ASSDirectExporter()
        line_start_ms = sentence.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sentence)
        text = exporter._generate_karaoke_text(
            sentence, line_start_ms, line_end_ms, singer_map=None
        )

        expected = (
            "{\\k0}{\\k23}奇|<き{\\k18}跡|<せ{\\k6}#|き{\\k5}？{\\k34}"
            "{\\k17}縁|<え{\\k16}#|に{\\k63}#|し{\\k0}"
        )
        assert text == expected, (
            f"轴点前标点未均分/未输出间隙段:\n  got: {text}\n  exp: {expected}"
        )

    def test_midline_pause_on_compound_tail(self):
        r"""轴点由连词尾字持有：尾字渲染进 kanji，停顿边界仍生效，
        间隙输出空文本段。{\k100}大冒険{\k50}{\k30}を。
        """
        project = Project()
        singer = project.singers[0]
        sent = Sentence.from_text("大冒険を", singer.id)
        sent.characters[0].add_timestamp(7000)
        sent.characters[0].linked_to_next = True
        sent.characters[1].linked_to_next = True
        sent.characters[2].is_sentence_end = True
        sent.characters[2].set_sentence_end_ts(8000)
        sent.characters[3].add_timestamp(8500)
        sent.characters[3].set_sentence_end_ts(8800)
        for ch in sent.characters:
            ch.set_offset(0)
        project.add_sentence(sent)

        exporter = ASSDirectExporter()
        line_start_ms = sent.global_timing_start_ms
        line_end_ms = exporter._compute_line_end_ms(sent)
        text = exporter._generate_karaoke_text(
            sent, line_start_ms, line_end_ms, singer_map=None
        )

        expected = "{\\k0}{\\k100}大冒険{\\k50}{\\k30}を{\\k0}"
        assert text == expected, (
            f"连词尾字轴点未生效:\n  got: {text}\n  exp: {expected}"
        )


class TestNicokaraExporter:
    """测试 Nicokara 导出器"""

    def test_timestamp_truncates_sub_centisecond_remainder(self):
        from strange_uta_game.backend.infrastructure.exporters.nicokara_exporter import (
            _format_nicokara_ts,
        )

        assert _format_nicokara_ts(19) == "[00:00:01]"

    def test_export_basic(self):
        """测试基本导出：单字符时间戳使用 [MM:SS:CC] 冒号格式"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 冒号分隔的厘秒格式 [00:12:34]
            assert "[00:12:34]" in content
            assert "测" in content
            # 不应包含旧格式的头部或 ASS 标签
            assert "# Nicokara" not in content
            assert "\\k" not in content
        finally:
            os.unlink(temp_path)

    def test_export_per_char_timestamps(self):
        """测试逐字时间戳：每个字符前有 [MM:SS:CC]"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("宝箱", singer.id)
        # 为两个字符分别打轴
        sentence.characters[0].add_timestamp(10330)
        sentence.characters[1].add_timestamp(10780)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 逐字格式: [00:10:33]宝[00:10:78]箱
            assert "[00:10:33]宝" in content
            assert "[00:10:78]箱" in content
        finally:
            os.unlink(temp_path)

    def test_export_line_end_timestamp(self):
        """测试行末结束时间戳"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("あ", singer.id)
        sentence.characters[0].add_timestamp(1000, checkpoint_idx=0)
        sentence.characters[0].set_sentence_end_ts(2000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # [00:01:00]あ[00:02:00]  （字符 + 行末时间戳）
            assert "[00:01:00]あ[00:02:00]" in content
        finally:
            os.unlink(temp_path)

    def test_export_strips_trailing_untagged_whitespace(self):
        """行末无时间戳空格不导出，行末释放 ts 紧跟最后一个有效字符"""
        project = Project()
        singer = project.singers[0]
        # 歌 + 行末悬挂半角空格 + 全角空格（均无时间戳）
        sentence = Sentence.from_text("あ \u3000", singer.id)
        sentence.characters[0].add_timestamp(1000, checkpoint_idx=0)
        sentence.characters[0].is_sentence_end = True
        sentence.characters[0].set_sentence_end_ts(2000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 行末空格被剥掉，释放时间戳仍输出且紧跟「あ」
            assert "[00:01:00]あ[00:02:00]" in content
            assert "あ " not in content
            assert "　" not in content
        finally:
            os.unlink(temp_path)

    def test_export_keeps_midline_whitespace(self):
        """行中间的空格（含全角）不受行末剥离影响"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("あ\u3000い ", singer.id)
        sentence.characters[0].add_timestamp(1000, checkpoint_idx=0)
        sentence.characters[2].add_timestamp(1500, checkpoint_idx=0)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 行中全角空格保留；行末无 ts 半角空格被剥
            assert "[00:01:00]あ　[00:01:50]い" in content
            assert "い " not in content
        finally:
            os.unlink(temp_path)

    def test_export_keeps_timestamped_trailing_space(self):
        """带时间戳的行末空格不剥（纯空格停顿行 [ts1] [ts2] 语义）"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("あ ", singer.id)
        sentence.characters[0].add_timestamp(1000, checkpoint_idx=0)
        sentence.characters[1].add_timestamp(2000, checkpoint_idx=0)
        sentence.characters[1].is_sentence_end = True
        sentence.characters[1].set_sentence_end_ts(3000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 空格自带起始 ts：不剥，连同其行末释放 ts 一起输出
            assert "[00:01:00]あ[00:02:00] [00:03:00]" in content
        finally:
            os.unlink(temp_path)

    def test_export_keeps_trailing_space_with_pause_ts_only(self):
        """只带停顿释放 ts（无起始 ts）的行末空格不剥"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("あ ", singer.id)
        sentence.characters[0].add_timestamp(1000, checkpoint_idx=0)
        sentence.characters[1].is_sentence_end = True
        sentence.characters[1].set_sentence_end_ts(3000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 空格虽无起始 ts，但有停顿释放 ts：保留空格及其释放时间戳
            assert "[00:01:00]あ [00:03:00]" in content
        finally:
            os.unlink(temp_path)

    def test_singer_inheritance_through_trailing_space(self):
        """行末空格块切换演唱者时静默追踪，下一行不再插重复标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        s1 = Sentence.from_text("あ ", singer_a.id)
        s1.characters[0].add_timestamp(1000)
        s1.characters[1].singer_id = singer_b.id  # 行末空格属于 B
        project.add_sentence(s1)

        s2 = Sentence.from_text("い", singer_b.id)
        s2.characters[0].add_timestamp(2000)
        project.add_sentence(s2)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map={singer_a.id: "A", singer_b.id: "B"},
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 行末无时间戳空格被剥；B 的空白块不插标签但更新继承，
            # 第二行继承 B（与解析器「末字符 singer 继承」一致），无重复标签
            assert "[00:01:00]あ" in content
            assert "【B】" not in content
        finally:
            os.unlink(temp_path)

    def test_export_file_extension(self):
        """测试文件扩展名为 .lrc"""
        exporter = NicokaraExporter()
        assert exporter.file_extension == ".lrc"

    def test_singer_tag_skip_whitespace_only_block(self):
        """演唱者切换后如果该演唱者的连续片段全是空白字符，则不插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        singer_map = {singer_a.id: "A", singer_b.id: "B"}

        sentence = Sentence.from_text("X Y", singer_a.id)
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].singer_id = singer_b.id
        sentence.characters[1].add_timestamp(2000)
        sentence.characters[2].add_timestamp(3000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "【B】" not in content
            assert "【A】" in content
        finally:
            os.unlink(temp_path)

    def test_singer_tag_whitespace_block_with_real_content(self):
        """空格后有同演唱者实际字符时仍应插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        singer_map = {singer_a.id: "A", singer_b.id: "B"}

        sentence = Sentence.from_text("X Z", singer_a.id)
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].singer_id = singer_b.id
        sentence.characters[1].add_timestamp(2000)
        sentence.characters[2].singer_id = singer_b.id
        sentence.characters[2].add_timestamp(3000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "【B】" in content
            assert "【A】" in content
        finally:
            os.unlink(temp_path)

    def test_singer_tag_leading_whitespace_block(self):
        """行首空格块属于不同演唱者时也不插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        singer_map = {singer_a.id: "A", singer_b.id: "B"}

        sentence = Sentence.from_text(" X", singer_b.id)
        sentence.characters[0].singer_id = singer_a.id
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].add_timestamp(2000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "【A】" not in content
            assert "【B】" in content
        finally:
            os.unlink(temp_path)

    def test_singer_tag_skip_multi_space_block(self):
        """A字 + B空格 + B空格 + C字，B连续两个空格块不插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        singer_c = Singer(name="C", color="#0000FF")
        project.add_singer(singer_b)
        project.add_singer(singer_c)

        singer_map = {singer_a.id: "A", singer_b.id: "B", singer_c.id: "C"}

        sentence = Sentence.from_text("X  Z", singer_a.id)
        sentence.characters[0].char = "X"
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].singer_id = singer_b.id
        sentence.characters[1].add_timestamp(2000)
        sentence.characters[2].singer_id = singer_b.id
        sentence.characters[2].add_timestamp(3000)
        sentence.characters[3].char = "Z"
        sentence.characters[3].singer_id = singer_c.id
        sentence.characters[3].add_timestamp(4000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "【B】" not in content
            assert "【A】" in content
            assert "【C】" in content
        finally:
            os.unlink(temp_path)

    def test_singer_force_tag_overrides_whitespace(self):
        """force_singer_tag=True 时即使空格块也强制插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_b = Singer(name="B", color="#00FF00")
        project.add_singer(singer_b)

        singer_map = {singer_a.id: "A", singer_b.id: "B"}

        sentence = Sentence.from_text("X Y", singer_a.id)
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].singer_id = singer_b.id
        sentence.characters[1].force_singer_tag = True
        sentence.characters[1].add_timestamp(2000)
        sentence.characters[2].add_timestamp(3000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert "【B】" in content
            assert "【A】" in content
        finally:
            os.unlink(temp_path)

    def test_singer_force_tag_same_singer(self):
        """force_singer_tag 相同演唱者也插入标签"""
        project = Project()
        singer_a = project.singers[0]
        singer_a.name = "A"
        singer_map = {singer_a.id: "A"}

        sentence = Sentence.from_text("XY", singer_a.id)
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[1].force_singer_tag = True
        sentence.characters[1].add_timestamp(2000)
        project.add_sentence(sentence)

        exporter = NicokaraExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project, temp_path,
                singer_ids=None,
                insert_singer_tags=True,
                singer_map=singer_map,
            )

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            assert content.count("【A】") == 2
        finally:
            os.unlink(temp_path)

class TestNicokaraWithRubyExporter:
    """测试带注音的 Nicokara 导出器"""

    def test_export_with_ruby(self):
        """测试 @Ruby 注音标签生成"""
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("赤い", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あか")]))
        sentence.characters[0].add_timestamp(5000)
        sentence.characters[1].add_timestamp(6000)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path, tag_data={})

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # tag_data 为空时 @Offset/@HeadOffset 均不输出（避免 round-trip 污染）
            assert "@Offset" not in content
            assert "@HeadOffset" not in content
            # 应包含 @Ruby 标签（朴素分段：kanji + reading + pos1 + pos2）
            assert "@Ruby1=赤,あか" in content
            # 歌词部分仍为逐字时间戳
            assert "[00:05:00]赤" in content
        finally:
            os.unlink(temp_path)

    def test_export_offset_and_head_offset_tags(self):
        """@Offset/@HeadOffset 标准栏位：非零时按 +ms/-ms 符号格式输出。"""
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("あ", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あ")]))
        sentence.characters[0].add_timestamp(5000)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(
                project,
                temp_path,
                tag_data={"offset": 250, "head_offset": -120, "custom": []},
            )

            with open(temp_path, "r", encoding="utf-8-sig") as f:
                lines = f.read().splitlines()

            assert "@Offset=+250" in lines
            assert "@HeadOffset=-120" in lines
            # 顺序：标准栏位输出在 @Ruby 之前
            offset_idx = next(i for i, l in enumerate(lines) if l.startswith("@Offset="))
            ruby_idx = next(i for i, l in enumerate(lines) if l.startswith("@Ruby1="))
            assert offset_idx < ruby_idx

            # 零值 → 不输出对应标签
            exporter.export(
                project, temp_path, tag_data={"offset": 0, "head_offset": 0}
            )
            with open(temp_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            assert "@Offset" not in content
            assert "@HeadOffset" not in content
        finally:
            os.unlink(temp_path)

    def test_export_ruby_relative_timestamps(self):
        """测试 @Ruby 读音中的相对时间戳"""
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("赤い", singer.id)
        # 设置「赤」的 check_count 为 2（对应读音 あか）
        sentence.characters[0].check_count = 2
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あ"), RubyPart(text="か")]))

        # checkpoint_idx=0 → あ, checkpoint_idx=1 → か
        sentence.characters[0].add_timestamp(5000, checkpoint_idx=0)
        sentence.characters[0].add_timestamp(5150, checkpoint_idx=1)
        sentence.characters[1].add_timestamp(6000)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # @Ruby 读音应包含相对时间戳
            # あ (offset 0) + [00:00:15] (150ms) + か
            assert "あ[00:00:15]か" in content
        finally:
            os.unlink(temp_path)

    def test_export_placeholder_parts_stripped_from_ruby(self):
        """占位符（停顿符）part 在 @Ruby 输出中被剥离，仅留时间戳。

        check_count 多于读音 mora 数时 parts 用停顿符占位（如 す,^,^），
        导出结果须与历史空串占位完全一致：す[ts][ts]，不泄漏 ^。
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("寿司", singer.id)
        sentence.characters[0].check_count = 3
        sentence.characters[0].set_ruby(
            Ruby(parts=[RubyPart(text="す"), RubyPart(text="^"), RubyPart(text="^")])
        )
        sentence.characters[0].add_timestamp(5000, checkpoint_idx=0)
        sentence.characters[0].add_timestamp(5150, checkpoint_idx=1)
        sentence.characters[0].add_timestamp(5300, checkpoint_idx=2)
        sentence.characters[1].set_ruby(Ruby(parts=[RubyPart(text="し")]))
        sentence.characters[1].add_timestamp(6000)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 读音 = す + 两个纯时间戳段（^ 被剥离）
            assert "す[00:00:15][00:00:30]" in content
            # @Ruby 标签行中不应泄漏占位符
            for line in content.splitlines():
                if line.startswith("@Ruby"):
                    assert "^" not in line, line
        finally:
            os.unlink(temp_path)


    def test_export_ruby_multi_reading_disambiguation(self):
        """linked_to_next 切段策略：同 ruby tag 内的多字必须语义构成连词，
        即 `Character.linked_to_next == True` 才能合并；否则必须切为独立 entry。

        本测试构造两个相邻但**未设连词**的单字 ruby（「言」「葉」），
        以及第二句独立的「言」ruby（不同读音）。预期输出 3 个独立 @RubyN：
          - @Ruby1: 言, こ[ts]と  （单字 + 字内相对时间戳）
          - @Ruby2: 葉, ば
          - @Ruby3: 言, ゆ
        三段各带独立位置范围。
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        # 第一句: 言 with reading こ[0]と[163]，葉 with reading ば
        # 两个 ruby 独立、未 linked → 必须切为两个 entry
        s1 = Sentence.from_text("言葉は", singer.id)
        s1.characters[0].check_count = 2
        s1.characters[0].set_ruby(
            Ruby(parts=[RubyPart(text="こ"), RubyPart(text="と")])
        )
        s1.characters[0].add_timestamp(1000, checkpoint_idx=0)
        s1.characters[0].add_timestamp(1163, checkpoint_idx=1)
        # 注意：linked_to_next 默认 False，符合「未设为连词」的语义
        s1.characters[1].set_ruby(Ruby(parts=[RubyPart(text="ば")]))
        s1.characters[1].add_timestamp(1300)
        s1.characters[2].add_timestamp(1500)
        project.add_sentence(s1)

        # 第二句: 言 with reading ゆ (不同的读音)
        s2 = Sentence.from_text("言う", singer.id)
        s2.characters[0].set_ruby(Ruby(parts=[RubyPart(text="ゆ")]))
        s2.characters[0].add_timestamp(5000)
        s2.characters[1].add_timestamp(5200)
        project.add_sentence(s2)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 第一段：「言」单独 entry，reading 含字内相对 ts
            assert re.search(
                r"@Ruby\d+=言,こ\[\d{2}:\d{2}:\d{2}\]と,"
                r"\[00:01:00\],\[00:01:30\]",
                content,
            ), f"未匹配「言」段 (单字+相对 ts):\n{content}"
            # 第二段：「葉」单独 entry
            assert re.search(
                r"@Ruby\d+=葉,ば,\[00:01:30\],\[00:01:50\]",
                content,
            ), f"未匹配「葉」段:\n{content}"
            # 第三段：第二句「言」独立 entry，自身位置范围
            assert re.search(
                r"@Ruby\d+=言,ゆ,\[00:05:00\],\[00:05:20\]",
                content,
            ), f"未匹配第二句「言」段:\n{content}"
            # 严格不允许把「言葉」合并（未 linked）
            assert "@Ruby1=言葉" not in content
            assert "@Ruby2=言葉" not in content
            assert "@Ruby3=言葉" not in content
        finally:
            os.unlink(temp_path)

    def test_export_ruby_linked_merges_into_single_entry(self):
        """linked_to_next=True 正向用例：相邻 ruby 字显式连词，
        必须合并为单一 @RubyN entry（与 disambiguation 测试构成正/反对照）。

        构造「言葉」两字均有 ruby、且 `言.linked_to_next == True`，
        预期输出：
          @Ruby1=言葉,こと[..]ば,[pos1],[pos2]
        其中 pos1 取「言」首 ts，pos2 取「は」(下一字) ts。
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        # 「言葉は」：言+葉 同为 ruby 且 linked → 合并 entry
        sentence = Sentence.from_text("言葉は", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="こと")]))
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[0].linked_to_next = True  # 显式连词
        sentence.characters[1].set_ruby(Ruby(parts=[RubyPart(text="ば")]))
        sentence.characters[1].add_timestamp(1300)
        sentence.characters[2].add_timestamp(1500)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 合并段：亲文字「言葉」，ruby「ことば」（含字间相对 ts），
            # pos1=言首 [00:01:00]，pos2=は开始 [00:01:50]
            assert re.search(
                r"@Ruby\d+=言葉,こと\[\d{2}:\d{2}:\d{2}\]ば,"
                r"\[00:01:00\],\[00:01:50\]",
                content,
            ), f"未匹配 linked 合并 entry:\n{content}"
            # 严格不允许被切成两段
            assert not re.search(r"@Ruby\d+=言,こと", content), (
                f"linked 字被错误切段:\n{content}"
            )
            assert not re.search(r"@Ruby\d+=葉,ば", content), (
                f"linked 字被错误切段:\n{content}"
            )
        finally:
            os.unlink(temp_path)

    def test_export_ruby_linked_allows_internal_singing_pause(self):
        """`is_sentence_end` 表示「演唱停顿」而非语义句末，**不参与**
        ruby 切段判断。即使连词内部某字 is_sentence_end=True，
        只要 linked_to_next=True，整个连词仍合并为单一 @RubyN entry。

        构造「言葉」连词，「言」同时 linked_to_next=True 且
        is_sentence_end=True（演唱时此处有呼吸停顿），预期：
          - 仍合并为单 entry @Ruby1=言葉,...
          - pos2 取「葉」段尾的下一字 ts（不是被「言」的 sentence_end_ts 切断）
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        sentence = Sentence.from_text("言葉は", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="こと")]))
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[0].linked_to_next = True
        # 演唱停顿但仍为连词：is_sentence_end 不应破坏切段
        sentence.characters[0].is_sentence_end = True
        sentence.characters[0].sentence_end_ts = 1200
        sentence.characters[1].set_ruby(Ruby(parts=[RubyPart(text="ば")]))
        sentence.characters[1].add_timestamp(1300)
        sentence.characters[2].add_timestamp(1500)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 合并段：「言葉」仍为单 entry，pos2=「は」起始 ts [00:01:50]
            # 关键反断言：不允许在「言」处被切断 → 不允许出现单独的「言」段
            assert re.search(
                r"@Ruby\d+=言葉,こと\[\d{2}:\d{2}:\d{2}\]ば,"
                r"\[00:01:00\],\[00:01:50\]",
                content,
            ), f"linked + is_sentence_end 被错误切段:\n{content}"
            assert not re.search(r"@Ruby\d+=言,こと", content), (
                f"is_sentence_end 错误地切断了 linked 连词:\n{content}"
            )
        finally:
            os.unlink(temp_path)

    def test_export_ruby_linked_downstream_no_ruby(self):
        """linked_to_next=True 但下游字无 ruby 的场景（如「明日」：
        「明」有 ruby「あした」、linked_to_next=True；
        「日」无 ruby、linked_to_next=False）。

        预期：
          - @Ruby1=明日,あした,[pos1],[pos2]  —— 「日」贡献 kanji 但无 reading
          - **不**出现 @Ruby=明（亲文字只含「明」是错的）
        """
        from strange_uta_game.backend.domain import Ruby, RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        # 「明日は」：明(ruby=あした, linked=True) + 日(ruby=None) + は(无ruby)
        sentence = Sentence.from_text("明日は", singer.id)
        sentence.characters[0].set_ruby(Ruby(parts=[RubyPart(text="あした")]))
        sentence.characters[0].add_timestamp(1000)
        sentence.characters[0].linked_to_next = True
        # 「日」无 ruby，check_count=0，linked=False
        sentence.characters[1].add_timestamp(1300)
        sentence.characters[2].add_timestamp(1500)
        project.add_sentence(sentence)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 亲文字必须是「明日」（含下游无 ruby 字）
            # pos2=[00:01:50] 取段尾字「日」的下一字「は」的 ts=1500ms
            assert re.search(
                r"@Ruby\d+=明日,あした,\[00:01:00\],\[00:01:50\]",
                content,
            ), f"明日 linked 未正确合并:\n{content}"
            # 严格不允许只含「明」
            assert not re.search(r"@Ruby\d+=明,", content), (
                f"「明日」被错误截断为「明」:\n{content}"
            )
        finally:
            os.unlink(temp_path)

    def test_export_ruby_overlap_next_line_closes_scope_at_own_line_end(self):
        """重叠歌词回归（ココ☆ナツ「発」案例）：下一行先于本行结束开始时，
        行尾 ruby 段的 pos2 必须用**本行自身**的行尾释放 ts 闭段，
        不得跨行借用下一行首 ts（否则作用域被压成极窄窗口，
        ニコカラメーカー匹配不到假名注音）。

        行1「爆発☆」：段尾字「発」后只剩无起始 ts 的「☆」（仅带行尾
        释放 ts 01:01.81）；行2 于 00:59.60 重叠插入。预期：
          @RubyN=発,は[00:02:09]つ,[00:59:59],[01:01:81]
        """
        from strange_uta_game.backend.domain import RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        s1 = Sentence.from_text("爆発☆", singer.id)
        c_baku = s1.characters[0]
        c_baku.set_ruby(Ruby(parts=[RubyPart(text="ば")]))
        c_baku.add_timestamp(58710)  # 00:58.71
        c_hatsu = s1.characters[1]
        c_hatsu.check_count = 2
        c_hatsu.set_ruby(Ruby(parts=[RubyPart(text="は"), RubyPart(text="つ")]))
        c_hatsu.add_timestamp(59590, checkpoint_idx=0)  # 00:59.59
        c_hatsu.add_timestamp(61680, checkpoint_idx=1)  # 01:01.68
        c_star = s1.characters[2]
        c_star.is_sentence_end = True
        c_star.set_sentence_end_ts(61810)  # 01:01.81 行尾释放
        project.add_sentence(s1)

        # 行2 在行1 结束（01:01.81）前即开始（00:59.60）—— 重叠
        s2 = Sentence.from_text("サシスセ", singer.id)
        for idx, (yomi, ts) in enumerate([
            ("さ", 59600), ("し", 59940), ("す", 60026), ("せ", 60410),
        ]):
            ch = s2.characters[idx]
            ch.set_ruby(Ruby(parts=[RubyPart(text=yomi)]))
            ch.add_timestamp(ts)
        s2.characters[3].is_sentence_end = True
        s2.characters[3].set_sentence_end_ts(60480)
        project.add_sentence(s2)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 「発」段：pos2 = 本行行尾释放 ts 01:01.81（不是行2 首 ts 00:59.60）
            assert re.search(
                r"@Ruby\d+=発,は\[00:02:09\]つ,\[00:59:59\],\[01:01:81\]",
                content,
            ), f"重叠行尾 ruby 段未用本行释放 ts 闭段:\n{content}"
            assert not re.search(
                r"@Ruby\d+=発,[^,]*,\[00:59:59\],\[00:59:60\]",
                content,
            ), f"「発」段 pos2 仍跨行借用了行2 首 ts:\n{content}"
            # 行2 自身条目不受影响
            assert re.search(
                r"@Ruby\d+=サ,さ,\[00:59:60\],\[00:59:94\]",
                content,
            ), f"行2 ruby 段被误改:\n{content}"
        finally:
            os.unlink(temp_path)

    def test_export_ruby_overlap_deep_no_inverted_scope(self):
        """重叠歌词深度重叠 + 本行完全无释放 ts：跨行回退取到的下一行
        首 ts 早于 pos1 时，钳制 pos2 >= pos1，不得导出倒挂作用域。

        行1 仅「発」（ruby ts 59590，无后续字符无释放）；行2 于 59500
        （更早）开始。跨行回退借到 59500 < pos1 → 钳回 [00:59:59]。
        """
        from strange_uta_game.backend.domain import RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        s1 = Sentence.from_text("発", singer.id)
        s1.characters[0].set_ruby(Ruby(parts=[RubyPart(text="は")]))
        s1.characters[0].add_timestamp(59590)  # 00:59.59
        project.add_sentence(s1)

        s2 = Sentence.from_text("サ", singer.id)
        s2.characters[0].add_timestamp(59500)  # 00:59.50 深度重叠
        project.add_sentence(s2)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # pos2 钳回 pos1，不输出 [00:59:50] 的倒挂区间
            assert re.search(
                r"@Ruby\d+=発,は,\[00:59:59\],\[00:59:59\]",
                content,
            ), f"深度重叠下未钳制倒挂作用域:\n{content}"
        finally:
            os.unlink(temp_path)

    def test_export_ruby_overlap_head_char_pos1_capped_by_own_line(self):
        """重叠歌词 pos1 镜像保护：段首字无起始 ts、跨行向上回退借到的
        「上一行末尾 ts」晚于本行自身首个 ts 时（上一行尚未唱完本行已开唱），
        pos1 钳到本行内文本顺序上其后的第一个 ts，不得晚于本行开始。

        上一行释放于 01:01.81 才结束；本行「☆発」首字「☆」无 ts、
        「発」于 00:59.60 开始。旧逻辑 pos1=01:01:81 > pos2，倒挂；
        新逻辑 pos1 钳到 00:59.60。
        """
        from strange_uta_game.backend.domain import RubyPart
        from strange_uta_game.backend.infrastructure.exporters import (
            NicokaraWithRubyExporter,
        )

        project = Project()
        singer = project.singers[0]

        s0 = Sentence.from_text("爆", singer.id)
        s0.characters[0].add_timestamp(58000)
        s0.characters[0].is_sentence_end = True
        s0.characters[0].set_sentence_end_ts(61810)  # 01:01.81 才释放
        project.add_sentence(s0)

        s1 = Sentence.from_text("☆発", singer.id)
        c_star = s1.characters[0]
        c_star.set_ruby(Ruby(parts=[RubyPart(text="し")]))  # 无起始 ts 的段首字
        c_hatsu = s1.characters[1]
        c_hatsu.set_ruby(Ruby(parts=[RubyPart(text="は")]))
        c_hatsu.add_timestamp(59600)  # 00:59.60
        project.add_sentence(s1)

        exporter = NicokaraWithRubyExporter()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            exporter.export(project, temp_path)

            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()

            # pos1 钳到本行首个 ts 00:59.60，不再倒挂取上一行的 01:01:81
            assert re.search(
                r"@Ruby\d+=☆,し,\[00:59:60\],\[00:59:60\]",
                content,
            ), f"段首字 pos1 未按本行 ts 钳制:\n{content}"
            assert not re.search(
                r"@Ruby\d+=☆,し,\[01:01:81\]",
                content,
            ), f"段首字 pos1 仍跨行借用了上一行末尾 ts:\n{content}"
        finally:
            os.unlink(temp_path)


class TestExporterUtils:
    """测试导出器工具函数"""

    def test_get_exporter_by_name(self):
        """测试根据名称获取导出器"""
        exporter = get_exporter_by_name("LRC (增强型)")
        assert isinstance(exporter, LRCExporter)

        exporter = get_exporter_by_name("KRA")
        assert isinstance(exporter, KRAExporter)

    def test_get_exporter_by_name_legacy(self):
        """测试旧名称 'LRC' 向后兼容"""
        exporter = get_exporter_by_name("LRC")
        assert isinstance(exporter, LRCExporter)

    def test_get_exporter_by_name_invalid(self):
        """测试获取不存在的导出器"""
        with pytest.raises(ValueError):
            get_exporter_by_name("INVALID")

    def test_get_all_exporters(self):
        """测试获取所有导出器"""
        exporters = get_all_exporters()
        assert len(exporters) >= 11

        names = [e.name for e in exporters]
        assert "LRC (增强型)" in names
        assert "LRC (逐行)" in names
        assert "LRC (逐字)" in names
        assert "KRA" in names
        assert "TXT" in names
        assert "SRT" in names


class TestExportService:
    """测试导出服务"""

    def test_get_available_formats(self):
        """测试获取可用格式"""
        service = ExportService()
        formats = service.get_available_formats()

        assert len(formats) >= 7

        lrc_format = next((f for f in formats if f["name"] == "LRC (增强型)"), None)
        assert lrc_format is not None
        assert lrc_format["extension"] == ".lrc"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('song: live? <take>|*', "song_ live_ _take_"),
            ("title. ", "title"),
            ("CON", "_CON"),
            ("com1", "_com1"),
            ("NUL.demo", "_NUL.demo"),
            ("\x00\r\n", "_"),
            ("日本語の曲名", "日本語の曲名"),
        ],
    )
    def test_sanitize_export_basename(self, raw, expected):
        assert sanitize_export_basename(raw) == expected

    def test_sanitize_export_basename_limits_utf16_length(self):
        sanitized = sanitize_export_basename("😀" * 150)

        assert len(sanitized.encode("utf-16-le")) <= 400
        assert sanitized == "😀" * 100

    def test_export(self):
        """测试导出功能"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        sentence.characters[0].add_timestamp(12345)
        project.add_sentence(sentence)

        service = ExportService()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".lrc", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name

        try:
            os.unlink(temp_path)  # 删除临时文件，让服务创建

            result = service.export(project, "LRC (增强型)", temp_path)

            assert result.success is True
            assert result.file_path == temp_path
            assert result.format_name == "LRC (增强型)"

            assert os.path.exists(temp_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_validate_before_export(self):
        """测试导出前验证"""
        project = Project()
        singer = project.singers[0]
        sentence = Sentence.from_text("测试歌词", singer.id)
        # 不添加时间标签
        project.add_sentence(sentence)

        service = ExportService()
        errors = service.validate_before_export(project)

        # 应该提示没有完成打轴
        assert len(errors) > 0
        assert any("没有时间标签" in e for e in errors)
