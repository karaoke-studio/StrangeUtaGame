"""打轴编辑对话框集合。

包含以下编辑对话框：
- ``ModifyCharacterDialog`` : 批量修改字符/注音
- ``InsertGuideSymbolDialog`` : 插入制导符号
- ``CharEditDialog`` : 单字符编辑
- ``SetSingerByLineDialog`` : 按行设置演唱者
"""

from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QRect, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from strange_uta_game.frontend.font_utils import ui_font
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
)


def _make_singer_color_pixmap(colors: list, w: int = 32, h: int = 18) -> QPixmap:
    """生成演唱者颜色预览 pixmap（支持分色，从上到下均分色带）"""
    pixmap = QPixmap(w, h)
    p = QPainter(pixmap)
    n = len(colors) if colors else 1
    base = colors[0] if colors else "#CCCCCC"
    if n == 1:
        p.fillRect(QRect(0, 0, w, h), QColor(base))
    else:
        for i, c in enumerate(colors):
            y0 = int(i * h / n)
            y1 = int((i + 1) * h / n)
            p.fillRect(QRect(0, y0, w, y1 - y0), QColor(c))
    p.end()
    return pixmap
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    CaptionLabel,
    BodyLabel,
    StrongBodyLabel,
    LineEdit,
    RadioButton,
    ScrollArea,
    SpinBox,
    SwitchButton,
    setCustomStyleSheet,
)


from strange_uta_game.frontend.theme import theme, ThemeColors
from strange_uta_game.frontend.fluent_widgets import FluentGroupBox
from strange_uta_game.frontend.window_sizing import fit_to_screen

from strange_uta_game.backend.infrastructure.exporters.nicokara_exporter import (
    strip_variation_selectors,
)
from strange_uta_game.backend.domain import (
    Character,
    Ruby,
    RubyPart,
    Sentence,
    Singer,
)


def _get_ruby_split_mode() -> str:
    """获取注音分段方式配置值"""
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        settings = AppSettings()
        return settings.get("ruby_split_mode", "mora")
    except Exception:
        return "mora"


def _set_ruby_split_mode(mode: str) -> None:
    """设置注音分段方式配置值"""
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        settings = AppSettings()
        settings.set("ruby_split_mode", mode)
        settings.save()
    except Exception:
        pass


def _create_ruby_split_group(parent: QWidget) -> tuple[QRadioButton, QRadioButton, QRadioButton, FluentGroupBox]:
    """创建注音分段方式选择组

    Returns:
        (radio_direct, radio_by_char, radio_by_mora, group_box)
    """
    from PyQt6.QtCore import QCoreApplication
    # 显式 QCoreApplication.translate 让 .ts 抽取器以 "RubySplit" 为上下文
    # 收录源串（lambda 包装时抽取器无法跟踪到内嵌上下文名）。
    group_box = FluentGroupBox(
        QCoreApplication.translate("RubySplit", "注音分段方式")
    )
    group_layout = group_box.contentLayout

    radio_direct = RadioButton(QCoreApplication.translate(
        "RubySplit", "直接应用（用逗号手动分段，无逗号则不分段）"
    ))
    radio_by_char = RadioButton(QCoreApplication.translate(
        "RubySplit", "按字符均分"
    ))
    radio_by_mora = RadioButton(QCoreApplication.translate(
        "RubySplit", "按 mora 均分（推荐）"
    ))

    # 读取配置值
    mode = _get_ruby_split_mode()
    if mode == "direct":
        radio_direct.setChecked(True)
    elif mode == "char":
        radio_by_char.setChecked(True)
    else:
        radio_by_mora.setChecked(True)

    group_layout.addWidget(radio_direct)
    group_layout.addWidget(radio_by_char)
    group_layout.addWidget(radio_by_mora)

    return radio_direct, radio_by_char, radio_by_mora, group_box


def _save_ruby_split_mode(radio_direct: QRadioButton, radio_by_char: QRadioButton, radio_by_mora: QRadioButton) -> None:
    """保存注音分段方式配置值"""
    _set_ruby_split_mode(_radio_split_mode(radio_direct, radio_by_char))


def _radio_split_mode(radio_direct: QRadioButton, radio_by_char: QRadioButton) -> str:
    """读取分段方式单选组的当前选择。

    解析写回必须用本函数取 radio 实时值并显式传给 parse_ruby_text，
    不能依赖落盘配置——配置在 _save_ruby_split_mode 之前还是旧值，
    会造成预览与写回分段方式不一致。
    """
    if radio_direct.isChecked():
        return "direct"
    if radio_by_char.isChecked():
        return "char"
    return "mora"


# ── 字符编辑类对话框（修改所选字符 / 编辑字符(F2) / 批量变更）统一外观 ──
# 三窗口共用同一默认尺寸与字体层级，保证视觉一致、信息区分度高。
CHAR_DIALOG_SIZE = (600, 700)
# 字体层级（pt）：显示值 > 字符字形 > 主输入 > 字段标签/行内输入 > 说明
FONT_DIALOG_BASE = 10      # 对话框基础字体
FONT_VALUE_DISPLAY = 16    # 顶部只读"当前字符"显示值（最醒目，加粗）
FONT_CHAR_GLYPH = 14       # 每字符行的字形标签（加粗）
FONT_MAIN_INPUT = 12       # 顶部"新字符/替换为"主输入框
FONT_FIELD_LABEL = 11      # 表单字段标签（当前字符: / 新字符: / 搜索词:）
FONT_ROW_INPUT = 11        # 每字符行的注音/节奏点输入框


def char_dialog_font(size: int, bold: bool = False) -> QFont:
    """构造字符编辑类对话框统一字体。"""
    return ui_font(size, QFont.Weight.Bold if bold else QFont.Weight.Normal)


def style_quick_link_button(btn: PushButton) -> None:
    """为"快速连词/取消连词"按钮应用主题感知配色。

    沿用 timing_interface 的 setCustomStyleSheet 方案：同时传入浅色 / 深色两套
    QSS，qfluentwidgets 在主题切换时自动选用对应版本，无需手动监听 theme.changed。
    用 accent_secondary（蓝）作为底色，使其与普通"执行/关闭"按钮区分开。
    """
    name = btn.objectName() or "btnQuickLink"
    btn.setObjectName(name)

    def make_qss(tc: ThemeColors) -> str:
        bg = tc.accent_secondary
        lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
        fg = "#1a1a1a" if lum > 150 else "#ffffff"
        return (
            f"#{name} {{ background-color: {bg.name()}; color: {fg}; border: none; }}"
            f" #{name}:hover {{ background-color: {bg.lighter(115).name()}; color: {fg}; }}"
            f" #{name}:pressed {{ background-color: {bg.darker(110).name()}; color: {fg}; }}"
            f" #{name}:disabled {{ background-color: {tc.bg_hover.name()};"
            f" color: {tc.text_disabled.name()}; }}"
        )

    setCustomStyleSheet(
        btn, make_qss(ThemeColors(is_dark=False)), make_qss(ThemeColors(is_dark=True))
    )


def collect_char_dialog_state(dialog, search_word: str = "") -> dict:
    """Collect the editable fields shared by the normal and bulk dialogs."""
    return {
        "search_word": search_word,
        "new_text": dialog.edit_new_chars.text(),
        "rows": [
            (edit_ruby.text(), edit_check.text(), check_linked.isChecked())
            for _, edit_ruby, edit_check, check_linked in dialog._char_rows
        ],
        "split_mode": _radio_split_mode(dialog._radio_direct, dialog._radio_by_char),
        "register": dialog.chk_register.isChecked(),
    }


def apply_char_dialog_state(dialog, state: dict | None) -> None:
    """Restore fields when switching between normal and bulk modes."""
    if not state:
        return
    dialog.edit_new_chars.setText(state.get("new_text", dialog.edit_new_chars.text()))
    for row, values in zip(dialog._char_rows, state.get("rows", [])):
        _, edit_ruby, edit_check, check_linked = row
        ruby_text, check_text, linked = values
        edit_ruby.setText(ruby_text)
        edit_check.setText(check_text)
        check_linked.setChecked(bool(linked))
    mode = state.get("split_mode")
    if mode == "direct":
        dialog._radio_direct.setChecked(True)
    elif mode == "char":
        dialog._radio_by_char.setChecked(True)
    elif mode == "mora":
        dialog._radio_by_mora.setChecked(True)
    dialog.chk_register.setChecked(bool(state.get("register", False)))
    dialog._update_preview()


def parse_ruby_text(
    raw: str, check_count: int = 1, mode: str | None = None
) -> Ruby | None:
    """解析 ruby 文本，根据 check_count 自动分段

    规则（详见 ``inline_format.split_ruby_segments``，预览与写回共用）：
    1. 直接应用：用逗号手动分段，空段解析为占位符（停顿符）
    2. 按字符均分：始终按字符拆分，忽略逗号
    3. 按 mora 均分：始终按 mora 拆分，忽略逗号
    4. 分段数 > check_count 时多余部分合到末段，< check_count 时补占位符
       （不再静默丢弃任何分段——空串 part 曾导致 checkcount/rubyparts 失配）

    Args:
        raw: 用户输入的注音文本
        check_count: 节奏点数量
        mode: 分段方式 "direct"/"char"/"mora"；None 时读取用户配置

    Returns:
        Ruby 对象，或 None（无注音时）
    """
    from strange_uta_game.backend.infrastructure.parsers.inline_format import (
        split_ruby_segments,
    )

    if mode is None:
        mode = _get_ruby_split_mode()
    parts = split_ruby_segments(raw, check_count, mode)
    if not parts:
        return None
    return Ruby(parts=[RubyPart(text=p) for p in parts])


def apply_linked_with_validation(
    chars: list,
    linked_req: list,
    start_abs_idx: int,
    total_after: int,
    failures: list,
) -> None:
    """按用户请求应用向后连词，并统一校验末字/行尾禁止连词。

    ModifyCharacterDialog / CharEditDialog 共用同一口径：末字或行尾字符
    的连词请求被跳过（linked_to_next 强制 False），并记入 failures
    （(abs_idx, char, reason) 列表，由调用方弹窗汇总）。
    停顿点（is_sentence_end）= 语气停顿点，允许连词。

    Args:
        chars: 目标字符序列（原地写 linked_to_next）
        linked_req: 与 chars 对齐的用户连词请求
        start_abs_idx: 首字符在句中的绝对索引
        total_after: 应用后句子总字符数（用于判定末字）
        failures: 连词失败项收集列表
    """
    for i, ch in enumerate(chars):
        req_linked = bool(linked_req[i]) if i < len(linked_req) else False
        abs_idx = start_abs_idx + i
        is_last_in_sentence = abs_idx >= total_after - 1
        if req_linked and (is_last_in_sentence or ch.is_line_end):
            reason = "最后一个字符" if is_last_in_sentence else "行尾"
            failures.append((abs_idx, ch.char, reason))
            ch.linked_to_next = False
        else:
            ch.linked_to_next = req_linked


