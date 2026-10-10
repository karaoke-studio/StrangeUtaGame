"""多项目拼接对话框。

每首 SUG 渲染为一张可拖拽重排的 SimpleCardWidget 卡片，
容器使用 DragWidget 模式，支持可视拖拽指示器与 QPixmap 拖拽预览。
项目音频长度以 MM:SS.ms 格式显示/输入。
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from PyQt6.QtCore import Qt, QMimeData, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QDrag, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SimpleCardWidget,
    TransparentToolButton,
)

from strange_uta_game.frontend.font_utils import ui_font
from strange_uta_game.frontend.fluent_widgets import FluentGroupBox
from strange_uta_game.frontend.window_sizing import fit_to_screen


# ── mm:ss.ms ⇔ ms 互转 ──


def _ms_to_mmssms(ms: int) -> str:
    """毫秒 → "MM:SS.ms" 格式字符串。"""
    if ms <= 0:
        ms = 0
    total_sec = ms / 1000.0
    minutes = int(total_sec // 60)
    seconds = total_sec % 60
    return f"{minutes:02d}:{seconds:06.3f}"


def _mmssms_to_ms(text: str) -> int:
    """"MM:SS.ms" 格式字符串 → 毫秒；解析失败返回 0。"""
    text = text.strip()
    if not text:
        return 0
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                m = int(parts[0])
                s = float(parts[1])
                return int(m * 60000 + s * 1000)
        return int(float(text))
    except (ValueError, TypeError):
        return 0


def _parse_int(text: str, default: int = 0) -> int:
    try:
        return int(text.strip())
    except (ValueError, TypeError):
        return default


# ── SugEntry ──


@dataclass
class SugEntry:
    """拼接列表中的一个SUG条目。"""

    file_path: str = ""
    offset_ms: int = 0
    duration_ms: int = 0
    gap_ms: int = 300
    title: str = ""
    media_path: str = ""

    @property
    def name(self) -> str:
        return Path(self.file_path).stem if self.file_path else ""

    @property
    def display_name(self) -> str:
        return self.title or self.name or Path(self.file_path).name


def _read_sug_entry(file_path: str) -> SugEntry:
    """从SUG文件读取元信息（不解析完整项目）。"""
    entry = SugEntry(file_path=file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return entry

    entry.title = data.get("metadata", {}).get("title", "") or ""
    entry.offset_ms = int(data.get("global_offset_ms") or 0)
    entry.duration_ms = int(data.get("audio_duration_ms") or 0)
    entry.media_path = data.get("media_path", "") or ""

    if entry.duration_ms == 0 and entry.media_path and Path(entry.media_path).exists():
        from strange_uta_game.frontend.editor.timing.sug_concat_worker import (
            _probe_audio_duration_multi,
        )
        entry.duration_ms = _probe_audio_duration_multi(entry.media_path)

    return entry


# ── 拖拽容器与卡片 ──

_DRAG_MIME = "application/x-sug-concat-item"


class _DragTargetIndicator(QWidget):
    """拖拽时显示的目标位置指示器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.setStyleSheet(
            "background-color: rgba(0, 120, 212, 0.7); border-radius: 2px;"
        )
        self.hide()


