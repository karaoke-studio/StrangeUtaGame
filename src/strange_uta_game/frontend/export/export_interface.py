"""导出界面。

提供多格式导出功能，支持 LRC/KRA/TXT/ASS/Nicokara。
Nicokara 格式支持导出字幕分组（按演唱者分色拆分多轴）和演唱者标签插入。
"""

from PyQt6.QtWidgets import (
    QWidget,
    QLayout,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QDialog,
)
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from qfluentwidgets import (
    PushButton,
    PrimaryPushButton,
    LineEdit,
    InfoBar,
    InfoBarPosition,
    FluentIcon as FIF,
    ScrollArea,
    SimpleCardWidget,
    CheckBox,
    TitleLabel,
    SubtitleLabel,
    CaptionLabel,
    StrongBodyLabel,
    TransparentPushButton,
    themeColor,
    setCustomStyleSheet,
)

from typing import Optional, Dict
from pathlib import Path
from copy import deepcopy
import html
import re

from strange_uta_game.backend.domain import Project, AxisGroup
from strange_uta_game.backend.application.export_service import (
    ExportService,
    sanitize_export_basename,
)
from strange_uta_game.backend.infrastructure.parsers.romaji import RomajiOptions
from strange_uta_game.frontend.settings.settings_interface import (
    AppSettings,
    NicokaraTagsDialog,
)
from strange_uta_game.frontend.theme import theme as _theme
from strange_uta_game.frontend.fluent_widgets import (
    FluentGroupBox,
    ThemedMenuTextEdit,
    message_question,
)
from strange_uta_game.frontend.window_sizing import fit_to_screen


class RubyMismatchDialog(QDialog):
    """注音分段不匹配对话框 — 预览按字符/mora 均分结果并支持直接应用后导出。"""

    def __init__(self, detail: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("注音分段不匹配"))
        fit_to_screen(self, 640, 500)
        self._action: str = "cancel"

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(self.tr(
            "以下字符的注音分段数量与节奏点数量不匹配。\n"
            "可选择自动均分方案修复后继续导出，或忽略继续导出。"
        ))
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self._preview = ThemedMenuTextEdit()
        self._preview.setReadOnly(True)
        self._build_preview_content(detail)
        layout.addWidget(self._preview, 1)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.btn_char = PrimaryPushButton(self.tr("按字符均分并导出"), self)
        self.btn_mora = PrimaryPushButton(self.tr("按mora均分并导出"), self)
        self.btn_ignore = PushButton(self.tr("忽略并继续导出"), self)
        self.btn_cancel = PushButton(self.tr("取消"), self)

        self.btn_char.clicked.connect(lambda: self._set_action("char"))
        self.btn_mora.clicked.connect(lambda: self._set_action("mora"))
        self.btn_ignore.clicked.connect(lambda: self._set_action("ignore"))
        self.btn_cancel.clicked.connect(self.reject)

        btn_layout.addWidget(self.btn_char)
        btn_layout.addWidget(self.btn_mora)
        btn_layout.addWidget(self.btn_ignore)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

    def _build_preview_content(self, detail: dict) -> None:
        lines: list[str] = []
        lines.append("=" * 50)
        lines.append(self.tr("【不匹配列表】"))
        lines.append("=" * 50)
        for line in detail.get("mismatch_lines", []):
            lines.append(f"  {line}")
        lines.append("")
        lines.append("=" * 50)
        lines.append(self.tr("【按字符均分预览】"))
        lines.append("=" * 50)
        for line in detail.get("char_preview_lines", []):
            lines.append(f"  {line}")
        lines.append("")
        lines.append("=" * 50)
        lines.append(self.tr("【按mora均分预览】"))
        lines.append("=" * 50)
        for line in detail.get("mora_preview_lines", []):
            lines.append(f"  {line}")
        self._preview.setPlainText("\n".join(lines))

    def _set_action(self, action: str) -> None:
        self._action = action
        self.accept()

    def get_action(self) -> str:
        return self._action