class ModifyCharacterDialog(QDialog):
    """修改所选字符对话框 — 替换选中区间的文本、注音、节奏点、连词。

    字符级独立输入框方案（批 18 #1/#2/#3）：
      - 顶部"新字符"文本框决定字符序列
      - 下方按新文本长度动态生成每字符一行：[字符] [注音] [节奏点] [向后连词]
      - 注音框内用半角逗号分隔 RubyPart（如 わ,た,し → 3 个 RubyPart）
      - 文本修改时自动重建字符行，并按位置尽量保留已输入值
      - 单字符修改时直接原地 set_ruby/check_count/linked_to_next/push_to_ruby，保留 timestamps
      - 字符数变化时才走替换 slice 流程（必然丢旧 timestamps）
      - 连词校验：末字/行尾字符禁止 linked_to_next=True（停顿点=语气停顿点，允许连词），
        提交时若有违规项则跳过该项的 linked_to_next 并在 failures 列表返回。
    """

    def __init__(
        self, sentence, start_idx, end_idx, parent=None, initial_state=None
    ):
        super().__init__(parent)
        self._sentence = sentence
        self._start_idx = start_idx
        self._end_idx = end_idx
        self._modified = False
        self._switch_to_bulk = False
        self._linked_failures: list[tuple[int, str, str]] = []
        # (pos, char, reason) 列表，执行后由调用方读取弹窗汇总
        self._char_rows: list[tuple[QLabel, QLineEdit, QLineEdit, QCheckBox]] = []

        self.setWindowTitle(self.tr("修改所选字符"))
        fit_to_screen(self, *CHAR_DIALOG_SIZE)
        self.setFont(char_dialog_font(FONT_DIALOG_BASE))

        layout = QVBoxLayout(self)

        # 原字符显示 + 新字符输入
        chars = sentence.characters[start_idx : end_idx + 1]
        current_text = "".join(c.char for c in chars)

        top_form = QFormLayout()
        lbl_current = StrongBodyLabel(current_text)
        lbl_current.setFont(char_dialog_font(FONT_VALUE_DISPLAY, bold=True))
        lbl_cur_label = BodyLabel(self.tr("当前选中字符:"))
        lbl_cur_label.setFont(char_dialog_font(FONT_FIELD_LABEL))
        current_row = QWidget(self)
        current_row_layout = QHBoxLayout(current_row)
        current_row_layout.setContentsMargins(0, 0, 0, 0)
        current_row_layout.addWidget(lbl_current)
        current_row_layout.addStretch()
        self.btn_switch_bulk = PushButton(self.tr("转为批量"), self)
        self.btn_switch_bulk.clicked.connect(self._on_switch_to_bulk)
        current_row_layout.addWidget(self.btn_switch_bulk)
        top_form.addRow(lbl_cur_label, current_row)
        self.edit_new_chars = LineEdit(self)
        self.edit_new_chars.setText(current_text)
        self.edit_new_chars.setPlaceholderText(self.tr("输入新字符"))
        self.edit_new_chars.setFont(char_dialog_font(FONT_MAIN_INPUT))
        lbl_new_label = BodyLabel(self.tr("新字符:"))
        lbl_new_label.setFont(char_dialog_font(FONT_FIELD_LABEL))
        top_form.addRow(lbl_new_label, self.edit_new_chars)
        layout.addLayout(top_form)

        # 字符级编辑区标题
        hint = CaptionLabel(self.tr("按字符编辑（注音用半角逗号分隔 RubyPart；节奏点为非负整数）:"))
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

        # 初始按当前字符填充
        for c in chars:
            ruby_str = (
                ",".join(p.text for p in c.ruby.parts) if c.ruby and c.ruby.parts else ""
            )
            self._append_char_row(c.char, ruby_str, str(c.check_count), c.linked_to_next)

        # 文本变更 → 重建行，保留已输入值
        self.edit_new_chars.textChanged.connect(self._rebuild_rows_on_text_change)

        # 注册词典 + 快速连词
        register_row = QHBoxLayout()
        self.chk_register = CheckBox(self.tr("将此词注册到读音词典"))
        register_row.addWidget(self.chk_register)
        register_row.addStretch()
        self.btn_toggle_linked = PushButton(self.tr("快速连词/取消连词"), self)
        self.btn_toggle_linked.setToolTip(self.tr(
            "若全部未连词，则将除最后一个字符外的向后连词全部勾选；否则全部取消连词"
        ))
        self.btn_toggle_linked.clicked.connect(self._on_toggle_all_linked)
        style_quick_link_button(self.btn_toggle_linked)
        register_row.addWidget(self.btn_toggle_linked)
        layout.addLayout(register_row)

        # 注音分段方式选择
        self._radio_direct, self._radio_by_char, self._radio_by_mora, ruby_split_group = _create_ruby_split_group(self)
        layout.addWidget(ruby_split_group)

        # 预览区域
        self.preview_label = CaptionLabel(self.tr("预览: "))
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        # 连接信号更新预览
        self.edit_new_chars.textChanged.connect(self._update_preview)
        self._radio_direct.toggled.connect(self._update_preview)
        self._radio_by_char.toggled.connect(self._update_preview)
        self._radio_by_mora.toggled.connect(self._update_preview)

        # 初始预览
        self._update_preview()
        self._update_toggle_linked_enabled()
        apply_char_dialog_state(self, initial_state)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_exec = PrimaryPushButton(self.tr("执行"), self)
        btn_exec.setDefault(True)
        btn_exec.clicked.connect(self._on_execute)
        btn_layout.addWidget(btn_exec)
        btn_query = PushButton(self.tr("查询候补字典"), self)
        btn_query.clicked.connect(self._on_query_dict_candidates)
        btn_layout.addWidget(btn_query)
        btn_close = PushButton(self.tr("关闭"), self)
        btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _on_switch_to_bulk(self) -> None:
        self._switch_to_bulk = True
        self.reject()

    def switch_to_bulk_requested(self) -> bool:
        return self._switch_to_bulk

    def get_switch_state(self) -> dict:
        chars = self._sentence.characters[self._start_idx : self._end_idx + 1]
        word = "".join(c.char for c in chars)
        return collect_char_dialog_state(self, word)

    def _on_query_dict_candidates(self):
        """查询候补字典：选中条目后按其格式填充并执行（关闭两窗口）。"""
        from strange_uta_game.frontend.editor.timing.dict_candidate_dialog import (
            DictCandidateDialog,
            apply_entry_to_dialog_rows,
        )

        word = self.edit_new_chars.text().strip()
        dlg = DictCandidateDialog(word, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.get_selected_entry()
        if not entry:
            return
        if apply_entry_to_dialog_rows(self, entry["word"], entry["reading"]):
            # 执行原对话框的应用逻辑（_on_execute 内部会 accept() 关闭本窗口）
            self._on_execute()

    def _append_char_row(
        self, char_str: str, ruby_str: str, check_str: str, linked: bool = False
    ):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
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
        row_layout.addWidget(lbl)
        row_layout.addWidget(edit_ruby, stretch=1)
        row_layout.addWidget(edit_check)
        row_layout.addWidget(chk_linked)
        self._rows_layout.addWidget(row_widget)
        self._char_rows.append((lbl, edit_ruby, edit_check, chk_linked))

    def _rebuild_rows_on_text_change(self, new_text: str):
        # 保留旧输入值按索引对齐
        old_vals = [
            (e_r.text(), e_c.text(), chk.isChecked())
            for _, e_r, e_c, chk in self._char_rows
        ]
        # 清空现有行
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._char_rows.clear()
        for i, ch in enumerate(new_text):
            if i < len(old_vals):
                r_val, c_val, l_val = old_vals[i]
            else:
                r_val, c_val, l_val = "", "1", False
            self._append_char_row(ch, r_val, c_val, l_val)
        # 更新预览
        self._update_preview()
        self._update_toggle_linked_enabled()

    def _on_row_user_edited(self, _text: str):
        """用户手动编辑行时更新预览"""
        self._update_preview()

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

    def _parse_ruby(self, raw: str, check_count: int = 1):
        """解析 ruby 文本，根据 check_count 自动分段（用 radio 实时模式）"""
        return parse_ruby_text(
            raw, check_count, _radio_split_mode(self._radio_direct, self._radio_by_char)
        )

    def _on_execute(self):
        from strange_uta_game.backend.domain.models import Character

        # 写回保留原文：.strip() 会吞掉有义空格，且把同长度原地改
        # 误判成换长度（长度比较用未 strip 文本）
        new_text = self.edit_new_chars.text()
        if not new_text.strip():
            return

        # 收集每行值：ruby / check_count / linked_to_next
        per_char_ruby = []
        per_char_check = []
        per_char_linked_req = []  # 用户请求的 linked_to_next
        for i in range(len(new_text)):
            if i >= len(self._char_rows):
                per_char_ruby.append(None)
                per_char_check.append(1)
                per_char_linked_req.append(False)
                continue
            _, edit_ruby, edit_check, chk_linked = self._char_rows[i]
            try:
                check_count = max(0, int(edit_check.text().strip()))
            except ValueError:
                check_count = 1
            per_char_check.append(check_count)
            per_char_ruby.append(self._parse_ruby(edit_ruby.text(), check_count))
            per_char_linked_req.append(bool(chk_linked.isChecked()))

        old_chars = self._sentence.characters[self._start_idx : self._end_idx + 1]
        old_last_is_sentence_end = old_chars[-1].is_sentence_end if old_chars else False
        old_last_is_line_end = old_chars[-1].is_line_end if old_chars else False
        singer_id = old_chars[0].singer_id if old_chars else ""

        self._linked_failures = []

        if len(new_text) == len(old_chars):
            # 字符数不变 → 原地修改，保留 timestamps 和 offset
            for i, ch_str in enumerate(new_text):
                tgt = old_chars[i]
                tgt.char = ch_str
                # 已配套 set_ruby 替换，force=True 安全（无 mora 退化）
                tgt.set_ruby(per_char_ruby[i])
                tgt.set_check_count(per_char_check[i], force=True)
                tgt.push_to_ruby()
            # linked_to_next 校验：末字/行尾禁止连词（停顿点=语气停顿点，可以连词）
            apply_linked_with_validation(
                old_chars, per_char_linked_req, self._start_idx,
                len(self._sentence.characters), self._linked_failures,
            )
            # 停顿点 / 行末由原字符保留，不动 is_sentence_end / is_line_end
        else:
            # 字符数变化 → 替换 slice（无法保留 timestamps）
            new_chars = []
            for i, ch_str in enumerate(new_text):
                new_ch = Character(
                    char=ch_str,
                    ruby=per_char_ruby[i],
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
            # 应用 linked_to_next（需与新的停顿点/行尾/末字状态校验）
            total_after = (
                len(self._sentence.characters) - len(old_chars) + len(new_chars)
            )
            apply_linked_with_validation(
                new_chars, per_char_linked_req, self._start_idx,
                total_after, self._linked_failures,
            )
            self._sentence.characters[self._start_idx : self._end_idx + 1] = new_chars

        # 词典注册：传 Ruby 对象列表 + 连词信息，完整保留用户设定
        if self.chk_register.isChecked():
            self._register_to_dictionary(new_text, per_char_ruby, per_char_linked_req)

        # 保存注音分段方式配置
        _save_ruby_split_mode(self._radio_direct, self._radio_by_char, self._radio_by_mora)

        self._modified = True
        self.accept()

    def get_linked_failures(self) -> list[tuple[int, str, str]]:
        """返回应用连词时因末字/行尾被跳过的项列表（abs_idx, char, reason）。"""
        return list(self._linked_failures)

    def _register_to_dictionary(self, word: str, per_char_ruby: list, per_char_linked: list | None = None):
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

    def was_modified(self) -> bool:
        return self._modified


def _find_prev_timestamp(sentence, char_idx: int) -> int:
    """向前搜索最近的一个时间戳（含停顿点时间戳）作为时间起点。

    从 char_idx-1 往前逐字符查找，取最近一个有时间戳字符的最大时间戳。
    搜索不到时返回 0（歌曲开始处 00:00:00）。
    """
    for i in range(char_idx - 1, -1, -1):
        ts_list = sentence.characters[i].all_timestamps
        if ts_list:
            return max(ts_list)
    return 0


def _build_guide_chars(sentence, char_idx, symbol, count, duration_ms, reverse):
    """构建要插入的导唱 Character 列表（不写回 sentence）。

    返回 ``(guide_chars, clamped)``，``clamped`` 表示有时间戳被钳到 0。
    时间戳赋给每个符号组的第一个字符，其余字符 check_count=0 且与后字连词。
    """
    ref_char = sentence.characters[char_idx]
    singer_id = ref_char.singer_id
    ref_ts = ref_char.timestamps[0] if ref_char.timestamps else None
    clamped = False
    guide_chars = []
    for i in range(count):
        for j, ch_str in enumerate(symbol):
            is_first_of_symbol = j == 0
            is_last_symbol = i == count - 1
            is_last_char_of_last_symbol = (j == len(symbol) - 1) and is_last_symbol
            new_ch = Character(
                char=ch_str,
                ruby=None,
                check_count=1 if is_first_of_symbol else 0,
                singer_id=singer_id,
                linked_to_next=not is_last_char_of_last_symbol,
                is_guide=True,
            )
            if ref_ts is not None and is_first_of_symbol:
                if reverse:
                    ts = ref_ts - duration_ms * (i + 1)
                else:
                    ts = ref_ts - duration_ms * (count - i)
                if ts < 0:
                    ts = 0
                    clamped = True
                new_ch.add_timestamp(ts)
            guide_chars.append(new_ch)
    # 反向导唱的尾部 [>…] 标记：外部渲染软件要求在最后一个导唱字符上
    # 附上停顿点时间戳——继续等差数列再往下推一步（末个导唱时间戳再减
    # 一个间隔 = ref_ts - (count+1)*duration），非反向不插入。
    if reverse and ref_ts is not None and guide_chars:
        end_ts = ref_ts - duration_ms * (count + 1)
        if end_ts < 0:
            end_ts = 0
            clamped = True
        last_ch = guide_chars[-1]
        last_ch.is_sentence_end = True
        last_ch.set_sentence_end_ts(end_ts)
    return guide_chars, clamped


def insert_guide_before(sentence, char_idx, symbol, count, manual_duration_ms, reverse, fill_gap):
    """计算并在 ``char_idx`` 前插入导唱符。不弹任何提示，结果由调用方处理。

    返回 dict::

        {
            "ok": bool,             # 是否完成插入
            "reason": None | "no_end_ts" | "invalid_gap",
            "start_ts": int,        # reason=="invalid_gap" 时有意义
            "end_ts": int,          # reason=="invalid_gap" 时有意义
            "inserted": int,        # 插入的字符数
            "clamped": bool,        # 是否有时间戳被钳到 0
        }
    """
    ref_char = sentence.characters[char_idx]
    ref_ts = ref_char.timestamps[0] if ref_char.timestamps else None

    if fill_gap:
        # 补足间隔时间：起点 = 向前最近的时间戳（搜索不到则 0），
        # 终点 = 本字符首个时间戳，按个数平均分配。
        if ref_ts is None:
            return {"ok": False, "reason": "no_end_ts"}
        start_ts = _find_prev_timestamp(sentence, char_idx)
        if start_ts >= ref_ts:
            return {"ok": False, "reason": "invalid_gap", "start_ts": start_ts, "end_ts": ref_ts}
        duration_ms = (ref_ts - start_ts) // count
    else:
        duration_ms = manual_duration_ms

    guide_chars, clamped = _build_guide_chars(sentence, char_idx, symbol, count, duration_ms, reverse)
    for idx, gc in enumerate(guide_chars):
        sentence.characters.insert(char_idx + idx, gc)
    return {"ok": True, "reason": None, "inserted": len(guide_chars), "clamped": clamped}


class InsertGuideSymbolDialog(QDialog):
    """插入导唱符对话框 — 在选中字符前插入导唱用字符"""

    def __init__(self, sentence, char_idx, parent=None):
        """
        Args:
            sentence: Sentence object
            char_idx: current selected char index (guide symbols insert BEFORE this)
            parent: parent widget
        """
        super().__init__(parent)
        self._sentence = sentence
        self._char_idx = char_idx
        self._modified = False
        self._clear_marker_requested = False
        self._fill_all_requested = False
        self._fill_all_params = None

        # 从 AppSettings 读取记忆的设置
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        settings = AppSettings()
        saved_symbol = settings.get("timing.guide_symbol", "")
        saved_count = settings.get("timing.guide_count", 1)
        saved_duration = settings.get("timing.guide_duration_ms", 1000)

        saved_reverse = settings.get("timing.guide_reverse", False)
        saved_fill_gap = settings.get("timing.guide_fill_gap", False)

        self.setWindowTitle(self.tr("插入导唱符"))
        fit_to_screen(self, 400, 360)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)

        form = QFormLayout()

        # Field 1: Current selected char (readonly)
        ch = sentence.characters[char_idx]
        lbl_current = QLabel(ch.char)
        lbl_current.setStyleSheet("font-size: 16px; font-weight: bold;")
        form.addRow(self.tr("当前选中字符:"), lbl_current)

        # Field 2: Guide symbol text
        self.edit_symbol = LineEdit(self)
        self.edit_symbol.setText(saved_symbol)
        self.edit_symbol.setPlaceholderText(self.tr("请填写要插入的导唱符"))
        form.addRow(self.tr("导唱符:"), self.edit_symbol)

        # Field 3: Count
        self.edit_count = LineEdit(self)
        self.edit_count.setText(str(saved_count))
        self.edit_count.setPlaceholderText(self.tr("个数"))
        form.addRow(self.tr("个数:"), self.edit_count)

        # Field 4: Duration per symbol
        self.edit_duration = LineEdit(self)
        self.edit_duration.setText(str(saved_duration))
        self.edit_duration.setPlaceholderText(self.tr("每个导唱符持续时间（毫秒）"))
        form.addRow(self.tr("持续时间 (ms):"), self.edit_duration)

        # Field 5: Fill gap — 补足间隔时间
        # 勾选后忽略手动持续时间，自动在「前一个时间戳」与「本字符首个时间戳」
        # 之间平均分配，前一个时间戳搜索不到时以 0ms（歌曲开始）为起点。
        self.chk_fill_gap = CheckBox(self.tr("补足间隔时间"))
        self.chk_fill_gap.setChecked(bool(saved_fill_gap))
        self.chk_fill_gap.toggled.connect(self._on_fill_gap_toggled)
        form.addRow("", self.chk_fill_gap)

        # Field 6: Reverse timestamp order
        self.chk_reverse = CheckBox(self.tr("时间戳反向"))
        self.chk_reverse.setChecked(bool(saved_reverse))
        form.addRow("", self.chk_reverse)

        # 初始化持续时间输入框可用状态
        self._on_fill_gap_toggled(self.chk_fill_gap.isChecked())

        layout.addLayout(form)
        layout.addStretch()

        # 填充所有 Todo 导唱：扫描整个项目里所有 needs_guide 标记的字符，
        # 按当前对话框数值批量插入导唱符。实际执行交由调用方（含项目与撤销栈）。
        todo_count = None
        proj = getattr(parent, "_project", None)
        if proj is not None:
            try:
                todo_count = sum(
                    1
                    for s in proj.sentences
                    for c in s.characters
                    if getattr(c, "needs_guide", False)
                )
            except Exception:
                todo_count = None
        fill_all_layout = QHBoxLayout()
        self.btn_fill_all = PushButton(self.tr("填充所有导唱待办"), self)
        if todo_count is not None:
            self.btn_fill_all.setText(
                self.tr("填充所有导唱待办 ({n})").format(n=todo_count)
            )
            self.btn_fill_all.setEnabled(todo_count > 0)
        self.btn_fill_all.setToolTip(
            self.tr("扫描整个项目中所有导唱待办标记，按当前数值批量插入导唱符")
        )
        self.btn_fill_all.clicked.connect(self._on_fill_all)
        fill_all_layout.addWidget(self.btn_fill_all)
        fill_all_layout.addStretch()
        layout.addLayout(fill_all_layout)

        # Buttons
        btn_layout = QHBoxLayout()
        # 清除导唱标记：仅在当前字符已有 needs_guide 标记时启用
        self.btn_clear_marker = PushButton(self.tr("清除导唱标记"), self)
        self.btn_clear_marker.setEnabled(bool(getattr(ch, "needs_guide", False)))
        self.btn_clear_marker.clicked.connect(self._on_clear_marker)
        btn_layout.addWidget(self.btn_clear_marker)
        btn_layout.addStretch()
        btn_exec = PrimaryPushButton(self.tr("执行"), self)
        btn_exec.setDefault(True)
        btn_exec.clicked.connect(self._on_execute)
        btn_layout.addWidget(btn_exec)
        btn_close = PushButton(self.tr("关闭"), self)
        btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _on_clear_marker(self):
        """点击"清除导唱标记"：设置请求标志并关闭对话框。

        不直接改 Character —— 由调用方统一通过命令系统执行，
        以保证撤销/重做语义一致。
        """
        self._clear_marker_requested = True
        self.accept()

    def was_clear_marker_requested(self) -> bool:
        return self._clear_marker_requested

    def _on_fill_gap_toggled(self, checked: bool):
        """勾选「补足间隔时间」时禁用手动持续时间输入框"""
        self.edit_duration.setEnabled(not checked)

    def _read_params(self):
        """从输入框读取并校验参数。

        返回 ``(symbol, count, manual_duration_ms, reverse, fill_gap)``；
        symbol 为空时返回 ``None``（调用方据此中止）。
        """
        symbol = strip_variation_selectors(self.edit_symbol.text().strip())
        if not symbol:
            return None
        try:
            count = max(1, int(self.edit_count.text().strip()))
        except ValueError:
            count = 1
        reverse = self.chk_reverse.isChecked()
        fill_gap = self.chk_fill_gap.isChecked()
        if fill_gap:
            manual_duration_ms = 0
        else:
            try:
                manual_duration_ms = max(100, int(self.edit_duration.text().strip()))
            except ValueError:
                manual_duration_ms = 1000
        return symbol, count, manual_duration_ms, reverse, fill_gap

    def _save_params(self, symbol, count, manual_duration_ms, reverse, fill_gap):
        """保存参数到 AppSettings（fill_gap 时不覆盖手动持续时间记忆）。"""
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        settings = AppSettings()
        settings.set("timing.guide_symbol", symbol)
        settings.set("timing.guide_count", count)
        if not fill_gap:
            settings.set("timing.guide_duration_ms", manual_duration_ms)
        settings.set("timing.guide_reverse", reverse)
        settings.set("timing.guide_fill_gap", fill_gap)
        settings.save()

    def _warn_insert_failure(self, result):
        """针对 insert_guide_before 失败结果弹出对应提示。"""
        reason = result.get("reason")
        if reason == "no_end_ts":
            InfoBar.warning(
                title=self.tr("无法补足间隔时间"),
                content=self.tr("当前字符没有时间戳，无法确定间隔终点。"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        elif reason == "invalid_gap":
            InfoBar.warning(
                title=self.tr("无法补足间隔时间"),
                content=self.tr("起点 {start}ms 不早于终点 {end}ms，间隔无效。").format(
                    start=result.get("start_ts", 0), end=result.get("end_ts", 0)
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _on_execute(self):
        params = self._read_params()
        if params is None:
            return
        symbol, count, manual_duration_ms, reverse, fill_gap = params

        self._save_params(symbol, count, manual_duration_ms, reverse, fill_gap)

        result = insert_guide_before(
            self._sentence, self._char_idx, symbol, count,
            manual_duration_ms, reverse, fill_gap,
        )
        if not result["ok"]:
            self._warn_insert_failure(result)
            return
        if result["clamped"]:
            InfoBar.warning(
                title=self.tr("时间戳越界"),
                content=self.tr("部分导唱符时间戳小于0，已自动设为0ms"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        self._modified = True
        self.accept()

    def _on_fill_all(self):
        """点击"填充所有导唱待办"：记下参数并关闭对话框。

        不直接改 Character —— 由调用方扫描整个项目里的 needs_guide 标记，
        统一执行并登记到撤销栈，以保证撤销/重做语义一致。
        """
        params = self._read_params()
        if params is None:
            return
        symbol, count, manual_duration_ms, reverse, fill_gap = params
        self._save_params(symbol, count, manual_duration_ms, reverse, fill_gap)
        self._fill_all_params = {
            "symbol": symbol,
            "count": count,
            "duration_ms": manual_duration_ms,
            "reverse": reverse,
            "fill_gap": fill_gap,
        }
        self._fill_all_requested = True
        self.accept()

    def was_fill_all_requested(self) -> bool:
        return self._fill_all_requested

    def fill_all_params(self):
        return self._fill_all_params

    def was_modified(self) -> bool:
        return self._modified


class CharEditDialog(QDialog):
    """注音编辑对话框 — 支持连词（Ruby 合并/拆分）和 CheckCount 编辑

    与 ModifyCharacterDialog 类似，但用于 F2 快捷键触发：
    - 直接获取对应字符的整个连词情况
    - 支持全文件替换功能

    UI 布局：
    - 当前字符显示（只读）
    - 新字符输入
    - 每字符一行：[字符] [注音] [节奏点] [向后连词]
    - 处理方式选择（直接应用/按字符均分/按 mora 均分）
    - 预览区域
    - 全文件替换选项
    - 确定/取消按钮
    """

    def __init__(
        self, sentence: "Sentence", char_idx: int, parent=None, initial_state=None
    ):
        super().__init__(parent)
        self._sentence = sentence
        self._char_idx = char_idx
        self._modified = False
        self._switch_to_bulk = False
        self._linked_failures: list[tuple[int, str, str]] = []
        self._char_rows: list[tuple[QLabel, QLineEdit, QLineEdit, QCheckBox]] = []

        self.setWindowTitle(self.tr("编辑字符"))
        fit_to_screen(self, *CHAR_DIALOG_SIZE)
        self.setFont(char_dialog_font(FONT_DIALOG_BASE))

        layout = QVBoxLayout(self)

        # 当前字符（只读）— 显示连词组内所有字符
        ch = sentence.characters[char_idx]
        # 查找连词组范围
        word_start, word_end = sentence.get_word_char_range(char_idx)
        word_len = word_end - word_start

        if word_len > 1:
            display = " + ".join(
                sentence.characters[i].char for i in range(word_start, word_end)
            )
        else:
            display = ch.char

        top_form = QFormLayout()
        lbl_current = StrongBodyLabel(display)
        lbl_current.setFont(char_dialog_font(FONT_VALUE_DISPLAY, bold=True))
        lbl_cur_label = BodyLabel(self.tr("当前字符:"))
        lbl_cur_label.setFont(char_dialog_font(FONT_FIELD_LABEL))
        current_row = QWidget(self)
        current_row_layout = QHBoxLayout(current_row)
        current_row_layout.setContentsMargins(0, 0, 0, 0)
        current_row_layout.addWidget(lbl_current)
        current_row_layout.addStretch()
        self.btn_switch_bulk = PushButton(self.tr("转为批量"), self)
        self.btn_switch_bulk.clicked.connect(self._on_switch_to_bulk)
        current_row_layout.addWidget(self.btn_switch_bulk)
        top_form.addRow(lbl_cur_label, current_row)
        # 新字符输入框只包含字符本身，不包含 " + "
        self.edit_new_chars = LineEdit(self)
        self.edit_new_chars.setText("".join(
            sentence.characters[i].char for i in range(word_start, word_end)
        ))
        self.edit_new_chars.setPlaceholderText(self.tr("输入新字符"))
        self.edit_new_chars.setFont(char_dialog_font(FONT_MAIN_INPUT))
        lbl_new_label = BodyLabel(self.tr("新字符:"))
        lbl_new_label.setFont(char_dialog_font(FONT_FIELD_LABEL))
        top_form.addRow(lbl_new_label, self.edit_new_chars)
        layout.addLayout(top_form)

        # 字符级编辑区标题
        hint = CaptionLabel(self.tr("按字符编辑（注音用半角逗号分隔 RubyPart；节奏点为非负整数）:"))
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

        # 初始按当前字符填充
        for i in range(word_start, word_end):
            c = sentence.characters[i]
            ruby_str = (
                ",".join(p.text for p in c.ruby.parts) if c.ruby and c.ruby.parts else ""
            )
            self._append_char_row(c.char, ruby_str, str(c.check_count), c.linked_to_next)

        # 文本变更 → 重建行，保留已输入值
        self.edit_new_chars.textChanged.connect(self._rebuild_rows_on_text_change)

        # 注册词典 + 快速连词
        register_row = QHBoxLayout()
        self.chk_register = CheckBox(self.tr("将此词注册到读音词典"))
        register_row.addWidget(self.chk_register)
        register_row.addStretch()
        self.btn_toggle_linked = PushButton(self.tr("快速连词/取消连词"), self)
        self.btn_toggle_linked.setToolTip(self.tr(
            "若全部未连词，则将除最后一个字符外的向后连词全部勾选；否则全部取消连词"
        ))
        self.btn_toggle_linked.clicked.connect(self._on_toggle_all_linked)
        style_quick_link_button(self.btn_toggle_linked)
        register_row.addWidget(self.btn_toggle_linked)
        layout.addLayout(register_row)

        # 注音分段方式选择
        self._radio_direct, self._radio_by_char, self._radio_by_mora, ruby_split_group = _create_ruby_split_group(self)
        layout.addWidget(ruby_split_group)

        # 预览区域
        self.preview_label = CaptionLabel(self.tr("预览: "))
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        # 连接信号更新预览
        self.edit_new_chars.textChanged.connect(self._update_preview)
        self._radio_direct.toggled.connect(self._update_preview)
        self._radio_by_char.toggled.connect(self._update_preview)
        self._radio_by_mora.toggled.connect(self._update_preview)

        self._word_start = word_start
        self._word_end = word_end

        # 初始预览
        self._update_preview()
        self._update_toggle_linked_enabled()
        apply_char_dialog_state(self, initial_state)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_ok = PrimaryPushButton(self.tr("确定"), self)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_accept)
        btn_query = PushButton(self.tr("查询候补字典"), self)
        btn_query.clicked.connect(self._on_query_dict_candidates)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_query)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _on_switch_to_bulk(self) -> None:
        self._switch_to_bulk = True
        self.reject()

    def switch_to_bulk_requested(self) -> bool:
        return self._switch_to_bulk

    def get_switch_state(self) -> dict:
        # ``display`` uses " + " only as a visual linked-word separator.  The
        # bulk search must receive the actual contiguous characters.
        word = "".join(
            self._sentence.characters[i].char
            for i in range(self._word_start, self._word_end)
        )
        return collect_char_dialog_state(self, word)

    def _on_query_dict_candidates(self):
        """查询候补字典：选中条目后按其格式填充并执行（关闭两窗口）。"""
        from strange_uta_game.frontend.editor.timing.dict_candidate_dialog import (
            DictCandidateDialog,
            apply_entry_to_dialog_rows,
        )

        word = self.edit_new_chars.text().strip()
        dlg = DictCandidateDialog(word, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.get_selected_entry()
        if not entry:
            return
        if apply_entry_to_dialog_rows(self, entry["word"], entry["reading"]):
            # _on_accept 内部会 accept() 关闭本窗口
            self._on_accept()

    def _append_char_row(
        self, char_str: str, ruby_str: str, check_str: str, linked: bool = False
    ):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
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
        row_layout.addWidget(lbl)
        row_layout.addWidget(edit_ruby, stretch=1)
        row_layout.addWidget(edit_check)
        row_layout.addWidget(chk_linked)
        self._rows_layout.addWidget(row_widget)
        self._char_rows.append((lbl, edit_ruby, edit_check, chk_linked))

    def _rebuild_rows_on_text_change(self, new_text: str):
        # 保留旧输入值按索引对齐
        old_vals = [
            (e_r.text(), e_c.text(), chk.isChecked())
            for _, e_r, e_c, chk in self._char_rows
        ]
        # 清空现有行
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._char_rows.clear()
        for i, ch in enumerate(new_text):
            if i < len(old_vals):
                r_val, c_val, l_val = old_vals[i]
            else:
                r_val, c_val, l_val = "", "1", False
            self._append_char_row(ch, r_val, c_val, l_val)
        # 更新预览
        self._update_preview()
        self._update_toggle_linked_enabled()

    def _on_row_user_edited(self, _text: str):
        """用户手动编辑行时更新预览"""
        self._update_preview()

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

    def _on_accept(self):
        # 写回保留原文：.strip() 会吞掉有义空格，且把同长度原地改
        # 误判成换长度（长度比较用未 strip 文本）
        new_text = self.edit_new_chars.text()

        if not new_text.strip():
            self.accept()
            return

        # 收集每行值：ruby / check_count / linked_to_next
        per_char_ruby = []
        per_char_check = []
        per_char_linked_req = []
        for i in range(len(new_text)):
            if i >= len(self._char_rows):
                per_char_ruby.append(None)
                per_char_check.append(1)
                per_char_linked_req.append(False)
                continue
            _, edit_ruby, edit_check, chk_linked = self._char_rows[i]
            try:
                check_count = max(0, int(edit_check.text().strip()))
            except ValueError:
                check_count = 1
            per_char_check.append(check_count)
            per_char_ruby.append(
                parse_ruby_text(
                    edit_ruby.text(),
                    check_count,
                    _radio_split_mode(self._radio_direct, self._radio_by_char),
                )
            )
            per_char_linked_req.append(bool(chk_linked.isChecked()))

        # 应用到当前连词组
        old_chars = [self._sentence.characters[i] for i in range(self._word_start, self._word_end)]
        old_last_is_sentence_end = old_chars[-1].is_sentence_end if old_chars else False
        old_last_is_line_end = old_chars[-1].is_line_end if old_chars else False

        self._linked_failures = []

        if len(new_text) == len(old_chars):
            # 字符数不变 → 原地修改
            for i, ch_str in enumerate(new_text):
                tgt = old_chars[i]
                tgt.char = ch_str
                tgt.set_ruby(per_char_ruby[i])
                tgt.set_check_count(per_char_check[i], force=True)
                tgt.push_to_ruby()
            # 连词校验与 ModifyCharacterDialog 同口径：末字/行尾禁止连词
            apply_linked_with_validation(
                old_chars, per_char_linked_req, self._word_start,
                len(self._sentence.characters), self._linked_failures,
            )
        else:
            # 字符数变化 → 替换 slice
            singer_id = old_chars[0].singer_id if old_chars else ""
            new_chars = []
            for i, ch_str in enumerate(new_text):
                new_ch = Character(
                    char=ch_str,
                    ruby=per_char_ruby[i],
                    check_count=per_char_check[i],
                    singer_id=singer_id,
                    linked_to_next=False,
                    is_line_end=False,
                    is_sentence_end=False,
                )
                new_chars.append(new_ch)
            # 行末 / 停顿点标志回填到新末字（换长度丢时间戳，但语义标志必须保留）
            if old_last_is_sentence_end and new_chars:
                new_chars[-1].is_sentence_end = True
            if old_last_is_line_end and new_chars:
                new_chars[-1].is_line_end = True
            # 连词按用户勾选应用 + 末字/行尾校验（与 ModifyCharacterDialog 同口径）
            total_after = (
                len(self._sentence.characters) - len(old_chars) + len(new_chars)
            )
            apply_linked_with_validation(
                new_chars, per_char_linked_req, self._word_start,
                total_after, self._linked_failures,
            )
            self._sentence.characters[self._word_start:self._word_end] = new_chars

        self._modified = True

        # 词典注册：传 Ruby 对象列表 + 连词信息，完整保留用户设定
        if self.chk_register.isChecked():
            self._register_to_dictionary(new_text, per_char_ruby, per_char_linked_req)

        # 保存注音分段方式配置
        _save_ruby_split_mode(self._radio_direct, self._radio_by_char, self._radio_by_mora)

        self.accept()

    def was_modified(self) -> bool:
        return self._modified

    def get_linked_failures(self) -> list[tuple[int, str, str]]:
        """返回应用连词时因末字/行尾被跳过的项列表（abs_idx, char, reason）。"""
        return list(self._linked_failures)

    def _register_to_dictionary(self, word: str, per_char_ruby: list, per_char_linked: list | None = None):
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


# ──────────────────────────────────────────────
# 按行设置演唱者对话框
# ──────────────────────────────────────────────


class SetSingerByLineDialog(QDialog):
    """按行设置演唱者对话框 — 批量为多行设置演唱者。

    显示所有行（只读），用户可通过复选框选择多行，
    然后从下拉列表中选择演唱者来批量设置。
    点击"应用"按钮后不关闭对话框，方便继续设置其他行。
    """

    apply_requested = pyqtSignal(dict)  # {line_idx: singer_id}
    preview_line_requested = pyqtSignal(int)  # line_idx

    def __init__(self, sentences: list[Sentence], singers: list[Singer], parent=None,
                 focus_line_idx: int = -1):
        super().__init__(parent)
        self._sentences = sentences
        self._singers = singers
        self._modified = False
        self._focus_line_idx = focus_line_idx
        # 历次「应用」累计的 {line_idx: singer_id}，result_map() 读取
        self._result_map: dict[int, str] = {}

        # 构建 singer_id -> Singer 映射
        self._singer_map = {s.id: s for s in singers}

        self.setWindowTitle(self.tr("按行设置演唱者"))
        fit_to_screen(self, 1200, 900)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)

        # 提示标签
        hint = CaptionLabel(self.tr("选择要设置演唱者的行：点击切换选择，Shift+点击范围选择，然后从下方选择演唱者，点击「应用」执行："))
        layout.addWidget(hint)

        # 行列表表格
        self.table = QTableWidget(len(sentences), 4, self)
        self.table.setHorizontalHeaderLabels([
            self.tr("选择"), self.tr("行号"), self.tr("歌词内容"), self.tr("当前演唱者"),
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 500)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        self._last_clicked_row = self._focus_line_idx if 0 <= self._focus_line_idx < len(sentences) else -1
        self.table.viewport().installEventFilter(self)

        for idx, sentence in enumerate(sentences):
            # 复选框
            chk = CheckBox()
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(idx, 0, chk_widget)

            # 行号
            line_num_item = QTableWidgetItem(str(idx + 1))
            line_num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 1, line_num_item)

            # 歌词内容（只读）
            text = sentence.text if sentence.characters else self.tr("(空行)")
            text_item = QTableWidgetItem(text)
            self.table.setItem(idx, 2, text_item)

            # 当前演唱者（只读）- 显示行内所有不同的演唱者
            singer_names = self._get_singer_names_for_sentence(sentence)
            singer_item = QTableWidgetItem(singer_names)
            singer_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 3, singer_item)

        layout.addWidget(self.table, stretch=1)

        if 0 <= self._last_clicked_row < len(sentences):
            self._highlight_row(self._last_clicked_row)

        # 全选/全不选按钮
        select_layout = QHBoxLayout()
        btn_select_all = PushButton(self.tr("全选"), self)
        btn_select_all.clicked.connect(self._select_all)
        btn_deselect_all = PushButton(self.tr("全不选"), self)
        btn_deselect_all.clicked.connect(self._deselect_all)
        select_layout.addWidget(btn_select_all)
        select_layout.addWidget(btn_deselect_all)
        select_layout.addStretch()
        layout.addLayout(select_layout)

        # 演唱者选择区（名称过滤 + 分组过滤 + 列表预览）
        singer_group = FluentGroupBox(self.tr("设置演唱者为"), self)
        sg_layout = singer_group.contentLayout
        sg_layout.setSpacing(4)

        # 过滤行
        sg_filter_row = QHBoxLayout()
        sg_filter_row.addWidget(QLabel(self.tr("名称:")))
        self.singer_name_filter = LineEdit(self)
        self.singer_name_filter.setPlaceholderText(self.tr("过滤名称..."))
        self.singer_name_filter.textChanged.connect(self._apply_singer_filter)
        sg_filter_row.addWidget(self.singer_name_filter, stretch=1)

        sg_filter_row.addWidget(QLabel(self.tr("分组:")))
        self.singer_group_filter = ComboBox(self)
        self.singer_group_filter.addItem(self.tr("全部"), userData="")
        _groups = sorted({s.group for s in singers if s.group})
        if any(not s.group for s in singers):
            self.singer_group_filter.addItem(self.tr("（无分组）"), userData="\x00nogroup")
        for _g in _groups:
            self.singer_group_filter.addItem(_g, userData=_g)
        self.singer_group_filter.currentIndexChanged.connect(self._apply_singer_filter)
        sg_filter_row.addWidget(self.singer_group_filter)
        sg_layout.addLayout(sg_filter_row)

        # 演唱者列表（带颜色图标预览）
        self.singer_list = QListWidget(self)
        self.singer_list.setFixedHeight(160)
        self.singer_list.setIconSize(QSize(36, 18))
        for singer in singers:
            item = QListWidgetItem()
            label = singer.name
            if singer.group:
                label += f"  [{singer.group}]"
            item.setText(label)
            item.setIcon(QIcon(_make_singer_color_pixmap(singer.get_all_colors(), 36, 18)))
            item.setData(Qt.ItemDataRole.UserRole, singer.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, singer.group)
            self.singer_list.addItem(item)
        sg_layout.addWidget(self.singer_list)

        layout.addWidget(singer_group)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_apply = PrimaryPushButton(self.tr("应用"), self)
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_apply)
        btn_close = PushButton(self.tr("关闭"), self)
        btn_close.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_apply)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _get_singer_names_for_sentence(self, sentence: Sentence) -> str:
        """获取句子内所有不同的演唱者名称，用逗号分隔"""
        if not sentence.characters:
            return ""
        singer_ids = set()
        for ch in sentence.characters:
            if ch.singer_id:
                singer_ids.add(ch.singer_id)
        if not singer_ids:
            return ""
        names = []
        for sid in singer_ids:
            singer = self._singer_map.get(sid)
            names.append(singer.name if singer else "未知")
        return ", ".join(names)

    def _select_all(self):
        """全选所有行"""
        for idx in range(self.table.rowCount()):
            widget = self.table.cellWidget(idx, 0)
            if widget:
                chk = widget.findChild(QCheckBox)
                if chk:
                    chk.setChecked(True)
        self._last_clicked_row = -1

    def _deselect_all(self):
        """全不选"""
        for idx in range(self.table.rowCount()):
            widget = self.table.cellWidget(idx, 0)
            if widget:
                chk = widget.findChild(QCheckBox)
                if chk:
                    chk.setChecked(False)
        self._last_clicked_row = -1

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            row = self.table.rowAt(pos.y())
            if row >= 0:
                column = self.table.columnAt(pos.x())
                modifiers = QApplication.keyboardModifiers()
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self._select_range(row)
                else:
                    self._toggle_row(row)
                if column == 2:
                    self.preview_line_requested.emit(row)
                return True
        return super().eventFilter(obj, event)

    def _get_checkbox(self, row: int):
        widget = self.table.cellWidget(row, 0)
        if widget:
            return widget.findChild(QCheckBox)
        return None

    def _select_range(self, row: int):
        if self._last_clicked_row < 0:
            self._last_clicked_row = row
        start = min(self._last_clicked_row, row)
        end = max(self._last_clicked_row, row)
        for i in range(start, end + 1):
            chk = self._get_checkbox(i)
            if chk:
                chk.setChecked(True)
        self._last_clicked_row = row
        self._highlight_row(row)

    def _toggle_row(self, row: int):
        chk = self._get_checkbox(row)
        if chk:
            chk.setChecked(not chk.isChecked())
        self._last_clicked_row = row
        self._highlight_row(row)

    def _highlight_row(self, row: int):
        normal_bg = QColor(0, 0, 0, 0)
        highlight_bg = theme.bg_selected
        for r in range(self.table.rowCount()):
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                if item:
                    item.setBackground(highlight_bg if r == row else normal_bg)
        focus_item = self.table.item(row, 2)
        if focus_item:
            self.table.scrollToItem(focus_item, QTableWidget.ScrollHint.EnsureVisible)

    def _apply_singer_filter(self):
        """按名称/分组过滤演唱者列表"""
        name_text = self.singer_name_filter.text().strip().lower()
        group_val = self.singer_group_filter.currentData() or ""
        for i in range(self.singer_list.count()):
            item = self.singer_list.item(i)
            item_text = item.text().lower()
            item_group = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            name_ok = not name_text or name_text in item_text
            if group_val == "\x00nogroup":
                group_ok = not item_group
            elif group_val:
                group_ok = item_group == group_val
            else:
                group_ok = True
            item.setHidden(not (name_ok and group_ok))

    def _on_apply(self):
        """应用按钮点击处理 - 不关闭对话框"""
        # 获取列表中当前选中的演唱者
        sel_items = self.singer_list.selectedItems()
        if not sel_items:
            return
        singer_id = sel_items[0].data(Qt.ItemDataRole.UserRole)
        if not singer_id:
            return

        # 收集选中的行
        selected_lines = []
        for idx in range(self.table.rowCount()):
            widget = self.table.cellWidget(idx, 0)
            if widget:
                chk = widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    selected_lines.append(idx)

        if not selected_lines:
            return

        # 构建结果映射并发出信号
        result_map = {line_idx: singer_id for line_idx in selected_lines}
        # 累计保存（对话框不关闭、可多次应用），供 result_map() 查询
        self._result_map.update(result_map)
        self._modified = True
        self.apply_requested.emit(result_map)

        # 更新表格中已应用行的当前演唱者显示
        singer = self._singer_map.get(singer_id)
        singer_name = singer.name if singer else "未知"
        for line_idx in selected_lines:
            item = self.table.item(line_idx, 3)
            if item:
                item.setText(singer_name)
                if singer:
                    item.setForeground(QColor(singer.color))

        # 取消已应用行的复选框选中状态
        for idx in selected_lines:
            widget = self.table.cellWidget(idx, 0)
            if widget:
                chk = widget.findChild(QCheckBox)
                if chk:
                    chk.setChecked(False)

    def was_modified(self) -> bool:
        return self._modified

    def result_map(self) -> dict[int, str]:
        """返回 {line_idx: singer_id} 映射"""
        return self._result_map


class ApplySingerDialog(QDialog):
    """应用演唱者对话框 — 为选中字符设置演唱者。

    显示当前选中字符内容、当前演唱者信息、过滤器和演唱者列表。
    用户可选择一个演唱者并应用到选中的字符。
    """

    apply_requested = pyqtSignal(str)  # singer_id

    def __init__(self, char_text: str, current_singers: list[Singer], all_singers: list[Singer], parent=None):
        super().__init__(parent)
        self._current_singers = current_singers
        self._all_singers = all_singers
        self._selected_singer_id = None

        self.setWindowTitle(self.tr("应用演唱者"))
        fit_to_screen(self, 400, 500)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)

        # 第一行：当前选中的字符内容（不可编辑）
        form = QFormLayout()
        lbl_char = QLabel(char_text)
        lbl_char.setStyleSheet("font-size: 16px; font-weight: bold;")
        form.addRow(self.tr("选中字符:"), lbl_char)

        # 第二行：当前演唱者信息
        if current_singers:
            singer_names = ", ".join(s.name for s in current_singers)
        else:
            singer_names = self.tr("无")
        lbl_current_singer = QLabel(singer_names)
        lbl_current_singer.setStyleSheet("font-size: 14px;")
        form.addRow(self.tr("当前演唱者:"), lbl_current_singer)
        layout.addLayout(form)

        # 第三行：过滤器
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel(self.tr("过滤:")))
        self.edit_filter = LineEdit(self)
        self.edit_filter.setPlaceholderText(self.tr("输入演唱者名称进行过滤"))
        self.edit_filter.textChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.edit_filter, stretch=1)
        layout.addLayout(filter_layout)

        # 第四行：演唱者列表
        self.list_singers = QTableWidget(len(all_singers), 2, self)
        self.list_singers.setHorizontalHeaderLabels([self.tr("演唱者"), self.tr("颜色预览")])
        self.list_singers.horizontalHeader().setStretchLastSection(True)
        self.list_singers.setColumnWidth(1, 64)
        self.list_singers.verticalHeader().setVisible(False)
        self.list_singers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.list_singers.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.list_singers.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.list_singers.setIconSize(QSize(48, 20))
        self.list_singers.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_singers.cellDoubleClicked.connect(self._on_double_click)

        # 填充列表
        for idx, singer in enumerate(all_singers):
            name_item = QTableWidgetItem(singer.name)
            name_item.setData(Qt.ItemDataRole.UserRole, singer.id)
            self.list_singers.setItem(idx, 0, name_item)

            color_item = QTableWidgetItem()
            color_item.setIcon(QIcon(_make_singer_color_pixmap(singer.get_all_colors(), 48, 20)))
            color_item.setFlags(color_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.list_singers.setItem(idx, 1, color_item)

        layout.addWidget(self.list_singers, stretch=1)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_apply = PrimaryPushButton(self.tr("应用"), self)
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_apply.setEnabled(False)
        btn_layout.addWidget(self.btn_apply)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _on_filter_changed(self, text: str):
        """过滤器文本变化时更新列表显示"""
        filter_text = text.strip().lower()
        for row in range(self.list_singers.rowCount()):
            name_item = self.list_singers.item(row, 0)
            if name_item:
                singer_name = name_item.text().lower()
                self.list_singers.setRowHidden(row, filter_text not in singer_name)

    def _on_selection_changed(self):
        """列表选择变化时更新应用按钮状态"""
        selected_items = self.list_singers.selectedItems()
        if selected_items:
            row = selected_items[0].row()
            name_item = self.list_singers.item(row, 0)
            if name_item:
                self._selected_singer_id = name_item.data(Qt.ItemDataRole.UserRole)
                self.btn_apply.setEnabled(True)
                return
        self._selected_singer_id = None
        self.btn_apply.setEnabled(False)

    def _on_apply(self):
        """应用按钮点击处理"""
        if self._selected_singer_id:
            self.apply_requested.emit(self._selected_singer_id)
            self.accept()

    def _on_double_click(self, row: int, column: int):
        """双击列表项时直接应用"""
        name_item = self.list_singers.item(row, 0)
        if name_item:
            singer_id = name_item.data(Qt.ItemDataRole.UserRole)
            if singer_id:
                self.apply_requested.emit(singer_id)
                self.accept()

    def get_selected_singer_id(self) -> str:
        """返回选中的演唱者ID"""
        return self._selected_singer_id


class CompleteTimestampDialog(QDialog):
    """补全时间戳对话框 — 自动查找需要补轴点的字符并补全时间戳。

    本功能用于所有打轴过程完成后，自动查找需要补轴点的字符（仅无普通时间戳字符），
    查找前后时间戳取平均值均分给待补偿时间戳。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("补全时间戳"))
        fit_to_screen(self, 480, 400)
        self.setFont(ui_font(10))
        self._apply_clicked = False

        # 读取配置
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = AppSettings()
            self._saved_scope_types = settings.get("complete_timestamp.scope_types", [
                "kanji", "hiragana", "katakana", "sokuon", "long_vowel", "chon", "chisai_kana",
                "hangul"
            ])
            self._saved_exclude_rules = settings.get("complete_timestamp.exclude_rules", ["linked"])
            self._saved_head_offset_ms = settings.get("complete_timestamp.head_offset_ms", 150)
            self._saved_tail_offset_ms = settings.get("complete_timestamp.tail_offset_ms", 150)
        except Exception:
            self._saved_scope_types = ["kanji", "hiragana", "katakana", "sokuon", "long_vowel", "chon", "chisai_kana", "hangul"]
            self._saved_exclude_rules = ["linked"]
            self._saved_head_offset_ms = 150
            self._saved_tail_offset_ms = 150

        layout = QVBoxLayout(self)

        # 第一行：功能说明
        desc_label = CaptionLabel(self.tr(
            "本功能用于所有打轴过程完成后，自动查找需要补轴点的字符（仅无普通时间戳字符），"
            "查找前后时间戳取平均值均分给待补偿时间戳。\n\n"
            "边界处理：行首无前方时间戳时，向后找到第一个时间戳并减去「行首扣除」值；"
            "行尾无后方时间戳时，向前找到第一个时间戳并加上「行尾增加」值。"
        ))
        desc_label.setWordWrap(True)
        desc_label.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(desc_label)

        # 第二行：适用范围（多选）
        scope_group = FluentGroupBox(self.tr("适用范围"))
        scope_layout = scope_group.contentLayout

        self._scope_checkboxes: dict[str, QCheckBox] = {}
        # 显示侧逐项显式 self.tr 以便 .ts 抽取器把源串归入本类上下文
        # （self.tr(变量) 无法静态求值）。key 作为持久化标识不变。
        scope_items: list[tuple[str, str]] = [
            ("kanji",       self.tr("汉字")),
            ("hiragana",    self.tr("平假名")),
            ("katakana",    self.tr("片假名")),
            ("sokuon",      self.tr("促音（っ/ッ）")),
            ("long_vowel",  self.tr("长音符号")),
            ("chon",        self.tr("拨音（ん/ン）")),
            ("chisai_kana", self.tr("捨仮名")),
            ("alphabet",    self.tr("英文字母")),
            ("hangul",      self.tr("韩文（谚文）")),
            ("number",      self.tr("数字")),
            ("symbol",      self.tr("特殊符号")),
        ]

        for key, label in scope_items:
            chk = CheckBox(label)
            chk.setChecked(key in self._saved_scope_types)
            self._scope_checkboxes[key] = chk
            scope_layout.addWidget(chk)

        layout.addWidget(scope_group)

        # 第三行：排除规则（多选）
        exclude_group = FluentGroupBox(self.tr("排除规则"))
        exclude_layout = exclude_group.contentLayout

        self._exclude_checkboxes: dict[str, CheckBox] = {}
        exclude_items: list[tuple[str, str]] = [
            ("linked", self.tr("排除被连词字符")),
        ]

        for key, label in exclude_items:
            chk = CheckBox(label)
            chk.setChecked(key in self._saved_exclude_rules)
            self._exclude_checkboxes[key] = chk
            exclude_layout.addWidget(chk)

        layout.addWidget(exclude_group)

        # 第四行：边界时间戳偏移设置
        offset_group = FluentGroupBox(self.tr("边界时间戳偏移"))
        offset_layout = QFormLayout()
        offset_group.contentLayout.addLayout(offset_layout)

        self._edit_head_offset = LineEdit(self)
        self._edit_head_offset.setText(str(self._saved_head_offset_ms))
        self._edit_head_offset.setPlaceholderText("150")
        self._edit_head_offset.setToolTip(self.tr(
            "行首字符无前方时间戳时，向后找到第一个时间戳后减去此值"
        ))
        offset_layout.addRow(self.tr("行首扣除时间戳 (ms):"), self._edit_head_offset)

        self._edit_tail_offset = LineEdit(self)
        self._edit_tail_offset.setText(str(self._saved_tail_offset_ms))
        self._edit_tail_offset.setPlaceholderText("150")
        self._edit_tail_offset.setToolTip(self.tr(
            "行尾字符无后方时间戳时，向前找到第一个时间戳后加上此值"
        ))
        offset_layout.addRow(self.tr("行尾增加时间戳 (ms):"), self._edit_tail_offset)

        layout.addWidget(offset_group)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_apply = PrimaryPushButton(self.tr("应用"), self)
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_apply)
        btn_layout.addWidget(btn_apply)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _on_apply(self):
        """应用按钮点击处理"""
        self._apply_clicked = True
        # 保存适用范围、排除规则和边界偏移到配置
        scope_types = self.get_scope_types()
        exclude_rules = self.get_exclude_rules()
        head_offset = self.get_head_offset_ms()
        tail_offset = self.get_tail_offset_ms()
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = AppSettings()
            settings.set("complete_timestamp.scope_types", list(scope_types))
            settings.set("complete_timestamp.exclude_rules", exclude_rules)
            settings.set("complete_timestamp.head_offset_ms", head_offset)
            settings.set("complete_timestamp.tail_offset_ms", tail_offset)
            settings.save()
        except Exception:
            pass
        self.accept()

    def was_apply_clicked(self) -> bool:
        return self._apply_clicked

    def get_scope_types(self) -> set[str]:
        """返回选中的适用范围类型集合"""
        return {key for key, chk in self._scope_checkboxes.items() if chk.isChecked()}

    def get_exclude_rules(self) -> list[str]:
        """返回选中的排除规则列表"""
        return [key for key, chk in self._exclude_checkboxes.items() if chk.isChecked()]

    def get_head_offset_ms(self) -> int:
        """返回行首扣除时间戳值（毫秒）"""
        try:
            return int(self._edit_head_offset.text())
        except ValueError:
            return 150

    def get_tail_offset_ms(self) -> int:
        """返回行尾增加时间戳值（毫秒）"""
        try:
            return int(self._edit_tail_offset.text())
        except ValueError:
            return 150


class AdjustRawTimestampDialog(QDialog):
    """调整原始时间戳对话框 — 非模态，应用后不关闭，允许边测试边调整。

    每次点击「应用」即执行一次偏移，结果可叠加。
    窗口与主界面并存，用户可切换回主界面试听后继续调整。

    ``scope`` 支持 ``"all"`` / ``"line"`` / ``"selected"`` 三种范围；
    ``scope_label`` 用于在描述中显示当前作用范围（如 "第 3 行"）。
    """

    apply_requested = pyqtSignal(int)  # delta_ms

    def __init__(self, parent=None, scope: str = "all", scope_label: str = ""):
        super().__init__(parent)
        self._scope = scope
        if scope == "line":
            self.setWindowTitle(self.tr("按行调整原始时间戳"))
            scope_text = self.tr("作用范围：{label}").format(label=scope_label or self.tr("当前行"))
            tip = self.tr("正数：该行所有原始时间戳向后移；负数：向前移。")
        elif scope == "selected":
            self.setWindowTitle(self.tr("调整所选字符原始时间戳"))
            scope_text = self.tr("作用范围：{label}").format(label=scope_label or self.tr("所选字符"))
            tip = self.tr("正数：所选字符原始时间戳向后移；负数：向前移。")
        else:
            self.setWindowTitle(self.tr("调整原始时间戳"))
            scope_text = self.tr("作用范围：所有原始时间戳")
            tip = self.tr("正数：所有原始时间戳向后移；负数：向前移。")

        self.setWindowModality(Qt.WindowModality.NonModal)
        fit_to_screen(self, 360, 220)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.lbl_scope = QLabel(scope_text)
        self.lbl_scope.setWordWrap(True)
        self.lbl_scope.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.lbl_scope)

        desc = QLabel(
            f"{tip}\n"
            + self.tr("每次点击「应用」立即执行偏移，可叠加多次操作。")
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        form = QFormLayout()
        self.spin_delta = SpinBox(self)
        self.spin_delta.setRange(-18000000, 18000000)
        self.spin_delta.setValue(0)
        self.spin_delta.setSuffix(" ms")
        form.addRow(self.tr("偏移量:"), self.spin_delta)
        layout.addLayout(form)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_apply = PrimaryPushButton(self.tr("应用"), self)
        self.btn_apply.setDefault(True)
        self.btn_apply.clicked.connect(self._on_apply)
        btn_layout.addWidget(self.btn_apply)
        btn_close = PushButton(self.tr("关闭"), self)
        btn_close.clicked.connect(self.close)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

    def _on_apply(self):
        delta = self.spin_delta.value()
        if delta == 0:
            self.lbl_status.setText(self.tr("偏移量为 0，未做任何修改"))
            return
        self.apply_requested.emit(delta)

    def set_status(self, text: str, success: bool = True):
        color = "#2d7d46" if success else "#c0392b"
        self.lbl_status.setText(f'<span style="color:{color}">{text}</span>')


# ── 分离符号时间戳 — 符号分组定义 ──────────────────────────────────────────
# 每组：(key, 中文标签, 示例显示, 字符集)
_SEPARATE_SYM_GROUPS: list[tuple[str, str, str, frozenset]] = [
    ("parentheses",        "圆括号",       "( )（）",         frozenset("()（）")),
    ("brackets",           "方括号",       "[ ]【】",          frozenset("[]【】")),
    ("exclamation",        "感叹号",       "! ！",             frozenset("!！")),
    ("angle_brackets",     "尖括号",       "< >《》〈〉",      frozenset("<>《》〈〉")),
    ("ja_quotes",          "日文引号",     "「 」",            frozenset("「」")),
    ("ja_dbl_quotes",      "日文双引号",   "『 』",            frozenset("『』")),
    ("curly_quotes",       "弯引号",       "“ ” ‘ ’",         frozenset("“”‘’")),
    ("straight_quotes",    "直引号",       "\" '",             frozenset("\"'")),
    ("question",           "问号",         "? ？",             frozenset("?？")),
    ("ellipsis",           "省略号",       "… .",              frozenset({"…", "."})),
    ("period",             "句号",         "。",               frozenset("。")),
    ("comma",              "逗号",         ", ，",             frozenset(",，")),
    ("ideographic_comma",  "顿号",         "、",               frozenset("、")),
    ("semicolon",          "分号",         "; ；",             frozenset(";；")),
    ("colon",              "冒号",         ": ：",             frozenset(":：")),
    ("wave",               "波浪号",       "~ ～ 〜",          frozenset("~～〜")),
    ("dash",               "破折号",       "— ―",              frozenset("—―")),
    ("middle_dot",         "中点",         "· ・ •",           frozenset("·・•")),
    ("music_note",         "音符",         "♪ ♫ ♩ ♬",        frozenset("♪♫♩♬")),
    ("heart",              "爱心",         "♡ ♥",              frozenset("♡♥")),
    ("star",               "星形",         "★ ☆",              frozenset("★☆")),
]

# 默认选中分组（用户明确列出的常见符号）
_SEPARATE_SYM_DEFAULT: frozenset[str] = frozenset({
    "parentheses", "brackets", "exclamation", "angle_brackets",
    "ja_quotes", "question", "ellipsis", "period", "comma",
})

# 本模块下方的辅助函数无 QObject self，翻译走显式 QCoreApplication.translate
# （模块级 _tr 别名转发 pylupdate6 看不见，会把条目误标 vanished）。


def _make_tri_checkbox(text: str, parent: QWidget) -> CheckBox:
    """创建三态复选框（全选 / 部分选中 / 未选）。

    点击行为：未选 → 全选；部分选 → 全选；全选 → 未选。
    部分选中状态由 ``_sync_parent_state()`` 程序化设置，
    ``clicked`` 回调会在此之后将意外的 PartiallyChecked 修正为 Checked。
    """
    chk = CheckBox(text, parent)
    chk.setTristate(True)

    def _fix_click():
        if chk.checkState() == Qt.CheckState.PartiallyChecked:
            chk.setCheckState(Qt.CheckState.Checked)

    chk.clicked.connect(_fix_click)
    return chk


class _GroupRow:
    """单组符号分组控件：父复选框 + 详情展开区 + 子复选框。"""

    def __init__(
        self,
        key: str,
        label: str,
        examples: str,
        char_set: frozenset,
        parent_widget: QWidget,
        saved_chars: set[str] | None,
    ):
        self._key = key
        self._char_set = char_set
        self._sub_chars: list[str] = sorted(char_set)

        self._container = QWidget(parent_widget)
        v = QVBoxLayout(self._container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        # ── 父行：复选框 + 标签 + 详情按钮 ──
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._parent_chk = _make_tri_checkbox(f"{label}   {examples}", parent_widget)
        self._parent_chk.clicked.connect(self._on_parent_clicked)
        row.addWidget(self._parent_chk)

        row.addStretch()

        self._detail_btn = QPushButton(QCoreApplication.translate("SeparateSymbolTimestampDialog", "详情 ▸"))
        self._detail_btn.setFixedWidth(56)
        self._detail_btn.setFixedHeight(22)
        self._detail_btn.setFlat(True)
        self._detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._detail_btn.clicked.connect(self._toggle_detail)
        row.addWidget(self._detail_btn)
        v.addLayout(row)

        # ── 子复选框区（默认隐藏） ──
        self._child_container = QWidget(parent_widget)
        child_layout = QVBoxLayout(self._child_container)
        child_layout.setContentsMargins(28, 0, 0, 0)
        child_layout.setSpacing(1)

        self._child_checkboxes: dict[str, CheckBox] = {}
        for ch in self._sub_chars:
            chk = CheckBox(ch, self._child_container)
            child_layout.addWidget(chk)
            chk.toggled.connect(self._on_child_toggled)
            self._child_checkboxes[ch] = chk

        self._child_container.setVisible(False)
        v.addWidget(self._child_container)

        # ── 初始化选中状态 ──
        if saved_chars is not None:
            for ch, chk in self._child_checkboxes.items():
                chk.setChecked(ch in saved_chars)
        else:
            for chk in self._child_checkboxes.values():
                chk.setChecked(True)

        self._sync_parent_state()
        self._update_detail_text()

    def _on_parent_clicked(self):
        if self._parent_chk.checkState() == Qt.CheckState.Checked:
            for chk in self._child_checkboxes.values():
                chk.setChecked(True)
        else:
            for chk in self._child_checkboxes.values():
                chk.setChecked(False)

    def _on_child_toggled(self):
        self._sync_parent_state()

    def _sync_parent_state(self):
        total = len(self._child_checkboxes)
        checked = sum(1 for chk in self._child_checkboxes.values() if chk.isChecked())
        if checked == 0:
            self._parent_chk.setCheckState(Qt.CheckState.Unchecked)
        elif checked == total:
            self._parent_chk.setCheckState(Qt.CheckState.Checked)
        else:
            self._parent_chk.setCheckState(Qt.CheckState.PartiallyChecked)

    @property
    def _is_expanded(self) -> bool:
        return self._child_container.isVisible()

    def _toggle_detail(self):
        expanded = not self._is_expanded
        self._child_container.setVisible(expanded)
        self._update_detail_text()

    def _update_detail_text(self):
        if self._is_expanded:
            self._detail_btn.setText(QCoreApplication.translate("SeparateSymbolTimestampDialog", "收起 ▾"))
        else:
            self._detail_btn.setText(QCoreApplication.translate("SeparateSymbolTimestampDialog", "详情 ▸"))

    @property
    def container(self) -> QWidget:
        return self._container

    def get_selected_chars(self) -> set[str]:
        return {ch for ch, chk in self._child_checkboxes.items() if chk.isChecked()}

    def get_group_key(self) -> str:
        return self._key

    def set_all_checked(self, checked: bool):
        for chk in self._child_checkboxes.values():
            chk.setChecked(checked)

    def is_any_checked(self) -> bool:
        return any(chk.isChecked() for chk in self._child_checkboxes.values())


class SeparateSymbolTimestampDialog(QDialog):
    """分离符号时间戳对话框 — 与补全时间戳类似的批量操作窗口。

    针对歌词中常见符号自动处理时间戳：

    * **后补偿**：符号 cc=0 且 is_sentence_end=True 且有 sentence_end_ts 时，
      将停顿点时间戳提升为普通时间戳（cc 改为 1），并将 sentence_end_ts 后移
      「后补偿」毫秒。
    * **前补偿**：符号 cc=1 且紧跟的第一个非符号字符 cc=0 时，将符号时间戳
      传递给该后续字符（cc 改为 1），并将符号自身时间戳前移「前补偿」毫秒。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("分离符号时间戳"))
        fit_to_screen(self, 680, 700)
        self.setFont(ui_font(10))
        self._apply_clicked = False

        # ── 读取上次设置 ──
        self._saved_group_chars: dict[str, set[str] | None] = {}
        self._saved_pre_comp: int = 150
        self._saved_post_comp: int = 150
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            s = AppSettings()
            gc = s.get("separate_symbol_ts.group_chars")
            if gc and isinstance(gc, dict):
                for gk, cv in gc.items():
                    if cv is None:
                        self._saved_group_chars[gk] = None
                    elif isinstance(cv, list):
                        self._saved_group_chars[gk] = set(cv)
            else:
                saved_groups = s.get("separate_symbol_ts.groups", list(_SEPARATE_SYM_DEFAULT))
                for gk in saved_groups:
                    if isinstance(gk, str):
                        self._saved_group_chars[gk] = None
            self._saved_pre_comp = s.get("separate_symbol_ts.pre_comp_ms", 150)
            self._saved_post_comp = s.get("separate_symbol_ts.post_comp_ms", 150)
            self._saved_force_copy: bool = s.get("separate_symbol_ts.force_copy", False)
        except Exception:
            for gk in _SEPARATE_SYM_DEFAULT:
                self._saved_group_chars[gk] = None
            self._saved_force_copy = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 说明文本 ──
        desc = QLabel(self.tr(
            "针对选中的符号分组，自动处理时间戳：\n"
            "• 后补偿：符号无普通时间戳（cc=0）但有停顿标记时，将停顿时间提升为普通时间戳，"
            "并将停顿点时间戳后移「后补偿」值。\n"
            "• 前补偿：符号已有时间戳（cc=1）且紧跟的第一个非符号字符无时间戳（cc=0）时，"
            "将符号时间戳传递给该字符，并将符号时间戳前移「前补偿」值。\n\n"
            "点击分组旁的「详情」可细化选择组内具体符号。"
        ))
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ── 符号分组选择（两列布局，可滚动） ──
        group_box = FluentGroupBox(self.tr("适用符号分组"))
        gbox_v = QVBoxLayout()
        group_box.contentLayout.addLayout(gbox_v)
        gbox_v.setContentsMargins(0, 0, 0, 0)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_w = QWidget()
        scroll.setWidget(scroll_w)
        gbox_v.addWidget(scroll)
        layout.addWidget(group_box)

        group_hlayout = QHBoxLayout(scroll_w)
        group_hlayout.setSpacing(12)
        group_hlayout.setContentsMargins(4, 4, 4, 4)
        mid = (len(_SEPARATE_SYM_GROUPS) + 1) // 2
        left_col = QVBoxLayout()
        right_col = QVBoxLayout()
        left_col.setSpacing(4)
        right_col.setSpacing(4)

        self._group_rows: dict[str, _GroupRow] = {}
        _TL = {  # noqa: N806  group-key → translated label
            "parentheses":       self.tr("圆括号"),
            "brackets":          self.tr("方括号"),
            "exclamation":       self.tr("感叹号"),
            "angle_brackets":    self.tr("尖括号"),
            "ja_quotes":         self.tr("日文引号"),
            "ja_dbl_quotes":     self.tr("日文双引号"),
            "curly_quotes":      self.tr("弯引号"),
            "straight_quotes":   self.tr("直引号"),
            "question":          self.tr("问号"),
            "ellipsis":          self.tr("省略号"),
            "period":            self.tr("句号"),
            "comma":             self.tr("逗号"),
            "ideographic_comma": self.tr("顿号"),
            "semicolon":         self.tr("分号"),
            "colon":             self.tr("冒号"),
            "wave":              self.tr("波浪号"),
            "dash":              self.tr("破折号"),
            "middle_dot":        self.tr("中点"),
            "music_note":        self.tr("音符"),
            "heart":             self.tr("爱心"),
            "star":              self.tr("星形"),
        }
        for i, (key, _raw_label, examples, char_set) in enumerate(_SEPARATE_SYM_GROUPS):
            saved = self._saved_group_chars.get(key)
            tr_label = _TL.get(key, _raw_label)
            row = _GroupRow(key, tr_label, examples, char_set, scroll_w, saved)
            self._group_rows[key] = row
            if i < mid:
                left_col.addWidget(row.container)
            else:
                right_col.addWidget(row.container)
        left_col.addStretch()
        right_col.addStretch()
        group_hlayout.addLayout(left_col)
        group_hlayout.addLayout(right_col)

        # ── 全选 / 全不选 ──
        sel_row = QHBoxLayout()
        btn_all = PushButton(self.tr("全选"), self)
        btn_all.clicked.connect(self._select_all)
        btn_none = PushButton(self.tr("全不选"), self)
        btn_none.clicked.connect(self._deselect_all)
        sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none)
        sel_row.addStretch()

        hint = QLabel(self.tr("ⓘ 悬停查看详情"))
        hint.setToolTip(self.tr(
            "前补偿：不考虑后方字符是否已有时间戳，前移符号时间戳并强制复制给后一字符\n"
            "后补偿：连续停顿点符号视为整体，符号组时间戳集中赋予前一字符，组内均匀分配"
        ))
        hint.setCursor(Qt.CursorShape.WhatsThisCursor)
        sel_row.addWidget(hint)

        self.sw_force_copy = SwitchButton(self)
        self.sw_force_copy.setOnText(self.tr("强制复制"))
        self.sw_force_copy.setOffText(self.tr("强制复制"))
        self.sw_force_copy.setChecked(self._saved_force_copy)
        self.sw_force_copy.setToolTip(self.tr(
            "前补偿：不考虑后方字符是否已有时间戳，前移符号时间戳并强制复制给后一字符\n"
            "后补偿：连续停顿点符号视为整体，符号组时间戳集中赋予前一字符，组内均匀分配"
        ))
        sel_row.addWidget(self.sw_force_copy)
        layout.addLayout(sel_row)

        # ── 补偿设置 ──
        comp_box = FluentGroupBox(self.tr("补偿时间戳"))
        comp_form = QFormLayout()
        comp_box.contentLayout.addLayout(comp_form)

        self.spin_pre_comp = SpinBox(self)
        self.spin_pre_comp.setRange(0, 99999)
        self.spin_pre_comp.setValue(self._saved_pre_comp)
        self.spin_pre_comp.setSuffix(" ms")
        self.spin_pre_comp.setToolTip(self.tr(
            "符号已有时间戳（cc=1）时，将其时间戳前移的量；\n"
            "同时将原始符号时间戳赋给紧跟的无时间戳非符号字符"
        ))
        comp_form.addRow(self.tr("前补偿（前移符号时间戳）:"), self.spin_pre_comp)

        self.spin_post_comp = SpinBox(self)
        self.spin_post_comp.setRange(0, 99999)
        self.spin_post_comp.setValue(self._saved_post_comp)
        self.spin_post_comp.setSuffix(" ms")
        self.spin_post_comp.setToolTip(self.tr(
            "符号无普通时间戳（cc=0）但有停顿时，将停顿点时间戳后移的量"
        ))
        comp_form.addRow(self.tr("后补偿（后移停顿点时间戳）:"), self.spin_post_comp)

        layout.addWidget(comp_box)

        # ── 按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_apply = PrimaryPushButton(self.tr("应用"), self)
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_apply)
        btn_layout.addWidget(btn_apply)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _select_all(self):
        for row in self._group_rows.values():
            row.set_all_checked(True)

    def _deselect_all(self):
        for row in self._group_rows.values():
            row.set_all_checked(False)

    def _on_apply(self):
        self._apply_clicked = True
        try:
            from strange_uta_game.frontend.settings.app_settings import AppSettings
            settings = AppSettings()
            gc: dict = {}
            for key, row in self._group_rows.items():
                sel = row.get_selected_chars()
                total = len(next(entry[3] for entry in _SEPARATE_SYM_GROUPS if entry[0] == key))
                if len(sel) == total:
                    gc[key] = None
                elif len(sel) == 0:
                    gc[key] = []
                else:
                    gc[key] = sorted(sel)
            settings.set("separate_symbol_ts.group_chars", gc)
            settings.set("separate_symbol_ts.pre_comp_ms", self.get_pre_comp_ms())
            settings.set("separate_symbol_ts.post_comp_ms", self.get_post_comp_ms())
            settings.set("separate_symbol_ts.force_copy", self.get_force_copy())
            settings.save()
        except Exception:
            pass
        self.accept()

    def was_apply_clicked(self) -> bool:
        return self._apply_clicked

    def get_selected_groups(self) -> set[str]:
        return {key for key, row in self._group_rows.items() if row.is_any_checked()}

    def get_symbol_chars(self) -> frozenset:
        chars: set = set()
        for row in self._group_rows.values():
            chars |= row.get_selected_chars()
        return frozenset(chars)

    def get_pre_comp_ms(self) -> int:
        return self.spin_pre_comp.value()

    def get_post_comp_ms(self) -> int:
        return self.spin_post_comp.value()

    def get_force_copy(self) -> bool:
        return self.sw_force_copy.isChecked()


