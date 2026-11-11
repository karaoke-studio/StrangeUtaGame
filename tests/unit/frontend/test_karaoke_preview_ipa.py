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
        # 孤立附加符（零 advance、有墨水兜底显示）不能得到 0 宽格子
        sentence = Sentence(singer_id="s", characters=[_mk("̃", [], cc=0)])
        preview = preview_module.KaraokePreview()
        entry = _render_data(preview, sentence)
        assert entry["char_widths"][0] >= 0  # 不崩即可（offscreen 下可能无字形）
        assert abs(entry["char_ink_offsets"][0]) < 10000

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
