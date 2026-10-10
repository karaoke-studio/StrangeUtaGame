"""全文本编辑界面。

以带内联时间戳的全文本格式编辑整篇歌词，自带完整时间轴（编解码委托后端
:mod:`strange_uta_game.backend.infrastructure.parsers.annotated_text`）。
应用时逐行独立解码，行的增删/重排/文本撞车都不会丢失或错配时间戳。
"""

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTextEdit,
    QDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QRect, QSize, QEvent
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)

from strange_uta_game.frontend.font_utils import ui_font
from qfluentwidgets import (
    CheckBox,
    PushButton,
    PrimaryPushButton,
    SpinBox,
    SwitchButton,
    InfoBar,
    InfoBarPosition,
    FluentIcon as FIF,
    CaptionLabel,
)
# TextEditMenu 未在 qfluentwidgets 包顶层导出，按库内部用法从子模块导入。
from qfluentwidgets.components.widgets.menu import TextEditMenu

import re
from typing import Optional, List

from strange_uta_game.backend.domain import (
    Project,
    Sentence,
    Character,
    Ruby,
    RubyPart,
)
from strange_uta_game.backend.application import AutoCheckService
from strange_uta_game.backend.infrastructure.parsers.text_splitter import (
    CharType,
)
from strange_uta_game.frontend.fluent_widgets import message_choice
from strange_uta_game.frontend.window_sizing import fit_to_screen


# 剥离行内结构化标签的正则（按顺序应用）
_STRIP_SINGER_RE = re.compile(r"【[^】]*】")
# 仅剥合规注音块：原文非空 + 含 ||（不合规块保留原样参与字宽统计）
_STRIP_RUBY_VALID_RE = re.compile(r"\{([^}]+?)\|\|[^}]*\}")
_STRIP_TIMESTAMP_RE = re.compile(r"\[>?(?:T|\d+:\d{2}\.\d{2})\]")  # 仅剥合法 token：[T]/[>T]/[mm:ss.xx]


def _strip_line_tags(line: str) -> str:
    """去除行内合规结构化标签，返回视觉文本（非法块/token 保留原样参与字宽统计）。"""
    text = _STRIP_SINGER_RE.sub("", line)
    text = _STRIP_RUBY_VALID_RE.sub(r"\1", text)
    text = _STRIP_TIMESTAMP_RE.sub("", text)
    return text


def _calc_ch_width(text: str, fm) -> float:
    """用字体度量计算文本视觉宽度（单位 ch）。

    1ch = 一个全角字符（汉字/假名）宽度的一半，即 ``fm.horizontalAdvance('一') / 2``。
    全角字符自然得到 2ch；拉丁字母按比例字体实际像素宽度折算，宽字母（如 W）
    会比窄字母（如 i）得到更大的 ch 值。整行一次测量兼顾字距调整。
    字体缺少 '一' 字形时退化为 'M' 宽度的两倍作为参考。
    """
    if not text:
        return 0.0
    ref = fm.horizontalAdvance("一")
    if ref <= 0:
        ref = fm.horizontalAdvance("M") * 2
    return fm.horizontalAdvance(text) / (ref / 2)


class _LineNumberArea(QWidget):
    """行号栏（绘制委托给 LineNumberPlainTextEdit）。"""

    def __init__(self, editor: "LineNumberPlainTextEdit"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)


class _LineInfoArea(QWidget):
    """行字数栏（绘制委托给 LineNumberPlainTextEdit）。"""

    def __init__(self, editor: "LineNumberPlainTextEdit"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_info_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_info_area_paint_event(event)


class LineNumberPlainTextEdit(QPlainTextEdit):
    """带左侧行号栏的纯文本编辑器。

    行号即文本块编号（每行对应一条 Sentence），从 1 起。
    """

    zoom_requested = pyqtSignal(int)  # Alt+滚轮缩放：+1 放大 / -1 缩小

    def __init__(self, parent=None):
        super().__init__(parent)
        self._show_ch_width = True
        # 字宽（ch）统计专用度量：跟随卡拉OK主文字字体，与编辑器显示字体解耦。
        # None 表示尚未设置，回退到编辑器自身 fontMetrics。
        self._ch_width_fm = None
        self._line_number_area = _LineNumberArea(self)
        self._line_info_area = _LineInfoArea(self)
        self.blockCountChanged.connect(lambda _=0: self._update_width())
        self.updateRequest.connect(self._on_update_request)
        self._update_width()

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits + 6

    def line_info_area_width(self) -> int:
        if not self._show_ch_width:
            return 0
        fm = self.fontMetrics()
        return fm.horizontalAdvance("99.9ch") + 16

    def _update_width(self):
        self.setViewportMargins(
            self.line_number_area_width(), 0, self.line_info_area_width(), 0
        )

    def set_show_ch_width(self, show: bool):
        if self._show_ch_width == show:
            return
        self._show_ch_width = show
        self._line_info_area.setVisible(show)
        self._update_width()

    def set_ch_width_font(self, family: str) -> str:
        """设置字宽统计的测量字体（跟随卡拉OK主文字字体）。

        缺少全角参考字 `一` 时回退微软雅黑测量（见 font_utils）。
        返回实际用于测量的字体族名，供界面提示显示。
        """
        from strange_uta_game.frontend.font_utils import make_ch_width_metrics

        self._ch_width_fm, effective = make_ch_width_metrics(family)
        self._line_info_area.update()
        return effective

    def _ch_width_metrics(self):
        """返回字宽统计使用的度量；未设置时回退编辑器自身度量。"""
        return self._ch_width_fm if self._ch_width_fm is not None else self.fontMetrics()

    def _on_update_request(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
            self._line_info_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
            self._line_info_area.update(
                0, rect.y(), self._line_info_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )
        info_w = self.line_info_area_width()
        self._line_info_area.setGeometry(
            QRect(cr.right() - info_w, cr.top(), info_w, cr.height())
        )

    def line_number_area_paint_event(self, event):
        from strange_uta_game.frontend.theme import theme

        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), theme.editor_gutter_bg)
        painter.setPen(theme.editor_gutter_fg)

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        bottom = top + round(self.blockBoundingRect(block).height())
        line_h = self.fontMetrics().height()
        width = self._line_number_area.width() - 4

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, top, width, line_h,
                    Qt.AlignmentFlag.AlignRight, str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    def line_info_area_paint_event(self, event):
        from strange_uta_game.frontend.theme import theme

        painter = QPainter(self._line_info_area)
        painter.fillRect(event.rect(), theme.editor_gutter_bg)
        painter.setPen(theme.editor_gutter_fg)

        block = self.firstVisibleBlock()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        bottom = top + round(self.blockBoundingRect(block).height())
        line_h = self.fontMetrics().height()
        area_w = self._line_info_area.width()

        # 字宽以卡拉OK主文字字体度量；标签文本仍用编辑器自身字体绘制。
        fm = self._ch_width_metrics()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                stripped = _strip_line_tags(block.text())
                ch = _calc_ch_width(stripped, fm)
                ch_r = round(ch, 1)
                label = f"{int(ch_r)}ch" if ch_r == int(ch_r) else f"{ch_r}ch"
                painter.drawText(
                    0, top, area_w - 6, line_h,
                    Qt.AlignmentFlag.AlignRight, label,
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())

    def contextMenuEvent(self, event):
        """右键菜单用 qfluentwidgets 主题菜单，跟随明暗主题配色。

        原生 ``QPlainTextEdit`` 的默认菜单不受 qfluentwidgets 主题接管，
        深色模式下仍是系统白色弹窗；``TextEditMenu`` 对
        ``QPlainTextEdit`` 同样适用（只依赖 textCursor/toPlainText 及
        cut/copy/paste/undo/selectAll 槽）。
        """
        TextEditMenu(self).exec(event.globalPos())

    def wheelEvent(self, event):
        """Alt+滚轮缩放字体（放大/缩小），其余情况维持默认滚动。

        注意：Windows 下按住 Alt 滚动时，滚轮增量会被转到水平轴
        （angleDelta().x()），故需同时读取 x/y，否则 y 恒为 0、缩放失效。
        """
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            ad = event.angleDelta()
            delta = ad.y() or ad.x()
            if delta > 0:
                self.zoom_requested.emit(1)
            elif delta < 0:
                self.zoom_requested.emit(-1)
            event.accept()
            return
        super().wheelEvent(event)