# ──────────────────────────────────────────────
# 自动生成间奏指引
# ──────────────────────────────────────────────


def _find_first_timestamped_char(sentences: list[Sentence]) -> int | None:
    """在所有字符中搜索第一个有时间戳的字符，返回其最小时间戳（毫秒）。

    搜索不到时返回 None（歌曲尚未打轴）。
    """
    for sentence in sentences:
        for ch in sentence.characters:
            ts_list = ch.all_timestamps
            if ts_list:
                return min(ts_list)
    return None


def _find_next_timestamp(sentences: list[Sentence], start_si: int, start_ci: int) -> int | None:
    """从 (start_si, start_ci) 的下一个字符开始向后搜索最近的时间戳。早停。"""
    found_start = False
    for si, sentence in enumerate(sentences):
        for ci, ch in enumerate(sentence.characters):
            if not found_start:
                if si == start_si and ci == start_ci:
                    found_start = True
                continue
            ts_list = ch.all_timestamps
            if ts_list:
                return min(ts_list)
    return None


def _build_guide_chars_for_interlude(
    text: str,
    singer_id: str,
    gap_start_ms: int,
    gap_end_ms: int,
    front_margin_ms: int,
    back_margin_ms: int,
) -> list[Character]:
    """构建间奏指引 Character 列表。

    第一个字符时间戳 = gap_start_ms + front_margin_ms；
    最后一个字符 is_sentence_end=True，sentence_end_ts = gap_end_ms - back_margin_ms；
    所有字符 cc=1，时间戳在区间内均匀分配。
    """
    chars = list(text)
    n = len(chars)
    if n == 0:
        return []

    start_ts = gap_start_ms + front_margin_ms
    end_ts = gap_end_ms - back_margin_ms
    if end_ts < start_ts:
        end_ts = start_ts

    result: list[Character] = []
    for i, ch_str in enumerate(chars):
        is_last = i == n - 1
        if n == 1:
            ts = start_ts
        else:
            ts = start_ts + int((end_ts - start_ts) * i / n)
        new_ch = Character(
            char=ch_str,
            ruby=None,
            check_count=1,
            singer_id=singer_id,
            is_sentence_end=is_last,
            linked_to_next=False,
            is_line_end=False,
        )
        new_ch.add_timestamp(ts)
        if is_last:
            new_ch.sentence_end_ts = end_ts
        result.append(new_ch)
    return result


