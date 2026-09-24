"""设置界面 — Pivot 选项卡 + 子页面布局。

架构说明
--------
- SettingsInterface 本身是一个 ScrollArea（沿用原来类型），
  但内部用 vBoxLayout 组织：标题 → Pivot → QStackedWidget。
- 每个选项卡对应一个 SubSettingInterface 子页面，子页面自己负责：
    load_settings(s)     从 AppSettings 读取值填写 UI
    collect_settings(s)  从 UI 读取值写入 AppSettings
    connect_signals()    把控件变更信号连到 _notify_changed
- SettingsInterface 作为协调层：
    _preload()           在主事件循环空闲时初始化子页面（QTimer.singleShot(0, ...)）
    _schedule_auto_save  防抖，500ms 后触发 _do_auto_save
    _do_auto_save        遍历所有子页面 collect_settings → save → notify
- reload_from_disk() / get_settings() 等外部接口保持兼容。

re-export（向后兼容原有 import 路径）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
)

from strange_uta_game.__version__ import __version__ as _app_version
from strange_uta_game.frontend.fluent_widgets import message_question

from .app_settings import AppSettings, _parse_rl_dictionary
from .calibration_dialog import CalibrationCanvas, CalibrationDialog
from .checkpoint_marker_dialog import CheckpointMarkerDialog
from .cards import (
    BrowseSettingCard,
    ComboSettingCard,
    DoubleSpinSettingCard,
    MultiBoolSettingCard,
    MultiCheckSettingCard,
    ShortcutSettingCard,
    SpinSettingCard,
    SwitchSettingCard,
    TextSettingCard,
)
from .dictionary_dialog import DictionaryEditDialog
from .nicokara_dialog import NicokaraTagsDialog
from .pivot import SettingPivot
from .sub_interfaces import (
    AboutSubInterface,
    AutoCheckSubInterface,
    AutoSaveSubInterface,
    DictionarySubInterface,
    ExportSubInterface,
    NetworkSubInterface,
    PlaybackSubInterface,
    ShortcutSubInterface,
    TimingSubInterface,
    UISubInterface,
)

__all__ = [
    "SettingsInterface",
    # re-exports for backward compatibility
    "AppSettings",
    "DictionaryEditDialog",
    "NicokaraTagsDialog",
    "CalibrationDialog",
    "CalibrationCanvas",
    "SpinSettingCard",
    "DoubleSpinSettingCard",
    "SwitchSettingCard",
    "ComboSettingCard",
    "BrowseSettingCard",
    "ShortcutSettingCard",
    "TextSettingCard",
    "_parse_rl_dictionary",
    # InfoBar re-export（测试中使用）
    "InfoBar",
]


class SettingsInterface(ScrollArea):
    """设置界面（Pivot 选项卡 + 子页面）。"""

    settings_changed = pyqtSignal()

    # 选项卡配置：(routeKey, 显示文本)
    TAB_CONFIG: list[tuple[str, str]] = [
        ("playback",   "演奏控制"),
        ("timing",     "打轴"),
        ("auto_save",  "自动保存"),
        ("auto_check", "AutoCheck"),
        ("dictionary", "读音词典"),
        ("ui",         "界面"),
        ("export",     "导出"),
        ("shortcut",   "快捷键"),
        ("network",    "网络"),
        ("about",      "关于/语言"),
    ]

    # ── 类属性兼容：测试代码通过 SettingsInterface._SHORTCUT_* 访问 ──
    _SHORTCUT_ACTIONS = ShortcutSubInterface._SHORTCUT_ACTIONS
    _SHORTCUT_MODES   = ShortcutSubInterface._SHORTCUT_MODES

    def __init__(
        self,
        parent=None,
        settings_provider=None,
        *,
        embedded: Optional[bool] = None,
    ):
        super().__init__(parent)
        self._store = None
        self._settings_provider = settings_provider
        self._settings = AppSettings(provider=settings_provider)
        # MainWindow knows whether it is embedded even when the host does not
        # provide a settings bridge. Keep provider inference as a compatibility
        # fallback for older/direct SettingsInterface callers.
        self._embedded = (
            settings_provider is not None if embedded is None else bool(embedded)
        )
        self._tab_config = [
            item for item in self.TAB_CONFIG
            if not (self._embedded and item[0] == "network")
        ]
        self._initialized = False       # True = _preload 已调度（但可能还未完成）
        self._fully_initialized = False  # True = 所有子页面全部创建并连接完毕

        # 防抖自动保存定时器
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(500)
        self._auto_save_timer.timeout.connect(self._do_auto_save)
        self._loading_settings = False

        # 主容器
        self.scrollWidget = QWidget()
        self.vBoxLayout = QVBoxLayout(self.scrollWidget)

        # 标题
        self.settingLabel = QLabel(self.tr("设置"), self)
        self.settingLabel.setObjectName("settingLabel")
        self._update_setting_label_style()  # 应用初始颜色（含主题颜色）

        # 选项卡 + 堆叠区
        self.pivot = SettingPivot(self)
        self.stackedWidget = QStackedWidget(self)

        self._init_widget()
        self._init_layout()

        # 主题变化时更新标题标签颜色（setStyleSheet 会在 Qt 内部创建独立 Palette
        # 副本，阻止后续 QApplication.setPalette 的 WindowText 更新传入）
        from strange_uta_game.frontend.theme import theme
        theme.changed.connect(self._update_setting_label_style)

        # 在主事件循环空闲时预载子页面（不阻塞 MainWindow 初始化）。
        # 注意：不能用 singleShot(0)，那会在 MSFluentWindow.showEvent 处理期间
        # 触发大量子控件创建，导致窗口重新布局重置任务栏图标。
        # 用 500ms 延迟确保窗口完全显示稳定后再初始化。
        QTimer.singleShot(500, self._preload)

    # ── 初始化 ────────────────────────────────────────────────────────

    def _update_setting_label_style(self):
        """更新标题 QLabel 的样式（含主题文字颜色）。

        setStyleSheet 会让 Qt 为该 widget 生成独立 Palette 副本，导致后续
        QApplication.setPalette 的 WindowText 变化不再传入。
        因此必须在 theme.changed 时显式写入 color，确保深/浅色正确切换。
        """
        from strange_uta_game.frontend.theme import theme
        color = theme.text_primary.name()
        self.settingLabel.setStyleSheet(
            f"QLabel#settingLabel{{"
            f"font-size:28px;font-weight:bold;padding:10px 0;color:{color};"
            f"}}"
        )

    def _init_widget(self):
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 0, 0, 0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setObjectName("settingInterface")
        self.scrollWidget.setObjectName("scrollWidget")
        # 断开 autoFillBackground 对系统 QPalette 的依赖，避免 OS 主题改变时
        # viewport / scrollWidget 的背景色跟随系统走（深色污染）。
        self.viewport().setAutoFillBackground(False)
        self.scrollWidget.setAutoFillBackground(False)

    def _init_layout(self):
        self.vBoxLayout.setContentsMargins(30, 20, 30, 0)
        self.vBoxLayout.setSpacing(10)
        self.vBoxLayout.addWidget(self.settingLabel)

        self.pivot.setFixedHeight(40)
        self.vBoxLayout.addWidget(self.pivot)

        for key, _raw_text in self._tab_config:
            # 类级 TAB_CONFIG 保留中文源串供测试引用，显示时统一 tr()
            self.pivot.addItem(
                routeKey=key,
                text=self._tab_display_text(key),
                onClick=lambda checked, k=key: self._on_tab_changed(k),
            )

        self.vBoxLayout.addWidget(self.stackedWidget)

        self.pivot.setCurrentItem(self._tab_config[0][0])
        self.stackedWidget.setCurrentIndex(0)

    def _preload(self):
        """分批创建子页面，每批之间让事件循环有机会处理消息，避免阻塞 UI。"""
        if self._initialized:
            return
        self._initialized = True

        # 第一批：创建子页面实例（分两次，每次让出事件循环）
        self.playbackInterface  = PlaybackSubInterface(self)
        self.timingInterface    = TimingSubInterface(
            self,
            embedded=self._embedded,
        )
        self.autoSaveInterface  = AutoSaveSubInterface(self)
        self.autoCheckInterface = AutoCheckSubInterface(self)
        self.dictionaryInterface= DictionarySubInterface(self)

        QTimer.singleShot(0, self._preload_second_batch)

    def _preload_second_batch(self):
        """第二批：创建剩余子页面。"""
        self.uiInterface        = UISubInterface(self)
        self.exportInterface    = ExportSubInterface(self)
        self.shortcutInterface  = ShortcutSubInterface(self)
        self.networkInterface   = None if self._embedded else NetworkSubInterface(self)
        self.aboutInterface     = AboutSubInterface(self)

        QTimer.singleShot(0, self._preload_finalize)

    def _preload_finalize(self):
        """第三批：连接信号、加载设置、注入 updater UI。"""
        # 把所有子页面按顺序加入 stackedWidget
        interfaces = [
            self.playbackInterface, self.timingInterface, self.autoSaveInterface,
            self.autoCheckInterface, self.dictionaryInterface, self.uiInterface,
            self.exportInterface, self.shortcutInterface,
            self.aboutInterface,
        ]
        if self.networkInterface is not None:
            interfaces.insert(-1, self.networkInterface)
        for iface in interfaces:
            self.stackedWidget.addWidget(iface)

        # 传入 store（若已由外层设置）
        if self._store is not None:
            self.uiInterface.set_store(self._store)

        # 连接保存/重置/KS导入按钮
        self.aboutInterface.btn_save.clicked.connect(self._on_save)
        self.aboutInterface.btn_reset.clicked.connect(self._reset_settings)
        self.aboutInterface.btn_import_ks.clicked.connect(self._import_from_ks_settings)

        # 让每个子页面把控件变更信号连到各自的 _notify_changed，
        # 再通过 set_change_callback 冒泡到 _schedule_auto_save。
        # 同时注入 _silent_save 通道：用于那些只在导出/导入时才被消费、
        # 改完不影响任何运行时状态的设置项，绕开整条 settings cascade。
        for iface in [
            self.playbackInterface, self.timingInterface, self.autoSaveInterface,
            self.autoCheckInterface, self.dictionaryInterface, self.uiInterface,
            self.exportInterface, self.shortcutInterface,
        ]:
            iface.set_change_callback(self._schedule_auto_save)
            iface.set_silent_save_callback(self._silent_save_setting)
            iface.connect_signals()
        # aboutInterface 不参与 dirty 自动保存（FFmpeg 路径与语言均即时落盘），
        # 但仍需 connect_signals 把语言下拉的 index_changed 连到自己的 handler。
        self.aboutInterface.connect_signals()

        # 初始加载设置
        self._load_current_settings()

        # 注入 updater UI（只在初始化阶段执行一次）
        if self.networkInterface is not None:
            self.networkInterface.attach_updater_ui(self._settings)

        self._fully_initialized = True

    # ── 热更新：Qt 自动派发的 LanguageChange ─────────────────────────

    def _tab_display_text(self, key: str) -> str:
        """各 tab 显示文本——逐项 self.tr 以便 .ts 抽取器把源串归入
        SettingsInterface 上下文（变量参数的 ``self.tr(var)`` 抓不到）。
        """
        if key == "playback":   return self.tr("演奏控制")
        if key == "timing":     return self.tr("打轴")
        if key == "auto_save":  return self.tr("自动保存")
        if key == "auto_check": return self.tr("AutoCheck")
        if key == "dictionary": return self.tr("读音词典")
        if key == "ui":         return self.tr("界面")
        if key == "export":     return self.tr("导出")
        if key == "shortcut":   return self.tr("快捷键")
        if key == "network":    return self.tr("网络")
        if key == "about":      return self.tr("关于/语言")
        return key

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            # 重新设置标题
            self.settingLabel.setText(self.tr("设置"))
            # 重新设置 Pivot 各 tab 的显示文本
            for routeKey, _raw_text in self._tab_config:
                try:
                    item = self.pivot.widget(routeKey)
                    if item is not None and hasattr(item, "setText"):
                        item.setText(self._tab_display_text(routeKey))
                except Exception:
                    pass
        super().changeEvent(event)

    # ── 选项卡切换 ────────────────────────────────────────────────────

    def _on_tab_changed(self, routeKey: str):
        tab_map = {k: i for i, (k, _) in enumerate(self._tab_config)}
        self.stackedWidget.setCurrentIndex(tab_map.get(routeKey, 0))

    # ── 外部接口 ──────────────────────────────────────────────────────

    def set_store(self, store):
        """由 MainWindow 注入 ProjectStore。"""
        self._store = store
        if self._fully_initialized:
            self.uiInterface.set_store(store)

    def get_settings(self) -> AppSettings:
        return self._settings

    def reload_from_disk(self):
        """从磁盘重新加载配置并刷新 UI（由 MainWindow.switchTo 调用）。"""
        self._settings.reload()
        if self._fully_initialized:
            self._load_current_settings()

    # ── 自动保存 ──────────────────────────────────────────────────────

    def _schedule_auto_save(self, *_args):
        if self._loading_settings:
            return
        self._auto_save_timer.start()

    def _silent_save_setting(self, path: str, value) -> None:
        """直接持久化单个 key，不触发 settings_changed / notify("settings")。

        子页面对"只在导出/导入时才被消费"的设置项调用本方法，避免每次
        微调都跑一遍 timing_interface._apply_settings 全量重应用
        （后者会遍历项目所有字符、可能触发 BASS 重载，是已知的 cascade
        噪声源；参见 commit fccb832 关于/语言 cascade 内异常变原生闪退的记录）。
        """
        if self._loading_settings:
            return
        try:
            self._settings.set(path, value)
            self._settings.save()
        except Exception as e:
            print(f"[Settings] 静默保存 {path} 失败: {e}")

    def _do_auto_save(self):
        if not self._fully_initialized:
            return
        self._collect_settings()
        self._settings.save()
        self.settings_changed.emit()
        if self._store is not None:
            self._store.notify("settings")
        self._apply_theme_setting()

    def _apply_theme_setting(self):
        # embedded 模式下主题归宿主独占 —— 不在这里改 ``theme.mode``，否则
        # 会顺带掀掉宿主 QApplication palette + qfluentwidgets 全局主题，导致
        # 工作台出现"半亮半暗"崩坏画面（EMBEDDING.md §5 红线）。
        if self._embedded:
            return
        from strange_uta_game.frontend.theme import theme, ThemeMode
        theme_value = self._settings.get("ui.theme", "auto")
        theme.mode = {"light": ThemeMode.LIGHT, "dark": ThemeMode.DARK}.get(
            theme_value, ThemeMode.AUTO)

    # ── 数据绑定 ──────────────────────────────────────────────────────

    def _load_current_settings(self):
        self._loading_settings = True
        try:
            s = self._settings
            for iface in [
                self.playbackInterface, self.timingInterface, self.autoSaveInterface,
                self.autoCheckInterface, self.dictionaryInterface, self.uiInterface,
                self.exportInterface, self.shortcutInterface, self.aboutInterface,
            ]:
                iface.load_settings(s)
            if self.networkInterface is not None:
                self.networkInterface.load_settings(s)
            self._apply_theme_setting()
        finally:
            self._loading_settings = False

    def _collect_settings(self):
        s = self._settings
        for iface in [
            self.playbackInterface, self.timingInterface, self.autoSaveInterface,
            self.autoCheckInterface, self.dictionaryInterface, self.uiInterface,
            self.exportInterface, self.shortcutInterface,
        ]:
            iface.collect_settings(s)

    # ── 事件 ──────────────────────────────────────────────────────────

    def showEvent(self, a0):
        """切换到设置页面时刷新数据（完全初始化后才刷新）。"""
        if self._fully_initialized:
            self._load_current_settings()
        super().showEvent(a0)

    def hideEvent(self, a0):
        """离开设置页面时：关闭校准弹窗、flush 未完成的自动保存。"""
        if self._fully_initialized:
            self.timingInterface.close_calibration()
            if self._auto_save_timer.isActive():
                self._auto_save_timer.stop()
                self._do_auto_save()
        super().hideEvent(a0)

    def keyPressEvent(self, a0: QKeyEvent | None):
        super().keyPressEvent(a0)

    # ── 操作 ──────────────────────────────────────────────────────────

    def _on_save(self):
        self._collect_settings()
        self._settings.save()
        self.settings_changed.emit()
        if self._store is not None:
            self._store.notify("settings")
        InfoBar.success(
            title=self.tr("设置已保存"), content=self.tr("所有设置已保存到配置文件"),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=3000, parent=self,
        )

    def _reset_settings(self):
        if message_question(
            self,
            self.tr("确认重置"),
            self.tr("确定要将所有设置重置为默认值吗？\n这将覆盖您当前的设置（用户词典和演唱者预设不受影响）。"),
            yes_text=self.tr("是"),
            no_text=self.tr("否"),
        ):
            try:
                if self._settings_provider is not None:
                    self._settings = AppSettings(provider=self._settings_provider)
                    self._settings._settings = self._settings._load_packaged_defaults()
                    # 整字典已替换为默认值；清 dirty 让 save() 走全量
                    # provider.save 而非增量 save_partial，重置才能完整。
                    self._settings._dirty_paths.clear()
                    self._settings.save()
                else:
                    # 丢弃共享实例前先把未保存修改写盘（交接语义），
                    # 再删文件、清缓存重建——新实例从内嵌默认值读起。
                    AppSettings.reset_shared_instances()
                    if self._settings._config_path.exists():
                        self._settings._config_path.unlink()
                    self._settings = AppSettings()
                self._load_current_settings()
                InfoBar.success(title=self.tr("设置已重置"), content=self.tr("所有设置已恢复为默认值"),
                    orient=Qt.Orientation.Horizontal, isClosable=True,
                    position=InfoBarPosition.TOP, duration=3000, parent=self)
            except Exception as e:
                InfoBar.error(title=self.tr("重置失败"), content=str(e),
                    orient=Qt.Orientation.Horizontal, isClosable=True,
                    position=InfoBarPosition.TOP, duration=5000, parent=self)

    # ── KS 配置导入 ──────────────────────────────────────────────────

    @staticmethod
    def _find_ks_settings_path() -> Optional[Path]:
        """按 Karaoke Studio 的路径解析逻辑查找 settings.json。

        优先级（与 KS 保持一致）：
        1. ``KARAOKE_STUDIO_SETTINGS_DIR`` 环境变量（绝对优先）
        2. ``%APPDATA%/{app_name}/settings.json``（Windows）
        3. ``$XDG_CONFIG_HOME/{app_name_lower}/settings.json``（Linux XDG）
        4. ``~/.config/{app_name_lower}/settings.json``（Linux fallback）

        其中 ``app_name`` 默认 ``"Karaoke Studio"``，可由
        ``KARAOKE_STUDIO_SETTINGS_APP_NAME`` 覆盖。
        同时尝试 ``"Karaoke Helper"``（旧版）和 ``"Karaoke Studio Dev"``（开发版）。
        """
        candidates: list[Path] = []

        settings_dir_env = os.getenv("KARAOKE_STUDIO_SETTINGS_DIR")
        if settings_dir_env:
            candidates.append(Path(settings_dir_env).expanduser() / "settings.json")

        app_name = os.getenv("KARAOKE_STUDIO_SETTINGS_APP_NAME", "Karaoke Studio").strip() or "Karaoke Studio"

        for name in (app_name, "Karaoke Helper", "Karaoke Studio Dev"):
            appdata = os.getenv("APPDATA")
            if os.name == "nt" and appdata:
                candidates.append(Path(appdata) / name / "settings.json")

            config_home = os.getenv("XDG_CONFIG_HOME")
            name_lower = name.lower().replace(" ", "-")
            if config_home:
                candidates.append(Path(config_home) / name_lower / "settings.json")
            candidates.append(Path.home() / ".config" / name_lower / "settings.json")

        for p in candidates:
            if p.is_file():
                return p
        return None

    def _import_from_ks_settings(self):
        """从 Karaoke Studio 的 settings.json 导入 SUG 相关配置。

        覆盖规则：KS 来源的同名字段优先，字典/演唱者按 word/name 合并（存在则替换，否则追加），
        网络词典缓存整体覆盖。
        """
        # 仅 standalone 模式支持 KS 导入
        if self._embedded:
            InfoBar.info(
                title=self.tr("不支持"),
                content=self.tr("嵌入模式下不支持从 KS 配置导入"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=3000, parent=self,
            )
            return

        # 确认对话框
        if not message_question(
            self,
            self.tr("确认导入"),
            self.tr(
                "确定要从前 Karaoke Studio 的 settings.json 导入配置吗？\n\n"
                "将从 KS 配置中提取以下内容并合并到当前 SUG 配置：\n"
                "  - 主设置（播放、打轴、界面、快捷键等）\n"
                "  - 用户词典\n"
                "  - 演唱者预设\n"
                "  - 网络词典缓存\n"
                "  - 界面主题\n"
                "  - 更新器设置\n\n"
                "KS 来源的配置将优先覆盖同名设置。"
            ),
            yes_text=self.tr("是"),
            no_text=self.tr("否"),
        ):
            return

        # 查找 KS settings.json
        ks_path = self._find_ks_settings_path()
        if ks_path is None:
            # 自动查找失败，让用户手动选择
            if not message_question(
                self,
                self.tr("未找到 KS 配置"),
                self.tr(
                    "未能自动找到 Karaoke Studio 的 settings.json。\n\n"
                    "是否手动选择文件？"
                ),
                yes_text=self.tr("浏览"),
                no_text=self.tr("取消"),
            ):
                return
            from PyQt6.QtWidgets import QFileDialog
            ks_path_str, _ = QFileDialog.getOpenFileName(
                self, self.tr("选择 KS settings.json"), "",
                "JSON (*.json);;" + self.tr("所有文件 (*.*)"),
            )
            if not ks_path_str:
                return
            ks_path = Path(ks_path_str)

        # 读取并解析 KS settings.json
        try:
            with open(ks_path, "r", encoding="utf-8") as f:
                ks_config = json.load(f)
        except Exception as e:
            InfoBar.error(
                title=self.tr("读取失败"),
                content=self.tr("无法读取 KS 配置文件: {err}").format(err=e),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self,
            )
            return

        if not isinstance(ks_config, dict):
            InfoBar.error(
                title=self.tr("格式错误"),
                content=self.tr("KS 配置文件格式不正确"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self,
            )
            return

        s = self._settings
        imported_items: list[str] = []

        # ── 1. 主配置 (lyrics_timing) ──
        ks_lt = ks_config.get("lyrics_timing")
        if isinstance(ks_lt, dict) and ks_lt:
            _deep_merge_dict(s._settings, ks_lt)
            imported_items.append(self.tr("主设置"))

        # ── 2. 用户词典 (lyrics_timing_dictionary) ──
        # 合并规则：(word, reading) 双键匹配——同 word 同 reading → 覆盖；
        # 同 word 不同 reading → 视为新词条置顶；新 word → 置顶。
        ks_dict = ks_config.get("lyrics_timing_dictionary")
        if isinstance(ks_dict, list) and ks_dict:
            current_dict = s.load_dictionary()
            wr_index: dict[tuple, int] = {}
            for i, entry in enumerate(current_dict):
                w = (entry.get("word") or "").strip()
                r = (entry.get("reading") or "").strip()
                if w:
                    wr_index[(w, r)] = i
            seen_new: set[tuple] = set()
            to_prepend: list = []
            for ks_entry in ks_dict:
                w = (ks_entry.get("word") or "").strip()
                r = (ks_entry.get("reading") or "").strip()
                if not w:
                    continue
                key = (w, r)
                if key in wr_index:
                    current_dict[wr_index[key]] = ks_entry
                elif key not in seen_new:
                    seen_new.add(key)
                    to_prepend.append(ks_entry)
            current_dict = to_prepend + current_dict
            s.save_dictionary(current_dict)
            imported_items.append(self.tr("用户词典"))

        # ── 3. 演唱者预设 (lyrics_timing_singers) ──
        # 合并规则：(name, group) 双键匹配——同 name 同 group → 覆盖；
        # 同 name 不同 group → 视为新条目置顶；新 name → 置顶。
        ks_singers = ks_config.get("lyrics_timing_singers")
        if isinstance(ks_singers, list) and ks_singers:
            current_singers = s.load_singer_presets()
            ng_index: dict[tuple, int] = {}
            for i, entry in enumerate(current_singers):
                n = (entry.get("name") or "").strip()
                g = (entry.get("group") or "").strip()
                if n:
                    ng_index[(n, g)] = i
            seen_new_singers: set[tuple] = set()
            to_prepend_singers: list = []
            for ks_entry in ks_singers:
                n = (ks_entry.get("name") or "").strip()
                g = (ks_entry.get("group") or "").strip()
                if not n:
                    continue
                key = (n, g)
                if key in ng_index:
                    current_singers[ng_index[key]] = ks_entry
                elif key not in seen_new_singers:
                    seen_new_singers.add(key)
                    to_prepend_singers.append(ks_entry)
            current_singers = to_prepend_singers + current_singers
            s.save_singer_presets(current_singers)
            imported_items.append(self.tr("演唱者预设"))

        # ── 4. 网络词典缓存 (lyrics_timing_network_dictionary) ──
        ks_net = ks_config.get("lyrics_timing_network_dictionary")
        if isinstance(ks_net, dict) and ks_net and s._network_dict_path is not None:
            # 整体覆盖缓存文件
            from copy import deepcopy
            s._save_json(s._network_dict_path, deepcopy(ks_net))
            imported_items.append(self.tr("网络词典缓存"))

        # ── 5. 界面主题 (ui_theme) ──
        ks_theme = ks_config.get("ui_theme")
        if isinstance(ks_theme, str) and ks_theme in ("auto", "light", "dark"):
            s.set("ui.theme", ks_theme)
            imported_items.append(self.tr("界面主题"))

        # ── 6. 更新器设置 (updater) ──
        ks_updater = ks_config.get("updater")
        if isinstance(ks_updater, dict):
            updated_updater = False
            for key in ("enabled", "check_on_startup"):
                if key in ks_updater:
                    s.set(f"updater.{key}", ks_updater[key])
                    updated_updater = True
            if "min_check_interval_hours" in ks_updater:
                s.set("updater.min_check_interval_hours", int(ks_updater["min_check_interval_hours"]))
                updated_updater = True
            if "source_order" in ks_updater:
                s.set("updater.source_order", list(ks_updater["source_order"]))
                updated_updater = True
            ks_proxy = ks_updater.get("proxy")
            if isinstance(ks_proxy, dict):
                if "mode" in ks_proxy:
                    s.set("updater.proxy.mode", str(ks_proxy["mode"]))
                    updated_updater = True
                if "manual_url" in ks_proxy:
                    s.set("updater.proxy.manual_url", str(ks_proxy["manual_url"]))
                    updated_updater = True
            if updated_updater:
                imported_items.append(self.tr("更新器设置"))

        # ── 保存并刷新 ──
        try:
            s.save()
        except Exception as e:
            InfoBar.warning(
                title=self.tr("保存失败"),
                content=str(e),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self,
            )
            return

        # 重新加载 UI
        self._load_current_settings()
        self._apply_theme_setting()

        if imported_items:
            InfoBar.success(
                title=self.tr("导入成功"),
                content=self.tr("已从 KS 配置导入: {items}").format(
                    items=", ".join(imported_items),
                ),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=5000, parent=self,
            )
        else:
            InfoBar.info(
                title=self.tr("无数据可导入"),
                content=self.tr("KS 配置文件中未找到 SUG 相关的设置数据"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=4000, parent=self,
            )


def _deep_merge_dict(base: dict, override: dict) -> None:
    """递归深度合并 override 到 base（override 优先）。"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge_dict(base[key], value)
        else:
            base[key] = value
