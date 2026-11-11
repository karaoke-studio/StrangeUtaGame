"""卡拉OK 歌词预览控件。

- 实时走字高亮 / Ruby 注音同步 / 连词平滑渲染
- 支持鼠标拖拽划词选区 + 右键菜单
- 渲染状态与 ``EditorInterface`` 通过信号耦合
"""

from __future__ import annotations

from bisect import bisect_right
import math
import sys
import time
from typing import Optional

from PyQt6.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QWheelEvent,
)

from strange_uta_game.frontend.font_utils import DEFAULT_FONT_FAMILY, ui_font
from PyQt6.QtWidgets import QScrollBar, QWidget
from qfluentwidgets import Action, RoundMenu

from strange_uta_game.backend.domain import Character, Project, Ruby
from strange_uta_game.backend.domain.graphemes import (
    chars_to_offsets,
    offset_to_index,
    truncate_graphemes,
)
from strange_uta_game.frontend.perf_log import (
    log_elapsed,
    log_slow_method,
    perf_enabled,
)
from strange_uta_game.frontend.theme import theme


# ──────────────────────────────────────────────
# 卡拉OK 歌词预览
# ──────────────────────────────────────────────


def _draw_split_text(
    painter: "QPainter",
    x: int,
    baseline: int,
    ch: str,
    colors: "list[QColor]",
    y_top: int,
    y_bottom: int,
) -> None:
    """绘制文字，支持分色（split）模式。

    单色时直接 setPen+drawText，无额外开销。
    分色时对每个色带用 IntersectClip，可在已有 wipe clip 内正确工作。
    y_top/y_bottom 为本行实际墨水上下边界（屏幕坐标），由调用方按行预计算传入，
    保证同行所有字符的色带分界线位于同一视觉高度。
    """
    n = len(colors)
    if n <= 1:
        painter.setPen(colors[0] if colors else QColor("white"))
        painter.drawText(x, baseline, ch)
        return
    total_h = y_bottom - y_top
    if total_h <= 0:
        painter.setPen(colors[0])
        painter.drawText(x, baseline, ch)
        return
    for i, color in enumerate(colors):
        y0 = y_top + int(i * total_h / n)
        y1 = y_top + int((i + 1) * total_h / n) + 1  # +1 防止浮点精度产生空隙
        painter.save()
        painter.setClipRect(
            QRect(-32768, y0, 65536, y1 - y0),
            Qt.ClipOperation.IntersectClip,
        )
        painter.setPen(color)
        painter.drawText(x, baseline, ch)
        painter.restore()


def _anchor_ratio(anchors: list[int], current_time: int) -> float:
    """根据锚点序列计算总 wipe 进度比例。

    anchors 形如 ``[ts_0, ts_1, ..., ts_N]``，共 N+1 个时间戳，
    把演唱划成 N 个 part 段。第 i 段区间为 ``[ts_i, ts_{i+1})``。

    返回值 = ``(i + segment_ratio) / N``，其中 i 为 current_time 落入的段索引，
    segment_ratio 为段内线性比例。clamp 到 [0.0, 1.0]。

    - ``current_time < anchors[0]`` → 0.0
    - ``current_time >= anchors[-1]`` → 1.0
    - 段时长 <= 0（异常数据）→ 该段视为瞬时完成
    - 锚点数 < 2 → 1.0（无法分段，按已完成处理；调用方负责回退）
    """
    n = len(anchors) - 1
    if n <= 0:
        return 1.0
    if current_time < anchors[0]:
        return 0.0
    if current_time >= anchors[-1]:
        return 1.0
    # 线性扫描即可（part 数极少，N <= 10 量级）
    for i in range(n):
        seg_start = anchors[i]
        seg_end = anchors[i + 1]
        if current_time < seg_end:
            seg_dur = seg_end - seg_start
            seg_ratio = (current_time - seg_start) / seg_dur if seg_dur > 0 else 1.0
            seg_ratio = max(0.0, min(1.0, seg_ratio))
            return (i + seg_ratio) / n
    return 1.0


def _anchor_segment(anchors: list[int], current_time: int) -> tuple[int, float, int]:
    """与 _anchor_ratio 同义，但返回 (段索引 i, 段内比例 seg_ratio, 段数 N)。

    - 已完成（>=anchors[-1]）→ (N-1, 1.0, N)
    - 未开始（<anchors[0]）→ (0, 0.0, N)
    - anchors 不足 2 元素 → (0, 1.0, 0)
    """
    n = len(anchors) - 1
    if n <= 0:
        return (0, 1.0, 0)
    if current_time < anchors[0]:
        return (0, 0.0, n)
    if current_time >= anchors[-1]:
        return (n - 1, 1.0, n)
    for i in range(n):
        seg_start = anchors[i]
        seg_end = anchors[i + 1]
        if current_time < seg_end:
            seg_dur = seg_end - seg_start
            seg_ratio = (current_time - seg_start) / seg_dur if seg_dur > 0 else 1.0
            seg_ratio = max(0.0, min(1.0, seg_ratio))
            return (i, seg_ratio, n)
    return (n - 1, 1.0, n)


def _anchor_part_ratio(
    anchors: list[int], current_time: int, part_ws: "Optional[list[int]]" = None
) -> float:
    """锚点轴上的整体 wipe 进度 ∈ [0,1]，段间按 ruby 部件宽度加权推进。

    part_ws 为各锚点段对应的 ruby 部件像素宽度（len == 段数 N 时启用）；
    第 i 段推进的总进度占比 = part_ws[i] / sum(part_ws)。
    部件宽度缺失或与段数不匹配时回退为各段等分（与 _anchor_ratio 等价）。
    """
    i, sr, n = _anchor_segment(anchors, current_time)
    if n <= 0:
        return 1.0
    if part_ws is not None and len(part_ws) == n:
        total = sum(part_ws)
        if total > 0:
            return (sum(part_ws[:i]) + part_ws[i] * sr) / total
    return (i + sr) / n


def _piecewise_wipe_ratio(
    segments: list[tuple[int, int, float, float]], current_time: int
) -> float:
    """按分段时间轴 + 累计墨水占比返回当前 wipe 比例 ∈ [0,1]。

    每个 segment = ``(t_start, t_end, w_start, w_end)``，``w`` 为该段结束时
    累计推进的墨水占比（[0,1]，单调不减）。

    - 空 part（无墨水）：``w_start == w_end`` —— 消耗时间但不推进 wipe。
    - 段间空隙（``current_time`` 落在上一段 end 与下一段 start 之间）：
      保持上一段 ``w_end``，不推进。
    - 未开始 → 0.0；已结束 → 1.0。
    """
    if not segments:
        return 1.0
    if current_time < segments[0][0]:
        return 0.0
    if current_time >= segments[-1][1]:
        return 1.0
    last_w = 0.0
    for t0, t1, w0, w1 in segments:
        if current_time < t0:
            return last_w
        if current_time < t1:
            f = (current_time - t0) / (t1 - t0) if t1 > t0 else 1.0
            return w0 + (w1 - w0) * f
        last_w = w1
    return 1.0


def _guide_colors(colors: "list[QColor]", alpha: float) -> "list[QColor]":
    """走字预览指引：将颜色列表整体套用不透明度（分色逐色保留，仅改 alpha）。"""
    out = []
    for _c in colors:
        _cc = QColor(_c)
        _cc.setAlphaF(alpha)
        out.append(_cc)
    return out


_RUBY_UNTIMED_ALPHA = 0.30


def _foreground_alpha_for_color(
    color: QColor, foreground: QColor, background: QColor
) -> float:
    """将现有实色换算为同一前景色叠在背景上时所需的不透明度。

    非当前歌词行目前用实心灰色区分 past/future。Ruby 要继续使用纯黑/白，
    因此把该灰色投影到 ``background -> foreground`` 的颜色轴上，得到它等效的
    alpha 上限。这样只改变透明度，不引入另一套灰色。
    """
    bg = (background.redF(), background.greenF(), background.blueF())
    fg = (foreground.redF(), foreground.greenF(), foreground.blueF())
    color_alpha = color.alphaF()
    target = tuple(
        color_alpha * channel + (1.0 - color_alpha) * bg_channel
        for channel, bg_channel in zip(
            (color.redF(), color.greenF(), color.blueF()), bg
        )
    )
    axis = tuple(fg_channel - bg_channel for fg_channel, bg_channel in zip(fg, bg))
    axis_length_sq = sum(component * component for component in axis)
    if axis_length_sq <= 0.0:
        return color_alpha
    projected = sum(
        (target_channel - bg_channel) * axis_component
        for target_channel, bg_channel, axis_component in zip(target, bg, axis)
    ) / axis_length_sq
    return max(0.0, min(1.0, projected))


def _ruby_timing_chunks(
    characters: list[Character], char_indices: list[int]
) -> list[tuple[str, bool]]:
    """按 RubyPart 返回注音文本及其是否已有对应 checkpoint 时间戳。

    这里只读取现有 ``RubyPart <-> checkpoint`` 的位置对应关系，不修改领域模型：

    - ``part_idx < len(global_timestamps)`` 的 part 已打轴；
    - 没有对应时间戳的尾部 part 未打轴；
    - ``check_count == 0`` 的注音没有节奏点，整段保持未打轴态；
    - 历史脏数据中超出 ``check_count`` 的 part 也保持未打轴态。

    ``char_indices`` 可以只含一个字符，也可以是一整个连词组。后者仍按各字符
    自己的 checkpoint 归属逐 part 判断，只在显示时拼成一行。
    """
    chunks: list[tuple[str, bool]] = []
    for char_idx in char_indices:
        character = characters[char_idx]
        ruby = character.ruby
        if ruby is None:
            continue
        timed_part_count = min(
            character.check_count, len(character.global_timestamps)
        )
        for part_idx, part in enumerate(ruby.parts):
            chunks.append(
                (
                    part.text,
                    character.check_count > 0 and part_idx < timed_part_count,
                )
            )
    return chunks


def _ruby_timed_spans(
    fm: QFontMetrics, chunks: list[tuple[str, bool]]
) -> tuple[str, list[tuple[int, int]]]:
    """把注音 part 状态转换成合并文本中的已打轴水平区间。

    区间使用整串各前缀的 advance 计算，因此 ``ちょ`` 这类多码点 mora 仍是
    一个不可拆的区间；相邻已打轴区间会合并，减少重复绘制。
    """
    text = ""
    timed_spans: list[tuple[int, int]] = []
    for part_text, is_timed in chunks:
        start = fm.horizontalAdvance(text)
        text += part_text
        end = fm.horizontalAdvance(text)
        if not is_timed or end <= start:
            continue
        if timed_spans and timed_spans[-1][1] >= start:
            timed_spans[-1] = (timed_spans[-1][0], max(timed_spans[-1][1], end))
        else:
            timed_spans.append((start, end))
    return text, timed_spans


def _ruby_timed_span_maps(
    fm_or_resolver,
    characters: list[Character],
    linked_leader_groups: dict[int, list[int]],
    linked_non_leader: set[int],
) -> tuple[dict[int, list[tuple[int, int]]], dict[int, list[tuple[int, int]]]]:
    """计算单字及连词组的 Ruby 静态打轴区间，不包含任何走字状态。

    ``fm_or_resolver``：QFontMetrics，或 ``(char_idx, merged_text) -> QFontMetrics``
    解析器。**连词组必须按合并后的整串 ruby 文本选字体**（与 paintEvent 绘制
    同源）——按 leader 自己的 ruby 选字体时，混合组（假名+IPA）的像素区间
    与绘制 advance 不一致（审查反馈 #6）。
    """

    def _resolve_fm(char_idx: int, merged_text: str = "") -> QFontMetrics:
        if callable(fm_or_resolver):
            try:
                return fm_or_resolver(char_idx, merged_text)
            except TypeError:
                return fm_or_resolver(char_idx)
        return fm_or_resolver

    char_spans: dict[int, list[tuple[int, int]]] = {}
    group_spans: dict[int, list[tuple[int, int]]] = {}

    for char_idx, character in enumerate(characters):
        if char_idx in linked_leader_groups or char_idx in linked_non_leader:
            continue
        if character.ruby and character.ruby.text:
            _, char_spans[char_idx] = _ruby_timed_spans(
                _resolve_fm(char_idx), _ruby_timing_chunks(characters, [char_idx])
            )
    for leader_idx, group in linked_leader_groups.items():
        if any(characters[char_idx].ruby for char_idx in group):
            merged = "".join(
                characters[g].ruby.text
                for g in group
                if characters[g].ruby and characters[g].ruby.text
            )
            _, group_spans[leader_idx] = _ruby_timed_spans(
                _resolve_fm(leader_idx, merged),
                _ruby_timing_chunks(characters, group),
            )
    return char_spans, group_spans


def _draw_ruby_timing_base(
    painter: QPainter,
    x: int,
    baseline: int,
    text: str,
    timed_spans: list[tuple[int, int]],
    fm: QFontMetrics,
    foreground: QColor,
    max_alpha: float,
) -> None:
    """绘制注音的静态打轴状态，不参与也不改变后续走字逻辑。

    当前行全串先以主题黑/白的 30% 不透明度绘制，再把已有 checkpoint 时间戳的
    mora 用 100% 重绘。非当前行以其原有 past/future 灰度对应的 ``max_alpha``
    为上限，按相同比例缩放为 ``30% * max_alpha / max_alpha``。调用方仍在此层
    之上执行原有 singer-color wipe。
    """
    max_alpha = max(0.0, min(1.0, max_alpha))
    untimed_color = QColor(foreground)
    untimed_color.setAlphaF(_RUBY_UNTIMED_ALPHA * max_alpha)
    painter.setPen(untimed_color)
    painter.drawText(x, baseline, text)

    if not timed_spans:
        return

    text_advance = fm.horizontalAdvance(text)
    ink_rect = fm.tightBoundingRect(text)
    ink_left = ink_rect.left()
    ink_right = ink_rect.right() + 1
    clip_top = baseline - fm.ascent() - 2
    clip_height = fm.height() + 4
    timed_color = QColor(foreground)
    timed_color.setAlphaF(max_alpha)
    painter.setPen(timed_color)
    for start, end in timed_spans:
        # 首尾区间把侧 bearing 一并纳入，避免最外沿抗锯齿仍残留 30% 底色。
        clip_left = min(start, ink_left) if start <= 0 else start
        clip_right = max(end, ink_right) if end >= text_advance else end
        if clip_right <= clip_left:
            continue
        painter.save()
        painter.setClipRect(
            QRect(x + clip_left, clip_top, clip_right - clip_left, clip_height),
            Qt.ClipOperation.IntersectClip,
        )
        painter.drawText(x, baseline, text)
        painter.restore()


def _draw_ruby_base(
    painter: QPainter,
    x: int,
    baseline: int,
    text: str,
    timed_spans: list[tuple[int, int]],
    fm: QFontMetrics,
    foreground: QColor,
    max_alpha: float,
    *,
    guide_enabled: bool,
    fallback_color: QColor,
) -> None:
    """按打轴指引总开关绘制注音底层，关闭时恢复旧版纯基色。"""
    if not guide_enabled:
        painter.setPen(fallback_color)
        painter.drawText(x, baseline, text)
        return

    _draw_ruby_timing_base(
        painter,
        x,
        baseline,
        text,
        timed_spans,
        fm,
        foreground,
        max_alpha,
    )


def _ink_bounds(fm: QFontMetrics, text: str) -> tuple[int, int]:
    """返回 ``text`` 在给定字体度量下的墨水边界：``(ink_left, ink_width)``。

    - ``ink_left``：墨水最左像素相对于 ``drawText(x, y, text)`` 中 ``x`` 的偏移
      （即 ``tightBoundingRect`` 的 ``x()`` 字段）。
    - ``ink_width``：墨水实际占用的水平像素宽度（即 ``tightBoundingRect.width()``）。

    Qt 的 ``tightBoundingRect`` 返回紧贴墨水的最小包围盒：
    - x 可能为负（字形墨水越过 origin 左侧，例如斜体 ``f``）；
    - width 一般 ≤ ``horizontalAdvance(text)``，但极少数斜体场景可能略大；
    - 对无字形码点（孤立的组合附加符、变体选择符等）返回 Qt 的空矩形
      **哨兵值**（x=100000）——此处归零，避免毒化连词框的墨水并集；
    - 对空白字符（U+0020 / U+3000 / NBSP / Tab）通常返回零宽 → 此处保留为 (0, 0)，
      由调用方跳过 wipe 绘制。

    注意 ``tightBoundingRect`` 在部分平台（Windows DirectWrite）**不计入
    GPOS 定位的组合附加符**（``n̩`` ``d̥`` 的下标符号、``t͡ʃ`` 的连结线都不在
    盒内）。需要含附加符的精确墨水时用 :func:`_vector_ink_bounds`。

    返回为 int；底层 Qt API 已是 int。
    """
    if not text:
        return (0, 0)
    rect = fm.tightBoundingRect(text)
    # 空矩形哨兵（width<=0 或 x 到达哨兵值）：无墨水
    if rect.width() <= 0 or rect.x() >= 100000 or rect.y() >= 100000:
        return (0, 0)
    return (int(rect.x()), int(rect.width()))


def _vector_ink_bounds(
    font: QFont, text: str, cache: Optional[dict] = None
) -> Optional[tuple[float, float, float, float]]:
    """矢量轮廓墨水盒 ``(x, top, width, height)``，相对 drawText 的 ``(x, baseline)``。

    用 ``QPainterPath.addText`` 的包围盒（与实际绘制同源、完整排版）：
    GPOS 定位的组合附加符（上下标）、连结线的左右越界、声调字母的高低
    全部计入——这是连词框 / 走字裁剪几何的可靠依据（tightBoundingRect
    在 Windows 上会漏算附加符）。

    ``top`` 相对基线（负=基线上方）。无墨水（空白 / 纯附加符孤段）返回
    ``None``。``cache`` 传入实例级缓存字典（键含字体签名）。
    """
    if not text:
        return None
    key = None
    if cache is not None:
        fams = font.families()
        key = (
            # 不只取首族名：IPA 字体与主字体可能同首族、不同回退链，缺字
            # 时墨水盒会不同（缓存键必须包含完整字体链与具体字面）。
            tuple(fams) if fams else (font.family(),),
            font.styleName(),
            font.pointSizeF() if font.pointSizeF() >= 0 else font.pointSize(),
            font.pixelSize(),
            int(font.weight()),
            font.italic(),
            font.stretch(),
            font.letterSpacing(),
            font.capitalization(),
            font.overline(),
            font.hintingPreference(),
            text,
        )
        hit = cache.get(key)
        if hit is not None:
            return hit if hit != "" else None
    path = QPainterPath()
    path.addText(0.0, 0.0, font, text)
    br = path.boundingRect()
    if br.isEmpty() or br.width() <= 0 or br.height() <= 0:
        result = None
    else:
        result = (br.x(), br.y(), br.width(), br.height())
    if cache is not None and key is not None:
        if len(cache) > 20000:
            cache.clear()
        cache[key] = result if result is not None else ""
    return result


def _has_multicodepoint_cluster(text: str) -> bool:
    """文本是否含多码点字素簇（基字+附加符 / emoji 序列）。

    用于 ruby/标签的 IPA 字体判定：迭代 str 只得到单码点串（``len(c) > 1``
    恒 False 的死条件），必须按簇比较（审查反馈 #5）。
    """
    from strange_uta_game.backend.domain.graphemes import grapheme_count

    return grapheme_count(text) != len(text)


def _wipe_ink_bounds(fm: QFontMetrics, text: str) -> tuple[int, int]:
    """返回 wipe 使用的墨水边界；所有空白字符只占排版宽度，不参与 wipe。"""
    if text.isspace():
        return (0, 0)
    return _ink_bounds(fm, text)


def _weighted_wipe_windows(
    char_widths: list[int],
    char_ink_widths: list[int],
    indices: range,
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, int]]:
    """按排版像素宽度把 ``[start_ts, end_ts]`` 加权切分为逐字符 wipe 窗口。

    零墨水字符（空格 / 全角空格 / NBSP 等无渲染字符）权重为 0，得到零时长
    窗口——不占用走字时长，整段时长由可见字符按宽度瓜分；否则空格会凭排版
    宽度分走一段时间，绘制层却又因无墨水跳过它，表现为走字停顿、后续字符
    被压缩。整段全为零墨水时（理论上的全空白段）回退为排版宽度加权，保证
    各字符仍有有限时间窗口。
    """
    weights = [char_widths[ci] if char_ink_widths[ci] > 0 else 0 for ci in indices]
    total = sum(weights)
    if total <= 0:
        weights = [char_widths[ci] for ci in indices]
        total = sum(weights)
    windows: list[tuple[int, int]] = []
    cum = 0
    for w in weights:
        ratio = cum / total if total > 0 else 0.0
        next_ratio = (cum + w) / total if total > 0 else 1.0
        windows.append(
            (
                int(start_ts + (end_ts - start_ts) * ratio),
                int(start_ts + (end_ts - start_ts) * next_ratio),
            )
        )
        cum += w
    return windows


class _AltWheelScrollBar(QScrollBar):
    """A horizontal scrollbar that leaves an unmodified wheel to its parent."""

    def wheelEvent(self, event: QWheelEvent | None) -> None:  # noqa: N802
        if event is None:
            return
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            angle = event.angleDelta()
            delta = angle.y() or angle.x()
            if delta:
                steps = delta / 120.0
                self.setValue(
                    int(round(self.value() - steps * self.singleStep()))
                )
            event.accept()
        else:
            event.ignore()