def execute_auto_interlude_guide(
    project,
    min_guide_time_s: float,
    format_str: str,
    position_mappings: dict,
    allow_inline: bool,
    new_line: bool,
    front_margin_ms: int,
    back_margin_ms: int,
    audio_duration_ms: int | None = None,
) -> dict:
    """执行自动生成间奏指引，原地修改 project.sentences。

    Returns:
        {"inserted": int, "skipped": int, "message": str}
    """
    min_guide_ms = int(min_guide_time_s * 1000)
    # 优先使用当前音频引擎给出的实际时长。project.audio_duration_ms 是持久化
    # 快照，旧项目以及“先导入音频、后创建项目”等路径中可能仍为 0 或已过期。
    if audio_duration_ms is None:
        audio_duration_ms = getattr(project, "audio_duration_ms", 0) or 0
    audio_duration_ms = max(0, int(audio_duration_ms))

    if not project.sentences:
        return {"inserted": 0, "skipped": 0, "message": "项目中没有歌词行"}

    # 纯文本导入等路径下字符级 singer_id 可能为空，行级 singer_id 由
    # Sentence 不变式保证非空，作为回退（与导出器 effective_id 逻辑一致）。
    singer_id = ""
    for s in project.sentences:
        if s.characters:
            singer_id = s.characters[0].singer_id or s.singer_id
            break

    gaps: list[tuple[int, int, int, int, int]] = []

    # Position 0
    first_ts = _find_first_timestamped_char(project.sentences)
    if first_ts is not None and first_ts >= min_guide_ms:
        pos_key = "0"
        if position_mappings.get(pos_key, {}).get("enabled", True):
            gaps.append((-1, -1, 0, 0, first_ts))

    # Position 1/2
    for si, sentence in enumerate(project.sentences):
        for ci, ch in enumerate(sentence.characters):
            if not ch.is_sentence_end or ch.sentence_end_ts is None:
                continue
            next_ts = _find_next_timestamp(project.sentences, si, ci)
            if next_ts is not None:
                pos = 1
                gap_end = next_ts
            else:
                pos = 2
                gap_end = audio_duration_ms
            gap_ms = gap_end - ch.sentence_end_ts
            if gap_ms < min_guide_ms:
                continue
            pos_key = str(pos)
            if not position_mappings.get(pos_key, {}).get("enabled", True):
                continue

            is_last_in_sentence = ci == len(sentence.characters) - 1
            if not allow_inline and not is_last_in_sentence:
                continue

            gaps.append((si, ci, pos, ch.sentence_end_ts, gap_end))

    if not gaps:
        return {"inserted": 0, "skipped": 0, "message": "未找到符合条件的间奏间隙"}

    gaps.sort(key=lambda g: (g[0], g[1]), reverse=True)

    inserted = 0
    for si, ci, pos, gap_start_ms, gap_end_ms in gaps:
        pos_key = str(pos)
        mapping_text = position_mappings.get(pos_key, {}).get("text", "")
        gap_time_s = (gap_end_ms - gap_start_ms) / 1000.0
        time_str = str(int(gap_time_s))

        guide_text = format_str.replace("{position}", mapping_text).replace("{time}", time_str)
        if not guide_text.strip():
            continue

        if pos == 0:
            if not project.sentences:
                continue
            first_sentence = project.sentences[0]
            guide_chars = _build_guide_chars_for_interlude(
                guide_text, singer_id, gap_start_ms, gap_end_ms,
                front_margin_ms, back_margin_ms,
            )
            if not guide_chars:
                continue
            if new_line:
                new_sentence = Sentence(
                    singer_id=singer_id,
                    characters=guide_chars,
                )
                if guide_chars:
                    guide_chars[-1].is_line_end = True
                project.sentences.insert(0, new_sentence)
            else:
                for i, gc in enumerate(guide_chars):
                    first_sentence.characters.insert(i, gc)
            inserted += 1
            continue

        if si < 0 or si >= len(project.sentences):
            continue
        sentence = project.sentences[si]
        if ci < 0 or ci >= len(sentence.characters):
            continue

        ref_char = sentence.characters[ci]
        ref_singer_id = ref_char.singer_id or sentence.singer_id or singer_id

        is_last_in_sentence = ci == len(sentence.characters) - 1
        guide_chars = _build_guide_chars_for_interlude(
            guide_text, ref_singer_id, gap_start_ms, gap_end_ms,
            front_margin_ms, back_margin_ms,
        )
        if not guide_chars:
            continue

        if is_last_in_sentence or not allow_inline:
            if new_line:
                new_sentence = Sentence(
                    singer_id=ref_singer_id,
                    characters=guide_chars,
                )
                if guide_chars:
                    guide_chars[-1].is_line_end = True
                project.sentences.insert(si + 1, new_sentence)
            else:
                if is_last_in_sentence:
                    sentence.characters[ci].is_line_end = False
                insert_pos = ci + 1
                for i, gc in enumerate(guide_chars):
                    sentence.characters.insert(insert_pos + i, gc)
                if is_last_in_sentence and guide_chars:
                    guide_chars[-1].is_line_end = True
        else:
            if new_line:
                right_chars = sentence.characters[ci + 1 :]
                del sentence.characters[ci + 1 :]
                sentence.characters[ci].is_line_end = True

                mid_sentence = Sentence(
                    singer_id=ref_singer_id,
                    characters=list(guide_chars),
                    song_part=sentence.song_part,
                )
                if guide_chars:
                    guide_chars[-1].is_line_end = True

                right_sentence = Sentence(
                    singer_id=ref_singer_id,
                    characters=right_chars,
                    song_part=sentence.song_part,
                )
                if right_chars:
                    right_chars[-1].is_line_end = True

                project.sentences.insert(si + 1, mid_sentence)
                project.sentences.insert(si + 2, right_sentence)
            else:
                insert_pos = ci + 1
                for i, gc in enumerate(guide_chars):
                    sentence.characters.insert(insert_pos + i, gc)

        inserted += 1

    return {
        "inserted": inserted,
        "skipped": 0,
        "message": "",
    }