class _SugCard(SimpleCardWidget):
    """单首 SUG 条目卡片，可整体拖拽。

    卡片内容：[拖拽柄] 序号 文件名 | 原项目偏移 | 项目音频长度 | 间隔(ms) | [✕]
    """

    removed = None  # 外部注入: callable(index)

    def __init__(self, entry: SugEntry, index: int, parent=None):
        super().__init__(parent)
        self._entry = entry
        self._index = index
        self.setFixedHeight(44)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(8)

        # 拖拽柄
        grip = QLabel("≡")
        grip.setFixedWidth(16)
        grip.setToolTip(self.tr("拖拽调整顺序"))
        grip.setStyleSheet("color: #999999; font-size: 16px;")
        grip.setCursor(Qt.CursorShape.OpenHandCursor)
        self._grip = grip
        row.addWidget(grip)

        # 序号
        self.lbl_idx = BodyLabel(str(index + 1))
        self.lbl_idx.setFixedWidth(24)
        self.lbl_idx.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.lbl_idx)

        # 文件名
        self.lbl_name = BodyLabel(entry.display_name)
        self.lbl_name.setMinimumWidth(100)
        self.lbl_name.setToolTip(entry.file_path)
        row.addWidget(self.lbl_name, stretch=1)

        # 原项目偏移
        self.edit_offset = LineEdit(self)
        self.edit_offset.setText(str(entry.offset_ms))
        self.edit_offset.setFixedWidth(80)
        self.edit_offset.setToolTip("ms")
        row.addWidget(self.edit_offset)

        # 项目音频长度
        self.edit_duration = LineEdit(self)
        self.edit_duration.setText(_ms_to_mmssms(entry.duration_ms))
        self.edit_duration.setToolTip(self.tr("格式: MM:SS.ms  或纯毫秒数"))
        self.edit_duration.setFixedWidth(96)
        row.addWidget(self.edit_duration)

        # 与下一首的间隔
        self.edit_gap = LineEdit(self)
        self.edit_gap.setText(str(entry.gap_ms))
        self.edit_gap.setFixedWidth(110)
        self.edit_gap.setToolTip("ms")
        row.addWidget(self.edit_gap)

        # 移除按钮（使用 FluentIcon.CLOSE 的透明工具按钮，主题感知清晰可见）
        self.btn_rm = TransparentToolButton(FIF.CLOSE, self)
        self.btn_rm.setFixedSize(28, 28)
        self.btn_rm.setToolTip(self.tr("移除"))
        self.btn_rm.clicked.connect(self._on_remove)
        row.addWidget(self.btn_rm)

    def _on_remove(self):
        if self.removed is not None:
            self.removed(self._index)

    def update_index(self, new_index: int):
        self._index = new_index
        self.lbl_idx.setText(str(new_index + 1))

    def mouseMoveEvent(self, e):
        """从拖拽柄发起拖拽。"""
        if e.buttons() != Qt.MouseButton.LeftButton:
            return
        # 仅在拖拽柄区域发起拖拽
        grip_rect = self._grip.geometry()
        if not grip_rect.contains(e.position().toPoint()):
            return

        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_DRAG_MIME, str(self._index).encode())
        drag.setMimeData(mime)

        # 生成拖拽预览图（2x 分辨率以适配 HiDPI）
        pix = QPixmap(self.width() * 2, self.height() * 2)
        pix.setDevicePixelRatio(2)
        self.render(pix)
        drag.setPixmap(pix)
        drag.setHotSpot(e.position().toPoint())

        drag.exec(Qt.DropAction.MoveAction)
        self.show()  # 防止拖出后被隐藏

    def collect(self) -> SugEntry:
        offset_text = self.edit_offset.text().strip()
        duration_text = self.edit_duration.text().strip()
        gap_text = self.edit_gap.text().strip()
        return SugEntry(
            file_path=self._entry.file_path,
            title=self._entry.title,
            media_path=self._entry.media_path,
            offset_ms=_parse_int(offset_text, 0),
            duration_ms=_mmssms_to_ms(duration_text) if ":" in duration_text else _parse_int(duration_text, 0),
            gap_ms=_parse_int(gap_text, 300),
        )


