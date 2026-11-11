"""批量变更对话框 (Ctrl+H)。

以"修改所选字符"对话框为模板的批量版：顶部加一个"搜索词"字段，
对项目中所有匹配该词的字符区间应用同一份字符级编辑。

行为契约：
- 搜索词空 → 禁用执行
- 新字符长度 == 搜索词长度 → 每处匹配原地修改，保留 timestamps
- 新字符长度 != 搜索词长度 → 弹确认，确认后逐处替换 slice（丢所有匹配处 timestamps）
- 执行后不关闭对话框，显示"已修改 N 处"
"""

from PyQt6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QFormLayout,
    QLineEdit,
    QScrollArea,
    QWidget,
)
from qfluentwidgets import (
    PushButton,
    PrimaryPushButton,
    CaptionLabel,
    BodyLabel,
    StrongBodyLabel,
    LineEdit,
    CheckBox,
    ScrollArea,
)
from typing import Optional, List, Tuple
from copy import deepcopy

from strange_uta_game.backend.domain import Project
from strange_uta_game.backend.domain.graphemes import (
    chars_to_offsets,
    grapheme_count,
    offset_to_index,
    split_graphemes,
)
from strange_uta_game.backend.domain.models import Character, Ruby, RubyPart
from strange_uta_game.frontend.fluent_widgets import message_info, message_question
from strange_uta_game.frontend.window_sizing import fit_to_screen


