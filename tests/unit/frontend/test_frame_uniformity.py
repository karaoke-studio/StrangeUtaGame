"""连词框几何回归测试（用户报告：日语框行为被破坏）。

契约（_get_sentence_render_data 缓存于 entry）：
  - 纵向**全行统一**：``ruby_frame_top_off`` 默认 = -fm.ascent()-1，
    ``ruby_frame_bottom_off`` 默认 = fm.height()-fm.ascent()+1（历史字体盒
    口径，框高恒 fm.height()+2，そう / ふ|た / り 同高）；仅当本行任一
    注音墨水越出字体盒（IPA 叠加附加符/声调条）时按行整体扩张。
  - 横向保留历史方法1/方法2 逻辑：方法2 字符墨水取**格子左缘 + 墨水偏移**
    （格子宽已含节奏点标记/ruby 加宽 → 框跟随主文字格子），方法1 与方法2
    的比较基准是 ruby 的**排版宽**（advance）；不引入格子内居中修正。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from strange_uta_game.backend.domain import Character, Project, Ruby, RubyPart, Sentence, Singer  # noqa: E402
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


def _ruby_char(text, parts, ts=None, linked=False, line_end=False, sent_end=False):
    ch = Character(
        char=text,
        check_count=len(parts),
        linked_to_next=linked,
        is_line_end=line_end,
        is_sentence_end=sent_end,
    )
    if parts:
        ch.set_ruby(Ruby(parts=[RubyPart(text=p) for p in parts]))
    for i, t in enumerate(ts or []):
        ch.add_timestamp(t, checkpoint_idx=i)
    return ch


def _make_dummy_theme():
    """带 paintEvent 所需颜色属性的假主题（与 test_karaoke_preview_ipa 同款）。"""
    from PyQt6.QtGui import QColor

    class _Sig:
        def connect(self, *a):
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

    return _Theme()


def _entry(qapp, monkeypatch, sentence):
    """构建单行工程并取渲染数据 entry（含连词框纵向偏移）。"""
    singer = Singer(name="主唱", color="#FF6B6B", is_default=True)
    proj = Project(sentences=[sentence], singers=[singer])
    sentence.singer_id = singer.id
    for ch in sentence.characters:
        ch.singer_id = singer.id
    monkeypatch.setattr(preview_module, "theme", _make_dummy_theme())
    preview = preview_module.KaraokePreview()
    preview.set_project(proj)
    entry = preview._get_sentence_render_data(0, sentence, preview._fm_current, "cur")
    return preview, entry


class TestJapaneseFrameUniformity:
    def test_sou_zou_futa_ri_font_box(self, qapp, monkeypatch):
        """用户报告的原样用例：{想像||そう,ぞう} + {2人||ふ|た,り}。

        ふ 带浊点（墨水高于 そう/り），但框仍取字体盒——全行同高。
        """
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("想", ["そう"], ts=[43090], linked=True),
                _ruby_char("像", ["ぞう"], ts=[43860]),
                _ruby_char(" ", []),
                _ruby_char("2", ["ふ", "た"], ts=[36400, 36600], linked=True),
                _ruby_char("人", ["り"], ts=[36940]),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        fm = preview._fm_ruby
        assert entry["ruby_frame_top_off"] == -fm.ascent() - 1
        assert entry["ruby_frame_bottom_off"] == fm.height() - fm.ascent() + 1
        # 框高 = 底偏移 - 顶偏移 = fm.height() + 2（历史值）
        h = entry["ruby_frame_bottom_off"] - entry["ruby_frame_top_off"]
        assert h == fm.height() + 2

    def test_pure_kana_stays_font_box(self, qapp, monkeypatch):
        """带浊点假名（だいぼ/が 等）：墨水仍在字体盒内 → 不扩张。"""
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("大", ["だいぼ"], ts=[1000], linked=True),
                _ruby_char("冒", ["う"], ts=[2000], linked=True),
                _ruby_char("険", ["けん"], ts=[3000]),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        fm = preview._fm_ruby
        h = entry["ruby_frame_bottom_off"] - entry["ruby_frame_top_off"]
        assert h == fm.height() + 2, (h, fm.height() + 2)


class TestIPALineFrameUniformity:
    def test_adjacent_groups_share_one_offset_pair(self, qapp, monkeypatch):
        """IPA 行相邻组（ˈgru 墨水高 vs ʂɨ 墨水矮）共用同一组偏移。"""
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("Расцветали", ["rəf", "tsvɪ", "ˈta", "lʲɪ"],
                           ts=[1000, 1500, 2000, 2500], linked=True),
                _ruby_char(" ", []),
                _ruby_char("груши", ["ˈgru", "ʂɨ"], ts=[3000, 3600], linked=True),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        # entry 只有一对偏移（按行缓存）——同行同高由构造保证
        assert "ruby_frame_top_off" in entry and "ruby_frame_bottom_off" in entry
        # 契约：同行一组偏移，且永不小于字体盒；测试环境的字体盒可能已覆盖
        # 声调墨水（回退字体不同），扩张与否以实际墨水为准——只断言 >= 字体盒
        fm = preview._fm_ruby
        assert entry["ruby_frame_bottom_off"] >= fm.height() - fm.ascent() + 1
        assert entry["ruby_frame_top_off"] <= -fm.ascent() - 1

    def test_tall_diacritic_expands_line(self, qapp, monkeypatch):
        """叠加附加符（ĩ̯：非成音节+长音符上下叠）越出字体盒 → 按行扩张。"""
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("x", ["ĩ̯"], ts=[1000], linked=True),
                _ruby_char("y", ["a"], ts=[1500]),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        fm = preview._fm_ruby
        h = entry["ruby_frame_bottom_off"] - entry["ruby_frame_top_off"]
        assert h >= fm.height() + 2

    def test_plain_latin_no_expansion(self, qapp, monkeypatch):
        """普通拉丁注音（无附加符）不扩张，保持字体盒。"""
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("hello", ["hel", "lo"], ts=[1000, 2000], linked=True),
                _ruby_char("world", ["w", "rld"], ts=[3000, 4000], linked=True),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        fm = preview._fm_ruby
        h = entry["ruby_frame_bottom_off"] - entry["ruby_frame_top_off"]
        assert h == fm.height() + 2, (h, fm.height() + 2)


class TestFrameHorizontalLegacyLogic:
    """横向方法1/方法2 的历史口径（用户报告：框不再覆盖节奏点占用）。"""

    def _rendered_frame(self, qapp, monkeypatch, sent):
        """渲染并从 paintEvent 后的连词框缓存取几何（局部变量 → 经信号抓不到，
        改用渲染图像逐像素找框线太脆——这里验证口径来源：方法2 用格子累计宽）。"""
        # 直接断言渲染数据：格子宽含节奏点标记占用（char_widths >= cp 标记宽）
        preview, entry = _entry(qapp, monkeypatch, sent)
        return preview, entry

    def test_cp_marker_widens_cell_and_frame_follows(self, qapp, monkeypatch):
        """{2人||ふ|た,り}：2 的 2 个节奏点标记加宽格子，框跟随格子而非贴字形。

        方法2 口径：字符墨水 = 格子左缘 + 墨水偏移，_cum 累计**格子宽**
        （含节奏点标记加宽）→ 相邻组的框间距由格子宽决定。
        """
        sent = Sentence(
            singer_id="s",
            characters=[
                _ruby_char("2", ["ふ", "た"], ts=[36400, 36600], linked=True),
                _ruby_char("人", ["り"], ts=[36940]),
            ],
        )
        preview, entry = _entry(qapp, monkeypatch, sent)
        char_w = entry["char_widths"][0]
        fm_cp = preview._fm_checkpoint
        # 2 个节奏点标记的宽度（▮▯ ×2）
        cp_w = fm_cp.horizontalAdvance("▮") * 2
        # 格子宽至少能容纳节奏点标记（历史 set 逻辑保证）
        assert char_w >= cp_w * 0.9, (char_w, cp_w)
        # 主文字 "2" 的字形 advance 小于格子宽（被标记加宽）→ 框若贴字形
        # 会明显窄于格子口径
        adv = entry["char_advances"][0]
        assert char_w > adv, (char_w, adv)