class KaraokePreview(QWidget):
    """多行歌词预览，带逐字高亮、注音显示和滚动支持。

    滚动模型：_scroll_center_line 表示视口中央对应的行索引（浮点数）。
    - 自动跟随：打轴推进时自动居中当前行
    - 手动滚动：鼠标滚轮浏览，点击某行后重新居中
    - 首行居中：_scroll_center_line=0 时首行在正中央，上方留空
    """

    line_clicked = pyqtSignal(int)
    checkpoint_clicked = pyqtSignal(int, int, int)  # line_idx, char_idx, checkpoint_idx
    char_edit_requested = pyqtSignal(int, int)  # line_idx, char_idx (F2 key)
    seek_to_char_requested = pyqtSignal(int, int)  # line_idx, char_idx (click)
    seek_to_checkpoint_requested = pyqtSignal(int, int ,int)  # line_idx, char_idx, checkpoint_idx (double-click)
    char_selected = pyqtSignal(int, int)  # line_idx, char_idx
    singer_change_requested = pyqtSignal(
        int, int, int, str
    )  # line_idx, start_char, end_char, singer_id
    delete_chars_requested = pyqtSignal(int, int, int)
    delete_chars_multi_requested = pyqtSignal(int, int, int, int)  # start_line, start_char, end_line, end_char
    singer_change_multi_requested = pyqtSignal(int, int, int, int, str)  # start_line, start_char, end_line, end_char, singer_id
    # 跨字符多选（Ctrl+左键）专用：
    singer_change_ranges_requested = pyqtSignal(object, str)  # ranges, singer_id
    selection_char_ranges_action = pyqtSignal(str, object)  # action, ranges
    delete_selection_ranges_requested = pyqtSignal(object)  # ranges
    selection_changed = pyqtSignal()  # 跨字符选区建立/变更/清除
    delete_timestamp_requested = pyqtSignal(int, int)
    insert_space_before_requested = pyqtSignal(int, int)
    insert_space_after_requested = pyqtSignal(int, int)
    merge_line_up_requested = pyqtSignal(int)
    delete_line_requested = pyqtSignal(int)
    insert_blank_line_before_requested = pyqtSignal(int)
    insert_blank_line_requested = pyqtSignal(int)
    add_checkpoint_requested = pyqtSignal(int, int)
    remove_checkpoint_requested = pyqtSignal(int, int)
    toggle_sentence_end_requested = pyqtSignal(int, int)
    toggle_needs_guide_requested = pyqtSignal(int, int)  # line_idx, char_idx
    auto_scroll_line_changed = pyqtSignal()
    user_interaction_during_auto_scroll = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project: Optional[Project] = None
        self._current_line_idx = 0
        self._current_char_idx = 0
        self._current_checkpoint_idx: Optional[int] = None
        self._current_time_ms = 0
        self._global_offset_ms = 0
        self._duration_ms = 0  # 音频总时长（用于行尾非停顿点时的wipe右边界）
        self._visible_lines = 7  # 视口内可见行数（决定行高）
        # 可见行数下限：常规 3 行；双谱模式下时间轴占用翻倍，经
        # set_min_visible_lines(1) 放宽到单行（EditorInterface 协商让位时切换）
        self._min_visible_lines = 3
        self._scroll_center_line: float = 0.0  # 视口中央对应的行索引
        self._checkpoint_hitboxes: list = []  # [(QRect, line_idx, char_idx, cp_idx)]
        self._char_hitboxes: list = []  # [(QRect, line_idx, char_idx)]
        self.setMinimumHeight(400)
        self.setMouseTracking(True)

        # 音频引擎引用（用于 paintEvent 主动拉取高精度时间）
        self._audio_engine = None

        # 划词选中状态
        self._focus_line_idx: int = -1
        self._focus_char_idx: int = -1
        self._focus_line_range_end: int = -1
        self._focus_char_range_end: int = -1
        # 只有用户显式划选/点选后的 legacy 选区才能并入 Ctrl 扩展选区；
        # set_project 的初始光标和纯键盘导航不算“已选中”。
        self._focus_explicit: bool = False
        self._focus_dragging: bool = False

        # 跨字符选区（Ctrl+左键）。None = 使用上面的单段焦点选区（原始行为，
        # 单行/跨行划选都走这条路径）；非 None = 由若干不相邻字符组成的集合，
        # 仅保存被选中的字符对象，供“不涉及字符增减”的批量操作使用。
        self._ext_selection_chars: Optional[set[tuple[int, int]]] = None

        # 扩展选区里“最后选中”的字符。C 类操作退出跨字符选区时保留它，
        # 作为后续单字/单行操作的落点。
        self._last_selected_char: Optional[tuple[int, int]] = None

        # 右键 C 类动作执行前的可选守卫（由 EditorInterface 注入）。
        # 返回 False 表示当前不能执行（如注音分析忙碌），preview 不清选区。
        self._c_class_action_guard = None

        # Ctrl+左键手势状态：按下时只记锚点，拖动中仅预览，松开才提交。
        self._ctrl_sel_active: bool = False
        self._ctrl_sel_anchor: Optional[tuple[int, int]] = None
        self._ctrl_sel_extent: Optional[tuple[int, int]] = None
        self._ctrl_sel_press_pos: Optional[tuple[int, int]] = None
        self._ctrl_sel_moved: bool = False
        # Ctrl+左键按在空白处：本次释放不触发普通单击逻辑
        self._suppress_left_release: bool = False

        self._disable_click_jump: bool = False  # 禁用单击跳转
        self._disable_click_recenter: bool = False  # 禁用点击/双击时居中到目标行
        self._suppress_click_recenter: bool = False  # 瞬态：点击/双击处理窗口内抑制居中
        self._hide_hitbox_highlights: bool = False  # 隐藏 current/focus 域 hitbox 高亮

        # 单击/双击处理：快照机制
        # 单击时锁定 hitbox 快照，双击时使用快照判断，避免居中导致 hitbox 变化
        self._pending_click: Optional[dict] = None  # 按下时记录的待处理点击
        self._click_snapshot: Optional[dict] = None  # 单击后保存的快照（用于双击判断）
        self._double_click_handled: bool = False  # 双击已处理标志，防止 Release #2 触发单击逻辑
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(300)  # 双击间隔
        self._click_timer.timeout.connect(self._clear_click_snapshot)
        self._press_pos: Optional[tuple] = None  # 鼠标按下位置 (x, y)
        self._CLICK_MOVE_THRESHOLD = 5  # 像素级防抖阈值：移动超过此距离视为划词

        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.setInterval(50)
        self._drag_scroll_timer.timeout.connect(self._on_drag_scroll_tick)
        self._drag_scroll_direction: int = 0  # -1=上, 0=无, 1=下
        self._drag_scroll_mouse_y: int = 0

        # 视口稳定防抖：滚动中心变化后延迟触发「重算横向滚动条范围 + 预热
        # 可视窗口」。滚动中每帧重算会在未缓存行上触发整段渲染（卡顿源）。
        self._viewport_settle_timer = QTimer(self)
        self._viewport_settle_timer.setSingleShot(True)
        self._viewport_settle_timer.setInterval(120)
        self._viewport_settle_timer.timeout.connect(self._on_viewport_settled)

        # 缓存字体和 QFontMetrics，避免每帧重建
        self._font_current = ui_font(22, QFont.Weight.Bold)
        self._font_context = ui_font(18)
        self._font_ruby = ui_font(10)
        self._font_checkpoint = self._build_checkpoint_font(8)
        self._font_line_number = ui_font(10)
        # IPA 文本段字体（设置加载后由 set_font_sizes 重建）
        from strange_uta_game.frontend.font_utils import phonetic_lyrics_font

        self._font_current_ipa = phonetic_lyrics_font(self._font_current)
        self._font_context_ipa = phonetic_lyrics_font(self._font_context)
        self._font_ruby_ipa = phonetic_lyrics_font(self._font_ruby)
        self._fm_current = QFontMetrics(self._font_current)
        self._fm_context = QFontMetrics(self._font_context)
        self._fm_ruby = QFontMetrics(self._font_ruby)
        self._fm_current_ipa = QFontMetrics(self._font_current_ipa)
        self._fm_context_ipa = QFontMetrics(self._font_context_ipa)
        self._fm_ruby_ipa = QFontMetrics(self._font_ruby_ipa)
        self._fm_checkpoint = QFontMetrics(self._font_checkpoint)
        self._fm_line_number = QFontMetrics(self._font_line_number)
        # 矢量墨水盒缓存：(字体签名, 文本) → (x, top, width, height) | None
        self._vector_ink_cache: dict = {}
        self._line_number_margin = 45  # 行号左侧区域宽度
        self._ruby_spacing = 4  # Ruby与主文字的垂直间距
        self._cp_spacing = 4  # 节奏点标记顶端与整行 ink 底部的垂直间距
        # 节奏点字形墨水顶端/底端相对 baseline 的 y（top 通常为负，bottom 通常 ≥0）。
        # 用于把所有 marker 的视觉顶端统一锚定到 (_line_ink_bottom + cp_spacing)，
        # 与 fm 的 ascent 解耦，避免 cp_spacing=0 时仍残留 fm.ascent 与字形顶之间的"空白冠"。
        # bottom 用于高亮/选中矩形扩展到最大 marker ink 底端。
        self._marker_glyph_top = 0
        self._marker_glyph_bottom = 0

        # 歌词对齐方式："left" / "center" / "right"
        self._alignment: str = "center"
        self._alignment_margin: int = 168  # 左/右对齐时的页边距(px)

        # Checkpoint 标记字符（可配置）
        self._checkpoint_markers: dict[str, str] = {
            "cp_first_timed": "▶",
            "cp_first_empty": "▷",
            "cp_multi_timed": "▮",
            "cp_multi_empty": "▯",
            "cp_sentence_end_timed": "⬟",
            "cp_sentence_end_empty": "⬠",
        }

        # 导唱待办标记（半透明红色叠加在字符左上角，符号与字号可配置）
        self._needs_guide_symbol: str = "✚"
        self._needs_guide_size: int = 12
        self._font_needs_guide = ui_font(self._needs_guide_size, QFont.Weight.Bold)
        self._fm_needs_guide = QFontMetrics(self._font_needs_guide)

        # 逐句渲染数据缓存（避免每帧重复计算）
        # 每行有自己的版本号，只有数据改变的行才重新计算
        # 同一行的当前行/上下文行字号不同，必须各自保留一份排版结果；两条
        # 预热路径共同填充这里，绘制时再按现场视觉状态选择对应条目。
        self._sentence_cache: dict[tuple[int, str], dict] = {}
        self._line_versions: dict = {}  # line_idx -> version
        self._global_version: int = 0  # 全局版本号，用于字体变化等全局刷新
        self._is_playing: bool = False
        # 打轴指引总开关：控制正文走字预览及注音打轴状态（默认开，
        # 实际值在应用设置时经 set_preview_guide_enabled 覆盖）。
        self._preview_guide_enabled: bool = True
        self._guide_prev_alpha: float = 1.0       # 上一个打的字不透明度
        self._guide_curr_alpha: float = 0.5       # 正在打的字不透明度
        self._guide_next_alpha: float = 0.2       # 下一个要打的字不透明度
        self._guide_prev_enabled: bool = True
        self._guide_curr_enabled: bool = True
        self._guide_next_enabled: bool = True
        self._auto_scroll_enabled: bool = True  # 自动滚动开关，特殊场景可关闭
        self._auto_scroll_suspended: bool = False  # 用户交互后挂起自动滚动
        self._scroll_mode: str = "auto"  # auto / always / never
        self._last_auto_scroll_line_idx: int = -1  # 上次自动滚动到的行（与 _current_line_idx 独立）
        self._line_switch_points: list[tuple[int, int]] = []  # [(switch_ms, line_idx)]
        self._line_switch_points_dirty: bool = False  # 编辑后置脏，待下次使用前重建
        self._current_switch_idx: int = 0  # 当前快照位置
        self._SCROLL_SCALE = 1000  # 滚动条精度缩放因子（1行=1000单位）

        # 右侧纵向滚动条，用于快速浏览整个歌词预览
        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._scrollbar.setVisible(False)
        self._scrollbar.valueChanged.connect(self._on_scrollbar_changed)
        self._scrollbar.setSingleStep(self._SCROLL_SCALE)      # 点击箭头 = 1行
        self._scrollbar.setPageStep(self._SCROLL_SCALE * 5)     # 点击轨道 = 5行
        self._SCROLLBAR_WIDTH = 14

        # 底部横向滚动条：仅当实际排版超出文本视口时出现。普通滚轮仍归
        # 纵向歌词浏览使用；Alt+滚轮才改变此滚动条。
        self._horizontal_scrollbar = _AltWheelScrollBar(
            Qt.Orientation.Horizontal, self
        )
        self._horizontal_scrollbar.setVisible(False)
        self._horizontal_scrollbar.setSingleStep(40)
        self._horizontal_scrollbar.valueChanged.connect(
            self._on_horizontal_scrollbar_changed
        )
        self._HORIZONTAL_SCROLLBAR_HEIGHT = 14
        self._horizontal_max_shift = 0
        self._horizontal_layout_dirty = True
        # 横向滚动条范围只按可视窗口计算；滚动中心变化后防抖补算
        self._viewport_calc_center: float = -1.0

        # 监听主题变化，触发重绘
        theme.changed.connect(self.update)

        # 初始化 marker 字形顶端缓存
        self._recompute_marker_glyph_top()

    @staticmethod
    def _build_checkpoint_font(cp_size: int) -> QFont:
        """构造节奏点标记字体（微软雅黑，整数点字号）。

        节奏点符号 ``▶▷▮▯⬟⬠`` 微软雅黑不含，Qt 逐字 fallback 到 MS UI Gothic /
        Segoe UI Symbol。这些老式 fallback 字体在小字号带内嵌点阵（embedded
        bitmap），``drawText`` 走点阵 strike 时顶端一行像素会残缺（实测 12px 犯、
        13px 好）。本类对 marker 一律改用 ``QPainterPath`` 矢量轮廓填充绘制
        （见 _marker_path / paintEvent），从构造上绕开点阵，无需在字号上做手脚。
        """
        return ui_font(cp_size)

    def _marker_path(self, ch: str, x: float, baseline_y: float) -> QPainterPath:
        """返回 marker 字符在 (x, baseline_y) 处的矢量轮廓路径。

        ``addText`` 经字体合并取到 fallback 字体（MS UI Gothic/Segoe UI Symbol）的
        **轮廓**而非点阵 strike，``fillPath`` 填充必然得到完整字形——根治"marker
        自身画不全（顶端残缺）"。
        """
        path = QPainterPath()
        path.addText(x, baseline_y, self._font_checkpoint, ch)
        return path

    def _recompute_marker_glyph_top(self):
        """计算所有 marker 字形相对 baseline 的统一墨水上下边界（矢量精确）。

        直接取 ``QPainterPath.addText`` 的 ``boundingRect``：矢量包围盒与 DPR、
        点阵 strike 完全无关，且与实际 ``fillPath`` 绘制同源，positioning 与渲染
        天然一致。取所有 marker 字符的 top 最小（最高）、bottom 最大（最低）。

        绘制时用：

            marker_y(baseline) = _line_ink_bottom + cp_spacing - _marker_glyph_top
            marker 视觉顶端     = _line_ink_bottom + cp_spacing

        只在字体/marker 字符变化时重算一次，paint 路径零额外开销。
        """
        import math

        top_min = 0.0
        bottom_max = 0.0
        any_char = False
        for char_str in set(self._checkpoint_markers.values()):
            if not char_str:
                continue
            br = self._marker_path(char_str, 0.0, 0.0).boundingRect()
            if br.isEmpty():
                continue
            top = br.top()       # 相对 baseline，负=上方
            bottom = br.bottom()
            if not any_char:
                top_min, bottom_max, any_char = top, bottom, True
            else:
                top_min = min(top_min, top)
                bottom_max = max(bottom_max, bottom)

        if any_char:
            # 顶端向下取整（更靠上、不切顶），底端向上取整放大包络
            self._marker_glyph_top = math.floor(top_min)
            self._marker_glyph_bottom = math.ceil(bottom_max)
        else:
            fm = self._fm_checkpoint
            self._marker_glyph_top = -fm.ascent()
            self._marker_glyph_bottom = fm.descent()

    def set_playing(self, playing: bool):
        """由外部同步播放状态，用于决定 paintEvent 是否旁路缓存。"""
        was_playing = self._is_playing
        self._is_playing = bool(playing)
        if playing:
            self._build_line_switch_points()
        if was_playing != self._is_playing:
            self._mark_horizontal_layout_dirty()
            self.update()

    def _build_line_switch_points(self):
        """构建换行时间点快照，记录每个时间点应切换到哪一行。"""
        self._line_switch_points = []
        self._current_switch_idx = 0
        if not self._project or not self._project.sentences:
            return
        for idx, sentence in enumerate(self._project.sentences):
            ts = sentence.global_timing_start_ms
            if ts is not None:
                self._line_switch_points.append((ts, idx))
        self._line_switch_points.sort()
        self._line_switch_points_dirty = False

    def character_global_rect(self, line_idx: int, char_idx: int) -> QRect | None:
        """Return a rendered character hitbox in global screen coordinates."""
        for rect, hit_line, hit_char in self._char_hitboxes:
            if hit_line == line_idx and hit_char == char_idx:
                return QRect(self.mapToGlobal(rect.topLeft()), rect.size())
        return None

    def set_disable_click_jump(self, disable: bool):
        """设置是否禁用单击跳转功能。"""
        self._disable_click_jump = bool(disable)

    def set_disable_click_recenter(self, disable: bool):
        """设置是否禁用点击/双击时居中到目标行（不影响播放自动滚动与键盘导航）。"""
        self._disable_click_recenter = bool(disable)

    def set_hide_hitbox_highlights(self, hide: bool):
        """设置是否隐藏 current 域和 focus 域 hitbox 高亮。
        启用时仅在拖拽多选过程中绘制 focus 域高亮。
        """
        self._hide_hitbox_highlights = bool(hide)
        self.update()

    def set_preview_guide_enabled(self, enabled: bool):
        """设置打轴指引总开关（正文走字预览与注音打轴状态）。"""
        self._preview_guide_enabled = bool(enabled)
        self.update()

    def set_preview_guide_config(
        self,
        prev_alpha: float,
        curr_alpha: float,
        next_alpha: float,
        prev_enabled: bool,
        curr_enabled: bool,
        next_enabled: bool,
    ):
        """设置走字预览指引的逐群不透明度和开关。"""
        self._guide_prev_alpha = float(prev_alpha)
        self._guide_curr_alpha = float(curr_alpha)
        self._guide_next_alpha = float(next_alpha)
        self._guide_prev_enabled = bool(prev_enabled)
        self._guide_curr_enabled = bool(curr_enabled)
        self._guide_next_enabled = bool(next_enabled)
        self.update()

    def set_auto_scroll_enabled(self, enabled: bool):
        """设置是否启用自动滚动功能。特殊场景可关闭。"""
        self._auto_scroll_enabled = bool(enabled)
        self._mark_horizontal_layout_dirty()
        self.update()

    def set_scroll_mode(self, mode: str):
        """设置滚动模式：auto（操作后挂起 6 秒）/ always（始终滚动）/ never（不滚动）。"""
        self._scroll_mode = mode
        if mode == "never":
            self._auto_scroll_enabled = False
        else:
            self._auto_scroll_enabled = True
            if mode == "always":
                self._auto_scroll_suspended = False
        self._mark_horizontal_layout_dirty()
        self.update()

    def _suspend_auto_scroll(self):
        """用户交互时挂起自动滚动，通知外部停止 cooldown timer。"""
        if self._scroll_mode == "always":
            return
        self._auto_scroll_suspended = True
        self._mark_horizontal_layout_dirty()
        self.user_interaction_during_auto_scroll.emit()

    def resume_auto_scroll(self):
        """恢复自动滚动：将视口同步到当前播放行（不改变编辑光标）。"""
        self._auto_scroll_suspended = False
        self._mark_horizontal_layout_dirty()
        self._scroll_to_line(self._last_auto_scroll_line_idx)
        self.update()

    def set_duration(self, duration_ms: int):
        """设置音频总时长（用于行尾非停顿点时的wipe右边界）"""
        self._duration_ms = duration_ms

    def set_project(self, project: Project):
        self._project = project
        self._scroll_center_line = 0.0
        self._sentence_cache.clear()
        # focus 域默认值：首个非空行的首字符。空项目保持 -1。
        # focus 是用户视觉/操作真理来源（点字符/拖选/纯←→），与 current（cp 域反馈）独立。
        self._focus_line_idx = -1
        self._focus_char_idx = -1
        self._focus_line_range_end = -1
        self._focus_char_range_end = -1
        self._focus_explicit = False
        self._focus_dragging = False
        self._ext_selection_chars = None
        self._last_selected_char = None
        self._reset_ctrl_selection_gesture()
        self._suppress_left_release = False
        if project and project.sentences:
            for idx, sentence in enumerate(project.sentences):
                if sentence.characters:
                    self._focus_line_idx = idx
                    self._focus_char_idx = 0
                    self._focus_line_range_end = idx
                    self._focus_char_range_end = 0
                    break
            # 预热：同步只渲染一屏（focus 附近），剩余分批后台补齐
            self._start_prewarm()
        # 换工程后换行快照必须重建：旧快照的行号指向新工程会找错播放行
        # （暂停 seek / 未播放时局部重绘也依赖此快照定位受影响行）
        self._build_line_switch_points()
        self._update_scrollbar_range()
        self._sync_scrollbar_to_scroll_center()
        self._mark_horizontal_layout_dirty()
        self._update_horizontal_scrollbar_range()
        self.request_repaint()

    def set_focus_position(self, line_idx:int = 0,char_idx: int = 0):
        # 用于打轴状态下更新foucs
        # 焦点移动意味着用户已离开跨字符选区（点击/键盘/打轴推进/结构编辑），
        # 扩展选区在此统一失效；原始单段选区仍按既有字段工作。
        if self._ext_selection_chars is not None or self._ctrl_sel_active:
            self.clear_extended_selection()
        new_line = float(line_idx)
        # 快路径：视口已居中且行/字符均未变化时仅刷新 focus 域
        # （行号对行号、字符号对字符号，此前曾错位比较导致 focus 行域残留旧值）
        if (new_line == self._scroll_center_line
                and line_idx == self._current_line_idx
                and char_idx == self._current_char_idx):
            self._focus_line_idx = line_idx
            self._focus_char_idx = char_idx
            self._focus_line_range_end = line_idx
            self._focus_char_range_end = char_idx
            self._focus_explicit = False
            self.request_repaint()
            return
        self._focus_line_idx = line_idx
        self._focus_char_idx = char_idx
        self._focus_line_range_end = line_idx
        self._focus_char_range_end = char_idx
        self._focus_explicit = False
        if self._is_playing:
            self._warm_nearby_cache(budget=2)
        self.request_repaint()

    def set_current_position(self, line_idx: int, char_idx: int = 0):
        """更新当前打轴位置（行+字符），并居中滚动。"""
        if line_idx == self._current_line_idx:
            self._current_char_idx = char_idx
            self.request_repaint()
            return
        self._current_line_idx = line_idx
        self._current_char_idx = char_idx
        self._mark_horizontal_layout_dirty()
        # 播放中：视口居中受滚动模式/挂起状态管控（set_current_time_ms 统一负责视口）
        # 非播放（手动导航）：始终居中
        if not self._is_playing or (self._auto_scroll_enabled and not self._auto_scroll_suspended):
            self.scroll_current_line_to_center()
        # 行切换时重新锚定预热中心（仅播放期间）
        if self._is_playing:
            self._warm_nearby_cache(budget=2)
        self.request_repaint()

    def scroll_current_line_to_center(self):
        """将当前行滚动到视口中央。

        幂等保护：若目标 line_idx 已是当前 scroll_center_line，
        避免短暂跳变导致空白行。

        点击/双击处理窗口内（_suppress_click_recenter）按用户设置跳过居中：
        编辑光标仍移动，仅视口不滚动。键盘导航/打轴推进/播放自动滚动不经此抑制。
        """
        if self._suppress_click_recenter:
            return
        new_line = float(self._current_line_idx)
        if new_line == self._scroll_center_line:
            return
        self._scroll_center_line = new_line
        self._sync_scrollbar_to_scroll_center()
        self.request_repaint()

    def is_multi_line_selection(self) -> bool:
        """原始单段选区是否跨行（扩展选区返回 False，见 is_extended_selection）。"""
        sel = self._legacy_normalized_selection()
        return sel is not None and sel[0] != sel[2]

    def is_extended_selection(self) -> bool:
        """是否存在 Ctrl 跨字符多选（与原始单段划选互斥）。"""
        return self._ext_selection_chars is not None

    def get_normalized_selection(self):
        """返回原始单段选区 (start_line, start_char, end_line, end_char) 闭区间，或 None。

        扩展选区（Ctrl 多选）没有单段等价表示，返回 None；调用方应改用
        :meth:`get_selected_char_ranges` / :meth:`get_selected_text`。
        """
        if self._ext_selection_chars is not None:
            return None
        return self._legacy_normalized_selection()

    def _legacy_normalized_selection(self):
        """原始单段选区的规范化结果（忽略扩展选区）。"""
        if self._focus_line_idx < 0 or self._focus_char_idx < 0 or self._focus_line_range_end < 0:
            return None
        if self._focus_line_idx < self._focus_line_range_end:
            return (
                self._focus_line_idx,
                self._focus_char_idx,
                self._focus_line_range_end,
                self._focus_char_range_end,
            )
        elif self._focus_line_idx > self._focus_line_range_end:
            return (
                self._focus_line_range_end,
                self._focus_char_range_end,
                self._focus_line_idx,
                self._focus_char_idx,
            )
        else:
            lo = min(self._focus_char_idx, self._focus_char_range_end)
            hi = max(self._focus_char_idx, self._focus_char_range_end)
            return (self._focus_line_idx, lo, self._focus_line_idx, hi)

    # ── 跨字符多选（Ctrl+左键） ──────────────────────────────

    def get_selected_char_ranges(self) -> list[tuple[int, int, int]]:
        """当前选区的逐行闭区间 ``[(line_idx, start_char, end_char), ...]``。

        - 原始单段选区：按行展开（跨行选区中间行整行选中），与原行为一致；
        - Ctrl 扩展选区：返回所有被选中字符合并后的逐行连续段；
        - 无选区：返回空列表（调用方按既有约定回退当前字符）。
        """
        if not self._project or not self._project.sentences:
            return []
        if self._ext_selection_chars is not None:
            return self._coalesce_chars_to_spans(self._ext_selection_chars)
        sel = self._legacy_normalized_selection()
        if sel is None:
            return []
        return self._range_to_line_spans(*sel)

    def get_selected_char_count(self) -> int:
        """当前选区覆盖的字符对象总数。"""
        return sum(e - s + 1 for _, s, e in self.get_selected_char_ranges())

    def get_selected_text(self) -> str:
        """当前选区覆盖的字符文本（按阅读顺序拼接，不含行分隔）。"""
        if not self._project or not self._project.sentences:
            return ""
        parts: list[str] = []
        for line_idx, start, end in self.get_selected_char_ranges():
            if 0 <= line_idx < len(self._project.sentences):
                chars = self._project.sentences[line_idx].characters
                parts.extend(chars[i].char for i in range(start, end + 1))
        return "".join(parts)

    def selection_contains(self, line_idx: int, char_idx: int) -> bool:
        """指定字符是否在当前选区内。"""
        for li, start, end in self.get_selected_char_ranges():
            if li == line_idx and start <= char_idx <= end:
                return True
        return False

    def clear_extended_selection(self) -> bool:
        """清除 Ctrl 扩展选区；返回是否确实清除了内容。

        不清除 legacy 焦点光标：焦点仍是用户当前的编辑位置，只是不再有
        跨字符选区高亮。
        """
        changed = self._ext_selection_chars is not None
        self._ext_selection_chars = None
        self._last_selected_char = None
        self._reset_ctrl_selection_gesture()
        if changed:
            self.update()
            self.selection_changed.emit()
        return changed

    def collapse_extended_selection_to_last(self) -> Optional[tuple[int, int]]:
        """退出扩展选区，只保留“最后选中”的字符并返回它。

        供 C 类操作（需要退化为普通单字/单行处理的操作）入口调用。
        若最后选中记录失效，则回退到选区中阅读顺序最后的字符。
        """
        if self._ext_selection_chars is None:
            return None
        chars = set(self._ext_selection_chars)
        target = self._last_selected_char
        if target not in chars:
            target = max(chars) if chars else None
        self._ext_selection_chars = None
        self._last_selected_char = None
        self._reset_ctrl_selection_gesture()
        if target is not None and self._project:
            line_idx, char_idx = target
            if 0 <= line_idx < len(self._project.sentences):
                n = len(self._project.sentences[line_idx].characters)
                if n:
                    char_idx = max(0, min(char_idx, n - 1))
                    self._set_legacy_selection_range(
                        line_idx, char_idx, line_idx, char_idx
                    )
                    self.update()
                    self.selection_changed.emit()
                    return (line_idx, char_idx)
        self.update()
        self.selection_changed.emit()
        return None

    def _collapse_extended_then_emit(
        self, signal, target_line: int, target_char: int, *args
    ) -> None:
        """右键菜单 C 类操作：先退出扩展选区，再按最后选中字符/行执行。

        target_* 仅用于非扩展选区的普通菜单路径；扩展选区按需求保留
        “最后选中”的字符，而不是右键命中的字符。
        """
        if self._ext_selection_chars is not None:
            guard = getattr(self, "_c_class_action_guard", None)
            if callable(guard) and not guard():
                return
            last = self.collapse_extended_selection_to_last()
            if last is not None:
                new_args = list(args)
                if new_args:
                    new_args[0] = last[0]
                if len(new_args) >= 2:
                    new_args[1] = last[1]
                signal.emit(*new_args)
                return
        signal.emit(*args)

    def _range_to_line_spans(
        self, start_line: int, start_char: int, end_line: int, end_char: int
    ) -> list[tuple[int, int, int]]:
        """把单段跨行选区展开为逐行连续段（跳过空行，索引夹紧）。"""
        if not self._project or not self._project.sentences:
            return []
        total = len(self._project.sentences)
        start_line = max(0, min(int(start_line), total - 1))
        end_line = max(0, min(int(end_line), total - 1))
        if start_line > end_line:
            start_line, end_line = end_line, start_line
            start_char, end_char = end_char, start_char
        spans: list[tuple[int, int, int]] = []
        for line_idx in range(start_line, end_line + 1):
            chars = self._project.sentences[line_idx].characters
            if not chars:
                continue
            n = len(chars)
            s = int(start_char) if line_idx == start_line else 0
            e = int(end_char) if line_idx == end_line else n - 1
            s = max(0, min(s, n - 1))
            e = max(0, min(e, n - 1))
            if s <= e:
                spans.append((line_idx, s, e))
        return spans

    def _iter_range_chars(self, start_line: int, start_char: int, end_line: int, end_char: int):
        """迭代单段范围覆盖的 (line_idx, char_idx)，逐行连续。"""
        for line_idx, start, end in self._range_to_line_spans(
            start_line, start_char, end_line, end_char
        ):
            for char_idx in range(start, end + 1):
                yield line_idx, char_idx

    def _coalesce_chars_to_spans(
        self, chars: "set[tuple[int, int]]"
    ) -> list[tuple[int, int, int]]:
        """把字符集合合并为逐行连续闭区间列表（已排序）。"""
        by_line: dict[int, list[int]] = {}
        for line_idx, char_idx in chars:
            by_line.setdefault(line_idx, []).append(char_idx)
        spans: list[tuple[int, int, int]] = []
        for line_idx in sorted(by_line):
            if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
                continue
            n = len(self._project.sentences[line_idx].characters)
            cols = sorted({c for c in by_line[line_idx] if 0 <= c < n})
            if not cols:
                continue
            start = end = cols[0]
            for col in cols[1:]:
                if col == end + 1:
                    end = col
                else:
                    spans.append((line_idx, start, end))
                    start = end = col
            spans.append((line_idx, start, end))
        return spans

    def _simple_range_from_chars(
        self, chars: "set[tuple[int, int]]"
    ) -> Optional[tuple[int, int, int, int]]:
        """若字符集合恰好等价于原始单段选区，返回其规范区间；否则 None。"""
        spans = self._coalesce_chars_to_spans(chars)
        if not spans or not self._project:
            return None
        if len(spans) == 1:
            line_idx, start, end = spans[0]
            return (line_idx, start, line_idx, end)
        lines = [s[0] for s in spans]
        if len(set(lines)) != len(lines):
            # 同一行内出现多个不连续段 → 不是单段选区
            return None
        first, last = spans[0], spans[-1]
        first_chars = self._project.sentences[first[0]].characters
        last_chars = self._project.sentences[last[0]].characters
        if not first_chars or first[2] != len(first_chars) - 1:
            return None
        if not last_chars or last[1] != 0:
            return None
        by_line = {line_idx: (s, e) for line_idx, s, e in spans}
        for line_idx in range(first[0], last[0] + 1):
            chars_here = self._project.sentences[line_idx].characters
            if not chars_here:
                continue
            if line_idx not in by_line:
                return None
            s, e = by_line[line_idx]
            if line_idx == first[0]:
                if s != first[1] or e != len(chars_here) - 1:
                    return None
            elif line_idx == last[0]:
                if s != 0 or e != last[2]:
                    return None
            elif s != 0 or e != len(chars_here) - 1:
                return None
        return (first[0], first[1], last[0], last[2])

    def _set_legacy_selection_range(
        self, start_line: int, start_char: int, end_line: int, end_char: int
    ) -> None:
        self._focus_line_idx = int(start_line)
        self._focus_char_idx = int(start_char)
        self._focus_line_range_end = int(end_line)
        self._focus_char_range_end = int(end_char)
        self._focus_explicit = True
        self._focus_dragging = False

    def _extended_chars_for_update(self) -> "set[tuple[int, int]]":
        """当前选区（原始或扩展）转成字符集合，供 Ctrl 手势增量修改。"""
        if self._ext_selection_chars is not None:
            return set(self._ext_selection_chars)
        if not self._focus_explicit:
            return set()
        sel = self._legacy_normalized_selection()
        if sel is None:
            return set()
        return set(self._iter_range_chars(*sel))

    def _commit_extended_selection(
        self, chars: "set[tuple[int, int]]", *, notify: bool = True
    ) -> None:
        """提交扩展选区集合；若它恰好构成单段选区，自动退化为原始选区。"""
        self._ext_selection_chars = set(chars) if chars else None
        if self._ext_selection_chars is not None:
            simple = self._simple_range_from_chars(self._ext_selection_chars)
            if simple is not None:
                self._ext_selection_chars = None
                self._set_legacy_selection_range(*simple)
        if self._ext_selection_chars is None:
            self._last_selected_char = None
        if notify:
            self.update()
            self.selection_changed.emit()

    def add_extended_selection_range(
        self,
        start_line: int,
        start_char: int,
        end_line: int,
        end_char: int,
        *,
        active_char: Optional[tuple[int, int]] = None,
    ) -> bool:
        """把一段整字范围追加到 Ctrl 选区；返回选区是否发生变化。"""
        chars = self._extended_chars_for_update()
        new_chars = set(
            self._iter_range_chars(start_line, start_char, end_line, end_char)
        )
        if not new_chars or new_chars <= chars:
            return False
        chars |= new_chars
        self._commit_extended_selection(chars)
        if self._ext_selection_chars is not None:
            target = active_char
            if target not in self._ext_selection_chars:
                target = (end_line, end_char)
            if target in self._ext_selection_chars:
                self._last_selected_char = target
            # 扩展选区没有单一范围；把焦点落在本次追加范围的落点，
            # 让仍按 focus 取目标的旧路径至少指向选区内。
            total = len(self._project.sentences) if self._project else 0
            if total:
                line_idx = max(0, min(int(start_line), total - 1))
                n = len(self._project.sentences[line_idx].characters)
                if n:
                    char_idx = max(0, min(int(start_char), n - 1))
                    self._focus_line_idx = line_idx
                    self._focus_char_idx = char_idx
                    self._focus_line_range_end = line_idx
                    self._focus_char_range_end = char_idx
        return True

    def toggle_extended_selection_char(self, line_idx: int, char_idx: int) -> bool:
        """切换一个字符对象的选中状态（Ctrl+单击）。返回选区是否变化。"""
        if not self._project or line_idx < 0 or line_idx >= len(self._project.sentences):
            return False
        chars_here = self._project.sentences[line_idx].characters
        if char_idx < 0 or char_idx >= len(chars_here):
            return False
        chars = self._extended_chars_for_update()
        key = (line_idx, char_idx)
        if key in chars:
            chars.discard(key)
            if chars:
                keep_line, keep_char = min(chars)
                self._focus_line_idx = keep_line
                self._focus_char_idx = keep_char
                self._focus_line_range_end = keep_line
                self._focus_char_range_end = keep_char
                if self._last_selected_char == key:
                    self._last_selected_char = max(chars)
            else:
                self._focus_line_idx = -1
                self._focus_char_idx = -1
                self._focus_line_range_end = -1
                self._focus_char_range_end = -1
                self._last_selected_char = None
        elif chars:
            chars.add(key)
            self._last_selected_char = key
            self._focus_line_idx = line_idx
            self._focus_char_idx = char_idx
            self._focus_line_range_end = line_idx
            self._focus_char_range_end = char_idx
        else:
            # 尚无选区：先建立单字符选区（原始路径即可）
            self._focus_line_idx = line_idx
            self._focus_char_idx = char_idx
            self._focus_line_range_end = line_idx
            self._focus_char_range_end = char_idx
            self._focus_explicit = True
            self._last_selected_char = key
            self._focus_dragging = False
            self.update()
            self.selection_changed.emit()
            return True
        self._commit_extended_selection(chars)
        return True

    def _ctrl_pending_chars(self) -> "set[tuple[int, int]]":
        if not self._ctrl_sel_active or self._ctrl_sel_anchor is None:
            return set()
        anchor = self._ctrl_sel_anchor
        extent = self._ctrl_sel_extent or anchor
        if anchor[0] > extent[0] or (anchor[0] == extent[0] and anchor[1] > extent[1]):
            anchor, extent = extent, anchor
        return set(self._iter_range_chars(anchor[0], anchor[1], extent[0], extent[1]))

    def _reset_ctrl_selection_gesture(self) -> None:
        self._ctrl_sel_active = False
        self._ctrl_sel_anchor = None
        self._ctrl_sel_extent = None
        self._ctrl_sel_press_pos = None
        self._ctrl_sel_moved = False

    def _char_at_position(self, x: int, y: int) -> Optional[tuple[int, int]]:
        """把控件坐标映射到最近的字符对象（用于 Ctrl 手势，允许落在空白处）。"""
        if not self._project or not self._project.sentences:
            return None
        for rect, line_idx, char_idx in self._char_hitboxes:
            if rect.contains(x, y):
                return line_idx, char_idx
        nearest = self._find_nearest_hitbox(x, y)
        if nearest:
            return nearest[1], nearest[2]
        h = self.height()
        if h <= 0:
            return None
        line_height = h / max(1, self._visible_lines)
        center_y = h / 2.0
        total = len(self._project.sentences)
        # 仅允许吸附到“当前绘制窗口内”的行：远离歌词区域（如顶部大片空白）
        # 的 Ctrl 单击/拖动不应改选区；空行也不吸附到相邻行，避免误选。
        half_visible = self._visible_lines / 2.0 + 1
        first_visible = max(0, int(self._scroll_center_line - half_visible))
        last_visible = min(total - 1, int(self._scroll_center_line + half_visible))
        raw_line = int(round(self._scroll_center_line + (y - center_y) / line_height))
        if raw_line < first_visible or raw_line > last_visible:
            return None
        by_line: dict[int, list[tuple[QRect, int]]] = {}
        for rect, line_idx, char_idx in self._char_hitboxes:
            by_line.setdefault(line_idx, []).append((rect, char_idx))
        rects = by_line.get(raw_line)
        if not rects:
            return None

        def _hdist(item):
            rect = item[0]
            if x < rect.left():
                return rect.left() - x
            if x > rect.right():
                return x - rect.right()
            return 0

        rect, char_idx = min(rects, key=_hdist)
        return raw_line, char_idx

    def _begin_ctrl_selection(self, x: int, y: int) -> bool:
        hit = self._char_at_position(x, y)
        if hit is None:
            return False
        self._suppress_left_release = False
        self._ctrl_sel_active = True
        self._drag_scroll_timer.stop()
        self._drag_scroll_direction = 0
        self._ctrl_sel_anchor = hit
        self._ctrl_sel_extent = hit
        self._ctrl_sel_press_pos = (x, y)
        self._ctrl_sel_moved = False
        # Ctrl 点击不参与单击/双击链路
        self._pending_click = None
        self._press_pos = None
        self._click_snapshot = None
        self._click_timer.stop()
        self._double_click_handled = False
        self._focus_dragging = False
        return True

    def _finish_ctrl_selection(self, x: int, y: int) -> None:
        anchor = self._ctrl_sel_anchor
        extent = self._ctrl_sel_extent or anchor
        press = self._ctrl_sel_press_pos
        moved = self._ctrl_sel_moved
        if press is not None:
            dx = x - press[0]
            dy = y - press[1]
            if dx * dx + dy * dy > self._CLICK_MOVE_THRESHOLD * self._CLICK_MOVE_THRESHOLD:
                moved = True
        self._reset_ctrl_selection_gesture()
        self._suppress_left_release = False
        self._drag_scroll_timer.stop()
        self._drag_scroll_direction = 0
        if anchor is None:
            return
        if moved and extent is not None and extent != anchor:
            active_char = extent
            if anchor[0] > extent[0] or (
                anchor[0] == extent[0] and anchor[1] > extent[1]
            ):
                anchor, extent = extent, anchor
            self.add_extended_selection_range(
                anchor[0],
                anchor[1],
                extent[0],
                extent[1],
                active_char=active_char,
            )
        else:
            self.toggle_extended_selection_char(anchor[0], anchor[1])
        self.update()

    def _scroll_to_line(self, line_idx: int):
        """纯视觉滚动：将指定行移到视口中央，不改变 _current_line_idx（编辑光标）。

        用于自动滚动，与编辑光标完全解耦。
        """
        if line_idx < 0:
            return
        new_line = float(line_idx)
        if new_line == self._scroll_center_line:
            return
        self._scroll_center_line = new_line
        self._sync_scrollbar_to_scroll_center()
        self.request_repaint()

    def _find_line_for_time(self, time_ms: int) -> Optional[int]:
        """查找当前时间对应的歌词行索引（使用快照索引，O(log n)判断）。

        Args:
            time_ms: 当前播放时间（毫秒）

        Returns:
            行索引，如果没有找到返回 None
        """
        points = self._line_switch_points
        if not points:
            return None

        # Manual seeks can jump backwards or far forwards. The old incremental
        # scan reset to 0 on backwards seeks, making late-song jumps O(n).
        idx = bisect_right(points, (time_ms, sys.maxsize)) - 1
        idx = max(0, min(idx, len(points) - 1))
        self._current_switch_idx = idx
        return points[idx][1]

    @log_slow_method(
        "preview.set_current_time_ms",
        20,
        lambda self, args, kwargs: {
            "time_ms": args[0] if args else kwargs.get("time_ms"),
            "playing": self._is_playing,
            "line": self._current_line_idx,
            "auto_line": self._last_auto_scroll_line_idx,
        },
    )
    def set_current_time_ms(self, time_ms: int):
        prev_time = self._current_time_ms
        self._current_time_ms = time_ms
        # 播放期间按就近扩散顺序预热少量邻近行，降低视口内首帧卡顿
        if self._is_playing:
            self._warm_nearby_cache(budget=2)
        # 自动滚动：检测播放行变化（用于 cooldown 判断），
        # 仅在未挂起时才移动视口（不改变编辑光标 _current_line_idx）
        line_changed = False
        if self._auto_scroll_enabled and self._is_playing:
            target_line_idx = self._find_line_for_time(time_ms)
            if target_line_idx is not None:
                if target_line_idx != self._last_auto_scroll_line_idx:
                    line_changed = True
                    self._last_auto_scroll_line_idx = target_line_idx
                    self._mark_horizontal_layout_dirty()
                    self.auto_scroll_line_changed.emit()
                    if not self._auto_scroll_suspended:
                        self._scroll_to_line(target_line_idx)
        if line_changed:
            # 换行瞬间：视口滚动 + 播放行切换大字体 + 上一行走字收尾，
            # 受影响像素遍布视口，全量重绘
            self.update()
        else:
            self._update_dynamic_playback_rows(prev_time)

    def _line_repaint_rect(self, line_idx: int) -> QRect:
        """单行重绘带：覆盖该行实际绘制内容的完整外沿。

        行内容垂直分布（以行基线 y_center 为基准）：
        - 上方：Ruby（基线 ``y - ascent - ruby_spacing``，墨水再向上一个
          ruby 字高；wipe 裁剪区比墨水再高 2px、连词框再高 1px）。
        - 下方：节奏点 marker（``descent + cp_spacing + marker 字高``）。

        注意不能直接用 ``height / _visible_lines`` 的几何行带：紧凑行距 /
        大字号下几何行带小于内容高度，按它裁剪会把 Ruby 顶端留在重绘区
        外，走字出现"只有下半在动"的横向截断条带。这里按内容包络计算，
        光标行与上下文行字号不同，取两者较大值，光标移动时重绘带不收缩。
        """
        if self.height() <= 0 or self._visible_lines <= 0:
            return self.rect()
        line_height = self.height() / self._visible_lines
        center_y = self.height() / 2.0
        y_center = center_y + (line_idx - self._scroll_center_line) * line_height
        ascent = max(self._fm_current.ascent(), self._fm_context.ascent())
        descent = max(self._fm_current.descent(), self._fm_context.descent())
        ruby_height = max(
            self._fm_ruby.height(), getattr(self, "_fm_ruby_ipa", self._fm_ruby).height()
        )
        # IPA 叠加附加符（ĩ̯ 等）与声调字母可能超出字体盒上下沿：加
        # ruby 字体高度的一半作为墨水余量，避免重绘带切掉附加符顶端
        pad = 4 + ruby_height // 2
        top = math.floor(
            y_center - ascent - self._ruby_spacing - ruby_height - pad
        )
        bottom = math.ceil(
            y_center + descent + self._cp_spacing + self._fm_checkpoint.height() + pad
        )
        return QRect(0, top, self.width(), max(1, bottom - top)).intersected(self.rect())

    def _wipe_affected_lines(self, t0: Optional[int], t1: int) -> Optional[set[int]]:
        """返回 ``[t0, t1]`` 之间 wipe 状态可能变化的行集合；跨多行返回 None。

        wipe 是 ``_current_time_ms`` 的纯函数，与自动滚动开关、播放状态均
        无关。受影响行 = 新旧时间各自所在的行 + 区间内开始演唱的行 ± 1 个
        邻居（夹在两个已打轴行之间的未打轴行由邻行时间戳插值走字，不会
        出现在换行快照里）。区间跨过多行（大跨度 seek）时返回 None，调用
        方回退全量重绘。
        """
        if not self._project or not self._project.sentences:
            return set()
        if not self._line_switch_points or self._line_switch_points_dirty:
            # 暂停 seek / 加载后未播放过时快照可能尚未构建；
            # 暂停期间的编辑会把快照置脏，局部重绘前必须重建
            self._build_line_switch_points()
        if not self._line_switch_points:
            return set()
        if t0 is None or t0 == t1:
            lo = hi = t1
        else:
            lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        total = len(self._project.sentences)
        rows: set[int] = set()
        for tv in (lo, hi):
            li = self._find_line_for_time(tv)
            if li is not None:
                rows.add(li)
        for ts, li in self._line_switch_points:
            if lo < ts <= hi and 0 <= li < total:
                rows.add(li)
        rows |= {
            li + d for li in list(rows) for d in (-1, 1) if 0 <= li + d < total
        }
        if len(rows) > 5:
            return None
        return rows

    def _update_dynamic_playback_rows(self, prev_time: Optional[int] = None) -> None:
        """逐帧只重绘走字状态会变化的行带，其余像素由 backing store 保留。

        行集合必须覆盖所有 wipe 随时间变化的行，而不是仅编辑光标行
        （1.5.1 曾因此回归：从不滚动模式下已打轴行失去光标后走字冻结）：
        - 从不滚动模式：``_last_auto_scroll_line_idx`` 不更新，播放行靠
          ``_wipe_affected_lines`` 按时间区间带出。
        - 暂停 seek：新旧时间所在行的 wipe 显示都会变化。
        - 大跨度 seek：受影响行过多，回退全量重绘。
        """
        rows = {self._effective_current_line()}
        # 播放行与编辑光标行可能不同（滚动挂起 / 从不滚动），二者都要刷新；
        # 自动滚动激活时 effective_current 已是播放行，此处为幂等兜底。
        if (
            self._is_playing
            and self._auto_scroll_enabled
            and self._last_auto_scroll_line_idx >= 0
        ):
            rows.add(self._last_auto_scroll_line_idx)
        if self._preview_guide_enabled:
            rows.add(self._current_line_idx)
        wipe_rows = self._wipe_affected_lines(prev_time, self._current_time_ms)
        if wipe_rows is None:
            self.update()
            return
        rows.update(wipe_rows)
        dirty = QRect()
        for line_idx in rows:
            dirty = dirty.united(self._line_repaint_rect(line_idx))
        if dirty.isValid() and not dirty.isEmpty():
            self.update(dirty)
        else:
            self.update()

    def _start_prewarm(self) -> None:
        """打开/换项目：同步预热 current_line 附近一屏（原就近预热口径）。

        预热拆为两路，不再做全曲扫描（旧全量预渲染 10k 字符实测 ≈67ms 卡 UI）：
          1. current_line 就近：本方法（打开时一次）+ 播放/导航期间的
             _warm_nearby_cache 增补（既有调用点不变）；
          2. 可视窗口：_on_viewport_settled（滚动停稳后防抖触发）。
        两路渲染前都会经 _get_sentence_render_data 的行版本校验——版本未变
        直接复用缓存条目，做过即跳过。非首尾行的渲染数据受前后第一个有
        时间戳行的影响（wipe 锚点跨行借用），该依赖由
        _invalidate_line_and_dependents 在编辑时把受影响行版本 +1 表达，
        预热据此自动重渲，不会互相覆盖。
        """
        self._warm_nearby_cache(budget=max(getattr(self, "_visible_lines", 8) + 2, 8))

    def _on_viewport_settled(self) -> None:
        """视口稳定后：重算横向滚动条范围并预热可视窗口内的行。"""
        if not self._project or not self._project.sentences:
            return
        # 可视窗口测量（内部对已缓存行零成本复用）
        self._update_horizontal_scrollbar_range()
        if not self.isVisible():
            return
        n = len(self._project.sentences)
        half = self._visible_lines / 2 + 1
        first = max(0, int(self._scroll_center_line - half))
        last = min(n - 1, int(self._scroll_center_line + half) + 1)
        for idx in range(first, last + 1):
            sentence = self._project.sentences[idx]
            if not sentence.characters:
                continue
            self._prewarm_sentence_states(idx, sentence)

    def showEvent(self, a0):
        super().showEvent(a0)
        # 重新可见：立即处理当前视口（不等防抖）
        self._on_viewport_settled()

    @log_slow_method(
        "preview.warm_nearby_cache",
        12,
        lambda self, args, kwargs: {
            "budget": args[0] if args else kwargs.get("budget", 2),
            "line": self._current_line_idx,
            "auto_line": self._last_auto_scroll_line_idx,
            "cache": len(self._sentence_cache),
        },
    )
    def _warm_nearby_cache(self, budget: int = 2) -> None:
        """按 L, L+1, L-1, L+2, L-2, ... 的就近扩散顺序预热 _sentence_cache。

        - 每次最多预热 budget 句，避免阻塞 paint
        - 每行同时预热 ``cur`` / ``ctx`` 两种排版状态
        - 两种状态均已缓存且版本匹配的行直接跳过，不重复计算
        - 行号边界：center 被 clamp 到 [0, n-1]，扩散候选再次越界检查；
          两端都越界后早退，避免无意义空转
        """
        if not self._project or not self._project.sentences:
            return
        sentences = self._project.sentences
        n = len(sentences)
        if n <= 0:
            return
        # 播放+自动滚动时以播放行为中心预热，否则以编辑光标为中心
        if (
            self._is_playing
            and self._auto_scroll_enabled
            and not self._auto_scroll_suspended
            and self._last_auto_scroll_line_idx >= 0
        ):
            center = max(0, min(self._last_auto_scroll_line_idx, n - 1))
        else:
            center = max(0, min(self._current_line_idx, n - 1))
        warmed = 0
        max_radius = max(4, budget * 2, self._visible_lines)
        max_radius = min(max_radius, n - 1)
        for offset in range(max_radius + 1):
            # 扩散序：0, +1, -1, +2, -2, +3, -3, ...
            if offset == 0:
                candidates = (center,)
            else:
                candidates = (center + offset, center - offset)
            any_valid = False
            for idx in candidates:
                if idx < 0 or idx >= n:
                    continue
                any_valid = True
                sentence = sentences[idx]
                if not sentence.characters:
                    continue
                if not self._prewarm_sentence_states(idx, sentence):
                    continue
                warmed += 1
                if warmed >= budget:
                    return
            # 两端都越界（向前到 0、向后到 n-1 之外）→ 无需继续扩散
            if offset > 0 and not any_valid:
                return

    def _prewarm_sentence_states(self, idx: int, sentence) -> bool:
        """补齐一行的当前行/上下文行排版缓存，返回是否实际计算过。"""
        rendered = False
        for font_key, fm in (("cur", self._fm_current), ("ctx", self._fm_context)):
            cache_key = (idx, font_key)
            entry = self._sentence_cache.get(cache_key)
            line_version = self._line_versions.get(idx, 0)
            if (
                entry
                and entry["v"] == line_version
                and entry["gv"] == self._global_version
            ):
                continue
            self._get_sentence_render_data(idx, sentence, fm, font_key)
            rendered = True
        return rendered

    def set_global_offset(self, offset_ms: int):
        """设置全局偏移量（毫秒），更新所有字符的时间戳"""
        self._global_offset_ms = offset_ms
        # 偏移变更时，清除缓存使 wipe 区间重新计算
        self._sentence_cache.clear()

    def set_audio_engine(self, engine):
        """设置音频引擎引用，使 paintEvent 可主动拉取高精度时间。"""
        self._audio_engine = engine
        self._line_versions.clear()
        self._global_version += 1
        self.update()

    def set_alignment(self, alignment: str):
        """设置歌词对齐方式。

        Args:
            alignment: "left"（左对齐）、"center"（居中对齐）或 "right"（右对齐）
        """
        if alignment not in ("left", "center", "right"):
            alignment = "center"
        if self._alignment != alignment:
            self._alignment = alignment
            self._mark_horizontal_layout_dirty()
            self.update()

    def set_alignment_margin(self, margin: int):
        """设置左/右对齐时的页边距。

        Args:
            margin: 页边距像素值
        """
        margin = max(0, min(500, margin))
        if self._alignment_margin != margin:
            self._alignment_margin = margin
            self._mark_horizontal_layout_dirty()
            self.update()

    def set_font_sizes(self, base_size: int, current_line_size: int = 0, ruby_size: int = 10, cp_size: int = 8, line_height_factor: float = 1.20, ruby_spacing: int = 4, main_font: str = DEFAULT_FONT_FAMILY, ruby_font: str = DEFAULT_FONT_FAMILY, cp_spacing: int = 4):
        """设置字体大小/字体族并自动适配预览行数。

        Args:
            base_size: 基础字体大小（非当前行）
            current_line_size: 当前行放大字体大小，0 表示自动比基础大4
            ruby_size: 注音字体大小
            cp_size: 节奏点标记字体大小
            line_height_factor: 行高系数（默认1.20）
            ruby_spacing: Ruby与主文字的垂直间距（默认4px）
            main_font: 主文字（当前行/上下文行）字体族，缺失时回退微软雅黑
            ruby_font: Ruby 注音字体族，缺失时回退微软雅黑。
                节奏点标记字体固定为微软雅黑，不随设置变化。
            cp_spacing: 节奏点标记顶端与主文字底部（descent）之间的垂直间距（默认4px）。
                Marker 顶端按此 gap 对齐，避免大 cp_size 时被主文字遮盖上半。
        """
        from strange_uta_game.frontend.font_utils import lyrics_font, phonetic_lyrics_font

        context_size = max(1, min(99, base_size))
        current_size = max(1, min(99, current_line_size if current_line_size > 0 else base_size + 4))
        ruby_size = max(1, min(99, ruby_size))
        cp_size = max(1, min(99, cp_size))
        line_height_factor = max(-1.0, min(5.0, line_height_factor))
        ruby_spacing = max(0, min(99, ruby_spacing))
        cp_spacing = max(0, min(99, cp_spacing))

        self._font_current = lyrics_font(main_font, current_size, QFont.Weight.Bold)
        self._font_context = lyrics_font(main_font, context_size)
        self._font_ruby = lyrics_font(ruby_font, ruby_size)
        # IPA 文本段字体：IPA 字体优先、原歌词字体链兜底（见 font_utils 模块
        # 注释——跨字体回退无法 GPOS 锚定，附加符/重音符会错位）
        self._font_current_ipa = phonetic_lyrics_font(self._font_current)
        self._font_context_ipa = phonetic_lyrics_font(self._font_context)
        self._font_ruby_ipa = phonetic_lyrics_font(self._font_ruby)
        self._font_checkpoint = self._build_checkpoint_font(cp_size)
        self._fm_current = QFontMetrics(self._font_current)
        self._fm_context = QFontMetrics(self._font_context)
        self._fm_ruby = QFontMetrics(self._font_ruby)
        self._fm_current_ipa = QFontMetrics(self._font_current_ipa)
        self._fm_context_ipa = QFontMetrics(self._font_context_ipa)
        self._fm_ruby_ipa = QFontMetrics(self._font_ruby_ipa)
        self._fm_checkpoint = QFontMetrics(self._font_checkpoint)
        self._ruby_spacing = ruby_spacing
        self._cp_spacing = cp_spacing
        self._line_height_factor = line_height_factor

        self._recompute_visible_lines()

        # 字体/marker 字符变化后刷新 marker 字形顶端缓存
        self._recompute_marker_glyph_top()

        # 清除缓存并重绘
        self._sentence_cache.clear()
        self._line_versions.clear()
        self._global_version += 1
        self._update_scrollbar_range()
        self._mark_horizontal_layout_dirty()
        self._update_horizontal_scrollbar_range()
        self.update()

    def _line_height_px(self) -> float:
        """单行像素高度：当前行字体 + ruby + 节奏点标记 + 间距，乘行高系数。"""
        total_height = (
            self._fm_current.height()
            + self._fm_ruby.height()
            + self._ruby_spacing
            + self._cp_spacing
            + self._fm_checkpoint.height()
        )
        # factor<=0 时视为极紧凑（显示最多行），避免除以零
        safe_factor = max(0.05, getattr(self, "_line_height_factor", 1.20))
        return total_height * safe_factor

    def _recompute_visible_lines(self) -> None:
        """按当前高度重算可见行数（行高不变，行数跟随视口）。"""
        line_h = self._line_height_px()
        h = self.height() if self.height() > 0 else 600
        self._visible_lines = max(
            self._min_visible_lines, min(15, int(h / line_h))
        )
        self._update_scrollbar_range()
        self._mark_horizontal_layout_dirty()
        self._update_horizontal_scrollbar_range()
        self.update()

    def set_min_visible_lines(self, minimum: int) -> None:
        """可见行数下限（1~3）：双谱模式放宽到 1 行，其余模式保持 3 行。

        下限变化立即重算行数并刷新滚动条——高度协商（EditorInterface 的
        预览让位）与行数计算共用这一入口。
        """
        minimum = max(1, min(3, int(minimum)))
        if minimum == self._min_visible_lines:
            return
        self._min_visible_lines = minimum
        self._recompute_visible_lines()

    def single_line_height(self) -> int:
        """单行显示所需的像素高度（双谱模式预览压缩到 1 句时的让位下限）。"""
        return max(1, int(math.ceil(self._line_height_px())))

    def set_checkpoint_markers(self, markers: dict[str, str]):
        """设置 checkpoint 标记字符并刷新缓存。"""
        self._checkpoint_markers.update(markers)
        self._recompute_marker_glyph_top()
        self._sentence_cache.clear()
        self._line_versions.clear()
        self._global_version += 1
        self._mark_horizontal_layout_dirty()
        self.update()

    def set_needs_guide_style(self, symbol: str, size: int) -> None:
        """设置导唱待办标记符号与字号。"""
        symbol = (symbol or "✚").strip() or "✚"
        size = max(4, min(64, int(size)))
        if symbol == self._needs_guide_symbol and size == self._needs_guide_size:
            return
        self._needs_guide_symbol = symbol
        self._needs_guide_size = size
        self._font_needs_guide = ui_font(size, QFont.Weight.Bold)
        self._fm_needs_guide = QFontMetrics(self._font_needs_guide)
        self.update()

    def request_repaint(self):
        """Request a repaint without invalidating sentence layout caches."""
        self.update()

    def _refresh_ruby_timing_state(self, line_idx: int) -> None:
        """只刷新 Ruby 静态打轴状态，保留既有走字时间轴缓存。"""
        if (
            not self._project
            or line_idx < 0
            or line_idx >= len(self._project.sentences)
        ):
            return

        characters = self._project.sentences[line_idx].characters
        fm_ruby = self._fm_ruby
        fm_ruby_ipa = getattr(self, "_fm_ruby_ipa", fm_ruby)
        from strange_uta_game.backend.infrastructure.parsers.ipa import has_ipa_text

        def _fm_resolver(ci: int, merged_text: str = "") -> QFontMetrics:
            # 连词组传合并后的整串（与绘制同字体）；单字传自身 ruby
            text = merged_text
            if not text:
                ruby = characters[ci].ruby if 0 <= ci < len(characters) else None
                text = ruby.text if ruby and ruby.text else ""
            if (
                fm_ruby_ipa is not fm_ruby
                and text
                and (has_ipa_text(text) or _has_multicodepoint_cluster(text))
            ):
                return fm_ruby_ipa
            return fm_ruby

        for (cached_line_idx, _font_key), entry in self._sentence_cache.items():
            if cached_line_idx != line_idx:
                continue
            char_spans, group_spans = _ruby_timed_span_maps(
                _fm_resolver,
                characters,
                entry["linked_leader_groups"],
                entry["linked_non_leader"],
            )
            entry["char_ruby_timed_spans"] = char_spans
            entry["group_ruby_timed_spans"] = group_spans

        self.update()

    def _update_display(self):
        self._global_version += 1
        # 内容（句子数）可能在结构性编辑后变化（如首次粘贴/导入歌词使项目从空
        # 变为有句子），需同步刷新滚动条的可见性与 range；否则滚动条会一直停留在
        # 初始的隐藏状态，直到某次 resize/主题切换才被动显示。
        self._update_scrollbar_range()
        self._mark_horizontal_layout_dirty()
        self.update()

    def _invalidate_line(self, line_idx: int):
        """使特定行的缓存失效（用于行内数据改变时）"""
        self._line_versions[line_idx] = self._line_versions.get(line_idx, 0) + 1
        self._mark_horizontal_layout_dirty()
        self.update()

    @staticmethod
    def _line_blocks_next_scan(line) -> bool:
        """行是否会终止 `_find_next_line_first_timestamp` 的向后扫描。

        返回 True 表示「屏障」或「能产出 first_ts」——无论哪种，扫描都会停在此行。
        False 表示「全 cc=0 且无任何时间戳」——扫描会跳过此行继续向后。
        """
        for ch in line.characters:
            if ch.is_sentence_end and ch.global_sentence_end_ts is None:
                return True
            if ch.check_count > 0 and not ch.global_timestamps:
                return True
            if ch.global_timestamps:
                return True
        return False

    @staticmethod
    def _line_blocks_prev_scan(line) -> bool:
        """行是否会终止 `_find_prev_line_last_timestamp` 的向前扫描。

        prev 扫描相比 next 多一个产出来源：`is_sentence_end` 且 `global_sentence_end_ts`
        非 None 也算 last_ts。其余规则同 next。
        """
        for ch in line.characters:
            if ch.is_sentence_end and ch.global_sentence_end_ts is None:
                return True
            if ch.check_count > 0 and not ch.global_timestamps:
                return True
            if ch.is_sentence_end and ch.global_sentence_end_ts is not None:
                return True
            if ch.global_timestamps:
                return True
        return False

    def _invalidate_line_and_dependents(self, changed_idx: int) -> None:
        """失效该行及其缓存依赖闭包内的所有行。

        per-line cache 不是纯行内的：每行的 wipe 计算会调
        `_find_next_line_first_timestamp` / `_find_prev_line_last_timestamp`，
        两者会跨过「全 cc=0 且无时间戳」的空行，直到找到能产出时间戳的行或屏障行。
        因此 line C 的改动可影响半径任意大的相邻行 —— 只要中间隔的全是可跳过的空行。
        这里按真实扫描语义双向扩散失效，确保 A·B(空)·C·D(空)·E 这种排布下 A/E 也被刷新。
        """
        if not self._project or not self._project.sentences:
            return
        sentences = self._project.sentences
        total = len(sentences)
        if not (0 <= changed_idx < total):
            return

        # 行内容变化可能改变行起始时间戳 → 换行快照置脏，
        # 待 _wipe_affected_lines / set_playing 下次使用前重建
        self._line_switch_points_dirty = True

        self._invalidate_line(changed_idx)

        # 向前：L < C 的行，其 forward-scan 能否到达 C，
        # 取决于 [L+1..C-1] 是否全为「next-scan 不阻断」。
        idx = changed_idx - 1
        while idx >= 0:
            self._invalidate_line(idx)
            if self._line_blocks_next_scan(sentences[idx]):
                break
            idx -= 1

        # 向后：L > C 的行，其 prev-scan 能否到达 C，
        # 取决于 [C+1..L-1] 是否全为「prev-scan 不阻断」。
        idx = changed_idx + 1
        while idx < total:
            self._invalidate_line(idx)
            if self._line_blocks_prev_scan(sentences[idx]):
                break
            idx += 1

    def _invalidate_all_lines(self):
        """使所有行的缓存失效（用于全局数据改变时）"""
        cached_line_indices = {line_idx for line_idx, _font_key in self._sentence_cache}
        line_indices = set(self._line_versions.keys()) | cached_line_indices
        for line_idx in line_indices:
            self._line_versions[line_idx] = self._line_versions.get(line_idx, 0) + 1
        self._mark_horizontal_layout_dirty()
        self.update()

    # ---- 窗口大小变化 ----

    def resizeEvent(self, event):
        """窗口大小变化时重新计算可见行数，保持行高不变。"""
        super().resizeEvent(event)
        if hasattr(self, '_fm_current') and hasattr(self, '_fm_ruby'):
            self._recompute_visible_lines()
        self._update_scrollbar_geometry()

    # ---- 滚动 ----

    def wheelEvent(self, a0):
        """普通滚轮纵向浏览歌词；Alt+滚轮横向浏览长行。"""
        if not a0 or not self._project or not self._project.sentences:
            return
        if a0.modifiers() & Qt.KeyboardModifier.AltModifier:
            if self._horizontal_scrollbar.isVisible():
                angle = a0.angleDelta()
                delta = angle.y() or angle.x()
                if delta:
                    steps = delta / 120.0
                    value = self._horizontal_scrollbar.value()
                    step = self._horizontal_scrollbar.singleStep()
                    self._horizontal_scrollbar.setValue(
                        int(round(value - steps * step))
                    )
                    a0.accept()
            return
        self._suspend_auto_scroll()
        delta = a0.angleDelta().y()
        # 每个滚轮 notch（120 单位）滚动 1 行
        self._scroll_center_line -= delta / 120.0
        total = len(self._project.sentences)
        self._scroll_center_line = max(
            0.0, min(float(total - 1), self._scroll_center_line)
        )
        self._sync_scrollbar_to_scroll_center()
        self.update()

    def _on_horizontal_scrollbar_changed(self, _value: int):
        self.update()

    def _mark_horizontal_layout_dirty(self) -> None:
        self._horizontal_layout_dirty = True

    def _effective_current_line(self) -> int:
        if (
            self._is_playing
            and self._auto_scroll_enabled
            and not self._auto_scroll_suspended
            and self._last_auto_scroll_line_idx >= 0
        ):
            return self._last_auto_scroll_line_idx
        return self._current_line_idx

    def _line_start_x(self, total_text_width: int, left: int, right: int) -> int:
        available_width = right - left
        if self._alignment == "left":
            return left + self._alignment_margin
        if self._alignment == "right":
            return right - total_text_width - self._alignment_margin
        return left + (available_width - total_text_width) // 2

    def _horizontal_shift(self) -> int:
        if not self._horizontal_scrollbar.isVisible():
            return 0
        return self._horizontal_max_shift - self._horizontal_scrollbar.value()

    def _update_scrollbar_geometry(self) -> None:
        scroll_w = getattr(self, "_SCROLLBAR_WIDTH", 14)
        horizontal_h = (
            self._HORIZONTAL_SCROLLBAR_HEIGHT
            if self._horizontal_scrollbar.isVisible()
            else 0
        )
        self._scrollbar.setGeometry(
            self.width() - scroll_w, 0, scroll_w, self.height() - horizontal_h
        )
        self._horizontal_scrollbar.setGeometry(
            0,
            self.height() - self._HORIZONTAL_SCROLLBAR_HEIGHT,
            self.width() - (scroll_w if self._scrollbar.isVisible() else 0),
            self._HORIZONTAL_SCROLLBAR_HEIGHT,
        )

    def _update_horizontal_scrollbar_range(self) -> None:
        """Show the bottom scrollbar iff any currently rendered line overflows.

        只测量可视窗口内的行（滚动到宽行时由 paint 的滚动中心变化检测补算），
        避免旧实现对全部行调用 _get_sentence_render_data 的全曲同步扫描。
        """
        self._horizontal_layout_dirty = False
        self._viewport_calc_center = self._scroll_center_line
        bar = self._horizontal_scrollbar
        old_shift = self._horizontal_shift()

        if not self._project or not self._project.sentences:
            bar.blockSignals(True)
            bar.setRange(0, 0)
            bar.setValue(0)
            bar.setVisible(False)
            bar.blockSignals(False)
            self._horizontal_max_shift = 0
            self._update_scrollbar_geometry()
            return

        left = self._line_number_margin + 5
        right = self.width() - (
            self._SCROLLBAR_WIDTH if self._scrollbar.isVisible() else 0
        )
        if right <= left:
            return

        min_x = left
        max_right = right
        effective_current = self._effective_current_line()
        n = len(self._project.sentences)
        half = self._visible_lines / 2 + 1
        first = max(0, int(self._scroll_center_line - half))
        last = min(n - 1, int(self._scroll_center_line + half) + 1)
        for idx in range(first, last + 1):
            sentence = self._project.sentences[idx]
            is_current = idx == effective_current
            fm = self._fm_current if is_current else self._fm_context
            render_data = self._get_sentence_render_data(
                idx, sentence, fm, "cur" if is_current else "ctx"
            )
            width = int(render_data["total_text_width"])
            start_x = self._line_start_x(width, left, right)
            min_x = min(min_x, start_x)
            max_right = max(max_right, start_x + width)

        min_shift = min(0, right - max_right)
        max_shift = max(0, left - min_x)
        span = max_shift - min_shift
        self._horizontal_max_shift = max_shift

        bar.blockSignals(True)
        if span <= 0:
            bar.setRange(0, 0)
            bar.setValue(0)
            bar.setVisible(False)
            self._horizontal_max_shift = 0
        else:
            bar.setRange(0, span)
            bar.setPageStep(max(1, right - left))
            bar.setValue(max(0, min(span, max_shift - old_shift)))
            bar.setVisible(True)
        bar.blockSignals(False)
        self._update_scrollbar_geometry()

    def _on_scrollbar_changed(self, value: int):
        """滚动条拖动 → 更新视口中央行索引"""
        if not self._project or not self._project.sentences:
            return
        self._suspend_auto_scroll()
        self._scroll_center_line = value / self._SCROLL_SCALE
        self.update()

    def _update_scrollbar_range(self):
        """根据当前句子总数刷新滚动条的 range / pageStep。

        滑块长度遵循"可见行数 / 总行数"的比例（与一般网页滚动条一致）：
        - 全部可见、不可滚动时 range 收为 0，滑块填满整条；
        - 可滚动时令 pageStep/(range跨度+pageStep)=visible/total，内容越长滑块越短，
          下限由全局 QSS 的 min-height 兜底。
        """
        self._scrollbar.blockSignals(True)
        if not self._project or not self._project.sentences:
            self._scrollbar.setVisible(False)
            self._scrollbar.blockSignals(False)
            return
        total = len(self._project.sentences)
        visible = max(1, self._visible_lines)
        self._scrollbar.setVisible(True)
        if total <= visible:
            # 全部可见、不可滚动：滑块填满整条
            self._scrollbar.setRange(0, 0)
            self._scrollbar.setPageStep(self._SCROLL_SCALE)
            self._scrollbar.setValue(0)
        else:
            # value 仍以"中心行"为单位映射到 [0, (total-1)*SCALE]，与
            # _sync_scrollbar_to_scroll_center / _on_scrollbar_changed 保持一致；
            # pageStep 取使滑块占比恰为 visible/total 的值。
            max_val = (total - 1) * self._SCROLL_SCALE
            page = int(round(visible * max_val / (total - visible)))
            self._scrollbar.setRange(0, max_val)
            self._scrollbar.setPageStep(page)
            self._scrollbar.setSingleStep(self._SCROLL_SCALE)  # 点击箭头 = 1 行
        self._scrollbar.blockSignals(False)

    def _sync_scrollbar_to_scroll_center(self):
        """将 _scroll_center_line 同步到滚动条 value（阻断信号防循环）"""
        if not self._project or not self._project.sentences:
            return
        val = int(round(self._scroll_center_line * self._SCROLL_SCALE))
        val = max(self._scrollbar.minimum(), min(self._scrollbar.maximum(), val))
        self._scrollbar.blockSignals(True)
        self._scrollbar.setValue(val)
        self._scrollbar.blockSignals(False)

    # ---- 点击 ----

    def focusOutEvent(self, a0=None):
        """失焦时取消未完成的 Ctrl 手势（弹出菜单/切换窗口会抢走鼠标释放）。"""
        if self._ctrl_sel_active:
            self._reset_ctrl_selection_gesture()
            self._drag_scroll_timer.stop()
            self._drag_scroll_direction = 0
            self.update()
        super().focusOutEvent(a0)

    def hideEvent(self, a0=None):
        """隐藏时取消未完成的 Ctrl 手势。"""
        if self._ctrl_sel_active:
            self._reset_ctrl_selection_gesture()
            self._drag_scroll_timer.stop()
            self._drag_scroll_direction = 0
        super().hideEvent(a0)

    def mousePressEvent(self, a0: Optional[QMouseEvent]):
        if not a0 or not self._project or not self._project.sentences:
            return
        self._suspend_auto_scroll()

        click_x = int(a0.position().x())
        click_y = int(a0.position().y())

        # 右键点击 → 打开上下文菜单
        if a0.button() == Qt.MouseButton.RightButton:
            # 右键会抢走左键释放：先取消未完成的 Ctrl 手势，避免下次左键被误提交
            self._suppress_left_release = False
            if self._ctrl_sel_active:
                self._reset_ctrl_selection_gesture()
                self._drag_scroll_timer.stop()
                self._drag_scroll_direction = 0
                self.update()
            self._show_context_menu(a0.globalPosition().toPoint(), click_x, click_y)
            return

        # Ctrl+左键：跨字符多选（点击切换单字 / 拖拽追加一段）。
        # 不移动打轴光标、不参与单击/双击链路；空白处按下不改变已有选区。
        if a0.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self._begin_ctrl_selection(click_x, click_y):
                self.update()
            else:
                # 空白处 Ctrl+左键：不改变已有选区，也不触发普通单击
                self._suppress_left_release = True
            return

        # 普通左键按下：离开跨字符多选，恢复原始单段划选行为。
        # 同时取消可能残留的 Ctrl 手势（例如上一下左键释放被弹出菜单抢走）。
        self._suppress_left_release = False
        if self._ctrl_sel_active:
            self._reset_ctrl_selection_gesture()
            self._drag_scroll_timer.stop()
            self._drag_scroll_direction = 0
        if self._ext_selection_chars is not None:
            self.clear_extended_selection()

        # 记录按下位置，用于像素级防抖判断
        self._press_pos = (click_x, click_y)

        # 检查是否点击在 checkpoint 标记上
        for marker_rect, line_idx, char_idx, cp_idx in self._checkpoint_hitboxes:
            if marker_rect.contains(click_x, click_y):
                self._pending_click = {
                    "type": "cp",
                    "line_idx": line_idx,
                    "char_idx": char_idx,
                    "cp_idx": cp_idx,
                }
                return

        # 检查字符文本点击 → 开始划词选择
        for char_rect, line_idx, char_idx in self._char_hitboxes:
            if char_rect.contains(click_x, click_y):
                self._focus_line_idx = line_idx
                self._focus_char_idx = char_idx
                self._focus_line_range_end = line_idx
                self._focus_char_range_end = char_idx
                self._focus_explicit = True
                self._focus_dragging = True
                self._pending_click = {
                    "type": "char",
                    "line_idx": line_idx,
                    "char_idx": char_idx,
                }
                self.update()
                return

        # 点击在行内空白区域：按水平距离找最近的 hitbox
        nearest = self._find_nearest_hitbox(click_x, click_y)
        if nearest:
            hit_type, line_idx, char_idx, cp_idx = nearest
            self._focus_line_idx = line_idx
            self._focus_char_idx = char_idx
            self._focus_line_range_end = line_idx
            self._focus_char_range_end = char_idx
            self._focus_explicit = True
            self._focus_dragging = True
            if hit_type == "cp":
                self._pending_click = {
                    "type": "cp",
                    "line_idx": line_idx,
                    "char_idx": char_idx,
                    "cp_idx": cp_idx,
                }
            else:
                self._pending_click = {
                    "type": "char",
                    "line_idx": line_idx,
                    "char_idx": char_idx,
                }
            self.update()
            return

        # 回退到行级别点击（纯空行或行间空隙）：根据 y 坐标反算行索引
        # 先清除旧快照（避免上次单击 300ms 计时器延迟触发 set_current_position）
        self._focus_line_idx = -1
        self._focus_char_idx = -1
        self._focus_line_range_end = -1
        self._focus_char_range_end = -1
        self._clear_click_snapshot()

        h = self.height()
        line_height = h / self._visible_lines
        center_y = h / 2.0
        clicked_line = self._scroll_center_line + (click_y - center_y) / line_height
        target_idx = int(round(clicked_line))
        total = len(self._project.sentences)
        if 0 <= target_idx < total:
            # 设置 focus 到目标行首位，走正常 pending_click 流程：
            # mouseReleaseEvent 会发出 char_selected(target_idx, 0)，
            # _on_char_selected 负责居中并通过 timing_service 向前找最近节奏点。
            self._focus_line_idx = target_idx
            self._focus_char_idx = 0
            self._focus_line_range_end = target_idx
            self._focus_char_range_end = 0
            self._focus_explicit = True
            self._focus_dragging = True
            self._pending_click = {
                "type": "char",
                "line_idx": target_idx,
                "char_idx": 0,
            }
        self.update()

    def mouseMoveEvent(self, a0: Optional[QMouseEvent]):
        """鼠标拖拽 → 扩展划词选择范围；边缘自动滚动。"""
        if not a0:
            return

        move_x = int(a0.position().x())
        move_y = int(a0.position().y())
        self._drag_scroll_mouse_y = move_y

        # Ctrl 跨字符多选：拖动只更新预览范围，松开时才提交。
        if self._ctrl_sel_active:
            press = self._ctrl_sel_press_pos
            if press is not None:
                dx = move_x - press[0]
                dy = move_y - press[1]
                if dx * dx + dy * dy > self._CLICK_MOVE_THRESHOLD * self._CLICK_MOVE_THRESHOLD:
                    self._ctrl_sel_moved = True
            hit = self._char_at_position(move_x, move_y)
            if hit is not None and hit != self._ctrl_sel_extent:
                self._ctrl_sel_extent = hit
                self.update()
            self._update_drag_autoscroll(move_y)
            return

        # 像素级防抖：移动超过阈值时清除待处理点击（说明用户是划词而非单击）
        if self._press_pos is not None and self._pending_click is not None:
            dx = move_x - self._press_pos[0]
            dy = move_y - self._press_pos[1]
            if dx * dx + dy * dy > self._CLICK_MOVE_THRESHOLD * self._CLICK_MOVE_THRESHOLD:
                self._pending_click = None

        if not self._focus_dragging:
            return

        hit = False
        for char_rect, line_idx, char_idx in self._char_hitboxes:
            if char_rect.contains(move_x, move_y):
                self._focus_line_range_end = line_idx
                self._focus_char_range_end = char_idx
                hit = True
                break
        if hit:
            self.update()

        self._update_drag_autoscroll(move_y)

    def _update_drag_autoscroll(self, move_y: int) -> None:
        """拖拽到预览上下边缘时启动/停止自动滚动（划词与 Ctrl 多选共用）。"""
        h = self.height()
        SCROLL_ZONE = 36
        new_dir = 0
        if self._project and self._project.sentences:
            total = len(self._project.sentences) - 1
            if move_y < SCROLL_ZONE and self._scroll_center_line > 0:
                new_dir = -1
            elif move_y > h - SCROLL_ZONE and self._scroll_center_line < total:
                new_dir = 1
        if new_dir != self._drag_scroll_direction:
            self._drag_scroll_direction = new_dir
            if new_dir != 0:
                self._drag_scroll_timer.start()
            else:
                self._drag_scroll_timer.stop()

    def _on_drag_scroll_tick(self):
        """拖拽边缘滚动：超出越多滚动越快。"""
        if (not self._focus_dragging and not self._ctrl_sel_active) or self._drag_scroll_direction == 0:
            self._drag_scroll_timer.stop()
            return
        h = self.height()
        SCROLL_ZONE = 36
        my = self._drag_scroll_mouse_y
        if self._drag_scroll_direction < 0:
            dist = SCROLL_ZONE - my
        else:
            dist = my - (h - SCROLL_ZONE)
        dist = max(0, min(dist, SCROLL_ZONE * 3))
        speed = 0.02 + (dist / SCROLL_ZONE) * 0.25
        delta = speed * self._drag_scroll_direction
        new_center = self._scroll_center_line + delta
        total = len(self._project.sentences) - 1 if self._project and self._project.sentences else 0
        new_center = max(0, min(float(total), new_center))
        if new_center != self._scroll_center_line:
            self._scroll_center_line = new_center
            self._sync_scrollbar_to_scroll_center()
            self.update()

    def mouseReleaseEvent(self, a0: Optional[QMouseEvent]):
        """鼠标释放 → 结束划词，或触发单击"""
        if not a0 or a0.button() != Qt.MouseButton.LeftButton:
            return

        # Ctrl+左键手势：松开才提交（单击切换 / 拖拽追加），不触发普通单击
        if self._ctrl_sel_active:
            self._finish_ctrl_selection(
                int(a0.position().x()), int(a0.position().y())
            )
            return
        if self._suppress_left_release:
            self._suppress_left_release = False
            self._pending_click = None
            self._press_pos = None
            return

        self._focus_dragging = False
        self._drag_scroll_timer.stop()
        self._drag_scroll_direction = 0

        # 双击的 Release #2：双击事件已处理，跳过一切单击逻辑
        if self._double_click_handled:
            self._double_click_handled = False
            self._pending_click = None
            self._press_pos = None
            return

        # 如果已有快照（双击的第二次松开），跳过单击
        if self._click_snapshot is not None:
            self._pending_click = None
            self._press_pos = None
            return

        # 如果有待处理点击 且 移动距离 < 阈值，立即执行单击
        if self._pending_click is not None and self._press_pos is not None:
            click = self._pending_click
            self._pending_click = None
            self._press_pos = None

            if not self._disable_click_jump:
                self._suppress_click_recenter = self._disable_click_recenter
                try:
                    if click["type"] == "cp":
                        self.checkpoint_clicked.emit(
                            click["line_idx"], click["char_idx"], click["cp_idx"]
                        )
                    elif click["type"] == "char":
                        self.char_selected.emit(click["line_idx"], click["char_idx"])
                        self.line_clicked.emit(click["line_idx"])
                finally:
                    self._suppress_click_recenter = False

                # 保存快照用于双击判断，300ms 后清除
                self._click_snapshot = click
                self._click_timer.start()
        else:
            # _pending_click 被拖拽检测清掉了，但 focus 已在 mousePressEvent
            # 里更新。用 focus 位置作为单击坐标，确保 current 域同步。
            if (
                not self._disable_click_jump
                and self._focus_line_idx >= 0
                and self._focus_char_idx >= 0
            ):
                self._suppress_click_recenter = self._disable_click_recenter
                try:
                    self.char_selected.emit(self._focus_line_idx, self._focus_char_idx)
                    self.line_clicked.emit(self._focus_line_idx)
                finally:
                    self._suppress_click_recenter = False
            self._pending_click = None
            self._press_pos = None

    def _clear_click_snapshot(self):
        """清除单击快照和相关定时器。

        300ms 超时 → 确认是单击而非双击，兜底同步 current 域到 focus 位置，
        防止期间有状态漂移。

        例外：当 focus 落在无 checkpoint 字符（cc=0 且非停顿点）时跳过同步，
        保持 _on_char_selected 中已正确设置的 current 域（指向最近有效 cp）。
        """
        self._click_snapshot = None
        self._click_timer.stop()
        if self._focus_line_idx >= 0 and self._focus_char_idx >= 0:
            if self._project and self._focus_line_idx < len(self._project.sentences):
                sentence = self._project.sentences[self._focus_line_idx]
                if self._focus_char_idx < len(sentence.characters):
                    ch = sentence.characters[self._focus_char_idx]
                    if ch.check_count == 0 and not ch.is_sentence_end:
                        return
            self.set_current_position(self._focus_line_idx, self._focus_char_idx)

    def _emit_multi_line_action(self, action_type: str, start_line: int, start_char: int, end_line: int, end_char: int, singer_id: str = ""):
        """跨行选择操作：发射对应的多行信号（由 timing_interface 批量处理）。"""
        if action_type == "delete":
            self.delete_chars_multi_requested.emit(start_line, start_char, end_line, end_char)
        elif action_type == "singer":
            self.singer_change_multi_requested.emit(start_line, start_char, end_line, end_char, singer_id)

    def _show_context_menu(self, global_pos, click_x: int, click_y: int):
        """显示字符上下文菜单。"""
        if not self._project or not self._project.sentences:
            return

        target_line_idx = self._current_line_idx
        target_char_idx = self._current_char_idx
        for char_rect, line_idx, char_idx in self._char_hitboxes:
            if char_rect.contains(click_x, click_y):
                target_line_idx = line_idx
                target_char_idx = char_idx
                # 上游评审：改走 char_selected 信号会触发完整选中副作用，
                # 保持既有直写行为（菜单目标行本就单独解析，不依赖 current）
                self._current_line_idx = line_idx
                self._current_char_idx = char_idx
                break
        else:
            # 未命中字符 hitbox：先试最近 hitbox（行内空白区域），
            # 再按 y 坐标反算行（纯空行 / 行间空隙）
            nearest = self._find_nearest_hitbox(click_x, click_y)
            if nearest:
                _, target_line_idx, target_char_idx, _ = nearest
            else:
                h = self.height()
                line_height = h / self._visible_lines
                center_y = h / 2.0
                clicked_line = self._scroll_center_line + (click_y - center_y) / line_height
                calc_idx = int(round(clicked_line))
                total = len(self._project.sentences)
                if 0 <= calc_idx < total:
                    target_line_idx = calc_idx
                    target_char_idx = 0
            # 右键非字符区域同步 focus 域（与左键回退路径一致，
            # 保证菜单关闭后 F4/F5 等操作指向正确位置）
            self._focus_line_idx = target_line_idx
            self._focus_char_idx = target_char_idx
            self._focus_line_range_end = target_line_idx
            self._focus_char_range_end = target_char_idx

        if target_line_idx < 0 or target_line_idx >= len(self._project.sentences):
            return

        sentence = self._project.sentences[target_line_idx]
        if target_char_idx < 0:
            target_char_idx = 0
        if sentence.characters and target_char_idx >= len(sentence.characters):
            target_char_idx = len(sentence.characters) - 1

        # 右键落在跨字符选区之外：收起旧选区，把焦点移到目标字符，
        # 菜单按常规单字/单段选区语义工作（与常见 UI 行为一致）。
        if self.is_extended_selection() and not self.selection_contains(
            target_line_idx, target_char_idx
        ):
            self.clear_extended_selection()
            self._focus_line_idx = target_line_idx
            self._focus_char_idx = target_char_idx
            self._focus_line_range_end = target_line_idx
            self._focus_char_range_end = target_char_idx
            self._focus_explicit = False

        extended = self.is_extended_selection()
        ext_ranges = self.get_selected_char_ranges() if extended else []
        norm_sel = None if extended else self.get_normalized_selection()
        multi_line = norm_sel is not None and norm_sel[0] != norm_sel[2]
        sel_start = target_char_idx
        sel_end = target_char_idx
        in_selection = False
        if extended:
            in_selection = self.selection_contains(target_line_idx, target_char_idx)
        elif norm_sel is not None:
            sel_start = norm_sel[1]
            sel_end = norm_sel[3]
            if multi_line:
                in_selection = norm_sel[0] <= target_line_idx <= norm_sel[2]
            else:
                in_selection = (
                    target_line_idx == norm_sel[0]
                    and sel_start <= target_char_idx <= sel_end
                )

        delete_start = sel_start if in_selection else target_char_idx
        delete_end = sel_end + 1 if in_selection else target_char_idx + 1
        ext_scope = extended and in_selection

        menu = RoundMenu(parent=self)

        if ext_scope:
            delete_action = Action(self.tr("删除字符"), menu)
            delete_action.triggered.connect(
                lambda checked=False: self.delete_selection_ranges_requested.emit(
                    list(ext_ranges)
                )
            )
            menu.addAction(delete_action)
        elif multi_line and in_selection:
            _dl_sl, _dl_sc, _dl_el, _dl_ec = norm_sel
            delete_action = Action(self.tr("删除字符"), menu)
            delete_action.triggered.connect(
                lambda checked=False: self._emit_multi_line_action(
                    "delete", _dl_sl, _dl_sc, _dl_el, _dl_ec
                )
            )
            menu.addAction(delete_action)
        else:
            delete_action = Action(self.tr("删除字符"), menu)
            delete_action.triggered.connect(
                lambda checked=False: self.delete_chars_requested.emit(
                    target_line_idx, delete_start, delete_end
                )
            )
            menu.addAction(delete_action)

        if ext_scope:
            delete_timestamp = Action(self.tr("删除所选范围时间戳"), menu)
            delete_timestamp.triggered.connect(
                lambda checked=False: self.selection_char_ranges_action.emit(
                    "clear_timestamps", list(ext_ranges)
                )
            )
        else:
            delete_timestamp = Action(self.tr("删除当前时间戳并回滚"), menu)
            delete_timestamp.triggered.connect(
                lambda checked=False: self.delete_timestamp_requested.emit(
                    target_line_idx, target_char_idx
                )
            )
        menu.addAction(delete_timestamp)

        insert_space_before_action = Action(self.tr("在此前插入空格"), menu)
        insert_space_before_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.insert_space_before_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
                target_char_idx,
            )
        )
        menu.addAction(insert_space_before_action)
        insert_space_after_action = Action(self.tr("在此后插入空格"), menu)
        insert_space_after_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.insert_space_after_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
                target_char_idx,
            )
        )
        menu.addAction(insert_space_after_action)
        menu.addSeparator()

        merge_line_idx = target_line_idx
        if extended and self._last_selected_char is not None:
            merge_line_idx = self._last_selected_char[0]
        merge_up_action = Action(self.tr("合并上一行"), menu)
        merge_up_action.setEnabled(merge_line_idx > 0)
        merge_up_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.merge_line_up_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
            )
        )
        menu.addAction(merge_up_action)

        delete_line_action = Action(self.tr("删除本行"), menu)
        delete_line_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.delete_line_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
            )
        )
        menu.addAction(delete_line_action)

        insert_blank_line_before_action = Action(self.tr("在此前插入空行"), menu)
        insert_blank_line_before_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.insert_blank_line_before_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
            )
        )
        menu.addAction(insert_blank_line_before_action)

        insert_blank_line_after_action = Action(self.tr("在此后插入空行"), menu)
        insert_blank_line_after_action.triggered.connect(
            lambda checked=False: self._collapse_extended_then_emit(
                self.insert_blank_line_requested,
                target_line_idx,
                target_char_idx,
                target_line_idx,
            )
        )
        menu.addAction(insert_blank_line_after_action)
        menu.addSeparator()

        add_checkpoint_action = Action(self.tr("增加节奏点"), menu)
        remove_checkpoint_action = Action(self.tr("减少节奏点"), menu)
        toggle_sentence_end_action = Action(self.tr("设置/取消停顿点"), menu)
        toggle_needs_guide_action = Action(self.tr("标记/取消导唱待办"), menu)
        if ext_scope:
            add_checkpoint_action.triggered.connect(
                lambda checked=False: self.selection_char_ranges_action.emit(
                    "add_checkpoint", list(ext_ranges)
                )
            )
            remove_checkpoint_action.triggered.connect(
                lambda checked=False: self.selection_char_ranges_action.emit(
                    "remove_checkpoint", list(ext_ranges)
                )
            )
            toggle_sentence_end_action.triggered.connect(
                lambda checked=False: self.selection_char_ranges_action.emit(
                    "toggle_sentence_end", list(ext_ranges)
                )
            )
            toggle_needs_guide_action.triggered.connect(
                lambda checked=False: self.selection_char_ranges_action.emit(
                    "toggle_needs_guide", list(ext_ranges)
                )
            )
        else:
            add_checkpoint_action.triggered.connect(
                lambda checked=False: self.add_checkpoint_requested.emit(
                    target_line_idx, target_char_idx
                )
            )
            remove_checkpoint_action.triggered.connect(
                lambda checked=False: self.remove_checkpoint_requested.emit(
                    target_line_idx, target_char_idx
                )
            )
            toggle_sentence_end_action.triggered.connect(
                lambda checked=False: self.toggle_sentence_end_requested.emit(
                    target_line_idx, target_char_idx
                )
            )
            toggle_needs_guide_action.triggered.connect(
                lambda checked=False: self.toggle_needs_guide_requested.emit(
                    target_line_idx, target_char_idx
                )
            )
        menu.addAction(add_checkpoint_action)
        menu.addAction(remove_checkpoint_action)
        menu.addAction(toggle_sentence_end_action)
        menu.addAction(toggle_needs_guide_action)
        menu.addSeparator()

        if ext_scope:
            singer_menu = RoundMenu(self.tr("设置演唱者"), self)
            default_singer = self._project.get_default_singer()
            default_action = Action(self.tr("默认演唱者"), singer_menu)
            default_action.triggered.connect(
                lambda checked=False, sid=default_singer.id: self.singer_change_ranges_requested.emit(
                    list(ext_ranges), sid
                )
            )
            singer_menu.addAction(default_action)
            singer_menu.addSeparator()
            for singer in self._project.singers:
                action = Action(singer.name, singer_menu)
                action.triggered.connect(
                    lambda checked=False, sid=singer.id: self.singer_change_ranges_requested.emit(
                        list(ext_ranges), sid
                    )
                )
                singer_menu.addAction(action)
            menu.addMenu(singer_menu)
        elif multi_line and in_selection:
            _dl_sl, _dl_sc, _dl_el, _dl_ec = norm_sel
            singer_menu = RoundMenu(self.tr("设置演唱者"), self)
            default_singer = self._project.get_default_singer()
            default_action = Action(self.tr("默认演唱者"), singer_menu)
            default_action.triggered.connect(
                lambda checked=False: self._emit_multi_line_action(
                    "singer", _dl_sl, _dl_sc, _dl_el, _dl_ec, singer_id=default_singer.id
                )
            )
            singer_menu.addAction(default_action)
            singer_menu.addSeparator()
            for singer in self._project.singers:
                action = Action(singer.name, singer_menu)
                action.triggered.connect(
                    lambda checked=False, sid=singer.id: self._emit_multi_line_action(
                        "singer", _dl_sl, _dl_sc, _dl_el, _dl_ec, singer_id=sid
                    )
                )
                singer_menu.addAction(action)
            menu.addMenu(singer_menu)
        else:
            singer_start = delete_start if in_selection else target_char_idx
            singer_end = delete_end - 1 if in_selection else target_char_idx
            singer_menu = RoundMenu(self.tr("设置演唱者"), self)
            default_singer = self._project.get_default_singer()
            default_action = Action(self.tr("默认演唱者"), singer_menu)
            default_action.triggered.connect(
                lambda checked=False: self.singer_change_requested.emit(
                    target_line_idx, singer_start, singer_end, default_singer.id
                )
            )
            singer_menu.addAction(default_action)
            singer_menu.addSeparator()
            for singer in self._project.singers:
                action = Action(singer.name, singer_menu)
                action.triggered.connect(
                    lambda checked=False, sid=singer.id: self.singer_change_requested.emit(
                        target_line_idx, singer_start, singer_end, sid
                    )
                )
                singer_menu.addAction(action)
            menu.addMenu(singer_menu)
        menu.exec(global_pos)

    def _find_next_line_first_timestamp(self, current_line_idx: int) -> Optional[int]:
        """向后逐行查找最近一个有时间戳行的第一个时间戳，用于行尾非停顿点时的 wipe 右边界。

        对每一行执行早停检测：若该行存在 is_sentence_end=True 但无 global_sentence_end_ts，
        或存在 check_count>0 但无 global_timestamps，视为该行未完整打轴，则结束（返回 None）。
        若该行全为 cc=0 且无时间戳，则跳过继续向后找。

        Returns:
            最近一个有时间戳行的第一个时间戳，或 None（遇到未完整打轴行或找到尾）。
        """
        if not self._project or not self._project.sentences:
            return None
        total = len(self._project.sentences)
        for line_idx in range(current_line_idx + 1, total):
            line = self._project.sentences[line_idx]
            first_ts: Optional[int] = None
            for ch in line.characters:
                if ch.is_sentence_end and ch.global_sentence_end_ts is None:
                    return None
                if ch.check_count > 0 and not ch.global_timestamps:
                    return None
                if ch.global_timestamps and first_ts is None:
                    first_ts = int(ch.global_timestamps[0])
            if first_ts is not None:
                return first_ts
        return None

    def _find_prev_line_last_timestamp(self, current_line_idx: int) -> Optional[int]:
        """向前逐行查找最近一个有时间戳行的最后时间戳，用于行首 cc=0 字符的 wipe 起始锚点。

        对每一行执行早停检测：若该行存在 is_sentence_end=True 但无 global_sentence_end_ts，
        或存在 check_count>0 但无 global_timestamps，视为该行未完整打轴，则结束（返回 None）。
        若该行全为 cc=0 且无时间戳，则跳过继续向前找。

        Returns:
            最近一个有时间戳行的最后时间戳，或 None（遇到未完整打轴行或找到头）。
        """
        if not self._project or not self._project.sentences or current_line_idx <= 0:
            return None
        for line_idx in range(current_line_idx - 1, -1, -1):
            line = self._project.sentences[line_idx]
            last_ts: Optional[int] = None
            for ch in line.characters:
                if ch.is_sentence_end and ch.global_sentence_end_ts is None:
                    return None  # 未完整打轴，作为屏障终止查找
                if ch.check_count > 0 and not ch.global_timestamps:
                    return None  # 未完整打轴，作为屏障终止查找
                if ch.is_sentence_end and ch.global_sentence_end_ts is not None:
                    t = int(ch.global_sentence_end_ts)
                    if last_ts is None or t > last_ts:
                        last_ts = t
                if ch.global_timestamps:
                    t = int(ch.global_timestamps[-1])
                    if last_ts is None or t > last_ts:
                        last_ts = t
            if last_ts is not None:
                return last_ts
            # 该行全为 cc=0 且无时间戳，继续向前找
        return None

    def _get_sentence_render_data(
        self, idx: int, sentence, main_fm, font_key: str
    ) -> dict:
        """返回逐句渲染数据。

        渲染模型：
          - wipe 时间线的单位 = 一整行（sentence），与打轴的连词组解耦
          - 行内所有带 global_timestamps 的字符 + 行尾（is_sentence_end +
            global_sentence_end_ts）构成"锚点"
          - 相邻锚点之间的所有字符（含无时间戳的）按字符像素宽度加权线性插值
            分配 wipe 区间——解决等字符数分配导致的宽度/节奏不匹配跳变；
            空格等零墨水的无渲染字符权重为 0（零时长窗口），不占用走字时长
          - 首锚之前 / 末锚之后的字符贴首/末锚（wipe 恒 0 或 1）
          - linked_to_next 只影响视觉渲染层（连词不拆字画），不参与 wipe 计算

        缓存策略：
          - 每行有自己的版本号，只有数据改变的行才重新计算
          - 全局版本号用于字体变化等全局刷新
        """
        line_version = self._line_versions.get(idx, 0)
        cache_key = (idx, font_key)
        entry = self._sentence_cache.get(cache_key)
        if entry and entry["v"] == line_version and entry["gv"] == self._global_version:
            return entry

        _perf_start = time.perf_counter() if perf_enabled() else None
        chars = sentence.chars
        characters = sentence.characters
        n_chars = len(chars)

        # ── IPA 段判定与字体选择 ──
        # IPA 词内的字符、以及本身是多码点簇（带组合附加符）的字符，用
        # IPA 字体排版：CJK 主字体缺 IPA 字形时跨字体回退无法 GPOS 锚定
        # （附加符漂移、ˈˌ 零宽与后字重叠）。
        ipa_font = getattr(self, "_font_current_ipa", None)
        if font_key == "ctx":
            ipa_font = getattr(self, "_font_context_ipa", None)
        fm_main_ipa = QFontMetrics(ipa_font) if ipa_font is not None else main_fm
        base_font = self._font_context if font_key == "ctx" else self._font_current
        has_ipa_styling = ipa_font is not None and ipa_font.families() != base_font.families()

        char_uses_ipa: list[bool] = [False] * n_chars
        if n_chars:
            from strange_uta_game.backend.infrastructure.parsers.ipa import (
                find_ipa_words,
            )

            offsets = chars_to_offsets(chars)
            for a, b, _w in find_ipa_words(sentence.text):
                i0 = offset_to_index(offsets, a)
                i1 = offset_to_index(offsets, b - 1)
                for ci in range(i0, min(i1 + 1, n_chars)):
                    char_uses_ipa[ci] = True
            for ci, ch in enumerate(chars):
                if len(ch) > 1:
                    char_uses_ipa[ci] = True

        # IPA/多码点簇的墨水判定与「是否真的换字体」解耦：即使主字体恰好
        # 就是 IPA 字体（或无 IPA 字体可用、has_ipa_styling=False），附加符
        # 仍必须用矢量墨水，否则 GPOS 标记会落在字体盒之外被裁掉。
        def _fm_for_char(ci: int) -> QFontMetrics:
            return fm_main_ipa if (has_ipa_styling and char_uses_ipa[ci]) else main_fm

        def _font_for_char(ci: int):
            return ipa_font if (has_ipa_styling and char_uses_ipa[ci]) else base_font

        # 字符像素宽度（初始为字符本身的 advance width，含侧 bearings/字间距）
        # 同时计算字符的"墨水"边界—— wipe 严格按墨水边界裁剪，不再扫过
        # 字形周围的透明像素；连词框取真实墨水并集（含上下附加符）。
        fm_ruby = self._fm_ruby
        fm_ruby_ipa = getattr(self, "_fm_ruby_ipa", fm_ruby)
        avg_char_w = main_fm.averageCharWidth()
        char_widths = []
        # char_ink_offsets[ci] = ink_left（相对 drawText 的 x）
        # char_ink_widths[ci]  = ink 实际像素宽度
        # 空白字符 ink_width = 0，wipe 时跳过裁剪绘制（视觉上保留空白等待）。
        char_ink_offsets: list[float] = []
        char_ink_widths: list[float] = []
        char_advances: list[float] = []
        # 行墨水上下界（相对 y_center；在下方首遍循环内累计，缓存于 entry，
        # paintEvent 不再每帧重算）。初值必须取「无墨水」哨兵，最后才回退
        # 字体盒——否则普通假名行的墨水会被放大到 ascent/descent，节奏点、
        # 打轴指示线、选区/hitbox 和分色色带随之整体偏移（非 IPA 回归）。
        line_ink_top = float("inf")
        line_ink_bottom = float("-inf")
        has_line_ink = False
        # IPA/多码点簇用矢量轮廓（tightBoundingRect 在 Windows 上不计
        # GPOS 附加符）；其余普通歌词沿用 tightBoundingRect 的像素口径，
        # 保证非 IPA 行的 wipe/注音几何与历史逐像素一致。
        for ci, ch in enumerate(chars):
            fm_ci = _fm_for_char(ci)
            font_ci = _font_for_char(ci)
            char_w = fm_ci.horizontalAdvance(ch)
            adv = char_w
            if ch == ' ':
                char_w = avg_char_w if fm_ci is main_fm else fm_ci.averageCharWidth()
            use_vector = bool(char_uses_ipa[ci])
            ink = (
                _vector_ink_bounds(font_ci, ch, self._vector_ink_cache)
                if use_vector
                else None
            )
            # 非 IPA 的孤立零 advance 附加符（U+0303 等）没有可排版的
            # tight 墨水，但矢量轮廓有；沿用 IPA 支持的兜底，避免变 0 宽
            # 格子后与后字重叠。普通正 advance 字符不会走到这里。
            if ink is None and char_w <= 0 and ch and not ch.isspace():
                ink = _vector_ink_bounds(font_ci, ch, self._vector_ink_cache)
            ink_top: Optional[float] = None
            ink_bottom: Optional[float] = None
            if ink is not None:
                ink_off, _ink_top, ink_w, _ink_h = ink
                # 零 advance 但有墨水（Tahoma 式零宽重音符等）：以墨水宽度
                # 作为格子宽，避免与后字重叠
                if char_w <= 0 and ink_w > 0:
                    char_w = ink_w + 2
                elif use_vector and ink_w > 0:
                    # 多码点簇（keycap/emoji/组合附加符）的轮廓可能比 advance
                    # 宽；格子至少要覆盖墨水右缘，否则后字会压上来。
                    _ink_cell = ink_w if ink_off < 0 else ink_off + ink_w
                    if _ink_cell > char_w:
                        char_w = _ink_cell
                ink_top = _ink_top
                ink_bottom = _ink_top + _ink_h  # QRectF 底边（不含）
            else:
                ink_off, ink_w = _wipe_ink_bounds(fm_ci, ch)
                if ch and not ch.isspace():
                    _br = fm_ci.tightBoundingRect(ch)
                    # 空矩形哨兵（无字形码点）与无高度退化盒不参与行墨水
                    if (
                        _br.height() > 0
                        and _br.x() < 100000
                        and _br.y() < 100000
                    ):
                        ink_top = _br.top()
                        # QRect.bottom() 是含端像素，paintEvent 统一 +1，
                        # 与历史 tightBoundingRect 行墨水口径一致
                        ink_bottom = _br.bottom()
            char_widths.append(char_w)
            char_advances.append(adv)
            char_ink_offsets.append(ink_off)
            char_ink_widths.append(ink_w)
            if ink_top is not None and ink_bottom is not None:
                has_line_ink = True
                if line_ink_top > ink_top:
                    line_ink_top = ink_top
                if line_ink_bottom < ink_bottom:
                    line_ink_bottom = ink_bottom
        # 整行无可绘制墨水（空行/纯空白/孤立附加符）：回退字体盒
        if not has_line_ink:
            line_ink_top = -main_fm.ascent()
            line_ink_bottom = main_fm.descent()

        # ---------- 连词组（仅用于视觉层，与 wipe 计算无关） ----------
        char_groups: list = []
        cur_grp: Optional[list] = None
        for ci in range(n_chars):
            if cur_grp is None:
                cur_grp = [ci]
                char_groups.append(cur_grp)
            elif ci > 0 and characters[ci - 1].linked_to_next:
                cur_grp.append(ci)
            else:
                cur_grp = [ci]
                char_groups.append(cur_grp)

        linked_leader_groups: dict = {}
        linked_non_leader: set = set()
        non_leader_to_leader: dict = {}
        for group in char_groups:
            if len(group) > 1:
                linked_leader_groups[group[0]] = group
                for _ci in group[1:]:
                    linked_non_leader.add(_ci)
                    non_leader_to_leader[_ci] = group[0]

        def _fm_for_ruby_text(text: str) -> QFontMetrics:
            """ruby 文本含 IPA 特有字符或多码点簇时用 IPA 注音字体度量。"""
            from strange_uta_game.backend.infrastructure.parsers.ipa import (
                has_ipa_text,
            )

            if fm_ruby_ipa is not fm_ruby and (has_ipa_text(text) or _has_multicodepoint_cluster(text)):
                return fm_ruby_ipa
            return fm_ruby

        # 连词组：将合并后的 ruby 宽度平均分配到组内每个字符
        for leader_ci, group in linked_leader_groups.items():
            merged_ruby_text = ""
            for _gci in group:
                _r = characters[_gci].ruby
                if _r:
                    merged_ruby_text += _r.text
            if merged_ruby_text:
                merged_ruby_w = _fm_for_ruby_text(merged_ruby_text).horizontalAdvance(
                    merged_ruby_text
                )
                group_total_char_w = sum(char_widths[g] for g in group)
                # 确保组的总宽度 >= 合并后ruby的宽度
                target_total_w = max(group_total_char_w, merged_ruby_w)
                # 将宽度平均分配到组内每个字符
                per_char_w = target_total_w / len(group)
                for g in group:
                    char_widths[g] = max(char_widths[g], per_char_w)

        # 单字符：确保宽度 >= ruby宽度（跳过连词组成员，已由连词组逻辑处理）
        for ci, ch in enumerate(chars):
            if ci in linked_leader_groups or ci in linked_non_leader:
                continue  # 连词组成员已处理
            ruby = characters[ci].ruby
            if ruby:
                ruby_w = _fm_for_ruby_text(ruby.text).horizontalAdvance(ruby.text)
                char_widths[ci] = max(char_widths[ci], ruby_w)

        # 确保字符宽度 >= CP标记总宽度（避免CP标记重叠）
        # 停顿点marker独立于普通marker，在字符右侧扩展半字符宽度
        fm_checkpoint = self._fm_checkpoint
        end_sentence_w: dict[int, int] = {}
        for ci, ch_obj in enumerate(characters):
            if ch_obj.total_timing_points > 0:
                total_cp_w = 0
                for cp_idx in range(ch_obj.total_timing_points):
                    is_sentence_end_marker = (
                        ch_obj.is_sentence_end and cp_idx == ch_obj.check_count
                    )
                    if is_sentence_end_marker:
                        continue  # 停顿点marker不计入普通CP宽度
                    elif cp_idx == 0:
                        marker_char = self._checkpoint_markers["cp_first_timed"] if cp_idx < len(ch_obj.global_timestamps) else self._checkpoint_markers["cp_first_empty"]
                    else:
                        marker_char = self._checkpoint_markers["cp_multi_timed"] if cp_idx < len(ch_obj.global_timestamps) else self._checkpoint_markers["cp_multi_empty"]
                    total_cp_w += fm_checkpoint.horizontalAdvance(marker_char)
                char_widths[ci] = max(char_widths[ci], total_cp_w)
            # 停顿点字符扩展一个全角汉字宽度的一半用于放置停顿点marker（单独追踪，不混入 char_widths）
            if ch_obj.is_sentence_end:
                end_sentence_w[ci] = main_fm.horizontalAdvance("一") // 2

        # ---------- wipe 时间线（离散字符开始时间模型） ----------
        # 每个字符的 wipe 开始时间 = 该字符第一个 cp 的时间戳（global_timestamps[0]）。
        # 字符 wipe 结束时间 = 同句子内下一个有 start_ts 的字符的开始时间；
        # 若后面无 start_ts，则使用停顿点时间戳（global_sentence_end_ts）。
        # 中间无 timestamp 的字符与上一个有 timestamp 的字符连读，共享同一段 wipe。
        # 一行可能含多个句子（多个 is_sentence_end），各句子独立计算段。
        start_times: dict[int, int] = {}
        for ci, ch in enumerate(characters):
            if ch.global_timestamps:
                start_times[ci] = int(ch.global_timestamps[0])

        # 按 is_sentence_end 拆分为句子范围 [(sent_start, sent_end)]，含边界
        sent_ranges: list[tuple[int, int]] = []
        s_start = 0
        for ci, ch in enumerate(characters):
            if ch.is_sentence_end:
                sent_ranges.append((s_start, ci))
                s_start = ci + 1
        if s_start < n_chars:
            sent_ranges.append((s_start, n_chars - 1))

        # 预处理：为行尾非停顿点的情况准备右边界
        # 如果最后一个 sent_range 的最后一个字符不是停顿点，去下一行借时间戳
        fallback_sentence_end_ts: Optional[int] = None
        if sent_ranges:
            last_sent_start, last_sent_end = sent_ranges[-1]
            last_char = characters[last_sent_end]
            if not last_char.is_sentence_end:
                next_ts = self._find_next_line_first_timestamp(idx)
                if next_ts is not None:
                    fallback_sentence_end_ts = next_ts
                elif self._duration_ms > 0:
                    fallback_sentence_end_ts = self._duration_ms

        char_wipe_times: dict = {}
        # ---------- 合并段锚点组（多cp leader + 后随 cc=0 字符） ----------
        # 当 leader 有 >=2 个时间戳且其段内后随字符全为 cc=0 时，把整段
        # （leader+followers）的 ink 合并成一条轴，用 [gts..., end_ts] 锚点走完；
        # 各锚点段的推进量按 leader 的 ruby 部件宽度加权（数量匹配时）。
        # seg_anchor_groups[leader_ci] = {anchors, ink_offsets, total_ink, part_ws}
        seg_anchor_groups: dict[int, dict] = {}
        seg_member_to_leader: dict[int, int] = {}
        _prev_line_last_ts: Optional[int] = None  # lazy-loaded，仅行首无 leader 时使用
        for sent_start, sent_end in sent_ranges:
            # 句子内有 start_ts 的 leader 字符索引
            leaders = [ci for ci in range(sent_start, sent_end + 1) if ci in start_times]
            if not leaders:
                # 整句无时间戳，仅发生在行首。
                # 只收集行首连续的 cc=0 字符；遇到第一个 cc>0 的字符立即停止，
                # 剩余字符回到老算法（无 char_wipe_times，显示 base color）。
                no_cc_end = sent_start
                for ci in range(sent_start, sent_end + 1):
                    if characters[ci].check_count == 0:
                        no_cc_end = ci + 1
                    else:
                        break
                if no_cc_end <= sent_start:
                    continue  # 行首第一个字符就有 cc，无需处理
                # 终点：优先取本句停顿点字符自身的 sentence_end_ts（cc=0 + is_sentence_end 场景），
                # 其次找本行后续第一个有时间戳字符的 ts，最后回落到 fallback
                end_ts_nl: Optional[int] = None
                sent_end_char = characters[sent_end]
                if sent_end_char.is_sentence_end and sent_end_char.global_sentence_end_ts is not None:
                    end_ts_nl = int(sent_end_char.global_sentence_end_ts)
                if end_ts_nl is None:
                    for ci in range(sent_end + 1, n_chars):
                        if ci in start_times:
                            end_ts_nl = start_times[ci]
                            break
                if end_ts_nl is None:
                    end_ts_nl = fallback_sentence_end_ts
                if end_ts_nl is None:
                    continue
                if _prev_line_last_ts is None:
                    _prev_line_last_ts = self._find_prev_line_last_timestamp(idx)
                if _prev_line_last_ts is None:
                    continue  # 上一行未完整打轴或不存在，无有效起点，不 wipe
                start_ts_nl = _prev_line_last_ts
                if end_ts_nl <= start_ts_nl:
                    continue
                for ci, win in zip(
                    range(sent_start, no_cc_end),
                    _weighted_wipe_windows(
                        char_widths,
                        char_ink_widths,
                        range(sent_start, no_cc_end),
                        start_ts_nl,
                        end_ts_nl,
                    ),
                ):
                    char_wipe_times[ci] = win
                continue

            for i, leader in enumerate(leaders):
                next_leader = leaders[i + 1] if i + 1 < len(leaders) else None
                seg_end = (next_leader - 1) if next_leader is not None else sent_end

                if next_leader is not None:
                    end_ts = start_times[next_leader]
                else:
                    # 句子最后一段：找 sentence_end_ts
                    end_ts = None
                    for ci in range(leader, sent_end + 1):
                        if characters[ci].is_sentence_end and characters[ci].global_sentence_end_ts is not None:
                            end_ts = int(characters[ci].global_sentence_end_ts)
                            break
                if end_ts is None:
                    end_ts = fallback_sentence_end_ts
                if end_ts is None or end_ts <= start_times[leader]:
                    continue

                # 整体：leader + 它后面的无 ts 字符，按像素宽度加权从左到右分配时间；
                # 空格等零墨水字符不占走字时长（零时长窗口），时长由可见字符瓜分。
                for ci, win in zip(
                    range(leader, seg_end + 1),
                    _weighted_wipe_windows(
                        char_widths,
                        char_ink_widths,
                        range(leader, seg_end + 1),
                        start_times[leader],
                        end_ts,
                    ),
                ):
                    char_wipe_times[ci] = win

                # 合并段锚点组：leader 有 >=2 个时间戳、后随组员全为 cc=0 时，
                # 把该段 ink 合并成一条轴，用 [gts..., end_ts] 走完（替代上面按
                # 像素切窗口的模型；char_wipe_times 保留作兜底）。
                # 典型场景：{今||[ts1]い|[ts2]ま}、 ——今按い/ま checkpoint 走字，
                # 顿号填补今与下一个 leader 之间的间隙。
                _gts = [int(t) for t in characters[leader].global_timestamps]
                if (
                    len(_gts) >= 2
                    and seg_end > leader
                    and all(
                        characters[ci].check_count == 0
                        for ci in range(leader + 1, seg_end + 1)
                    )
                    and all(_gts[k] < _gts[k + 1] for k in range(len(_gts) - 1))
                    and end_ts > _gts[-1]
                ):
                    _anchors = _gts + [int(end_ts)]
                    _ink_offsets: dict[int, int] = {}
                    _cum_ink = 0
                    for ci in range(leader, seg_end + 1):
                        _ink_offsets[ci] = _cum_ink
                        _cum_ink += char_ink_widths[ci]
                    if _cum_ink > 0:
                        # ruby 部件宽度（数量与锚点段数匹配时按部件宽度推进）
                        _part_ws: Optional[list[int]] = None
                        _l_ruby = characters[leader].ruby
                        if _l_ruby and _l_ruby.parts and len(_l_ruby.parts) == len(_anchors) - 1:
                            _fm_l = _fm_for_ruby_text(_l_ruby.text)
                            _part_ws = [
                                _fm_l.horizontalAdvance(p.text) for p in _l_ruby.parts
                            ]
                        seg_anchor_groups[leader] = {
                            "anchors": _anchors,
                            "ink_offsets": _ink_offsets,
                            "total_ink": _cum_ink,
                            "part_ws": _part_ws,
                        }
                        for ci in range(leader, seg_end + 1):
                            seg_member_to_leader[ci] = leader

            # 句子内第一个 leader 之前的无 ts 字符
            first_leader = leaders[0]
            if first_leader > sent_start:
                # 收集句首连续 cc=0 字符；遇第一个 cc>0 立即停止
                no_cc_end = sent_start
                for ci in range(sent_start, first_leader):
                    if characters[ci].check_count == 0:
                        no_cc_end = ci + 1
                    else:
                        break

                # 确定起始锚点：
                #   sent_start == 0（行首句）→ 借上一行末尾时间戳
                #   sent_start  > 0（行中句）→ 借上一句 global_sentence_end_ts
                if no_cc_end > sent_start:
                    _start_anchor: Optional[int] = None
                    if sent_start == 0:
                        if _prev_line_last_ts is None:
                            _prev_line_last_ts = self._find_prev_line_last_timestamp(idx)
                        _start_anchor = _prev_line_last_ts
                    else:
                        prev_end_ts = characters[sent_start - 1].global_sentence_end_ts
                        if prev_end_ts is not None:
                            _start_anchor = int(prev_end_ts)
                    if _start_anchor is None:
                        no_cc_end = sent_start  # 无有效锚点，不 wipe

                # 将 cc=0 字符分配到 [_start_anchor, first_leader_ts]
                old_algo_start = sent_start
                if no_cc_end > sent_start:
                    _end_nl = start_times[first_leader]
                    if _end_nl > _start_anchor:  # type: ignore[operator]
                        for ci, win in zip(
                            range(sent_start, no_cc_end),
                            _weighted_wipe_windows(
                                char_widths,
                                char_ink_widths,
                                range(sent_start, no_cc_end),
                                _start_anchor,  # type: ignore[arg-type]
                                _end_nl,
                            ),
                        ):
                            char_wipe_times[ci] = win
                    old_algo_start = no_cc_end

                # 剩余 pre-leader 字符（cc>0 未打轴）：
                # 老算法——按像素宽度加权分配到 first_leader 的 wipe 窗口内
                # （权重含 leader 自身，但只回写 pre-leader 字符的窗口）
                if old_algo_start < first_leader and first_leader in char_wipe_times:
                    leader_start_ts, leader_end_ts = char_wipe_times[first_leader]
                    for ci, win in zip(
                        range(old_algo_start, first_leader),
                        _weighted_wipe_windows(
                            char_widths,
                            char_ink_widths,
                            range(old_algo_start, first_leader + 1),
                            leader_start_ts,
                            leader_end_ts,
                        ),
                    ):
                        char_wipe_times[ci] = win

        # ---------- 每字符的 part 锚点序列（用于 check_count>=2 的多 checkpoint 字符） ----------
        # char_part_anchors[ci] = [ts_0, ts_1, ..., ts_N]，N = part 数
        #   - 仅在 ch.global_timestamps 数量 >= 2 时构造（即 check_count>=2 且至少打了 2 个轴）
        #   - 末尾的 ts_N 取 char_wipe_times[ci][1]（沿用现状的回退链：下一字符 ts[0] /
        #     global_sentence_end_ts / 下一行首 ts / _duration_ms）
        #   - ruby.parts 数与 check_count 不匹配也启用：渲染层会按 ruby 文本像素 N 等分回退
        char_part_anchors: dict[int, list[int]] = {}
        for ci, ch_obj in enumerate(characters):
            gts = list(ch_obj.global_timestamps)
            if len(gts) < 2:
                continue
            wt = char_wipe_times.get(ci)
            if not wt:
                continue
            seg_end_ts = wt[1]
            # 末尾锚点必须严格大于倒数第二个，否则视为脏数据，跳过
            if seg_end_ts <= gts[-1]:
                continue
            char_part_anchors[ci] = gts + [int(seg_end_ts)]

        # ---------- Ruby 整串墨水边界缓存 ----------
        # 单字符 ruby：ruby.text 整串的墨水盒；连词组 ruby：组内所有
        # ruby.text 拼接后整串的墨水盒。IPA/多码点簇用矢量轮廓（含 GPOS
        # 定位的附加符，tightBoundingRect 在 Windows 上会漏算），普通注音
        # 沿用 tightBoundingRect 像素口径。
        # 返回 (ink_left, ink_width, ink_top, ink_bottom)——top/bottom 相对
        # 基线（负=上方），wipe 裁剪与连词框几何用；无墨水时为 (0,0,0,0)。
        char_ruby_ink: dict[int, tuple[float, float, float, float]] = {}
        group_ruby_ink: dict[int, tuple[float, float, float, float]] = {}
        # ruby 是否走 IPA 字体（缓存于 entry，paintEvent 免逐帧扫描判定）
        char_ruby_is_ipa: dict[int, bool] = {}
        group_ruby_is_ipa: dict[int, bool] = {}

        def _ruby_ink_bounds(text: str) -> tuple[float, float, float, float]:
            fm_txt = _fm_for_ruby_text(text)
            if fm_txt is fm_ruby_ipa:
                # IPA 注音：矢量轮廓，含 GPOS 定位的上下附加符
                font_txt = self._font_ruby_ipa
                ink = _vector_ink_bounds(font_txt, text, self._vector_ink_cache)
                if ink is not None:
                    x, top, w, h = ink
                    return (x, w, top, top + h)
            # 非 IPA 注音：沿用历史 tightBoundingRect 像素口径
            rect = fm_txt.tightBoundingRect(text)
            if (
                rect.width() <= 0
                or rect.height() <= 0
                or rect.x() >= 100000
                or rect.y() >= 100000
            ):
                return (0.0, 0.0, 0.0, 0.0)
            return (
                float(rect.x()),
                float(rect.width()),
                float(rect.top()),
                float(rect.bottom()),
            )

        for ci, ch_obj in enumerate(characters):
            if ci in linked_leader_groups or ci in linked_non_leader:
                continue
            ruby = ch_obj.ruby
            if ruby and ruby.text:
                char_ruby_ink[ci] = _ruby_ink_bounds(ruby.text)
                char_ruby_is_ipa[ci] = _fm_for_ruby_text(ruby.text) is not fm_ruby
        for leader_ci, group in linked_leader_groups.items():
            merged_text = ""
            for _gci in group:
                _r = characters[_gci].ruby
                if _r:
                    merged_text += _r.text
            if merged_text:
                group_ruby_ink[leader_ci] = _ruby_ink_bounds(merged_text)
                group_ruby_is_ipa[leader_ci] = (
                    _fm_for_ruby_text(merged_text) is not fm_ruby
                )

        def _fm_for_char_ruby(ci: int, merged_text: str = "") -> QFontMetrics:
            # 连词组传合并后的整串（与 paintEvent 绘制同字体）；单字传自身
            text = merged_text
            if not text:
                ruby = characters[ci].ruby
                text = ruby.text if ruby and ruby.text else ""
            return _fm_for_ruby_text(text) if text else fm_ruby

        char_ruby_timed_spans, group_ruby_timed_spans = _ruby_timed_span_maps(
            _fm_for_char_ruby, characters, linked_leader_groups, linked_non_leader
        )

        # ---------- 连词框纵向几何（全行统一）----------
        # 历史行为：框顶/框高一律取字体盒（ruby_y - ascent - 1，height + 2），
        # 同行所有框高度一致——这是日语注音框的既视感基线。改按逐组墨水后
        # そう / ふ|た / り 相邻框高低不一（回归，用户报告），IPA 行 ˈgru / ʂɨ
        # 同理。修复：默认恢复字体盒，仅当**本行**任一注音的墨水越出字体盒
        # （叠加附加符/声调条）时按行统一扩张——保持「同行同高」。
        # 先取界（无填充），后统一 ±1 填充：墨水在盒内时结果与历史值逐像素
        # 相同（-ascent-1 / height-ascent+1），不会因填充把盒内墨水又顶出去。
        _box_top = -fm_ruby.ascent()
        _box_bottom = fm_ruby.height() - fm_ruby.ascent()
        for _ink in char_ruby_ink.values():
            _box_top = min(_box_top, _ink[2])
            _box_bottom = max(_box_bottom, _ink[3])
        for _ink in group_ruby_ink.values():
            _box_top = min(_box_top, _ink[2])
            _box_bottom = max(_box_bottom, _ink[3])
        ruby_frame_top_off = _box_top - 1
        ruby_frame_bottom_off = _box_bottom + 1

        # ---------- 连词组 ruby 的分段 wipe 时间轴 ----------
        # 连词组的 ruby 横跨整个组（如「明日」中「日」无节奏点，与「明」合并重分配
        # 区间）。ruby 的假名边界必须来自各成员**实际打轴的** global_timestamps，
        # 而非成员主文字 wipe 窗口的末端——后者可能是重分配出来的临时分割点
        # （明的主文字在组中段就走完，若用它当 ruby 末锚点，あした 会在「明」走完时
        # 就全部走完，丢失「日」对应的时长）。
        #
        # 做法：把组内所有成员的 (mora 时间戳, mora 墨水 advance) 按序摊平，相邻时间戳
        # 即段边界；**最后一段延伸到整组真正的结束时间**（组尾成员 wipe 终点）。
        # 空 part（advance=0）消耗时间但不推进墨水；段间空隙保持不推进。
        group_ruby_wipe: dict[int, list[tuple[int, int, float, float]]] = {}
        for leader_ci, group in linked_leader_groups.items():
            # 组真正的结束时间：组尾成员主文字 wipe 终点（无则回退到组内最大终点）
            group_end: int | None = None
            _gw_last = char_wipe_times.get(group[-1])
            if _gw_last:
                group_end = _gw_last[1]
            else:
                for gci in group:
                    _wt = char_wipe_times.get(gci)
                    if _wt and (group_end is None or _wt[1] > group_end):
                        group_end = _wt[1]
            # 摊平各成员的 mora：(时间戳, 墨水 advance)
            flat: list[tuple[int, float]] = []
            for gci in group:
                ruby = characters[gci].ruby
                if not ruby or not ruby.text:
                    continue
                fm_ruby_member = _fm_for_ruby_text(ruby.text)
                gts = list(characters[gci].global_timestamps)
                parts = ruby.parts if ruby.parts else []
                if gts and len(parts) == len(gts):
                    for _ts, _p in zip(gts, parts):
                        flat.append((int(_ts), float(fm_ruby_member.horizontalAdvance(_p.text))))
                elif gts:
                    # parts 数与时间戳数不一致：整串 advance 等分到各时间戳
                    _per = float(fm_ruby_member.horizontalAdvance(ruby.text)) / len(gts)
                    for _ts in gts:
                        flat.append((int(_ts), _per))
                else:
                    # 成员无自身节奏点但带 ruby：以其主文字 wipe 起点为锚整段推进
                    _wt = char_wipe_times.get(gci)
                    if _wt:
                        flat.append(
                            (_wt[0], float(fm_ruby_member.horizontalAdvance(ruby.text)))
                        )
            if not flat or group_end is None:
                continue
            # 时间戳须单调（防脏数据）；末锚点延伸到 group_end
            flat.sort(key=lambda it: it[0])
            total_w = sum(w for _, w in flat)
            if total_w <= 0:
                continue
            segs: list[tuple[int, int, float, float]] = []
            cum = 0.0
            for i, (_ts, _w) in enumerate(flat):
                t0 = _ts
                t1 = flat[i + 1][0] if i + 1 < len(flat) else group_end
                if t1 < t0:
                    t1 = t0
                w0 = cum / total_w
                cum += _w
                segs.append((t0, t1, w0, cum / total_w))
            group_ruby_wipe[leader_ci] = segs

        entry = {
            "v": line_version,
            "gv": self._global_version,
            "fk": font_key,
            "char_widths": char_widths,
            "char_ink_offsets": char_ink_offsets,
            "char_ink_widths": char_ink_widths,
            "char_advances": char_advances,
            "char_uses_ipa": char_uses_ipa,
            "ipa_styling": has_ipa_styling,
            "line_ink_top": line_ink_top,
            "line_ink_bottom": line_ink_bottom,
            "end_sentence_w": end_sentence_w,
            "total_text_width": sum(char_widths) + sum(end_sentence_w.values()),
            "char_wipe_times": char_wipe_times,
            "linked_leader_groups": linked_leader_groups,
            "linked_non_leader": linked_non_leader,
            "non_leader_to_leader": non_leader_to_leader,
            "char_part_anchors": char_part_anchors,
            "seg_anchor_groups": seg_anchor_groups,
            "seg_member_to_leader": seg_member_to_leader,
            "char_ruby_ink": char_ruby_ink,
            "group_ruby_ink": group_ruby_ink,
            "char_ruby_is_ipa": char_ruby_is_ipa,
            "group_ruby_is_ipa": group_ruby_is_ipa,
            "ruby_frame_top_off": ruby_frame_top_off,
            "ruby_frame_bottom_off": ruby_frame_bottom_off,
            "char_ruby_timed_spans": char_ruby_timed_spans,
            "group_ruby_timed_spans": group_ruby_timed_spans,
            "group_ruby_wipe": group_ruby_wipe,
        }
        self._sentence_cache[cache_key] = entry
        if _perf_start is not None:
            log_elapsed(
                "preview.render_data",
                _perf_start,
                8,
                line=idx,
                chars=n_chars,
                font=font_key,
                linked_groups=len(linked_leader_groups),
                anchors=len(start_times),
                cache=len(self._sentence_cache),
            )
        return entry

    def _find_nearest_hitbox(self, click_x: int, click_y: int) -> tuple[str, int, int, int | None] | None:
        """在行内空白区域找水平距离最近的 hitbox

        Returns:
            (hit_type, line_idx, char_idx, cp_idx) 或 None
        """
        # 收集所有 hitbox（字符和 checkpoint），按行分组
        line_hitboxes: dict[int, list[tuple[QRect, str, int, int, int | None]]] = {}
        for char_rect, line_idx, char_idx in self._char_hitboxes:
            if line_idx not in line_hitboxes:
                line_hitboxes[line_idx] = []
            line_hitboxes[line_idx].append((char_rect, "char", line_idx, char_idx, None))
        for marker_rect, line_idx, char_idx, cp_idx in self._checkpoint_hitboxes:
            if line_idx not in line_hitboxes:
                line_hitboxes[line_idx] = []
            line_hitboxes[line_idx].append((marker_rect, "cp", line_idx, char_idx, cp_idx))

        # 找到点击所在的行（垂直范围内）
        for line_idx, hitboxes in line_hitboxes.items():
            if not hitboxes:
                continue
            # 检查是否在行内垂直范围内
            first_rect = hitboxes[0][0]
            rect_top = first_rect.top()
            rect_bottom = first_rect.bottom()
            if not (rect_top <= click_y <= rect_bottom):
                continue
            # 在该行内找水平距离最近的 hitbox
            min_dist = float('inf')
            nearest = None
            for rect, hit_type, li, ci, cpi in hitboxes:
                # 计算水平距离
                if click_x < rect.left():
                    dist = rect.left() - click_x
                elif click_x > rect.right():
                    dist = click_x - rect.right()
                else:
                    dist = 0
                if dist < min_dist:
                    min_dist = dist
                    nearest = (hit_type, li, ci, cpi)
            return nearest
        return None

    def mouseDoubleClickEvent(self, a0: Optional[QMouseEvent]):
        """双击 → 跳转到时间戳"""
        if not a0 or not self._project or not self._project.sentences:
            return

        click_x = int(a0.position().x())
        click_y = int(a0.position().y())

        # Ctrl+左键属于跨字符多选手势，不参与双击跳转
        if a0.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return

        # 双击时停止单击定时器
        self._click_timer.stop()
        # 标记双击已处理，防止随后的 Release #2 触发单击逻辑（Qt 双击序列：
        # Press→Release→DblClick→Release，Release #2 需要跳过）
        self._double_click_handled = True

        # 双击同样受"禁用点击时居中"控制：seek 信号链路同步执行，
        # 在窗口内置位即可抑制随后的 scroll_current_line_to_center。
        self._suppress_click_recenter = self._disable_click_recenter
        try:
            # 优先使用快照判断双击目标（快照在单击时锁定，避免居中导致 hitbox 变化）
            # 禁用单击跳转时不使用快照，直接走当前 hitbox 判断
            if not self._disable_click_jump and self._click_snapshot is not None:
                snapshot = self._click_snapshot
                self._click_snapshot = None
                if snapshot["type"] == "cp":
                    self.seek_to_checkpoint_requested.emit(
                        snapshot["line_idx"], snapshot["char_idx"], snapshot["cp_idx"]
                    )
                    return
                elif snapshot["type"] == "char":
                    self.seek_to_char_requested.emit(
                        snapshot["line_idx"], snapshot["char_idx"]
                    )
                    return

            # 快照不存在时（如禁用单击跳转），回退到当前 hitbox 判断
            for marker_rect, line_idx, char_idx, cp_idx in self._checkpoint_hitboxes:
                if marker_rect.contains(click_x, click_y):
                    self.seek_to_checkpoint_requested.emit(line_idx, char_idx, cp_idx)
                    return

            for char_rect, line_idx, char_idx in self._char_hitboxes:
                if char_rect.contains(click_x, click_y):
                    self.seek_to_char_requested.emit(line_idx, char_idx)
                    return

            # 双击在行内空白区域：按水平距离找最近的 hitbox
            nearest = self._find_nearest_hitbox(click_x, click_y)
            if nearest:
                hit_type, line_idx, char_idx, cp_idx = nearest
                if hit_type == "cp":
                    self.seek_to_checkpoint_requested.emit(line_idx, char_idx, cp_idx)
                else:
                    self.seek_to_char_requested.emit(line_idx, char_idx)
                return
        finally:
            self._suppress_click_recenter = False

    def _compute_guide_alpha(self, characters, cursor_idx: int) -> dict:
        """走字预览指引：返回 ``char_pos -> 不透明度(alphaF)`` 映射。

        以打轴光标 ``cursor_idx``（``_current_char_idx``）为锚，而非时间戳分布——
        "用户正在打哪个字"的唯一真相来源是光标，回跳/seek/跳着打都能正确跟随。

        分群：每个 ``check_count > 0`` 的字符开启一个新字群，其后紧邻的
        ``check_count == 0``（无 cc，不单独打轴）字符并入该字群——即"上一个打的字
        与当前打的字之间不需要打的字"会被组合进同一个字群。

        再按光标所在字群相对位置上色（分色由 ``_draw_split_text`` 负责），
        不透明度由用户自定义的 ``set_preview_guide_config`` 提供。
        """
        groups: list[list[int]] = []
        char_to_group: dict[int, int] = {}
        cur: Optional[list[int]] = None
        for ci, ch in enumerate(characters):
            if ch.check_count > 0:
                cur = [ci]
                groups.append(cur)
            elif cur is not None:
                cur.append(ci)
            else:
                cur = [ci]
                groups.append(cur)
            char_to_group[ci] = len(groups) - 1
        if not groups:
            return {}

        ci_clamped = max(0, min(cursor_idx, len(characters) - 1))
        cur_gi = char_to_group.get(ci_clamped, 0)

        alpha: dict[int, float] = {}
        if self._guide_curr_enabled:
            for ci in groups[cur_gi]:
                alpha[ci] = self._guide_curr_alpha
        if self._guide_prev_enabled and cur_gi - 1 >= 0:
            for ci in groups[cur_gi - 1]:
                alpha[ci] = self._guide_prev_alpha
        if self._guide_next_enabled and cur_gi + 1 < len(groups):
            for ci in groups[cur_gi + 1]:
                alpha[ci] = self._guide_next_alpha
        return alpha

    # ---- 绘制 ----

    @log_slow_method(
        "preview.paint",
        20,
        lambda self, args, kwargs: {
            "time_ms": self._current_time_ms,
            "playing": self._is_playing,
            "line": self._current_line_idx,
            "auto_line": self._last_auto_scroll_line_idx,
            "visible": self._visible_lines,
            "cache": len(self._sentence_cache),
        },
    )
    def paintEvent(self, a0: Optional[QPaintEvent]):
        if self._horizontal_layout_dirty:
            self._update_horizontal_scrollbar_range()
        elif self._viewport_calc_center != self._scroll_center_line:
            # 滚动中心变化：防抖，停稳后重算范围 + 预热新可视窗口
            self._viewport_settle_timer.start()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 填充背景
        painter.fillRect(self.rect(), theme.karaoke_bg)

        # 清空 hitbox 缓存
        self._checkpoint_hitboxes = []
        self._char_hitboxes = []

        # 渲染时间：播放中主动拉取基于 perf_counter 外推的高精度时间，
        # 消除 QTimer 间隔 + Qt paint 调度带来的 ~16ms 抖动。
        if self._is_playing and self._audio_engine is not None:
            display_getter = getattr(self._audio_engine, "get_display_position_ms", None)
            current_time = (
                int(display_getter())
                if callable(display_getter)
                else self._audio_engine.get_position_ms()
            )
            self._current_time_ms = current_time
        else:
            current_time = self._current_time_ms

        if not self._project or not self._project.sentences:
            painter.setPen(theme.text_hint)
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                self.tr("请拖入sug项目或者歌词文件或ctrl+v粘贴剪贴板上的歌词文件")
            )
            painter.end()
            return

        w, h = self.width(), self.height()
        total = len(self._project.sentences)
        line_height = h / self._visible_lines
        center_y = h / 2.0

        font_current = self._font_current
        font_context = self._font_context
        font_ruby = self._font_ruby
        font_checkpoint = self._font_checkpoint
        # IPA 段字体（与上方常规字体成对出现；详见 _get_sentence_render_data）
        font_current_ipa = getattr(self, "_font_current_ipa", font_current)
        font_context_ipa = getattr(self, "_font_context_ipa", font_context)
        font_ruby_ipa = getattr(self, "_font_ruby_ipa", font_ruby)

        fm_current = self._fm_current
        fm_context = self._fm_context
        fm_ruby = self._fm_ruby
        fm_checkpoint = self._fm_checkpoint
        fm_current_ipa = getattr(self, "_fm_current_ipa", fm_current)
        fm_context_ipa = getattr(self, "_fm_context_ipa", fm_context)
        fm_ruby_ipa = getattr(self, "_fm_ruby_ipa", fm_ruby)

        default_highlight = theme.default_highlight

        # 计算可见行范围（留 1 行余量避免边缘裁切）
        half_visible = self._visible_lines / 2.0 + 1
        first_visible = max(0, int(self._scroll_center_line - half_visible))
        last_visible = min(total - 1, int(self._scroll_center_line + half_visible))

        # 播放+自动滚动时，用播放行做视觉高亮，不污染编辑光标
        effective_current = self._effective_current_line()

        _norm_sel = self.get_normalized_selection() if self._focus_char_idx >= 0 else None
        _ext_sel_set: Optional[set[tuple[int, int]]] = None
        if self._ext_selection_chars is not None:
            _ext_sel_set = set(self._ext_selection_chars)
            if self._ctrl_sel_active:
                _ext_sel_set |= self._ctrl_pending_chars()
        elif self._ctrl_sel_active:
            # Ctrl 拖拽期间仍显示原有单段选区，松开时才并入扩展选区
            _ext_sel_set = self._ctrl_pending_chars()
            _legacy_sel = self._legacy_normalized_selection()
            if _legacy_sel is not None:
                _ext_sel_set |= set(self._iter_range_chars(*_legacy_sel))
        _draw_focus_selection = bool(_ext_sel_set) or (
            _norm_sel is not None
            and (_norm_sel[0] != _norm_sel[2] or _norm_sel[1] != _norm_sel[3])
        )

        for idx in range(first_visible, last_visible + 1):
            # 行中心 y 坐标
            y_center_f = center_y + (idx - self._scroll_center_line) * line_height
            y_center = int(round(y_center_f))

            # 跳过完全不可见的行
            if y_center_f < -line_height or y_center_f > h + line_height:
                continue

            line = self._project.sentences[idx]
            is_current = idx == effective_current

            # 绘制行号（左侧固定区域）
            painter.setFont(self._font_line_number)
            line_num_color = theme.line_number_current if is_current else theme.line_number_normal
            painter.setPen(line_num_color)
            line_num_text = str(idx + 1)
            line_num_w = self._fm_line_number.horizontalAdvance(line_num_text)
            painter.drawText(
                int(self._line_number_margin - line_num_w - 5),
                int(y_center),
                line_num_text,
            )

            # 根据演唱者获取行级别默认高亮颜色
            singer = (
                self._project.get_singer(line.singer_id) if line.singer_id else None
            )
            highlight_color = (
                QColor(singer.color) if singer and singer.color else default_highlight
            )

            # 预计算每个字符的 per-char singer 颜色（从 Character.singer_id 读取）
            # 值为 List[QColor]：solid 模式长度为 1，split 模式长度 2-5
            _char_singer_colors: dict = {}  # char_idx -> List[QColor] (基色列表)
            _char_complement_colors: dict = {}  # char_idx -> QColor (选中高亮色 = 演唱者补色)
            default_singer = self._project.get_default_singer()
            for ci, char in enumerate(line.characters):
                singer_obj = self._project.get_singer(char.singer_id)
                eff = singer_obj if (singer_obj and singer_obj.color) else default_singer
                if eff:
                    all_colors = [QColor(c) for c in eff.get_all_colors()]
                    comp_color = eff.complement_color or eff.color
                else:
                    all_colors = [QColor("#FFFFFF")]
                    comp_color = "#FFFFFF"
                _char_singer_colors[ci] = all_colors
                _char_complement_colors[ci] = QColor(comp_color)

            if is_current:
                main_font = font_current
                main_fm = fm_current
                base_color = theme.karaoke_text_current
            elif idx < effective_current:
                main_font = font_context
                main_fm = fm_context
                base_color = theme.karaoke_text_past
            else:
                main_font = font_context
                main_fm = fm_context
                base_color = theme.karaoke_text_future

            # 打轴指引开启时，Ruby 使用主题纯黑/白；非当前行把原 past/future
            # 灰度换算成等效 alpha 上限，未打轴/已打轴再保持 30%/100% 比例。
            # 关闭时由 _draw_ruby_base 直接绘制原有 base_color。
            _ruby_foreground = theme.karaoke_text_current
            _ruby_max_alpha = _foreground_alpha_for_color(
                base_color, _ruby_foreground, theme.karaoke_bg
            )

            # 走字预览指引：仅在「设置开启 + 播放中 + 打轴光标所在行」时，对本行
            # 以光标 _current_char_idx 为锚，计算"上一个/正在/下一个"字群的过渡不透明度。
            # 锚定 _current_line_idx（打轴光标行）而非 effective_current（视觉高亮行），
            # 两者在播放/自动滚动挂起等状态下可能不一致。
            if (
                self._preview_guide_enabled
                and self._is_playing
                and idx == self._current_line_idx
            ):
                guide_alpha = self._compute_guide_alpha(
                    line.characters, self._current_char_idx
                )
            else:
                guide_alpha = {}

            # 使用缓存的渲染数据（字符宽度/分组/wipe时间/连词信息）
            _rd = self._get_sentence_render_data(
                idx, line, main_fm, "cur" if is_current else "ctx"
            )
            char_widths = _rd["char_widths"]
            _char_ink_offsets = _rd["char_ink_offsets"]
            _char_ink_widths = _rd["char_ink_widths"]
            char_advances = _rd.get("char_advances")
            _char_uses_ipa = _rd.get("char_uses_ipa")
            _end_sentence_w = _rd["end_sentence_w"]
            total_text_width = _rd["total_text_width"]
            char_wipe_times = _rd["char_wipe_times"]
            _linked_leader_groups = _rd["linked_leader_groups"]
            _linked_non_leader = _rd["linked_non_leader"]
            _non_leader_to_leader = _rd["non_leader_to_leader"]
            _char_part_anchors = _rd["char_part_anchors"]
            _seg_anchor_groups = _rd["seg_anchor_groups"]
            _seg_member_to_leader = _rd["seg_member_to_leader"]
            # 连词组 ruby 框几何缓存：leader 那一轮画完框后存 (x, y, w, h, color)，
            # 后续 non-leader 字符的 fillRect 会盖住框的右半段 outline，需要从这里
            # 查表重绘以恢复框。
            _linked_frame_data: dict = {}
            _char_ruby_ink = _rd["char_ruby_ink"]
            _group_ruby_ink = _rd["group_ruby_ink"]
            _char_ruby_is_ipa = _rd.get("char_ruby_is_ipa", {})
            _group_ruby_is_ipa = _rd.get("group_ruby_is_ipa", {})
            # 连词框纵向几何（全行统一，见 _get_sentence_render_data）
            _frame_top_off = _rd.get("ruby_frame_top_off", -fm_ruby.ascent() - 1)
            _frame_bottom_off = _rd.get(
                "ruby_frame_bottom_off", fm_ruby.height() - fm_ruby.ascent() + 1
            )
            _char_ruby_timed_spans = _rd["char_ruby_timed_spans"]
            _group_ruby_timed_spans = _rd["group_ruby_timed_spans"]
            _group_ruby_wipe = _rd["group_ruby_wipe"]

            # 根据对齐方式计算起始 x 坐标
            text_area_left = self._line_number_margin + 5  # 行号区域右侧留 5px 间距
            sb_w = self._SCROLLBAR_WIDTH if self._scrollbar.isVisible() else 0
            text_area_right = w - sb_w  # 文本区域右边界，为滚动条留出空间
            start_x = self._line_start_x(
                total_text_width, text_area_left, text_area_right
            )
            start_x += self._horizontal_shift()

            curr_x = start_x

            # 本行所有非空白字符的实际墨水上下边界（缓存于 entry：普通文本
            # 用 tightBoundingRect 像素口径，IPA/多码点簇用矢量轮廓含上下
            # 附加符；无字形码点的空矩形哨兵已剔除，零 advance 孤立附加符
            # 由矢量墨水兜底计入，避免 0 宽格子）。
            # 用于分色渲染时统一色带分界线，使同行各字符的分色视觉位置一致。
            _line_ink_top = int(y_center + _rd["line_ink_top"])
            _line_ink_bottom = int(y_center + _rd["line_ink_bottom"]) + 1

            # 高亮/选中/hitbox 矩形几何：覆盖从主文字 ink 顶到最大 marker ink 底，
            # per-line 统一。
            # - 顶端 _line_ink_top - 2px 留呼吸 padding；ruby 框被 non-leader fillRect
            #   覆盖的问题由下面的 non-leader 框重绘逻辑兜底，不依赖几何缝。
            # - 底端用 fm_checkpoint.height() 作为 marker 包络（而非 tightBoundingRect
            #   高度），抗 hinting/AA 边缘像素，避免 rect 切到 ▯/▮ 顶端外缘。
            _LINE_RECT_PAD = 2
            _marker_extent_h = fm_checkpoint.height()
            _line_marker_ink_bottom = _line_ink_bottom + self._cp_spacing + _marker_extent_h
            _rect_top = _line_ink_top - _LINE_RECT_PAD
            _rect_bottom = _line_marker_ink_bottom + _LINE_RECT_PAD
            _rect_height = max(1, _rect_bottom - _rect_top)
            # 行高 clamp（极紧凑布局时不溢出本行）
            if _rect_height > int(line_height):
                _rect_height = int(line_height)
                _rect_top = int(round(y_center_f - _rect_height / 2))

            for char_pos, ch in enumerate(line.chars):
                char_w = char_widths[char_pos]

                # 当前打轴位置高亮背景
                if (not self._hide_hitbox_highlights) and is_current and char_pos == self._current_char_idx:
                    highlight_bg = theme.karaoke_highlight_bg
                    bg_rect = QRect(
                        int(curr_x) - 1,
                        _rect_top,
                        int(char_w) + 2,
                        _rect_height,
                    )
                    painter.fillRect(bg_rect, highlight_bg)

                # 隐藏模式下仅当存在跨字符多选时绘制 focus 高亮（拖选结束仍保持）
                _show_sel = (not self._hide_hitbox_highlights) or _draw_focus_selection
                _char_selected = False
                if _ext_sel_set is not None:
                    _char_selected = (idx, char_pos) in _ext_sel_set
                elif _show_sel and _norm_sel is not None:
                    _sl, _sc, _el, _ec = _norm_sel
                    if _sl <= idx <= _el:
                        _lo = _sc if idx == _sl else 0
                        _hi = _ec if idx == _el else len(line.chars) - 1
                        _char_selected = _lo <= char_pos <= _hi
                if _show_sel and _char_selected:
                    sel_bg = theme.karaoke_selection_bg
                    sel_rect = QRect(
                        int(curr_x) - 1,
                        _rect_top,
                        int(char_w) + 2,
                        _rect_height,
                    )
                    painter.fillRect(sel_rect, sel_bg)

                # 连词组 non-leader：fillRect 可能覆盖 leader 那一轮画的 ruby 框的
                # 右半段 outline，这里查表重绘恢复。leader 自己不需要重绘（它本轮的
                # 框绘制在 fillRect 之后已经在上方完成）。
                if char_pos in _linked_non_leader:
                    _ldr = _non_leader_to_leader.get(char_pos)
                    _fd = _linked_frame_data.get(_ldr) if _ldr is not None else None
                    if _fd is not None:
                        _fx, _fy, _fw, _fh, _fcolor = _fd
                        painter.save()
                        _fp = QPen(_fcolor, 1.0)
                        _fp.setStyle(Qt.PenStyle.SolidLine)
                        painter.setPen(_fp)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawRoundedRect(_fx, _fy, _fw, _fh, 2, 2)
                        painter.restore()

                # 存储字符 hitbox 用于点击检测（与高亮矩形对齐）
                char_rect = QRect(
                    int(curr_x),
                    _rect_top,
                    int(char_w),
                    _rect_height,
                )
                self._char_hitboxes.append((char_rect, idx, char_pos))

                # Ruby — 连词组合并绘制 / 单字独立绘制
                if char_pos in _linked_non_leader:
                    pass  # Ruby 由组 leader 统一绘制
                elif char_pos in _linked_leader_groups:
                    # 连词组 leader：收集组内所有 ruby 合并绘制
                    _grp = _linked_leader_groups[char_pos]
                    _grp_rubies: list = []
                    for _gci in _grp:
                        _r = line.characters[_gci].ruby
                        if _r:
                            _grp_rubies.append(_r)
                    if _grp_rubies:
                        _merged = "".join(r.text for r in _grp_rubies)
                        # ruby 含 IPA 时整串用 IPA 注音字体（同字体才能锚定附加符）
                        # IPA 判定已在渲染数据缓存（免逐帧扫描，审查反馈 #8）
                        _ruby_is_ipa = _group_ruby_is_ipa.get(char_pos, False)
                        _ruby_font = font_ruby_ipa if _ruby_is_ipa else font_ruby
                        _fm_ruby_here = fm_ruby_ipa if _ruby_is_ipa else fm_ruby
                        _grp_w = sum(char_widths[g] for g in _grp)
                        ruby_text_w = _fm_ruby_here.horizontalAdvance(_merged)
                        ruby_x = curr_x + (_grp_w - ruby_text_w) // 2
                        ruby_y = int(y_center - main_fm.ascent() - self._ruby_spacing)
                        # Ruby 整串墨水边界（合并后整体墨水盒；普通注音为
                        # tightBoundingRect 像素口径，IPA 为矢量轮廓）：
                        # wipe 按墨水起止点裁剪，不再扫过 ruby 首尾的透明侧 bearings。
                        _r_ink_off, _r_ink_w, _r_ink_top, _r_ink_bottom = _group_ruby_ink.get(
                            char_pos, (0, ruby_text_w, -_fm_ruby_here.ascent(), _fm_ruby_here.descent())
                        )
                        _r_ink_x = ruby_x + _r_ink_off
                        painter.setFont(_ruby_font)
                        _rh_colors = _char_singer_colors.get(_grp[0], [highlight_color])
                        # Ruby 分色边界：基于该 ruby 串的实际墨水范围
                        _rh_ink_top = int(ruby_y + _r_ink_top)
                        _rh_ink_bottom = int(ruby_y + _r_ink_bottom) + 1
                        # 注音状态接入打轴指引总开关；关闭时恢复旧版纯基色。
                        # 后面的 singer-color wipe 保持既有实现，原样叠在此底层之上。
                        _draw_ruby_base(
                            painter,
                            int(ruby_x),
                            ruby_y,
                            _merged,
                            _group_ruby_timed_spans.get(char_pos, []),
                            _fm_ruby_here,
                            _ruby_foreground,
                            _ruby_max_alpha,
                            guide_enabled=self._preview_guide_enabled,
                            fallback_color=base_color,
                        )
                        # Wipe — 连词组 ruby 与原字符逻辑一致：按各成员/各 part 的
                        # 时间轴分段，空 part 不推进、段间空隙保持，墨水边缘走字（非匀速）。
                        # 缺分段数据时回退为整段线性（组首 wipe 始 → 组尾 wipe 终）。
                        _segs = _group_ruby_wipe.get(char_pos)
                        if _segs:
                            _rr = _piecewise_wipe_ratio(_segs, current_time)
                            if _rr >= 1.0:
                                _draw_split_text(
                                    painter, int(ruby_x), ruby_y, _merged,
                                    _rh_colors, _rh_ink_top, _rh_ink_bottom,
                                )
                            elif _rr > 0 and _r_ink_w > 0:
                                painter.save()
                                _rww = int(_r_ink_w * _rr)
                                painter.setClipRect(
                                    QRect(
                                        int(_r_ink_x),
                                        int(ruby_y + _r_ink_top) - 2,
                                        _rww,
                                        int(_r_ink_bottom - _r_ink_top) + 4,
                                    )
                                )
                                _draw_split_text(
                                    painter, int(ruby_x), ruby_y, _merged,
                                    _rh_colors, _rh_ink_top, _rh_ink_bottom,
                                )
                                painter.restore()
                        else:
                            _fw = char_wipe_times.get(_grp[0])
                            _lw = char_wipe_times.get(_grp[-1])
                            _rs = _fw[0] if _fw else None
                            _re = _lw[1] if _lw else None
                            if _rs is not None and _re is not None:
                                if current_time >= _re:
                                    _draw_split_text(
                                        painter, int(ruby_x), ruby_y, _merged,
                                        _rh_colors, _rh_ink_top, _rh_ink_bottom,
                                    )
                                elif current_time >= _rs:
                                    _rd = _re - _rs
                                    _rr = (
                                        min(1.0, (current_time - _rs) / _rd)
                                        if _rd > 0
                                        else 1.0
                                    )
                                    if _rr > 0 and _r_ink_w > 0:
                                        painter.save()
                                        _rww = int(_r_ink_w * _rr)
                                        painter.setClipRect(
                                            QRect(
                                                int(_r_ink_x),
                                                int(ruby_y + _r_ink_top) - 2,
                                                _rww,
                                                int(_r_ink_bottom - _r_ink_top) + 4,
                                            )
                                        )
                                        _draw_split_text(
                                            painter, int(ruby_x), ruby_y, _merged,
                                            _rh_colors, _rh_ink_top, _rh_ink_bottom,
                                        )
                                        painter.restore()
                        # 连词框：Ruby 拼接串实际总宽 vs 字符组墨水宽度取更宽者
                        # （历史方法1/方法2 逻辑，保留原口径）。方法2 的字符墨水
                        # 取**格子左缘 + 墨水偏移**、_cum 累计格子宽——格子宽已含
                        # 节奏点标记/ruby 的加宽，因此框跟随下方主文字格子（含
                        # 节奏点占用），不引入格子内居中修正（用户报告：加了居中
                        # 修正后框贴到字形边缘、不再覆盖 2人 的节奏点占用，是回归）。
                        # --- 方法1：Ruby 拼接串墨水边界 ---
                        # 比较基准沿用历史口径：**排版宽**（advance）而非墨水宽
                        # （原代码 _ruby_box_w = ruby_text_w；方法选择的行为不变）
                        _ruby_box_left = _r_ink_x
                        _ruby_box_right = _r_ink_x + _r_ink_w
                        _ruby_box_w = ruby_text_w
                        # --- 方法2：字符组墨水边界并集（格子口径，历史行为）---
                        _char_ink_left = float('inf')
                        _char_ink_right = float('-inf')
                        _cum = 0
                        for _gci in _grp:
                            _char_ink_x = curr_x + _cum + _char_ink_offsets[_gci]
                            _char_ink_left = min(_char_ink_left, _char_ink_x)
                            _char_ink_right = max(_char_ink_right, _char_ink_x + _char_ink_widths[_gci])
                            _cum += char_widths[_gci]
                        _char_box_w = _char_ink_right - _char_ink_left
                        if _ruby_box_w < _char_box_w:
                            _box_left = int(_char_ink_left)
                            _box_right = int(_char_ink_right)
                        else:
                            _box_left = int(_ruby_box_left)
                            _box_right = int(_ruby_box_right)
                        _frame_x = _box_left - 2
                        # 框纵向几何**全行统一**（渲染数据缓存）：默认字体盒
                        # （ruby_y - ascent - 1 / height + 2，历史行为，同行同高），
                        # 仅当本行任一注音墨水越出字体盒（IPA 叠加附加符/声调条）
                        # 时按行整体扩张。逐组各用各的墨水会让 そう / ふ|た / り
                        # 相邻框高低不一（用户报告的回归）。
                        _frame_y = int(ruby_y + _frame_top_off)
                        _frame_w = (_box_right - _box_left) + 4
                        _frame_h = int(_frame_bottom_off - _frame_top_off)
                        _frame_color = QColor(base_color)
                        _frame_color.setAlpha(120)
                        painter.save()
                        _fp = QPen(_frame_color, 1.0)
                        _fp.setStyle(Qt.PenStyle.SolidLine)
                        painter.setPen(_fp)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawRoundedRect(
                            _frame_x, _frame_y, _frame_w, _frame_h, 2, 2,
                        )
                        painter.restore()
                        # 缓存框几何，供同组 non-leader 字符的 fillRect 后重绘
                        _linked_frame_data[char_pos] = (
                            _frame_x, _frame_y, _frame_w, _frame_h, _frame_color,
                        )
                else:
                    ruby = line.characters[char_pos].ruby
                    if ruby:
                        # 单字符 ruby（per-char 模型）
                        _ruby_disp = ruby.text
                        # IPA 判定已在渲染数据缓存（免逐帧扫描，审查反馈 #8）
                        _ruby_is_ipa = _char_ruby_is_ipa.get(char_pos, False)
                        _ruby_font = font_ruby_ipa if _ruby_is_ipa else font_ruby
                        _fm_ruby_here = fm_ruby_ipa if _ruby_is_ipa else fm_ruby
                        ruby_text_w = _fm_ruby_here.horizontalAdvance(_ruby_disp)
                        ruby_x = curr_x + (char_w - ruby_text_w) // 2
                        ruby_y = int(y_center - main_fm.ascent() - self._ruby_spacing)
                        # Ruby 文本整串墨水边界（普通注音 tight 像素口径，
                        # IPA 矢量轮廓含上下附加符）
                        _r_ink_off, _r_ink_w, _r_ink_top, _r_ink_bottom = _char_ruby_ink.get(
                            char_pos,
                            (0, ruby_text_w, -_fm_ruby_here.ascent(), _fm_ruby_here.descent()),
                        )
                        _r_ink_x = ruby_x + _r_ink_off
                        painter.setFont(_ruby_font)
                        ruby_highlight_colors = _char_singer_colors.get(
                            char_pos, [highlight_color]
                        )
                        # Ruby 分色边界：基于该 ruby 串的实际墨水范围
                        _ruby_ink_top = int(ruby_y + _r_ink_top)
                        _ruby_ink_bottom = int(ruby_y + _r_ink_bottom) + 1
                        # 注音状态接入打轴指引总开关；关闭时恢复旧版纯基色。
                        # 后面的 singer-color wipe 保持既有实现，原样叠在此底层之上。
                        _draw_ruby_base(
                            painter,
                            int(ruby_x),
                            ruby_y,
                            _ruby_disp,
                            _char_ruby_timed_spans.get(char_pos, []),
                            _fm_ruby_here,
                            _ruby_foreground,
                            _ruby_max_alpha,
                            guide_enabled=self._preview_guide_enabled,
                            fallback_color=base_color,
                        )
                        # Wipe — 优先用 part 锚点轴分段；缺锚点回退旧整段线性
                        _r_anchors = _char_part_anchors.get(char_pos)
                        if _r_anchors is not None and len(_r_anchors) >= 2:
                            _i, _sr, _n = _anchor_segment(_r_anchors, current_time)
                            if _n > 0:
                                # 计算整体进度比例 _ratio ∈ [0,1]，再乘 ink 宽度。
                                # parts 数与段数 N 匹配则按 part 实际 advance 占比，否则等分。
                                _parts = ruby.parts if ruby.parts else []
                                if len(_parts) == _n and _n > 0:
                                    _part_ws = [
                                        _fm_ruby_here.horizontalAdvance(p.text)
                                        for p in _parts
                                    ]
                                    _total_pw = sum(_part_ws)
                                    if _total_pw > 0:
                                        _cum = sum(_part_ws[:_i])
                                        _local = _part_ws[_i] * _sr
                                        _ratio = (_cum + _local) / _total_pw
                                    else:
                                        _ratio = (_i + _sr) / _n
                                else:
                                    _ratio = (_i + _sr) / _n
                                r_wipe_w = int(_r_ink_w * _ratio) if _r_ink_w > 0 else 0
                                if _ratio >= 1.0:
                                    _draw_split_text(
                                        painter, int(ruby_x), ruby_y, _ruby_disp,
                                        ruby_highlight_colors, _ruby_ink_top, _ruby_ink_bottom,
                                    )
                                elif r_wipe_w > 0:
                                    painter.save()
                                    painter.setClipRect(
                                        QRect(
                                            int(_r_ink_x),
                                            int(ruby_y + _r_ink_top) - 2,
                                            r_wipe_w,
                                            int(_r_ink_bottom - _r_ink_top) + 4,
                                        )
                                    )
                                    _draw_split_text(
                                        painter, int(ruby_x), ruby_y, _ruby_disp,
                                        ruby_highlight_colors, _ruby_ink_top, _ruby_ink_bottom,
                                    )
                                    painter.restore()
                        else:
                            ruby_wipe_st = char_wipe_times.get(char_pos)
                            ruby_st = ruby_wipe_st[0] if ruby_wipe_st else None
                            if ruby_st is not None:
                                ruby_wipe_et = char_wipe_times.get(char_pos)
                                ruby_et = ruby_wipe_et[1] if ruby_wipe_et else ruby_st + 300
                                if current_time >= ruby_et:
                                    _draw_split_text(
                                        painter, int(ruby_x), ruby_y, _ruby_disp,
                                        ruby_highlight_colors, _ruby_ink_top, _ruby_ink_bottom,
                                    )
                                elif current_time >= ruby_st:
                                    r_dur = ruby_et - ruby_st
                                    r_ratio = (
                                        min(1.0, (current_time - ruby_st) / r_dur)
                                        if r_dur > 0
                                        else 1.0
                                    )
                                    if r_ratio > 0 and _r_ink_w > 0:
                                        painter.save()
                                        r_wipe_w = int(_r_ink_w * r_ratio)
                                        painter.setClipRect(
                                            QRect(
                                                int(_r_ink_x),
                                                int(ruby_y + _r_ink_top) - 2,
                                                r_wipe_w,
                                                int(_r_ink_bottom - _r_ink_top) + 4,
                                            )
                                        )
                                        _draw_split_text(
                                            painter, int(ruby_x), ruby_y, _ruby_disp,
                                            ruby_highlight_colors, _ruby_ink_top, _ruby_ink_bottom,
                                        )
                                        painter.restore()

                # 主文字 — 基于 checkpoint 的逐字 wipe
                # IPA 段字符换用 IPA 字体（CJK 主字体对 IPA 的跨字体回退无法
                # 锚定附加符）；度量同步切换。仅当本行确实启用了独立 IPA
                # 字体时才换字体——墨水矢量判定（char_uses_ipa）与字体切换
                # 解耦，避免主字体==IPA 字体时绘制/度量口径不一致。
                _char_is_ipa = bool(
                    _char_uses_ipa
                    and _char_uses_ipa[char_pos]
                    and _rd.get("ipa_styling", False)
                )
                if _char_is_ipa:
                    _main_font_here = (
                        font_current_ipa if is_current else font_context_ipa
                    )
                    _main_fm_here = fm_current_ipa if is_current else fm_context_ipa
                else:
                    _main_font_here = main_font
                    _main_fm_here = main_fm
                painter.setFont(_main_font_here)
                # 使用 per-char singer 颜色（List[QColor]，支持分色）
                char_colors = _char_singer_colors.get(char_pos, [highlight_color])

                # 字符在 char_w 宽度内居中（与 ruby 对齐）。多码点/矢量簇
                # 的轮廓可能比 advance 宽（如 keycap 1️⃣）：此时按墨水左缘
                # 对齐格子，避免居中后墨迹越过下一字符。
                _is_vector_char = bool(
                    _char_uses_ipa and _char_uses_ipa[char_pos]
                )
                char_text_w = _main_fm_here.horizontalAdvance(ch)
                if (
                    _is_vector_char
                    and _char_ink_widths[char_pos] > char_text_w
                ):
                    char_draw_x = curr_x - min(
                        0.0, _char_ink_offsets[char_pos]
                    )
                else:
                    char_draw_x = curr_x + (char_w - char_text_w) // 2

                # 走字预览指引：命中时把"未走字部分的底色"换成走字后分色 × 不透明度
                # （上一个=0.8 / 正在=0.5 / 下一个=0.2）。底色之上 wipe 照常叠加——
                # 能 wipe 的字走字后色会盖住底色自然走字，打完检查时一切正常，无需拦截。
                _g_alpha = guide_alpha.get(char_pos)

                if char_pos in char_wipe_times:
                    char_time, next_time = char_wipe_times[char_pos]

                    # 决定 wipe ratio 来源：
                    # 1) 合并段锚点组（多cp leader + 后随 cc=0）→ 整段 ink 合并走轴，
                    #    各锚点段按 ruby 部件宽度推进，映射回该字符 ink 区间
                    # 2) 字符 part 锚点（check_count>=2 且打过轴）→ 该字符 ratio
                    # 3) 否则 → 整字线性（char_wipe_times 已按像素宽度加权分配）
                    # 注：连词组（linked_to_next）仅影响视觉层（ruby 合并绘制），
                    #     不改变 wipe 时间分配。
                    _seg_leader = _seg_member_to_leader.get(char_pos)
                    if _seg_leader is not None:
                        _seg = _seg_anchor_groups[_seg_leader]
                        _seg_ratio = _anchor_part_ratio(
                            _seg["anchors"], current_time, _seg["part_ws"]
                        )
                        _wiped_ink = _seg["total_ink"] * _seg_ratio
                        _local_ink = _wiped_ink - _seg["ink_offsets"][char_pos]
                        _my_ink = _char_ink_widths[char_pos]
                        if _local_ink <= 0:
                            wipe_ratio = 0.0
                        elif _my_ink <= 0 or _local_ink >= _my_ink:
                            wipe_ratio = 1.0
                        else:
                            wipe_ratio = _local_ink / _my_ink
                    elif char_pos in _char_part_anchors:
                        wipe_ratio = _anchor_ratio(
                            _char_part_anchors[char_pos], current_time
                        )
                    else:
                        if current_time >= next_time and next_time > char_time:
                            wipe_ratio = 1.0
                        elif current_time >= char_time:
                            duration = next_time - char_time
                            wipe_ratio = (
                                min(1.0, (current_time - char_time) / duration)
                                if duration > 0
                                else 0.0
                            )
                        else:
                            wipe_ratio = 0.0

                    if wipe_ratio >= 1.0:
                        # 已唱完 → 全高亮（支持分色）
                        _draw_split_text(
                            painter, int(char_draw_x), int(y_center),
                            ch, char_colors, _line_ink_top, _line_ink_bottom,
                        )
                    elif wipe_ratio > 0.0:
                        # 正在唱 → wipe 渐变。底色优先用指引过渡色（命中时）。
                        if _g_alpha is not None:
                            _draw_split_text(
                                painter, int(char_draw_x), int(y_center),
                                ch, _guide_colors(char_colors, _g_alpha),
                                _line_ink_top, _line_ink_bottom,
                            )
                        else:
                            painter.setPen(base_color)
                            painter.drawText(int(char_draw_x), int(y_center), ch)

                        # 按字形墨水（ink）边界裁剪，而非 advance box：
                        # - 起点 = char_draw_x + ink_left（字形真正起墨像素列）
                        # - 终点 = 起点 + ink_width × ratio（字形墨水终止像素列）
                        # 这样 wipe 不会扫过字符左右两侧的透明侧 bearings；
                        # 停顿点扩展区（_esw）只用于放置 marker，不再参与 wipe。
                        ink_w = _char_ink_widths[char_pos]
                        if ink_w > 0:
                            ink_off = _char_ink_offsets[char_pos]
                            painter.save()
                            wipe_w = int(ink_w * wipe_ratio)
                            # 垂直裁剪用行墨水包络（叠加附加符可能超出字体盒）
                            clip_rect = QRect(
                                int(char_draw_x + ink_off),
                                _line_ink_top - 5,
                                wipe_w,
                                (_line_ink_bottom - _line_ink_top) + 10,
                            )
                            painter.setClipRect(clip_rect)
                            # _draw_split_text 内部用 IntersectClip，与 wipe clip 正确叠加
                            _draw_split_text(
                                painter, int(char_draw_x), int(y_center),
                                ch, char_colors, _line_ink_top, _line_ink_bottom,
                            )
                            painter.restore()
                        # ink_w == 0（半角/全角空格/NBSP/Tab 等零宽空白）：
                        # 没有可见墨水，跳过 clip 绘制，wipe 期间保持 base_color 即可。
                    else:
                        # 未唱 → 基色（命中指引时改用过渡色）
                        if _g_alpha is not None:
                            _draw_split_text(
                                painter, int(char_draw_x), int(y_center),
                                ch, _guide_colors(char_colors, _g_alpha),
                                _line_ink_top, _line_ink_bottom,
                            )
                        else:
                            painter.setPen(base_color)
                            painter.drawText(int(char_draw_x), int(y_center), ch)
                else:
                    # 不在任何字符组内 → 基色（命中指引时改用过渡色）
                    if _g_alpha is not None:
                        _draw_split_text(
                            painter, int(char_draw_x), int(y_center),
                            ch, _guide_colors(char_colors, _g_alpha),
                            _line_ink_top, _line_ink_bottom,
                        )
                    else:
                        painter.setPen(base_color)
                        painter.drawText(int(char_draw_x), int(y_center), ch)

                # 导唱待办标记：在字符本体左上角叠加半透明红色 ✚
                # 不参与 wipe / 不参与布局，仅做视觉提示；红色取主题 accent_warning + alpha=128
                _ng_ch = line.characters[char_pos]
                if _ng_ch.needs_guide:
                    painter.save()
                    painter.setFont(self._font_needs_guide)
                    _ng_color = QColor(theme.accent_warning)
                    _ng_color.setAlpha(160)
                    painter.setPen(_ng_color)
                    # 锚点：字符 ink 区左上角附近。横向贴 char_draw_x；纵向贴 main_fm.ascent() 顶部。
                    # baseline 算法：top = y_center - main_fm.ascent()，符号 baseline = top + ascent。
                    _ng_top = int(y_center) - main_fm.ascent()
                    _ng_baseline = _ng_top + self._fm_needs_guide.ascent()
                    # 轻微向左偏一点 advance 的一半，让符号坐在字符左上角而非完全压在字符里
                    _ng_w = self._fm_needs_guide.horizontalAdvance(self._needs_guide_symbol)
                    _ng_x = int(char_draw_x) - _ng_w // 3
                    painter.drawText(_ng_x, _ng_baseline, self._needs_guide_symbol)
                    painter.restore()

                # 当前打轴位置指示线：锚定到整行实际 ink 底部（_line_ink_bottom），
                # 与字符的 fm.descent() 解耦——所有字符共用同一 y，
                # 避免短字符（无 descender）下方留大块空白。
                # y 偏移随主字号 descent 比例缩放，但与 marker 视觉顶端保持至少
                # 2 px 间距（cp_spacing - 2），避免大字号下 AA 让指示线色彩
                # 渗入 marker 顶端 stroke（典型现象：▯/▮ 顶端横划被指示线高亮色淹没）。
                if is_current and char_pos == self._current_char_idx:
                    _esw = _end_sentence_w.get(char_pos, 0)
                    _ind_off_natural = max(2, main_fm.descent() // 3)
                    _ind_off = min(_ind_off_natural, max(2, self._cp_spacing - 2))
                    _ind_y = int(_line_ink_bottom + _ind_off)
                    painter.setPen(highlight_color)
                    painter.drawLine(
                        int(curr_x),
                        _ind_y,
                        int(curr_x + char_w + _esw),
                        _ind_y,
                    )

                # Checkpoint 标记（逐 checkpoint 绘制）
                # 停顿点marker独立于普通marker，绘制在字符右侧扩展区域
                ch_obj = line.characters[char_pos]
                if ch_obj.total_timing_points > 0:
                    painter.setFont(font_checkpoint)

                    # 普通markers（不含停顿点marker）
                    regular_markers = []
                    for cp_idx in range(ch_obj.check_count):
                        has_timed = cp_idx < len(ch_obj.global_timestamps)
                        if cp_idx == 0:
                            marker_char = self._checkpoint_markers["cp_first_timed"] if has_timed else self._checkpoint_markers["cp_first_empty"]
                        else:
                            marker_char = self._checkpoint_markers["cp_multi_timed"] if has_timed else self._checkpoint_markers["cp_multi_empty"]
                        regular_markers.append((cp_idx, marker_char, has_timed))

                    # 左对齐排列普通marker（在原始字符宽度内）
                    # marker 视觉顶端对齐到 (整行 ink 底部 + cp_spacing)：
                    #   - 锚点 = _line_ink_bottom（该行所有字符的实际墨水最低 y）
                    #   - cp_spacing=0 时 marker 顶端紧贴主文字 ink 底部
                    #   - 用 marker 字形矢量 boundingRect.top() 反推 baseline（_marker_glyph_top），
                    #     消除 fm.ascent 与字形顶之间的"空白冠"
                    mx = curr_x
                    marker_visible_top = _line_ink_bottom + self._cp_spacing
                    marker_y = int(marker_visible_top - self._marker_glyph_top)

                    for cp_idx, marker_char, has_timed in regular_markers:
                        is_selected = (
                            ch_obj.selected_checkpoint_idx == cp_idx
                        )
                        if is_selected:
                            color = _char_singer_colors.get(char_pos, [highlight_color])[0]
                        elif not has_timed:
                            color = theme.karaoke_text_current
                        else:
                            color = base_color

                        mw = fm_checkpoint.horizontalAdvance(marker_char)

                        # 矢量轮廓填充（非 drawText 点阵）——保证字形完整，不被点阵 strike 切顶
                        painter.fillPath(
                            self._marker_path(marker_char, float(int(mx)), float(marker_y)),
                            color,
                        )

                        # hitbox 从可视顶端起算，避免上沿越过主文字 ink 区与字符 hitbox 重叠
                        marker_rect = QRect(
                            int(mx),
                            int(marker_visible_top),
                            int(mw),
                            fm_checkpoint.height(),
                        )
                        self._checkpoint_hitboxes.append(
                            (marker_rect, idx, char_pos, cp_idx)
                        )

                        mx += mw

                    # 停顿点marker：独立绘制在字符右侧扩展区域
                    if ch_obj.is_sentence_end:
                        se_cp_idx = ch_obj.check_count
                        has_timed = ch_obj.global_sentence_end_ts is not None
                        marker_char = self._checkpoint_markers["cp_sentence_end_timed"] if has_timed else self._checkpoint_markers["cp_sentence_end_empty"]

                        is_selected = (
                            ch_obj.selected_checkpoint_idx == se_cp_idx
                        )
                        if is_selected:
                            color = _char_singer_colors.get(char_pos, [highlight_color])[0]
                        elif not has_timed:
                            color = theme.karaoke_text_current
                        else:
                            color = base_color

                        # 扩展区域：字符右侧，宽度为半字符宽
                        se_area_x = curr_x + char_w
                        se_area_w = _end_sentence_w.get(char_pos, 0)

                        # 矢量轮廓填充（同上，保证停顿点 marker 完整）
                        painter.fillPath(
                            self._marker_path(marker_char, float(int(se_area_x)), float(marker_y)),
                            color,
                        )

                        # hitbox覆盖整个扩展区域（高度与字符区域一致）
                        se_rect = QRect(
                            int(se_area_x),
                            _rect_top,
                            int(se_area_w),
                            _rect_height,
                        )
                        self._checkpoint_hitboxes.append(
                            (se_rect, idx, char_pos, se_cp_idx)
                        )

                curr_x += char_w + _end_sentence_w.get(char_pos, 0)
