"""时间轴控件。

显示音频波形、当前播放位置、打轴节奏点分布。
支持缩放和横向滚动，类似视频剪辑软件的时间线。
"""

from __future__ import annotations

import bisect
import math
from collections import OrderedDict
from typing import Callable, List, NamedTuple, Optional, Tuple

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSize, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QWheelEvent,
    QPainter,
    QPaintEvent,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygon,
    QPolygonF,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import CaptionLabel, FluentIcon, Slider, SwitchButton, TransparentToolButton

from strange_uta_game.backend.infrastructure.audio import spectrum as spectrum_core
from strange_uta_game.frontend.editor.timing.inline_edit_label import (
    InlineEditableCaptionLabel,
)
from strange_uta_game.frontend.perf_log import log_slow_method
from strange_uta_game.frontend.theme import theme
from strange_uta_game.frontend.workers import SpectrogramWorker


# (line_idx, char_idx, cp_idx, is_sentence_end) —— 可反查模型到具体 checkpoint 的句柄。
# 选中态、命中测试、拖拽提交都以此为身份，跨 set_time_tags 重排存活。
TagHandle = Tuple[int, int, int, bool]

# 波形/声谱高级设置与底部拖拽手柄共用同一组高度边界。
# 波形 lane 的下限单独放宽到 60px（纯描线绘制的波形 lane 更矮也可读）；
# 声谱 lane 保住 120px——热图 + 频率刻度再矮就糊了。
_MIN_DISPLAY_HEIGHT = 120
_MAX_DISPLAY_HEIGHT = 400
_WAVEFORM_MIN_DISPLAY_HEIGHT = 60

# 声谱模式左侧频率轴 gutter 宽度（px）：容纳强度色卡和频率刻度；
# tag/热图/播放头从轴区右侧开始，两者物理分离互不覆盖。
_SPECTRUM_AXIS_W = 50



def _has_multicodepoint_cluster(text: str) -> bool:
    """文本是否含多码点字素簇（基字+附加符 / emoji 序列）。

    迭代 str 只得到单码点串（``len(c) > 1`` 恒 False 的死条件），必须按簇比较。
    """
    from strange_uta_game.backend.domain.graphemes import grapheme_count

    return grapheme_count(text) != len(text)


class TimeTag(NamedTuple):
    """波形上一个时间标签的渲染 + 命中数据。

    ``label`` 仅在该字符第一个 checkpoint 时非空（跨 checkpoint 去重）；``ruby`` 为该
    checkpoint 对应注音文本。``handle`` 用于命中后定位模型 checkpoint。
    """

    ts: int
    label: Optional[str]
    ruby: Optional[str]
    handle: TagHandle


# ──────────────────────────────────────────────
# 波形显示区域
# ──────────────────────────────────────────────