class AutoGenerateInterludeGuideDialog(QDialog):
    """自动生成间奏指引对话框。

    自动扫描整首歌中所有 is_sentence_end=True 的字符，根据前后时间戳间隔
    自动生成间奏指引文本并插入到对应位置。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("自动生成间奏指引"))
        fit_to_screen(self, 520, 460)
        self.setFont(ui_font(10))
        self._apply_clicked = False

        from strange_uta_game.frontend.settings.app_settings import AppSettings
        settings = AppSettings()
        self._saved_min_guide_time = float(settings.get("auto_interlude_guide.min_guide_time_s", 5.0))
        self._saved_format = settings.get("auto_interlude_guide.format", self.tr("「{position}约{time}秒」"))
        self._saved_position_mappings = settings.get("auto_interlude_guide.position_mappings", {
            "0": {"enabled": True, "text": self.tr("前奏")},
            "1": {"enabled": True, "text": self.tr("间奏")},
            "2": {"enabled": True, "text": self.tr("后奏")},
        })
        self._saved_allow_inline = bool(settings.get("auto_interlude_guide.allow_inline", False))
        self._saved_new_line = bool(settings.get("auto_interlude_guide.new_line", True))
        self._saved_front_margin = int(settings.get("auto_interlude_guide.front_margin_ms", 150))
        self._saved_back_margin = int(settings.get("auto_interlude_guide.back_margin_ms", 150))

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 功能说明 ──
        desc_label = CaptionLabel(self.tr(
            "自动扫描整首歌中所有 is_sentence_end=True 的字符，根据前后时间戳间隔"
            "自动生成间奏指引文本并插入到对应位置。"
        ))
        desc_label.setWordWrap(True)
        desc_label.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(desc_label)

        # ── 基本设置 ──
        basic_group = FluentGroupBox(self.tr("基本设置"))
        basic_layout = basic_group.contentLayout

        form_basic = QFormLayout()
        self.edit_min_time = LineEdit(self)
        self.edit_min_time.setText(str(self._saved_min_guide_time))
        self.edit_min_time.setPlaceholderText("5")
        self.edit_min_time.setToolTip(self.tr("间隔时间小于此值的间隙将被跳过，支持小数"))
        form_basic.addRow(self.tr("最小生成间奏指引时间 (s):"), self.edit_min_time)
        basic_layout.addLayout(form_basic)

        basic_layout.addWidget(QLabel(self.tr("间奏指引格式:")))
        self.edit_format = LineEdit(self)
        self.edit_format.setText(self._saved_format)
        self.edit_format.setPlaceholderText(self.tr("「{position}约{time}秒」"))
        self.edit_format.setToolTip(self.tr("{position}=位置映射文本, {time}=计算间隔时间（秒）"))
        basic_layout.addWidget(self.edit_format)

        chk_layout = QHBoxLayout()
        self.chk_allow_inline = CheckBox(self.tr("允许单行内插入"))
        self.chk_allow_inline.setChecked(self._saved_allow_inline)
        self.chk_allow_inline.setToolTip(self.tr("允许在行中间的 is_sentence_end 字符处也进行插入"))
        self.chk_new_line = CheckBox(self.tr("新建一行"))
        self.chk_new_line.setChecked(self._saved_new_line)
        self.chk_new_line.setToolTip(self.tr("将生成的间奏指引文本放在独立的新行中"))
        chk_layout.addWidget(self.chk_allow_inline)
        chk_layout.addWidget(self.chk_new_line)
        chk_layout.addStretch()
        basic_layout.addLayout(chk_layout)

        layout.addWidget(basic_group)

        # ── Position 相关设置 ──
        self._build_position_settings(layout)

        # ── 时间戳偏移 ──
        offset_group = FluentGroupBox(self.tr("时间戳偏移"))
        offset_layout = QFormLayout()
        offset_group.contentLayout.addLayout(offset_layout)

        front_row = QHBoxLayout()
        self.edit_front_margin = LineEdit(self)
        self.edit_front_margin.setText(str(self._saved_front_margin))
        self.edit_front_margin.setPlaceholderText("150")
        self.edit_front_margin.setToolTip(self.tr("生成文本第一个字符的时间戳偏移"))
        front_row.addWidget(self.edit_front_margin)
        front_row.addWidget(CaptionLabel("ms"))
        front_row.addStretch()
        offset_layout.addRow(self.tr("前余量:"), front_row)

        back_row = QHBoxLayout()
        self.edit_back_margin = LineEdit(self)
        self.edit_back_margin.setText(str(self._saved_back_margin))
        self.edit_back_margin.setPlaceholderText("150")
        self.edit_back_margin.setToolTip(self.tr("生成文本最后一个字符的停顿点时间戳回退"))
        back_row.addWidget(self.edit_back_margin)
        back_row.addWidget(CaptionLabel("ms"))
        back_row.addStretch()
        offset_layout.addRow(self.tr("后余量:"), back_row)

        layout.addWidget(offset_group)

        # ── 按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_apply = PrimaryPushButton(self.tr("应用"), self)
        btn_apply.setDefault(True)
        btn_apply.clicked.connect(self._on_apply)
        btn_layout.addWidget(btn_apply)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def _build_position_settings(self, layout):
        """构建 Position 相关设置区域"""
        pos_gb = FluentGroupBox(self.tr("Position相关设置"), self)
        gb_layout = pos_gb.contentLayout

        # 表头行
        header_row = QHBoxLayout()
        hdr_enable = CaptionLabel(self.tr("启用"))
        hdr_enable.setFixedWidth(44)
        header_row.addWidget(hdr_enable)
        hdr_cat = CaptionLabel(self.tr("类别"))
        hdr_cat.setFixedWidth(48)
        header_row.addWidget(hdr_cat)
        header_row.addWidget(CaptionLabel(self.tr("映射文本")), stretch=1)
        gb_layout.addLayout(header_row)

        pos_keys = ["0", "1", "2"]
        pos_labels = [
            self.tr("前奏"),
            self.tr("间奏"),
            self.tr("后奏"),
        ]
        default_texts = [
            self.tr("前奏"),
            self.tr("间奏"),
            self.tr("后奏"),
        ]
        self._pos_checkboxes: dict[str, CheckBox] = {}
        self._pos_edits: dict[str, LineEdit] = {}

        for i, key in enumerate(pos_keys):
            row = QHBoxLayout()
            chk = CheckBox("", self)
            chk.setFixedWidth(44)
            chk.setChecked(self._saved_position_mappings.get(key, {}).get("enabled", True))
            self._pos_checkboxes[key] = chk
            row.addWidget(chk)

            cat_lbl = QLabel(pos_labels[i])
            cat_lbl.setFixedWidth(48)
            row.addWidget(cat_lbl)

            edit = LineEdit(self)
            text_val = self._saved_position_mappings.get(key, {}).get("text", default_texts[i])
            edit.setText(text_val)
            self._pos_edits[key] = edit
            row.addWidget(edit, stretch=1)
            gb_layout.addLayout(row)

        layout.addWidget(pos_gb)

    def _on_apply(self):
        front_ms = self._read_int(self.edit_front_margin.text(), 150)
        back_ms = self._read_int(self.edit_back_margin.text(), 150)
        try:
            min_ms = int(float(self.edit_min_time.text().strip()) * 1000)
        except (ValueError, TypeError):
            min_ms = 5000

        if front_ms + back_ms < min_ms:
            InfoBar.warning(
                title=self.tr("参数提示"),
                content=self.tr("前余量 + 后余量 ({sum}ms) 小于最小生成间奏指引时间 ({min}ms)，\n"
                                "某些间隙可能无法容纳生成的文本。").format(
                    sum=front_ms + back_ms, min=min_ms),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )

        self._apply_clicked = True

        from strange_uta_game.frontend.settings.app_settings import AppSettings
        settings = AppSettings()
        settings.set("auto_interlude_guide.min_guide_time_s", self.get_min_guide_time_s())
        settings.set("auto_interlude_guide.format", self.get_format())
        settings.set("auto_interlude_guide.position_mappings", self.get_position_mappings())
        settings.set("auto_interlude_guide.allow_inline", self.get_allow_inline())
        settings.set("auto_interlude_guide.new_line", self.get_new_line())
        settings.set("auto_interlude_guide.front_margin_ms", self.get_front_margin_ms())
        settings.set("auto_interlude_guide.back_margin_ms", self.get_back_margin_ms())
        settings.save()
        self.accept()

    @staticmethod
    def _read_int(text: str, default: int) -> int:
        try:
            return int(text.strip())
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _read_float(text: str, default: float) -> float:
        try:
            return float(text.strip())
        except (ValueError, TypeError):
            return default

    def was_apply_clicked(self) -> bool:
        return self._apply_clicked

    def get_min_guide_time_s(self) -> float:
        return self._read_float(self.edit_min_time.text(), 5.0)

    def get_format(self) -> str:
        return self.edit_format.text().strip()

    def get_position_mappings(self) -> dict:
        return {
            key: {
                "enabled": self._pos_checkboxes[key].isChecked(),
                "text": self._pos_edits[key].text(),
            }
            for key in ["0", "1", "2"]
        }

    def get_allow_inline(self) -> bool:
        return self.chk_allow_inline.isChecked()

    def get_new_line(self) -> bool:
        return self.chk_new_line.isChecked()

    def get_front_margin_ms(self) -> int:
        return self._read_int(self.edit_front_margin.text(), 150)

    def get_back_margin_ms(self) -> int:
        return self._read_int(self.edit_back_margin.text(), 150)
