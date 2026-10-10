"""用户读音词典编辑对话框。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from strange_uta_game.frontend.font_utils import ui_font
from strange_uta_game.frontend.window_sizing import fit_min_size
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
    QVBoxLayout,
)
from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
)

from .app_settings import _parse_rl_dictionary
from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
    parse_annotated_line,
)
from strange_uta_game.backend.infrastructure.parsers.rl_dictionary import (
    read_rl_dictionary_file,
)


class DictionaryEditDialog(QDialog):
    """用户读音词典编辑对话框

    三列表格：启用 | 词 | 注音(annotated 行内格式)
    按从上到下排列，顶部 = 最高优先级。新条目默认添加到顶部。
    支持导入 RL 字典文件和上下移动调整优先级。

    注音格式（annotated 行内格式）：
        ``{原文||段1,段2,...}``
        块内 ``|`` 分 mora（RubyPart），``,`` 分字符；空段表示该字符无 ruby；
        ``{...||...}`` 块外的字面字符无 ruby，但参与连词。
    """

    def __init__(self, entries: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("读音词典"))
        fit_min_size(self, 560, 480)
        self._entries = [dict(e) for e in entries]  # 深拷贝

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(self.tr("读音词典"))
        title.setFont(ui_font(14))
        layout.addWidget(title)

        desc = QLabel(self.tr(
            "设置固定读音的词汇。词典中的词将以子串严格匹配方式覆盖自动注音（最高优先级）。\n"
            "优先级从上到下递减，新添加的词条默认在顶部（最高优先级）。\n"
            "注音格式：{原文||段1,段2,...}（块内 | 分 mora、, 分字符；空段=无 ruby）。\n"
            "示例：{微笑||ほほ,え}ん  /  {大冒険||だ|い,ぼ|う,け|ん}"
        ))
        desc.setFont(ui_font(10))
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 实时过滤框：按「词」列子串匹配，不匹配的行隐藏（行号保持原词典序号）
        self._filter_edit = SearchLineEdit(self)
        self._filter_edit.setPlaceholderText(self.tr("输入以筛选词（行号保持原词典序号）…"))
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_edit)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels([self.tr("启用"), self.tr("词"), self.tr("注音(annotated)")])
        header = self._table.horizontalHeader()
        if header is not None:
            # 词列按内容自动撑开，注音列拉伸吃满剩余宽度；"启用" 复选框列固定窄宽。
            from PyQt6.QtWidgets import QHeaderView
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 50)
        # 列 1 (词) 的最小宽度——内容更短时不至于过窄，长内容自然撑开。
        self._table.setColumnWidth(1, 140)
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table)

        # 填充数据
        for entry in self._entries:
            self._append_row(
                entry.get("enabled", True),
                entry.get("word", ""),
                entry.get("reading", ""),
            )

        # 按钮行
        btn_row = QHBoxLayout()
        btn_add = PushButton(self.tr("添加"), self)
        btn_add.clicked.connect(self._on_add)
        btn_del = PushButton(self.tr("删除选中"), self)
        btn_del.clicked.connect(self._on_delete)
        self._btn_up = PushButton(self.tr("上移"), self)
        self._btn_up.clicked.connect(self._on_move_up)
        self._btn_down = PushButton(self.tr("下移"), self)
        self._btn_down.clicked.connect(self._on_move_down)
        btn_top = PushButton(self.tr("置顶"), self)
        btn_top.clicked.connect(self._on_move_to_top)
        btn_bottom = PushButton(self.tr("置底"), self)
        btn_bottom.clicked.connect(self._on_move_to_bottom)
        btn_import = PushButton(self.tr("导入RL字典"), self)
        btn_import.clicked.connect(self._on_import_rl)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_del)
        btn_row.addWidget(self._btn_up)
        btn_row.addWidget(self._btn_down)
        btn_row.addWidget(btn_top)
        btn_row.addWidget(btn_bottom)
        btn_row.addWidget(btn_import)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # 确定/取消
        ok_row = QHBoxLayout()
        btn_ok = PrimaryPushButton(self.tr("确定"), self)
        btn_ok.clicked.connect(self._on_accept)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        ok_row.addStretch()
        ok_row.addWidget(btn_ok)
        ok_row.addWidget(btn_cancel)
        layout.addLayout(ok_row)

    def _apply_filter(self, text: str):
        """按「词」列实时过滤：不匹配的行隐藏，行号保持原词典序号。

        过滤期间禁用上移/下移（隐藏行的存在会让交换目标不明确）。
        """
        needle = (text or "").strip().casefold()
        if needle:
            # 隐藏行仍保留选中态，可能导致「删除选中」误删不可见行——先清掉
            self._table.clearSelection()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 1)
            word = item.text() if item else ""
            self._table.setRowHidden(
                row, bool(needle) and needle not in word.casefold()
            )
        self._btn_up.setEnabled(not needle)
        self._btn_down.setEnabled(not needle)

    def _append_row(self, enabled: bool, word: str = "", reading: str = ""):
        """在表格末尾追加一行。"""
        row = self._table.rowCount()
        self._table.insertRow(row)

        chk = QTableWidgetItem()
        chk.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self._table.setItem(row, 0, chk)
        self._table.setItem(row, 1, QTableWidgetItem(word))
        self._table.setItem(row, 2, QTableWidgetItem(reading))

    def _insert_row_at(self, row: int, enabled: bool, word: str, reading: str):
        """在指定位置插入一行。"""
        self._table.insertRow(row)
        chk = QTableWidgetItem()
        chk.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self._table.setItem(row, 0, chk)
        self._table.setItem(row, 1, QTableWidgetItem(word))
        self._table.setItem(row, 2, QTableWidgetItem(reading))

    def _on_add(self):
        # 新条目插入到顶部（最高优先级）；先清空过滤确保新行可见
        self._filter_edit.clear()
        self._insert_row_at(0, True, "", "")
        self._table.scrollToTop()
        self._table.selectRow(0)

    def _on_delete(self):
        rows = sorted(
            set(idx.row() for idx in self._table.selectedIndexes()), reverse=True
        )
        for row in rows:
            self._table.removeRow(row)

    def _on_move_up(self):
        """将选中行上移一位（提高优先级）。"""
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        if not rows or rows[0] == 0:
            return
        for row in rows:
            self._swap_rows(row, row - 1)
        # 重新选中
        self._table.clearSelection()
        for row in rows:
            self._table.selectRow(row - 1)

    def _on_move_down(self):
        """将选中行下移一位（降低优先级）。"""
        rows = sorted(
            set(idx.row() for idx in self._table.selectedIndexes()), reverse=True
        )
        if not rows or rows[0] == self._table.rowCount() - 1:
            return
        for row in rows:
            self._swap_rows(row, row + 1)
        # 重新选中
        self._table.clearSelection()
        for row in rows:
            self._table.selectRow(row + 1)

    def _on_move_to_top(self):
        """选中行整体移到词典顶部（保持相对顺序）；过滤状态下同样可用。"""
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        if not rows or rows == list(range(len(rows))):
            return
        self._relocate_rows(rows, 0)

    def _on_move_to_bottom(self):
        """选中行整体移到词典底部（保持相对顺序）；过滤状态下同样可用。"""
        rows = sorted(set(idx.row() for idx in self._table.selectedIndexes()))
        total = self._table.rowCount()
        if not rows or rows == list(range(total - len(rows), total)):
            return
        self._relocate_rows(rows, total - len(rows))

    def _relocate_rows(self, rows: list, insert_at: int):
        """把 rows（升序）整体搬运到 insert_at，保持相对顺序并重新选中。

        先抽取数据、倒序删行（避免位移），再在目标位置按原顺序插回。
        过滤状态下同样安全：插入后重新套用过滤条件，仅补选仍可见的行。
        """
        data = [self._row_data(r) for r in rows]
        for r in reversed(rows):
            self._table.removeRow(r)
        for i, (enabled, word, reading) in enumerate(data):
            self._insert_row_at(insert_at + i, enabled, word, reading)
        # 新插入的行默认可见，需重新套用当前过滤（过滤时会顺带清空选中态）
        self._apply_filter(self._filter_edit.text())
        self._table.clearSelection()
        # setRangeSelected 累加选中（selectRow 程序化调用会替换选择，多行时只剩最后一行）
        last_col = self._table.columnCount() - 1
        for i in range(len(data)):
            row = insert_at + i
            if not self._table.isRowHidden(row):
                self._table.setRangeSelected(
                    QTableWidgetSelectionRange(row, 0, row, last_col), True
                )

    def _row_data(self, row: int) -> tuple:
        """读取一行的 (enabled, word, reading) 数据。"""
        chk = self._table.item(row, 0)
        word_item = self._table.item(row, 1)
        reading_item = self._table.item(row, 2)
        enabled = chk.checkState() == Qt.CheckState.Checked if chk else True
        word = word_item.text() if word_item else ""
        reading = reading_item.text() if reading_item else ""
        return enabled, word, reading

    def _swap_rows(self, row_a: int, row_b: int):
        """交换两行数据。"""
        for col in range(self._table.columnCount()):
            item_a = self._table.takeItem(row_a, col)
            item_b = self._table.takeItem(row_b, col)
            if item_a and item_b:
                self._table.setItem(row_a, col, item_b)
                self._table.setItem(row_b, col, item_a)

    def _on_import_rl(self):
        """导入 RL 字典文件。"""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择RL字典文件"), "",
            self.tr("文本文件 (*.txt);;所有文件 (*)")
        )
        if not path:
            return

        try:
            text = read_rl_dictionary_file(path)
        except Exception as e:
            InfoBar.warning(
                title=self.tr("读取失败"),
                content=str(e),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        new_entries = _parse_rl_dictionary(text)
        if not new_entries:
            InfoBar.warning(
                title=self.tr("导入失败"),
                content=self.tr("未找到有效的词典条目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        # 允许同 word 多读音并存：仅去重 (word, reading) 完全一致的条目；
        # 新条目整批插入顶部，保留导入文件原顺序（首条最优先）。
        existing: set = set()
        for r in range(self._table.rowCount()):
            w_item = self._table.item(r, 1)
            r_item = self._table.item(r, 2)
            if w_item and r_item:
                existing.add((w_item.text().strip(), r_item.text().strip()))

        added = 0
        skipped = 0
        # 逆序插入到位置 0：最终顺序与 new_entries 一致（首条在顶）
        for entry in reversed(new_entries):
            word = entry["word"]
            reading = entry["reading"]
            if (word, reading) in existing:
                skipped += 1
                continue
            existing.add((word, reading))
            self._insert_row_at(0, True, word, reading)
            added += 1

        # 导入的新行也需要套用当前过滤条件，保持「可见即匹配」的视图一致性
        self._apply_filter(self._filter_edit.text())
        self._table.scrollToTop()
        InfoBar.success(
            title=self.tr("导入完成"),
            content=self.tr("新增 {added} 条，跳过重复 {skipped} 条（共 {total} 条，新增条目已置顶）").format(
                added=added, skipped=skipped, total=len(new_entries)
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=3000,
            parent=self,
        )

    def _validate_entries(self) -> list:
        """校验所有行的 annotated 格式。

        返回有问题的行号列表（0-based）。仅对启用且非空的行做校验：
        - reading 必须能被 ``parse_annotated_line`` 解析
        - 解析后的 raw_text 必须等于 word
        """
        bad_rows: list = []
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            word_item = self._table.item(row, 1)
            reading_item = self._table.item(row, 2)
            enabled = chk.checkState() == Qt.CheckState.Checked if chk else True
            word = word_item.text().strip() if word_item else ""
            reading = reading_item.text().strip() if reading_item else ""
            if not enabled:
                continue
            if not word and not reading:
                continue
            if not word or not reading:
                bad_rows.append(row)
                continue
            try:
                parsed_raw_text, _chars, _ruby = parse_annotated_line(reading)
            except Exception:
                bad_rows.append(row)
                continue
            if parsed_raw_text != word:
                bad_rows.append(row)
        return bad_rows

    def _on_accept(self):
        """点击确定：先校验，再 accept。校验失败软警告但不阻塞。"""
        bad_rows = self._validate_entries()
        if bad_rows:
            preview = ", ".join(str(r + 1) for r in bad_rows[:5])
            more = "" if len(bad_rows) <= 5 else self.tr(" 等 {n} 行").format(n=len(bad_rows))
            InfoBar.warning(
                title=self.tr("注音格式异常"),
                content=self.tr(
                    "第 {rows}{more} 行的注音不符合 annotated 格式或 raw_text 与词不一致；"
                    "这些行在匹配时可能被忽略。仍可保存。"
                ).format(rows=preview, more=more),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )
        self.accept()

    def get_entries(self) -> list:
        entries = []
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            word_item = self._table.item(row, 1)
            reading_item = self._table.item(row, 2)
            word = word_item.text().strip() if word_item else ""
            reading = reading_item.text().strip() if reading_item else ""
            if not word and not reading:
                continue
            enabled = chk.checkState() == Qt.CheckState.Checked if chk else True
            entries.append({"enabled": enabled, "word": word, "reading": reading})
        return entries