class WaveformDisplay(QWidget):
    """波形显示区域 - 绘制音频波形 + 时间网格 + 标签 + 播放头"""

    seek_requested = pyqtSignal(int)
    scroll_position_changed = pyqtSignal(float)
    zoom_changed = pyqtSignal(float)
    # 单击时间标签把手：(line_idx, char_idx, cp_idx, is_sentence_end)
    tag_clicked = pyqtSignal(int, int, int, bool)
    # 拖拽提交：(handles: List[TagHandle], delta_ms: int)
    tags_drag_committed = pyqtSignal(object, int)
    # 双谱交界拖拽（改两 lane 比例，不动总高）：TimelineWidget 走与底边
    # 手柄相同的「拖动中重排、松手才持久化」链路
    lane_resize_started = pyqtSignal()
    lane_resize_moved = pyqtSignal(int)
    lane_resize_finished = pyqtSignal(int)

    # ── 把手几何与命中常量 ──
    _HANDLE_HALF_W = 5          # 未选中把手命中/绘制半宽（px）
    _HANDLE_SEL_HALF_W = 7      # 选中把手放大后半宽（px）
    _HANDLE_HEIGHT = 9          # 把手块高度（px）
    _MIN_HANDLE_SPACING = 8     # 相邻把手最小间距，低于此值密度门控不绘制把手/不可命中
    _HANDLE_HIT_Y_BAND = 12     # 命中仅在把手中心 ±该像素范围内有效
    # 行首/行尾（每行 ts 最小/最大的时间标签，可能是普通点也可能是句尾停顿点）
    # 的强调只作用于竖线：加粗 + 加长 + 背景色描边衬边；把手不变
    _EDGE_LINE_WIDTH_BONUS = 1  # 行首/行尾竖线相对普通竖线的额外加粗（px）
    _EDGE_LINE_SPAN_EXT = 0.14  # 行首/行尾竖线向上下各延长的 lane 高度比例
    _EDGE_LINE_SPAN_CAP = 0.96  # 延长后的下界上限（避开声谱 lane 底部内容）
    # 行首/行尾标签文字的右移量（px）：竖线加粗后含 halo 半宽约 3.5px 且上端
    # 延进标签轨道，文字底板（左缘在 a-2）右移让开，保证竖线完整可见
    _EDGE_LABEL_DX = 6
    # 双谱交界命中半宽（px）：刻意窄于把手命中带——把手在各 lane 中心
    # ±12px，交界 ±3px 不会侵占 tag 的可交互区
    _LANE_HIT_TOLERANCE = 3

    def _handle_center_y(self, h: int) -> float:
        """tag 把手竖直中心：恒在显示区中央（波形与任意高度的声谱一致），
        靠近 tag 竖线中部，方便点击。双谱模式按 lane 各自居中（见
        _draw_time_tags / _tag_lanes），不走本方法。"""
        return h / 2.0

    def _tag_lanes(self, h: int) -> List[tuple]:
        """tag 渲染的 lane 列表：(top, lane_h, is_spectrum)。

        波形/声谱单模式 = 单 lane 全高；双谱 = 上波形 lane + 下声谱 lane，
        两条 lane 读**同一份** `_time_tags` / `_warning_time_tags`，竖线与
        把手各绘一份——数据单源、拖拽提交走同一模型链路，双谱两条谱上的
        tag 标记天然保持同步。
        """
        if self._display_mode == "dual":
            wave_h, spec_h = self._dual_lane_heights(h)
            return [(0, wave_h, False), (wave_h, spec_h, True)]
        return [(0, h, self._display_mode == "spectrum")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._duration_ms = 0
        self._current_ms = 0
        self._range_start_ms: Optional[int] = None
        self._range_end_ms: Optional[int] = None
        # 时间标签列表（含模型句柄）；label 仅在该字符第一个 checkpoint 时非空
        self._time_tags: List[TimeTag] = []
        self._warning_time_tags: List[TimeTag] = []
        # 增量更新状态（B：顺序打轴快路径）。_last_tags_input 缓存上次 collect 原始
        # 列表，用于前缀比对识别"末尾追加一个"；其余为增量分类所需的累积量。
        self._last_tags_input: Optional[List[tuple]] = None
        self._running_max_ts: int = -1
        self._seen_char_keys: set = set()
        # 文件序最大键 (line, char, cp)，用于 try_append_tag 判定"末尾追加"
        self._max_file_order_key: Optional[Tuple[int, int, int]] = None
        # Raw entries keyed by stable handle. This lets an interior checkpoint be
        # inserted without collecting every timestamp from the project again.
        self._entries_by_handle: dict[TagHandle, tuple] = {}
        # 每行首/尾时间标签（该行 ts 最小/最大者，跨 normal/warning 合并判定）
        # 的句柄集合，用于行界强调绘制；_line_bounds 为其增量维护所需的
        # line_idx -> (min_ts, min_handle, max_ts, max_handle)。
        self._line_edge_handles: set = set()
        self._line_bounds: dict = {}

        # 音频数据
        self._samples: Optional[np.ndarray] = None
        self._waveform_samples: Optional[np.ndarray] = None
        self._sample_rate: int = 44100
        self._channels: int = 2

        # 缩放和滚动
        self._zoom_factor: float = 50.0  # 默认50x缩放，减少初始渲染压力
        self._zoom_enabled: bool = True
        self._scroll_position: float = 0.0
        self._center_playhead_mode: bool = False
        self._is_playing: bool = False

        # 波形峰值缓存
        self._peaks_cache: Optional[List[tuple]] = None
        self._peaks_cache_key: Optional[tuple] = None  # (width, zoom, scroll, samples_id)
        # 逐采样视图缓存（spp<1 区间）：(values, anchor_x, anchor_v)
        self._sample_view_cache: Optional[tuple] = None
        self._sample_view_cache_key: Optional[tuple] = None
        # 整段音频的多分辨率峰值缓存：bin_size -> (mins, maxs, rmss)。同一缩放
        # 粒度只归约一次，播放滚动时仅从缓存中取当前窗口。LRU + 内存预算：
        # 长音频多缩放层不设限会累积数百 MB。
        self._waveform_peak_levels: "OrderedDict[int, tuple]" = OrderedDict()
        # 在途峰值预热任务的取消闭包；换歌/清除音频时先取消再启动新的，
        # worker 正常完成/报错/取消后释放为 None（迟到信号只做身份过滤）。
        self._preheat_cancel: Optional[Callable[[], None]] = None

        # 显示模式与高级设置（齿轮对话框 / timing.* 设置键）
        # "waveform" | "spectrum" 互斥；"dual" = 双谱模式（波形图 + 声谱图
        # 上下并排在同一时间轴，两条 lane 共用同一份 tag 数据，天然同步）
        self._display_mode = "waveform"
        self._grid_mode = "time"         # "time" | "bpm"
        self._grid_bpm = 120.0
        # BPM 网格偏移（毫秒）：拍线相位对齐——歌曲节拍通常不从 0ms 开始，
        # 正值网格整体后移（延迟）、负值前移。仅作用于 BPM 网格。
        self._grid_offset_ms = 0
        # 拍号分子（每小节拍数，1~16）：BPM 网格小节线/小节号与节拍器重音
        # 共用的循环周期（默认 4/4）
        self._beats_per_bar = 4
        self._grid_line_width = 2        # 网格线宽（0~100px，时间/BPM 共用；0=不绘制）
        self._spectrum_fft_size = 8192
        self._spectrum_overlap = 0.75  # 窗口重叠（SV 口径）；帧距=fft·(1-overlap)
        self._spectrum_freq_scale = "log"  # "log" | "linear"
        self._spectrum_dyn_range_db = 60
        self._spectrum_colormap = "inferno"  # 项目原有色带；渲染期参数
        # 显示期频率钳制（Hz，0 = 自动/全谱）：只影响行映射与频率轴，
        # 不触发声谱重算；f_min ≥ f_max 时按全范围渲染（resolve_freq_range）
        self._spectrum_freq_min_hz = 0
        self._spectrum_freq_max_hz = 0
        # 显示高度按模式独立保存（120~400px）；旧版公共 display_height
        # 由加载/设置兼容层同时投影到两者。
        self._waveform_display_height = 120
        self._spectrum_display_height = 120
        # 声谱活动门禁：False 时任何路径（换音频/改参数/切模式）都不得重启计算
        self._spectrum_active = True
        # 双层波形（外层 min/max 峰值 + 内层 RMS 核心带）绘制开关，默认开
        self._waveform_rms_enabled = True
        # 节拍器（纯状态，不参与绘制）：播放期间按 BPM/偏移触发节拍音，
        # 由 EditorInterface 的 PlaybackMetronome 消费（经设置链透传/持久化）
        self._metronome_enabled = False
        self._metronome_volume = 100
        # 加载后自动检测 BPM 开关（纯状态）：开启时音频加载完成自动跑一次
        # 检测并回填 BPM；实际调度在 EditorInterface（经设置链透传/持久化）
        self._auto_bpm_enabled = False
        # 实际使用的重叠率（预算降级后可能与用户选择不同；None=未计算）
        self._actual_overlap: Optional[float] = None

        # 声谱缓存与后台计算状态。线程由 task_runner 注册表持有（不 parent
        # 到本控件），_spectrum_worker 仅作身份比对——迟到信号按 sender 丢弃。
        self._spectrum: Optional[dict] = None
        self._spectrum_state = "idle"    # idle | computing | ready | error
        self._spectrum_progress_pct = -1
        self._spectrum_error = ""
        self._spectrum_worker: Optional[SpectrogramWorker] = None
        self._spectrum_view_cache: Optional[np.ndarray] = None
        self._spectrum_view_cache_key: Optional[tuple] = None
        # 静态层重建用的最终 RGBA QImage（与 view 缓存同键缓存，避免逐帧分配；
        # QImage 不持有 buffer 引用，bytes 必须随缓存一并保活）
        self._spectrum_image_cache: Optional[QImage] = None
        self._spectrum_image_cache_buffer: Optional[bytes] = None
        self._spectrum_image_cache_key: Optional[tuple] = None
        self._spectrum_lut_cache: Optional[np.ndarray] = None
        self._spectrum_lut_cache_key: Optional[tuple] = None

        # 网格、波形、范围和标签在普通播放期间不变。缓存为静态图层后，
        # 逐帧只需绘制该图层和播放头；缩放/滚动/数据变化时再失效重建。
        self._static_layer: Optional[QPixmap] = None
        self._static_layer_key: Optional[tuple] = None

        # 左键拖动平移状态
        self._pan_start_x: Optional[float] = None
        self._pan_start_scroll: float = 0.0
        self._is_panning: bool = False

        # 时间标签拖拽编辑（总开关 + 选中态 + 拖拽状态）
        self._tag_edit_enabled: bool = True
        # 是否在时间标签上显示本体字符 / 注音文本（两个独立开关）
        self._tag_char_enabled: bool = True
        self._tag_ruby_enabled: bool = True
        self._selected_handles: set = set()           # set[TagHandle]
        self._handle_index: dict = {}                 # TagHandle -> TimeTag，set_time_tags 时重建
        self._hit_boxes: List[Tuple[int, TagHandle, int]] = []  # (x_px, handle, ts)，每次绘制重建
        # 把手按下/拖拽运行态
        self._press_handle: Optional[TagHandle] = None
        self._press_handle_ts: int = 0
        self._press_x: float = 0.0
        self._drag_armed: bool = False                # 按下的是已选中把手 → 允许拖拽
        self._is_dragging_tags: bool = False
        self._drag_anchor_handle: Optional[TagHandle] = None
        self._drag_anchor_ts: int = 0
        self._drag_delta_ms: int = 0

        # 双谱交界拖拽运行态（悬停高亮 + 按下拖拽）
        self._lane_boundary_hovered: bool = False
        self._lane_dragging: bool = False
        self._lane_press_global_y: Optional[int] = None

        # 自动滚动挂起（用户手动操作后 6s 内不跟随播放头）
        self._auto_scroll_suspended: bool = False
        self._suspension_timer = QTimer(self)
        self._suspension_timer.setSingleShot(True)
        self._suspension_timer.setInterval(6000)
        self._suspension_timer.timeout.connect(self._resume_auto_scroll)

        # 初始波形模式使用波形自己的期望高度。
        self._apply_display_height()
        self.setMouseTracking(True)

        # 监听主题变化，触发重绘
        theme.changed.connect(self._on_theme_changed)

    def _invalidate_static_layer(self) -> None:
        self._static_layer = None
        self._static_layer_key = None

    def _on_theme_changed(self) -> None:
        self._invalidate_static_layer()
        self.update()

    def resizeEvent(self, event: Optional[QResizeEvent]) -> None:
        self._invalidate_static_layer()
        super().resizeEvent(event)

    def _max_scroll(self) -> float:
        """当前缩放下允许的最大滚动位置，保证视窗末尾不超出音频范围。"""
        if self._zoom_factor <= 1.0:
            return 0.0
        return max(0.0, 1.0 - 1.0 / self._zoom_factor)

    def _clamp_scroll(self, position: float) -> float:
        return max(0.0, min(self._max_scroll(), position))

    def set_duration(self, ms: int):
        self._duration_ms = ms
        # 时长变化后重新 clamp，避免旧滚动位置超出新范围
        self._scroll_position = self._clamp_scroll(self._scroll_position)
        # 换音频/时长变化后拖拽状态必须清场，避免过期锚点残留
        self._reset_drag()
        self._lane_dragging = False
        self._lane_press_global_y = None
        self._pan_start_x = None
        self._is_panning = False
        # Keep this method usable by the lightweight non-QWidget test double.
        self._static_layer = None
        self._static_layer_key = None
        self.update()

    def set_playback_range(
        self, start_ms: Optional[int], end_ms: Optional[int]
    ) -> None:
        self._range_start_ms = start_ms
        self._range_end_ms = end_ms
        self._invalidate_static_layer()
        self.update()

    def _suspend_auto_scroll(self) -> None:
        """用户手动操作后挂起自动滚动，重置 6s 倒计时。"""
        self._auto_scroll_suspended = True
        self._suspension_timer.start()

    def _resume_auto_scroll(self) -> None:
        """恢复自动跟随播放头。"""
        self._auto_scroll_suspended = False
        self._suspension_timer.stop()

    def set_playing(self, playing: bool) -> None:
        """播放状态变化：重新开始播放时取消自动滚动挂起。"""
        was_playing = self._is_playing
        self._is_playing = bool(playing)
        if playing:
            self._resume_auto_scroll()
            if self._center_playhead_mode:
                self._sync_scroll_to_playhead()
        elif was_playing and self._center_playhead_mode:
            # 离开居中播放态时把虚拟视窗收回音频有效范围，暂停画面不跳回旧位置。
            self._sync_scroll_to_playhead()
        self.update()

    def set_center_playhead_mode(self, enabled: bool) -> None:
        """播放时将播放头锁在中央，由波形和时间标签从右向左滚动。"""
        enabled = bool(enabled)
        if enabled == self._center_playhead_mode:
            return
        self._center_playhead_mode = enabled
        if self._is_playing:
            self._resume_auto_scroll()
            self._sync_scroll_to_playhead()
        self.update()

    def _sync_scroll_to_playhead(self) -> None:
        if self._duration_ms <= 0:
            return
        half_window_ms = self._visible_duration_ms() / 2.0
        self._scroll_position = self._clamp_scroll(
            (self._current_ms - half_window_ms) / self._duration_ms
        )
        self.scroll_position_changed.emit(self._scroll_position)

    def set_position(self, ms: int):
        old_scroll = self._scroll_position
        self._current_ms = ms
        if self._center_playhead_mode and self._is_playing:
            self._sync_scroll_to_playhead()
        # 自动滚动保持播放头可见（用户手动操作后挂起）
        elif self._duration_ms > 0 and self._zoom_factor > 1.0 and not self._auto_scroll_suspended:
            visible_start = self._scroll_position * self._duration_ms
            visible_end = visible_start + self._duration_ms / self._zoom_factor
            if ms < visible_start or ms > visible_end:
                new_scroll = self._clamp_scroll(
                    (ms - self._duration_ms / (2 * self._zoom_factor)) / self._duration_ms
                )
                self._scroll_position = new_scroll
                self.scroll_position_changed.emit(self._scroll_position)
        if self._center_playhead_mode or self._scroll_position != old_scroll:
            self._invalidate_static_layer()
        self.update()

    @log_slow_method(
        "timeline.set_time_tags",
        12,
        lambda self, args, kwargs: {"tags": len(args[0]) if args else 0},
    )
    def set_time_tags(self, tags: List[Tuple[int, str, int, int, int, bool, Optional[str]]]):
        # tags: (timestamp_ms, char_text, line_idx, char_idx, cp_idx, is_sentence_end, ruby_text)
        # 同一字符 (line_idx, char_idx) 的第一个 checkpoint 携带 char 标签，后续不重复；ruby 始终携带

        # ── B 快路径：顺序打轴 = 上次列表 + 末尾恰好追加一个 → O(log N) 增量插入 ──
        prev = self._last_tags_input
        if prev is not None and len(tags) == len(prev) + 1 and tags[:-1] == prev:
            self._append_time_tag(tags[-1])
            self._last_tags_input = tags
            return

        # ── 全量重建（其余所有情况：改时间 / 删除 / 乱序补打 / 结构变更）──
        seen_chars: set = set()
        normal: List[TimeTag] = []
        warning: List[TimeTag] = []
        running_max = -1
        for ts, char, line_idx, char_idx, cp_idx, is_end, ruby_text in tags:
            char_key = (line_idx, char_idx)
            label: Optional[str] = char if char_key not in seen_chars else None
            seen_chars.add(char_key)
            handle: TagHandle = (line_idx, char_idx, cp_idx, is_end)
            item = TimeTag(ts, label, ruby_text, handle)
            if ts < running_max:
                warning.append(item)
            else:
                normal.append(item)
                running_max = ts
        self._time_tags = sorted(normal, key=lambda x: x.ts)
        self._warning_time_tags = sorted(warning, key=lambda x: x.ts)
        self._recompute_line_edge_handles()
        # 重建句柄索引（命中/选中/拖拽以句柄为身份）；丢弃已不存在的选中项
        self._handle_index = {
            t.handle: t for t in (self._time_tags + self._warning_time_tags)
        }
        if self._selected_handles:
            self._selected_handles &= set(self._handle_index)
        # 刷新增量状态（collect 为文件序，末项即文件序最大键）
        self._seen_char_keys = seen_chars
        self._running_max_ts = running_max
        self._last_tags_input = tags
        self._max_file_order_key = (tags[-1][2], tags[-1][3], tags[-1][4]) if tags else None
        self._entries_by_handle = {
            (entry[2], entry[3], entry[4], entry[5]): entry for entry in tags
        }
        WaveformDisplay._invalidate_static_layer(self)
        self.update()

    def _append_time_tag(self, entry: Tuple[int, str, int, int, int, bool, Optional[str]]) -> None:
        """顺序打轴增量插入单个新标签（位于文件序末尾，不影响已有标签归类）。"""
        ts, char, line_idx, char_idx, cp_idx, is_end, ruby_text = entry
        char_key = (line_idx, char_idx)
        label: Optional[str] = char if char_key not in self._seen_char_keys else None
        self._seen_char_keys.add(char_key)
        handle: TagHandle = (line_idx, char_idx, cp_idx, is_end)
        item = TimeTag(ts, label, ruby_text, handle)
        if ts < self._running_max_ts:
            bisect.insort(self._warning_time_tags, item, key=lambda x: x.ts)
        else:
            bisect.insort(self._time_tags, item, key=lambda x: x.ts)
            self._running_max_ts = ts
        self._handle_index[handle] = item
        self._entries_by_handle[handle] = entry
        self._max_file_order_key = (line_idx, char_idx, cp_idx)
        self._update_line_edges_on_append(line_idx, ts, handle)
        WaveformDisplay._invalidate_static_layer(self)
        self.update()

    def _recompute_line_edge_handles(self) -> None:
        """重算每行首/尾时间标签句柄集合（该行 ts 最小/最大者）。

        行首可能是普通 checkpoint，行尾在打了停顿点时是 is_sentence_end 标签——
        两类都可能成为行界，统一按 ts 判定。normal/warning 两列表各自按 ts
        升序，合并排序后顺序扫描即可得每行极值；同 ts 并列时取扫描靠后者
        （同 x 渲染，视觉无差）。
        """
        bounds: dict = {}
        for t in sorted(self._time_tags + self._warning_time_tags, key=lambda x: x.ts):
            b = bounds.get(t.handle[0])
            if b is None:
                bounds[t.handle[0]] = [t.ts, t.handle, t.ts, t.handle]
            elif t.ts >= b[2]:
                b[2], b[3] = t.ts, t.handle
        self._line_bounds = {k: tuple(v) for k, v in bounds.items()}
        self._line_edge_handles = {
            h for v in self._line_bounds.values() for h in (v[1], v[3])
        }

    def _update_line_edges_on_append(self, line_idx: int, ts: int,
                                     handle: TagHandle) -> None:
        """增量追加后的行界维护：新标签成为该行新的最小/最大 ts 时顶替旧句柄。

        句柄内嵌 line_idx，行间互不冲突，直接改写该行在集合中的两个句柄即可
        （单标签行的首尾为同一句柄，集合中仅一份）。
        """
        prev = self._line_bounds.get(line_idx)
        if prev is None:
            self._line_bounds[line_idx] = (ts, handle, ts, handle)
            self._line_edge_handles.add(handle)
            return
        min_ts, min_h, max_ts, max_h = prev
        if ts < min_ts or ts >= max_ts:
            if ts < min_ts:
                min_ts, min_h = ts, handle
            else:
                max_ts, max_h = ts, handle
            self._line_bounds[line_idx] = (min_ts, min_h, max_ts, max_h)
            self._line_edge_handles.difference_update((prev[1], prev[3]))
            self._line_edge_handles.update((min_h, max_h))

    def try_append_tag(self, ts: int, char: str, line_idx: int, char_idx: int,
                       cp_idx: int, is_end: bool, ruby: Optional[str]) -> bool:
        """顺序打轴增量追加单个标签（绕过前端 collect + 全量重建）。

        仅当该标签是**新增**且在**文件序上位于所有现有标签之后**时成功：此时它不会改变
        任何已有标签的单调/非单调归类，O(log N) 直接插入并重绘，返回 True。否则不改任何
        状态、返回 False，调用方回退到全量 set_time_tags。
        """
        handle: TagHandle = (line_idx, char_idx, cp_idx, is_end)
        if handle in self._handle_index:
            return False  # 已存在 = 改时间，非新增
        file_key = (line_idx, char_idx, cp_idx)
        if self._max_file_order_key is not None and file_key <= self._max_file_order_key:
            return False  # 非文件序末尾 → 可能改变他者归类，回退
        self._append_time_tag((ts, char, line_idx, char_idx, cp_idx, is_end, ruby))
        # 直接增量后让下次 set_time_tags 走全量重建（避免前缀比对基准失配）
        self._last_tags_input = None
        return True

    def try_add_tag(self, ts: int, char: str, line_idx: int, char_idx: int,
                    cp_idx: int, is_end: bool, ruby: Optional[str]) -> bool:
        """Add a newly timed checkpoint without rescanning the project.

        The common file-tail case keeps the O(log N) append path. Filling an
        earlier gap rebuilds classification from the widget's local raw-entry
        cache because it can change which later timestamps are warnings.
        """
        handle: TagHandle = (line_idx, char_idx, cp_idx, is_end)
        if handle in self._handle_index:
            return False
        if self.try_append_tag(ts, char, line_idx, char_idx, cp_idx, is_end, ruby):
            return True

        entry = (ts, char, line_idx, char_idx, cp_idx, is_end, ruby)
        cached = list(self._entries_by_handle.values())
        cached.append(entry)
        cached.sort(key=lambda item: (item[2], item[3], item[4], item[5]))
        self.set_time_tags(cached)
        return True

    def set_audio_data(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        mono: Optional[np.ndarray] = None,
    ):
        self._samples = samples
        # 波形滚动会逐帧重算可见峰值；立体声只在加载时混合一次，避免播放时
        # 每帧扫描整首音频。mono 是引擎加载线程预混的分析用单声道——直接
        # 引用，不在 UI 线程重复降混（10 分钟立体声实测 ≈200ms 会卡界面）。
        if mono is not None:
            self._waveform_samples = mono
        elif channels > 1:
            self._waveform_samples = np.mean(samples, axis=1, dtype=np.float32)
        else:
            self._waveform_samples = samples
        self._sample_rate = sample_rate
        self._channels = channels
        # 清除波形缓存
        self._peaks_cache = None
        self._peaks_cache_key = None
        self._sample_view_cache = None
        self._sample_view_cache_key = None
        self._waveform_peak_levels.clear()
        # 长音频换短音频时缩放上限收缩，旧 zoom 可能越界
        self._zoom_factor = min(self._zoom_factor, self.zoom_cap())
        self._scroll_position = self._clamp_scroll(self._scroll_position)
        # 声谱缓存随音频作废；声谱模式下立即重启后台计算
        self._reset_spectrum_cache()
        self._ensure_spectrum()
        # 后台预热峰值层（粗层优先）：首次滚轮/缩放不再在 UI 线程全曲扫描
        self._preheat_peak_levels()
        self._invalidate_static_layer()
        self.update()

    def clear_audio_data(self):
        self._samples = None
        self._waveform_samples = None
        self._sample_rate = 0
        self._channels = 0
        self._peaks_cache = None
        self._peaks_cache_key = None
        self._sample_view_cache = None
        self._sample_view_cache_key = None
        self._waveform_peak_levels.clear()
        self._cancel_peak_preheat()
        self._reset_spectrum_cache()
        # 清空音频后拖拽/pan 状态一并清场，避免过期锚点残留
        self._reset_drag()
        self._lane_dragging = False
        self._lane_press_global_y = None
        self._pan_start_x = None
        self._is_panning = False
        self._invalidate_static_layer()
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom_factor = max(1.0, min(self.zoom_cap(), zoom))
        # 缩放变化后重新 clamp，避免当前滚动位置超出新的有效范围
        self._scroll_position = self._clamp_scroll(self._scroll_position)
        self._invalidate_static_layer()
        self.update()

    def set_zoom_enabled(self, enabled: bool) -> None:
        self._zoom_enabled = enabled

    def set_scroll_position(self, position: float):
        self._scroll_position = self._clamp_scroll(position)
        self._invalidate_static_layer()
        self.update()

    # ── 时间标签拖拽编辑：总开关 + 选中态 ──

    def set_tag_edit_enabled(self, enabled: bool) -> None:
        """总开关：关闭时回退为旧的纯显示模式（不画把手/不可命中/无选中）。"""
        enabled = bool(enabled)
        if enabled == self._tag_edit_enabled:
            return
        self._tag_edit_enabled = enabled
        if not enabled:
            self._reset_drag()
            self._selected_handles.clear()
            self._press_handle = None
            self._drag_armed = False
            self.unsetCursor()
        self._invalidate_static_layer()
        self.update()

    def set_tag_char_enabled(self, enabled: bool) -> None:
        """是否在时间标签上显示本体字符文本（独立于拖拽编辑总开关）。"""
        enabled = bool(enabled)
        if enabled != self._tag_char_enabled:
            self._tag_char_enabled = enabled
            self._invalidate_static_layer()
            self.update()

    def set_tag_ruby_enabled(self, enabled: bool) -> None:
        """是否在时间标签上显示注音(ruby)文本（独立于拖拽编辑总开关）。"""
        enabled = bool(enabled)
        if enabled != self._tag_ruby_enabled:
            self._tag_ruby_enabled = enabled
            self._invalidate_static_layer()
            self.update()

    def clear_tag_selection(self) -> None:
        """清空选中集（外部数据变更后调用，避免悬空句柄）。"""
        if self._is_dragging_tags:
            self._reset_drag()
        if self._selected_handles:
            self._selected_handles.clear()
            self._invalidate_static_layer()
            self.update()

    def _reset_drag(self) -> None:
        self._is_dragging_tags = False
        self._drag_anchor_handle = None
        self._drag_anchor_ts = 0
        self._drag_delta_ms = 0
        self._press_handle = None
        self._drag_armed = False

    def event(self, e) -> bool:
        # 窗口失活（Alt+Tab 等）时统一清场：进行中的把手拖拽/pan/交界拖拽
        # 状态残留会让下次点击复用过期锚点提交错误偏移。
        if e.type() == QEvent.Type.WindowDeactivate:
            self._reset_drag()
            self._lane_dragging = False
            self._lane_press_global_y = None
            self._pan_start_x = None
            self._is_panning = False
            self.unsetCursor()
            self._lane_boundary_hovered = False
        return super().event(e)

    # ── 视窗 / 坐标换算 ──

    def _visible_start_ms(self) -> float:
        if self._center_playhead_mode and self._is_playing:
            # 不 clamp：开头/结尾保留半屏空白，播放头才能在整段播放期间严格居中。
            return self._current_ms - self._visible_duration_ms() / 2.0
        return self._scroll_position * self._duration_ms

    def _visible_duration_ms(self) -> float:
        return self._duration_ms / self._zoom_factor

    def _ts_to_x(self, ts: float, visible_start_ms: float, visible_duration_ms: float, w: int) -> int:
        if visible_duration_ms <= 0:
            return 0
        return int((ts - visible_start_ms) / visible_duration_ms * w)

    def _x_to_time(self, x: float, width: Optional[int] = None) -> int:
        """把波形横坐标换算为时间；居中播放时同样使用滚动中的虚拟视窗。

        传 width 时 x 视为绘图区坐标（测试约定）；不传时 x 是 widget 坐标，
        声谱模式下先扣除左侧频率轴 gutter。
        """
        if width is not None:
            w = width
        else:
            w = self._plot_width()
            x = x - self._spectrum_axis_width()
        if self._duration_ms <= 0 or w <= 0:
            return 0
        ratio = max(0.0, min(1.0, x / w))
        target_ms = self._visible_start_ms() + ratio * self._visible_duration_ms()
        return max(0, min(self._duration_ms, int(round(target_ms))))

    def _draw_ts(self, tag: "TimeTag") -> int:
        """拖拽预览：被选中且正在拖拽的标签按 delta 平移其显示时间戳。"""
        if self._is_dragging_tags and tag.handle in self._selected_handles:
            return tag.ts + self._drag_delta_ms
        return tag.ts

    def _visible_slice(self, tag_list: List["TimeTag"], vs: float, ve: float) -> List["TimeTag"]:
        """A：返回 [vs, ve] 可见窗内的标签子列表（列表按 ts 升序，二分截取）。

        拖拽态下选中标签会按 delta 偏移，可能移入/移出视窗，二分会漏，故回退全量
        （由 _draw_line 的 ts 范围判断兜底）；拖拽是短暂交互态，全量遍历可接受。
        """
        if self._is_dragging_tags:
            return tag_list
        lo = bisect.bisect_left(tag_list, vs, key=lambda t: t.ts)
        hi = bisect.bisect_right(tag_list, ve, key=lambda t: t.ts)
        return tag_list[lo:hi]

    def _hit_test_handle(self, x: float, y: float):
        """命中把手块：返回 (handle, ts, x_px) 或 None。仅在编辑开启时有效。

        x 为 widget 坐标，声谱/双谱模式下先扣除频率轴 gutter；y 与各把手
        所属 lane 的中心（_hit_boxes 里记录的 cy）比对——双谱模式下上下
        两条谱上的把手都可命中，任一条上的拖拽都作用于同一份模型数据。
        """
        if not self._tag_edit_enabled:
            return None
        x = x - self._spectrum_axis_width()
        best = None
        best_dx = self._HANDLE_HALF_W + 1
        for hx, handle, ts, cy in self._hit_boxes:
            if abs(y - cy) > self._HANDLE_HIT_Y_BAND:
                continue
            dx = abs(x - hx)
            if dx <= self._HANDLE_HALF_W and dx < best_dx:
                best = (handle, ts, hx)
                best_dx = dx
        return best

    def _clamp_drag_delta(self, delta: int) -> int:
        """组级 0 夹紧：保证所有选中标签的显示时间戳平移后不小于 0（保持刚性）。"""
        if not self._selected_handles:
            return delta
        sel_ts = [
            self._handle_index[h].ts
            for h in self._selected_handles
            if h in self._handle_index
        ]
        if not sel_ts:
            return delta
        return max(delta, -min(sel_ts))

    @log_slow_method(
        "timeline.compute_waveform_peaks",
        20,
        lambda self, args, kwargs: {
            "width": args[0] if args else kwargs.get("width"),
            "zoom": f"{self._zoom_factor:.2f}",
        },
    )
    def _compute_waveform_peaks(self, width: int) -> Optional[List[tuple]]:
        """从整段峰值层中截取当前窗口；每种采样粒度只计算一次。

        SV 式口径：每像素 min/max 峰值（外层轮廓）+ RMS 均方根（内层核心），
        分辨率由缩放决定——粗览 bin 宽、深放大 bin 细，无独立粒度设置。
        """
        if self._samples is None or self._duration_ms <= 0 or width <= 0:
            return None

        # 居中播放时 visible_start 随播放位置逐帧变化，必须进入缓存键。
        samples_id = id(self._samples)
        visible_start_ms = self._visible_start_ms()
        cache_key = (width, self._zoom_factor, visible_start_ms, samples_id)

        if self._peaks_cache_key == cache_key and self._peaks_cache is not None:
            return self._peaks_cache

        samples = self._waveform_samples
        if samples is None:
            return None

        visible_duration_ms = self._visible_duration_ms()
        samples_per_pixel = max(
            1.0, visible_duration_ms / 1000.0 * self._sample_rate / width
        )
        # 量化到 2 的幂，缩放/resize 时可复用少量固定层级；选不大于目标
        # 粒度的层，确保缓存不会吃掉瞬态峰值。
        bin_size = 1 << max(0, int(math.floor(math.log2(samples_per_pixel))))

        # 直读路径（SV getSummaries 的 direct-read 分支）：目标粒度深于最细
        # 缓存层（长音频的峰值层受内存预算所限建不了那么细）而可见采样量
        # 有界时，直接对原始单声道逐像素归约——比任何缓存层都准（无 bin
        # 量化涂抹），成本 O(可见采样)，≈0.3ms @128K 采样。
        n_visible = visible_duration_ms / 1000.0 * self._sample_rate
        finest_cached = min(self._waveform_peak_levels.keys(), default=None)
        if (
            finest_cached is None or bin_size < finest_cached
        ) and n_visible <= self._DIRECT_READ_MAX_SAMPLES:
            ms_per_pixel = visible_duration_ms / width
            left_times = (
                visible_start_ms + np.arange(width, dtype=np.float64) * ms_per_pixel
            )
            right_times = left_times + ms_per_pixel
            # 像素段边界 = 各像素左端时间的采样索引（floor，与金字塔
            # _level_indices 同口径）：段间无缝无重叠，瞬态不跨像素丢失
            edges = np.floor(
                left_times / 1000.0 * self._sample_rate
            ).astype(np.int64)
            edges = np.append(
                edges,
                int(
                    np.clip(
                        np.ceil(right_times[-1] / 1000.0 * self._sample_rate),
                        0,
                        len(samples),
                    )
                ),
            )
            mins, maxs, rmss = spectrum_core.reduce_peaks_by_edges(samples, edges)
            valid = (right_times > 0.0) & (left_times < self._duration_ms)
            mins = np.where(valid, mins, 0.0)
            maxs = np.where(valid, maxs, 0.0)
            rmss = np.where(valid, rmss, 0.0)
            peaks = [
                (float(lo), float(hi), float(rm))
                for lo, hi, rm in zip(mins, maxs, rmss)
            ]
            self._peaks_cache = peaks
            self._peaks_cache_key = cache_key
            return peaks

        cached = self._waveform_peak_levels.get(bin_size)
        if cached is not None:
            self._waveform_peak_levels.move_to_end(bin_size)
            level_min, level_max, level_rms = cached
        else:
            # 精确层未就绪：尝试任何**已缓存**的层（即使更粗），映射到像素。
            # 绝不在 paint 路径扫描原始样本（10min 音频实测 52ms 会卡 UI）。
            # 预热通常在加载后 <200ms 内完成，首次闪空帧可接受。
            fallback = self._find_cached_fallback_level(bin_size)
            if fallback is None:
                return None  # 完全没有层：显示占位（中心线+空波形）
            bin_size, (level_min, level_max, level_rms) = fallback

        ms_per_pixel = visible_duration_ms / width
        left_times = visible_start_ms + np.arange(width, dtype=np.float64) * ms_per_pixel
        center_times = left_times + ms_per_pixel * 0.5
        right_times = left_times + ms_per_pixel
        valid = (right_times > 0.0) & (left_times < self._duration_ms)

        def _level_indices(times: np.ndarray) -> np.ndarray:
            sample_indices = np.floor(
                np.clip(times, 0.0, float(self._duration_ms))
                / 1000.0
                * self._sample_rate
            ).astype(np.int64)
            return np.clip(sample_indices // bin_size, 0, len(level_min) - 1)

        left_idx = _level_indices(left_times)
        center_idx = _level_indices(center_times)
        # 右边界减一个极小量，避免恰好落入下一个像素区间。
        right_idx = _level_indices(np.nextafter(right_times, left_times))
        mins = np.minimum.reduce(
            (level_min[left_idx], level_min[center_idx], level_min[right_idx])
        )
        maxs = np.maximum.reduce(
            (level_max[left_idx], level_max[center_idx], level_max[right_idx])
        )
        # RMS：三点取最大（能量核心不会低于实际响度）
        rmss = np.maximum.reduce(
            (level_rms[left_idx], level_rms[center_idx], level_rms[right_idx])
        )
        mins = np.where(valid, mins, 0.0)
        maxs = np.where(valid, maxs, 0.0)
        rmss = np.where(valid, rmss, 0.0)
        peaks = [
            (float(lo), float(hi), float(rm))
            for lo, hi, rm in zip(mins, maxs, rmss)
        ]

        # 更新缓存
        self._peaks_cache = peaks
        self._peaks_cache_key = cache_key

        return peaks

    def _find_cached_fallback_level(self, target_bin: int):
        """找任意已缓存的峰值层（优先最细的可用层）。

        粗层的 min/max/RMS 仍然真实（只是时间分辨率较低），映射到像素后
        波形形状正确、瞬态不丢。返回 (bin_size, (mins, maxs, rmss)) 或 None。
        """
        best_bin = None
        for available_bin in self._waveform_peak_levels:
            if available_bin <= target_bin:
                if best_bin is None or available_bin > best_bin:
                    best_bin = available_bin
        if best_bin is not None:
            self._waveform_peak_levels.move_to_end(best_bin)
            return best_bin, self._waveform_peak_levels[best_bin]
        # 所有可用层都比目标粗——退而用最细的可用层：min/max/RMS 仍然真实，
        # 只是时间分辨率低。绝不能用最粗层（旧 bug）：那会把整窗极值抹进
        # 每个像素，深放大的长音频整屏糊成实心砖块。
        if self._waveform_peak_levels:
            finest = min(self._waveform_peak_levels.keys())
            self._waveform_peak_levels.move_to_end(finest)
            return finest, self._waveform_peak_levels[finest]
        return None

    # 峰值层缓存总预算（min/max/rms 三个 float32 数组 × 各层）
    _PEAK_LEVEL_BUDGET_BYTES = 96 * 1024 * 1024

    def _trim_peak_level_cache(self) -> None:
        """LRU 淘汰：缓存总字节超预算时逐出最久未用的层。"""
        total = sum(
            a.nbytes for level in self._waveform_peak_levels.values() for a in level
        )
        while total > self._PEAK_LEVEL_BUDGET_BYTES and len(self._waveform_peak_levels) > 1:
            _, evicted = self._waveform_peak_levels.popitem(last=False)
            total -= sum(a.nbytes for a in evicted)

    def _note_peak_level_used(self, bin_size: int) -> None:
        if bin_size in self._waveform_peak_levels:
            self._waveform_peak_levels.move_to_end(bin_size)

    def _cancel_peak_preheat(self) -> None:
        """取消在途峰值预热任务并释放句柄。

        任何让旧预热结果失效的路径都必须调用：新音频进入（无论长短）、
        清除音频——否则旧 worker 会继续扫描已被替换的音频，白耗 CPU
        与内存带宽。owner.destroyed 时 task_runner 也会自动取消。
        """
        cancel, self._preheat_cancel = self._preheat_cancel, None
        if cancel is not None:
            cancel()

    def _preheat_peak_levels(self) -> None:
        """构建 min/max/RMS 峰值层；长音频后台、短音频同步（极快）。

        新音频进入时先取消旧预热——包括短音频和无音频的提前返回路径。
        """
        self._cancel_peak_preheat()
        samples = self._waveform_samples
        if samples is None or len(samples) < 1:
            return

        # 短音频（<64K 样本 ≈ 1.5s @44.1k）：同步构建，耗时 <1ms
        if len(samples) < 65536:
            from strange_uta_game.backend.infrastructure.audio import spectrum

            levels = spectrum.build_peak_levels_single_pass(samples)
            if levels:
                self._waveform_peak_levels.update(levels)
                self._trim_peak_level_cache()
            return

        # 长音频：后台构建（单遍归约、可取消），UI 零等待
        samples_id = id(samples)

        from strange_uta_game.frontend.workers import PeakLevelWorker
        from strange_uta_game.frontend.editor.timing import task_runner

        worker = PeakLevelWorker(samples)

        def _cancel_stale() -> None:
            worker.request_cancel()

        def _release_cancel() -> None:
            # worker 正常完成/报错后释放句柄；仅当仍是当前任务（未被更新的
            # 预热替换）才清，避免迟到信号抹掉新任务的取消闭包。
            if self._preheat_cancel is _cancel_stale:
                self._preheat_cancel = None

        def _merge(levels) -> None:
            _release_cancel()
            # 身份门禁：取消是请求式的，worker 可能在收到通知前已算完——
            # 迟到结果不得写入新音频的缓存（第二层保护）。
            if id(self._waveform_samples) != samples_id or not levels:
                return
            for b, level in levels.items():
                if b not in self._waveform_peak_levels:
                    self._waveform_peak_levels[b] = level
            self._trim_peak_level_cache()
            self._invalidate_static_layer()
            self.update()

        self._preheat_cancel = _cancel_stale

        task_runner.start_task(
            self,
            worker,
            on_finished=_merge,
            on_error=lambda _msg: _release_cancel(),
        )

    # ── 显示模式 / 高级设置（齿轮对话框消费） ──

    _SPECTRUM_FFT_CHOICES = (
        64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768,
    )
    # 窗口重叠选项（Sonic Visualiser 口径：overlap = 1 - hop/fft）
    _SPECTRUM_OVERLAP_CHOICES = (0.5, 0.75, 0.875, 0.9375, 0.96875, 0.984375)
    # 缩放上限基准（无音频/短音频时 zoom_cap() 的下限；加载长音频后动态放宽）
    _MAX_ZOOM = 1000.0
    # 最深缩放的采样密度（采样/像素）下限：0.25 = 一帧摊 4 像素——进入
    # 逐采样区间（SV PixelsPerFrame）后靠过采样曲线保持平滑。旧口径是
    # "全曲 1/1000"的相对上限，长音频永远到不了采样级。
    _ZOOM_CAP_MIN_SPP = 0.25
    # 直读路径的可见采样量上限：paint 路径 O(可见采样) 的安全阀
    _DIRECT_READ_MAX_SAMPLES = 128 * 1024
    # 逐采样视图的过采样 sinc 半宽（输入采样数）——SV WaveformOversampler 口径
    _OVERSAMPLE_HALF_WIDTH = 16

    def zoom_cap(self) -> float:
        """动态缩放上限：不浅于 _MAX_ZOOM，最深约 _ZOOM_CAP_MIN_SPP 采样/像素。

        绝对深度口径（对齐 SV 的帧/像素）：cap = 总采样数 / (屏宽 × 最浅
        采样密度)。3 分钟歌 @44.1kHz、屏宽 1200 → cap ≈ 26400x，最深处
        一帧摊 4 像素，可逐字核对 0.0001s 量级的瞬态位置。
        """
        width = self._plot_width()
        if self._duration_ms <= 0 or self._sample_rate <= 0 or width <= 0:
            return self._MAX_ZOOM
        total_samples = self._duration_ms / 1000.0 * self._sample_rate
        return max(self._MAX_ZOOM, total_samples / (width * self._ZOOM_CAP_MIN_SPP))

    @classmethod
    def _spectrum_hop_for(cls, fft_size: int, overlap: float) -> int:
        """窗口重叠 → STFT 帧距（hop）。重叠越大，时间粒度越细。"""
        divisors = {0.5: 2, 0.75: 4, 0.875: 8, 0.9375: 16, 0.96875: 32, 0.984375: 64}
        return max(1, fft_size // divisors.get(overlap, 4))

    def set_display_mode(self, mode: str) -> None:
        """波形图 / 声谱图互斥切换，双谱模式 = 两者上下并排。

        声谱/双谱模式需要更高的显示区并按需启动后台计算；切回波形模式时
        取消在途声谱计算（省 CPU），但保留已完成的缓存，便于快速切回。
        """
        if mode not in ("waveform", "spectrum", "dual") or mode == self._display_mode:
            return
        self._display_mode = mode
        self._apply_display_height()
        self._spectrum_view_cache = None
        self._spectrum_image_cache = None
        self._spectrum_image_cache_buffer = None
        self._spectrum_image_cache_key = None
        if mode in ("spectrum", "dual"):
            self._ensure_spectrum()
        else:
            self._cancel_spectrum_worker()
        self._invalidate_static_layer()
        self.updateGeometry()
        self.update()

    def set_spectrum_active(self, active: bool) -> None:
        """活动性门禁：False 时**任何路径**都不得重启声谱计算。

        状态持久化为 `_spectrum_active`——隐藏/后台期间 set_audio_data、
        改参数、切模式都会走 `_ensure_spectrum()`，统一在此拦截；恢复可见
        时若仍处于声谱模式则按需启动。
        """
        active = bool(active)
        if active == self._spectrum_active:
            return
        self._spectrum_active = active
        if active:
            self._ensure_spectrum()
        else:
            self._cancel_spectrum_worker()

    def set_waveform_rms_enabled(self, enabled: bool) -> None:
        """双层波形开关：关闭后只画外层 min/max 峰值轮廓（旧行为）。"""
        enabled = bool(enabled)
        if enabled == self._waveform_rms_enabled:
            return
        self._waveform_rms_enabled = enabled
        self._invalidate_static_layer()
        self.update()

    def set_metronome_enabled(self, enabled: bool) -> None:
        """节拍器开关（纯状态，不重绘；实际调度在 EditorInterface）。"""
        self._metronome_enabled = bool(enabled)

    def set_metronome_volume(self, volume_pct: int) -> None:
        """节拍器音量（0~100%，纯状态；实际音量在播放器上设置）。"""
        self._metronome_volume = int(max(0, min(100, int(volume_pct))))

    def set_auto_bpm_enabled(self, enabled: bool) -> None:
        """加载后自动检测 BPM 开关（纯状态；实际检测在 EditorInterface）。"""
        self._auto_bpm_enabled = bool(enabled)

    def set_grid_mode(self, mode: str) -> None:
        if mode not in ("time", "bpm") or mode == self._grid_mode:
            return
        self._grid_mode = mode
        self._invalidate_static_layer()
        self.update()

    def set_grid_bpm(self, bpm: float) -> None:
        bpm = float(bpm)
        if not 10.0 <= bpm <= 600.0 or bpm == self._grid_bpm:
            return
        self._grid_bpm = bpm
        self._invalidate_static_layer()
        self.update()

    def set_grid_offset(self, offset_ms: int) -> None:
        """BPM 网格偏移（±10 分钟，毫秒）：拍线相位对齐用。

        正值网格整体后移（延迟），负值前移；仅作用于 BPM 网格。
        """
        try:
            offset_ms = int(offset_ms)
        except (TypeError, ValueError):
            return
        offset_ms = max(-600000, min(600000, offset_ms))
        if offset_ms == self._grid_offset_ms:
            return
        self._grid_offset_ms = offset_ms
        self._invalidate_static_layer()
        self.update()

    def set_beats_per_bar(self, beats: int) -> None:
        """拍号分子（每小节 1~16 拍）：小节线/小节号与节拍器重音的循环周期。"""
        try:
            beats = int(beats)
        except (TypeError, ValueError):
            return
        beats = max(1, min(16, beats))
        if beats == self._beats_per_bar:
            return
        self._beats_per_bar = beats
        self._invalidate_static_layer()
        self.update()

    def set_grid_line_width(self, width_px: int) -> None:
        """网格线宽（0~100px，时间/BPM 共用）；半拍/拍线/小节线按层级递进。

        0 = 不绘制任何网格（含时间标签与小节号）。
        """
        width_px = int(max(0, min(100, width_px)))
        if width_px == self._grid_line_width:
            return
        self._grid_line_width = width_px
        self._invalidate_static_layer()
        self.update()

    def _bpm_grid_widths(self) -> tuple:
        """三类 BPM 网格线宽（半拍, 拍线, 小节线）：保持视觉层级递进。"""
        base = self._grid_line_width
        return (max(1, base - 1), base, base + 1)

    def set_spectrum_params(
        self,
        fft_size: Optional[int] = None,
        overlap: Optional[float] = None,
        freq_scale: Optional[str] = None,
        dyn_range_db: Optional[int] = None,
        display_height: Optional[int] = None,
        freq_min_hz: Optional[int] = None,
        freq_max_hz: Optional[int] = None,
        colormap: Optional[str] = None,
        waveform_display_height: Optional[int] = None,
        spectrum_display_height: Optional[int] = None,
    ) -> None:
        """频谱参数；FFT 窗口/重叠变化触发后台重算，其余即时生效。

        freq_min_hz / freq_max_hz：显示期频率钳制（0=自动/全谱，只重绘）。
        """
        changed = False
        recompute = False
        if (
            fft_size is not None
            and fft_size in self._SPECTRUM_FFT_CHOICES
            and fft_size != self._spectrum_fft_size
        ):
            self._spectrum_fft_size = fft_size
            changed = recompute = True
        if (
            overlap is not None
            and overlap in self._SPECTRUM_OVERLAP_CHOICES
            and overlap != self._spectrum_overlap
        ):
            self._spectrum_overlap = overlap
            changed = recompute = True
        if freq_scale in ("log", "linear") and freq_scale != self._spectrum_freq_scale:
            self._spectrum_freq_scale = freq_scale
            changed = True
        if dyn_range_db is not None:
            dyn_range_db = int(max(20, min(120, dyn_range_db)))
            if dyn_range_db != self._spectrum_dyn_range_db:
                self._spectrum_dyn_range_db = dyn_range_db
                changed = True
        if colormap in spectrum_core.SPECTRUM_COLORMAPS and colormap != self._spectrum_colormap:
            self._spectrum_colormap = colormap
            changed = True
        if freq_min_hz is not None:
            freq_min_hz = int(max(0, min(96000, int(freq_min_hz))))
            if freq_min_hz != self._spectrum_freq_min_hz:
                self._spectrum_freq_min_hz = freq_min_hz
                changed = True
        if freq_max_hz is not None:
            freq_max_hz = int(max(0, min(96000, int(freq_max_hz))))
            if freq_max_hz != self._spectrum_freq_max_hz:
                self._spectrum_freq_max_hz = freq_max_hz
                changed = True
        # 旧调用方只传 display_height 时保持兼容：同时更新两种模式。
        if display_height is not None:
            if waveform_display_height is None:
                waveform_display_height = display_height
            if spectrum_display_height is None:
                spectrum_display_height = display_height
        if waveform_display_height is not None:
            height = int(
                max(
                    _WAVEFORM_MIN_DISPLAY_HEIGHT,
                    min(_MAX_DISPLAY_HEIGHT, waveform_display_height),
                )
            )
            if height != self._waveform_display_height:
                self._waveform_display_height = height
                changed = True
        if spectrum_display_height is not None:
            height = int(
                max(
                    _MIN_DISPLAY_HEIGHT,
                    min(_MAX_DISPLAY_HEIGHT, spectrum_display_height),
                )
            )
            if height != self._spectrum_display_height:
                self._spectrum_display_height = height
                changed = True
        if changed:
            self._apply_display_height()
        if recompute:
            self._reset_spectrum_cache()
            self._ensure_spectrum()
        if changed:
            self._invalidate_static_layer()
            self.update()

    def display_settings(self) -> dict:
        """当前显示设置的快照（齿轮对话框初始化 / timing.* 持久化共用）。"""
        return {
            "display_mode": self._display_mode,
            "grid_mode": self._grid_mode,
            "grid_bpm": self._grid_bpm,
            "grid_offset_ms": self._grid_offset_ms,
            "beats_per_bar": self._beats_per_bar,
            "grid_line_width": self._grid_line_width,
            "spectrum_fft_size": self._spectrum_fft_size,
            "spectrum_overlap": self._spectrum_overlap,
            "spectrum_freq_scale": self._spectrum_freq_scale,
            "spectrum_dyn_range_db": self._spectrum_dyn_range_db,
            "spectrum_colormap": self._spectrum_colormap,
            "spectrum_freq_min_hz": self._spectrum_freq_min_hz,
            "spectrum_freq_max_hz": self._spectrum_freq_max_hz,
            "waveform_display_height": self._waveform_display_height,
            "spectrum_display_height": self._spectrum_display_height,
            # 兼容旧调用方：返回当前模式正在使用的高度。
            "display_height": self._active_display_height(),
            "waveform_rms_enabled": self._waveform_rms_enabled,
            "actual_spectrum_overlap": self._actual_overlap,
            # 时间标签行为（与设置页「波形时间标签」组共用 timing.* 键）
            "tag_edit_enabled": self._tag_edit_enabled,
            "center_playhead_enabled": self._center_playhead_mode,
            "tag_char_enabled": self._tag_char_enabled,
            "tag_ruby_enabled": self._tag_ruby_enabled,
            # 节拍器（EditorInterface 的调度器消费；此处仅为设置链快照）
            "metronome_enabled": self._metronome_enabled,
            "metronome_volume": self._metronome_volume,
            # 加载后自动检测 BPM（纯开关快照，实际检测在 EditorInterface）
            "auto_bpm_on_load": self._auto_bpm_enabled,
        }

    def spectrum_audio_source(self) -> Optional[tuple]:
        """BPM 检测的数据源：(单声道 samples, sample_rate)，无音频时 None。"""
        if self._waveform_samples is None or self._sample_rate <= 0:
            return None
        return self._waveform_samples, self._sample_rate

    def _apply_display_height(self) -> None:
        """「显示高度」经 minimumHeight 领取空间（sizeHint 会被 Expanding 预览吃掉）。

        预览（Expanding）在 VBox 中吸收所有剩余空间，Preferred 的 sizeHint
        会被忽略——必须用 minimumHeight 才能领到期望高度。预览同步让位到
        160（由 EditorInterface 管理），确保总 min 不超出常规窗口。
        """
        self.setMinimumHeight(self._active_display_height())
        self.updateGeometry()

    def _active_display_height(self) -> int:
        if self._display_mode == "spectrum":
            return self._spectrum_display_height
        if self._display_mode == "dual":
            # 双谱：上下两条 lane 各按期望高度领取空间
            return self._waveform_display_height + self._spectrum_display_height
        return self._waveform_display_height

    def _dual_lane_heights(self, h: int) -> tuple:
        """双谱模式上下 lane 的高度分配：按两种模式的期望高度比例分摊实际高度。

        窗口空间不足被布局压缩时两 lane 同比例收缩，波形/声谱的相对占比
        仍与用户设置的「显示高度」一致。
        """
        total_expected = self._waveform_display_height + self._spectrum_display_height
        if total_expected <= 0 or h <= 2:
            half = h // 2
            return half, h - half
        wave_h = int(round(h * self._waveform_display_height / total_expected))
        wave_h = max(1, min(wave_h, h - 1))
        return wave_h, h - wave_h

    def _dual_boundary_y(self) -> Optional[int]:
        """双谱模式上下 lane 交界线的 y（widget 坐标）；非双谱返回 None。"""
        if self._display_mode != "dual":
            return None
        wave_h, _ = self._dual_lane_heights(self.height())
        return int(wave_h)  # _dual_lane_heights 仅标注 -> tuple，收窄为 int

    def _lane_hit_at(self, y: float) -> bool:
        """y 是否落在双谱交界的窄命中带内（±_LANE_HIT_TOLERANCE）。"""
        boundary = self._dual_boundary_y()
        return boundary is not None and abs(y - boundary) <= self._LANE_HIT_TOLERANCE

    def sizeHint(self):
        from PyQt6.QtCore import QSize

        return QSize(super().sizeHint().width(), self._active_display_height())

    # ── 声谱后台计算（QThread + moveToThread，遵循 workers.py 约定） ──

    def _reset_spectrum_cache(self) -> None:
        """音频/FFT 变化：作废在途任务与缓存（generation 机制丢弃过期结果）。"""
        self._cancel_spectrum_worker()
        self._spectrum = None
        self._actual_overlap = None  # 上一音源的降级值不残留
        self._spectrum_state = "idle"
        self._spectrum_progress_pct = -1
        self._spectrum_error = ""
        self._spectrum_view_cache = None
        self._spectrum_view_cache_key = None
        self._spectrum_image_cache = None
        self._spectrum_image_cache_buffer = None
        self._spectrum_image_cache_key = None

    def _ensure_spectrum(self) -> None:
        if not self._spectrum_active:
            return  # 隐藏/后台门禁：恢复可见前不启动任何计算
        if self._display_mode not in ("spectrum", "dual"):
            return
        if self._spectrum_worker is not None:
            return  # 已有任务在途
        if self._waveform_samples is None or self._sample_rate <= 0:
            return
        overlap = self._effective_spectrum_overlap()
        if overlap is not None:
            expected_hop = self._spectrum_hop_for(self._spectrum_fft_size, overlap)
            if (
                self._spectrum is not None
                and self._spectrum.get("fft_size") == self._spectrum_fft_size
                and self._spectrum.get("hop") == expected_hop
            ):
                return
        # overlap 为 None（超预算）时由 _start_spectrum_worker 统一置 error
        self._start_spectrum_worker()

    def _effective_spectrum_overlap(self) -> Optional[float]:
        """基础矩阵预算内可用的最大重叠；None 表示连 50% 都超（应拒绝）。

        未设门禁时 1 小时音频 @93.75% 重叠的基础矩阵约 1.18GiB（OOM 风险）。
        """
        return spectrum_core.pick_overlap_within_budget(
            len(self._waveform_samples),
            self._sample_rate,
            self._spectrum_fft_size,
            self._spectrum_overlap,
        )

    def _start_spectrum_worker(self) -> None:
        overlap = self._effective_spectrum_overlap()
        if overlap is None:
            self._spectrum_state = "error"
            self._spectrum_error = self.tr("音频过长：超出声谱内存预算，请降低窗口重叠率或缩短音频")
            self._invalidate_static_layer()
            self.update()
            return
        self._cancel_spectrum_worker()
        self._spectrum_state = "computing"
        self._spectrum_progress_pct = 0
        self._actual_overlap = overlap  # 记录实际使用值（可能被预算降级）

        worker = SpectrogramWorker(
            self._waveform_samples,
            self._sample_rate,
            self._spectrum_fft_size,
            hop=self._spectrum_hop_for(self._spectrum_fft_size, overlap),
        )
        self._spectrum_worker = worker
        # 线程由 task_runner 注册表持有并自回收（不 parent 到本控件——控件
        # 销毁不得析构运行中的线程）；槽是本控件的方法引用，控件销毁时连接
        # 自动断开，迟到信号不会触碰已销毁的 UI。
        from strange_uta_game.frontend.editor.timing import task_runner

        task_runner.start_task(
            self,
            worker,
            on_progress=self._on_spectrum_progress,
            on_finished=self._on_spectrum_finished,
            on_error=self._on_spectrum_error,
            is_current=lambda w: w is self._spectrum_worker,
        )
        self._invalidate_static_layer()
        self.update()

    def _cancel_spectrum_worker(self) -> None:
        """请求取消并立即返回（UI 路径禁止同步 wait 冻结界面）。

        worker 分块检查取消标志（每块 ≈70ms），返回后由 task_runner 的自
        回收链（finished → thread.quit → thread.finished → deleteLater）
        完成退出与销毁，全程不依赖本控件存活。
        """
        worker = self._spectrum_worker
        self._spectrum_worker = None
        if worker is not None:
            worker.request_cancel()
            if self._spectrum_state == "computing":
                self._spectrum_state = "idle"

    def is_task_current(self, worker) -> bool:
        """task_runner 中继的身份判定：只有当前任务的信号进入 UI 槽。"""
        return worker is self._spectrum_worker

    def _on_spectrum_progress(self, value: float) -> None:
        if self._spectrum_worker is None or self._spectrum_state != "computing":
            return  # 过期任务的迟到进度（中继已过滤，双保险）
        pct = int(value * 100)
        if pct == self._spectrum_progress_pct:
            return
        self._spectrum_progress_pct = pct
        self._invalidate_static_layer()
        self.update()

    def _on_spectrum_finished(self, result: dict) -> None:
        if result is None:
            return  # 已取消
        self._spectrum = result
        self._spectrum_state = "ready"
        self._spectrum_view_cache = None
        self._spectrum_view_cache_key = None
        self._spectrum_image_cache = None
        self._spectrum_image_cache_buffer = None
        self._spectrum_image_cache_key = None
        self._invalidate_static_layer()
        self.update()

    def _on_spectrum_error(self, message: str) -> None:
        self._spectrum_state = "error"
        self._spectrum_error = message
        self._invalidate_static_layer()
        self.update()

    # ── 声谱视图渲染（金字塔选层 → reduceat → LUT → QImage） ──

    def _compute_spectrum_view(self, w: int, h: int) -> Optional[np.ndarray]:
        """当前可见窗的 (w, h) uint8 视图；行 0 = 最低频段。带缓存。"""
        spec = self._spectrum
        if spec is None or w <= 0 or h <= 0:
            return None
        visible_start_ms = self._visible_start_ms()
        visible_duration_ms = self._visible_duration_ms()
        if visible_duration_ms <= 0:
            return None
        key = (
            w,
            h,
            round(visible_start_ms, 1),
            round(visible_duration_ms, 1),
            id(spec["matrix"]),
            self._spectrum_freq_scale,
            self._spectrum_freq_min_hz,
            self._spectrum_freq_max_hz,
        )
        if self._spectrum_view_cache_key == key and self._spectrum_view_cache is not None:
            return self._spectrum_view_cache

        matrix = spec["matrix"]
        frames_per_ms = spec["sample_rate"] / (spec["hop"] * 1000.0)
        frame_start = max(0, int(math.floor(visible_start_ms * frames_per_ms)))
        frame_end = min(
            matrix.shape[0],
            int(math.ceil((visible_start_ms + visible_duration_ms) * frames_per_ms)) + 1,
        )
        if frame_end <= frame_start:
            return None
        level, level_matrix = self._spectrum_level_matrix(spec, frame_start, frame_end, w)
        group_start = frame_start >> level
        group_end = min(
            (frame_end + (1 << level) - 1) >> level, level_matrix.shape[0]
        )
        sub = level_matrix[group_start:group_end]
        if sub.shape[0] < 1:
            return None
        cols = spectrum_core.reduce_columns(sub, w)
        bin_edges = spectrum_core.frequency_bin_edges(
            cols.shape[1], spec["sample_rate"], spec["fft_size"], h,
            self._spectrum_freq_scale,
            self._spectrum_freq_min_hz,
            self._spectrum_freq_max_hz,
        )
        view = spectrum_core.reduce_rows(cols, bin_edges)

        self._spectrum_view_cache = view
        self._spectrum_view_cache_key = key
        return view

    def _spectrum_level_matrix(self, spec: dict, frame_start: int, frame_end: int,
                               cols: int) -> tuple:
        """返回 (shift, 矩阵)；UI 路径不同步构建任何完整层。

        - 预算内（levels 存在）：直接用金字塔第 level 层。
        - 超预算：level ≥ coarse_mid 时用 worker 后台备好的粗层金字塔
          （levels[level - mid]）；更深的缩放（level < mid）退回原始矩阵的
          可见切片归约——深缩放可见帧少，扫描量远小于全矩阵。
        """
        levels = spec.get("levels")
        level = self._spectrum_pick_level(spec, frame_start, frame_end, cols)
        if levels is not None:
            return level, levels[level]
        mid = spec.get("coarse_mid", 0)
        coarse = spec.get("coarse_levels") or []
        if mid > 0 and level >= mid and (level - mid) < len(coarse):
            return level, coarse[level - mid]
        return 0, spec["matrix"]

    def _spectrum_pick_level(self, spec: dict, frame_start: int, frame_end: int,
                             cols: int) -> int:
        """选层；超预算（levels=None）时按后台粗层金字塔推虚拟层深。"""
        levels = spec.get("levels")
        if levels is not None:
            return spectrum_core.pick_level(levels, frame_start, frame_end, cols)
        mid = spec.get("coarse_mid", 0)
        coarse = spec.get("coarse_levels") or []
        # pick_level 只用 len(levels) 计算层深，不索引内容 → 占位列表即可
        return spectrum_core.pick_level(
            [None] * (mid + len(coarse)), frame_start, frame_end, cols
        )

    def _spectrum_lut(self) -> np.ndarray:
        """uint8 dB → RGBA 颜色查找表；随动态范围变化重建。

        矩阵编码为 -128dB→0、0dB→255，动态范围 R(dB) 的可见下沿
        (-R dB) 对应 u = (128 - R)·255/128（方向不能反，否则 R 越大
        截得越狠）。LUT 内部把可见段拉伸到完整色带；低于地板填色带
        底部色（不依赖主题背景，LUT 缓存键包含 floor_u 与色带名）。
        """
        floor_u = int(round((128 - self._spectrum_dyn_range_db) * 255.0 / 128.0))
        floor_u = max(0, min(255, floor_u))
        key = (floor_u, self._spectrum_colormap)
        if self._spectrum_lut_cache is None or self._spectrum_lut_cache_key != key:
            self._spectrum_lut_cache = spectrum_core.build_colormap_lut(
                floor_u, self._spectrum_colormap
            )
            self._spectrum_lut_cache_key = key
        return self._spectrum_lut_cache

    def _draw_spectrum_image(self, painter: QPainter, w: int, h: int) -> None:
        """频谱热图本体（计算中显示进度占位文字）。"""
        if self._samples is None:
            return
        if self._spectrum_state != "ready" or self._spectrum is None:
            if self._spectrum_state == "error":
                text = self.tr("声谱计算失败")
            else:
                text = self.tr("声谱计算中") + f" {max(0, self._spectrum_progress_pct)}%"
            painter.setPen(theme.text_hint)
            painter.drawText(
                QRect(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text
            )
            return
        view = self._compute_spectrum_view(w, h)
        if view is None:
            return
        lut = self._spectrum_lut()
        # 与 _spectrum_view_cache 同键缓存最终 RGBA QImage：拖拽/滚动逐帧
        # 重建静态层时不再重复 LUT 查表 + flipud + tobytes 的全量分配
        # （与波形侧 _sample_view_cache 同型优化）。
        image_key = (self._spectrum_view_cache_key, self._spectrum_lut_cache_key)
        image = self._spectrum_image_cache
        if (
            image is None
            or self._spectrum_image_cache_key != image_key
        ):
            # 翻转行序（顶行 = 高频）并转成 QImage 需要的 (H, W, 4) 行主序。
            rgba = np.ascontiguousarray(lut[np.flipud(view.T)])
            buffer = rgba.tobytes()
            image = QImage(buffer, w, h, w * 4, QImage.Format.Format_RGBA8888)
            # QImage 不持有 buffer 引用，bytes 必须与 image 一同保活
            self._spectrum_image_cache = image
            self._spectrum_image_cache_buffer = buffer
            self._spectrum_image_cache_key = image_key
        painter.drawImage(0, 0, image)

    def _draw_freq_axis(self, painter: QPainter, axis_w: int, h: int) -> None:
        """声谱左侧独立轴区：强度色卡 + 频率刻度 + 分隔线。

        轴区与时间绘图区物理分离——tag/热图/播放头都在轴区右侧，频率刻度
        不再被视口左缘的 tag 覆盖，反之亦然。
        """
        painter.fillRect(0, 0, axis_w, h, theme.waveform_bg)
        painter.setPen(QPen(theme.border_primary, 1))
        painter.drawLine(axis_w - 1, 0, axis_w - 1, h)
        self._draw_spectrum_colorbar(painter, h)
        if h < 80:
            return
        nyquist = self._sample_rate / 2.0
        # 热图行映射与轴刻度共用同一区间（resolve_freq_range），钳制永不脱节
        f_lo, f_hi = spectrum_core.resolve_freq_range(
            nyquist, self._spectrum_freq_scale,
            self._spectrum_freq_min_hz, self._spectrum_freq_max_hz,
        )
        if self._spectrum_freq_scale == "log":
            marks = (50, 100, 200, 500, 1000, 2000, 4000, 8000, 16000)

            def freq_pos(f: float) -> float:
                return math.log(f / f_lo) / math.log(f_hi / f_lo)
        else:
            # 步长按可见跨度自适应（约 3~8 根刻度），从细到粗取第一档
            span = f_hi - f_lo
            step = next(
                (s for s in (100, 200, 500, 1000, 2000, 4000, 8000, 16000)
                 if span / s <= 8),
                16000,
            )
            marks = tuple(
                int(round(m))
                for m in np.arange(f_lo + step, f_hi + step * 0.01, step)
            )

            def freq_pos(f: float) -> float:
                return (f - f_lo) / (f_hi - f_lo)

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        last_y = None
        for f in marks:
            if not f_lo < f < f_hi:
                continue
            y = int((1.0 - freq_pos(f)) * (h - 1))
            if last_y is not None and abs(y - last_y) < 26:
                continue
            last_y = y
            text = str(int(f)) if f < 1000 else f"{f / 1000:g}k"
            painter.setPen(theme.text_secondary)
            painter.drawText(
                QRect(12, y - 8, axis_w - 18, 16),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                text,
            )
            # 小刻度线
            painter.setPen(QPen(theme.border_primary, 1))
            painter.drawLine(axis_w - 5, y, axis_w - 1, y)

    def _draw_spectrum_colorbar(self, painter: QPainter, h: int) -> None:
        """在频率轴左缘绘制当前色带；顶部强、底部为动态范围下沿。"""
        bar_h = max(1, h - 4)
        floor_u = int(round((128 - self._spectrum_dyn_range_db) * 255.0 / 128.0))
        floor_u = max(0, min(255, floor_u))
        levels = np.rint(np.linspace(255, floor_u, bar_h)).astype(np.uint8)
        rgba = np.ascontiguousarray(self._spectrum_lut()[levels].reshape(bar_h, 1, 4))
        buffer = rgba.tobytes()
        image = QImage(buffer, 1, bar_h, 4, QImage.Format.Format_RGBA8888)
        painter.drawImage(QRect(2, 2, 7, bar_h), image)
        painter.setPen(QPen(theme.border_primary, 1))
        painter.drawRect(1, 1, 8, bar_h + 1)

    @log_slow_method(
        "timeline.paint",
        20,
        lambda self, args, kwargs: {
            "tags": len(self._time_tags) + len(self._warning_time_tags),
            "zoom": f"{self._zoom_factor:.2f}",
        },
    )
    def paintEvent(self, a0: Optional[QPaintEvent]):
        _ = a0
        painter = QPainter(self)
        w, h = self.width(), self.height()

        if self._duration_ms <= 0:
            painter.fillRect(self.rect(), theme.waveform_bg)
            painter.setPen(theme.text_hint)
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("请加载音频文件")
            )
            return

        visible_start_ms = self._visible_start_ms()
        visible_duration_ms = self._duration_ms / self._zoom_factor
        visible_end_ms = visible_start_ms + visible_duration_ms
        device_pixel_ratio = max(1.0, float(self.devicePixelRatioF()))

        layer_key = (
            w,
            h,
            device_pixel_ratio,
            visible_start_ms,
            visible_duration_ms,
            self._range_start_ms,
            self._range_end_ms,
            self._tag_edit_enabled,
            self._tag_char_enabled,
            self._tag_ruby_enabled,
            frozenset(self._selected_handles),
            self._drag_delta_ms if self._is_dragging_tags else 0,
        )
        if self._static_layer is None or self._static_layer_key != layer_key:
            self._static_layer = self._render_static_layer(
                w, h, visible_start_ms, visible_end_ms, visible_duration_ms
            )
            self._static_layer_key = layer_key
        painter.drawPixmap(0, 0, self._static_layer)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 播放头/拖拽徽标画在轴区右侧的绘图区内（与静态层同一坐标系）
        axis = self._spectrum_axis_width()
        painter.save()
        if axis:
            painter.translate(axis, 0)
        try:
            self._draw_playhead(painter, w - axis, h, visible_start_ms, visible_duration_ms)
            self._draw_drag_badge(painter, w - axis, h, visible_start_ms, visible_duration_ms)
            # 双谱交界悬停/拖拽高亮：覆盖线画在静态层之上，悬停切换只重绘
            # 不失效静态层缓存
            if self._lane_boundary_hovered or self._lane_dragging:
                boundary = self._dual_boundary_y()
                if boundary is not None:
                    painter.setPen(QPen(theme.accent_primary, 2))
                    painter.drawLine(0, boundary, w - axis, boundary)
        finally:
            painter.restore()

    def _spectrum_axis_width(self) -> int:
        """声谱/双谱模式左侧的频率轴 gutter 宽度（tag/热图不进入，防相互覆盖）。"""
        return _SPECTRUM_AXIS_W if self._display_mode in ("spectrum", "dual") else 0

    def _plot_width(self) -> int:
        """绘图区宽度（扣除频率轴 gutter）。"""
        return max(1, self.width() - self._spectrum_axis_width())

    def _render_static_layer(
        self,
        w: int,
        h: int,
        visible_start_ms: float,
        visible_end_ms: float,
        visible_duration_ms: float,
    ) -> QPixmap:
        # QPixmap dimensions are physical pixels. Without a matching DPR the
        # logical-size cache is stretched by Windows at 125%/150%/200%, which
        # blurs both the waveform and the small ruby labels.
        dpr = max(1.0, float(self.devicePixelRatioF()))
        layer = QPixmap(
            max(1, int(math.ceil(w * dpr))),
            max(1, int(math.ceil(h * dpr))),
        )
        layer.setDevicePixelRatio(dpr)
        layer.fill(theme.waveform_bg)
        painter = QPainter(layer)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # 频率轴 gutter：热图/网格/tag/播放头统一从轴区右侧开始（tag 不再
        # 覆盖频率刻度，频率刻度也不盖 tag——两者物理分离）。
        axis = self._spectrum_axis_width()
        is_dual = self._display_mode == "dual"
        # 双谱 lane 分配只算一次，热图/波形/频率轴/tag 共用同一组边界
        dual_wave_h, dual_spec_h = self._dual_lane_heights(h) if is_dual else (0, h)
        painter.save()
        if axis:
            painter.translate(axis, 0)
        try:
            plot_w = w - axis
            # 频谱热图是不透明的整幅位图，必须垫在网格/标签之下；
            # 波形是描线，画在网格之上即可。
            if is_dual:
                # 双谱：热图垫在下半声谱 lane（进度占位文字也钳在该 lane 内）
                painter.save()
                painter.translate(0, dual_wave_h)
                self._draw_spectrum_image(painter, plot_w, dual_spec_h)
                painter.restore()
            elif self._display_mode == "spectrum":
                self._draw_spectrum_image(painter, plot_w, h)
            # 网格横跨整个绘图区——双谱两 lane 共用同一时间轴
            self._draw_time_grid(painter, plot_w, h, visible_start_ms, visible_end_ms)
            if is_dual:
                painter.save()
                painter.setClipRect(QRect(0, 0, plot_w, dual_wave_h))
                self._draw_waveform(painter, plot_w, dual_wave_h)
                painter.restore()
                # lane 分隔线：1px 边框色，两 lane 边界可辨
                painter.setPen(QPen(theme.border_primary, 1))
                painter.drawLine(0, dual_wave_h, plot_w, dual_wave_h)
            elif self._display_mode != "spectrum":
                self._draw_waveform(painter, plot_w, h)
            self._draw_playback_range(
                painter, plot_w, h, visible_start_ms, visible_duration_ms
            )
            self._draw_time_tags(painter, plot_w, h, visible_start_ms, visible_end_ms)
        finally:
            painter.restore()
        # 轴区：不透明背景 + 强度色卡 + 分隔线 + 频率刻度（双谱只标声谱 lane）。
        if axis:
            if is_dual:
                painter.save()
                painter.translate(0, dual_wave_h)
                self._draw_freq_axis(painter, axis, dual_spec_h)
                painter.restore()
            else:
                self._draw_freq_axis(painter, axis, h)
        painter.end()
        return layer

    def _draw_time_grid(self, painter: QPainter, w: int, h: int,
                        visible_start_ms: float, visible_end_ms: float):
        if self._grid_line_width <= 0:
            return  # 网格线宽 0 = 不绘制网格（时间/BPM 共用口径）
        if self._grid_mode == "bpm" and self._grid_bpm > 0:
            self._draw_bpm_grid(painter, w, h, visible_start_ms, visible_end_ms)
            return
        visible_duration = visible_end_ms - visible_start_ms
        # 粒度按像素密度选（每根线 ≥64px）：粗览保持 1s~1min 档；深放大
        # 细分到 0.1ms——采样级缩放下时间轴读得出 0.0001s 量级的位置
        threshold = visible_duration * 64.0 / max(1, w)
        grid_interval = next(
            (
                iv
                for iv in (
                    0.1, 0.5, 1, 5, 10, 50, 100, 500,
                    1000, 5000, 10000, 60000,
                )
                if iv >= threshold
            ),
            60000.0,
        )

        grid_width = self._grid_line_width
        painter.setPen(QPen(theme.border_primary, grid_width))
        # 整数序号循环（t = n×interval）避开浮点步进漂移；亚秒档用秒的
        # 小数标注（interval=0.1ms → "0.0001s"）
        first_n = int(math.ceil(visible_start_ms / grid_interval - 1e-9))
        last_n = int(math.floor(visible_end_ms / grid_interval + 1e-9))
        if grid_interval >= 1000:
            decimals = 0
        else:
            decimals = max(1, min(4, int(math.ceil(math.log10(1000.0 / grid_interval)))))
        for n_idx in range(first_n, last_n + 1):
            t = n_idx * grid_interval
            if visible_duration > 0:
                x = int((t - visible_start_ms) / visible_duration * w)
                painter.drawLine(x, 0, x, h)

                painter.setPen(theme.text_secondary)
                if decimals == 0:
                    s = int(t // 1000)
                    time_text = f"{s // 60}:{s % 60:02d}"
                else:
                    time_text = f"{t / 1000.0:.{decimals}f}s"
                painter.drawText(x + 2, 12, time_text)
                painter.setPen(QPen(theme.border_primary, grid_width))

    def _draw_bpm_grid(self, painter: QPainter, w: int, h: int,
                       visible_start_ms: float, visible_end_ms: float) -> None:
        """BPM 网格：拍线、按拍号的小节线（加重 + 小节号）、深放大时半拍细分。

        拍时刻 = 偏移 + b·拍长（b 为整数，可为负）——偏移用于把网格相位
        对齐到歌曲实际节拍（节拍通常不从 0ms 开始）。b=0 是第 1 小节的
        第 1 拍；负数拍仍画线但不标注小节号。小节线/小节号与节拍器重音
        共用 ``_beats_per_bar``（拍号分子）作为循环周期。
        """
        if self._grid_line_width <= 0:
            return  # 网格线宽 0 = 不绘制网格
        visible_duration = visible_end_ms - visible_start_ms
        if visible_duration <= 0:
            return
        beat_ms = 60000.0 / self._grid_bpm
        offset = float(self._grid_offset_ms)
        pixels_per_beat = beat_ms / visible_duration * w
        first_beat = int(math.ceil((visible_start_ms - offset) / beat_ms))
        last_beat = int(math.floor((visible_end_ms - offset) / beat_ms))
        if last_beat < first_beat:
            return

        def beat_x(b: float) -> int:
            return int((offset + b * beat_ms - visible_start_ms)
                       / visible_duration * w)

        half, beat, bar = self._bpm_grid_widths()
        half_color = QColor(theme.border_primary)
        half_color.setAlpha(110)
        pen_half = QPen(half_color, half)
        pen_beat = QPen(theme.border_primary, beat)
        pen_bar = QPen(theme.waveform_line, bar)

        # 半拍细分线（放大到一拍 ≥28px 才画，避免糊成一片）
        if pixels_per_beat >= 28:
            painter.setPen(pen_half)
            for b in range(first_beat, last_beat + 1):
                x = beat_x(b + 0.5)
                if 0 <= x < w:
                    painter.drawLine(x, 0, x, h)

        # 拍线与小节线（按拍号：每 bar_beats 拍一个小节）。
        # 像素密度门控 + 步长遍历：拍距小于 ~4px 时按 2 的幂提升步长并对齐
        # 小节，使绘制的线条数量与窗口宽度同量级——600BPM × 2 小时
        # 全览时逐拍空转循环实测 173ms，会卡 UI。
        bar_beats = self._beats_per_bar
        beat_step = 1
        while beat_step * pixels_per_beat < 4 and beat_step < 4096:
            beat_step *= 2
        if beat_step > 1:
            # 粗化时至少按小节，且步长须为小节拍数的倍数——非 2 幂拍号
            #（3/4、5/4、7/4）下向上取整到小节倍数，否则小节线会被跳过
            beat_step = max(beat_step, bar_beats)
            if beat_step % bar_beats:
                beat_step += bar_beats - (beat_step % bar_beats)
            first_beat += (-first_beat) % beat_step
        draw_beats = beat_step == 1
        for b in range(first_beat, last_beat + 1, beat_step):
            x = beat_x(b)
            if not 0 <= x < w:
                continue
            if b % bar_beats == 0:
                painter.setPen(pen_bar)
                painter.drawLine(x, 0, x, h)
                if b >= 0:
                    painter.setPen(theme.text_secondary)
                    painter.drawText(x + 2, 12, str(b // bar_beats + 1))
            elif draw_beats:
                painter.setPen(pen_beat)
                painter.drawLine(x, 0, x, h)

    def _samples_per_pixel(self) -> float:
        """当前视窗的采样密度（采样数/像素）；< 1 时进入逐采样显示区间。"""
        width = self._plot_width()
        if self._duration_ms <= 0 or self._sample_rate <= 0 or width <= 0:
            return float("inf")
        return self._visible_duration_ms() / 1000.0 * self._sample_rate / width

    def _compute_sample_view(self, width: int) -> Optional[tuple]:
        """逐采样视图（SV PixelsPerFrame 区间，每像素不足一个采样时）。

        返回 (values, anchor_x, anchor_v)：values 为各像素中心位置的窗化
        sinc 过采样值（长 width，音频范围外为 0）；anchor_x/anchor_v 为可见
        真实采样落点所在的像素列与原始值（描点方块用，SV 口径画 2px 方块）。
        不可用返回 None。
        """
        if self._samples is None or self._duration_ms <= 0 or width <= 0:
            return None
        samples = self._waveform_samples
        if samples is None or len(samples) == 0:
            return None

        visible_start_ms = self._visible_start_ms()
        cache_key = (width, self._zoom_factor, visible_start_ms, id(samples))
        if (
            self._sample_view_cache_key == cache_key
            and self._sample_view_cache is not None
        ):
            return self._sample_view_cache

        visible_duration_ms = self._visible_duration_ms()
        ms_per_pixel = visible_duration_ms / width
        center_times = (
            visible_start_ms
            + (np.arange(width, dtype=np.float64) + 0.5) * ms_per_pixel
        )
        positions = center_times / 1000.0 * self._sample_rate
        values = spectrum_core.oversample_windowed_sinc(
            samples, positions, self._OVERSAMPLE_HALF_WIDTH
        )
        # 音频范围外的像素（居中播放的半屏空白等）置 0，与包络口径一致
        valid = (center_times > 0.0) & (center_times < self._duration_ms)
        values = np.where(valid, values, 0.0).astype(np.float32)

        # 可见采样 → 锚点像素（采样时间落在哪一列，方块就画在哪一列；
        # spp<1 时每列至多一个采样）
        first_k = max(0, int(np.floor(visible_start_ms / 1000.0 * self._sample_rate)))
        last_k = min(
            len(samples),
            int(
                np.ceil(
                    (visible_start_ms + visible_duration_ms)
                    / 1000.0
                    * self._sample_rate
                )
            ),
        )
        if first_k < last_k:
            ks = np.arange(first_k, last_k, dtype=np.int64)
            anchor_x = np.floor(
                (ks / self._sample_rate * 1000.0 - visible_start_ms) / ms_per_pixel
            ).astype(np.int64)
            keep = (anchor_x >= 0) & (anchor_x < width)
            anchor_x = anchor_x[keep]
            anchor_v = samples[first_k:last_k][keep].astype(np.float32)
        else:
            anchor_x = np.empty(0, dtype=np.int64)
            anchor_v = np.empty(0, dtype=np.float32)

        view = (values, anchor_x, anchor_v)
        self._sample_view_cache = view
        self._sample_view_cache_key = cache_key
        return view

    def _draw_sample_view(self, painter: QPainter, w: int, h: int) -> None:
        """逐采样绘制：过采样平滑曲线 + 采样点方块（SV PixelsPerFrame 口径）。"""
        mid_y = h // 2
        amplitude_scale = h / 2.0

        # 中心线
        painter.setPen(QPen(theme.waveform_line, 1))
        painter.drawLine(0, mid_y, w, mid_y)

        view = self._compute_sample_view(w)
        if view is None:
            return
        values, anchor_x, anchor_v = view

        # 平滑曲线：一条 polyline 穿过所有像素中心的过采样值
        ys = mid_y - values * amplitude_scale
        polygon = QPolygonF()
        for x in range(w):
            polygon.append(QPointF(float(x), float(ys[x])))
        painter.setPen(QPen(theme.waveform_line, 1))
        painter.drawPolyline(polygon)

        # 采样点方块：真实采样值（非插值值），SV 在该区间的小方块描点
        if len(anchor_x):
            size = 2.0
            path = QPainterPath()
            for ax, av in zip(anchor_x, anchor_v):
                y = mid_y - av * amplitude_scale
                path.addRect(
                    QRectF(ax + 0.5 - size / 2, y - size / 2, size, size)
                )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.waveform_line)
            painter.drawPath(path)

    def _rasterize_waveform_bars(
        self, painter: QPainter, w: int, h: int, peaks: List[tuple]
    ) -> Optional[QImage]:
        """把 min/max 包络 + RMS 核心带直接光栅化进 ARGB 缓冲（numpy）。

        播放（居中跟随）时静态层逐帧全量重建，QPainter 逐像素图元
        （drawRect 循环实测 ~12ms/层、数千子路径的 drawPath+AA 实测
        ~280ms）都撑不住 60fps；这里每像素只算一对整数行界，掩码填充
        两个颜色层，一次 drawImage 上屏（实测 ~2ms/帧）。

        必须按**物理像素**出图：静态层 QPixmap 带 DPR（Windows 125%/
        150%/200% 缩放），若按逻辑分辨率出图再交给 Qt 放大，每根柱会被
        拉成 dpr 个物理像素（变粗发糊）；这里在物理网格上填充并把 QImage
        的 DPR 设成同值，blit 时 1:1 无重采样。

        峰值按**物理列**计算和绘制：HiDPI 下 1 个逻辑像素对应 dpr 个不同
        的峰值列，而不是只画中间一列留下规则空隙。SV 的 HiDPI 支持同样
        会让 layer data 使用设备分辨率；这是截图中密实而不呈“栅栏”的关键。

        竖线之外还有 **中点连接线**（SV WaveformLayer.cpp 情况 B 的
        ``lineTo(rangeMiddle)``）：相邻列幅值范围不重叠（contiguous=
        false）或本列范围不足 1px 高（trivialRange）时，从上一列中点
        连一条 1px 折线到本列中点——波形的低谷/细尾因此是连续细线而非
        散点（漏掉它就是"峰与峰之间全是间隙"）。范围重叠的相邻列不画
        连接线（竖线本身已覆盖，SV 情况 A 同样只画竖线）。

        RMS 核心带是横向连续的带状，保持整列填充。
        """
        if not peaks or w <= 0 or h <= 0:
            return None
        arr = np.asarray(peaks, dtype=np.float32)  # (w, 3): min, max, rms
        dpr = max(1.0, float(painter.device().devicePixelRatioF()))
        pw = max(1, int(math.ceil(w * dpr)))
        ph = max(1, int(math.ceil(h * dpr)))
        # 兼容直接调用者传入逻辑列；正式绘制路径会传入 pw 个独立峰值。
        if len(arr) != pw:
            src = np.minimum(
                (np.arange(pw, dtype=np.float64) * len(arr) / pw).astype(np.int64),
                len(arr) - 1,
            )
            arr = arr[src]
        mins = arr[:, 0]
        maxs = arr[:, 1]
        rmss = arr[:, 2]

        mid = h / 2.0
        amp = h / 2.0

        # 连续性/细线判断必须留在逻辑坐标中：SV 的阈值是 1.0/0.5
        # 逻辑像素。若先乘 DPR 再取整，125%~200% 屏幕会在不同缩放倍率
        # 得到不同拓扑，细尾时断时续。
        top_logical = np.clip(mid - maxs * amp, 0.0, float(h))
        bot_logical = np.clip(mid - mins * amp, 0.0, float(h))
        top = np.clip(np.rint(top_logical * dpr), 0, ph).astype(np.int32)
        bot = np.clip(np.rint(bot_logical * dpr), 0, ph).astype(np.int32)
        bot = np.minimum(np.maximum(bot, top + 1), ph)  # 每列至少 1px

        stroke_argb = int(theme.waveform_fill.rgba())
        line_argb = int(theme.waveform_line.rgba())

        buf = np.zeros((ph, pw), dtype=np.uint32)

        # 包络：每个物理列都有自己对应时间片的 min/max，不留规则空列。
        rows = np.arange(ph, dtype=np.int32)[:, None]
        stroke_mask = (rows >= top[None, :]) & (rows < bot[None, :])
        buf[stroke_mask] = stroke_argb

        # 中点连接线（SV 情况 B）：非重叠相邻列 / 不足 1px 高的列之间，
        # 在竖线列之间的空隙物理列上补 1px 插值点，串成连续折线
        mids = ((top + bot) // 2).astype(np.int32)
        trivial = (bot_logical - top_logical) < 1.0
        contig = np.ones(pw, dtype=bool)
        contig[1:] = (
            (top_logical[1:] <= bot_logical[:-1] + 0.5)
            & (bot_logical[1:] >= top_logical[:-1] - 0.5)
        )
        active = (~contig) | trivial  # 本列与前一列之间需要连接线
        active[0] = False
        if pw > 1:
            # 相邻物理列之间没有空 x 可插值；在当前列补齐两个中点间的
            # 像素，等价于无 AA 的陡斜线，避免低幅尾部断成散点。
            conn_top = np.minimum(mids, np.r_[mids[0], mids[:-1]])
            conn_bot = np.maximum(mids, np.r_[mids[0], mids[:-1]]) + 1
            conn_mask = (
                (rows >= conn_top[None, :])
                & (rows < conn_bot[None, :])
                & active[None, :]
            )
            buf[conn_mask] = stroke_argb

        if self._waveform_rms_enabled:
            # RMS 核心带：钳在包络内，整列填充（带状横向连续）
            r_top = np.clip(
                np.rint((mid - np.minimum(rmss, np.abs(maxs)) * amp) * dpr),
                top,
                ph,
            ).astype(np.int32)
            r_bot = np.clip(
                np.rint((mid + np.minimum(rmss, np.abs(mins)) * amp) * dpr),
                0,
                bot,
            ).astype(np.int32)
            drawn = rmss > 0.0
            r_bot = np.where(
                drawn, np.minimum(np.maximum(r_bot, r_top + 1), ph), r_top
            )
            buf[
                (rows >= r_top[None, :]) & (rows < r_bot[None, :])
            ] = line_argb

        image = QImage(buf.tobytes(), pw, ph, pw * 4, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(dpr)
        return image

    def _draw_waveform(self, painter: QPainter, w: int, h: int):
        """波形绘制（Sonic Visualiser 口径，按采样密度分区间）：

        - 每像素 ≥ 1 采样（FramesPerPixel）：双层包络——外层 min/max 峰值
          cosmetic 细线（瞬态极值）+ 可选的内层 RMS 核心带；
        - 每像素 < 1 采样（PixelsPerFrame）：逐采样视图（过采样平滑曲线 +
          采样点方块），见 _draw_sample_view。
        """
        if self._samples_per_pixel() < 1.0:
            self._draw_sample_view(painter, w, h)
            return

        dpr = max(1.0, float(painter.device().devicePixelRatioF()))
        peak_columns = max(1, int(math.ceil(w * dpr)))
        peaks = self._compute_waveform_peaks(peak_columns)
        if not peaks:
            return

        image = self._rasterize_waveform_bars(painter, w, h, peaks)
        if image is not None:
            painter.drawImage(0, 0, image)

        # 中心线
        painter.setPen(QPen(theme.waveform_line, 1))
        painter.drawLine(0, h // 2, w, h // 2)

    def _draw_playback_range(
        self,
        painter: QPainter,
        w: int,
        h: int,
        visible_start_ms: float,
        visible_duration_ms: float,
    ) -> None:
        """Shade playback-excluded areas and draw the locked A/B boundaries."""
        if visible_duration_ms <= 0:
            return
        visible_end_ms = visible_start_ms + visible_duration_ms

        shade = QColor(theme.waveform_bg)
        shade.setAlpha(150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(shade))

        if self._range_start_ms is not None:
            start_x = self._ts_to_x(
                self._range_start_ms, visible_start_ms, visible_duration_ms, w
            )
            if self._range_start_ms > visible_start_ms:
                painter.drawRect(0, 0, min(w, max(0, start_x)), h)
        if self._range_end_ms is not None:
            end_x = self._ts_to_x(
                self._range_end_ms, visible_start_ms, visible_duration_ms, w
            )
            if self._range_end_ms < visible_end_ms:
                painter.drawRect(max(0, min(w, end_x)), 0, w, h)

        def draw_boundary(ms: Optional[int], color, label: str) -> None:
            if ms is None or not (visible_start_ms <= ms <= visible_end_ms):
                return
            x = self._ts_to_x(ms, visible_start_ms, visible_duration_ms, w)
            painter.setPen(QPen(color, 3))
            painter.drawLine(x, 0, x, h)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRect(max(0, x - 8), 1, 16, 15), 3, 3)
            painter.setPen(theme.waveform_bg)
            painter.drawText(QRect(max(0, x - 8), 1, 16, 15), Qt.AlignmentFlag.AlignCenter, label)

        draw_boundary(self._range_start_ms, theme.status_complete, "A")
        draw_boundary(self._range_end_ms, theme.accent_warning, "B")

    @staticmethod
    def _format_ruby_label(ruby_text: str) -> str:
        # 按字素簇截断：附加符不与基字截开（IPA ɛ̃/n̩ 等）
        from strange_uta_game.backend.domain.graphemes import (
            grapheme_count,
            truncate_graphemes,
        )

        if grapheme_count(ruby_text) > 4:
            return f"「{truncate_graphemes(ruby_text, 4)}...」"
        return f"「{ruby_text}」"

    def _draw_label_plate(self, painter: QPainter, x: int, label_y: int,
                          fm, text: str, color, text_w: int) -> None:
        """带半透明背景底片绘制标签文字：在文字后铺一层接近不透明的背景色底，
        把文字从蓝色波形上分离出来，可读性显著优于细光晕，且仍透出少量波形。
        声谱/双谱模式下底片更不透明——热力图颜色杂乱，需要更强的分离。

        含 IPA 的标签临时换用 IPA 字体（CJK 主字体跨字体回退无法锚定
        附加符）；底片几何按**实际绘制字体**的度量计算（审查反馈 #7：
        IPA 字体的 ascent/descent 与基字体不同，用基字体度量会让附加符
        越出底片）；底片宽度由调用方按同字体度量传入。
        """
        from strange_uta_game.backend.infrastructure.parsers.ipa import has_ipa_text

        draw_font = None
        if has_ipa_text(text) or _has_multicodepoint_cluster(text):
            from strange_uta_game.frontend.font_utils import phonetic_lyrics_font

            draw_font = phonetic_lyrics_font(painter.font())
        fm_plate = QFontMetrics(draw_font) if draw_font is not None else fm
        top = label_y - fm_plate.ascent()
        plate = QColor(theme.waveform_bg)
        plate.setAlpha(245 if self._display_mode in ("spectrum", "dual") else 225)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(plate))
        painter.drawRoundedRect(
            QRect(x - 2, top - 1, text_w + 4, fm_plate.height() + 1), 2, 2
        )
        painter.setPen(color)
        if draw_font is not None:
            painter.save()
            painter.setFont(draw_font)
            painter.drawText(x, label_y, text)
            painter.restore()
        else:
            painter.drawText(x, label_y, text)

    def _draw_time_tags(self, painter: QPainter, w: int, h: int,
                        visible_start_ms: float, visible_end_ms: float):
        self._hit_boxes = []  # (x, handle, ts, cy)——cy 为把手所属 lane 的中心
        visible_duration = visible_end_ms - visible_start_ms
        if visible_duration <= 0:
            return

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()
        label_y = fm.ascent() + 1  # 所有标签统一贴顶显示，竖线在其下方展开
        label_dx = 2
        show_char = self._tag_char_enabled
        # 注音显示以字符显示为前提：字符关则注音也不显示
        show_ruby = self._tag_ruby_enabled and self._tag_char_enabled

        normal_color = theme.accent_warning
        warn_color = theme.timetag_nonmonotonic

        # 双谱两 lane 读同一份 tag 列表：竖线/把手逐 lane 绘制（同步的根源
        # 就是单数据源 + 双份渲染）；顶部标签轨道整体只有一条，只画一份。
        lanes = self._tag_lanes(h)
        label_lane = lanes[0]

        for lane_top, lane_h, lane_is_spec in lanes:
            # 声谱 lane：竖线从顶部轨道一直延伸进内容区（把手在轨道下沿），
            # 并加高对比 halo——inferno 色带覆盖紫/红/橙/黄，语义色竖线单靠
            # 颜色在部分区域不可辨；警告 tag 额外用虚线区分（两类 lane 一致）。
            if lane_is_spec:
                line_top, line_bot = 0.0, 0.92
                warn_top, warn_bot = 0.0, 0.96
            else:
                line_top, line_bot = 0.2, 0.8
                warn_top, warn_bot = 0.1, 0.9

            # ── pass 1：竖线（语义色不变，拖拽中的选中标签按 delta 平移）；收集可见标签 ──
            # entries：(x, tag, is_warning, color)
            entries: List[Tuple[int, TimeTag, bool, object]] = []

            def _draw_line(tag: TimeTag, color, width_px: int,
                           y_top: float, y_bot: float, is_warning: bool,
                           _top=lane_top, _lane_h=lane_h,
                           _is_spec=lane_is_spec, _entries=entries):
                ts = self._draw_ts(tag)
                if not (visible_start_ms <= ts <= visible_end_ms):
                    return
                x = self._ts_to_x(ts, visible_start_ms, visible_duration, w)
                # 行首/行尾强调（只动竖线，把手不变）：加粗一档、向两端显著
                # 延长，并垫一圈背景色 halo——波形 lane 上相当于给竖线加描边；
                # 密集缩放下把手会被密度门控隐藏，竖线保证行边界仍可辨认
                is_edge = tag.handle in self._line_edge_handles
                if is_edge:
                    width_px += self._EDGE_LINE_WIDTH_BONUS
                    y_top = max(0.0, y_top - self._EDGE_LINE_SPAN_EXT)
                    y_bot = min(self._EDGE_LINE_SPAN_CAP, y_bot + self._EDGE_LINE_SPAN_EXT)
                if _is_spec or is_edge:
                    painter.setPen(QPen(theme.waveform_bg, width_px + 3))
                    painter.drawLine(x, int(_top + _lane_h * y_top),
                                     x, int(_top + _lane_h * y_bot))
                pen = QPen(color, width_px)
                if is_warning:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(x, int(_top + _lane_h * y_top),
                                 x, int(_top + _lane_h * y_bot))
                _entries.append((x, tag, is_warning, color))

            # A：只遍历可见窗内的标签（列表已按 ts 排序，二分定位），把每帧 O(N) 降到 O(可见数)
            for tag in self._visible_slice(self._time_tags, visible_start_ms, visible_end_ms):
                _draw_line(tag, normal_color, 2, line_top, line_bot, False)
            for tag in self._visible_slice(self._warning_time_tags, visible_start_ms, visible_end_ms):
                _draw_line(tag, warn_color, 3, warn_top, warn_bot, True)

            # ── pass 2：标签文字按优先级放置，避免重叠（先画，把手随后覆盖其上）──
            # 优先级：选中的标签无条件显示；其余按 x 从左到右贪心，左侧优先占位。
            # 双谱两 lane 的 entries 完全一致，只在第一条 lane 画一份。
            if (lane_top, lane_h, lane_is_spec) == label_lane:
                labels = []  # (a, b, tag, color, text)
                for x, tag, is_warning, color in entries:
                    text = ""
                    if show_char and tag.label:
                        text += tag.label
                    if show_ruby and tag.ruby:
                        text += self._format_ruby_label(tag.ruby)
                    if not text:
                        continue
                    # 行首/行尾的标签右移让开加宽的竖线（见 _EDGE_LABEL_DX）
                    dx = (self._EDGE_LABEL_DX
                          if tag.handle in self._line_edge_handles else label_dx)
                    a = x + dx
                    # 含 IPA 的标签用 IPA 字体度量（与 _draw_label_plate 同字体）
                    from strange_uta_game.backend.infrastructure.parsers.ipa import (
                        has_ipa_text,
                    )

                    if has_ipa_text(text) or _has_multicodepoint_cluster(text):
                        from strange_uta_game.frontend.font_utils import (
                            phonetic_lyrics_font,
                        )

                        fm_label = QFontMetrics(phonetic_lyrics_font(font))
                    else:
                        fm_label = fm
                    b = a + fm_label.horizontalAdvance(text)
                    labels.append((a, b, tag, color, text))

                for a, b, tag, color, text in self._resolve_label_layout(labels):
                    self._draw_label_plate(painter, a, label_y, fm, text, color, b - a)

            # ── pass 3：把手在所属 lane 竖直中央（波形谱/声谱 lane 各自居中）。
            # 仅编辑开启。密度门控：相邻把手过近则跳过（不绘制 / 不可命中）──逐
            # lane 独立判定，双谱两条谱互不影响。
            if self._tag_edit_enabled:
                sel_color = theme.accent_secondary
                cy = int(lane_top + lane_h / 2)
                last_x = None
                for x, tag, is_warning, color in sorted(entries, key=lambda e: e[0]):
                    if last_x is not None and (x - last_x) < self._MIN_HANDLE_SPACING:
                        continue
                    last_x = x
                    selected = tag.handle in self._selected_handles
                    half = self._HANDLE_SEL_HALF_W if selected else self._HANDLE_HALF_W
                    rect = QRect(x - half, cy - self._HANDLE_HEIGHT // 2, half * 2, self._HANDLE_HEIGHT)
                    # 实心色块（选中=蓝，未选中=标签语义色）+ 背景色描边，声谱 lane 描边加宽
                    painter.setPen(QPen(theme.waveform_bg, 2 if lane_is_spec else 1))
                    painter.setBrush(QBrush(sel_color if selected else color))
                    painter.drawRect(rect)
                    self._hit_boxes.append((x, tag.handle, tag.ts, cy))

    def _resolve_label_layout(self, labels):
        """标签防重叠优先级布局。

        labels: ``(a, b, tag, color, text)`` 列表（a/b 为标签左右像素边界）。
        规则：选中的标签无条件保留；其余按左边界从左到右贪心占位，与已占用区间
        重叠则丢弃（左侧优先）。返回需要绘制的标签子集。
        """
        occupied: List[Tuple[int, int]] = []

        def _fits(a: int, b: int) -> bool:
            return all(b < oa or a > ob for oa, ob in occupied)

        out = []
        # 选中优先：无条件保留并占位
        for item in labels:
            a, b, tag = item[0], item[1], item[2]
            if tag.handle in self._selected_handles:
                out.append(item)
                occupied.append((a, b))
        # 其余：从左到右贪心，重叠则跳过（左侧优先）
        for item in sorted(labels, key=lambda L: L[0]):
            a, b, tag = item[0], item[1], item[2]
            if tag.handle in self._selected_handles:
                continue
            if _fits(a, b):
                out.append(item)
                occupied.append((a, b))
        return out

    def _draw_playhead(self, painter: QPainter, w: int, h: int,
                       visible_start_ms: float, visible_duration_ms: float):
        if visible_duration_ms <= 0:
            return

        if visible_start_ms <= self._current_ms <= visible_start_ms + visible_duration_ms:
            ratio = (self._current_ms - visible_start_ms) / visible_duration_ms
            x = int(ratio * w)

            painter.setPen(QPen(theme.accent_primary, 2))
            painter.drawLine(x, 0, x, h)

            # 播放头三角形标记
            painter.setBrush(QBrush(theme.accent_primary))
            triangle = QPolygon([
                QPoint(x - 6, 0),
                QPoint(x + 6, 0),
                QPoint(x, 10),
            ])
            painter.drawPolygon(triangle)

    @staticmethod
    def _format_ms(ms: int) -> str:
        ms = max(0, int(ms))
        m, s = divmod(ms // 1000, 60)
        return f"{m}:{s:02d}.{ms % 1000:03d}"

    def _draw_drag_badge(self, painter: QPainter, w: int, h: int,
                         visible_start_ms: float, visible_duration_ms: float):
        """拖拽时显示偏差值徽标（主：有符号 delta；辅：锚点绝对时间）。"""
        if not self._is_dragging_tags:
            return
        delta = self._drag_delta_ms
        sign = "+" if delta >= 0 else "−"  # 减号 U+2212
        line1 = f"Δ {sign}{abs(delta)} ms"
        anchor_ts = self._drag_anchor_ts + delta
        line2 = f"→ {self._format_ms(anchor_ts)}" if self._drag_anchor_handle is not None else None

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        fm = painter.fontMetrics()
        text_w = max(fm.horizontalAdvance(line1), fm.horizontalAdvance(line2 or ""))
        line_h = fm.height()
        pad = 4
        box_w = text_w + pad * 2
        box_h = line_h * (2 if line2 else 1) + pad * 2

        # 徽标定位在锚点当前 x 的右下角，夹到控件内
        anchor_x = self._ts_to_x(anchor_ts, visible_start_ms, visible_duration_ms, w)
        bx = anchor_x + 8
        by = h // 2 + 12
        bx = max(2, min(bx, w - box_w - 2))
        by = max(2, min(by, h - box_h - 2))

        bg = QColor(0, 0, 0, 170)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(QRect(bx, by, box_w, box_h), 3, 3)
        painter.setPen(theme.accent_primary)
        ty = by + pad + fm.ascent()
        painter.drawText(bx + pad, ty, line1)
        if line2:
            painter.drawText(bx + pad, ty + line_h, line2)

    def mousePressEvent(self, a0: Optional[QMouseEvent]):
        if a0 is None or self._duration_ms <= 0:
            return
        if a0.button() != Qt.MouseButton.LeftButton:
            return
        x = a0.position().x()
        y = a0.position().y()
        # 最高优先：双谱交界窄命中带（几何手势，先于 tag 把手与 pan）
        if self._lane_hit_at(y):
            self._lane_dragging = True
            self._lane_press_global_y = int(round(a0.globalPosition().y()))
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            self.lane_resize_started.emit()
            a0.accept()
            return
        # 优先命中顶部把手（编辑模式）：命中则进入把手交互，不启动 pan
        if self._tag_edit_enabled:
            hit = self._hit_test_handle(x, y)
            if hit is not None:
                handle, ts, _hx = hit
                ctrl = bool(a0.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self._press_handle = handle
                self._press_handle_ts = ts
                self._press_x = x
                # 已选中且非 Ctrl → 允许拖动；否则按下仅用于点击选中
                self._drag_armed = (handle in self._selected_handles) and not ctrl
                return
        # 回退：原 pan/seek 预备态
        self._pan_start_x = x
        self._pan_start_scroll = self._scroll_position
        self._is_panning = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseMoveEvent(self, a0: Optional[QMouseEvent]):
        if a0 is None:
            return
        # 交界拖拽中：只发增量，不走把手/pan 分支（拖拽中时长可能被清空，
        # 放在 _duration_ms 守卫之前保证必能收到 release 前的 move）
        if self._lane_dragging:
            if self._lane_press_global_y is None:
                self._lane_dragging = False
            elif a0.buttons() & Qt.MouseButton.LeftButton:
                delta = int(round(a0.globalPosition().y())) - self._lane_press_global_y
                self.lane_resize_moved.emit(delta)
                a0.accept()
                return
        if self._duration_ms <= 0:
            return
        x = a0.position().x()
        y = a0.position().y()
        if not (a0.buttons() & Qt.MouseButton.LeftButton):
            # 悬停光标反馈：交界窄命中带 > 把手可点光标 > 默认
            lane_hovered = self._lane_hit_at(y)
            if lane_hovered:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif self._tag_edit_enabled and self._hit_test_handle(x, y) is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.unsetCursor()
            if lane_hovered != self._lane_boundary_hovered:
                self._lane_boundary_hovered = lane_hovered
                self.update()
            return
        # 把手按下中：拖拽分支
        if self._press_handle is not None:
            if not self._is_dragging_tags:
                if self._drag_armed and abs(x - self._press_x) > 4:
                    self._is_dragging_tags = True
                    self._drag_anchor_handle = self._press_handle
                    self._drag_anchor_ts = self._press_handle_ts
                    self._suspend_auto_scroll()
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    return
            visible_duration_ms = self._visible_duration_ms()
            raw_delta = (x - self._press_x) / self._plot_width() * visible_duration_ms
            self._drag_delta_ms = self._clamp_drag_delta(int(round(raw_delta)))
            self.update()
            return
        # 回退：原 pan 平移
        if self._pan_start_x is None:
            return
        delta_x = x - self._pan_start_x
        if not self._is_panning and abs(delta_x) > 4:
            self._is_panning = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._is_panning:
            visible_duration_ms = self._visible_duration_ms()
            delta_ms = delta_x / self._plot_width() * visible_duration_ms
            new_scroll = self._clamp_scroll(
                self._pan_start_scroll - delta_ms / self._duration_ms)
            if new_scroll != self._scroll_position:
                self._suspend_auto_scroll()
                self._scroll_position = new_scroll
                self.scroll_position_changed.emit(self._scroll_position)
                self.update()

    def mouseReleaseEvent(self, a0: Optional[QMouseEvent]):
        if a0 is None:
            return
        # 交界拖拽收尾（放在时长守卫前：拖拽中清空音频也能正常复位）
        if (
            self._lane_dragging
            and self._lane_press_global_y is not None
            and a0.button() == Qt.MouseButton.LeftButton
        ):
            delta = int(round(a0.globalPosition().y())) - self._lane_press_global_y
            self._lane_dragging = False
            self._lane_press_global_y = None
            self.unsetCursor()
            self.lane_resize_finished.emit(delta)
            a0.accept()
            return
        if self._duration_ms <= 0:
            return
        if a0.button() != Qt.MouseButton.LeftButton:
            return
        # 把手拖拽提交
        if self._is_dragging_tags:
            if self._drag_delta_ms != 0 and self._selected_handles:
                # 锚点（被按住拖动的把手）置于首位，供下游 preview 选中同步
                handles = list(self._selected_handles)
                anchor = self._drag_anchor_handle
                if anchor in self._selected_handles:
                    handles.remove(anchor)
                    handles.insert(0, anchor)
                self.tags_drag_committed.emit(handles, self._drag_delta_ms)
            self._reset_drag()
            self.unsetCursor()
            self.update()
            return
        # 把手单击（未拖动）：选中 / 多选切换
        if self._press_handle is not None:
            handle = self._press_handle
            ts = self._press_handle_ts
            ctrl = bool(a0.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl:
                if handle in self._selected_handles:
                    self._selected_handles.discard(handle)
                else:
                    self._selected_handles.add(handle)
            else:
                self._selected_handles = {handle}
                self.seek_requested.emit(ts)
                self.tag_clicked.emit(handle[0], handle[1], handle[2], handle[3])
            self._press_handle = None
            self._drag_armed = False
            self.update()
            return
        # 回退：原 seek
        if not self._is_panning and self._pan_start_x is not None:
            self.seek_requested.emit(self._x_to_time(a0.position().x()))
        self._pan_start_x = None
        self._is_panning = False
        self.unsetCursor()

    def leaveEvent(self, event) -> None:
        # 鼠标离开后清掉交界悬停高亮（光标由 Qt 按控件进出自动恢复）
        if self._lane_boundary_hovered:
            self._lane_boundary_hovered = False
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, a0: Optional[QWheelEvent]):
        if a0 is None:
            return
        if not self._zoom_enabled:
            a0.ignore()
            return

        delta = a0.angleDelta().y()
        if delta == 0:
            a0.ignore()
            return

        if a0.modifiers() & Qt.KeyboardModifier.ControlModifier:
            new_zoom = self._zoom_factor * (1.2 if delta > 0 else 1 / 1.2)
            new_zoom = max(1.0, min(self.zoom_cap(), new_zoom))

            if new_zoom != self._zoom_factor:
                mouse_ratio = max(
                    0.0,
                    min(
                        1.0,
                        (a0.position().x() - self._spectrum_axis_width())
                        / max(1, self._plot_width()),
                    ),
                )
                visible_start = self._scroll_position
                visible_duration = 1.0 / self._zoom_factor
                audio_position = visible_start + mouse_ratio * visible_duration

                self._suspend_auto_scroll()
                self._zoom_factor = new_zoom
                new_visible_duration = 1.0 / self._zoom_factor
                self._scroll_position = self._clamp_scroll(
                    audio_position - mouse_ratio * new_visible_duration)

                self.zoom_changed.emit(self._zoom_factor)
                self.scroll_position_changed.emit(self._scroll_position)
                self.update()
        else:
            # 一格滚轮平移可见窗口的 10%；向上滚回到更早时间，向下滚到更晚时间。
            wheel_steps = delta / 120.0
            new_scroll = self._clamp_scroll(
                self._scroll_position - wheel_steps / (self._zoom_factor * 10.0)
            )
            if new_scroll != self._scroll_position:
                self._suspend_auto_scroll()
                self._scroll_position = new_scroll
                self.scroll_position_changed.emit(self._scroll_position)
                self.update()

        a0.accept()


# ──────────────────────────────────────────────
# 时间轴控件（包含波形显示 + 缩放控制 + 滚动条）
# ──────────────────────────────────────────────

class _TimelineHeightHandle(QWidget):
    """时间轴底部的纵向拖拽手柄。"""

    drag_started = pyqtSignal()
    drag_moved = pyqtSignal(int)
    drag_finished = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_global_y: Optional[int] = None
        self._hovered = False
        self.setFixedHeight(8)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def event(self, e) -> bool:
        # 窗口失活（Alt+Tab 等）时清掉按住状态，避免过期锚点残留
        if e.type() == QEvent.Type.WindowDeactivate:
            self._press_global_y = None
            self.update()
        return super().event(e)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self._hovered or self._press_global_y is not None
        color = theme.accent_primary if active else theme.border_primary
        line_h = 3 if active else 1
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(
            QRectF(8, (self.height() - line_h) / 2, max(0, self.width() - 16), line_h),
            line_h / 2,
            line_h / 2,
        )

    def mousePressEvent(self, event: Optional[QMouseEvent]) -> None:
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_global_y = int(round(event.globalPosition().y()))
        self.drag_started.emit()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: Optional[QMouseEvent]) -> None:
        if event is None or self._press_global_y is None:
            super().mouseMoveEvent(event)
            return
        delta = int(round(event.globalPosition().y())) - self._press_global_y
        self.drag_moved.emit(delta)
        event.accept()

    def mouseReleaseEvent(self, event: Optional[QMouseEvent]) -> None:
        if (
            event is None
            or event.button() != Qt.MouseButton.LeftButton
            or self._press_global_y is None
        ):
            super().mouseReleaseEvent(event)
            return
        delta = int(round(event.globalPosition().y())) - self._press_global_y
        self._press_global_y = None
        self.drag_finished.emit(delta)
        self.update()
        event.accept()


class TimelineWidget(QWidget):
    """时间轴 - 显示音频波形 + 时间网格 + 时间标签 + 播放位置"""

    seek_requested = pyqtSignal(int)
    waveform_visibility_changed = pyqtSignal(bool)
    # 显示设置变化（display_settings() 的键），timing_interface 持久化用
    display_settings_changed = pyqtSignal(dict)
    # 声谱区实际显示高度变化（齿轮弹窗「实际 N px」提示实时同步）
    actual_spectrum_height_changed = pyqtSignal(int)
    tag_clicked = pyqtSignal(int, int, int, bool)
    tags_drag_committed = pyqtSignal(object, int)

    # 横向滚动条整数分辨率：把"整段时长"映射为 [0, _SCROLL_SCALE] 个单位，
    # pageStep = 可见时间窗占比（_SCROLL_SCALE / zoom），使滑块长度随缩放自动伸缩。
    _SCROLL_SCALE = 100000
    # 缩放上限基准（滑杆映射下限；长音频实际由 zoom_cap() 放宽到采样级）
    _MAX_ZOOM = 1000.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._duration_ms = 0
        self._waveform_visible = True
        self._zoom_enabled = True
        # 高级设置对话框的强引用（非模态窗口，销毁前不回收）
        self._advanced_dialog = None
        # 程序化 setValue 时抑制缩放滑条回调，避免反馈环（但不能 blockSignals，
        # 否则 qfluentwidgets Slider 的 valueChanged→_adjustHandlePos 抓手不跟动）
        self._suppress_zoom_slider = False
        # 声谱活动性合成：波形开关请求 × 应用/窗口可见性
        self._spectrum_requested = True
        self._app_visible = True
        self._resize_start_height = 0
        self._resize_start_settings: Optional[dict] = None
        # 双谱交界拖拽快照（settings + 起始边界/实际高度，见 _begin_lane_resize）
        self._lane_resize_start: Optional[dict] = None
        self._init_ui()

    def changeEvent(self, event):
        """切语言时精确 retranslate——本 widget 持有 waveform 缓存，不能整体 rebuild。"""
        from PyQt6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.Type.LanguageChange:
            if hasattr(self, "switch_waveform"):
                self.switch_waveform.setToolTip(self.tr("波形显示"))
                # SwitchButton 的 On/Off 文本：本来由 FluentTranslator 处理，但
                # pseudo 模式下需要我们的 tr 接管才能显示 ⟦⟧。
                self.switch_waveform.setOnText(self.tr("开"))
                self.switch_waveform.setOffText(self.tr("关"))
            if hasattr(self, "btn_waveform_settings"):
                self.btn_waveform_settings.setToolTip(self.tr("波形图高级设置"))
            # 音频名标签：用布尔 flag 标记是否是占位，不靠字符串比较——
            # 切到 ja_JP 后 "未加载音频" 变成 "音声未読み込み"，再切到
            # en_US 时字符串比较失败导致不刷新。
            if hasattr(self, "lbl_audio_name") and getattr(self, "_audio_name_is_placeholder", True):
                self.lbl_audio_name.setText(self.tr("未加载音频"))
            # WaveformDisplay 的绘制 placeholder 自带 self.tr，自动跟随
            if hasattr(self, "waveform_display"):
                self.waveform_display.update()
        super().changeEvent(event)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # 波形显示区域
        self.waveform_display = WaveformDisplay(self)
        self.waveform_display.seek_requested.connect(self.seek_requested.emit)
        self.waveform_display.zoom_changed.connect(self._on_zoom_changed)
        self.waveform_display.scroll_position_changed.connect(self._on_scroll_changed)
        self.waveform_display.tag_clicked.connect(self.tag_clicked.emit)
        self.waveform_display.tags_drag_committed.connect(self.tags_drag_committed.emit)
        # 双谱交界拖拽：改两 lane 比例（总高不变），复用底边手柄的持久化链
        self.waveform_display.lane_resize_started.connect(self._begin_lane_resize)
        self.waveform_display.lane_resize_moved.connect(self._move_lane_resize)
        self.waveform_display.lane_resize_finished.connect(self._finish_lane_resize)
        layout.addWidget(self.waveform_display, stretch=1)
        # 显示区高度变化（含模式/期望/窗口变化）→ 实时推送实际显示高度
        self.waveform_display.installEventFilter(self)

        # 底部控制栏：独立容器 + 明确最小高度 + 不透明背景。
        # 不能用裸 QHBoxLayout——空间不足时 WaveformDisplay 的大 minimumHeight
        # 会与底栏控件重叠（曾实测声谱侵入底栏 34~274px）。
        self._bottom_bar = QWidget(self)
        self._bottom_bar.setAutoFillBackground(True)
        self._bottom_bar.setMinimumHeight(28)
        # 垂直 Fixed: 波形区隐藏时底栏不被布局拉伸(否则产生大块空白)
        from PyQt6.QtWidgets import QSizePolicy as _SP
        self._bottom_bar.setSizePolicy(
            _SP.Policy.Preferred, _SP.Policy.Fixed
        )
        bottom_layout = QHBoxLayout(self._bottom_bar)
        bottom_layout.setContentsMargins(4, 2, 4, 2)
        bottom_layout.setSpacing(8)

        # 缩放控制（对数刻度：滑条 0-10000 线性对应 zoom 1x-100x 对数）
        self.zoom_slider = Slider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setRange(0, 10000)
        self.zoom_slider.setValue(self._zoom_to_slider(50.0))  # 默认50x
        # slider 用 minimum 而非 fixed：横幅有富余时让它跟着加宽，便于精确缩放
        self.zoom_slider.setMinimumWidth(120)
        self.zoom_slider.setMaximumWidth(220)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)
        bottom_layout.addWidget(self.zoom_slider)

        self.zoom_label = InlineEditableCaptionLabel("50.0x", self)
        self.zoom_label.setMinimumWidth(40)
        self.zoom_label.value_committed.connect(self._commit_zoom_text)
        bottom_layout.addWidget(self.zoom_label)

        # 横向滚动条
        self.scroll_bar = QScrollBar(Qt.Orientation.Horizontal, self)
        self.scroll_bar.valueChanged.connect(self._on_scroll_bar_changed)
        bottom_layout.addWidget(self.scroll_bar, stretch=1)
        # 初始按当前缩放设置 pageStep/range/value（滑块长度=可见窗占比）
        self._update_scroll_bar_metrics()

        # 音频名称标签（_audio_name_is_placeholder 标志位让 changeEvent
        # 不靠字符串比较即可判断是否需要重译）
        self.lbl_audio_name = CaptionLabel(self.tr("未加载音频"), self)
        self._audio_name_is_placeholder = True
        # 音频名很长时（长文件名 + 翻译后的"未加载音频"前缀）让标签可压缩；
        # 用 maxWidth 限制上限避免吃掉太多 toolbar 空间。
        self.lbl_audio_name.setMaximumWidth(400)
        self.lbl_audio_name.setMinimumWidth(120)
        from PyQt6.QtCore import Qt as _Qt
        self.lbl_audio_name.setTextInteractionFlags(_Qt.TextInteractionFlag.NoTextInteraction)
        self.lbl_audio_name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom_layout.addWidget(self.lbl_audio_name)

        # 波形图高级设置（齿轮）：显示模式 / BPM 网格 / 频谱参数
        self.btn_waveform_settings = TransparentToolButton(FluentIcon.SETTING, self)
        self.btn_waveform_settings.setFixedSize(24, 24)
        self.btn_waveform_settings.setIconSize(QSize(14, 14))
        self.btn_waveform_settings.setToolTip(self.tr("波形图高级设置"))
        self.btn_waveform_settings.clicked.connect(self._on_waveform_settings_clicked)
        bottom_layout.addWidget(self.btn_waveform_settings)

        # 波形显示开关
        self.switch_waveform = SwitchButton(self)
        self.switch_waveform.setChecked(True)
        self.switch_waveform.setMinimumWidth(50)
        # 显式覆盖默认 "On"/"Off"——qfluentwidgets 的 FluentTranslator 不一定能
        # 覆盖到 SUG 当前语言（且 pseudo 模式下要让 ⟦⟧ 可视化）。
        self.switch_waveform.setOnText(self.tr("开"))
        self.switch_waveform.setOffText(self.tr("关"))
        self.switch_waveform.setToolTip(self.tr("波形显示"))
        self.switch_waveform.checkedChanged.connect(self._on_waveform_visibility_changed)
        bottom_layout.addWidget(self.switch_waveform)

        layout.addWidget(self._bottom_bar)

        # 波形打开时可直接拖动底边改变显示高度。高度仍写回高级设置使用的
        # waveform/spectrum 两个键，因此下次启动和设置弹窗都保持一致。
        self._height_handle = _TimelineHeightHandle(self)
        self._height_handle.drag_started.connect(self._begin_height_drag)
        self._height_handle.drag_moved.connect(self._move_height_drag)
        self._height_handle.drag_finished.connect(self._finish_height_drag)
        # 作为底边覆盖层而非布局项，不额外抬高窗口最小高度；命中带向内
        # 覆盖 8px，视觉线仍落在时间轴与预览的实际交界处。
        self._position_height_handle()

    def sizeHint(self):
        """高度提示: 波形可见 = 显示区 minimumHeight + 底栏; 隐藏 = 仅底栏.

        用逻辑状态 _waveform_visible 而非 isVisible()——后者在父控件
        未 show 时恒为 False. minimumHeight 由 _apply_display_height 设置
        为用户期望的显示高度（同时是布局领取空间的手段）.
        """
        from PyQt6.QtCore import QSize

        bar = (
            self._bottom_bar.minimumHeight()
            if hasattr(self, "_bottom_bar") else 28
        )
        spacing = self.layout().spacing() if self.layout() is not None else 2
        if self._waveform_visible:
            height = (
                self.waveform_display.minimumHeight() + bar + spacing + 4
            )
        else:
            height = bar + 4  # 仅底栏
        return QSize(super().sizeHint().width(), height)

    def actual_spectrum_display_height(self) -> int:
        """当前实际显示的声谱区高度（弹窗提示用，区别于期望高度）。"""
        return self.waveform_display.height()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_height_handle()

    def _position_height_handle(self) -> None:
        handle = getattr(self, "_height_handle", None)
        if handle is None:
            return
        handle.setGeometry(
            0,
            max(0, self.height() - handle.height()),
            self.width(),
            handle.height(),
        )
        handle.raise_()

    def bottom_bar_height(self) -> int:
        """底部控制栏（缩放/滚动/齿轮/开关）的当前高度。"""
        return self._bottom_bar.height() or self._bottom_bar.minimumHeight()

    def set_spectrum_active(self, active: bool) -> None:
        """波形区显示开关变化（与全局可见性相与后下发到显示区）。"""
        self._spectrum_requested = active
        self._sync_spectrum_activity()

    def set_app_visible(self, visible: bool) -> None:
        """应用/窗口可见性变化（最小化、切后台）——不可见时暂停在途计算，
        回到前台且仍处于声谱模式时按需恢复。"""
        if visible == self._app_visible:
            return
        self._app_visible = visible
        self._sync_spectrum_activity()

    def _sync_spectrum_activity(self) -> None:
        self.waveform_display.set_spectrum_active(
            self._spectrum_requested and self._app_visible
        )

    def eventFilter(self, obj, event):
        if (
            obj is self.waveform_display
            and event.type() == QEvent.Type.Resize
        ):
            self.actual_spectrum_height_changed.emit(self.waveform_display.height())
        return super().eventFilter(obj, event)

    def set_duration(self, ms: int):
        self._duration_ms = ms
        self.waveform_display.set_duration(ms)

    def set_position(self, ms: int):
        self.waveform_display.set_position(ms)

    def set_playback_range(
        self, start_ms: Optional[int], end_ms: Optional[int]
    ) -> None:
        self.waveform_display.set_playback_range(start_ms, end_ms)

    def set_time_tags(self, tags: List[Tuple[int, str, int, int, int, bool, Optional[str]]]):
        self.waveform_display.set_time_tags(tags)

    def set_tag_edit_enabled(self, enabled: bool) -> None:
        self.waveform_display.set_tag_edit_enabled(enabled)

    def try_append_tag(self, ts: int, char: str, line_idx: int, char_idx: int,
                       cp_idx: int, is_end: bool, ruby: Optional[str]) -> bool:
        return self.waveform_display.try_append_tag(
            ts, char, line_idx, char_idx, cp_idx, is_end, ruby)

    def try_add_tag(self, ts: int, char: str, line_idx: int, char_idx: int,
                    cp_idx: int, is_end: bool, ruby: Optional[str]) -> bool:
        return self.waveform_display.try_add_tag(
            ts, char, line_idx, char_idx, cp_idx, is_end, ruby)

    def set_tag_char_enabled(self, enabled: bool) -> None:
        self.waveform_display.set_tag_char_enabled(enabled)

    def set_tag_ruby_enabled(self, enabled: bool) -> None:
        self.waveform_display.set_tag_ruby_enabled(enabled)

    def clear_tag_selection(self) -> None:
        self.waveform_display.clear_tag_selection()

    def set_audio_data(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        mono: Optional[np.ndarray] = None,
    ):
        self.waveform_display.set_audio_data(samples, sample_rate, channels, mono=mono)
        # 缩放上限随音频长度变化：重算滑杆位置/读数/滚动条长度
        self._on_zoom_changed(self.waveform_display._zoom_factor)
        # 非模态弹窗可能保持打开：立即推送新音源 + 刷新实际重叠率提示
        dialog = getattr(self, "_advanced_dialog", None)
        if dialog is not None:
            dialog.set_audio_source(self.waveform_display.spectrum_audio_source())
            dialog.refresh_overlap_hint(self.waveform_display.display_settings())

    def clear_audio_data(self):
        self.waveform_display.clear_audio_data()
        dialog = getattr(self, "_advanced_dialog", None)
        if dialog is not None:
            dialog.set_audio_source(None)
            dialog.refresh_overlap_hint(self.waveform_display.display_settings())
        self._audio_name_is_placeholder = True
        self.lbl_audio_name.setText(self.tr("未加载音频"))

    def set_audio_name(self, name: str):
        """设置音频文件名称显示"""
        self._audio_name_is_placeholder = False
        self.lbl_audio_name.setText(name)

    def set_playing(self, playing: bool) -> None:
        self.waveform_display.set_playing(playing)

    def set_center_playhead_mode(self, enabled: bool) -> None:
        self.waveform_display.set_center_playhead_mode(enabled)

    def set_zoom_enabled(self, enabled: bool) -> None:
        self._zoom_enabled = enabled
        self.waveform_display.set_zoom_enabled(enabled)
        self.zoom_slider.setEnabled(enabled)
        self.zoom_label.setEnabled(enabled)

    # ---- 缩放对数刻度转换（slider 0-10000 → 1x..上限；上限随音频长度动态，
    # 无音频时为 _MAX_ZOOM，长音频可到采样级——见 WaveformDisplay.zoom_cap）----

    def _slider_to_zoom(self, value: int) -> float:
        """滑条整数值 → 实际放大倍数（对数映射，上限随音频长度动态）。"""
        cap = self.waveform_display.zoom_cap()
        return cap ** (value / 10000.0)

    def _zoom_to_slider(self, zoom: float) -> int:
        """实际放大倍数 → 滑条整数值（对数映射，上限随音频长度动态）。"""
        cap = self.waveform_display.zoom_cap()
        zoom = max(1.0, min(cap, zoom))
        return int(round(math.log(zoom) / math.log(cap) * 10000))

    # ---- 回调 ----

    def _on_zoom_changed(self, zoom: float):
        # 用标志位而非 blockSignals：要让 Slider 的 valueChanged→_adjustHandlePos
        # 正常触发（抓手跟随），仅抑制我们自己的 _on_zoom_slider_changed 回环。
        self._suppress_zoom_slider = True
        self.zoom_slider.setValue(self._zoom_to_slider(zoom))
        self._suppress_zoom_slider = False
        self.zoom_label.setText(f"{zoom:.1f}x")
        # 缩放变化 → 可见窗占比变化 → 刷新滑块长度
        self._update_scroll_bar_metrics()

    def _on_scroll_changed(self, position: float):
        self.scroll_bar.blockSignals(True)
        self.scroll_bar.setValue(int(round(position * self._SCROLL_SCALE)))
        self.scroll_bar.blockSignals(False)

    def _update_scroll_bar_metrics(self):
        """根据当前缩放刷新横向滚动条的 pageStep / range / value。

        滑块长度 = pageStep / (range跨度 + pageStep) = (1/zoom)，即可见时间窗占
        整段时长的比例：1x 时填满整条，放大越多滑块越短（下限由 QSS min-width 兜底）。
        """
        if not hasattr(self, "scroll_bar"):
            return
        zoom = max(1.0, getattr(self.waveform_display, "_zoom_factor", 1.0))
        page = max(1, int(round(self._SCROLL_SCALE / zoom)))
        position = getattr(self.waveform_display, "_scroll_position", 0.0)
        self.scroll_bar.blockSignals(True)
        self.scroll_bar.setPageStep(page)
        self.scroll_bar.setSingleStep(max(1, page // 10))
        self.scroll_bar.setRange(0, self._SCROLL_SCALE - page)
        self.scroll_bar.setValue(int(round(position * self._SCROLL_SCALE)))
        self.scroll_bar.blockSignals(False)

    def _on_zoom_slider_changed(self, value: int):
        if self._suppress_zoom_slider or not self._zoom_enabled:
            return
        self.waveform_display._suspend_auto_scroll()
        zoom = self._slider_to_zoom(value)
        self.waveform_display.set_zoom(zoom)
        self.zoom_label.setText(f"{zoom:.1f}x")
        # set_zoom 不发 zoom_changed 信号（仅 Ctrl+滚轮缩放才发），故缩放滑条
        # 改变后需在此显式刷新滚动条 pageStep/range，使滑块长度跟随缩放比例。
        self._update_scroll_bar_metrics()

    def _commit_zoom_text(self, text: str) -> None:
        """Apply a multiplier entered in the zoom value label."""
        value = text.strip()
        if value.lower().endswith("x"):
            value = value[:-1].strip()
        try:
            zoom = float(value)
        except (TypeError, ValueError):
            self.zoom_label.setText(
                f"{self.waveform_display._zoom_factor:.1f}x"
            )
            return
        if not math.isfinite(zoom):
            self.zoom_label.setText(
                f"{self.waveform_display._zoom_factor:.1f}x"
            )
            return
        zoom = max(1.0, min(self.waveform_display.zoom_cap(), zoom))
        self.waveform_display._suspend_auto_scroll()
        self.waveform_display.set_zoom(zoom)
        self._on_zoom_changed(zoom)

    def _on_scroll_bar_changed(self, value: int):
        self.waveform_display._suspend_auto_scroll()
        position = value / self._SCROLL_SCALE
        self.waveform_display.set_scroll_position(position)

    def _apply_waveform_visibility(self, checked: bool) -> None:
        """统一可见性处理入口: 只在此处改状态/发信号."""
        if checked == self._waveform_visible:
            return  # 相同状态的重复设置直接返回(不发信号)
        self._waveform_visible = checked
        self.waveform_display.setVisible(checked)
        self._height_handle.setVisible(checked)
        # 不直接下发 set_spectrum_active——那会绕过 _app_visible 后台门禁
        #(应用后台时切开关会重启声谱 worker). 统一走 _sync_spectrum_activity.
        self._sync_spectrum_activity()
        # 波形隐藏/恢复后时间轴高度变化, 需重新协商布局
        self.updateGeometry()
        self.waveform_visibility_changed.emit(checked)

    def _on_waveform_visibility_changed(self, checked: bool):
        self._apply_waveform_visibility(checked)

    def is_waveform_visible(self) -> bool:
        return self._waveform_visible

    def set_waveform_visible(self, visible: bool):
        if visible == self._waveform_visible:
            return  # 去重(P3-1: 不发重复信号)
        # 只改开关状态, 由 checkedChanged -> _on_waveform_visibility_changed
        # -> _apply_waveform_visibility 统一处理(信号只发一次)
        self.switch_waveform.setChecked(visible)

    # ---- 底边拖拽调整显示高度 ----

    def _begin_height_drag(self) -> None:
        self._resize_start_settings = dict(self.display_settings())
        mode = self._resize_start_settings.get("display_mode", "waveform")
        if mode == "dual":
            self._resize_start_height = (
                int(self._resize_start_settings["waveform_display_height"])
                + int(self._resize_start_settings["spectrum_display_height"])
            )
        else:
            self._resize_start_height = int(
                self._resize_start_settings["display_height"]
            )

    def _move_height_drag(self, delta: int) -> None:
        if self._resize_start_settings is None:
            return
        self._set_dragged_display_height(self._resize_start_height + int(delta))

    def _finish_height_drag(self, delta: int) -> None:
        before = self._resize_start_settings
        if before is None:
            return
        self._set_dragged_display_height(self._resize_start_height + int(delta))
        self._resize_start_settings = None
        self._commit_display_settings_change(before)

    def _commit_display_settings_change(self, before: dict) -> None:
        """拖拽类调整的收尾：有实际变化才持久化并同步齿轮弹窗。"""
        after = self.display_settings()
        if after == before:
            return
        # 拖动中只重排布局；松手时才一次性持久化，避免每个像素都写配置。
        self.display_settings_changed.emit(dict(after))
        dialog = getattr(self, "_advanced_dialog", None)
        if dialog is not None and hasattr(dialog, "sync_display_heights"):
            dialog.sync_display_heights(after)

    # ---- 双谱交界拖拽（只改两 lane 比例，总高不变 → 布局不跳动） ----

    def _begin_lane_resize(self) -> None:
        display = self.waveform_display
        if display._display_mode != "dual":
            return
        h = display.height()
        wave_h, _ = display._dual_lane_heights(h)
        self._lane_resize_start = {
            "settings": dict(display.display_settings()),
            "boundary": wave_h,
            "height": h,
        }

    def _move_lane_resize(self, delta: int) -> None:
        state = self._lane_resize_start
        if state is None:
            return
        h = int(state["height"])
        start = state["settings"]
        total = int(start["waveform_display_height"]) + int(
            start["spectrum_display_height"]
        )
        if h <= 0 or total <= 0:
            return
        # 反解期望高度：实际交界 ∝ 期望比例，压缩布局下交界仍精确跟手；
        # 期望总值不变 → minimumHeight 不变，拖拽中不触发布局协商
        wave = int(round(total * (int(state["boundary"]) + int(delta)) / h))
        wave = max(_WAVEFORM_MIN_DISPLAY_HEIGHT, min(_MAX_DISPLAY_HEIGHT, wave))
        wave = max(wave, total - _MAX_DISPLAY_HEIGHT)   # 声谱 ≤ 400
        wave = min(wave, total - _MIN_DISPLAY_HEIGHT)   # 声谱 ≥ 120
        self.waveform_display.set_spectrum_params(
            waveform_display_height=wave,
            spectrum_display_height=total - wave,
        )

    def _finish_lane_resize(self, delta: int) -> None:
        state = self._lane_resize_start
        if state is None:
            return
        self._move_lane_resize(delta)
        self._lane_resize_start = None
        self._commit_display_settings_change(state["settings"])

    def _set_dragged_display_height(self, requested: int) -> None:
        """应用拖拽目标高度；双谱按原比例分配并同时满足两栏边界。"""
        start = self._resize_start_settings
        if start is None:
            return
        mode = start.get("display_mode", "waveform")
        if mode == "dual":
            # 双谱总高下限 = 波形下限(60) + 声谱下限(120)；两 lane 分配时
            # 波形可压到 60，声谱仍保 120。
            total = max(
                _WAVEFORM_MIN_DISPLAY_HEIGHT + _MIN_DISPLAY_HEIGHT,
                min(_MAX_DISPLAY_HEIGHT * 2, int(requested)),
            )
            start_wave = int(start["waveform_display_height"])
            start_spec = int(start["spectrum_display_height"])
            start_total = max(1, start_wave + start_spec)
            wave = int(round(total * start_wave / start_total))
            wave_low = max(_WAVEFORM_MIN_DISPLAY_HEIGHT, total - _MAX_DISPLAY_HEIGHT)
            wave_high = min(_MAX_DISPLAY_HEIGHT, total - _MIN_DISPLAY_HEIGHT)
            wave = max(wave_low, min(wave_high, wave))
            self.set_spectrum_params(
                waveform_display_height=wave,
                spectrum_display_height=total - wave,
            )
        elif mode == "spectrum":
            self.set_spectrum_params(spectrum_display_height=requested)
        else:
            self.set_spectrum_params(waveform_display_height=requested)

    # ---- 显示模式 / 高级设置（齿轮对话框） ----

    def _on_waveform_settings_clicked(self):
        from strange_uta_game.frontend.editor.timing.waveform_advanced_dialog import (
            WaveformAdvancedDialog,
        )

        dialog = self._advanced_dialog
        actual = self.actual_spectrum_display_height()
        if dialog is not None:
            # 已打开（可能只是被用户关闭隐藏）：刷新音源/实际高度后重新显示
            dialog.set_audio_source(self.waveform_display.spectrum_audio_source())
            dialog.set_height_cap(actual)
            dialog.refresh_overlap_hint(self.waveform_display.display_settings())
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        # 普通非模态顶层窗口：以主窗口为锚点定位/管理生命周期，不遮挡其他区域
        dialog = WaveformAdvancedDialog(
            self.waveform_display.display_settings(),
            self.waveform_display.spectrum_audio_source(),
            parent=self.window(),
        )
        dialog.applied.connect(self._apply_display_settings)
        dialog.destroyed.connect(self._on_advanced_dialog_destroyed)
        # 实际显示高度变化 → 弹窗「实际 N px」提示实时同步
        #（拖滑条/窗口变化/预览让位都会改显示区高度）
        self.actual_spectrum_height_changed.connect(dialog.set_height_cap)
        self._advanced_dialog = dialog  # 强引用，销毁前不回收
        dialog.set_height_cap(actual)  # 首开也要立即提示当前实际显示高度
        dialog.show()

    def _on_advanced_dialog_destroyed(self, *_args) -> None:
        self._advanced_dialog = None

    def _apply_display_settings(self, settings: dict):
        """把对话框（或持久化设置）施加到波形区；实际变化时向外发持久化信号。"""
        wd = self.waveform_display
        before = wd.display_settings()
        wd.set_display_mode(settings.get("display_mode", "waveform"))
        wd.set_grid_mode(settings.get("grid_mode", "time"))
        wd.set_grid_bpm(float(settings.get("grid_bpm", 120.0)))
        wd.set_grid_offset(int(settings.get("grid_offset_ms", 0)))
        beats_per_bar = settings.get("beats_per_bar")
        if beats_per_bar is not None:
            wd.set_beats_per_bar(int(beats_per_bar))
        wd.set_grid_line_width(int(settings.get("grid_line_width", 2)))
        wd.set_waveform_rms_enabled(bool(settings.get("waveform_rms_enabled", True)))
        # 时间标签行为键（设置页「波形时间标签」组也走这里，保证两处联动）
        tag_edit = settings.get("tag_edit_enabled")
        if tag_edit is not None:
            wd.set_tag_edit_enabled(bool(tag_edit))
        center_playhead = settings.get("center_playhead_enabled")
        if center_playhead is not None:
            wd.set_center_playhead_mode(bool(center_playhead))
        tag_char = settings.get("tag_char_enabled")
        if tag_char is not None:
            wd.set_tag_char_enabled(bool(tag_char))
        tag_ruby = settings.get("tag_ruby_enabled")
        if tag_ruby is not None:
            wd.set_tag_ruby_enabled(bool(tag_ruby))
        # 节拍器两键（纯状态）：缺席时保持现值（兼容旧调用方）
        metronome_enabled = settings.get("metronome_enabled")
        if metronome_enabled is not None:
            wd.set_metronome_enabled(bool(metronome_enabled))
        metronome_volume = settings.get("metronome_volume")
        if metronome_volume is not None:
            wd.set_metronome_volume(int(metronome_volume))
        # 加载后自动检测 BPM 开关（纯状态）：缺席时保持现值（兼容旧调用方）
        auto_bpm = settings.get("auto_bpm_on_load")
        if auto_bpm is not None:
            wd.set_auto_bpm_enabled(bool(auto_bpm))
        wd.set_spectrum_params(
            fft_size=settings.get("spectrum_fft_size"),
            overlap=settings.get("spectrum_overlap"),
            freq_scale=settings.get("spectrum_freq_scale"),
            dyn_range_db=settings.get("spectrum_dyn_range_db"),
            display_height=settings.get("display_height"),
            freq_min_hz=settings.get("spectrum_freq_min_hz"),
            freq_max_hz=settings.get("spectrum_freq_max_hz"),
            colormap=settings.get("spectrum_colormap"),
            waveform_display_height=settings.get("waveform_display_height"),
            spectrum_display_height=settings.get("spectrum_display_height"),
        )
        after = wd.display_settings()
        if after != before:
            self.display_settings_changed.emit(dict(after))
        # 刷新弹窗的实际重叠率提示（预算可能在本次设置变化后降/升级）
        dialog = getattr(self, "_advanced_dialog", None)
        if dialog is not None and hasattr(dialog, "refresh_overlap_hint"):
            dialog.refresh_overlap_hint(after)
        # 快捷键或设置恢复也能改变显示模式；同步打开弹窗的药丸选中态与
        # 模式专属内容，sync 内部阻断信号以避免 applied 回环。
        if dialog is not None and hasattr(dialog, "sync_display_mode"):
            dialog.sync_display_mode(after)
        # 设置页改了时间标签键时同步弹窗开关（弹窗自身改动值相同，为空操作）
        if dialog is not None and hasattr(dialog, "sync_tag_settings"):
            dialog.sync_tag_settings(after)

    def set_display_mode(self, mode: str) -> None:
        self.waveform_display.set_display_mode(mode)

    def display_mode(self) -> str:
        """当前显示模式（waveform / spectrum / dual）；预览让位协商用。"""
        return self.waveform_display._display_mode

    def set_waveform_rms_enabled(self, enabled: bool) -> None:
        """双层波形开关：转发到显示区（TimelineWidget 自身无渲染状态）。"""
        self.waveform_display.set_waveform_rms_enabled(enabled)

    def set_grid_mode(self, mode: str) -> None:
        self.waveform_display.set_grid_mode(mode)

    def set_grid_bpm(self, bpm: float) -> None:
        self.waveform_display.set_grid_bpm(bpm)

    def set_grid_offset(self, offset_ms: int) -> None:
        self.waveform_display.set_grid_offset(offset_ms)

    def set_spectrum_params(
        self,
        fft_size: Optional[int] = None,
        overlap: Optional[float] = None,
        freq_scale: Optional[str] = None,
        dyn_range_db: Optional[int] = None,
        display_height: Optional[int] = None,
        freq_min_hz: Optional[int] = None,
        freq_max_hz: Optional[int] = None,
        colormap: Optional[str] = None,
        waveform_display_height: Optional[int] = None,
        spectrum_display_height: Optional[int] = None,
    ) -> None:
        self.waveform_display.set_spectrum_params(
            fft_size, overlap, freq_scale, dyn_range_db, display_height,
            freq_min_hz, freq_max_hz, colormap,
            waveform_display_height, spectrum_display_height,
        )

    def display_settings(self) -> dict:
        return self.waveform_display.display_settings()