class ExportInterface(QWidget):
    """导出界面"""

    export_to_next_requested = pyqtSignal()

    def __init__(self, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self._embedded = bool(embedded)
        self._project: Optional[Project] = None
        self._export_service = ExportService()
        # 后台导出任务状态：导出移出 UI 线程执行，期间「导出」按钮转为取消入口
        self._exporting = False
        self._export_worker = None
        self._export_thread = None
        # 输出目录是否由用户通过「浏览」主动选定。一经选定即保留，自动预填不再
        # 覆盖它（切换音频、改设置均不影响），直到用户再次「浏览」更换、或切换项目。
        # 放在 __init__ 而非 _init_ui，避免切语言重建时被清零。
        self._output_user_set = False
        self._init_ui()
        # ExportInterface 本身不会在语言切换时销毁，只连接一次主题信号。
        # _init_ui() 会被 LanguageChange 重复调用，不能在那里累积连接。
        _theme.changed.connect(self._update_theme_style)

    def changeEvent(self, event):
        """切语言时整张导出页拆掉重建。保留输出路径/文件名/格式选中等用户状态。"""
        if event.type() == QEvent.Type.LanguageChange:
            from strange_uta_game.frontend.localization import detach_layout_for_rebuild
            saved = {
                "output": self.line_output.text() if hasattr(self, "line_output") else "",
                "fname": self.line_filename.text() if hasattr(self, "line_filename") else "",
                "fmt_row": self.format_list.currentRow() if hasattr(self, "format_list") else -1,
            }
            detach_layout_for_rebuild(self)
            self._init_ui()  # _init_ui 内部已会调 _populate_formats，无需再调
            # 还原用户输入
            if hasattr(self, "line_output"):
                self.line_output.setText(saved["output"])
            if hasattr(self, "line_filename"):
                self.line_filename.setText(saved["fname"])
            if hasattr(self, "format_list") and saved["fmt_row"] >= 0:
                try:
                    self.format_list.setCurrentRow(saved["fmt_row"])
                except Exception:
                    pass
            # 导出中整页重建：新按钮文字须反映「取消导出」态而非默认「导出」
            if getattr(self, "_exporting", False) and hasattr(self, "btn_export"):
                self.btn_export.setText(self.tr("取消导出"))
            # 重新填充导出字幕分组摘要（_init_ui 不负责，仅在 set_store 时被调）
            if hasattr(self, "_store") and self._store is not None:
                try:
                    self._refresh_axis_group_summary()
                except Exception:
                    pass
        super().changeEvent(event)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        # 标题（保存为实例变量，防止 Python GC 清出 WeakKeyDictionary 导致主题失效）
        self.title_label = TitleLabel(self.tr("导出"))
        layout.addWidget(self.title_label)

        desc = CaptionLabel(self.tr("将项目导出为多种歌词格式"))
        layout.addWidget(desc)

        # 格式选择
        content = QHBoxLayout()
        content.setSpacing(20)

        # 左侧：格式列表
        left_card = SimpleCardWidget()
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(20, 20, 20, 20)
        left_layout.setSpacing(10)

        left_label = SubtitleLabel(self.tr("选择导出格式"))
        left_layout.addWidget(left_label)

        self.format_list = QListWidget()
        self.format_list.setMinimumHeight(200)
        left_layout.addWidget(self.format_list)

        content.addWidget(left_card, 1)

        # 右侧：导出配置
        right_card = SimpleCardWidget()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(20, 20, 20, 20)
        right_layout.setSpacing(15)

        right_label = SubtitleLabel(self.tr("导出设置"))
        right_layout.addWidget(right_label)

        # 设置项可能因格式和演唱者数量显著增高。只让中间设置区滚动，
        # 保持标题与底部导出按钮始终可见。
        self._settings_scroll = ScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._settings_scroll.setObjectName("exportSettingsScroll")
        self._settings_scroll.setStyleSheet(
            "#exportSettingsScroll { border: none; background: transparent; }"
        )
        self._settings_scroll.viewport().setAutoFillBackground(False)
        self._settings_scroll.viewport().setStyleSheet("background: transparent;")

        self._settings_widget = QWidget()
        self._settings_widget.setObjectName("exportSettingsContent")
        self._settings_widget.setAutoFillBackground(False)
        self._settings_widget.setStyleSheet(
            "QWidget#exportSettingsContent { background: transparent; }"
        )
        self._settings_layout = QVBoxLayout(self._settings_widget)
        self._settings_layout.setContentsMargins(0, 0, 6, 0)
        self._settings_layout.setSpacing(15)
        self._settings_layout.setSizeConstraint(
            QLayout.SizeConstraint.SetMinAndMaxSize
        )
        self._settings_scroll.setWidget(self._settings_widget)
        right_layout.addWidget(self._settings_scroll, 1)

        # 输出路径
        path_label = CaptionLabel(self.tr("输出路径"))
        self._settings_layout.addWidget(path_label)

        path_row = QHBoxLayout()
        self.line_output = LineEdit()
        self.line_output.setPlaceholderText(self.tr("选择导出目录..."))
        self.line_output.setReadOnly(True)
        path_row.addWidget(self.line_output)

        btn_browse = PushButton(self.tr("浏览..."), self)
        btn_browse.setIcon(FIF.FOLDER)
        btn_browse.clicked.connect(self._on_browse)
        path_row.addWidget(btn_browse)
        self._settings_layout.addLayout(path_row)

        # 文件名
        fname_label = CaptionLabel(self.tr("文件名（不含扩展名）"))
        self._settings_layout.addWidget(fname_label)

        self.line_filename = LineEdit()
        self.line_filename.setPlaceholderText("untitled")
        self._settings_layout.addWidget(self.line_filename)

        # Kirakara 可在同一个 .krl 格式中切换单注音/双注音。
        # 勾选框右侧带「跳转到设置」按钮：Kirakara 罗马音的转换风格
        # （长音复写/链接、促音链接、大写）统一在设置的「罗马音注音设置」卡配置，
        # 点击后直达该卡并展开。
        self._chk_export_romaji = CheckBox(self.tr("导出罗马音"))
        self._chk_export_romaji.setToolTip(
            self.tr("勾选时输出假名与罗马音双注音；不勾选时仅输出假名注音")
        )
        self._chk_export_romaji.setChecked(True)
        self.btn_romaji_settings = TransparentPushButton(
            FIF.SETTING, self.tr("跳转到设置"), self
        )
        self.btn_romaji_settings.clicked.connect(self._on_jump_to_romaji_settings)
        romaji_row = QHBoxLayout()
        romaji_row.setContentsMargins(0, 0, 0, 0)
        romaji_row.addWidget(self._chk_export_romaji)
        romaji_row.addStretch(1)
        romaji_row.addWidget(self.btn_romaji_settings)
        self._romaji_option_row = QWidget()
        self._romaji_option_row.setLayout(romaji_row)
        self._romaji_option_row.hide()
        self._settings_layout.addWidget(self._romaji_option_row)

        # Nicokara 标签设置按钮（仅 Nicokara 格式显示）
        self.btn_tags = PushButton(self.tr("Nicokara 标签设置..."), self)
        self.btn_tags.setIcon(FIF.TAG)
        self.btn_tags.clicked.connect(self._on_nicokara_tags)
        self.btn_tags.hide()
        self._settings_layout.addWidget(self.btn_tags)

        # 导出字幕分组（Nicokara / Kirakara 格式显示）——原「演唱者过滤」升级：
        # 默认小窗显示分组行（每组一行胶囊卡片），点「修改分组...」弹出大对话框编辑。
        self._singer_group = FluentGroupBox(self.tr("导出字幕分组"))
        singer_group_layout = self._singer_group.contentLayout
        singer_group_layout.setSpacing(8)

        # 分组摘要的纯文本载体（语义口径，供测试与读屏），视觉呈现由下方
        # 分组行承担，故保持隐藏、不进布局。
        self._axis_summary_label = CaptionLabel("", self._singer_group)
        self._axis_summary_label.setWordWrap(True)
        self._axis_summary_label.hide()

        # 分组行容器：每组一行（主分组徽标 + 组名 + 成员色点名单），
        # 行内容在 _refresh_axis_group_summary 里整行重建
        self._axis_chips_widget = QWidget()
        self._axis_chips_widget.setObjectName("axisChips")
        self._axis_chips_widget.setStyleSheet(
            "QWidget#axisChips { background: transparent; }"
        )
        self._axis_chips_layout = QVBoxLayout(self._axis_chips_widget)
        self._axis_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._axis_chips_layout.setSpacing(6)
        singer_group_layout.addWidget(self._axis_chips_widget)

        self._btn_axis_groups = PushButton(self.tr("修改分组..."), self)
        self._btn_axis_groups.setIcon(FIF.EDIT)
        self._btn_axis_groups.setToolTip(
            self.tr(
                "打开导出字幕分组编辑器：按组勾选演唱者（分色）、命名分组、"
                "指定主分组。存在 1 个以上分组时，导出将按组拆分为多个文件"
                "（文件名追加 _分组名）；主分组的文件携带完整标签信息"
            )
        )
        self._btn_axis_groups.clicked.connect(self._on_axis_groups)
        singer_group_layout.addWidget(self._btn_axis_groups)

        self._chk_insert_singer_tags = CheckBox(self.tr("插入【演唱者名】标签"))
        self._chk_insert_singer_tags.setToolTip(
            self.tr("导出时，当演唱者发生变化，在字符前自动插入演唱者名称标签")
        )
        self._chk_insert_singer_tags.hide()

        self._chk_insert_singer_each_line = CheckBox(self.tr("->每行行首都插入演唱者"))
        self._chk_insert_singer_each_line.setToolTip(
            self.tr("每一行开头都插入演唱者名称标签（需先启用「插入【演唱者名】标签」）")
        )
        self._chk_insert_singer_each_line.setEnabled(False)
        self._chk_insert_singer_each_line.hide()
        self._chk_insert_singer_tags.stateChanged.connect(
            lambda state: self._chk_insert_singer_each_line.setEnabled(bool(state))
        )

        # 分色标签设置助手按钮（仅 Nicokara 格式显示，紧接「插入演唱者标签」之后）
        self._btn_emoji_config = PushButton(self.tr("分色标签设置助手..."), self)
        self._btn_emoji_config.setToolTip(
            self.tr("为每位演唱者配置 @Emoji 分色标签，配置后自动写入 Nicokara 标签的自定义字段")
        )
        self._btn_emoji_config.clicked.connect(self._on_emoji_config)
        self._btn_emoji_config.hide()

        self._singer_group.hide()
        self._settings_layout.addWidget(self._singer_group)
        self._settings_layout.addWidget(self._chk_insert_singer_tags)
        self._settings_layout.addWidget(self._chk_insert_singer_each_line)
        self._settings_layout.addWidget(self._btn_emoji_config)
        # widgetResizable 会把内容 widget 撑到视口高度。显式用末尾 stretch
        # 吸收富余空间，避免 LineEdit 等 Preferred/Expanding 控件被纵向拉开。
        self._settings_layout.addStretch(1)

        # 导出按钮。宿主联动入口只在 embedded 模式显示；两个按钮使用相同
        # stretch，始终各占可用宽度的一半。
        self._export_button_row = QHBoxLayout()
        self._export_button_row.setSpacing(12)
        self.btn_export = PrimaryPushButton(self.tr("导出"), self)
        self.btn_export.setIcon(FIF.SHARE)
        self.btn_export.setMinimumHeight(45)
        self.btn_export.clicked.connect(self._on_export)
        self._export_button_row.addWidget(self.btn_export, 1)

        self.btn_export_to_next = None
        if self._embedded:
            self.btn_export_to_next = PrimaryPushButton(
                self.tr("进入下一步"), self
            )
            self.btn_export_to_next.setIcon(FIF.RIGHT_ARROW)
            self.btn_export_to_next.setMinimumHeight(45)
            self.btn_export_to_next.setToolTip(
                self.tr(
                    "把项目送往宿主的下一步；已配置导出字幕分组时，"
                    "宿主按分组拆分多个轴"
                )
            )
            self.btn_export_to_next.clicked.connect(
                self._on_export_to_next
            )
            self._export_button_row.addWidget(self.btn_export_to_next, 1)
        right_layout.addLayout(self._export_button_row)

        content.addWidget(right_card, 1)

        layout.addLayout(content, 1)

        # 所有控件创建完毕后再填充格式列表（_populate_formats 会访问 btn_tags 等控件）
        self._populate_formats()

        # 刷新 QListWidget 和标题标签样式（二者不在 qfluentwidgets 管理中）。
        # 主题信号在 __init__ 中只连接一次；语言切换重建后在这里立即刷新新控件。
        self._update_theme_style()

    def _update_theme_style(self) -> None:
        """主题变化时刷新不受 qfluentwidgets 管理的控件样式。

        - title_label (TitleLabel)：局部变量创建后可能被 GC 移出
          styleSheetManager 的 WeakKeyDictionary，需显式更新颜色。
        - format_list (QListWidget)：纯 Qt 控件，依赖 QPalette 渲染，
          需要显式 QSS 覆盖。
        """
        text = _theme.text_primary.name()
        self.title_label.setStyleSheet(f"color: {text};")

        bg     = _theme.bg_primary.name()
        border = _theme.border_primary.name()
        hover  = _theme.bg_hover.name()
        sel    = _theme.bg_selected.name()
        # 选中行始终用白字（bg_selected 是深蓝色，深浅模式下均与白字对比度最佳）
        self.format_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {bg};
                color: {text};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 8px;
                border-radius: 4px;
            }}
            QListWidget::item:selected {{
                background-color: {sel};
                color: #ffffff;
            }}
            QListWidget::item:hover:!selected {{
                background-color: {hover};
            }}
        """)

        # 主题变化时分组行整行重建（底色/徽标色现取现用，深浅两套外观一致）
        if hasattr(self, "_axis_chips_layout"):
            self._refresh_axis_group_summary()

    @staticmethod
    def _strip_extension_hint(name: str) -> str:
        """去除格式名末尾形如 '(.ext)' 的后缀提示，便于名称比对。

        例如 'LRC (增强型) (.lrc)' → 'LRC (增强型)'
        """
        return re.sub(r"\s*\(\.[^)]+\)$", "", name).strip()

    @classmethod
    def _normalize_format_name(cls, name: str) -> str:
        """把旧配置中的已升级格式名映射到当前名称。"""
        stripped = cls._strip_extension_hint(name)
        if stripped == "春日向注音（带罗马音）":
            return "Kirakara"
        return stripped

    def _tr_format_name(self, name: str) -> str:
        """显式枚举各 format key → 让 .ts 抽取器把源串纳入 ExportInterface
        上下文（变量参数的 self.tr(var) 抓不到）。"""
        if name == "LRC (增强型)":         return self.tr("LRC (增强型)")
        if name == "LRC (逐行)":           return self.tr("LRC (逐行)")
        if name == "LRC (逐字)":           return self.tr("LRC (逐字)")
        if name == "Nicokara (带注音)":    return self.tr("Nicokara (带注音)")
        if name == "RL 编辑模式":           return self.tr("RL 编辑模式")
        if name == "春日向注音":            return self.tr("春日向注音")
        # KRA / TXT / SRT / txt2ass / ASS / Nicokara / Kirakara 都是英文
        return name

    def _populate_formats(self):
        """填充格式列表"""
        # 必须先 clear——changeEvent 重建后会再次调，不清会双份
        self.format_list.clear()
        formats = self._export_service.get_available_formats()
        for fmt in formats:
            # name 是 config.json 里的 key（如 "LRC (增强型)"），不能改写；
            # 列表里只翻译显示文本，UserRole 仍存原 name 供保存/查询用。
            display = f"{self._tr_format_name(fmt['name'])} ({fmt['extension']})"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, fmt["name"])
            self.format_list.addItem(item)
        if self.format_list.count() > 0:
            default_format = self._normalize_format_name(
                AppSettings().get("export.default_format", "")
            )
            default_row = 0
            if default_format:
                for i in range(self.format_list.count()):
                    item = self.format_list.item(i)
                    if item and self._normalize_format_name(
                        item.data(Qt.ItemDataRole.UserRole)
                    ) == default_format:
                        default_row = i
                        break
            self.format_list.setCurrentRow(default_row)
        self.format_list.currentItemChanged.connect(self._on_format_selected)
        # 信号在 setCurrentRow 之后才连接，需手动触发一次以初始化格式专属控件
        self._on_format_selected(self.format_list.currentItem(), None)

    def _get_setting_interface(self):
        """向上查找宿主的设置界面（嵌入/独立窗口均兼容）。"""
        widget = self
        while widget is not None:
            setting_iface = getattr(widget, "settingInterface", None)
            if setting_iface is not None:
                return setting_iface
            widget = widget.parentWidget()
        return getattr(self.window(), "settingInterface", None)

    def _on_jump_to_romaji_settings(self) -> None:
        """跳转到设置页的「罗马音注音设置」卡片并展开。"""
        setting_iface = self._get_setting_interface()
        if setting_iface is None:
            return
        window = setting_iface.window()
        switch = getattr(window, "switchTo", None)
        if callable(switch):
            switch(setting_iface)
        setting_iface.switch_to_tab("auto_check")
        auto_check_iface = getattr(setting_iface, "autoCheckInterface", None)
        if auto_check_iface is not None and hasattr(
            auto_check_iface, "focus_romaji_style_card"
        ):
            auto_check_iface.focus_romaji_style_card()

    def _on_format_selected(self, current, _previous):
        """根据所选格式显示/隐藏格式专用控件。"""
        if current:
            name = current.data(Qt.ItemDataRole.UserRole)
            is_nicokara = "nicokara" in name.lower()
            is_kirakara = name.lower() == "kirakara"
            has_singer_options = is_nicokara or is_kirakara
            self.btn_tags.setVisible(is_nicokara)
            self._romaji_option_row.setVisible(is_kirakara)
            self._singer_group.setVisible(has_singer_options)
            self._chk_insert_singer_tags.setVisible(has_singer_options)
            self._chk_insert_singer_each_line.setVisible(has_singer_options)
            self._btn_emoji_config.setVisible(is_nicokara)
            if is_kirakara:
                self._chk_insert_singer_tags.setText(
                    self.tr("插入【@演唱者名】标签")
                )
                self._chk_insert_singer_tags.setToolTip(
                    self.tr("导出时，当演唱者发生变化，在字符前自动插入【@演唱者名】标签")
                )
                self._chk_insert_singer_each_line.setToolTip(
                    self.tr("每一行开头都插入【@演唱者名】标签（需先启用演唱者标签）")
                )
            else:
                self._chk_insert_singer_tags.setText(
                    self.tr("插入【演唱者名】标签")
                )
                self._chk_insert_singer_tags.setToolTip(
                    self.tr("导出时，当演唱者发生变化，在字符前自动插入演唱者名称标签")
                )
                self._chk_insert_singer_each_line.setToolTip(
                    self.tr("每一行开头都插入演唱者名称标签（需先启用「插入【演唱者名】标签」）")
                )
            if has_singer_options:
                self._refresh_axis_group_summary()
            self._update_settings_geometry()

    def _update_settings_geometry(self) -> None:
        """让动态显隐/增删的设置项立即更新外层滚动区域的内容高度。"""
        self._settings_layout.invalidate()
        self._settings_layout.activate()
        self._settings_widget.updateGeometry()
        self._settings_scroll.updateGeometry()

    def set_project(self, project: Project):
        self._project = project

    def _get_export_offset(self) -> int:
        """从设置中获取导出时间偏移（毫秒）。"""
        settings = AppSettings()
        return settings.get("export.offset_ms", 0)

    def _get_software_compensation(self) -> int:
        """从设置中获取软件导出补偿（毫秒）。"""
        settings = AppSettings()
        return settings.get("export.software_compensation_ms", 0)

    def set_store(self, store):
        """接入 ProjectStore 统一数据中心。"""
        self._store = store
        store.data_changed.connect(self._on_data_changed)

    def _on_data_changed(self, change_type: str):
        """响应 ProjectStore 的数据变更。"""
        try:
            if change_type == "project":
                self._project = self._store.project
                self._sync_default_filename()
                # 切换项目才清除用户上次的浏览选择
                self._sync_default_output_dir(reset_user_choice=True)
                self._refresh_axis_group_summary()
            elif change_type == "audio":
                # 音频变更即刻反映到默认文件名（无需等待"创建项目"）
                self._sync_default_filename()
                self._sync_default_output_dir()
            elif change_type == "singers":
                if self._store and self._store.project:
                    self._project = self._store.project
                self._refresh_axis_group_summary()
            elif change_type == "settings":
                self._sync_default_format()
                self._sync_default_output_dir()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[ExportInterface] _on_data_changed(%s) 失败: %s",
                change_type, e, exc_info=True)
            self._store.error_notify.emit("数据刷新异常", str(e))

    def _sync_default_format(self):
        """将 format_list 的选中项与配置中的 default_format 同步。"""
        default_format = self._normalize_format_name(
            AppSettings().get("export.default_format", "")
        )
        if not default_format:
            return
        for i in range(self.format_list.count()):
            item = self.format_list.item(i)
            if item and self._normalize_format_name(
                item.data(Qt.ItemDataRole.UserRole)
            ) == default_format:
                self.format_list.setCurrentRow(i)
                # 若目标行与当前行相同，setCurrentRow 不会 emit currentItemChanged，
                # 需手动触发以确保格式专属控件（Nicokara 区块等）正确刷新
                self._on_format_selected(item, None)
                return

    def _sync_default_output_dir(self, reset_user_choice: bool = False):
        """根据 store 的导出目录自动预填导出路径（仅影响预填值）。

        预填取值优先级见 ``ProjectStore.export_dir``（已保存项目 > 默认导出
        目录 > 上次加载目录）。用户通过「浏览」主动选过路径后，该选择会被
        保留，自动预填不再覆盖它，直到用户再次「浏览」更换、或切换项目。

        Args:
            reset_user_choice: 为 True 时清除用户上次的浏览选择（仅切换项目时
                传入；切换音频、改设置都不重置）。
        """
        if not self._store:
            return
        if reset_user_choice:
            self._output_user_set = False
        # 用户通过「浏览」指定的导出路径应当保留，不被自动预填覆盖
        if self._output_user_set:
            return
        export_dir = self._store.export_dir
        if export_dir:
            self.line_output.setText(export_dir)

    def _sync_default_filename(self):
        """根据当前 store 的音频 / 项目元数据刷新默认导出文件名。"""
        audio_path = getattr(self._store, "audio_path", None) if self._store else None
        if audio_path:
            default_name = Path(audio_path).stem
        elif self._project and self._project.metadata.title:
            default_name = self._project.metadata.title
        else:
            default_name = ""
        self.line_filename.setText(default_name)

    def _get_used_singers(self) -> list:
        """导出过滤器 / 轴分组的候选演唱者：项目中实际使用且启用的演唱者。

        口径与过滤器勾选框列表一致：行级与 per-char 演唱者都计入，
        未知演唱者（含 "?"）归一到默认演唱者，禁用的演唱者剔除。
        """
        if not self._project:
            return []

        used_singer_ids = set()
        known_singer_ids = {s.id for s in self._project.singers}
        # 查找默认演唱者 ID（用于归一化未知演唱者）
        default_singer_id = None
        for s in self._project.singers:
            if s.is_default:
                default_singer_id = s.id
                break
        if default_singer_id is None and self._project.singers:
            default_singer_id = self._project.singers[0].id

        for sentence in getattr(self._project, "sentences", []) or []:
            # 行级别演唱者
            sentence_singer = getattr(sentence, "singer_id", None)
            if sentence_singer:
                if sentence_singer in known_singer_ids:
                    used_singer_ids.add(sentence_singer)
                elif default_singer_id:
                    # 未知演唱者视为默认演唱者
                    used_singer_ids.add(default_singer_id)
            elif default_singer_id:
                used_singer_ids.add(default_singer_id)
            # per-char 级别演唱者
            for character in getattr(sentence, "characters", []) or []:
                singer_id = getattr(character, "singer_id", None)
                if singer_id:
                    if singer_id in known_singer_ids:
                        used_singer_ids.add(singer_id)
                    elif singer_id in ("?", "未知") and default_singer_id:
                        used_singer_ids.add(default_singer_id)

        return [
            s
            for s in self._project.singers
            if s.id in used_singer_ids and s.enabled
        ]

    def _refresh_axis_group_summary(self):
        """刷新「导出字幕分组」小窗。

        - 隐藏的 ``_axis_summary_label`` 承载纯文本摘要（语义口径）：
          未配置分组显示「未分组」；已配置逐组列出 组名（演唱者），主分组
          带「主·」前缀；未入组的演唱者单独提示；
        - 可视呈现由 :meth:`_render_axis_group_rows` 重建的分组行承担：
          每组一行胶囊卡片（主分组徽标 + 组名 + 成员色点名单）。
        """
        if not hasattr(self, "_axis_summary_label"):
            return

        used_singers = self._get_used_singers()
        used = {s.id: s.name for s in used_singers}
        groups = (
            list(getattr(self._project, "axis_groups", None) or [])
            if self._project
            else []
        )

        if not groups:
            text = self.tr("未分组：导出全部演唱者")
        else:
            parts = []
            for group in groups:
                if group.singer_ids:
                    names = "、".join(
                        used.get(sid, self.tr("未知"))
                        for sid in group.singer_ids
                    )
                else:
                    # 空 = 全部演唱者（过滤器「不勾选则导出全部」口径）
                    names = self.tr("全部")
                prefix = "主·" if group.is_primary else ""
                parts.append(f"{prefix}{group.name}（{names}）")
            text = self.tr("共 {n} 组：{parts}").format(
                n=len(groups), parts=" ｜ ".join(parts)
            )
            assigned = set()
            for group in groups:
                # 空组（= 全部）覆盖所有使用中的演唱者
                assigned.update(group.singer_ids or used.keys())
            unassigned = [name for sid, name in used.items() if sid not in assigned]
            if unassigned:
                text += "\n" + self.tr("未入组（不进入任何轴）：{names}").format(
                    names="、".join(unassigned)
                )
        self._axis_summary_label.setText(text)
        self._render_axis_group_rows(used_singers, groups, used)
        self._update_settings_geometry()

    def _render_axis_group_rows(
        self, used_singers: list, groups: list, used: Dict[str, str]
    ) -> None:
        """重建分组行：每组一行胶囊卡片，未入组提示行紧随其后。

        行的底色、主分组徽标颜色与成员色点都从当前主题现取；主题切换时
        :meth:`_update_theme_style` 会整行重建，深浅两套外观保持一致。
        """
        layout = self._axis_chips_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        if not groups:
            empty = CaptionLabel(self.tr("未分组：导出全部演唱者"))
            empty.setWordWrap(True)
            self._add_axis_row(layout, empty)
            return

        colors = {s.id: getattr(s, "color", "#888888") for s in used_singers}
        for group in groups:
            self._add_axis_row(
                layout, self._make_axis_group_row(group, used, colors)
            )

        # 未入组提示（有分组时才有意义；空组 = 全部，覆盖所有演唱者）
        assigned: set = set()
        for group in groups:
            assigned.update(group.singer_ids or used.keys())
        unassigned = [name for sid, name in used.items() if sid not in assigned]
        if unassigned:
            warn = CaptionLabel(
                self.tr("未入组（不进入任何轴）：{names}").format(
                    names="、".join(unassigned)
                )
            )
            warn.setWordWrap(True)
            accent = _theme.accent_warning.name()
            setCustomStyleSheet(
                warn,
                f"CaptionLabel {{ color: {accent}; }}",
                f"CaptionLabel {{ color: {accent}; }}",
            )
            self._add_axis_row(layout, warn)

    @staticmethod
    def _add_axis_row(layout: QVBoxLayout, widget: QWidget) -> None:
        """把分组行加入布局并清除「未显示过」的隐藏标记。

        顶层创建的 widget 自带 hidden 标记，``addWidget`` 挂进容器后该标记
        的清除依赖延迟事件，在这里的实际布局链中不会生效——布局会把 hidden
        控件从 sizeHint 中剔除，导致卡片高度塌陷裁掉内容。须在挂入布局
        （取得父控件）**之后**置可见：先 setVisible 会把无父控件当顶层
        窗口直接弹出。
        """
        layout.addWidget(widget)
        widget.setVisible(True)

    def _make_axis_group_row(
        self, group, used: Dict[str, str], colors: Dict[str, str]
    ) -> QFrame:
        """构造单组胶囊行：[主徽标] 组名  ●成员A ●成员B（或「全部」）。"""
        row = QFrame()
        row.setObjectName("axisGroupRow")
        # 色值统一取自 ThemeManager：深色沿用灰阶填充做层级，浅色用白底 +
        # 描边（灰底在浅色界面里显脏）
        if _theme.is_dark:
            row_bg = _theme.bg_hover
            row.setStyleSheet(
                f"QFrame#axisGroupRow {{ background: {row_bg.name()};"
                f" border-radius: 6px; }}"
            )
        else:
            row_bg = _theme.bg_primary
            row.setStyleSheet(
                f"QFrame#axisGroupRow {{ background: {row_bg.name()};"
                f" border: 1px solid {_theme.border_primary.name()};"
                f" border-radius: 6px; }}"
            )
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(10, 5, 10, 5)
        row_layout.setSpacing(8)

        if group.is_primary:
            # 原生 QLabel：纯自定义外观（主题色底星标徽标），不走 fluent
            # 注册，避免主题重刷覆盖掉徽标配色（行本身随分组行重建刷新）
            badge = QLabel("★", row)
            badge.setFixedSize(18, 18)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setToolTip(self.tr("主分组"))
            badge.setStyleSheet(
                f"background: {themeColor().name()}; color: #FFFFFF;"
                " border-radius: 4px; font-size: 11px;"
            )
            row_layout.addWidget(badge)

        row_layout.addWidget(StrongBodyLabel(group.name, row))

        members = CaptionLabel(row)
        # 组内未勾选 = 全部演唱者（过滤器口径）：外侧把所有人真实列出，
        # 用户不必点开编辑器就能看清这一轴包含谁
        member_ids = group.singer_ids or list(used.keys())
        chips = []
        for sid in member_ids:
            name = html.escape(used.get(sid, self.tr("未知")))
            # 名字内部的不换行空格：保证断行只发生在成员之间，
            # 「●名字」永不被拆开
            name = name.replace(" ", "&nbsp;")
            # 色点与对话框勾选框同口径：原始演唱者色对行底色做对比度
            # 校正（保留色相），否则浅色主题下亮色（黄等）几乎不可读
            raw = QColor(colors.get(sid, "#888888"))
            dot_color = _theme.ensure_contrast(raw, row_bg).name()
            chips.append(f'<span style="color:{dot_color};">●</span>{name}')
        # 普通空格连接 = 成员之间的可断行点；配合下方 wordWrap，
        # 演唱者较多时成员名单自动折行而不是被裁掉
        members.setText(" ".join(chips))
        members.setWordWrap(True)
        row_layout.addWidget(members, 1)
        return row

    def _get_singer_map(self) -> Dict[str, str]:
        """获取 singer_id → 显示名 的映射"""
        if not self._project:
            return {}
        return {s.id: s.name for s in self._project.singers}

    # @Emoji / @EmojiN= 行（触发词 = 等号后第一个逗号前的字段，如 【演唱者名】）
    _EMOJI_LINE_RE = re.compile(r"^@Emoji\d*=", re.IGNORECASE)

    @classmethod
    def _emoji_trigger(cls, line: str) -> Optional[str]:
        """解析 @Emoji 行的触发词；非 @Emoji 行返回 None。"""
        text = line.strip()
        if not cls._EMOJI_LINE_RE.match(text):
            return None
        rest = text[text.index("=") + 1:]
        comma = rest.find(",")
        return (rest if comma < 0 else rest[:comma]).strip()

    def _build_axis_tag_data(self, group: AxisGroup, is_primary: bool) -> dict:
        """按轴分组构造 Nicokara 标签快照（tag_data）。

        - **所有组**：custom 中的 @Emoji 行按「本组实际使用的演唱者」解析
          触发词——只保留触发词（【演唱者名】或裸名）能对应到本组演唱者
          的行，其余 @Emoji 行剔除（其他轴的颜色标签对本轴无效）。
          **组内未勾选演唱者 = 全部演唱者**（保留全部能对上号的 @Emoji）。
        - **主分组**：携带完整标签信息（@Title/@Artist/@Album/@TaggingBy +
          非 @Emoji 的 custom 行原样保留）。
        - **非主分组**：剔除信息性标签（title/artist/album/tagging_by）与
          非 @Emoji 的 custom 行，只留本组 @Emoji；计时字段
          （offset/head_offset/silence_ms）保持原样——缺了会破坏时间轴。
        """
        tags = deepcopy(AppSettings().get("nicokara_tags") or {})
        if not self._project:
            return tags

        name_by_id = {s.id: s.name for s in self._project.singers}
        # 空 = 全部：触发词集合取全体演唱者
        group_singer_ids = group.singer_ids or list(name_by_id.keys())
        triggers = set()
        for sid in group_singer_ids:
            singer_name = name_by_id.get(sid)
            if singer_name:
                triggers.add(f"【{singer_name}】")
                triggers.add(singer_name)

        kept: list = []
        for line in tags.get("custom", []) or []:
            if not line:
                continue
            trigger = self._emoji_trigger(line)
            if trigger is not None:
                if trigger in triggers:
                    kept.append(line)
            elif is_primary:
                kept.append(line)
        tags["custom"] = kept

        if not is_primary:
            for key in ("title", "artist", "album", "tagging_by"):
                tags.pop(key, None)
        return tags

    def _on_browse(self):
        # 优先用导出专用目录（默认导出目录 > 项目/音频/歌词），回退到 last_export_dir
        default_dir = ""
        if self._store:
            default_dir = self._store.export_dir
        if not default_dir:
            default_dir = AppSettings().get("export.last_export_dir", "")
        path = QFileDialog.getExistingDirectory(self, self.tr("选择导出目录"), default_dir)
        if path:
            self.line_output.setText(path)
            self._output_user_set = True

    def _on_nicokara_tags(self):
        """打开 Nicokara 标签设置对话框"""
        settings = AppSettings()
        tag_data = settings.get("nicokara_tags") or {}
        dialog = NicokaraTagsDialog(tag_data, self)
        if dialog.exec() == NicokaraTagsDialog.DialogCode.Accepted:
            new_tags = dialog.get_tag_data()
            settings.set("nicokara_tags", new_tags)
            settings.save()
            if self._store:
                self._store.mark_dirty()

    def _on_emoji_config(self):
        """打开分色标签设置助手对话框。

        演唱者列表取项目中启用的全部演唱者（原「以过滤器勾选为准」随
        演唱者过滤升级为导出字幕分组而移除）。已有 @Emoji 标签优先读取，
        无匹配项回退到默认参数。配置确认后自动写入 nicokara_tags.custom
        并记忆首行参数。
        """
        from strange_uta_game.frontend.export.emoji_tag_dialog import (
            EmojiTagDialog,
            split_params,
        )

        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        singer_list: list[tuple[str, str]] = []
        for singer in self._project.singers:
            if not singer.enabled:
                continue
            singer_list.append((singer.id, singer.name))

        if not singer_list:
            InfoBar.warning(
                title=self.tr("无演唱者"),
                content=self.tr("项目中没有可用的演唱者"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        # 解析已有的 @Emoji 标签，按触发字符建立映射。
        # 只匹配 @Emoji= (主标签)，排除 @EmojiN= (叠加层变体)。
        settings = AppSettings()
        tag_data = settings.get("nicokara_tags") or {}
        custom = tag_data.get("custom", [])
        existing_tags: dict[str, tuple[str, str, str]] = {}
        for line in custom:
            line = line.strip()
            if line.lower().startswith("@emoji="):
                rest = line[len("@Emoji="):]
                comma_idx = rest.find(",")
                if comma_idx > 0:
                    trigger = rest[:comma_idx]
                    params = rest[comma_idx + 1:]
                    existing_tags[trigger] = split_params(params)

        dialog = EmojiTagDialog(singer_list, self, existing_tags=existing_tags)
        if dialog.exec() == EmojiTagDialog.DialogCode.Accepted:  # apply_emoji_tags_to_settings 在 _on_accept 内部调用
            if self._store:
                self._store.mark_dirty()

    def _open_axis_group_dialog(self) -> Optional[list]:
        """打开导出字幕分组对话框；确认返回分组列表，取消/校验失败返回 None。

        初始状态：项目已保存过分组则恢复编辑；否则给出一张空分组卡片。
        """
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return None

        used_singers = self._get_used_singers()
        if not used_singers:
            InfoBar.warning(
                title=self.tr("无演唱者"),
                content=self.tr("项目中没有可用的演唱者"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return None

        from strange_uta_game.frontend.export.axis_group_dialog import (
            AxisGroupDialog,
        )

        initial = list(getattr(self._project, "axis_groups", None) or [])
        if not initial:
            initial = [AxisGroup(name="", singer_ids=[])]
        dialog = AxisGroupDialog(
            [(s.id, s.name, s.color) for s in used_singers],
            initial_groups=initial,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.get_axis_groups()

    def _on_axis_groups(self):
        """「修改分组...」：编辑并写回项目的导出字幕分组，刷新小窗摘要。"""
        groups = self._open_axis_group_dialog()
        if groups is None:
            return
        self._project.set_axis_groups(groups)
        store = getattr(self, "_store", None)
        if store is not None:
            store.mark_dirty()
        self._refresh_axis_group_summary()

    def _on_export_to_next(self):
        """嵌入式「进入下一步」：直接发信号。

        分组编辑统一在导出页「导出字幕分组」小窗完成；本方法不再弹窗、
        也不改写分组——宿主随后的 ``export_to_next_payload()`` 读取当前
        ``project.axis_groups``（空 = 单轴）。
        """
        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        self.export_to_next_requested.emit()

    def _on_export(self):
        if self._exporting:
            # 导出进行中，「导出」按钮已转为取消入口
            self._on_export_cancel_requested()
            return

        if not self._project:
            InfoBar.warning(
                title=self.tr("无项目"),
                content=self.tr("请先创建或打开项目"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        selected = self.format_list.currentItem()
        if not selected:
            InfoBar.warning(
                title=self.tr("未选择格式"),
                content=self.tr("请选择导出格式"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        output_dir = self.line_output.text()
        if not output_dir:
            # 弹出文件选择
            default_dir = ""
            if self._store:
                default_dir = self._store.export_dir
            if not default_dir:
                default_dir = AppSettings().get("export.last_export_dir", "")
            output_dir = QFileDialog.getExistingDirectory(self, self.tr("选择导出目录"), default_dir)
            if not output_dir:
                return
            self.line_output.setText(output_dir)

        # 导出前提示：仍有未补的"导唱待办"标记时让用户确认
        needs_guide_marks: list[tuple[int, int]] = [
            (line_idx, char_idx)
            for line_idx, s in enumerate(self._project.sentences)
            for char_idx, c in enumerate(s.characters)
            if c.needs_guide
        ]
        if needs_guide_marks:
            preview_lines = [
                self.tr("第 {line} 行 第 {char} 字").format(line=l + 1, char=c + 1)
                for l, c in needs_guide_marks[:10]
            ]
            extra = (
                self.tr("\n...另 {n} 处").format(n=len(needs_guide_marks) - 10)
                if len(needs_guide_marks) > 10
                else ""
            )
            if not message_question(
                self,
                self.tr("仍有导唱待办未处理"),
                self.tr("还剩 {n} 个标记点未添加导唱符。").format(n=len(needs_guide_marks))
                + "\n\n"
                + "\n".join(preview_lines)
                + extra,
                yes_text=self.tr("继续导出"),
                no_text=self.tr("取消"),
            ):
                return

        # 导出前验证
        warnings = self._export_service.validate_before_export(self._project)
        if warnings:
            InfoBar.warning(
                title=self.tr("导出提醒"),
                content="\n".join(warnings[:3]),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        # 校验 rubyPart 数量与 checkCount 是否匹配
        ruby_mismatches = self._export_service.validate_ruby_parts(self._project)
        if ruby_mismatches:
            detail = self._export_service.get_ruby_mismatch_detail(self._project)
            dialog = RubyMismatchDialog(detail, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            action = dialog.get_action()
            if action in ("char", "mora"):
                # 均分会改写字符的 ruby.parts：先快照再应用，登记为可撤销命令
                before_sentences = deepcopy(self._project.sentences)
                self._export_service.apply_ruby_parts_split(self._project, action)
                self._push_sentences_undo(
                    before_sentences,
                    self.tr("注音按{mode}均分").format(
                        mode=self.tr("字符") if action == "char" else "mora"
                    ),
                )
                if self._store:
                    self._store.mark_dirty()
                    self._store.notify("rubies")
            elif action == "ignore":
                pass  # 忽略不匹配，继续导出
            elif action == "cancel":
                return

        name = selected.data(Qt.ItemDataRole.UserRole)
        romaji_options = RomajiOptions.from_mapping(
            AppSettings().get("auto_check", {})
        )
        # 获取扩展名
        formats = self._export_service.get_available_formats()
        ext = ""
        for fmt in formats:
            if fmt["name"] == name:
                ext = fmt["extension"]
                break

        requested_base_name = (
            self.line_filename.text().strip()
            or self._project.metadata.title
            or "untitled"
        )
        base_name = sanitize_export_basename(requested_base_name)
        if base_name != requested_base_name:
            self.line_filename.setText(base_name)

        # 轴分组拆分导出：仅对支持演唱者过滤的格式（Nicokara / Kirakara）
        # 生效。存在 1 个以上分组时按组导出多个文件，文件名追加「_分组名」；
        # 单个分组 = 按该组过滤的正常导出（不加后缀）。
        name_lower = name.lower()
        has_singer_options = (
            "nicokara" in name_lower or name_lower == "kirakara"
        )
        axis_groups = (
            list(getattr(self._project, "axis_groups", None) or [])
            if has_singer_options
            else []
        )

        if axis_groups:
            primary = self._project.primary_axis_group()
            multi = len(axis_groups) > 1
            export_jobs = []
            for idx, group in enumerate(axis_groups):
                gname = sanitize_export_basename(group.name or f"轴{idx + 1}")
                filename = base_name + (f"_{gname}" if multi else "") + ext
                export_jobs.append((filename, group, group is primary))

            # 撞名校验：不同分组名 sanitize 后可能得到相同文件名（如 "A/B"
            # 与 "A_B"），后导出的会静默覆盖先导出的 —— 撞名直接中止提示。
            seen_names: list[str] = []
            for filename, _group, _is_primary in export_jobs:
                if filename in seen_names:
                    InfoBar.error(
                        title=self.tr("导出文件名冲突"),
                        content=self.tr(
                            "多个分组的导出文件名相同：{filename}\n"
                            "请修改轴分组名称后重试"
                        ).format(filename=filename),
                        orient=Qt.Orientation.Horizontal,
                        isClosable=True,
                        position=InfoBarPosition.TOP,
                        duration=8000,
                        parent=self,
                    )
                    return
                seen_names.append(filename)

            # 覆盖确认：合并为一次弹出——先收集全部已存在的目标文件再
            # 统一询问，取消则整体中止（逐一弹窗会在多组时连弹多次）。
            existing_files = [
                filename
                for filename, _group, _is_primary in export_jobs
                if (Path(output_dir) / filename).exists()
            ]
            if existing_files:
                preview = "\n".join(existing_files[:10])
                if len(existing_files) > 10:
                    preview += "\n" + self.tr("...另 {n} 个文件").format(
                        n=len(existing_files) - 10
                    )
                if not message_question(
                    self,
                    self.tr("文件已存在"),
                    self.tr("以下文件已存在：\n{files}").format(files=preview)
                    + "\n\n"
                    + self.tr("是否覆盖这些文件？"),
                    yes_text=self.tr("覆盖"),
                    no_text=self.tr("取消"),
                ):
                    return
        else:
            export_jobs = [(base_name + ext, None, False)]
            # 检查文件是否已存在
            filepath = str(Path(output_dir) / (base_name + ext))
            if Path(filepath).exists():
                if not message_question(
                    self,
                    self.tr("文件已存在"),
                    self.tr("文件已存在：\n{filename}").format(filename=base_name + ext)
                    + "\n\n"
                    + self.tr("是否覆盖该文件？"),
                    yes_text=self.tr("覆盖"),
                    no_text=self.tr("取消"),
                ):
                    return

        # 统一后台执行：UI 线程只做预检查与任务编排，逐任务写临时文件成功后
        # os.replace 原子替换（H3）。
        self._start_export_jobs(name, ext, export_jobs, romaji_options=romaji_options)

    def _push_sentences_undo(self, before_sentences, description: str) -> None:
        """把注音均分等 sentences 结构变更登记为可撤销命令。

        经主窗口打轴服务的共享 CommandManager 入撤销栈；找不到（独立宿主
        等场景）时静默跳过，保持原直改行为。
        """
        if not self._project:
            return
        timing_service = getattr(self.window(), "_timing_service", None)
        command_manager = (
            getattr(timing_service, "command_manager", None) if timing_service else None
        )
        if command_manager is None:
            return

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

    def _start_export_jobs(
        self,
        format_name: str,
        ext: str,
        export_jobs: list,
        romaji_options=None,
    ) -> None:
        """后台线程执行导出任务（H3：不阻塞 UI + 临时文件原子落盘 + 取消）。

        export_jobs: [(filename, AxisGroup | None, is_primary)]。导出中再次
        点击「导出」按钮转为取消请求（任务间生效）；逐任务先写同目录临时
        文件，成功后 os.replace 覆盖目标，失败清理临时文件并汇总成功 N / 失败 M。
        romaji_options: 上游罗马音转换选项，None 时由导出服务按默认行为处理。
        """
        from PyQt6.QtCore import QThread
        from qfluentwidgets import StateToolTip

        from strange_uta_game.frontend.workers import ExportTaskWorker

        output_dir = self.line_output.text()
        singer_map = self._get_singer_map()
        jobs = []
        for filename, group, is_primary in export_jobs:
            jobs.append({
                "file_path": str(Path(output_dir) / filename),
                "format_name": format_name,
                "ext": ext,
                "kwargs": {
                    "offset_ms": self._get_export_offset(),
                    # group=None = 未配置分组，导出全部演唱者、不按组过滤标签
                    "singer_ids": (
                        set(group.singer_ids) or None
                    ) if group is not None else None,
                    "insert_singer_tags": self._chk_insert_singer_tags.isChecked(),
                    "insert_singer_each_line": self._chk_insert_singer_each_line.isChecked(),
                    "singer_map": singer_map,
                    "export_romaji": self._chk_export_romaji.isChecked(),
                    "romaji_options": romaji_options,
                    "software_compensation_ms": self._get_software_compensation(),
                    "tag_data": (
                        self._build_axis_tag_data(group, is_primary)
                        if group is not None
                        else None
                    ),
                },
            })

        # 副本由 ExportTaskWorker 在导出线程内制作（大项目 deepcopy 不卡
        # UI 线程），导出期间 UI 继续编辑不影响导出内容

        green = _theme.status_complete.name()
        state_tooltip = StateToolTip(self.tr("正在导出"), self.tr("准备中..."), self)
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

        worker = ExportTaskWorker(self._project, self._export_service, jobs)
        # 不以页面为父：导出进行中页面被销毁时，父子关系会导致
        # "QThread: Destroyed while thread is still running"；引用由
        # _export_thread/_cleanup 管理，thread.finished 链接 deleteLater
        thread = QThread()
        worker.moveToThread(thread)
        self._exporting = True
        self._export_worker = worker
        self._export_thread = thread
        # 导出按钮转为取消入口
        btn = getattr(self, "btn_export", None)
        if btn is not None:
            btn.setText(self.tr("取消导出"))

        def _on_progress(phase: str, current: int, total: int) -> None:
            state_tooltip.setContent(f"{phase} {current}/{total}")

        def _cleanup() -> None:
            # 无父 QThread 的 C++ 对象由 Python 引用保活：必须先 quit+wait
            # 让线程真正停止，再清引用（worker 的销毁由已连接的
            # thread.finished → worker.deleteLater 链在线程收尾时完成，
            # 这里不再重复 deleteLater——线程已停止时会因对象已被删除
            # 抛 RuntimeError）。若先清引用，最后一个 Python 引用归零会
            # 立即析构仍在运行的 QThread → qFatal（"QThread: Destroyed
            # while thread is still running"）→ 导出文件已落盘但进程闪退。
            thread = self._export_thread
            if thread is not None:
                thread.quit()
                thread.wait()
                self._export_thread = None
            self._export_worker = None
            self._exporting = False
            btn = getattr(self, "btn_export", None)
            if btn is not None:
                btn.setText(self.tr("导出"))

        def _on_finished(result: dict) -> None:
            state_tooltip.setState(True)
            _cleanup()
            exported = result.get("exported", [])
            failed = result.get("failed", [])
            cancelled = result.get("cancelled", False)

            if exported:
                # 将本次使用的格式持久化为默认导出格式
                settings = AppSettings()
                settings.set("export.default_format", format_name)
                settings.save()
                InfoBar.success(
                    title=self.tr("导出成功"),
                    content="\n".join(exported),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )
            if failed:
                detail = "\n".join(
                    f"{Path(path).name}: {msg or self.tr('未知错误')}"
                    for path, msg in failed
                )
                title = self.tr("导出失败（成功 {n} / 失败 {m}）").format(
                    n=len(exported), m=len(failed)
                )
                InfoBar.error(
                    title=title,
                    content=detail,
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self,
                )
            elif cancelled and not exported:
                InfoBar.info(
                    title=self.tr("已取消"),
                    content=self.tr("导出已取消，未写入任何文件"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )

        def _on_error(err: str) -> None:
            state_tooltip.setState(True)
            _cleanup()
            InfoBar.error(
                title=self.tr("导出失败"),
                content=err,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=8000,
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

        # 退出收尾（仅挂接一次）：导出进行中退出应用时取消并等待线程
        # 结束——无父 QThread 在解释器关闭阶段被销毁时若仍在运行会触发
        # qFatal（与 task_runner 的 aboutToQuit 收尾同型）
        if not getattr(self, "_export_quit_hooked", False):
            self._export_quit_hooked = True
            from PyQt6.QtCore import QCoreApplication

            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._shutdown_export_on_quit)

        thread.start()

    def _shutdown_export_on_quit(self) -> None:
        """应用退出收尾：取消并等待导出线程结束（仅退出路径允许 wait）。"""
        worker = getattr(self, "_export_worker", None)
        if worker is not None:
            worker.request_cancel()
        thread = getattr(self, "_export_thread", None)
        if thread is not None:
            thread.quit()
            thread.wait(3000)

    def _on_export_cancel_requested(self) -> None:
        """请求取消当前导出（在任务边界生效，正在写盘的当前任务会完成）。"""
        worker = getattr(self, "_export_worker", None)
        if worker is not None:
            worker.request_cancel()