class _TimedFormatHighlighter(QSyntaxHighlighter):
    """带时间戳全文本格式的语法着色（类 VSCode）。

    着色：起始时间戳 [..]、停顿点时间戳 [>..]、演唱者标签 【..】、
    花括号/分隔符 { } || | ,。颜色随深/浅主题切换。
    """

    # 合规块：{原文||读音...}（原文非空）
    _VALID_BLOCK_RE = re.compile(r"\{[^{}]+\|\|[^{}]*\}")
    _SEP_INNER_RE = re.compile(r"\|\||[{}|,]")   # 仅在合规块内着色
    _SINGER_RE = re.compile(r"【[^】]*】")
    # 仅匹配合法 token：[T] / [>T] 或 [mm:ss.xx] / [>mm:ss.xx]
    _END_TS_RE = re.compile(r"\[>(?:T|\d+:\d{2}\.\d{2})\]")
    _START_TS_RE = re.compile(r"\[(?:T|\d+:\d{2}\.\d{2})\]")

    def __init__(self, document):
        super().__init__(document)
        self._formats = self._build_formats()

    @staticmethod
    def _build_formats():
        from strange_uta_game.frontend.theme import theme

        palette = {
            "sep": theme.syntax_separator,
            "singer": theme.syntax_singer,
            "start": theme.syntax_timestamp,
            "end": theme.syntax_timestamp_end,
        }
        fmts = {}
        for key, color in palette.items():
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            fmts[key] = f
        return fmts

    def rehighlight_with_theme(self):
        """主题切换后重建颜色并重刷。"""
        self._formats = self._build_formats()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        # 分隔符着色仅限合规块内（不合规的 {..} 当普通文字，不着色）
        sep_fmt = self._formats["sep"]
        for block_m in self._VALID_BLOCK_RE.finditer(text):
            block_start = block_m.start()
            for sep_m in self._SEP_INNER_RE.finditer(block_m.group()):
                self.setFormat(block_start + sep_m.start(), sep_m.end() - sep_m.start(), sep_fmt)

        # 演唱者标签 / 时间戳：全局范围着色
        for regex, key in (
            (self._SINGER_RE, "singer"),
            (self._START_TS_RE, "start"),
            (self._END_TS_RE, "end"),
        ):
            fmt = self._formats[key]
            for m in regex.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


class DeleteRubyByTypeDialog(QDialog):
    """按字符类型选择要删除注音的对话框。

    片假名拆分为两个子类型：
    - katakana_hiragana_ruby: 注音全为平假名的片假名字符
    - katakana_english_ruby:  注音含有非平假名内容（如英文）的片假名字符
    两者默认均不启用。
    """

    _TYPE_LABELS: list[tuple] = [
        (CharType.HIRAGANA, "ひらがな（平假名）"),
        ("katakana_hiragana_ruby", "カタカナ（片假名・注音为平假名）"),
        ("katakana_english_ruby", "カタカナ（片假名・注音含有英文）"),
        (CharType.KANJI, "漢字（汉字）"),
        (CharType.ALPHABET, "アルファベット（英文字母）"),
        (CharType.HANGUL, "ハングル（韩文谚文）"),
        (CharType.NUMBER, "数字"),
        (CharType.SYMBOL, "記号（符号）"),
        (CharType.LONG_VOWEL, "長音符号（ー、～等）"),
        (CharType.SOKUON, "促音（っ/ッ）"),
        (CharType.OTHER, "その他（♪等特殊符号）"),
        (CharType.SPACE, "空格"),
    ]

    _TYPE_NAME_MAP: dict = {
        CharType.HIRAGANA: "hiragana",
        "katakana_hiragana_ruby": "katakana_hiragana_ruby",
        "katakana_english_ruby": "katakana_english_ruby",
        CharType.KANJI: "kanji",
        CharType.ALPHABET: "alphabet",
        CharType.HANGUL: "hangul",
        CharType.NUMBER: "number",
        CharType.SYMBOL: "symbol",
        CharType.LONG_VOWEL: "long_vowel",
        CharType.SOKUON: "sokuon",
        CharType.OTHER: "other",
        CharType.SPACE: "space",
    }

    _NAME_TYPE_MAP = {v: k for k, v in _TYPE_NAME_MAP.items()}

    def __init__(self, parent=None, initial_types: list[str] | None = None):
        """
        Args:
            parent: 父组件
            initial_types: 初始选中的类型名称列表（config 格式），如 ["hiragana", "katakana_hiragana_ruby"]
        """
        super().__init__(parent)
        self.setWindowTitle(self.tr("按类型删除注音"))
        fit_to_screen(self, 320, 400)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        lbl = QLabel(self.tr("选择要删除注音的字符类型："))
        lbl.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl)

        # 确定默认选中项（片假名两个子类型默认均不启用）
        if initial_types is not None:
            default_set = {self._NAME_TYPE_MAP[n] for n in initial_types if n in self._NAME_TYPE_MAP}
            # 兼容 1.4.5 预发布版本曾保存的独立全角空格选项。
            if "full_space" in initial_types:
                default_set.add(CharType.SPACE)
        else:
            default_set = {CharType.HIRAGANA}

        # 显式枚举每个 label 的 self.tr 直呼，让 .ts 抽取器把源串归入
        # 本类上下文（class 级常量里的字符串无法在抽取期定位到 self，
        # 也不能经别名转发——pylupdate6 只认 self.tr("字面量") 直呼）。
        self.tr("ひらがな（平假名）")
        self.tr("カタカナ（片假名・注音为平假名）")
        self.tr("カタカナ（片假名・注音含有英文）")
        self.tr("漢字（汉字）")
        self.tr("アルファベット（英文字母）")
        self.tr("ハングル（韩文谚文）")
        self.tr("数字")
        self.tr("記号（符号）")
        self.tr("長音符号（ー、～等）")
        self.tr("促音（っ/ッ）")
        self.tr("その他（♪等特殊符号）")
        self.tr("空格")

        self._checkboxes: list[tuple] = []
        for char_type, label in self._TYPE_LABELS:
            cb = CheckBox(self.tr(label), self)
            cb.setChecked(char_type in default_set)
            layout.addWidget(cb)
            self._checkboxes.append((char_type, cb))

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_ok = PrimaryPushButton(self.tr("删除选中类型"), self)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def selected_types(self) -> list:
        """返回用户选中的类型键列表（CharType 或特殊字符串）。"""
        return [ct for ct, cb in self._checkboxes if cb.isChecked()]

    def selected_type_names(self) -> list[str]:
        """返回用户选中的类型名称列表（config 格式）。"""
        return [self._TYPE_NAME_MAP[ct] for ct, cb in self._checkboxes if cb.isChecked()]


