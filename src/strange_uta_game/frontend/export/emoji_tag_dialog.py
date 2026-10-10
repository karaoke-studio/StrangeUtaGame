"""分色标签设置助手对话框。

按演唱者逐行配置 @Emoji 标签参数，应用后将结果写入
AppSettings["nicokara_tags"]["custom"]，并记忆首行参数供下次预填。

@Emoji 格式（SHINTA NicokaraMaker3 规格）：
  @Emoji=<用于替换为图片的字符>,<擦除前图片>,<擦除后图片（可省略）>,<选项...>

选项：
  Zoom=n%     ：按字幕尺寸缩放图片，保持宽高比（10%~500%，默认 100%）
  Fix         ：保持原图尺寸
  NoDecor     ：不给表情符号添加文字装饰
  ForceWipeDecor：前后图片相同时也强制擦除文字装饰
  MarginLeft=n, MarginRight=n, MarginBottom=n：留白（像素，允许负值）

典型用途（无图标分色，仅用透明 1x1 占位）：
  @Emoji=【演唱者名】,透明画像1x1.png,,Zoom=1,NoDecor,MarginRight=-170
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# AppSettings 延迟导入（避免循环 / Qt 未初始化时失败），
# 同时在模块级声明以便 unittest.mock.patch 可以 patch。
try:
    from strange_uta_game.frontend.settings.settings_interface import AppSettings
except Exception:  # pragma: no cover
    AppSettings = None  # type: ignore[assignment,misc]

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from strange_uta_game.frontend.font_utils import ui_font
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SubtitleLabel,
    ToolButton,
    TransparentToolButton,
)

from strange_uta_game.frontend.window_sizing import fit_min_size, fit_to_screen

# 匹配 @Emoji 或 @EmojiN= 开头的行（大小写不敏感）
_EMOJI_TAG_RE = re.compile(r"^@Emoji\d*=", re.IGNORECASE)

DEFAULT_PARAMS = "透明画像1x1.png,,Zoom=1,NoDecor,MarginRight=-170"


def split_params(params_str: str) -> Tuple[str, str, str]:
    """拆分参数字符串为 (前画像, 后画像, 选项)。

    Args:
        params_str: 逗号分隔的参数串，格式为 "前画像,后画像,选项..."
                    例如 "img.png,,Zoom=1,NoDecor"

    Returns:
        (前画像, 后画像, 选项) 三元组，缺失字段返回空串。
    """
    parts = params_str.split(",")
    front_img = parts[0] if len(parts) > 0 else ""
    back_img = parts[1] if len(parts) > 1 else ""
    options = ",".join(parts[2:]) if len(parts) > 2 else ""
    return front_img, back_img, options


def parse_option_str(options_str: str) -> Dict[str, Any]:
    """解析选项字符串为各参数字典。

    Args:
        options_str: 如 "Zoom=1,NoDecor,MarginRight=-170"

    Returns:
        {"zoom_mode": "Zoom"|"Fix"|None, "zoom_value": int,
         "nodecor": bool, "forcewipedecor": bool,
         "margin_left": int, "margin_right": int, "margin_bottom": int}
    """
    result: Dict[str, Any] = {
        "zoom_mode": None,
        "zoom_value": 100,
        "nodecor": False,
        "forcewipedecor": False,
        "margin_left": 0,
        "margin_right": 0,
        "margin_bottom": 0,
    }
    if not options_str.strip():
        return result

    for part in options_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
            key_lower = key.strip().lower()
            val = val.strip()
            if key_lower == "zoom":
                result["zoom_mode"] = "Zoom"
                val_clean = val.rstrip("%")
                try:
                    result["zoom_value"] = int(val_clean)
                except ValueError:
                    pass
            elif key_lower == "marginleft":
                try:
                    result["margin_left"] = int(val)
                except ValueError:
                    pass
            elif key_lower == "marginright":
                try:
                    result["margin_right"] = int(val)
                except ValueError:
                    pass
            elif key_lower == "marginbottom":
                try:
                    result["margin_bottom"] = int(val)
                except ValueError:
                    pass
        else:
            kw = part.lower()
            if kw == "fix":
                result["zoom_mode"] = "Fix"
            elif kw == "nodecor":
                result["nodecor"] = True
            elif kw == "forcewipedecor":
                result["forcewipedecor"] = True
    return result


def build_option_str(opts: Dict[str, Any]) -> str:
    """从各参数字典构建选项字符串。

    Args:
        opts: parse_option_str 返回格式的字典。

    Returns:
        选项字符串，如 "Zoom=1,NoDecor,MarginRight=-170"
    """
    parts: List[str] = []

    zoom_mode = opts.get("zoom_mode")
    if zoom_mode == "Zoom":
        parts.append(f"Zoom={opts.get('zoom_value', 100)}")
    elif zoom_mode == "Fix":
        parts.append("Fix")

    if opts.get("nodecor"):
        parts.append("NoDecor")
    if opts.get("forcewipedecor"):
        parts.append("ForceWipeDecor")

    for key, label in [("margin_left", "MarginLeft"),
                       ("margin_right", "MarginRight"),
                       ("margin_bottom", "MarginBottom")]:
        val = opts.get(key, 0)
        if val != 0:
            parts.append(f"{label}={val}")

    return ",".join(parts)


def join_params(front_img: str, back_img: str, options: str) -> str:
    """将分拆的三个字段合并为参数字符串。

    Args:
        front_img: 前画像路径（可空）
        back_img:  后画像路径（可空）
        options:   逗号分隔的选项（可空）

    Returns:
        逗号连接的参数字符串，如 "img.png,,Zoom=1,NoDecor"
    """
    parts = [front_img, back_img]
    if options:
        parts.append(options)
    return ",".join(parts)


def strip_emoji_tags(custom: List[str]) -> List[str]:
    """从 custom 列表中删除所有 @Emoji 行，返回新列表。"""
    return [line for line in custom if not _EMOJI_TAG_RE.match(line.strip())]


def build_emoji_tag(trigger: str, params: str) -> str:
    """构建一行 @Emoji 标签字符串。

    Args:
        trigger: 触发字符，如 「【太郎】」
        params:  逗号分隔的参数部分，如 「透明画像1x1.png,,NoDecor,MarginRight=-170」

    Returns:
        完整的标签行，如 "@Emoji=【太郎】,透明画像1x1.png,,NoDecor,MarginRight=-170"
    """
    return f"@Emoji={trigger},{params}"


def apply_emoji_tags_to_settings(
    singer_params: List[Tuple[str, str, str]],
) -> None:
    """将 @Emoji 配置写入 AppSettings["nicokara_tags"]["custom"]，
    并记忆首行参数到 AppSettings["nicokara_emoji_default"]。

    旧的 @Emoji 行会被全部删除，再写入新的行。
    其他 custom 行（非 @Emoji）保持不变。

    Args:
        singer_params: [(singer_name, trigger, params), ...]
            singer_name: 演唱者显示名（仅用于注释，不写入文件）
            trigger:     触发字符，如 "【太郎】"
            params:      @Emoji= 后第二个字段起的内容，如 "img.png,,NoDecor,MarginRight=-170"
    """
    if AppSettings is None:
        return  # pragma: no cover
    settings = AppSettings()
    tags: dict = settings.get("nicokara_tags") or {}
    old_custom: List[str] = tags.get("custom", [])

    # 清除旧 @Emoji 行，保留其余 custom
    new_custom = strip_emoji_tags(old_custom)

    # 追加新 @Emoji 行
    for _, trigger, params in singer_params:
        if trigger.strip():
            new_custom.append(build_emoji_tag(trigger.strip(), params.strip()))

    tags["custom"] = new_custom
    settings.set("nicokara_tags", tags)

    # 记忆首行参数（不含触发字符，以便跨项目复用图像/选项）
    if singer_params:
        first_params = singer_params[0][2].strip()
        settings.set("nicokara_emoji_default", first_params)

    settings.save()


class EmojiTagDialog(QDialog):
    """分色标签设置助手弹窗

    按演唱者逐行配置 @Emoji 标签。每位演唱者一个分组卡片，包含：
    - 触发字符（默认 =【演唱者名】，必须填写）
    - 前画像（擦除前图片，可留空）
    - 後画像（擦除后图片，可省略；留空则与前画像相同）
    - 缩放模式：未指定（默认 100%）/ Zoom（指定比例）/ Fix（保持原尺寸）
    - NoDecor / ForceWipeDecor 复选框
    - 左／右／下 余白（像素，可负值）

    「确定」后调用 apply_emoji_tags_to_settings 写入配置。
    """

    def __init__(
        self,
        singers: List[Tuple[str, str]],  # [(singer_id, singer_name), ...]
        parent: Optional[QWidget] = None,
        existing_tags: Optional[Dict[str, Tuple[str, str, str]]] = None,
    ):
        """分色标签设置助手。

        Args:
            singers: 演唱者列表 [(singer_id, singer_name), ...]
            parent: 父窗口
            existing_tags: 已有的 @Emoji 标签映射 {trigger: (front, back, options)}
                           打开对话框时优先读取，无匹配项则回退到默认参数。
        """
        super().__init__(parent)
        self.setWindowTitle(self.tr("分色标签设置助手"))
        # 加宽以容纳浏览/删除按钮；不超过屏幕可用区域
        fit_min_size(self, 1040, 320)

        self._singers = singers
        self._existing_tags: Dict[str, Tuple[str, str, str]] = existing_tags or {}
        self._rows: List[Dict[str, Any]] = []

        self._init_ui()
        self._populate_rows()

    def _init_ui(self):
        screen = self.parent().screen() if self.parent() else QApplication.primaryScreen()
        if screen:
            self.setMaximumHeight(int(screen.availableGeometry().height() * 0.85))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 16)
        layout.setSpacing(12)

        title = SubtitleLabel(self.tr("分色标签设置助手"))
        layout.addWidget(title)

        desc = CaptionLabel(self.tr(
            "为每位演唱者配置 @Emoji 标签。触发字符与正文中的【演唱者名】对应。\n"
            "後画像留空时，表示与前画像相同（无擦除效果）。\n"
            "缩放默认 100%（与字幕等大）；Zoom 可指定 10%~500% 比例；Fix 保持原图尺寸。"
        ))
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 添加行按钮（手动新增演唱者行）
        add_row = QHBoxLayout()
        btn_add = PushButton(self.tr("添加行"), self)
        btn_add.setIcon(FIF.ADD)
        btn_add.clicked.connect(self._on_add_row)
        add_row.addWidget(btn_add)
        add_row.addStretch()
        layout.addLayout(add_row)

        # 滚动区容纳演唱者卡片列表
        self._row_widget = QWidget()
        self._row_layout = QVBoxLayout(self._row_widget)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setSpacing(8)
        # 末尾留一个伸缩项，卡片始终插入其前
        self._row_layout.addStretch()

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._row_widget)
        layout.addWidget(scroll, stretch=1)
        self._scroll = scroll

        # 确定/取消
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = PrimaryPushButton(self.tr("应用并保存"), self)
        btn_ok.clicked.connect(self._on_accept)
        btn_copy = PushButton(self.tr("将首行参数复制到其他行"), self)
        btn_copy.clicked.connect(self._copy_first_row_params)
        btn_cancel = PushButton(self.tr("取消"), self)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_copy)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    def _get_default_params(self) -> str:
        """读取上次记忆的参数，无则用内置默认值。"""
        if AppSettings is None:
            return DEFAULT_PARAMS  # pragma: no cover
        try:
            saved = AppSettings().get("nicokara_emoji_default", "")
            return saved if saved else DEFAULT_PARAMS
        except Exception:
            return DEFAULT_PARAMS

    def _make_option_controls(self, default_opts: Dict[str, Any]):
        """创建一行选项控件，返回各控件字典。"""
        ctrls: Dict[str, Any] = {}

        ctrls["zoom_mode"] = ComboBox()
        # "未指定" 是显示标签；同时是 _on_accept 里 "zoom_mode_text != '未指定'" 的
        # 判断常量。tr 后内部判断也得用 tr 版，否则切语言后判定恒为不等。
        # 为避免该耦合，这里保留中文 sentinel；UI 上的"未指定"暂不本地化。
        ctrls["zoom_mode"].addItems(["未指定", "Zoom", "Fix"])
        ctrls["zoom_mode"].setMinimumWidth(96)
        if default_opts["zoom_mode"] == "Fix":
            ctrls["zoom_mode"].setCurrentText("Fix")
        elif default_opts["zoom_mode"] == "Zoom":
            ctrls["zoom_mode"].setCurrentText("Zoom")

        ctrls["zoom_value"] = LineEdit()
        ctrls["zoom_value"].setFont(ui_font(10))
        ctrls["zoom_value"].setText(str(default_opts["zoom_value"]))
        ctrls["zoom_value"].setMinimumWidth(54)
        ctrls["zoom_value"].setPlaceholderText("100")
        ctrls["zoom_value"].setEnabled(default_opts["zoom_mode"] == "Zoom")

        ctrls["zoom_mode"].currentTextChanged.connect(
            lambda text: ctrls["zoom_value"].setEnabled(text == "Zoom")
        )

        ctrls["zoom_pct"] = BodyLabel("%")

        ctrls["nodecor"] = CheckBox("NoDecor")
        ctrls["nodecor"].setChecked(default_opts["nodecor"])

        ctrls["forcewipedecor"] = CheckBox("ForceWipeDecor")
        ctrls["forcewipedecor"].setChecked(default_opts["forcewipedecor"])

        ctrls["margin_lbl"] = BodyLabel(self.tr("余白"))

        ctrls["margin_left_lbl"] = BodyLabel("L")

        ctrls["margin_left"] = LineEdit()
        ctrls["margin_left"].setFont(ui_font(10))
        ctrls["margin_left"].setText(str(default_opts["margin_left"]))
        ctrls["margin_left"].setMinimumWidth(68)
        ctrls["margin_left"].setPlaceholderText("0")
        ctrls["margin_left"].setToolTip(self.tr("MarginLeft：图片左侧留白（像素，允许负值）"))

        ctrls["margin_right_lbl"] = BodyLabel("R")

        ctrls["margin_right"] = LineEdit()
        ctrls["margin_right"].setFont(ui_font(10))
        ctrls["margin_right"].setText(str(default_opts["margin_right"]))
        ctrls["margin_right"].setMinimumWidth(68)
        ctrls["margin_right"].setPlaceholderText("0")
        ctrls["margin_right"].setToolTip(self.tr("MarginRight：图片右侧留白（像素，允许负值）"))

        ctrls["margin_bottom_lbl"] = BodyLabel("B")

        ctrls["margin_bottom"] = LineEdit()
        ctrls["margin_bottom"].setFont(ui_font(10))
        ctrls["margin_bottom"].setText(str(default_opts["margin_bottom"]))
        ctrls["margin_bottom"].setMinimumWidth(68)
        ctrls["margin_bottom"].setPlaceholderText("0")
        ctrls["margin_bottom"].setToolTip(self.tr("MarginBottom：图片下方留白（像素，允许负值）"))

        return ctrls

    def _add_option_row(self, card_layout: QVBoxLayout, ctrls: Dict[str, Any]):
        """将选项控件添加到卡片布局的第二行。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 2, 0, 0)

        row.addWidget(ctrls["zoom_mode"])
        row.addWidget(ctrls["zoom_value"])
        row.addWidget(ctrls["zoom_pct"])
        row.addSpacing(10)
        row.addWidget(ctrls["nodecor"])
        row.addWidget(ctrls["forcewipedecor"])
        row.addSpacing(10)
        row.addWidget(ctrls["margin_lbl"])
        row.addWidget(ctrls["margin_left_lbl"])
        row.addWidget(ctrls["margin_left"])
        row.addWidget(ctrls["margin_right_lbl"])
        row.addWidget(ctrls["margin_right"])
        row.addWidget(ctrls["margin_bottom_lbl"])
        row.addWidget(ctrls["margin_bottom"])
        row.addStretch()

        card_layout.addLayout(row)

    def _make_browse_button(self, edit: "LineEdit") -> ToolButton:
        """创建一个浏览按钮，点击后选取图片并把文件名填入 ``edit``。"""
        btn = ToolButton(FIF.FOLDER)
        btn.setToolTip(self.tr("浏览图片文件（仅取文件名）"))
        btn.clicked.connect(lambda: self._browse_image(edit))
        return btn

    def _browse_image(self, edit: "LineEdit"):
        """弹出文件选择框，从上次加载目录开始，选中后把文件名写入 ``edit``。"""
        init_dir = self._last_image_dir()
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择图片文件"),
            init_dir,
            self.tr("图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*.*)"),
        )
        if path:
            edit.setText(Path(path).name)
            self._save_image_dir(path)

    def _last_image_dir(self) -> str:
        """读取上次浏览图片的目录。"""
        if AppSettings is None:
            return ""  # pragma: no cover
        try:
            return AppSettings().get("nicokara_emoji_last_image_dir", "") or ""
        except Exception:
            return ""

    def _save_image_dir(self, path: str):
        """记忆本次浏览图片所在目录，供下次预填。"""
        if AppSettings is None:
            return  # pragma: no cover
        try:
            settings = AppSettings()
            settings.set("nicokara_emoji_last_image_dir", str(Path(path).parent))
            settings.save()
        except Exception:
            pass

    def _add_card(
        self,
        singer_name: str,
        default_front: str,
        default_back: str,
        default_opts: Dict[str, Any],
        trigger_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新增一张演唱者配置卡片，插入到列表末尾的伸缩项之前。

        Args:
            singer_name: 演唱者显示名
            default_front: 前画像默认值
            default_back: 後画像默认值
            default_opts: 选项默认值
            trigger_text: 触发字符。为 None 时自动由 singer_name 生成。
        """
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setFrameShadow(QFrame.Shadow.Raised)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        # --- 第一行：演唱者名 · 触发字符 · 前画像 · 後画像 · 删除 ---
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        name_edit = LineEdit()
        name_edit.setFont(ui_font(10))
        name_edit.setText(singer_name)
        name_edit.setPlaceholderText(self.tr("演唱者名"))
        name_edit.setFixedWidth(96)
        row1.addWidget(name_edit)

        trigger_lbl = BodyLabel(self.tr("触发"))
        row1.addWidget(trigger_lbl)

        trigger_edit = LineEdit()
        trigger_edit.setFont(ui_font(10))
        if trigger_text is not None:
            trigger_edit.setText(trigger_text)
        else:
            trigger_edit.setText(f"【{singer_name}】" if singer_name else "")
        trigger_edit.setPlaceholderText(self.tr("必填"))
        trigger_edit.setMinimumWidth(110)
        row1.addWidget(trigger_edit)

        front_lbl = BodyLabel(self.tr("前画像"))
        front_lbl.setToolTip(self.tr("擦除前图片文件名（可留空）"))
        row1.addWidget(front_lbl)

        front_edit = LineEdit()
        front_edit.setFont(ui_font(10))
        front_edit.setText(default_front)
        front_edit.setPlaceholderText(self.tr("可留空"))
        row1.addWidget(front_edit, stretch=2)
        row1.addWidget(self._make_browse_button(front_edit))

        back_lbl = BodyLabel(self.tr("後画像"))
        back_lbl.setToolTip(self.tr("擦除后图片文件名（可省略；留空=与前画像相同）"))
        row1.addWidget(back_lbl)

        back_edit = LineEdit()
        back_edit.setFont(ui_font(10))
        back_edit.setText(default_back)
        back_edit.setPlaceholderText(self.tr("留空=同前画像"))
        row1.addWidget(back_edit, stretch=2)
        row1.addWidget(self._make_browse_button(back_edit))

        del_btn = TransparentToolButton(FIF.DELETE)
        del_btn.setToolTip(self.tr("删除此行"))
        row1.addWidget(del_btn)

        card_layout.addLayout(row1)

        # --- 第二行：缩放 · 飾り · 余白 ---
        opt_ctrls = self._make_option_controls(default_opts)
        self._add_option_row(card_layout, opt_ctrls)

        # 插入到末尾伸缩项之前
        self._row_layout.insertWidget(self._row_layout.count() - 1, card)

        row = {
            "card": card,
            "name": name_edit,
            "trigger": trigger_edit,
            "front": front_edit,
            "back": back_edit,
            "zoom_mode": opt_ctrls["zoom_mode"],
            "zoom_value": opt_ctrls["zoom_value"],
            "nodecor": opt_ctrls["nodecor"],
            "forcewipedecor": opt_ctrls["forcewipedecor"],
            "margin_left": opt_ctrls["margin_left"],
            "margin_right": opt_ctrls["margin_right"],
            "margin_bottom": opt_ctrls["margin_bottom"],
        }
        del_btn.clicked.connect(lambda: self._remove_card(row))
        self._rows.append(row)
        return row

    def _remove_card(self, row: Dict[str, Any]):
        """删除指定行的卡片。"""
        if row not in self._rows:
            return
        self._rows.remove(row)
        card = row["card"]
        self._row_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()

    def _on_add_row(self):
        """手动新增一空白演唱者行，画像/选项沿用记忆的默认参数。

        新增后自动放大窗口（不超过屏幕），并把视觉焦点滚动到新行。
        """
        default_str = self._get_default_params()
        default_front, default_back, default_opts_str = split_params(default_str)
        default_opts = parse_option_str(default_opts_str)
        row = self._add_card("", default_front, default_back, default_opts)

        # 让窗口尽量长高以容纳新行（受屏幕与 maximumHeight 限制）
        card = row["card"]
        card.adjustSize()
        delta = card.sizeHint().height() + self._row_layout.spacing()
        fit_to_screen(self, self.width(), self.height() + delta)

        # 布局更新后再滚动到新行并聚焦其演唱者名输入框
        def _reveal():
            self._scroll.ensureWidgetVisible(card)
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            row["name"].setFocus()

        QTimer.singleShot(0, _reveal)

    def _populate_rows(self):
        """根据 self._singers 填充每位演唱者的配置卡片。

        优先级：已存在的 @Emoji 标签 > nicokara_emoji_default > 内置默认值。
        未匹配歌手的已有 @Emoji 标签也作为额外行保留。
        """
        default_str = self._get_default_params()
        default_front, default_back, default_opts_str = split_params(default_str)
        default_opts = parse_option_str(default_opts_str)

        matched_triggers: set[str] = set()

        for _singer_id, singer_name in self._singers:
            trigger = f"【{singer_name}】"
            tag_params = self._existing_tags.get(trigger)
            if tag_params:
                front, back, opts_str = tag_params
                opts = parse_option_str(opts_str)
                matched_triggers.add(trigger)
            else:
                front, back, opts_str = default_front, default_back, default_opts_str
                opts = default_opts
            self._add_card(singer_name, front, back, opts)

        # 添加未匹配歌手的已有 @Emoji 标签（如自定义触发字符等非歌手标签）
        for trigger, (front, back, opts_str) in self._existing_tags.items():
            if trigger not in matched_triggers:
                opts = parse_option_str(opts_str)
                display_name = (
                    trigger.strip("【】")
                    if trigger.startswith("【") and trigger.endswith("】")
                    else trigger
                )
                self._add_card(display_name, front, back, opts, trigger_text=trigger)

    def get_singer_params(self) -> List[Tuple[str, str, str]]:
        """返回 [(singer_name, trigger, params), ...] 列表。"""
        def _int(text: str, default: int = 0) -> int:
            try:
                return int(text.strip())
            except (ValueError, AttributeError):
                return default

        result = []
        for row in self._rows:
            trigger = row["trigger"].text().strip()
            front = row["front"].text().strip()
            back = row["back"].text().strip()

            zoom_mode_text = row["zoom_mode"].currentText()
            opts = {
                "zoom_mode": zoom_mode_text if zoom_mode_text != "未指定" else None,
                "zoom_value": _int(row["zoom_value"].text(), 100),
                "nodecor": row["nodecor"].isChecked(),
                "forcewipedecor": row["forcewipedecor"].isChecked(),
                "margin_left": _int(row["margin_left"].text(), 0),
                "margin_right": _int(row["margin_right"].text(), 0),
                "margin_bottom": _int(row["margin_bottom"].text(), 0),
            }
            options_str = build_option_str(opts)
            params = join_params(front, back, options_str)
            result.append((row["name"].text().strip(), trigger, params))
        return result

    def _copy_first_row_params(self):
        if not self._rows:
            return
        first = self._rows[0]
        for row in self._rows[1:]:
            row["front"].setText(first["front"].text())
            row["back"].setText(first["back"].text())
            row["zoom_mode"].setCurrentText(first["zoom_mode"].currentText())
            row["zoom_value"].setText(first["zoom_value"].text())
            row["nodecor"].setChecked(first["nodecor"].isChecked())
            row["forcewipedecor"].setChecked(first["forcewipedecor"].isChecked())
            row["margin_left"].setText(first["margin_left"].text())
            row["margin_right"].setText(first["margin_right"].text())
            row["margin_bottom"].setText(first["margin_bottom"].text())

    def _on_accept(self):
        singer_params = self.get_singer_params()
        apply_emoji_tags_to_settings(singer_params)
        self.accept()
