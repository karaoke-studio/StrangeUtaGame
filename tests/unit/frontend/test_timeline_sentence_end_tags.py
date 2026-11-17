from __future__ import annotations

import pytest
from PyQt6.QtGui import QImage, QPainter

from strange_uta_game.frontend.editor.timing.timeline_widget import WaveformDisplay
from strange_uta_game.frontend.theme import ThemeColors, theme


@pytest.mark.parametrize("mode", ["waveform", "spectrum", "dual"])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_sentence_end_color_shape_warning_and_hit_identity(qtbot, monkeypatch, mode, dark, selected):
    """同一字符的起点仍为方块；结束点及倒序结束点均为可命中的菱形。"""
    view = WaveformDisplay()
    qtbot.addWidget(view)
    view.resize(550, 240)
    view._display_mode = mode
    view.set_tag_edit_enabled(True)
    view.set_tag_char_enabled(False)
    view.set_time_tags([
        (200, "a", 0, 0, 0, False, None),
        (400, "a", 0, 0, 1, True, None),
        (700, "b", 1, 0, 0, False, None),
        (600, "b", 1, 0, 1, True, None),
    ])
    normal, end, warning = (0, 0, 0, False), (0, 0, 1, True), (1, 0, 1, True)
    if selected:
        view._selected_handles = {normal, end, warning}
    monkeypatch.setattr(theme, "_colors", ThemeColors(dark))
    image = QImage(500, 240, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(theme.waveform_bg)
    painter = QPainter(image)
    try:
        view._draw_time_tags(painter, 500, 240, 0, 1000)
    finally:
        painter.end()

    for lane_top, lane_h, _is_spectrum in view._tag_lanes(240):
        cy = int(lane_top + lane_h / 2)
        ordinary_fill = theme.accent_secondary if selected else theme.accent_warning
        end_fill = theme.accent_secondary if selected else theme.timetag_sentence_end
        warning_fill = theme.accent_secondary if selected else theme.timetag_nonmonotonic
        assert image.pixelColor(100, cy) == ordinary_fill
        assert image.pixelColor(200, cy) == end_fill
        assert image.pixelColor(300, cy) == warning_fill
        # 方块角落有填色，菱形的相同位置留空；选中放大后仍保留区别。
        corner_x = 5 if selected else 3
        assert image.pixelColor(100 + corner_x, cy + 2) == ordinary_fill
        assert image.pixelColor(200 + corner_x, cy + 2) != end_fill
        assert image.pixelColor(300 + corner_x, cy + 2) != warning_fill
        assert image.pixelColor(200, cy + 15) == theme.timetag_sentence_end
        assert image.pixelColor(100, cy + 15) == theme.accent_warning
        # 倒序点保持原来的警告虚线，不能被橙色覆盖。
        warning_line = {image.pixelColor(300, y).name()
                        for y in range(cy + 10, min(int(lane_top + lane_h * .85), 240))}
        assert theme.timetag_nonmonotonic.name() in warning_line
        assert theme.waveform_bg.name() in warning_line
        assert theme.timetag_sentence_end.name() not in warning_line
        hit = view._hit_test_handle(200 + view._spectrum_axis_width(), cy)
        assert hit is not None and hit[:2] == (end, 400)