class DeleteCheckpointByTypeDialog(QDialog):
    """按字符类型选择要删除节奏点的对话框。

    与 DeleteRubyByTypeDialog 的差异：节奏点删除与注音内容无关，因此没有
    片假名注音子类型，片假名为单一类型。类型勾选的连带语义（平假名→っ、
    片假名→ッ/ー）由后端 delete_checkpoints_by_type_names 决定。
    """

    _TYPE_LABELS: list[tuple] = [
        (CharType.HIRAGANA, "ひらがな（平假名）"),
        (CharType.KATAKANA, "カタカナ（片假名）"),
        (CharType.KANJI, "漢字（汉字）"),
        (CharType.ALPHABET, "アルファベット（英文字母）"),
        (CharType.HANGUL, "ハングル（韩文谚文）"),
        (CharType.NUMBER, "数字"),
        (CharType.SYMBOL, "記号（符号）"),
        (CharType.LONG_VOWEL, "長音符号（ー、～等）"),
        (CharType.SOKUON, "促音（っ/ッ）"),
        ("small_kana", "小写假名（ぁゃ等）"),
        (CharType.OTHER, "その他（♪等特殊符号）"),
        (CharType.SPACE, "空格"),
    ]

    _TYPE_NAME_MAP: dict = {
        CharType.HIRAGANA: "hiragana",
        CharType.KATAKANA: "katakana",
        CharType.KANJI: "kanji",
        CharType.ALPHABET: "alphabet",
        CharType.HANGUL: "hangul",
        CharType.NUMBER: "number",
        CharType.SYMBOL: "symbol",
        CharType.LONG_VOWEL: "long_vowel",
        CharType.SOKUON: "sokuon",
        "small_kana": "small_kana",
        CharType.OTHER: "other",
        CharType.SPACE: "space",
    }

    _NAME_TYPE_MAP = {v: k for k, v in _TYPE_NAME_MAP.items()}

    def __init__(self, parent=None, initial_types: list[str] | None = None):
        """
        Args:
            parent: 父组件
            initial_types: 初始选中的类型名称列表（config 格式），如 ["hiragana", "kanji"]
        """
        super().__init__(parent)
        self.setWindowTitle(self.tr("按类型删除节奏点"))
        fit_to_screen(self, 320, 400)
        self.setFont(ui_font(10))

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        lbl = QLabel(self.tr("选择要删除节奏点的字符类型："))
        lbl.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl)

        if initial_types is not None:
            default_set = {self._NAME_TYPE_MAP[n] for n in initial_types if n in self._NAME_TYPE_MAP}
            # 兼容预发布版本的独立全角空格键。
            if "full_space" in initial_types:
                default_set.add(CharType.SPACE)
        else:
            default_set = {CharType.HIRAGANA}

        # 显式枚举每个 label 的 self.tr 直呼，让 .ts 抽取器把源串归入
        # 本类上下文（class 级常量里的字符串无法在抽取期定位到 self，
        # 也不能经别名转发——pylupdate6 只认 self.tr("字面量") 直呼）。
        self.tr("ひらがな（平假名）")
        self.tr("カタカナ（片假名）")
        self.tr("漢字（汉字）")
        self.tr("アルファベット（英文字母）")
        self.tr("ハングル（韩文谚文）")
        self.tr("数字")
        self.tr("記号（符号）")
        self.tr("長音符号（ー、～等）")
        self.tr("促音（っ/ッ）")
        self.tr("小写假名（ぁゃ等）")
        self.tr("その他（♪等特殊符号）")
        self.tr("空格")

        self._checkboxes: list[tuple] = []
        for char_type, label in self._TYPE_LABELS:
            cb = CheckBox(self.tr(label), self)
            cb.setChecked(char_type in default_set)
            layout.addWidget(cb)
            self._checkboxes.append((char_type, cb))

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_ok = PrimaryPushButton(self.tr("删除选中类型"), self)
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self.accept)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)

    def selected_types(self) -> list:
        """返回用户选中的类型键列表（CharType）。"""
        return [ct for ct, cb in self._checkboxes if cb.isChecked()]

    def selected_type_names(self) -> list[str]:
        """返回用户选中的类型名称列表（config 格式）。"""
        return [self._TYPE_NAME_MAP[ct] for ct, cb in self._checkboxes if cb.isChecked()]

from strange_uta_game.frontend.editor.find_dialog import FindDialog