class _DragContainer(QWidget):
    """接受卡片拖拽重排的容器。

    实现基于 https://www.pythonguis.com/faq/pyqt6-drag-drop-widgets/
    带 DragTargetIndicator 可视放置位置指示器。
    """

    order_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self._indicator = _DragTargetIndicator(self)
        self._layout.addWidget(self._indicator)
        self._layout.addStretch()

        # 拖拽到边缘时自动滚动父 ScrollArea
        self._auto_scroll_timer = QTimer(self)
        self._auto_scroll_timer.setInterval(50)
        self._auto_scroll_timer.timeout.connect(self._do_auto_scroll)
        self._auto_scroll_dx = 0
        self._auto_scroll_dy = 0

    def _scroll_area(self):
        p = self.parent()
        while p is not None:
            if isinstance(p, ScrollArea):
                return p
            p = p.parent()
        return None

    def _start_auto_scroll(self, pos: QPoint | None):
        """根据鼠标在视口内的位置决定滚动方向与速度。

        距上下边缘 60px 内开始滚动，越靠近边缘速度越快。
        """
        sa = self._scroll_area()
        if sa is None or pos is None:
            self._auto_scroll_dy = 0
            self._auto_scroll_timer.stop()
            return
        vp = sa.viewport()
        vp_h = vp.height() if vp else 0
        y = pos.y()
        edge = 60
        if y < edge and vp_h > 0:
            self._auto_scroll_dy = -max(2, int((edge - y) / 6))
        elif y > vp_h - edge and vp_h > 0:
            self._auto_scroll_dy = max(2, int((y - (vp_h - edge)) / 6))
        else:
            self._auto_scroll_dy = 0
        if self._auto_scroll_dy != 0:
            if not self._auto_scroll_timer.isActive():
                self._auto_scroll_timer.start()
        else:
            self._auto_scroll_timer.stop()

    def _do_auto_scroll(self):
        sa = self._scroll_area()
        if sa is None or self._auto_scroll_dy == 0:
            self._auto_scroll_timer.stop()
            return
        bar = sa.verticalScrollBar()
        if bar is None:
            return
        bar.setValue(bar.value() + self._auto_scroll_dy)

    def _stop_auto_scroll(self):
        self._auto_scroll_dy = 0
        self._auto_scroll_timer.stop()

    def add_card(self, card: _SugCard, index: int = -1):
        if index < 0 or index >= self._layout.count() - 1:
            # 插入到 stretch 之前
            self._layout.insertWidget(self._layout.count() - 1, card)
        else:
            self._layout.insertWidget(index, card)

    def remove_card(self, card: _SugCard):
        self._layout.removeWidget(card)

    def card_index_in_layout(self, card: _SugCard) -> int:
        return self._layout.indexOf(card)

    # ── 拖拽事件 ──

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(_DRAG_MIME):
            e.accept()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._indicator.hide()
        self._stop_auto_scroll()
        e.accept()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(_DRAG_MIME):
            e.ignore()
            return
        # 视口坐标用于自动滚动判断
        # QDragMoveEvent 只有 position()（相对于本容器），需经全局坐标桥接到
        # ScrollArea viewport 坐标系（两者不在同一 parent 层级，直接 mapFrom 会报错）。
        sa = self._scroll_area()
        if sa is not None:
            vp = sa.viewport()
            if vp is not None:
                global_pt = self.mapToGlobal(e.position().toPoint())
                vp_pos = vp.mapFromGlobal(global_pt)
                self._start_auto_scroll(vp_pos)
        index = self._find_drop_location(e)
        if index is not None:
            self._layout.insertWidget(index, self._indicator)
            source = e.source()
            if source is not None and isinstance(source, _SugCard):
                source.hide()
            self._indicator.show()
        e.accept()

    def dropEvent(self, e):
        if not e.mimeData().hasFormat(_DRAG_MIME):
            e.ignore()
            return
        source = e.source()
        if not isinstance(source, _SugCard):
            e.ignore()
            return

        self._indicator.hide()
        self._stop_auto_scroll()
        index = self._layout.indexOf(self._indicator)
        # 从原位置移除，插入到目标位置
        self._layout.removeWidget(source)
        if index >= 0:
            self._layout.insertWidget(index, source)
        else:
            self._layout.insertWidget(self._layout.count() - 1, source)
        source.show()
        self._layout.activate()
        e.accept()
        self.order_changed.emit()

    def _find_drop_location(self, e) -> int | None:
        pos = e.position()
        spacing = self._layout.spacing() / 2
        for n in range(self._layout.count()):
            item = self._layout.itemAt(n)
            w = item.widget() if item else None
            if w is None or w is self._indicator or not w.isVisible():
                continue
            drop_here = (
                pos.y() >= w.y() - spacing
                and pos.y() <= w.y() + w.size().height() + spacing
            )
            if drop_here:
                return n
        return None


# ── 主对话框 ──


