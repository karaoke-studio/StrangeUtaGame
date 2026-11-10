"""导出设定子页面。"""

from __future__ import annotations

from qfluentwidgets import FluentIcon as FIF, SettingCardGroup

from ..cards import BrowseSettingCard, ComboSettingCard, SpinSettingCard, TextSettingCard
from .base import SubSettingInterface

_FMT_TO_IDX = {
    "LRC (增强型)": 0, "LRC (逐行)": 1, "LRC (逐字)": 2,
    "KRA": 3, "TXT": 4, "SRT": 5, "txt2ass": 6, "ASS": 7,
    "Nicokara": 8, "Nicokara (带注音)": 9, "RL 编辑模式": 10,
    "春日向注音": 11, "Kirakara": 12, "TTML": 13,
    "春日向注音（带罗马音）": 12,  # 旧配置兼容
    "LRC": 0,  # 旧配置兼容
}
_IDX_TO_FMT = {
    0: "LRC (增强型)", 1: "LRC (逐行)", 2: "LRC (逐字)",
    3: "KRA", 4: "TXT", 5: "SRT", 6: "txt2ass", 7: "ASS",
    8: "Nicokara", 9: "Nicokara (带注音)", 10: "RL 编辑模式",
    11: "春日向注音", 12: "Kirakara", 13: "TTML",
}


class ExportSubInterface(SubSettingInterface):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        tr = self.tr
        g = SettingCardGroup(tr("导出设定"), self.scrollWidget)
        self._tr_register(g, title_source="导出设定")
        # config.json 存的是中文 key（如 "LRC (增强型)"），由 _FMT_TO_IDX /
        # _IDX_TO_FMT 走"索引"做映射。这里 items 只决定显示文本，可安全 tr 翻译。
        self.card_default_format = self._tr_register(
            ComboSettingCard(FIF.SHARE, tr("默认导出格式"),
                tr("导出歌词时的默认文件格式"),
                items=[
                    tr("LRC (增强型)"), tr("LRC (逐行)"), tr("LRC (逐字)"),
                    "KRA", "TXT", "SRT", "txt2ass", "ASS",
                    "Nicokara", tr("Nicokara (带注音)"), tr("RL 编辑模式"),
                    tr("春日向注音"), "Kirakara", tr("TTML（Beta）"),
                ],
                parent=g),
            title_source="默认导出格式",
            content_source="导出歌词时的默认文件格式")
        self.card_default_format.set_item_sources([
            "LRC (增强型)", "LRC (逐行)", "LRC (逐字)",
            "KRA", "TXT", "SRT", "txt2ass", "ASS",
            "Nicokara", "Nicokara (带注音)", "RL 编辑模式",
            "春日向注音", "Kirakara", "TTML",
        ])
        self.card_export_dir = self._tr_register(
            BrowseSettingCard(FIF.FOLDER, tr("默认导出目录"),
                tr("设置后，导出时将始终优先使用此目录。\n留空则不启用，导出时自动使用已保存项目 / 最近加载的文件所在目录。"),
                clearable=True, parent=g),
            title_source="默认导出目录",
            content_source="设置后，导出时将始终优先使用此目录。\n留空则不启用，导出时自动使用已保存项目 / 最近加载的文件所在目录。")
        self.card_software_compensation = self._tr_register(
            SpinSettingCard(FIF.HISTORY, tr("软件导出补偿"),
                tr("导出时给时间戳加上此补偿值（除.sug外的所有格式），负值=提前，正值=延后"),
                min_val=-5000, max_val=5000, step=10, suffix=" ms", parent=g),
            title_source="软件导出补偿",
            content_source="导出时给时间戳加上此补偿值（除.sug外的所有格式），负值=提前，正值=延后")
        self.card_nicokara_pause_char = self._tr_register(
            TextSettingCard(FIF.EDIT, tr("Nicokara停顿符"),
                tr("导出带注音的格式（Nicokara·ASS·KRL）时，删除ruby中的此字符"),
                placeholder=tr("输入停顿符"), max_length=5, parent=g),
            title_source="Nicokara停顿符",
            content_source="导出带注音的格式（Nicokara·ASS·KRL）时，删除ruby中的此字符")
        for c in [self.card_default_format, self.card_export_dir,
                  self.card_software_compensation, self.card_nicokara_pause_char]:
            g.addSettingCard(c)
        self.expandLayout.addWidget(g)

    def connect_signals(self):
        self.card_default_format.index_changed.connect(self._notify_changed)
        self.card_export_dir.path_changed.connect(self._notify_changed)
        # 软件导出补偿只在导出/导入时被消费，不影响任何运行时状态。
        # 走静默保存通道，避免触发整条 settings cascade
        # （_apply_settings 会遍历所有字符、可能触发 BASS 重载）。
        self.card_software_compensation.value_changed.connect(
            lambda v: self._silent_save("export.software_compensation_ms", v)
        )
        self.card_nicokara_pause_char.value_changed.connect(self._notify_changed)

    def load_settings(self, s):
        self.card_default_format.setCurrentIndex(
            _FMT_TO_IDX.get(s.get("export.default_format", "Nicokara (带注音)"), 9))
        export_dir = s.get("export.default_export_dir", "")
        if export_dir:
            self.card_export_dir.setText(export_dir)
        self.card_software_compensation.setValue(s.get("export.software_compensation_ms", 0))
        self.card_nicokara_pause_char.setValue(s.get("export.nicokara_pause_char", "^"))

    def collect_settings(self, s):
        s.set("export.default_format",
              _IDX_TO_FMT.get(self.card_default_format.currentIndex(), "Nicokara (带注音)"))
        s.set("export.default_export_dir", self.card_export_dir.text())
        s.set("export.software_compensation_ms", self.card_software_compensation.value())
        s.set("export.nicokara_pause_char", self.card_nicokara_pause_char.value())
