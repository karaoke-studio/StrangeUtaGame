"""渲染层 IPA 支持测试：渲染数据几何 + 与 Qt 边界查找器交叉验证。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from strange_uta_game.backend.domain import Character, Project, Ruby, RubyPart, Sentence, Singer  # noqa: E402
from strange_uta_game.backend.domain import graphemes as G  # noqa: E402
from strange_uta_game.frontend.editor.timing import karaoke_preview as preview_module  # noqa: E402


# 持有 QApplication 强引用：模块夹具结束时若销毁 QApplication，其析构会
# 级联删除 theme 单例，导致后续测试模块构造控件时崩溃（既有测试同型问题，
# 见 test_karaoke_preview_focus_and_wipe._APP_HOLDER 注释）。
_APP_HOLDER: list = []


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    return app


def _project_from(sentences):
    singer = Singer(name="主唱", color="#FF6B6B", is_default=True)
    proj = Project(sentences=sentences, singers=[singer])
    for s in proj.sentences:
        s.singer_id = singer.id
        for ch in s.characters:
            ch.singer_id = singer.id
    return proj


def _mk(text, ruby_parts=(), *, cc=None, linked=False, ts=None, line_end=False, sent_end=False):
    count = cc if cc is not None else (1 if not ruby_parts else len(ruby_parts))
    ch = Character(
        char=text, check_count=count, linked_to_next=linked,
        is_line_end=line_end, is_sentence_end=sent_end,
    )
    if ruby_parts:
        ch.set_ruby(Ruby(parts=[RubyPart(text=p) for p in ruby_parts]))
    for i, t in enumerate(ts or []):
        ch.add_timestamp(t, checkpoint_idx=i)
    return ch


def _render_data(preview, sentence):
    preview.set_project(_project_from([sentence]))
    preview._sentence_cache.clear()
    entry = preview._get_sentence_render_data(0, sentence, preview._fm_current, "cur")
    return entry


class TestQtCrossValidation:
    """字素簇切分与 Qt QTextBoundaryFinder 在相关区段一致（连结线裁剪除外）。"""

    def test_combining_marks_block(self, qapp):
        from PyQt6.QtCore import QTextBoundaryFinder

        def qt_clusters(s):
            f = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, s)
            out, pos = [], 0
            u16 = s.encode("utf-16-le")
            while True:
                nxt = f.toNextBoundary()
                if nxt is None:
                    break
                if nxt < 0:
                    break
                out.append(u16[pos * 2 : nxt * 2].decode("utf-16-le"))
                pos = nxt
            return out

        samples = []
        # U+0300–036F 全部附加符 × 多种上下文（双基字附加符——连结线等
        # 035C–0362——除外：那是本模块刻意的 IPA 裁剪，见专项用例）
        doubles = {chr(cp) for cp in range(0x035C, 0x0363)}
        for cp in range(0x0300, 0x0370):
            ch = chr(cp)
            if ch in doubles:
                continue
            samples += [ch, "a" + ch, ch + "a", "a" + ch + "b"]
        samples += [
            "a\u0303", "ɛ̃", "n̩", "d̥", "ŋ̊", "e\u0301\u0303", "か\u3099",
            "⛰️", "\U0001F468\u200d\U0001F469\u200d\U0001F467",
            "\U0001F1EF\U0001F1F5", "\u1100\u1161\u11a8", "\u0915\u094d\u0937\u093f",
            "赤い花", "hello", "사랑해",
        ]
        for s in samples:
            mine = G.split_graphemes(s)
            theirs = qt_clusters(s)
            assert "".join(mine) == s
            assert mine == theirs, f"{s!r}: mine={mine} qt={theirs}"

    def test_tie_bar_tailoring_differs_intentionally(self):
        assert G.split_graphemes("t\u0361\u0283") == ["t\u0361\u0283"]


class TestRenderDataGeometry:
    def test_no_sentinel_ink_for_lone_marks(self, qapp, monkeypatch):
        """连词框墨水并集不再被哨兵值（100000）毒化。"""
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        c7 = [
            _mk("p", ["p"], linked=True, ts=[1000]),
            _mk("ɛ", ["e"], linked=True, ts=[1300]),
            _mk("̃", [], cc=0, linked=True),
            _mk("t", ["t"], line_end=True, sent_end=True, ts=[1600]),
        ]
        sentence = Sentence(singer_id="s", characters=c7)
        preview = preview_module.KaraokePreview()
        entry = _render_data(preview, sentence)
        for off in entry["char_ink_offsets"]:
            assert abs(off) < 10000, off

    def _preview_with_distinct_ipa_font(self):
        """显式给 preview 一个与主字体可区分的「IPA 字体」（环境无关）。"""
        from PyQt6.QtGui import QFont, QFontMetrics

        preview = preview_module.KaraokePreview()
        ipa = QFont()
        ipa.setFamilies(["Arial"])
        ipa.setPointSize(preview._font_current.pointSize())
        preview._font_current_ipa = ipa
        preview._fm_current_ipa = QFontMetrics(ipa)
        return preview

    def test_ipa_chars_flagged_for_ipa_font(self, qapp, monkeypatch):
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        sentence = Sentence.from_text("赤い ˈhɛləʊ", singer_id="s")
        preview = self._preview_with_distinct_ipa_font()
        entry = _render_data(preview, sentence)
        uses = entry["char_uses_ipa"]
        # 赤い 与空格不用 IPA 字体；IPA 词内全部用
        assert uses[0] is False and uses[1] is False
        ipa_part = uses[3:]
        assert all(ipa_part), ipa_part

    def test_multi_codepoint_cluster_flagged(self, qapp, monkeypatch):
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        sentence = Sentence.from_text("ãb", singer_id="s")
        preview = self._preview_with_distinct_ipa_font()
        entry = _render_data(preview, sentence)
        assert entry["char_uses_ipa"] == [True, False]

    def test_mixed_line_gates_vector_ink_per_char(self, qapp, monkeypatch):
        """同一行：普通汉字走 tight 像素口径，IPA 簇走矢量轮廓。"""
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        sentence = Sentence.from_text("赤 t͡ʃ", singer_id="s")
        preview = self._preview_with_distinct_ipa_font()
        entry = _render_data(preview, sentence)
        fm_main = preview._fm_current

        assert entry["char_uses_ipa"][0] is False
        cluster_idx = next(
            i for i, flag in enumerate(entry["char_uses_ipa"]) if flag
        )
        tight = preview_module._wipe_ink_bounds(fm_main, "赤")
        assert entry["char_ink_offsets"][0] == tight[0]
        assert entry["char_ink_widths"][0] == tight[1]

        vec = preview_module._vector_ink_bounds(
            preview._font_current_ipa, "t͡ʃ"
        )
        assert vec is not None
        assert entry["char_ink_offsets"][cluster_idx] == vec[0]
        assert entry["char_ink_widths"][cluster_idx] == vec[2]

        busy = fm_main.tightBoundingRect("赤")
        assert entry["line_ink_top"] == min(busy.top(), vec[1])
        assert entry["line_ink_bottom"] == max(busy.bottom(), vec[1] + vec[3])

    def test_vector_ink_kept_when_ipa_font_equals_main_font(
        self, qapp, monkeypatch
    ):
        """主字体==IPA 字体（has_ipa_styling=False）时 IPA 仍须用矢量墨水。"""
        from PyQt6.QtGui import QFont, QFontMetrics

        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        sentence = Sentence.from_text("t͡ʃ", singer_id="s")
        preview = preview_module.KaraokePreview()
        preview._font_current_ipa = QFont(preview._font_current)
        preview._fm_current_ipa = QFontMetrics(preview._font_current_ipa)
        entry = _render_data(preview, sentence)

        assert entry["ipa_styling"] is False
        vec = preview_module._vector_ink_bounds(
            preview._font_current, "t͡ʃ"
        )
        assert vec is not None
        assert entry["char_ink_widths"][0] == vec[2]
        assert entry["line_ink_top"] == vec[1]
        assert entry["line_ink_bottom"] == vec[1] + vec[3]
        # 不能退回字体盒/tight 口径
        assert entry["line_ink_top"] != -preview._fm_current.ascent()

    def test_paint_switches_font_only_with_independent_ipa_face(
        self, qapp, monkeypatch
    ):
        """绘制层也必须遵守 ipa_styling：主字体==IPA 字体时不能切字体。"""
        from PyQt6.QtGui import QFont, QFontMetrics, QImage

        def paint_records(preview, sentence):
            _render_data(preview, sentence)
            real = preview_module._draw_split_text
            records = []

            def spy(painter, x, baseline, ch, colors, y_top, y_bottom):
                font = painter.font()
                records.append(
                    (ch, tuple(font.families()), font.pointSizeF())
                )
                return real(painter, x, baseline, ch, colors, y_top, y_bottom)

            monkeypatch.setattr(preview_module, "_draw_split_text", spy)
            preview.resize(900, 300)
            preview._visible_lines = 1
            preview._current_line_idx = 0
            preview._scroll_center_line = 0.0
            preview.set_current_time_ms(3000)
            image = QImage(preview.size(), QImage.Format.Format_ARGB32)
            image.fill(0)
            preview.render(image)
            return records

        def sample():
            last = _mk(
                "t͡ʃ", [], ts=[1500], line_end=True, sent_end=True
            )
            last.set_sentence_end_ts(2500)
            return Sentence(
                singer_id="s",
                characters=[
                    _mk("赤", [], ts=[1000]),
                    _mk(" ", [], cc=0),
                    last,
                ],
            )

        # 1) 独立 IPA 字体：普通字用主字体，IPA 簇用 IPA 字体
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        preview = self._preview_with_distinct_ipa_font()
        records = paint_records(preview, sample())
        used = {ch: (fams[0], size) for ch, fams, size in records}
        assert used["赤"][0] == preview._font_current.families()[0]
        assert used["t͡ʃ"][0] == preview._font_current_ipa.families()[0]

        # 2) 主字体==IPA 字体（families 相同，仅字号不同，has_ipa_styling=False）：
        #    绘制必须留在主字体，不能因 char_uses_ipa=True 切到 IPA 字体副本
        preview = preview_module.KaraokePreview()
        base = QFont(preview._font_current)
        ipa = QFont(base)
        ipa.setPointSizeF(base.pointSizeF() + 6)
        preview._font_current_ipa = ipa
        preview._fm_current_ipa = QFontMetrics(ipa)
        records = paint_records(preview, sample())
        sizes = {ch: size for ch, _fams, size in records}
        assert sizes["赤"] == base.pointSizeF()
        assert sizes["t͡ʃ"] == base.pointSizeF()
        assert sizes["t͡ʃ"] != ipa.pointSizeF()

    def test_ruby_ink_is_4tuple_with_mark_height(self, qapp, monkeypatch):
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        ch = _mk("天", ["tʰiɛn˥"], ts=[1000], line_end=True, sent_end=True)
        sentence = Sentence(singer_id="s", characters=[ch])
        preview = preview_module.KaraokePreview()
        entry = _render_data(preview, sentence)
        ink = entry["char_ruby_ink"][0]
        assert len(ink) == 4
        # 矢量墨水：上标修饰（ʰ ˥）高于基线以上，top < 0，height 覆盖
        assert ink[2] < 0
        assert ink[3] - ink[2] > 0

    def test_zero_advance_char_gets_ink_width_cell(self, qapp, monkeypatch):
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        # 孤立附加符（零 advance、tight 无墨水）必须回退矢量轮廓得到格子，
        # 不能变 0 宽而与后字重叠。字体相关，这里用固定矢量盒锁定逻辑。
        real_vector = preview_module._vector_ink_bounds

        def fake_vector(font, text, cache=None):
            if text == "\u0303":
                return (0.0, -5.0, 8.0, 10.0)
            return real_vector(font, text, cache)

        monkeypatch.setattr(preview_module, "_vector_ink_bounds", fake_vector)
        sentence = Sentence(singer_id="s", characters=[_mk("\u0303", [], cc=0)])
        preview = preview_module.KaraokePreview()
        if preview._fm_current.horizontalAdvance("\u0303") > 0:
            pytest.skip("当前字体给孤立 U+0303 正 advance，测不到零宽兜底")
        entry = _render_data(preview, sentence)
        assert entry["char_uses_ipa"] == [False]
        assert entry["char_ink_offsets"][0] == 0.0
        assert entry["char_ink_widths"][0] == 8.0
        assert entry["char_widths"][0] == 10.0  # 墨水宽 + 2px 呼吸
        assert entry["line_ink_top"] == -5.0
        assert entry["line_ink_bottom"] == 5.0

    def test_line_ink_bounds_cached(self, qapp, monkeypatch):
        monkeypatch.setattr(preview_module, "theme", _DummyTheme())
        sentence = Sentence.from_text("ˈt͡ʃaɪ", singer_id="s")
        preview = preview_module.KaraokePreview()
        entry = _render_data(preview, sentence)
        assert entry["line_ink_top"] < 0
        assert entry["line_ink_bottom"] > 0


class _DummyTheme:
    class _Sig:
        def connect(self, *a):
            pass

        def emit(self, *a):
            pass

    changed = _Sig()
    karaoke_bg = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#101010")
    karaoke_text_current = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#FFFFFF")
    karaoke_text_past = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#777777")
    karaoke_text_future = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#555555")
    line_number_current = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#FFFFFF")
    line_number_normal = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#888888")
    karaoke_highlight_bg = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#223344")
    karaoke_selection_bg = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#334455")
    default_highlight = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#FFFFFF")
    text_hint = __import__("PyQt6.QtGui", fromlist=["QColor"]).QColor("#666666")