class SugConcatDialog(QDialog):
    """多项目拼接对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("多项目拼接"))
        fit_to_screen(self, 700, 720)
        self.setFont(ui_font(10))
        self._apply_clicked = False
        self._cards: list[_SugCard] = []

        self._init_ui()
        self._load_saved_settings()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 说明 ──
        desc = CaptionLabel(
            self.tr(
                "选择多个SUG文件进行拼接。自动读取每个SUG的原项目偏移、音频长度。\n"
                "可拖拽卡片调整顺序，编辑偏移/长度/间隔后生成新SUG项目。"
            )
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ── 工具栏 ──
        toolbar = QHBoxLayout()
        btn_add = PushButton(self.tr("添加SUG文件"), self)
        btn_add.setIcon(FIF.ADD)
        btn_add.clicked.connect(self._on_add_files)
        toolbar.addWidget(btn_add)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        # ── 项目列表 FluentGroupBox ──
        list_group = FluentGroupBox(self.tr("项目列表"))
        list_layout = list_group.contentLayout

        # 表头行：列标题与卡片内各列严格对齐
        # 卡片行 = contentsMargins(12,4,12,4) + spacing(8)
        # 顺序：grip(16) idx(24) name(stretch) offset(80) duration(96) gap(110) rm(28)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(12, 0, 12, 0)
        header_row.setSpacing(8)

        hdr_grip = CaptionLabel("")  # 占位与拖拽柄对齐
        hdr_grip.setFixedWidth(16)
        header_row.addWidget(hdr_grip)

        hdr_idx = CaptionLabel("#")
        hdr_idx.setFixedWidth(24)
        hdr_idx.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(hdr_idx)

        hdr_name = CaptionLabel(self.tr("文件名"))
        header_row.addWidget(hdr_name, stretch=1)

        hdr_off = CaptionLabel(self.tr("原项目偏移"))
        hdr_off.setFixedWidth(80)
        hdr_off.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(hdr_off)

        hdr_dur = CaptionLabel(self.tr("项目音频长度"))
        hdr_dur.setFixedWidth(96)
        hdr_dur.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(hdr_dur)

        hdr_gap = CaptionLabel(self.tr("与下一首的间隔"))
        hdr_gap.setFixedWidth(110)
        hdr_gap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_row.addWidget(hdr_gap)

        hdr_act = CaptionLabel("")
        hdr_act.setFixedWidth(28)
        header_row.addWidget(hdr_act)

        list_layout.addLayout(header_row)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        # 去掉 ScrollArea 默认边框，使内部卡片与表头严格左对齐
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._container = _DragContainer()
        # 拖拽排序信号只在初始化时连接一次（_add_card 每次连接会重复触发）
        self._container.order_changed.connect(self._on_order_changed)
        scroll.setWidget(self._container)

        self._empty_hint = CaptionLabel(
            self.tr("尚未添加SUG文件，请点击「添加SUG文件」选择文件。")
        )
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._container._layout.insertWidget(0, self._empty_hint)

        list_layout.addWidget(scroll)
        layout.addWidget(list_group, stretch=1)

        # ── 输出设置 ──
        bottom = FluentGroupBox(self.tr("输出设置"))
        bot = bottom.contentLayout

        # 全局默认间隔
        gap_row = QHBoxLayout()
        gap_row.addWidget(BodyLabel(self.tr("全局默认间隔:")))
        self.edit_default_gap = LineEdit(self)
        self.edit_default_gap.setText("300")
        self.edit_default_gap.setFixedWidth(64)
        self.edit_default_gap.setToolTip("ms")
        gap_row.addWidget(self.edit_default_gap)
        gap_row.addWidget(CaptionLabel("ms"))
        btn_apply = PushButton(self.tr("应用到全部"), self)
        btn_apply.clicked.connect(self._on_apply_global_gap)
        gap_row.addWidget(btn_apply)
        gap_row.addStretch()
        bot.addLayout(gap_row)

        # 拼接后文件名
        name_row = QHBoxLayout()
        name_row.addWidget(BodyLabel(self.tr("拼接后文件名:")))
        self.edit_output_name = LineEdit(self)
        self.edit_output_name.setPlaceholderText(self.tr("默认为第一首SUG名称+……"))
        name_row.addWidget(self.edit_output_name, stretch=1)
        name_row.addWidget(CaptionLabel(".sug"))
        bot.addLayout(name_row)

        # 最终项目偏移
        off_row = QHBoxLayout()
        off_row.addWidget(BodyLabel(self.tr("最终项目偏移:")))
        self.edit_final_offset = LineEdit(self)
        self.edit_final_offset.setText("0")
        self.edit_final_offset.setFixedWidth(64)
        self.edit_final_offset.setToolTip("ms")
        off_row.addWidget(self.edit_final_offset)
        off_row.addWidget(CaptionLabel("ms"))
        off_row.addStretch()
        bot.addLayout(off_row)

        layout.addWidget(bottom)

        # ── 按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = PushButton(self.tr("取消"), self)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        apply_btn = PrimaryPushButton(self.tr("应用"), self)
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(apply_btn)
        layout.addLayout(btn_row)

    # ── 设置持久化 ──

    def _load_saved_settings(self):
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            s = AppSettings()
            self.edit_default_gap.setText(str(s.get("sug_concat.default_gap_ms", 300)))
            # 优先上次使用的值，否则采用用户全局偏移配置
            off = s.get("sug_concat.final_offset_ms")
            if off is None:
                off = s.get("export.offset_ms", 0)
            self.edit_final_offset.setText(str(int(off) if isinstance(off, (int, float)) else 0))
        except Exception:
            pass

    def _save_settings(self):
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            s = AppSettings()
            s.set("sug_concat.default_gap_ms", _parse_int(self.edit_default_gap.text(), 300))
            s.set("sug_concat.final_offset_ms", _parse_int(self.edit_final_offset.text(), 0))
            s.save()
        except Exception:
            pass

    # ── 卡片管理 ──

    def _add_card(self, entry: SugEntry):
        idx = len(self._cards)
        card = _SugCard(entry, idx, self._container)
        card.removed = self._on_card_removed
        self._cards.append(card)
        self._container.add_card(card)
        self._update_ui()

    def _on_card_removed(self, index: int):
        if 0 <= index < len(self._cards):
            card = self._cards.pop(index)
            self._container.remove_card(card)
            card.deleteLater()
            self._renumber()
            self._update_ui()

    def _on_order_changed(self):
        """拖拽完成后，按布局中的实际顺序重建 _cards 列表。"""
        new_order = []
        for n in range(self._container._layout.count()):
            w = self._container._layout.itemAt(n).widget() if self._container._layout.itemAt(n) else None
            if isinstance(w, _SugCard):
                new_order.append(w)
        if len(new_order) == len(self._cards):
            self._cards = new_order
            self._renumber()

    def _renumber(self):
        for i, card in enumerate(self._cards):
            card.update_index(i)

    def _update_ui(self):
        self._empty_hint.setVisible(len(self._cards) == 0)
        self._update_output_name()

    def _update_output_name(self):
        if self._cards and not self.edit_output_name.text():
            first = self._cards[0]._entry
            name = first.display_name + "……" if first.display_name else ""
            if name:
                self.edit_output_name.setText(name)

    # ── 按钮回调 ──

    def _on_add_files(self):
        init_dir = ""
        try:
            pw = self.parent()
            if pw and hasattr(pw, "_store"):
                init_dir = pw._store.working_dir
        except Exception:
            pass

        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("选择要拼接的SUG文件"),
            init_dir,
            self.tr("StrangeUtaGame 项目 (*.sug);;所有文件 (*.*)"),
        )
        if not paths:
            return

        gap = _parse_int(self.edit_default_gap.text(), 300)
        for path in paths:
            entry = _read_sug_entry(path)
            entry.gap_ms = gap
            self._add_card(entry)
        self._update_output_name()

    def _on_apply_global_gap(self):
        gap = self.edit_default_gap.text()
        for card in self._cards:
            card.edit_gap.setText(gap)

    # ── 公开接口 ──

    def was_apply_clicked(self) -> bool:
        return self._apply_clicked

    def get_entries(self) -> list[SugEntry]:
        return [card.collect() for card in self._cards]

    def get_output_name(self) -> str:
        name = self.edit_output_name.text().strip()
        if not name and self._cards:
            name = self._cards[0]._entry.display_name + "……"
        return name or "拼接项目"

    def get_uniform_offset(self) -> int:
        return _parse_int(self.edit_final_offset.text(), 0)

    def _on_apply(self):
        if not self._cards:
            InfoBar.warning(
                title=self.tr("无SUG文件"),
                content=self.tr("请先添加至少一个SUG文件。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        self._apply_clicked = True
        self._save_settings()
        self.accept()