class BulkChangeDialog(QDialog):
    """批量变更对话框 — 搜索词 + 字符级编辑，批量应用到所有匹配处。

    构造参数：
        project: 当前项目（None 时执行按钮不起作用）
        parent: 父窗口（期望具备 _store / _timing_service / refresh_lyric_display 等）
        initial_word: 初始搜索词
        initial_reading: 初始注音（逗号分隔，对应每个字符）
    """

    def __init__(
        self,
        project: Optional[Project],
        parent=None,
        initial_word: str = "",
        initial_reading: str = "",
        initial_state: Optional[dict] = None,
    ):
        super().__init__(parent)
        self._project = project
        self._char_rows: List[Tuple[QLabel, QLineEdit, QLineEdit, QCheckBox]] = []
        # 用户是否已手动编辑过 rows / 新字符框；一旦手动编辑，搜索词变化不再覆盖
        self._rows_user_edited = False
        self._new_chars_user_edited = False
        # 抑制程序性 textChanged 触发的标志
        self._suppress_row_signals = False
        self._suppress_new_chars_signal = False
        # 执行后的失败项汇总：(sentence_index, abs_char_idx, char, reason)
        self._linked_failures: List[Tuple[int, int, str, str]] = []
        self._switch_to_normal = False

        from strange_uta_game.frontend.editor.timing.dialogs import (
            CHAR_DIALOG_SIZE,
            char_dialog_font,
            FONT_DIALOG_BASE,
            FONT_MAIN_INPUT,
            FONT_FIELD_LABEL,
        )

        self.setWindowTitle(self.tr("批量变更"))
        fit_to_screen(self, *CHAR_DIALOG_SIZE)
        self.setFont(char_dialog_font(FONT_DIALOG_BASE))

        layout = QVBoxLayout(self)

        # 搜索词行
        search_row = QHBoxLayout()
        self.edit_word = LineEdit(self)
        self.edit_word.setText(initial_word)
        self.edit_word.setPlaceholderText(self.tr("输入要搜索的词"))
        self.edit_word.setFont(char_dialog_font(FONT_MAIN_INPUT))
        self.lbl_match = CaptionLabel("")
        lbl_search = BodyLabel(self.tr("搜索词:"))
        lbl_search.setFont(char_dialog_font(FONT_FIELD_LABEL))
        search_row.addWidget(lbl_search)
        search_row.addWidget(self.edit_word, stretch=1)
        search_row.addWidget(self.lbl_match)
        self.btn_switch_normal = PushButton(self.tr("切换为普通"), self)
        self.btn_switch_normal.clicked.connect(self._on_switch_to_normal)
        search_row.addWidget(self.btn_switch_normal)
        layout.addLayout(search_row)

        # 新字符行
        top_form = QFormLayout()
        self.edit_new_chars = LineEdit(self)
        self.edit_new_chars.setText(initial_word)
        self.edit_new_chars.setPlaceholderText(self.tr("输入替换后的字符（默认=搜索词）"))
        self.edit_new_chars.setFont(char_dialog_font(FONT_MAIN_INPUT))
        lbl_replace = BodyLabel(self.tr("替换为:"))
        lbl_replace.setFont(char_dialog_font(FONT_FIELD_LABEL))
        top_form.addRow(lbl_replace, self.edit_new_chars)
        layout.addLayout(top_form)

        hint = CaptionLabel(self.tr(
            "按字符编辑（注音用半角逗号分隔 RubyPart；节奏点为非负整数）。\n"
            "字符数与搜索词相同 → 保留时间戳；不同 → 丢失所有匹配处时间戳。"
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Scroll area with per-char rows
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(4, 4, 4, 4)
        self._rows_layout.setSpacing(4)
        scroll.setWidget(self._rows_container)
        layout.addWidget(scroll, stretch=1)

        # 注册到词典 + 快速连词
        register_row = QHBoxLayout()
        self.chk_register = CheckBox(self.tr("将此词注册到读音词典"))
        register_row.addWidget(self.chk_register)
        register_row.addStretch()
        self.btn_toggle_linked = PushButton(self.tr("快速连词/取消连词"), self)
        self.btn_toggle_linked.setToolTip(self.tr(
            "若全部未连词，则将除最后一个字符外的向后连词全部勾选；否则全部取消连词"
        ))
        self.btn_toggle_linked.clicked.connect(self._on_toggle_all_linked)
        from strange_uta_game.frontend.editor.timing.dialogs import style_quick_link_button
        style_quick_link_button(self.btn_toggle_linked)
        register_row.addWidget(self.btn_toggle_linked)
        layout.addLayout(register_row)

        # 注音分段方式选择
        from strange_uta_game.frontend.editor.timing.dialogs import _create_ruby_split_group, _save_ruby_split_mode, parse_ruby_text
        self._radio_direct, self._radio_by_char, self._radio_by_mora, ruby_split_group = _create_ruby_split_group(self)
        layout.addWidget(ruby_split_group)

        # 预览区域
        self.preview_label = CaptionLabel(self.tr("预览: "))
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        # 连接信号更新预览
        self._radio_direct.toggled.connect(self._update_preview)
        self._radio_by_char.toggled.connect(self._update_preview)
        self._radio_by_mora.toggled.connect(self._update_preview)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_exec = PrimaryPushButton(self.tr("执行"), self)
        self.btn_exec.setDefault(True)
        self.btn_exec.clicked.connect(self._on_execute)
        btn_row.addWidget(self.btn_exec)
        self.btn_query = PushButton(self.tr("查询候补字典"), self)
        self.btn_query.clicked.connect(self._on_query_dict_candidates)
        btn_row.addWidget(self.btn_query)
        btn_close = PushButton(self.tr("关闭"), self)
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        # 信号连接
        self.edit_word.textChanged.connect(self._on_word_changed)
        self.edit_new_chars.textChanged.connect(self._on_new_chars_changed)

        # 首次填充：按初始搜索词首匹配
        self._refresh_match_count()
        self._refill_from_first_match(initial_reading)
        if initial_state:
            from strange_uta_game.frontend.editor.timing.dialogs import (
                apply_char_dialog_state,
            )
            apply_char_dialog_state(self, initial_state)
            self._rows_user_edited = True
            self._new_chars_user_edited = True

    def _on_switch_to_normal(self) -> None:
        self._switch_to_normal = True
        self.reject()

    def switch_to_normal_requested(self) -> bool:
        return self._switch_to_normal

    def get_switch_state(self) -> dict:
        from strange_uta_game.frontend.editor.timing.dialogs import (
            collect_char_dialog_state,
        )
        return collect_char_dialog_state(self, self.edit_word.text().strip())

    def _on_query_dict_candidates(self):
        """查询候补字典：以搜索词为 word，选中条目后按其格式填充并执行+关闭两窗口。"""
        from strange_uta_game.frontend.editor.timing.dict_candidate_dialog import (
            DictCandidateDialog,
            apply_entry_to_dialog_rows,
        )

        word = self.edit_word.text().strip()
        dlg = DictCandidateDialog(word, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.get_selected_entry()
        if not entry:
            return
        # 标记 rows 已被外部填充，避免后续搜索词变化覆盖
        self._rows_user_edited = True
        self._new_chars_user_edited = True
        if apply_entry_to_dialog_rows(self, entry["word"], entry["reading"]):
            # 批量执行（_on_execute 不关闭对话框）后，主动关闭本窗口
            self._on_execute()
            self.accept()

    # ---------- 行管理 ----------

    def _append_char_row(
        self, char_str: str, ruby_str: str, check_str: str, linked: bool = False
    ):
        """追加一行：[字符] [注音] [节奏点] [向后连词]。"""
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        from strange_uta_game.frontend.editor.timing.dialogs import (
            char_dialog_font,
            FONT_CHAR_GLYPH,
            FONT_ROW_INPUT,
        )
        lbl = StrongBodyLabel(char_str)
        lbl.setFixedWidth(32)
        lbl.setFont(char_dialog_font(FONT_CHAR_GLYPH, bold=True))
        edit_ruby = LineEdit(row_widget)
        edit_ruby.setText(ruby_str)
        edit_ruby.setPlaceholderText(self.tr("注音（逗号分隔多 RubyPart）"))
        edit_ruby.setFont(char_dialog_font(FONT_ROW_INPUT))
        edit_check = LineEdit(row_widget)
        edit_check.setText(check_str)
        edit_check.setPlaceholderText(self.tr("节奏点"))
        edit_check.setFixedWidth(64)
        edit_check.setFont(char_dialog_font(FONT_ROW_INPUT))
        chk_linked = CheckBox(self.tr("向后连词"))
        chk_linked.setChecked(bool(linked))
        chk_linked.setToolTip(self.tr(
            "连接到下一字符（末字/行尾不可连词，提交时将跳过并提示；停顿点允许连词）"
        ))
        # 监控用户手动编辑
        edit_ruby.textEdited.connect(self._on_row_user_edited)
        edit_check.textEdited.connect(self._on_row_user_edited)
        chk_linked.stateChanged.connect(self._on_row_checkbox_edited)
        row_layout.addWidget(lbl)
        row_layout.addWidget(edit_ruby, stretch=1)
        row_layout.addWidget(edit_check)
        row_layout.addWidget(chk_linked)
        self._rows_layout.addWidget(row_widget)
        self._char_rows.append((lbl, edit_ruby, edit_check, chk_linked))

    def _clear_rows(self):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._char_rows.clear()

    def _rebuild_rows_for_text(
        self,
        new_text: str,
        ruby_list: Optional[List[str]] = None,
        check_list: Optional[List[str]] = None,
        linked_list: Optional[List[bool]] = None,
    ):
        """按 new_text 重建行；ruby_list/check_list/linked_list 为初始值（按索引对齐）。"""
        # 若未传初始值，尝试保留现有 rows 的输入值
        if ruby_list is None or check_list is None:
            old_vals = [
                (e_r.text(), e_c.text(), chk.isChecked())
                for _, e_r, e_c, chk in self._char_rows
            ]
        else:
            linked_list = linked_list or [False] * len(ruby_list)
            old_vals = list(zip(ruby_list, check_list, linked_list))
        self._suppress_row_signals = True
        try:
            self._clear_rows()
            for i, ch in enumerate(split_graphemes(new_text)):
                if i < len(old_vals):
                    r_val, c_val, l_val = old_vals[i]
                else:
                    r_val, c_val, l_val = "", "1", False
                self._append_char_row(ch, r_val, c_val, l_val)
        finally:
            self._suppress_row_signals = False
        # 更新预览
        self._update_preview()
        self._update_toggle_linked_enabled()

    def _update_toggle_linked_enabled(self):
        """字符数 <= 1 时禁用快速连词按钮。"""
        self.btn_toggle_linked.setEnabled(len(self._char_rows) > 1)

    def _on_toggle_all_linked(self):
        """快速连词/取消连词：全部未连词→除末字外全部连词；否则→全部取消连词。"""
        rows = self._char_rows
        if len(rows) <= 1:
            return
        all_unlinked = all(not row[3].isChecked() for row in rows)
        for i, row in enumerate(rows):
            row[3].setChecked(all_unlinked and i < len(rows) - 1)
        self._update_preview()

    def _update_preview(self):
        """更新预览区域（与写回共用 split_ruby_segments，所见即所得）"""
        from strange_uta_game.backend.infrastructure.parsers.inline_format import (
            split_ruby_segments,
        )
        from strange_uta_game.frontend.editor.timing.dialogs import _radio_split_mode

        mode = _radio_split_mode(self._radio_direct, self._radio_by_char)
        preview_items = []
        for _, edit_ruby, edit_check, _ in self._char_rows:
            try:
                check_count = max(1, int(edit_check.text().strip()))
            except ValueError:
                check_count = 1
            parts = split_ruby_segments(edit_ruby.text(), check_count, mode)
            preview_items.append(f"[{','.join(parts)}]")

        self.preview_label.setText(self.tr("预览: {items}").format(items=' '.join(preview_items)))

    # ---------- 信号处理 ----------

    def _on_row_user_edited(self, _text: str):
        if self._suppress_row_signals:
            return
        self._rows_user_edited = True
        # 更新预览
        self._update_preview()

    def _on_row_checkbox_edited(self, _state: int):
        if self._suppress_row_signals:
            return
        self._rows_user_edited = True

    def _on_new_chars_changed(self, new_text: str):
        if not self._suppress_new_chars_signal:
            self._new_chars_user_edited = True
        # 文本变化 → 按新长度重建行，保留现有输入
        self._rebuild_rows_for_text(new_text)

    def _on_word_changed(self, _word: str):
        self._refresh_match_count()
        # 若用户未手动改过 rows 和新字符框 → 用新搜索词首匹配覆盖
        if not self._rows_user_edited and not self._new_chars_user_edited:
            self._refill_from_first_match("")

    # ---------- 匹配扫描 ----------

    def _iter_matches(self, word: str):
        """生成 ``(sentence, char_idx, n_chars)`` 非重叠匹配；空词返回空。

        文本按码点扫描（``sentence.text`` 的偏移），再映射到**字素簇字符**下标
        与簇数——``Character.char`` 不再保证单码点，调用方按下标/簇数切片。
        """
        if not self._project or not word:
            return
        w_len = len(word)
        for sentence in self._project.sentences:
            text = sentence.text
            offsets = chars_to_offsets([c.char for c in sentence.characters])
            pos = 0
            while pos <= len(text) - w_len:
                if text[pos : pos + w_len] == word:
                    idx = offset_to_index(offsets, pos)
                    end_idx = offset_to_index(offsets, pos + w_len - 1)
                    yield sentence, idx, end_idx - idx + 1
                    pos += w_len
                else:
                    pos += 1

    def _refresh_match_count(self):
        word = self.edit_word.text().strip()
        if not word:
            self.lbl_match.setText("")
            return
        count = sum(1 for _ in self._iter_matches(word))
        self.lbl_match.setText(self.tr("找到 {n} 处").format(n=count))

    def _refill_from_first_match(self, fallback_reading: str):
        """用首个匹配的字符/注音/节奏点填充新字符框和 rows。

        若无匹配：用搜索词填新字符框，rows 用搜索词字符 + fallback_reading 拆分。
        """
        word = self.edit_word.text().strip()
        if not word:
            self._suppress_new_chars_signal = True
            try:
                self.edit_new_chars.setText("")
            finally:
                self._suppress_new_chars_signal = False
            self._rebuild_rows_for_text("")
            return

        first = next(iter(self._iter_matches(word)), None)
        if first is not None:
            sentence, pos, n_chars = first
            chars = sentence.characters[pos : pos + n_chars]
            new_text = "".join(c.char for c in chars)
            ruby_list = [
                ",".join(p.text for p in c.ruby.parts)
                if c.ruby and c.ruby.parts
                else ""
                for c in chars
            ]
            check_list = [str(c.check_count) for c in chars]
            linked_list = [bool(c.linked_to_next) for c in chars]
        else:
            # 无匹配：用搜索词 + fallback reading
            new_text = word
            n_units = grapheme_count(word)
            if fallback_reading:
                # 逐段保留头尾空格（实义注音字符），不做 strip。
                parts = fallback_reading.split(",")
                ruby_list = [
                    parts[i] if i < len(parts) else "" for i in range(n_units)
                ]
            else:
                ruby_list = ["" for _ in range(n_units)]
            check_list = ["1" for _ in range(n_units)]
            linked_list = [False for _ in range(n_units)]

        self._suppress_new_chars_signal = True
        try:
            self.edit_new_chars.setText(new_text)
        finally:
            self._suppress_new_chars_signal = False
        self._rebuild_rows_for_text(new_text, ruby_list, check_list, linked_list)

    # ---------- 解析 ----------

    def _parse_ruby(self, raw: str, check_count: int = 1) -> Optional[Ruby]:
        """解析 ruby 文本，根据 check_count 自动分段（用 radio 实时模式）"""
        from strange_uta_game.frontend.editor.timing.dialogs import (
            parse_ruby_text,
            _radio_split_mode,
        )
        return parse_ruby_text(
            raw, check_count, _radio_split_mode(self._radio_direct, self._radio_by_char)
        )

    def _collect_per_char(
        self, new_text: str
    ) -> Tuple[List[Optional[Ruby]], List[int], List[bool]]:
        per_char_ruby: List[Optional[Ruby]] = []
        per_char_check: List[int] = []
        per_char_linked: List[bool] = []
        for i in range(grapheme_count(new_text)):
            if i >= len(self._char_rows):
                per_char_ruby.append(None)
                per_char_check.append(1)
                per_char_linked.append(False)
                continue
            _, edit_ruby, edit_check, chk_linked = self._char_rows[i]
            try:
                check_count = max(0, int(edit_check.text().strip()))
            except ValueError:
                check_count = 1
            per_char_check.append(check_count)
            per_char_ruby.append(self._parse_ruby(edit_ruby.text(), check_count))
            per_char_linked.append(bool(chk_linked.isChecked()))
        return per_char_ruby, per_char_check, per_char_linked

    # ---------- 执行 ----------

    def _on_execute(self):
        if not self._project:
            return
        word = self.edit_word.text().strip()
        if not word:
            return
        # 搜索词仅用于匹配，可 strip；替换文本写回保留原文：
        # .strip() 会吞掉有义空格，且把同长度原地改误判成换长度
        new_text = self.edit_new_chars.text()
        if not new_text.strip():
            return

        per_char_ruby, per_char_check, per_char_linked_req = self._collect_per_char(
            new_text
        )

        # 收集所有匹配（按 sentence 分组，位置升序）
        matches_by_sentence: dict = {}
        for sentence, idx, n_hit in self._iter_matches(word):
            matches_by_sentence.setdefault(id(sentence), (sentence, []))[1].append(
                (idx, n_hit)
            )
        total_matches = sum(len(v[1]) for v in matches_by_sentence.values())
        if total_matches == 0:
            self.lbl_match.setText(self.tr("找到 0 处（无改动）"))
            return

        same_len = grapheme_count(new_text) == grapheme_count(word)
        if not same_len:
            # 丢时间戳确认
            if not message_question(
                self,
                self.tr("确认批量替换"),
                self.tr(
                    "替换后字符数 ({new}) 与搜索词 ({word}) 不同，\n"
                    "将丢失全部 {n} 处匹配的时间戳。是否继续？"
                ).format(
                    new=grapheme_count(new_text),
                    word=grapheme_count(word),
                    n=total_matches,
                ),
                yes_text=self.tr("是"),
                no_text=self.tr("否"),
            ):
                return

        # 执行前快照（用于 CommandManager 的 undo/redo）
        before_sentences = deepcopy(self._project.sentences)

        self._linked_failures = []
        changed = 0
        new_units = split_graphemes(new_text)

        # 以 project.sentences 的索引记录失败位置，便于 UI 定位
        sentence_idx_map = {id(s): i for i, s in enumerate(self._project.sentences)}

        for sentence, positions in matches_by_sentence.values():
            s_idx = sentence_idx_map.get(id(sentence), -1)
            if same_len:
                # 原地修改，正向遍历即可（长度不变，索引稳定）
                for pos, n_hit in positions:
                    for i, ch_str in enumerate(new_units[:n_hit]):
                        ci = pos + i
                        if ci >= len(sentence.characters):
                            break
                        tgt = sentence.characters[ci]
                        tgt.char = ch_str
                        # 已配套 set_ruby 替换，force=True 安全（无 mora 退化）
                        tgt.set_ruby(per_char_ruby[i])
                        tgt.set_check_count(per_char_check[i], force=True)
                        tgt.push_to_ruby()
                        # linked_to_next 校验：末字/行尾禁止连词（停顿点=语气停顿点，允许连词）
                        req_linked = per_char_linked_req[i]
                        sentence_len = len(sentence.characters)
                        is_last_in_sentence = ci >= sentence_len - 1
                        if req_linked and (
                            is_last_in_sentence
                            or tgt.is_line_end
                        ):
                            reason = (
                                self.tr("最后一个字符")
                                if is_last_in_sentence
                                else self.tr("行尾")
                            )
                            self._linked_failures.append(
                                (s_idx, ci, ch_str, reason)
                            )
                            tgt.linked_to_next = False
                        else:
                            tgt.linked_to_next = req_linked
                    changed += 1
            else:
                # 替换 slice，必须倒序以保持前序位置稳定
                for pos, n_hit in sorted(positions, reverse=True):
                    old_chars = sentence.characters[pos : pos + n_hit]
                    if not old_chars:
                        continue
                    old_last_is_sentence_end = old_chars[-1].is_sentence_end
                    old_last_is_line_end = old_chars[-1].is_line_end
                    singer_id = old_chars[0].singer_id
                    new_chars = []
                    for i, ch_str in enumerate(new_units):
                        # per_char_ruby 每次新建独立 Ruby 对象避免共享
                        src_ruby = per_char_ruby[i]
                        ruby_copy = (
                            Ruby(parts=[RubyPart(text=p.text) for p in src_ruby.parts])
                            if src_ruby is not None
                            else None
                        )
                        new_ch = Character(
                            char=ch_str,
                            ruby=ruby_copy,
                            check_count=per_char_check[i],
                            singer_id=singer_id,
                            linked_to_next=False,
                            is_line_end=False,
                            is_sentence_end=False,
                        )
                        new_chars.append(new_ch)
                    if old_last_is_sentence_end:
                        new_chars[-1].is_sentence_end = True
                    if old_last_is_line_end:
                        new_chars[-1].is_line_end = True
                    # 应用 linked_to_next（需考虑该匹配点处的新末字状态）
                    new_total_len = (
                        len(sentence.characters) - len(old_chars) + len(new_chars)
                    )
                    for i, new_ch in enumerate(new_chars):
                        req_linked = per_char_linked_req[i]
                        abs_idx = pos + i
                        is_last_in_sentence = abs_idx >= new_total_len - 1
                        if req_linked and (
                            is_last_in_sentence
                            or new_ch.is_line_end
                        ):
                            reason = (
                                self.tr("最后一个字符")
                                if is_last_in_sentence
                                else self.tr("行尾")
                            )
                            self._linked_failures.append(
                                (s_idx, abs_idx, new_ch.char, reason)
                            )
                            new_ch.linked_to_next = False
                        else:
                            new_ch.linked_to_next = req_linked
                    sentence.characters[pos : pos + n_hit] = new_chars
                    changed += 1

        # 注册到词典（传入连词信息，保留连词块结构）
        if self.chk_register.isChecked():
            self._register_to_dictionary(new_text, per_char_ruby, per_char_linked_req)

        # 保存注音分段方式配置
        from strange_uta_game.frontend.editor.timing.dialogs import _save_ruby_split_mode
        _save_ruby_split_mode(self._radio_direct, self._radio_by_char, self._radio_by_mora)

        # 将本次批量变更登记为一次 CommandManager 快照命令（支持撤销/重做）
        self._register_snapshot_command(before_sentences, changed)

        # 通知父窗口刷新
        parent = self.parent()
        timing_service = getattr(parent, "_timing_service", None)
        if timing_service is not None:
            try:
                timing_service.rebuild_global_checkpoints()
            except Exception:
                pass
        reapply_offset = getattr(parent, "_reapply_global_offset", None)
        if callable(reapply_offset):
            reapply_offset()
        refresh = getattr(parent, "refresh_lyric_display", None)
        if callable(refresh):
            refresh()
        update_time_tags = getattr(parent, "_update_time_tags_display", None)
        if callable(update_time_tags):
            update_time_tags()
        update_status = getattr(parent, "_update_status", None)
        if callable(update_status):
            update_status()
        store = getattr(parent, "_store", None)
        if store is not None:
            store.notify("rubies")
            store.notify("checkpoints")
            store.notify("lyrics")
            store.notify("timetags")

        # 弹窗汇总连词失败项
        if self._linked_failures:
            self._show_linked_failures_popup()

        self.lbl_match.setText(self.tr("已修改 {n} 处").format(n=changed))
        # 一次执行后，后续搜索词变化不应再覆盖 rows（用户已 commit 过）
        self._rows_user_edited = True

    def _register_snapshot_command(self, before_sentences, changed: int) -> None:
        """将批量变更包成 SentenceSnapshotCommand 放进 CommandManager。"""
        if not self._project or changed == 0:
            return
        parent = self.parent()
        timing_service = getattr(parent, "_timing_service", None)
        if timing_service is None:
            return
        command_manager = getattr(timing_service, "command_manager", None)
        if command_manager is None:
            return
        try:
            # 延迟导入避免循环依赖
            from strange_uta_game.backend.application.commands import (
                SentenceSnapshotCommand,
            )
        except Exception:
            return
        after_sentences = deepcopy(self._project.sentences)
        word = self.edit_word.text().strip()
        description = self.tr("批量变更「{word}」（{n} 处）").format(word=word, n=changed)
        command = SentenceSnapshotCommand(
            self._project, before_sentences, after_sentences, description
        )
        command_manager.execute(command)

    def _show_linked_failures_popup(self) -> None:
        """弹窗列出连词失败项。"""
        if not self._linked_failures:
            return
        lines = []
        for s_idx, c_idx, ch, reason in self._linked_failures[:20]:
            lines.append(self.tr("  第 {s} 句 第 {c} 字「{ch}」：{reason}").format(
                s=s_idx + 1, c=c_idx + 1, ch=ch, reason=reason))
        more = ""
        if len(self._linked_failures) > 20:
            more = self.tr("\n...（还有 {n} 项未显示）").format(
                n=len(self._linked_failures) - 20)
        message_info(
            self,
            self.tr("部分连词设置未应用"),
            self.tr("以下位置为末字/行尾，不能设置连词，已自动跳过：\n\n")
            + "\n".join(lines)
            + more,
        )

    def _register_to_dictionary(
        self,
        word: str,
        per_char_ruby: List[Optional[Ruby]],
        per_char_linked: "List[bool] | None" = None,
    ):
        """将词注册到用户词典，完整保留用户设定的 Ruby parts（mora）与连词信息。"""
        try:
            from strange_uta_game.frontend.settings.settings_interface import (
                AppSettings,
            )
            from strange_uta_game.frontend.settings.app_settings import (
                build_annotated_reading,
            )

            reading = build_annotated_reading(word, per_char_ruby, per_char_linked)
            AppSettings().register_dictionary_word(word, reading)
        except Exception:
            pass