class RubyInterface(QWidget):
    """注音编辑界面

    全文本视图 + 批量操作。
    """

    rubies_changed = pyqtSignal()
    close_requested = pyqtSignal()  # 底部「关闭」按钮请求关闭承载它的对话框

    def __init__(self, parent=None):
        super().__init__(parent)

        self._project: Optional[Project] = None
        self._find_highlights: list = []
        self._find_dialog: Optional[FindDialog] = None
        # 后台注音任务（分析/删除/更新节奏点）互斥标志；worker/thread 强引用
        # 挂在同名属性下，防止 QThread 被提前回收。
        self._ruby_analyzing = False

        self._init_ui()

    def _init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        # 标题（存为实例属性以便 retranslate）
        self._lbl_title = QLabel(self.tr("全文本编辑"))
        self._lbl_title.setStyleSheet("font-size: 22px; font-weight: bold;")
        self._lbl_title_tip = CaptionLabel(self.tr("（编辑后点「应用更改」写回；行号对应歌词行，时间轴随文本保留）"))
        title_layout = QHBoxLayout()
        title_layout.addWidget(self._lbl_title)
        title_layout.addWidget(self._lbl_title_tip)
        title_layout.addStretch()
        layout.addLayout(title_layout)

        # 说明（功能 + 格式，简洁）
        self._lbl_desc = CaptionLabel(self.tr(
            "逐行编辑整篇歌词。格式：{原文||读音} 为注音块，"
            "注音块中`|` 分 RubyPart、`,` 分字；时间戳在字前 [分:秒.厘秒]（空=[T]），"
            "停顿点 [>…] 贴在字后，演唱者切换用 【名】。"
        ))
        self._lbl_desc.setWordWrap(True)
        layout.addWidget(self._lbl_desc)

        # 批量操作按钮
        batch_layout = QHBoxLayout()

        self.btn_auto_all = PushButton(self.tr("全部注音 · 含节奏点"), self)
        self.btn_auto_all.setIcon(FIF.SYNC)
        self.btn_auto_all.clicked.connect(self._on_auto_analyze_all)
        self.btn_auto_all.setEnabled(False)
        batch_layout.addWidget(self.btn_auto_all)

        self.btn_auto_all_no_cp = PushButton(self.tr("全部注音 · 仅注音"), self)
        self.btn_auto_all_no_cp.setIcon(FIF.SYNC)
        self.btn_auto_all_no_cp.clicked.connect(self._on_auto_analyze_all_no_cp)
        self.btn_auto_all_no_cp.setEnabled(False)
        batch_layout.addWidget(self.btn_auto_all_no_cp)

        self.btn_delete_by_type = PushButton(self.tr("按类型删除注音"), self)
        self.btn_delete_by_type.setIcon(FIF.DELETE)
        self.btn_delete_by_type.clicked.connect(self._on_delete_rubies_by_type)
        self.btn_delete_by_type.setEnabled(False)
        batch_layout.addWidget(self.btn_delete_by_type)

        self.btn_update_cp = PushButton(self.tr("更新节奏点"), self)
        self.btn_update_cp.setIcon(FIF.UPDATE)
        self.btn_update_cp.clicked.connect(self._on_update_checkpoints)
        self.btn_update_cp.setEnabled(False)
        batch_layout.addWidget(self.btn_update_cp)

        self.btn_find = PushButton(self.tr("查找和替换"), self)
        self.btn_find.setIcon(FIF.SEARCH)
        self.btn_find.clicked.connect(self._open_find)
        batch_layout.addWidget(self.btn_find)

        # 字号调整（与上面按钮同栏；也可用 Alt+滚轮）
        batch_layout.addSpacing(12)

        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        _settings = AppSettings()
        _saved_font_size = _settings.get("fulltext_editor.font_size", 12)
        _saved_show_ch = _settings.get("fulltext_editor.show_ch_width", True)
        self._ch_width_font = _settings.get("ui.main_font", "Microsoft YaHei")

        self._lbl_font_size = CaptionLabel(self.tr("字号"))
        batch_layout.addWidget(self._lbl_font_size)
        self.spin_font = SpinBox(self)
        self.spin_font.setRange(8, 48)
        self.spin_font.setValue(_saved_font_size)
        self.spin_font.setMinimumWidth(130)
        self.spin_font.valueChanged.connect(self._apply_font_size)
        batch_layout.addWidget(self.spin_font)

        batch_layout.addSpacing(12)
        self._lbl_ch_width = CaptionLabel(self.tr("字宽统计"))
        batch_layout.addWidget(self._lbl_ch_width)
        self.switch_ch_width = SwitchButton(self)
        self.switch_ch_width.setChecked(_saved_show_ch)
        self.switch_ch_width.setMinimumWidth(50)
        self.switch_ch_width.setOnText(self.tr("开"))
        self.switch_ch_width.setOffText(self.tr("关"))
        self.switch_ch_width.checkedChanged.connect(self._on_ch_width_toggled)
        batch_layout.addWidget(self.switch_ch_width)

        # 提示当前用于计算字宽的字体（跟随卡拉OK主文字字体）
        self.lbl_ch_font = CaptionLabel("")
        batch_layout.addWidget(self.lbl_ch_font)

        batch_layout.addStretch()

        layout.addLayout(batch_layout)

        # 全文本编辑器（带行号栏 + 语法着色，Alt+滚轮缩放字体）
        self.text_edit = LineNumberPlainTextEdit()
        self.text_edit.setFont(ui_font(12))
        self.text_edit.setPlaceholderText(self.tr(
            "加载项目后，歌词将以带时间戳的注音格式显示在此处...\n"
            "示例: {大冒険||[00:01.00]だ|[00:01.20]い,...}"
        ))
        self.text_edit.setMinimumHeight(300)
        self._highlighter = _TimedFormatHighlighter(self.text_edit.document())
        # 实时高亮光标所在行
        self.text_edit.cursorPositionChanged.connect(self._highlight_current_line)
        # Alt+滚轮缩放字体 → 同步到字号 SpinBox（由其驱动实际字号）
        self.text_edit.zoom_requested.connect(self._on_zoom_requested)
        # 主题切换时刷新着色 / 行号栏 / 当前行高亮颜色
        from strange_uta_game.frontend.theme import theme
        theme.changed.connect(self._on_theme_changed)
        layout.addWidget(self.text_edit, stretch=1)

        self._find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self.text_edit)
        self._find_shortcut.activated.connect(self._open_find)

        self.text_edit.set_show_ch_width(_saved_show_ch)
        # 字宽统计跟随卡拉OK主文字字体（缺字回退微软雅黑测量），并提示实际字体
        _effective_ch_font = self.text_edit.set_ch_width_font(self._ch_width_font)
        self._update_ch_font_label(_effective_ch_font)

        # 底部栏：左下角信息，右下角 应用更改 / 还原 / 关闭
        action_layout = QHBoxLayout()

        self.lbl_stats = CaptionLabel(self.tr("共 {lines} 行，{rubies} 个注音").format(lines=0, rubies=0))
        action_layout.addWidget(self.lbl_stats)

        action_layout.addStretch()

        self.btn_apply = PrimaryPushButton(self.tr("应用更改"), self)
        self.btn_apply.setIcon(FIF.ACCEPT)
        self.btn_apply.clicked.connect(self._on_apply_changes)
        self.btn_apply.setEnabled(False)
        action_layout.addWidget(self.btn_apply)

        self.btn_revert = PushButton(self.tr("还原"), self)
        self.btn_revert.setIcon(FIF.CANCEL)
        self.btn_revert.clicked.connect(self._on_revert)
        self.btn_revert.setEnabled(False)
        action_layout.addWidget(self.btn_revert)

        self.btn_close = PushButton(self.tr("关闭"), self)
        self.btn_close.setIcon(FIF.CLOSE)
        self.btn_close.clicked.connect(self.close_requested.emit)
        action_layout.addWidget(self.btn_close)

        layout.addLayout(action_layout)

    # ==================== 公共接口 ====================

    def set_project(self, project: Project, line_idx: int = 0):
        """设置项目"""
        self._project = project
        self._refresh_display()

    def set_store(self, store):
        """接入 ProjectStore 统一数据中心。"""
        self._store = store
        store.data_changed.connect(self._on_data_changed)

    def _on_data_changed(self, change_type: str):
        """响应 ProjectStore 的数据变更。"""
        try:
            if change_type == "project":
                self._project = self._store.project
                self._refresh_display()
            elif change_type in ("rubies", "lyrics"):
                self._refresh_display()
            elif change_type == "settings":
                self._refresh_ch_width_font()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[FulltextInterface] _on_data_changed(%s) 失败: %s",
                change_type, e, exc_info=True)
            self._store.error_notify.emit("数据刷新异常", str(e))

    def _refresh_ch_width_font(self):
        """设置变更时，重新读取卡拉OK主文字字体并应用到字宽统计。"""
        from strange_uta_game.frontend.settings.settings_interface import AppSettings

        self._ch_width_font = AppSettings().get("ui.main_font", "Microsoft YaHei")
        effective = self.text_edit.set_ch_width_font(self._ch_width_font)
        self._update_ch_font_label(effective)

    def is_dirty(self) -> bool:
        """检查文本编辑器内容是否与项目数据不同"""
        if not self._project:
            return False
        return self.text_edit.toPlainText() != self._lines_to_text()

    def scroll_to_line(self, line_idx: int, char_idx: int = 0):
        """从打轴界面切换至全文本编辑时，将输入光标跳转到 (line_idx, char_idx)。

        文本由 _lines_to_text() 以带时间戳格式生成；这里用后端
        :func:`...annotated_text.timed_line_columns` 复算同一编码、得到目标
        字符的字形列号（计入内联时间戳 token、演唱者标签、花括号等语法长度），
        并按相同的跨行演唱者延续计算行首继承，保证列号与渲染严格对齐。
        """
        if not self._project or not self._project.sentences:
            return
        if not (0 <= line_idx < len(self._project.sentences)):
            return
        sentence = self._project.sentences[line_idx]
        chars = sentence.characters
        if char_idx < 0:
            char_idx = 0
        if char_idx > len(chars):
            char_idx = len(chars)

        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            sentence_to_timed_line,
            timed_line_columns,
        )

        id_to_name, _name_to_id, default_id = self._singer_context()
        offset = self._global_offset()
        # 按 _lines_to_text 的跨行演唱者延续，算出本行行首继承的 singer
        inherited = default_id
        for s in self._project.sentences[:line_idx]:
            _, inherited = sentence_to_timed_line(
                s.characters,
                singer_id_to_name=id_to_name,
                line_singer_id=s.singer_id,
                default_singer_id=default_id,
                inherited_singer_id=inherited,
                offset_ms=offset,
            )

        cols = timed_line_columns(
            chars,
            singer_id_to_name=id_to_name,
            line_singer_id=sentence.singer_id,
            default_singer_id=default_id,
            inherited_singer_id=inherited,
            offset_ms=offset,
        )
        if 0 <= char_idx < len(cols):
            column = cols[char_idx]
        elif cols:
            # char_idx == len(chars)：定位到末字符之后
            column = cols[-1] + 1
        else:
            column = 0

        # 定位 QTextCursor
        doc = self.text_edit.document()
        if doc is None:
            return
        block = doc.findBlockByNumber(line_idx)
        if not block.isValid():
            return
        cursor = self.text_edit.textCursor()
        cursor.setPosition(block.position() + min(column, block.length() - 1))
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()
        self.text_edit.setFocus()

    def focus_line(self, line_idx: int, char_idx: int = 0):
        """进入界面时聚焦某行：光标定位（触发整行高亮）+ 把该行滚到视口顶部。"""
        if not self._project or not self._project.sentences:
            return
        line_idx = max(0, min(line_idx, len(self._project.sentences) - 1))
        self.scroll_to_line(line_idx, char_idx)
        # 尽量把目标行滚到视口顶部（而非跳到深处），便于从该行往下看
        bar = self.text_edit.verticalScrollBar()
        if bar is not None:
            bar.setValue(min(line_idx, bar.maximum()))

    def _highlight_current_line(self):
        """实时高亮光标所在行（随光标移动刷新，extraSelection 不改文本）。"""
        doc = self.text_edit.document()
        if doc is None:
            return
        block = self.text_edit.textCursor().block()
        if not block.isValid():
            return
        from strange_uta_game.frontend.theme import theme

        selections: list = []

        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(theme.editor_current_line)
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        sel.cursor = cursor
        selections.append(sel)

        selections.extend(self._find_highlights)

        self.text_edit.setExtraSelections(selections)

    def _on_theme_changed(self):
        """主题切换：重建着色配色、刷新行号栏与当前行高亮。"""
        if hasattr(self, "_highlighter"):
            self._highlighter.rehighlight_with_theme()
        self.text_edit._line_number_area.update()
        self.text_edit._line_info_area.update()
        self._highlight_current_line()

    def _refresh_highlight(self):
        """强制刷新高亮（当前行 + 查找匹配）。"""
        if hasattr(self, "_highlighter"):
            self._highlight_current_line()

    def _open_find(self):
        """打开查找对话框。如已存在则激活置顶。"""
        if self._find_dialog is not None and self._find_dialog.isVisible():
            self._find_dialog.raise_()
            self._find_dialog.activateWindow()
            self._find_dialog._search_input.setFocus()
            self._find_dialog._search_input.selectAll()
            return
        dlg = FindDialog(self, self.window())
        self._find_dialog = dlg
        dlg.finished.connect(lambda: self._on_find_dialog_closed())
        dlg.show()

    def _on_find_dialog_closed(self):
        self._find_highlights = []
        if self._find_dialog is not None:
            # 关闭即销毁：对话框仍以主窗口为父，留着只会随 textChanged
            # 残留连接反复重跑搜索、复活已清掉的高亮（僵尸）。textChanged
            # 连接已在 FindDialog.closeEvent 内断开，这里回收对象本身。
            self._find_dialog.deleteLater()
            self._find_dialog = None
        self._highlight_current_line()

    def _apply_font_size(self, pt: int):
        """设置编辑器字号（由字号 SpinBox 与 Alt+滚轮共同驱动）。"""
        font = self.text_edit.font()
        font.setPointSize(pt)
        self.text_edit.setFont(font)
        self.text_edit._update_width()
        self.text_edit._line_number_area.update()
        self.text_edit._line_info_area.update()
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        _settings = AppSettings()
        _settings.set("fulltext_editor.font_size", pt)
        _settings.save()

    def _on_zoom_requested(self, delta: int):
        """Alt+滚轮：调整字号 SpinBox（其 valueChanged 再驱动实际字号）。"""
        self.spin_font.setValue(self.spin_font.value() + delta)

    def _update_ch_font_label(self, effective_family: str):
        """更新字宽统计字体提示。effective_family 为实际测量所用字体族。"""
        if effective_family and effective_family != self._ch_width_font:
            # 所选字体缺少全角参考字形，已回退测量
            self.lbl_ch_font.setText(
                self.tr("字宽字体：{src} → {eff}").format(
                    src=self._ch_width_font, eff=effective_family
                )
            )
            self.lbl_ch_font.setToolTip(
                self.tr(
                    "所选字体「{src}」缺少全角参考字形，字宽改用「{eff}」测量"
                ).format(src=self._ch_width_font, eff=effective_family)
            )
        else:
            self.lbl_ch_font.setText(
                self.tr("字宽字体：{eff}").format(eff=effective_family)
            )
            self.lbl_ch_font.setToolTip(
                self.tr("字宽统计跟随卡拉OK主文字字体（设置 › 界面设定 › 主文字字体）")
            )

    def _on_ch_width_toggled(self, checked: bool):
        self.text_edit.set_show_ch_width(checked)
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        _settings = AppSettings()
        _settings.set("fulltext_editor.show_ch_width", checked)
        _settings.save()

    # ==================== 内部方法 ====================

    def _refresh_display(self):
        """刷新全部显示"""
        has_project = self._project is not None

        for btn in (
            self.btn_auto_all,
            self.btn_auto_all_no_cp,
            self.btn_delete_by_type,
            self.btn_update_cp,
            self.btn_apply,
            self.btn_revert,
        ):
            btn.setEnabled(has_project)

        if has_project:
            self.text_edit.setPlainText(self._lines_to_text())
            self._update_stats()
        else:
            self.text_edit.setPlainText("")
            self.lbl_stats.setText(
                self.tr("共 {lines} 行，{rubies} 个注音").format(lines=0, rubies=0)
            )

    def _global_offset(self) -> int:
        """全局偏移（项目优先，回退设置）——编解码时用它使显示与打轴一致。"""
        if not self._project:
            return 0
        offset = self._project.global_offset_ms
        if offset is None:
            try:
                from strange_uta_game.frontend.settings.settings_interface import (
                    AppSettings,
                )
                offset = AppSettings().get("export.offset_ms", 0)
            except Exception:
                offset = 0
        return offset or 0

    def _singer_context(self):
        """返回 (id→name, name→id, default_singer_id)，供带时间戳格式编解码用。"""
        singers = list(getattr(self._project, "singers", []) or [])
        id_to_name = {s.id: s.name for s in singers}
        name_to_id = {s.name: s.id for s in singers}
        default_id = ""
        for s in singers:
            if getattr(s, "is_default", False):
                default_id = s.id
                break
        if not default_id and singers:
            default_id = singers[0].id
        return id_to_name, name_to_id, default_id

    def _lines_to_text(self) -> str:
        """将项目歌词转为带内联时间戳的全文本（无损往返格式）。

        每行委托给后端
        :func:`...annotated_text.sentence_to_timed_line`，并跨行延续演唱者
        （与 Nicokara 导出 prev_singer 行为一致）。
        """
        if not self._project:
            return ""
        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            sentence_to_timed_line,
        )

        id_to_name, _name_to_id, default_id = self._singer_context()
        offset = self._global_offset()
        inherited = default_id
        lines: List[str] = []
        for sentence in self._project.sentences:
            line, inherited = sentence_to_timed_line(
                sentence.characters,
                singer_id_to_name=id_to_name,
                line_singer_id=sentence.singer_id,
                default_singer_id=default_id,
                inherited_singer_id=inherited,
                offset_ms=offset,
            )
            lines.append(line)
        return "\n".join(lines)

    def _update_stats(self):
        """更新统计标签"""
        if not self._project:
            self.lbl_stats.setText(
                self.tr("共 {lines} 行，{rubies} 个注音").format(lines=0, rubies=0)
            )
            return

        total = sum(
            sum(1 for c in s.characters if c.ruby) for s in self._project.sentences
        )
        self.lbl_stats.setText(
            self.tr("共 {lines} 行，{rubies} 个注音").format(
                lines=len(self._project.sentences), rubies=total
            )
        )

    def _create_auto_check_service(
        self, auto_detect_chinese: bool = False, annotate_korean: bool = False
    ):
        """创建带设置的自动检查服务

        Args:
            auto_detect_chinese: True=自动检测中文歌词并走中文模式。
                「更新节奏点」传 True，使其与主编辑器注音流程行为一致；
                「全部注音」传 False，用户明确表达注音意图不做中文检测。
            annotate_korean: True=韩文歌词带风格注音（片假名/平假名/罗马音）；
                False=韩文分支仅重算节奏点（每字 1 拍，不写注音，
                与中文路径无拼音时的语义一致）。
        """
        from strange_uta_game.frontend.settings.settings_interface import AppSettings
        from strange_uta_game.backend.application import is_chinese_lyrics, is_korean_lyrics

        app_settings = AppSettings()
        all_settings = app_settings.get_all()
        auto_check_flags = all_settings.get("auto_check", {})
        user_dict = app_settings.load_effective_dictionary()
        annotate_katakana_with_english = app_settings.get(
            "ruby_dictionary.annotate_katakana_with_english", False
        )

        chinese_mode = (
            auto_detect_chinese
            and auto_check_flags.get("chinese_lyrics_detection", True)
            and bool(self._project)
            and is_chinese_lyrics("".join(s.text for s in self._project.sentences))
        )

        # 韩文歌词检测（含谚文即韩文，「韩文歌词检测」设置门控）：
        # 注音与更新节奏点均按韩文分支处理节奏点；是否写风格注音
        # 由 annotate_korean 区分调用方意图。
        korean_mode = (
            auto_check_flags.get("korean_lyrics_detection", True)
            and bool(self._project)
            and is_korean_lyrics("".join(s.text for s in self._project.sentences))
        )
        if korean_mode:
            korean_analyzer = None
            if annotate_korean:
                from strange_uta_game.backend.infrastructure.parsers.korean_reading import (
                    create_korean_reading_analyzer,
                )

                korean_analyzer = create_korean_reading_analyzer(
                    auto_check_flags.get("korean_annotation_style", "katakana")
                )
            return AutoCheckService(
                auto_check_flags=auto_check_flags,
                user_dictionary=user_dict,
                annotate_katakana_with_english=annotate_katakana_with_english,
                korean_mode=True,
                korean_analyzer=korean_analyzer,
            )

        # 用户主动触发：不做中文检测——按下"自动分析全部注音"按钮即明确表达
        # 注音意图，避免纯汉字日文行被误判跳过。
        # LLM 整首一次发送：传入全部行文本以保留上下文（LLM 未激活时忽略）。
        lines = [s.text for s in self._project.sentences] if self._project else []
        analyzer = app_settings.build_ruby_analyzer(
            lines, annotate_katakana_with_english=annotate_katakana_with_english
        )
        return AutoCheckService(
            ruby_analyzer=analyzer,
            auto_check_flags=auto_check_flags,
            user_dictionary=user_dict,
            annotate_katakana_with_english=annotate_katakana_with_english,
            chinese_mode=chinese_mode,
        )

    # ==================== 批量操作 ====================

    def _on_auto_analyze_all_no_cp(self):
        """自动分析全部注音（不更新节奏点）— 只刷注音、保留现有节奏点。"""
        self._on_auto_analyze_all(update_checkpoints=False)

    def _on_auto_analyze_all(self, update_checkpoints: bool = True):
        """自动分析全部注音（异步）：拆成注音与节奏点两步；节奏点失败不影响注音更新。

        弹三选项对话框：
        - 全部重新分析（覆盖已有注音）
        - 仅分析未注音字符（保留已有注音）
        - 取消

        分析在后台 QThread 对项目副本执行，StateToolTip 显示
        「阶段 当前/总数」文字进度，完成后写回并刷新。
        update_checkpoints=False 时只刷注音、保留现有节奏点不动。
        """
        if not self._project:
            return
        if self._ruby_analyzing:
            InfoBar.warning(
                title=self.tr("注音操作进行中"),
                content=self.tr("请等待当前注音操作完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        # 先把文本框里的修改写回项目，避免按钮刷新后丢失用户编辑
        if self.is_dirty():
            self._on_apply_changes()

        from strange_uta_game.frontend.settings.settings_interface import AppSettings

        _app_flags = AppSettings().get_all().get("auto_check", {})
        _llm_active = AppSettings().llm_ruby_active()
        # 韩文歌词不需要日语 IME，跳过 WinRT 安装引导
        from strange_uta_game.backend.application import is_korean_lyrics

        _korean = (
            _app_flags.get("korean_lyrics_detection", True)
            and bool(self._project)
            and is_korean_lyrics("".join(s.text for s in self._project.sentences))
        )
        # LLM 注音激活时不需要本地日语 IME，跳过 WinRT 安装引导。
        if not _llm_active and not _korean:
            from strange_uta_game.frontend.winrt_japanese_guide import (
                ensure_winrt_japanese,
            )

            if not ensure_winrt_japanese(self):
                return

        cp_note = (
            self.tr("（分析后会重算节奏点）")
            if update_checkpoints
            else self.tr("（保留现有节奏点不动）")
        )
        # 三选项对话框
        choice = message_choice(
            self,
            self.tr("自动分析全部注音"),
            self.tr("请选择分析范围：")
            + cp_note
            + "\n\n"
            + self.tr(
                "「全部重新分析」会覆盖现有注音。\n"
                "「仅分析未注音字符」会保留已有的人工/字典注音。"
            ),
            [self.tr("全部重新分析"), self.tr("仅分析未注音字符"), self.tr("取消")],
            default=1,
        )
        if choice in (-1, 2):
            return
        only_noruby = choice == 1

        from copy import deepcopy

        from PyQt6.QtCore import QThread
        from qfluentwidgets import StateToolTip

        from strange_uta_game.frontend.theme import theme
        from strange_uta_game.frontend.workers import RubyAnalyzeWorker

        # AutoCheckService（含 WinRTAnalyzer / LLMRubyAnalyzer）在主线程创建，
        # 确保 WinRT STA apartment 正确；worker 只在自己的线程执行计算。
        # 韩文歌词按「韩文注音风格」设置标注片假名/平假名/罗马音。
        auto_check = self._create_auto_check_service(annotate_korean=True)
        # LLM 注音时是否仍应用用户词典（非 LLM 模式恒为 True）。
        _apply_user_dict = (
            AppSettings().llm_apply_user_dict() if _llm_active else True
        )

        project_copy = deepcopy(self._project)
        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在分析注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = RubyAnalyzeWorker(
            project_copy, auto_check, only_noruby, [],
            llm_apply_user_dict=_apply_user_dict,
            update_checkpoints=update_checkpoints,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._ruby_analyze_worker = worker
        self._ruby_analyze_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_analyze_worker = None
            self._ruby_analyze_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project, deleted_count: int) -> None:
            state_tooltip.setState(True)
            _cleanup()

            # 分析期间（可达数分钟）编辑框可能有未应用的修改，整体写回会
            # 静默回滚它们 —— 先让用户选择覆盖修改还是保留修改。
            if not self._confirm_keep_dirty_edits(self.tr("自动分析注音")):
                return

            before_sentences = deepcopy(self._project.sentences)
            self._project.sentences = deepcopy(analyzed_project.sentences)
            self._push_sentences_undo(before_sentences, self.tr("自动分析注音"))
            self._refresh_display()
            if hasattr(self, "_store"):
                self._store.notify("rubies")
                if update_checkpoints:
                    self._store.notify("checkpoints")
            # LLM 注音失败时已回退本地引擎，提示用户。
            _analyzer = getattr(auto_check, "_analyzer", None)
            if getattr(_analyzer, "llm_failed", False):
                InfoBar.warning(
                    title=self.tr("LLM 注音失败，已回退本地引擎"),
                    content=str(getattr(_analyzer, "last_error", "") or ""),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )
            success_content = (
                self.tr("已为 {lines} 行自动分析注音并更新节奏点")
                if update_checkpoints
                else self.tr("已为 {lines} 行自动分析注音（保留现有节奏点）")
            ).format(lines=len(self._project.sentences))
            InfoBar.success(
                title=self.tr("分析完成"),
                content=success_content,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("注音分析失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self,
            )

        def _on_llm_waiting() -> None:
            state_tooltip.setContent(self.tr("正在等待 LLM 返回…（整首歌词一次性发送，请稍候）"))

        def _on_llm_progress(msg: str) -> None:
            state_tooltip.setContent(msg)

        thread.started.connect(worker.run)
        worker.llm_waiting.connect(_on_llm_waiting)
        worker.llm_progress.connect(_on_llm_progress)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_delete_rubies_by_type(self):
        """按类型删除注音（异步）。

        删除在后台线程对项目副本执行（复用后端 delete_rubies_by_type_names，
        与自动注音管线的删除步骤同一实现），StateToolTip 显示
        「删除注音 当前/总数」文字进度。
        """
        if not self._project:
            return
        if self._ruby_analyzing:
            InfoBar.warning(
                title=self.tr("注音操作进行中"),
                content=self.tr("请等待当前注音操作完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        # 先把文本框里的修改写回项目，避免删除操作刷新后丢失用户编辑
        if self.is_dirty():
            self._on_apply_changes()

        from strange_uta_game.frontend.settings.settings_interface import AppSettings

        app_settings = AppSettings()
        saved_types = app_settings.get("auto_check.delete_ruby_types", [])

        dlg = DeleteRubyByTypeDialog(self, initial_types=saved_types)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected = dlg.selected_types()
        type_names = dlg.selected_type_names()

        # 保存用户选择到配置（无论是否有变化）
        app_settings.set("auto_check.delete_ruby_types", type_names)
        app_settings.save()

        if not selected:
            return

        from copy import deepcopy

        from PyQt6.QtCore import QThread
        from qfluentwidgets import StateToolTip

        from strange_uta_game.backend.application.auto_check_service import (
            delete_rubies_by_type_names,
        )
        from strange_uta_game.frontend.theme import theme
        from strange_uta_game.frontend.workers import ProjectTaskWorker

        project_copy = deepcopy(self._project)
        removed_box = [0]

        def _task(proj, progress_cb):
            removed_box[0] = delete_rubies_by_type_names(
                proj, type_names, progress_callback=progress_cb
            )

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在删除注音"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = ProjectTaskWorker(project_copy, _task)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._ruby_delete_worker = worker
        self._ruby_delete_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._ruby_delete_worker = None
            self._ruby_delete_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project) -> None:
            state_tooltip.setState(True)
            _cleanup()

            # 任务期间编辑框可能有未应用的修改，整体写回前先让用户选择。
            if not self._confirm_keep_dirty_edits(self.tr("删除注音")):
                return

            before_sentences = deepcopy(self._project.sentences)
            self._project.sentences = deepcopy(analyzed_project.sentences)
            self._push_sentences_undo(before_sentences, self.tr("删除注音"))
            self._refresh_display()
            if hasattr(self, "_store"):
                self._store.notify("rubies")

            InfoBar.success(
                title=self.tr("删除完成"),
                content=self.tr("已删除 {count} 个注音（类型: {types}）").format(
                    count=removed_box[0],
                    types=", ".join(
                        self.tr(label)
                        for ct, label in DeleteRubyByTypeDialog._TYPE_LABELS
                        if ct in selected
                    ),
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("删除注音失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    def _on_update_checkpoints(self):
        """根据当前注音更新节奏点（异步，不重新分析注音）。

        先保存文本编辑框内的内容，然后在后台线程对项目副本根据内容和设置
        更新所有节奏点，StateToolTip 显示「更新节奏点 当前/总数」文字进度。
        """
        if not self._project:
            return
        if self._ruby_analyzing:
            InfoBar.warning(
                title=self.tr("注音操作进行中"),
                content=self.tr("请等待当前注音操作完成后再试"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2500,
                parent=self,
            )
            return

        # 先将文本编辑器内容应用回项目数据
        if self.is_dirty():
            self._on_apply_changes()

        from copy import deepcopy

        from PyQt6.QtCore import QThread
        from qfluentwidgets import StateToolTip

        from strange_uta_game.frontend.theme import theme
        from strange_uta_game.frontend.workers import ProjectTaskWorker

        # AutoCheckService 在主线程创建（确保 WinRT STA apartment 正确）。
        auto_check = self._create_auto_check_service(auto_detect_chinese=True)

        project_copy = deepcopy(self._project)

        def _task(proj, progress_cb):
            if auto_check._chinese_mode:
                # 中文项目：按中文模式更新（复用 _apply_chinese_to_sentence
                # 的 check 规则，但保留每个字符已有的 ruby/拼音注音）
                total = len(proj.sentences)
                for i, sentence in enumerate(proj.sentences):
                    text = sentence.text
                    if text:
                        chars = list(text)
                        n = len(chars)
                        check_counts: list = [1] * n
                        auto_check._apply_flags_filter(
                            chars, check_counts, text
                        )
                        auto_check._apply_english_and_endpoints(
                            sentence, check_counts
                        )
                    progress_cb("更新节奏点", i + 1, total)
            else:
                auto_check.update_checkpoints_for_project(
                    proj, progress_callback=progress_cb
                )

        green = theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在更新节奏点"), self.tr("准备中..."), self)
        state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        state_tooltip.move(state_tooltip.getSuitablePos())
        state_tooltip.show()
        self._ruby_analyzing = True

        worker = ProjectTaskWorker(project_copy, _task)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._update_cp_worker = worker
        self._update_cp_thread = thread

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            self._update_cp_worker = None
            self._update_cp_thread = None
            self._ruby_analyzing = False

        def _on_finished(analyzed_project) -> None:
            state_tooltip.setState(True)
            _cleanup()

            # 任务期间编辑框可能有未应用的修改，整体写回前先让用户选择。
            if not self._confirm_keep_dirty_edits(self.tr("更新节奏点")):
                return

            before_sentences = deepcopy(self._project.sentences)
            self._project.sentences = deepcopy(analyzed_project.sentences)
            self._push_sentences_undo(before_sentences, self.tr("更新节奏点"))
            if hasattr(self, "_store"):
                self._store.notify("checkpoints")

            self._refresh_display()

            InfoBar.success(
                title=self.tr("更新完成"),
                content=self.tr("已根据注音更新 {lines} 行的节奏点").format(
                    lines=len(self._project.sentences)
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=2000,
                parent=self,
            )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.warning(
                title=self.tr("更新失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

        thread.started.connect(worker.run)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()

    # ==================== 应用/还原 ====================

    def _find_timing_service(self):
        """沿父链查找 TimingService（其 command_manager 即全局撤销栈）。

        独立窗口（无打轴服务祖先）返回 None，调用方退回直改行为。
        """
        widget = self
        while widget is not None:
            timing_service = getattr(widget, "_timing_service", None)
            if timing_service is not None:
                return timing_service
            widget = widget.parent()
        return None

    def _push_sentences_undo(self, before_sentences, description: str) -> None:
        """把 sentences 整体替换登记为 SentenceSnapshotCommand（可撤销）。

        before 传替换前的深拷贝快照，after 取当前项目状态。找不到共享
        CommandManager 时静默跳过（保持原不可撤销行为）。
        """
        if not self._project:
            return
        timing_service = self._find_timing_service()
        command_manager = (
            getattr(timing_service, "command_manager", None) if timing_service else None
        )
        if command_manager is None:
            return
        from copy import deepcopy

        from strange_uta_game.backend.application.commands import (
            SentenceSnapshotCommand,
        )

        command = SentenceSnapshotCommand(
            self._project,
            before_sentences,
            deepcopy(self._project.sentences),
            description,
        )
        command_manager.execute(command)

    def _confirm_keep_dirty_edits(self, action: str) -> bool:
        """后台注音任务完成、即将整体写回 sentences 前的未应用修改检查。

        任务（可能耗时数分钟）期间在编辑框输入但未「应用更改」的修改会被
        整体写回静默回滚。有修改时弹窗让用户选择；返回 True 表示继续写回，
        False 表示保留编辑框内容、放弃任务结果（含用户取消）。
        """
        if not self.is_dirty():
            return True
        choice = message_choice(
            self,
            self.tr("有未应用的修改"),
            self.tr(
                "{action}已完成，但你在编辑框中的修改尚未「应用更改」。\n\n"
                "「覆盖修改」：写入{action}结果，未应用的修改将被丢弃；\n"
                "「保留修改」：放弃{action}结果，编辑框内容保持不变。"
            ).format(action=action),
            [self.tr("覆盖修改"), self.tr("保留修改"), self.tr("取消")],
            default=1,
        )
        if choice == 1:
            InfoBar.info(
                title=self.tr("已保留修改"),
                content=self.tr("已放弃{action}结果，编辑框内容未变").format(action=action),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
        return choice == 0

    def _on_apply_changes(self):
        """将文本编辑器内容应用回项目（逐行独立解码，自带完整时间轴）。

        采用带内联时间戳的全文本格式：每行用 ``parse_timed_line`` 独立解码成
        一条 Sentence，时间戳/停顿点/连词/演唱者全部来自文本本身。因此**不再做
        任何跨行映射或 diff** —— 行的增删、重排、文本撞车都不会丢失或错配
        时间戳；新增的无 token 字符自然得到空轴。
        """
        if not self._project:
            return

        from copy import deepcopy

        from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
            parse_timed_line,
        )

        text = self.text_edit.toPlainText()
        new_line_strs = text.split("\n")

        _id_to_name, name_to_id, default_singer = self._singer_context()
        if not default_singer:
            default_singer = (
                self._project.sentences[0].singer_id
                if self._project.sentences
                else "default"
            )

        offset = self._global_offset()
        before_sentences = deepcopy(self._project.sentences)
        new_sentences: List[Sentence] = []
        parse_errors = []
        inherited = default_singer
        for i, ls in enumerate(new_line_strs):
            try:
                chars, inherited = parse_timed_line(
                    ls,
                    name_to_singer_id=name_to_id,
                    default_singer_id=default_singer,
                    inherited_singer_id=inherited,
                    offset_ms=offset,
                )
            except Exception as e:
                parse_errors.append(self.tr("第 {n} 行: {err}").format(n=i + 1, err=e))
                chars = []
            # 行级 singer：取首字符 singer，空行沿用 inherited
            if chars and chars[0].singer_id:
                line_singer = chars[0].singer_id
            else:
                line_singer = inherited or default_singer
            new_sentences.append(Sentence(singer_id=line_singer, characters=chars))

        if parse_errors:
            InfoBar.warning(
                title=self.tr("部分行解析失败"),
                content="\n".join(parse_errors[:3]),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        self._project.sentences = new_sentences

        # parse_timed_line 已减去 offset 还原为原始时间戳；这里再统一 set_offset
        # 派生 global_*（含新建字符），与编码时加的 offset 对称。
        for sentence in self._project.sentences:
            for ch in sentence.characters:
                ch.set_offset(offset)

        # 整体重建 sentences 纳入撤销栈（应用前快照 → 可撤销）
        self._push_sentences_undo(before_sentences, self.tr("应用全文本更改"))

        if hasattr(self, "_store"):
            self._store.notify("lyrics")
            self._store.notify("rubies")
        self._update_stats()

        InfoBar.success(
            title=self.tr("应用成功"),
            content=self.tr("已更新 {lines} 行").format(
                lines=len(self._project.sentences)
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )

    def changeEvent(self, event):
        # 全文本编辑器有大量状态（文本内容/高亮器/字号/字宽统计），不做整体
        # 重建——只针对可见字符串精准 retranslate，避免编辑中丢失内容。
        if event.type() == QEvent.Type.LanguageChange:
            self._retranslate_ui()
        super().changeEvent(event)

    def _retranslate_ui(self):
        """语言切换：刷新所有可见字符串（不重建 UI）。"""
        # 标题区
        if hasattr(self, "_lbl_title"):
            self._lbl_title.setText(self.tr("全文本编辑"))
        if hasattr(self, "_lbl_title_tip"):
            self._lbl_title_tip.setText(self.tr("（编辑后点「应用更改」写回；行号对应歌词行，时间轴随文本保留）"))
        if hasattr(self, "_lbl_desc"):
            self._lbl_desc.setText(self.tr(
                "逐行编辑整篇歌词。格式：{原文||读音} 为注音块，"
                "注音块中`|` 分 RubyPart、`,` 分字；时间戳在字前 [分:秒.厘秒]（空=[T]），"
                "停顿点 [>…] 贴在字后，演唱者切换用 【名】。"
            ))
        if hasattr(self, "_lbl_font_size"):
            self._lbl_font_size.setText(self.tr("字号"))
        if hasattr(self, "_lbl_ch_width"):
            self._lbl_ch_width.setText(self.tr("字宽统计"))
        if hasattr(self, "switch_ch_width"):
            self.switch_ch_width.setOnText(self.tr("开"))
            self.switch_ch_width.setOffText(self.tr("关"))
        # 按钮
        if hasattr(self, "btn_auto_all"):
            self.btn_auto_all.setText(self.tr("全部注音 · 含节奏点"))
        if hasattr(self, "btn_auto_all_no_cp"):
            self.btn_auto_all_no_cp.setText(self.tr("全部注音 · 仅注音"))
        if hasattr(self, "btn_delete_by_type"):
            self.btn_delete_by_type.setText(self.tr("按类型删除注音"))
        if hasattr(self, "btn_update_cp"):
            self.btn_update_cp.setText(self.tr("更新节奏点"))
        if hasattr(self, "btn_find"):
            self.btn_find.setText(self.tr("查找和替换"))
        if hasattr(self, "btn_apply"):
            self.btn_apply.setText(self.tr("应用更改"))
        if hasattr(self, "btn_revert"):
            self.btn_revert.setText(self.tr("还原"))
        if hasattr(self, "btn_close"):
            self.btn_close.setText(self.tr("关闭"))
        # 文本框 placeholder
        if hasattr(self, "text_edit"):
            self.text_edit.setPlaceholderText(self.tr(
                "加载项目后，歌词将以带时间戳的注音格式显示在此处...\n"
                "示例: {大冒険||[00:01.00]だ|[00:01.20]い,...}"
            ))
        # 统计标签
        try:
            self._update_stats()
        except Exception:
            pass
        # 字宽字体提示——若已有 effective_family，则刷新；否则跳过
        try:
            if hasattr(self, "lbl_ch_font") and hasattr(self, "text_edit"):
                effective = self.text_edit.set_ch_width_font(self._ch_width_font)
                self._update_ch_font_label(effective)
        except Exception:
            pass

    def _on_revert(self):
        """还原编辑器内容为项目当前状态"""
        self._refresh_display()

        InfoBar.info(
            title=self.tr("已还原"),
            content=self.tr("编辑器内容已还原为项目当前状态"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2000,
            parent=self,
        )


class FullTextEditDialog(QDialog):
    """全文本编辑对话框。

    从打轴界面以对话框形式打开 :class:`RubyInterface`，
    通过共享的 ProjectStore 与打轴界面双向同步：在此处「应用更改」会
    notify("lyrics"/"rubies")，打轴界面据此刷新。
    """

    def __init__(self, store, parent=None, current_line: int = 0, current_char: int = 0):
        super().__init__(parent)
        self.setWindowTitle(self.tr("全文本编辑"))
        # 支持最大化/全屏（QDialog 默认无最大化按钮）
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        fit_to_screen(self, 1400, 900)
        main_win = parent.window() if parent is not None else None
        if main_win is not None and main_win.isMaximized():
            self.setWindowState(Qt.WindowState.WindowMaximized)
        elif main_win is not None and main_win.windowState() & Qt.WindowState.WindowFullScreen:
            self.setWindowState(Qt.WindowState.WindowFullScreen)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.interface = RubyInterface(self)
        self.interface.set_store(store)
        self.interface.close_requested.connect(self.accept)
        if getattr(store, "project", None) is not None:
            self.interface.set_project(store.project)
        layout.addWidget(self.interface, stretch=1)

        # 进入时定位到当前行的当前字符（高亮该行、光标落在该字符），并滚到视口顶部
        self.interface.focus_line(current_line, current_char)

    def _ruby_task_busy(self) -> bool:
        """内嵌 RubyInterface 是否有后台注音任务（分析/删除/更新节奏点）在跑。"""
        iface = getattr(self, "interface", None)
        return bool(iface is not None and getattr(iface, "_ruby_analyzing", False))

    def _notify_task_busy(self) -> None:
        InfoBar.warning(
            title=self.tr("注音操作进行中"),
            content=self.tr("请等待当前注音操作完成后再关闭窗口"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=2500,
            parent=self,
        )

    def accept(self):
        # 后台注音任务运行期间禁止关闭：worker 完成回调依赖本对话框内的控件
        # 与项目引用，中途销毁会导致结果丢失甚至崩溃。Esc/标题栏关闭同样走
        # reject()，由下方守卫拦截。
        if self._ruby_task_busy():
            self._notify_task_busy()
            return
        super().accept()

    def reject(self):
        if self._ruby_task_busy():
            self._notify_task_busy()
            return
        super().reject()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.setWindowTitle(self.tr("全文本编辑"))
        super().changeEvent(event)
