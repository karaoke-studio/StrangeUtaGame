"""查找与替换对话框。

在 QPlainTextEdit 中全文本搜索/替换，支持标签过滤。
"""

import re

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QTextEdit,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QTextCursor
from qfluentwidgets import (
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SwitchButton,
)

from strange_uta_game.frontend.font_utils import ui_font
from strange_uta_game.frontend.fluent_widgets import ThemedMenuLineEdit

# 剥离行内结构化标签的正则（与 fulltext_interface 保持一致）
_STRIP_SINGER_RE = re.compile(r"【[^】]*】")
_STRIP_TIMESTAMP_RE = re.compile(r"\[>?(?:T|\d+:\d{2}\.\d{2})\]")


def _build_stripped_mapping(text: str):
    """扫描原文，构建视觉文本与原文位置的映射。

    视觉文本：剥离演唱者标签【】、时间戳 [...]、注音块 {原文||注音}（仅保留原文）。
    Returns:
        (visual_text, position_map): visual_text 是剥离后的纯文本，
        position_map[i] 是 visual_text[i] 在原文中的位置。
    """
    visual: list[str] = []
    mapping: list[int] = []
    i = 0
    while i < len(text):
        si_m = _STRIP_SINGER_RE.match(text, i)
        if si_m:
            i = si_m.end()
            continue

        ts_m = _STRIP_TIMESTAMP_RE.match(text, i)
        if ts_m:
            i = ts_m.end()
            continue

        if text[i] == "{":
            close = text.find("}", i)
            if close != -1:
                inner = text[i + 1 : close]
                sep_idx = inner.find("||")
                if sep_idx != -1:
                    orig_text = inner[:sep_idx]
                    for j, c in enumerate(orig_text):
                        visual.append(c)
                        mapping.append(i + 1 + j)
                    i = close + 1
                    continue

        visual.append(text[i])
        mapping.append(i)
        i += 1

    return "".join(visual), mapping


