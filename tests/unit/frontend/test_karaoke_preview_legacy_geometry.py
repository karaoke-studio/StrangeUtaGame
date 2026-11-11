"""非 IPA 走字/注音几何回归。

背景：IPA 支持把行墨水改成「字体盒」并用矢量轮廓统一取墨水后，普通假名行
的节奏点 marker、打轴指示线、字符 hitbox/选区矩形整体偏移（用户报告 2人/
喀秋莎框回归的同批问题）。本模块锁定：

- 普通行 ``line_ink_*`` 必须仍是 ``tightBoundingRect`` 的实际墨水；
- 普通字符的 wipe 墨水宽度/偏移必须仍是 ``_wipe_ink_bounds`` 的像素口径；
- 普通注音的整串墨水必须仍是 ``tightBoundingRect``（纵向含 top/bottom）；
- marker 的可视顶端与 hitbox 上沿由上述墨水边界推出，不允许回到字体盒。

IPA 文本仍走矢量轮廓（含 GPOS 附加符），由 test_karaoke_preview_ipa 覆盖。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from strange_uta_game.backend.domain import (  # noqa: E402
    Character,
    Project,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)
from strange_uta_game.frontend.editor.timing import (  # noqa: E402
    karaoke_preview as preview_module,
)


# 持有 QApplication 强引用，避免模块夹具析构级联删除 theme 单例。
_APP_HOLDER: list = []


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    _APP_HOLDER.append(app)
    return app


def _dummy_theme():
    class _Sig:
        def connect(self, *a):
            pass

        def emit(self, *a):
            pass

    class _Theme:
        changed = _Sig()
        karaoke_bg = QColor("#101010")
        karaoke_text_current = QColor("#FFFFFF")
        karaoke_text_past = QColor("#777777")
        karaoke_text_future = QColor("#555555")
        line_number_current = QColor("#FFFFFF")
        line_number_normal = QColor("#888888")
        karaoke_highlight_bg = QColor("#223344")
        karaoke_selection_bg = QColor("#334455")
        default_highlight = QColor("#FFFFFF")
        text_hint = QColor("#666666")
        accent_warning = QColor("#CC3333")

    return _Theme()


def _char(text, cc, ts, *, ruby=None, linked=False, end=False, end_ts=None):
    ch = Character(char=text, check_count=cc, linked_to_next=linked)
    if ruby:
        ch.set_ruby(Ruby(parts=[RubyPart(text=x) for x in ruby]))
    for i, t in enumerate(ts):
        ch.add_timestamp(t, checkpoint_idx=i)
    if end:
        ch.is_sentence_end = True
        ch.set_sentence_end_ts(end_ts)
    return ch


def _preview_for(qapp, monkeypatch, sentence, *, width=900, height=300):
    singer = Singer(name="主唱", color="#FF6B6B", is_default=True)
    proj = Project(sentences=[sentence], singers=[singer])
    sentence.singer_id = singer.id
    for ch in sentence.characters:
        ch.singer_id = singer.id
    monkeypatch.setattr(preview_module, "theme", _dummy_theme())
    preview = preview_module.KaraokePreview()
    preview.resize(width, height)
    # 单行视口：y_center 恒为控件高度一半，便于把 hitbox 与墨水边界对账
    preview._visible_lines = 1
    preview.set_project(proj)
    preview._scroll_center_line = 0.0
    preview._current_line_idx = 0
    return preview


def _entry(preview, sentence):
    return preview._get_sentence_render_data(
        0, sentence, preview._fm_current, "cur"
    )


class TestPlainLineInk:
    def test_kana_line_ink_is_tight_ink(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char("あ", 1, [1000]),
                _char("い", 1, [1400]),
                _char("う", 1, [1800]),
                _char("。", 0, [], end=True, end_ts=2400),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        fm = preview._fm_current
        rects = [fm.tightBoundingRect(ch) for ch in sent.chars if ch.strip()]
        assert entry["line_ink_top"] == min(r.top() for r in rects)
        # paintEvent 统一在 entry 底边 +1；历史 tight 口径是 QRect.bottom()
        assert entry["line_ink_bottom"] == max(r.bottom() for r in rects)

    def test_blank_only_line_falls_back_to_font_box(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char(" ", 0, []),
                _char("\u3000", 0, []),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        assert entry["line_ink_top"] == -preview._fm_current.ascent()
        assert entry["line_ink_bottom"] == preview._fm_current.descent()


class TestPlainWipeInk:
    def test_char_ink_stays_pixel_tight(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char("あ", 1, [1000]),
                _char("い", 1, [1400]),
                _char(" ", 0, []),
                _char("う", 1, [1800]),
                _char("。", 0, [], end=True, end_ts=2400),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        fm = preview._fm_current
        expected = [
            preview_module._wipe_ink_bounds(fm, ch) for ch in sent.chars
        ]
        assert entry["char_ink_offsets"] == [off for off, _w in expected]
        assert entry["char_ink_widths"] == [w for _off, w in expected]

    def test_kana_ruby_ink_stays_pixel_tight(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char("想", 1, [1000], ruby=["そう"]),
                _char("像", 1, [1800], ruby=["ぞう"], end=True, end_ts=3000),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        fm = preview._fm_ruby
        for ci, text in ((0, "そう"), (1, "ぞう")):
            rect = fm.tightBoundingRect(text)
            assert entry["char_ruby_ink"][ci] == (
                float(rect.x()),
                float(rect.width()),
                float(rect.top()),
                float(rect.bottom()),
            )

    def test_linked_group_ruby_ink_stays_pixel_tight(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char("想", 1, [1000], ruby=["そう"], linked=True),
                _char("像", 1, [1800], ruby=["ぞう"], end=True, end_ts=3000),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        fm = preview._fm_ruby
        rect = fm.tightBoundingRect("そうぞう")
        assert entry["group_ruby_ink"][0] == (
            float(rect.x()),
            float(rect.width()),
            float(rect.top()),
            float(rect.bottom()),
        )
        # 非 IPA 连词组框仍取历史字体盒（同行同高）
        assert entry["ruby_frame_top_off"] == -fm.ascent() - 1
        assert entry["ruby_frame_bottom_off"] == fm.height() - fm.ascent() + 1


class TestMarkerAndHitbox:
    def test_wide_multicodepoint_cluster_cell_covers_ink(self, qapp, monkeypatch):
        """keycap 等簇的轮廓可比 advance 宽：格子必须覆盖墨水右缘。"""
        real_vector = preview_module._vector_ink_bounds

        def fake_vector(font, text, cache=None):
            if text == "1️⃣":
                return (2.0, -20.0, 30.0, 40.0)
            return real_vector(font, text, cache)

        monkeypatch.setattr(preview_module, "_vector_ink_bounds", fake_vector)
        sent = Sentence(
            singer_id="s",
            characters=[_char("1️⃣", 1, [1000], end=True, end_ts=2000)],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        entry = _entry(preview, sent)
        assert entry["char_uses_ipa"] == [True]
        assert entry["char_ink_offsets"][0] == 2.0
        assert entry["char_ink_widths"][0] == 30.0
        assert entry["char_widths"][0] == 32.0
        assert entry["line_ink_top"] == -20.0
        assert entry["line_ink_bottom"] == 20.0

    def test_marker_and_hitbox_follow_tight_ink(self, qapp, monkeypatch):
        sent = Sentence(
            singer_id="s",
            characters=[
                _char("あ", 1, [1000]),
                _char("い", 1, [1400]),
                _char("。", 0, [], end=True, end_ts=2400),
            ],
        )
        preview = _preview_for(qapp, monkeypatch, sent)
        image = QImage(preview.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        preview.render(image)

        # 期望值必须独立按 tightBoundingRect 计算，不能从 entry["line_ink_*"]
        # 反推——否则 line_ink 退回字体盒时 marker/hitbox 会一起漂移而测试
        # 仍然通过。
        fm = preview._fm_current
        rects = [fm.tightBoundingRect(ch) for ch in sent.chars if ch.strip()]
        tight_top = min(r.top() for r in rects)
        tight_bottom = max(r.bottom() for r in rects)
        y_center = int(round(preview.height() / 2.0))
        line_top = y_center + tight_top
        line_bottom = y_center + tight_bottom + 1

        # 先确认修复目标：不能再是字体盒（旧回归的 y 值）
        assert line_top != y_center - fm.ascent()
        assert line_bottom != y_center + fm.descent()

        char_rect = next(
            rect for rect, line, char in preview._char_hitboxes
            if line == 0 and char == 0
        )
        # 历史 hitbox：行墨水顶 - 2px 呼吸
        assert char_rect.top() == line_top - 2

        cp_rect = next(
            rect for rect, line, char, cp in preview._checkpoint_hitboxes
            if line == 0 and char == 0 and cp == 0
        )
        # 历史 marker：行墨水底 + cp_spacing
        assert cp_rect.top() == line_bottom + preview._cp_spacing
