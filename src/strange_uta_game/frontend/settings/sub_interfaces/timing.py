"""打轴设定 + Offset校准子页面。"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import Qt
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import PushButton, SettingCard, SettingCardGroup

from strange_uta_game.frontend.dpi_policy import HIGH_DPI_SCALING_KEY
from strange_uta_game.frontend.font_utils import ui_font

from ..calibration_dialog import CalibrationDialog
from ..cards import ComboSettingCard, SpinSettingCard, SwitchSettingCard
from ..preview_guide_dialog import PreviewGuideDialog
from .base import SubSettingInterface


class TimingSubInterface(SubSettingInterface):
    def __init__(self, parent=None, *, embedded: Optional[bool] = None):
        super().__init__(parent)
        # None preserves compatibility for direct callers that historically
        # identified embedded mode through AppSettings._provider.
        self._embedded = embedded
        self._settings_ref = None
        self._calibration_dialog = None
        self._high_dpi_setting_available = False
        self._init_ui()

    def _init_ui(self):
        tr = self.tr
        # ── 分组 1：性能 ──
        g_performance = SettingCardGroup(tr("性能"), self.scrollWidget)
        self._tr_register(g_performance, title_source="性能")
        self.card_ui_refresh_fps = self._tr_register(
            ComboSettingCard(
                FIF.SPEED_MEDIUM,
                tr("打轴界面刷新率"),
                tr("低配置电脑可选择 30 帧；仅降低界面动画刷新率，不影响打轴时间戳精度"),
                items=[tr("流畅（60 帧）"), tr("低性能模式（30 帧）")],
                parent=g_performance,
            ),
            title_source="打轴界面刷新率",
            content_source="低配置电脑可选择 30 帧；仅降低界面动画刷新率，不影响打轴时间戳精度",
        )
        self.card_ui_refresh_fps.set_item_sources(
            ["流畅（60 帧）", "低性能模式（30 帧）"]
        )
        self.card_high_dpi_scaling = self._tr_register(
            SwitchSettingCard(
                FIF.ZOOM,
                tr("高分屏适配"),
                tr(
                    "关闭后由 Windows 缩放整个程序，可降低高分屏渲染开销，"
                    "但画面会变模糊；重启软件后生效"
                ),
                parent=g_performance,
            ),
            title_source="高分屏适配",
            content_source=(
                "关闭后由 Windows 缩放整个程序，可降低高分屏渲染开销，"
                "但画面会变模糊；重启软件后生效"
            ),
        )
        g_performance.addSettingCard(self.card_ui_refresh_fps)
        g_performance.addSettingCard(self.card_high_dpi_scaling)
        self.expandLayout.addWidget(g_performance)

        # ── 分组 2：时间补正 ──
        g_correct = SettingCardGroup(tr("时间补正"), self.scrollWidget)
        self._tr_register(g_correct, title_source="时间补正")
        self.card_offset = self._tr_register(
            SpinSettingCard(FIF.DATE_TIME, tr("按键补偿"),
                tr("建议用下方的offset校正来矫正，用于设备引起的反应延迟（负值=提前，正值=延后）"),
                min_val=-5000, max_val=5000, step=10, suffix=" ms", parent=g_correct),
            title_source="按键补偿",
            content_source="建议用下方的offset校正来矫正，用于设备引起的反应延迟（负值=提前，正值=延后）")
        self.card_speed_correction = self._tr_register(
            SpinSettingCard(FIF.SPEED_MEDIUM, tr("速度补正"),
                tr("打轴时间戳的速度修正系数"),
                min_val=50, max_val=200, step=5, suffix=" %", parent=g_correct),
            title_source="速度补正", content_source="打轴时间戳的速度修正系数")
        self.card_export_offset = self._tr_register(
            SpinSettingCard(FIF.HISTORY, tr("全局偏移"),
                tr("全局偏移，用于控制本软件内整体轴时间偏移（毫秒），（负值=提前，正值=延后）"),
                min_val=-5000, max_val=5000, step=10, suffix=" ms", parent=g_correct),
            title_source="全局偏移",
            content_source="全局偏移，用于控制本软件内整体轴时间偏移（毫秒），（负值=提前，正值=延后）")
        self.card_timing_step = self._tr_register(
            SpinSettingCard(FIF.UP, tr("微调时间戳步长"),
                tr("Alt+↑/Alt+↓ 微调选中节奏点时间戳的步长"),
                min_val=1, max_val=500, step=1, suffix=" ms", parent=g_correct),
            title_source="微调时间戳步长",
            content_source="Alt+↑/Alt+↓ 微调选中节奏点时间戳的步长")
        # 节拍器校准并入「时间补正」组（与「按键补偿」呼应：用下方 offset 校正来矫正）
        cal_card = self._tr_register(
            SettingCard(FIF.SPEED_HIGH, tr("节拍器校准"),
                tr("打开校准弹窗，跟随节拍器按空格键测量 Offset"), g_correct),
            title_source="节拍器校准",
            content_source="打开校准弹窗，跟随节拍器按空格键测量 Offset")
        self.btn_cal_open = PushButton(tr("开始校准"), cal_card)
        self._tr_register_text(self.btn_cal_open, "setText", "开始校准")
        self.btn_cal_open.setFont(ui_font(10))
        self.btn_cal_open.clicked.connect(self._open_calibration_dialog)
        cal_card.hBoxLayout.addWidget(self.btn_cal_open, 0, Qt.AlignmentFlag.AlignRight)
        cal_card.hBoxLayout.addSpacing(16)
        for c in [self.card_offset, self.card_speed_correction,
                  self.card_export_offset, self.card_timing_step, cal_card]:
            g_correct.addSettingCard(c)
        self.expandLayout.addWidget(g_correct)

        # ── 分组 3：波形时间标签 ──
        g_wave = SettingCardGroup(tr("波形时间标签"), self.scrollWidget)
        self._tr_register(g_wave, title_source="波形时间标签")
        self.card_waveform_tag_edit = self._tr_register(
            SwitchSettingCard(FIF.EDIT, tr("波形时间标签拖拽"),
                tr("在波形区把时间标签作为可拖动对象：单击把手选中并跳转、拖动把手改时间、Ctrl 多选批量平移；关闭则恢复为旧的纯显示模式"), parent=g_wave),
            title_source="波形时间标签拖拽",
            content_source="在波形区把时间标签作为可拖动对象：单击把手选中并跳转、拖动把手改时间、Ctrl 多选批量平移；关闭则恢复为旧的纯显示模式")
        self.card_waveform_center_playhead = self._tr_register(
            SwitchSettingCard(FIF.PIN, tr("播放头居中模式"),
                tr("播放时将播放头锁定在时间轴中央，由波形和时间标签随播放向左滚动"), parent=g_wave),
            title_source="播放头居中模式",
            content_source="播放时将播放头锁定在时间轴中央，由波形和时间标签随播放向左滚动")
        self.card_waveform_tag_char = self._tr_register(
            SwitchSettingCard(FIF.FONT, tr("波形标签显示字符"),
                tr("在波形时间标签上显示对应的本体字符文本；关闭后该字符不在波形上标注"), parent=g_wave),
            title_source="波形标签显示字符",
            content_source="在波形时间标签上显示对应的本体字符文本；关闭后该字符不在波形上标注")
        self.card_waveform_tag_ruby = self._tr_register(
            SwitchSettingCard(FIF.FONT, tr("波形标签显示注音"),
                tr("在波形时间标签上显示对应的注音(ruby)文本；关闭后注音不在波形上标注"), parent=g_wave),
            title_source="波形标签显示注音",
            content_source="在波形时间标签上显示对应的注音(ruby)文本；关闭后注音不在波形上标注")
        for c in [self.card_waveform_center_playhead, self.card_waveform_tag_edit, self.card_waveform_tag_char,
                  self.card_waveform_tag_ruby]:
            g_wave.addSettingCard(c)
        self.expandLayout.addWidget(g_wave)

        # ── 分组 4：鼠标与焦点 ──
        g_mouse = SettingCardGroup(tr("鼠标与焦点"), self.scrollWidget)
        self._tr_register(g_mouse, title_source="鼠标与焦点")
        self.card_disable_click_jump = self._tr_register(
            SwitchSettingCard(FIF.CLOSE, tr("禁用单击跳转"),
                tr("关闭单击字符/节奏点延迟后跳转到目标行的功能（双击跳转不受影响）"), parent=g_mouse),
            title_source="禁用单击跳转",
            content_source="关闭单击字符/节奏点延迟后跳转到目标行的功能（双击跳转不受影响）")
        self.card_disable_click_recenter = self._tr_register(
            SwitchSettingCard(FIF.PIN, tr("禁用点击时居中"),
                tr("单击或双击字符/节奏点时不再把目标行滚动到视口中央（光标仍会移动；播放自动滚动与键盘导航不受影响）"), parent=g_mouse),
            title_source="禁用点击时居中",
            content_source="单击或双击字符/节奏点时不再把目标行滚动到视口中央（光标仍会移动；播放自动滚动与键盘导航不受影响）")
        self.card_hide_hitbox_highlights = self._tr_register(
            SwitchSettingCard(FIF.TRANSPARENT, tr("隐藏焦点高亮"),
                tr("隐藏 current 域和 focus 域的 hitbox 高亮背景；启用后仅在拖拽多选时显示 focus 域高亮"), parent=g_mouse),
            title_source="隐藏焦点高亮",
            content_source="隐藏 current 域和 focus 域的 hitbox 高亮背景；启用后仅在拖拽多选时显示 focus 域高亮")
        for c in [self.card_disable_click_jump, self.card_disable_click_recenter,
                  self.card_hide_hitbox_highlights]:
            g_mouse.addSettingCard(c)
        self.expandLayout.addWidget(g_mouse)

        # ── 分组 5：预览指引 ──
        g_guide = SettingCardGroup(tr("预览指引"), self.scrollWidget)
        self._tr_register(g_guide, title_source="预览指引")
        self.card_preview_guide = self._tr_register(
            SwitchSettingCard(FIF.VIEW, tr("打轴预览指引"),
                tr("打轴播放时在当前行以光标为锚用过渡色提示上一个/正在/下一个打的字；具体不透明度与开关可在下方「预览指引方式」中自定义"), parent=g_guide),
            title_source="打轴预览指引",
            content_source="打轴播放时在当前行以光标为锚用过渡色提示上一个/正在/下一个打的字；具体不透明度与开关可在下方「预览指引方式」中自定义")
        self.card_preview_guide_style = self._tr_register(
            SettingCard(FIF.PALETTE, tr("预览指引方式"),
                tr("设置预览指引中上一个/正在/下一个字群的不透明度和开关"), g_guide),
            title_source="预览指引方式",
            content_source="设置预览指引中上一个/正在/下一个字群的不透明度和开关")
        self.btn_guide_style = PushButton(tr("设置指引"), self.card_preview_guide_style)
        self._tr_register_text(self.btn_guide_style, "setText", "设置指引")
        self.btn_guide_style.clicked.connect(self._open_preview_guide_dialog)
        self.card_preview_guide_style.hBoxLayout.addWidget(self.btn_guide_style, 0, Qt.AlignmentFlag.AlignRight)
        self.card_preview_guide_style.hBoxLayout.addSpacing(16)
        for c in [self.card_preview_guide, self.card_preview_guide_style]:
            g_guide.addSettingCard(c)
        self.expandLayout.addWidget(g_guide)

        # ── 分组 6：按键音效 ──
        g_sound = SettingCardGroup(tr("按键音效"), self.scrollWidget)
        self._tr_register(g_sound, title_source="按键音效")
        self.card_keysound = self._tr_register(
            SwitchSettingCard(FIF.MUSIC, tr("按键音"),
                tr("打轴时按下按键播放按下音、抬起停顿点按键播放抬起音"), parent=g_sound),
            title_source="按键音", content_source="打轴时按下按键播放按下音、抬起停顿点按键播放抬起音")
        self.card_keysound_volume = self._tr_register(
            SpinSettingCard(FIF.VOLUME, tr("按键音音量"),
                tr("按键音的播放音量（100 = 原始音量）"),
                min_val=0, max_val=200, step=5, suffix=" %", parent=g_sound),
            title_source="按键音音量", content_source="按键音的播放音量（100 = 原始音量）")
        self.card_keysound_style = self._tr_register(
            ComboSettingCard(FIF.PALETTE, tr("按键音风格"),
                tr("选择按键音音效风格"),
                items=[tr("默认"), "osu", tr("街机风"), tr("金属感")], parent=g_sound),
            title_source="按键音风格", content_source="选择按键音音效风格")
        self.card_keysound_style.set_item_sources(["默认", "osu", "街机风", "金属感"])
        for c in [self.card_keysound, self.card_keysound_volume, self.card_keysound_style]:
            g_sound.addSettingCard(c)
        self.expandLayout.addWidget(g_sound)

    def _open_preview_guide_dialog(self):
        if self._settings_ref is None:
            return
        current = {
            "prev_alpha": self._settings_ref.get("timing.preview_guide_prev_alpha", 100),
            "curr_alpha": self._settings_ref.get("timing.preview_guide_curr_alpha", 50),
            "next_alpha": self._settings_ref.get("timing.preview_guide_next_alpha", 20),
            "prev_enabled": self._settings_ref.get("timing.preview_guide_prev_enabled", True),
            "curr_enabled": self._settings_ref.get("timing.preview_guide_curr_enabled", True),
            "next_enabled": self._settings_ref.get("timing.preview_guide_next_enabled", True),
        }
        dialog = PreviewGuideDialog(current, self)
        if dialog.exec() == PreviewGuideDialog.DialogCode.Accepted:
            cfg = dialog.get_guide_config()
            self._settings_ref.set("timing.preview_guide_prev_alpha", int(cfg["prev_alpha"]))
            self._settings_ref.set("timing.preview_guide_curr_alpha", int(cfg["curr_alpha"]))
            self._settings_ref.set("timing.preview_guide_next_alpha", int(cfg["next_alpha"]))
            self._settings_ref.set("timing.preview_guide_prev_enabled", bool(cfg["prev_enabled"]))
            self._settings_ref.set("timing.preview_guide_curr_enabled", bool(cfg["curr_enabled"]))
            self._settings_ref.set("timing.preview_guide_next_enabled", bool(cfg["next_enabled"]))
            self._settings_ref.save()
            self._notify_changed()

    def _sync_ruby_card_enabled(self, *_):
        """注音显示卡的可用性跟随字符显示卡：字符关 → 注音卡禁用（灰显）。"""
        self.card_waveform_tag_ruby.setEnabled(self.card_waveform_tag_char.isChecked())

    def _open_calibration_dialog(self):
        self._calibration_dialog = CalibrationDialog(self)
        self._calibration_dialog.exec()
        if self._calibration_dialog is not None:
            self._calibration_dialog._stop_metronome()
        self._calibration_dialog = None

    def close_calibration(self):
        if self._calibration_dialog is not None:
            self._calibration_dialog.close()
            self._calibration_dialog = None

    def connect_signals(self):
        self.card_offset.value_changed.connect(self._notify_changed)
        self.card_speed_correction.value_changed.connect(self._notify_changed)
        self.card_export_offset.value_changed.connect(self._notify_changed)
        self.card_timing_step.value_changed.connect(self._notify_changed)
        self.card_ui_refresh_fps.index_changed.connect(self._notify_changed)
        # 启动期设置即时落盘，但不触发运行时 settings cascade；当前进程的
        # DPI awareness 不可安全切换，下一次启动才会读取并应用。
        self.card_high_dpi_scaling.checked_changed.connect(
            self._on_high_dpi_scaling_changed
        )
        self.card_waveform_tag_edit.checked_changed.connect(self._notify_changed)
        self.card_waveform_center_playhead.checked_changed.connect(self._notify_changed)
        self.card_waveform_tag_char.checked_changed.connect(self._notify_changed)
        # 注音显示以字符显示为前提：字符关则注音卡禁用（联动）
        self.card_waveform_tag_char.checked_changed.connect(self._sync_ruby_card_enabled)
        self.card_waveform_tag_ruby.checked_changed.connect(self._notify_changed)
        self.card_disable_click_jump.checked_changed.connect(self._notify_changed)
        self.card_disable_click_recenter.checked_changed.connect(self._notify_changed)
        self.card_hide_hitbox_highlights.checked_changed.connect(self._notify_changed)
        self.card_preview_guide.checked_changed.connect(self._notify_changed)
        self.card_keysound.checked_changed.connect(self._notify_changed)
        self.card_keysound_volume.value_changed.connect(self._notify_changed)
        self.card_keysound_style.index_changed.connect(self._notify_changed)

    _STYLE_KEYS = ["default", "osu", "arcade", "sci"]

    def load_settings(self, s):
        self._settings_ref = s
        embedded = (
            getattr(s, "_provider", None) is not None
            if self._embedded is None
            else self._embedded
        )
        self._high_dpi_setting_available = (
            sys.platform == "win32" and not embedded
        )
        self.card_high_dpi_scaling.setVisible(self._high_dpi_setting_available)
        self.card_high_dpi_scaling.setChecked(
            bool(s.get(HIGH_DPI_SCALING_KEY, True))
        )
        self.card_offset.setValue(s.get("timing.tag_offset_ms", -230))
        self.card_speed_correction.setValue(s.get("timing.speed_correction", 80))
        self.card_export_offset.setValue(s.get("export.offset_ms", 0))
        self.card_timing_step.setValue(s.get("timing.timing_adjust_step_ms", 10))
        refresh_fps = s.get("timing.ui_refresh_fps", 60)
        self.card_ui_refresh_fps.setCurrentIndex(1 if refresh_fps == 30 else 0)
        self.card_waveform_tag_edit.setChecked(s.get("timing.waveform_tag_edit_enabled", True))
        self.card_waveform_center_playhead.setChecked(s.get("timing.waveform_center_playhead_enabled", False))
        self.card_waveform_tag_char.setChecked(s.get("timing.waveform_tag_char_enabled", True))
        self.card_waveform_tag_ruby.setChecked(s.get("timing.waveform_tag_ruby_enabled", True))
        self._sync_ruby_card_enabled()
        self.card_disable_click_jump.setChecked(s.get("timing.disable_click_jump", False))
        self.card_disable_click_recenter.setChecked(s.get("timing.disable_click_recenter", False))
        self.card_hide_hitbox_highlights.setChecked(s.get("timing.hide_hitbox_highlights", False))
        self.card_preview_guide.setChecked(s.get("timing.preview_guide_enabled", False))
        self.card_keysound.setChecked(s.get("timing.keysound_enabled", True))
        self.card_keysound_volume.setValue(s.get("timing.keysound_volume", 100))
        style = s.get("timing.keysound_style", "default")
        idx = self._STYLE_KEYS.index(style) if style in self._STYLE_KEYS else 0
        self.card_keysound_style.setCurrentIndex(idx)

    def collect_settings(self, s):
        if self._high_dpi_setting_available:
            s.set(
                HIGH_DPI_SCALING_KEY,
                self.card_high_dpi_scaling.isChecked(),
            )
        s.set("timing.tag_offset_ms", self.card_offset.value())
        s.set("timing.speed_correction", self.card_speed_correction.value())
        s.set("export.offset_ms", self.card_export_offset.value())
        s.set("timing.timing_adjust_step_ms", self.card_timing_step.value())
        s.set(
            "timing.ui_refresh_fps",
            30 if self.card_ui_refresh_fps.currentIndex() == 1 else 60,
        )
        s.set("timing.waveform_tag_edit_enabled", self.card_waveform_tag_edit.isChecked())
        s.set("timing.waveform_center_playhead_enabled", self.card_waveform_center_playhead.isChecked())
        s.set("timing.waveform_tag_char_enabled", self.card_waveform_tag_char.isChecked())
        s.set("timing.waveform_tag_ruby_enabled", self.card_waveform_tag_ruby.isChecked())
        s.set("timing.disable_click_jump", self.card_disable_click_jump.isChecked())
        s.set("timing.disable_click_recenter", self.card_disable_click_recenter.isChecked())
        s.set("timing.hide_hitbox_highlights", self.card_hide_hitbox_highlights.isChecked())
        s.set("timing.preview_guide_enabled", self.card_preview_guide.isChecked())
        s.set("timing.keysound_enabled", self.card_keysound.isChecked())
        s.set("timing.keysound_volume", self.card_keysound_volume.value())
        idx = self.card_keysound_style.currentIndex()
        s.set("timing.keysound_style", self._STYLE_KEYS[idx] if idx < len(self._STYLE_KEYS) else "default")

    def _on_high_dpi_scaling_changed(self, checked: bool) -> None:
        """持久化启动期 DPI 模式；实际切换留到下次启动。"""
        if not self._high_dpi_setting_available or self._settings_ref is None:
            return
        value = bool(checked)
        if bool(self._settings_ref.get(HIGH_DPI_SCALING_KEY, True)) == value:
            return
        self._settings_ref.set(HIGH_DPI_SCALING_KEY, value)
        self._settings_ref.save()