class FindDialog(QDialog):
    """查找与替换对话框 —— 在 QPlainTextEdit 中全文本搜索/替换，支持标签过滤。"""

    def __init__(self, ruby_interface, parent=None):
        super().__init__(parent)
        self._ruby_interface = ruby_interface
        self._text_edit = ruby_interface.text_edit
        self._filter_tags = False
        self._matches: list[tuple[int, int]] = []  # (start, end) in original text
        self._match_body_positions: list[list[int]] = []  # per-match orig body char positions (filter_tags only)
        self._current_match_idx = -1

        self.setWindowTitle(ruby_interface.tr("查找和替换"))
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowTitleHint
        )
        self.setMinimumWidth(480)
        self.setMinimumHeight(1)

        self._init_ui()
        self._connect_signals()

        cursor = self._text_edit.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            self._search_input.setText(selected)
            self._search_input.selectAll()

        self._search_input.setFocus()

    def _init_ui(self):
        tr = self._ruby_interface.tr

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Row 1: "查找内容" + prev/next buttons
        row1 = QHBoxLayout()
        lbl1 = QLabel(tr("查找内容:"))
        lbl1.setFont(ui_font(9))
        row1.addWidget(lbl1)
        self._search_input = ThemedMenuLineEdit()
        self._search_input.setPlaceholderText(tr("输入要查找的文本"))
        self._search_input.setFont(ui_font(10))
        self._search_input.setMinimumWidth(180)
        row1.addWidget(self._search_input, stretch=1)
        self._btn_prev = PushButton(tr("上一个"))
        self._btn_prev.setEnabled(False)
        row1.addWidget(self._btn_prev)
        self._btn_next = PrimaryPushButton(tr("下一个"))
        self._btn_next.setEnabled(False)
        row1.addWidget(self._btn_next)
        layout.addLayout(row1)

        # Row 2: "替换为" + replace / replace-all buttons
        row2 = QHBoxLayout()
        lbl2 = QLabel(tr("替换为:"))
        lbl2.setFont(ui_font(9))
        row2.addWidget(lbl2)
        self._replace_input = ThemedMenuLineEdit()
        self._replace_input.setPlaceholderText(tr("输入替换文本（留空则删除）"))
        self._replace_input.setFont(ui_font(10))
        self._replace_input.setMinimumWidth(180)
        row2.addWidget(self._replace_input, stretch=1)
        self._btn_replace = PushButton(tr("替换"))
        self._btn_replace.setEnabled(False)
        row2.addWidget(self._btn_replace)
        self._btn_replace_all = PushButton(tr("全部替换"))
        self._btn_replace_all.setEnabled(False)
        row2.addWidget(self._btn_replace_all)
        layout.addLayout(row2)

        # Row 3: "过滤标签" + match count
        row3 = QHBoxLayout()
        self._switch_filter = SwitchButton()
        self._switch_filter.setOnText(tr("开"))
        self._switch_filter.setOffText(tr("关"))
        row3.addWidget(QLabel(tr("过滤标签")))
        row3.addWidget(self._switch_filter)
        row3.addSpacing(16)
        self._lbl_count = CaptionLabel("")
        row3.addWidget(self._lbl_count)
        row3.addStretch()
        layout.addLayout(row3)

    def _connect_signals(self):
        self._search_input.textChanged.connect(self._on_search)
        self._search_input.returnPressed.connect(self._on_find_next)
        self._replace_input.returnPressed.connect(self._on_replace)
        self._btn_next.clicked.connect(self._on_find_next)
        self._btn_prev.clicked.connect(self._on_find_prev)
        self._btn_replace.clicked.connect(self._on_replace)
        self._btn_replace_all.clicked.connect(self._on_replace_all)
        self._switch_filter.checkedChanged.connect(self._on_filter_changed)
        # 连接具名槽而非 lambda：closeEvent 需要断开这条连接，
        # 否则对话框关闭后（对象仍挂在父窗口下）每次编辑都会
        # 重跑搜索并复活已清掉的高亮。
        self._text_edit.textChanged.connect(self._on_text_edit_changed)
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_search)

    def _on_text_edit_changed(self):
        self._debounce_search()

    def _debounce_search(self):
        self._debounce_timer.stop()
        self._debounce_timer.start(250)

    # ─── search ────────────────────────────────────────────────

    def _on_search(self):
        query = self._search_input.text()
        if not query:
            self._clear_highlights()
            self._btn_next.setEnabled(False)
            self._btn_prev.setEnabled(False)
            self._btn_replace.setEnabled(False)
            self._btn_replace_all.setEnabled(False)
            self._lbl_count.setText("")
            self._matches = []
            self._match_body_positions = []
            return

        full_text = self._text_edit.toPlainText()
        self._matches, self._match_body_positions = self._find_all_matches(full_text, query)

        self._current_match_idx = -1
        count = len(self._matches)
        has_match = count > 0
        self._btn_next.setEnabled(has_match)
        self._btn_prev.setEnabled(has_match)
        self._btn_replace.setEnabled(has_match)
        self._btn_replace_all.setEnabled(has_match)
        self._lbl_count.setText(
            self._ruby_interface.tr("找到 {count} 个匹配").format(count=count)
        )
        self._highlight_all_matches()
        self._ruby_interface._refresh_highlight()

    def _find_all_matches(self, full_text: str, query: str):
        """返回 (matches, body_positions)。matches 为 [(start, end), ...] 用于导航/高亮；
        body_positions 为过滤标签模式下的逐字符原文位置列表，普通模式为空列表。"""
        if self._filter_tags:
            visual_text, mapping = _build_stripped_mapping(full_text)
            matches: list[tuple[int, int]] = []
            body_positions: list[list[int]] = []
            pos = 0
            while True:
                idx = visual_text.find(query, pos)
                if idx == -1:
                    break
                positions = sorted(set(mapping[idx : idx + len(query)]))
                body_positions.append(positions)
                start = positions[0]
                end = positions[-1] + 1
                matches.append((start, end))
                pos = idx + 1
            return matches, body_positions
        else:
            matches: list[tuple[int, int]] = []
            pos = 0
            while True:
                idx = full_text.find(query, pos)
                if idx == -1:
                    break
                matches.append((idx, idx + len(query)))
                pos = idx + 1
            return matches, []

    def _on_filter_changed(self, checked: bool):
        self._filter_tags = checked
        self._on_search()

    # ─── navigate ──────────────────────────────────────────────

    def _on_find_next(self):
        if not self._matches:
            return
        current_pos = self._cursor_pos()
        next_idx = self._next_match_idx_from(current_pos)
        if next_idx == -1:
            return
        self._go_to_match(next_idx)

    def _on_find_prev(self):
        if not self._matches:
            return
        current_pos = self._cursor_pos()
        prev_idx = self._prev_match_idx_from(current_pos)
        if prev_idx == -1:
            return
        self._go_to_match(prev_idx)

    def _cursor_pos(self) -> int:
        cursor = self._text_edit.textCursor()
        return cursor.selectionStart() if cursor.hasSelection() else cursor.position()

    def _next_match_idx_from(self, pos: int) -> int:
        for i, (start, _end) in enumerate(self._matches):
            if start >= pos:
                return i
        if self._matches:
            return 0  # wrap around
        return -1

    def _prev_match_idx_from(self, pos: int) -> int:
        for i in range(len(self._matches) - 1, -1, -1):
            start, _end = self._matches[i]
            if start < pos:
                return i
        if self._matches:
            return len(self._matches) - 1  # wrap around
        return -1

    def _go_to_match(self, idx: int):
        self._current_match_idx = idx
        start, end = self._matches[idx]
        cursor = self._text_edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._text_edit.setTextCursor(cursor)
        self._text_edit.ensureCursorVisible()
        self._highlight_all_matches()
        self._ruby_interface._refresh_highlight()

    # ─── replace ───────────────────────────────────────────────

    @staticmethod
    def _apply_body_replacement(
        doc, positions: list[int], replacement: str
    ) -> QTextCursor:
        """逐正文字符位写入替换文本（过滤标签模式专用），纳入一个撤销块。

        * 替换字符多于匹配位 → 多位各自分配一字符，剩余字符全写入最后一个匹配位
        * 替换字符少于匹配位 → 分配不到的匹配位直接删除
        """
        n = len(positions)
        m = len(replacement)
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        threshold = min(n, m) - 1
        for i in range(n - 1, -1, -1):
            pos = positions[i]
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, 1)
            if i >= m:
                c.removeSelectedText()
            elif i == threshold:
                c.insertText(replacement[i:])
            else:
                c.insertText(replacement[i])
        cursor.endEditBlock()
        return cursor

    def _on_replace(self):
        """替换当前匹配项并跳到下一个。"""
        if self._current_match_idx < 0 or self._current_match_idx >= len(self._matches):
            self._on_find_next()
            if self._current_match_idx < 0:
                return

        replacement = self._replace_input.text()

        if self._filter_tags and self._match_body_positions:
            positions = self._match_body_positions[self._current_match_idx]
            doc = self._text_edit.document()
            self._apply_body_replacement(doc, positions, replacement)
        else:
            start, end = self._matches[self._current_match_idx]
            doc = self._text_edit.document()
            cursor = QTextCursor(doc)
            cursor.beginEditBlock()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(replacement)
            cursor.endEditBlock()
            self._text_edit.setTextCursor(cursor)

        self._on_search()
        if self._matches:
            current_pos = self._cursor_pos()
            next_idx = self._next_match_idx_from(current_pos)
            if next_idx != -1:
                self._go_to_match(next_idx)

    def _on_replace_all(self):
        query = self._search_input.text()
        if not query or not self._matches:
            return

        replacement = self._replace_input.text()
        full_text = self._text_edit.toPlainText()

        if self._filter_tags:
            new_text, count = self._replace_all_with_filter(full_text, query, replacement)
        else:
            new_text = full_text.replace(query, replacement)
            count = full_text.count(query)

        if new_text != full_text:
            doc = self._text_edit.document()
            cursor = QTextCursor(doc)
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(new_text)
            cursor.endEditBlock()

        InfoBar.success(
            title=self._ruby_interface.tr("替换完成"),
            content=self._ruby_interface.tr("共替换了 {count} 处").format(count=count),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self._ruby_interface,
        )
        self._on_search()

    def _replace_all_with_filter(self, text: str, query: str, replacement: str):
        """过滤标签模式下执行全部替换，返回 (new_text, count)。"""
        visual_text, mapping = _build_stripped_mapping(text)

        match_ranges: list[tuple[int, int]] = []
        pos = 0
        while True:
            idx = visual_text.find(query, pos)
            if idx == -1:
                break
            match_ranges.append((idx, idx + len(query)))
            pos = idx + 1

        if not match_ranges:
            return text, 0

        m = len(replacement)
        result = list(text)
        for v_start, v_end in reversed(match_ranges):
            positions = sorted(set(mapping[v_start:v_end]))
            n = len(positions)
            threshold = min(n, m) - 1
            for i in range(n - 1, -1, -1):
                p = positions[i]
                if i >= m:
                    result[p] = ""
                elif i == threshold:
                    result[p] = replacement[i:]
                else:
                    result[p] = replacement[i]

        return "".join(result), len(match_ranges)

    # ─── highlights ────────────────────────────────────────────

    def _highlight_all_matches(self):
        from strange_uta_game.frontend.theme import theme

        selections: list = []
        ts_color = QColor(theme.syntax_timestamp)

        if 0 <= self._current_match_idx < len(self._matches):
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor("#FF9632"))
            sel.format.setForeground(QColor("#000000"))
            start, end = self._matches[self._current_match_idx]
            c = QTextCursor(self._text_edit.document())
            c.setPosition(start)
            c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = c
            selections.append(sel)

        for i, (start, end) in enumerate(self._matches):
            if i == self._current_match_idx:
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(ts_color.lighter(160))
            c = QTextCursor(self._text_edit.document())
            c.setPosition(start)
            c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = c
            selections.append(sel)

        self._ruby_interface._find_highlights = selections

    def _clear_highlights(self):
        self._matches = []
        self._match_body_positions = []
        self._current_match_idx = -1
        self._ruby_interface._find_highlights = []
        self._ruby_interface._refresh_highlight()

    # ─── events ────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # 断开对编辑器的监听并停掉防抖定时器：对话框关闭后对象仍以父窗口
        # 存活，残留连接会让每次编辑重跑搜索、把已清掉的高亮"复活"。
        try:
            self._text_edit.textChanged.disconnect(self._on_text_edit_changed)
        except TypeError:
            pass  # 未连接（重复关闭等）时忽略
        self._debounce_timer.stop()
        self._clear_highlights()
        super().closeEvent(event)
