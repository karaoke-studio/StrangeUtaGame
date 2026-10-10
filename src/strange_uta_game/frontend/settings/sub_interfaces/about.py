"""关于/语言子页面。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, Qt, QProcess, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from strange_uta_game.frontend.font_utils import ui_font
from PyQt6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon as FIF,
    InfoBar, InfoBarPosition,
    PrimaryPushButton, PushButton,
    SettingCard, SettingCardGroup,
)

from strange_uta_game import app_dirs
from strange_uta_game.__version__ import __version__ as _app_version
from strange_uta_game.frontend.localization import (
    AVAILABLE_LANGUAGES,
    DEFAULT_LANGUAGE,
    localization,
)
from ..cards import ComboSettingCard
from .base import SubSettingInterface


class AboutSubInterface(SubSettingInterface):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings_ref = None
        # 语言下拉变更的延迟应用标记（防抖：连续快速切换只应用最后一次）
        self._language_apply_pending = False
        self._init_ui()

    def _init_ui(self):
        # ── 语言设置 ────────────────────────────────────────────
        # 当前仅注册简体中文 (zh_CN)；之后 EN/JA 翻译完成后扩展
        # AVAILABLE_LANGUAGES 即可，本卡片自动出现新选项。
        self.language_group = SettingCardGroup(self.tr("语言"), self.scrollWidget)
        self._language_codes = [lang.code for lang in AVAILABLE_LANGUAGES]
        self._language_card = ComboSettingCard(
            FIF.LANGUAGE,
            self.tr("界面语言"),
            self.tr("切换 UI 显示语言，更改后需重启软件生效"),
            [lang.native_name for lang in AVAILABLE_LANGUAGES],
            self.language_group,
        )
        self.language_group.addSettingCard(self._language_card)
        self.expandLayout.addWidget(self.language_group)

        self.about_group = SettingCardGroup(self.tr("关于/语言"), self.scrollWidget)

        self._about_card = SettingCard(FIF.INFO, self.tr("StrangeUtaGame - 歌词打轴软件"),
            self.tr("版本 v{ver}  |  由 RhythmicaLyrics 启发").format(ver=_app_version),
            self.about_group)
        self.about_group.addSettingCard(self._about_card)

        self._github_url = "https://github.com/karaoke-studio/StrangeUtaGame"
        self._link_card = SettingCard(FIF.GITHUB, "GitHub",
            self._github_url, self.about_group)
        self._btn_open_github = PushButton(self.tr("打开"), self._link_card)
        self._btn_open_github.setFont(ui_font(10))
        self._btn_open_github.setIcon(FIF.LINK)
        self._btn_open_github.clicked.connect(self._open_github)
        self._link_card.hBoxLayout.addWidget(self._btn_open_github, 0, Qt.AlignmentFlag.AlignRight)
        self._link_card.hBoxLayout.addSpacing(16)
        self.about_group.addSettingCard(self._link_card)

        self._path_card = SettingCard(FIF.FOLDER, self.tr("配置文件位置"),
            self.tr("（未加载）"), self.about_group)
        self._btn_open_dir = PushButton(self.tr("打开目录"), self._path_card)
        self._btn_open_dir.setFont(ui_font(10))
        self._btn_open_dir.clicked.connect(self._open_config_dir)
        self._btn_change_dir = PushButton(self.tr("更改位置"), self._path_card)
        self._btn_change_dir.setFont(ui_font(10))
        self._btn_change_dir.clicked.connect(self._change_config_dir)
        self._path_card.hBoxLayout.addWidget(self._btn_open_dir, 0, Qt.AlignmentFlag.AlignRight)
        self._path_card.hBoxLayout.addWidget(self._btn_change_dir, 0, Qt.AlignmentFlag.AlignRight)
        self._path_card.hBoxLayout.addSpacing(16)
        self.about_group.addSettingCard(self._path_card)

        # FFmpeg 路径设置卡
        self.tools_group = SettingCardGroup(self.tr("工具配置"), self.scrollWidget)
        self._ffmpeg_card = SettingCard(
            FIF.MOVIE, self.tr("FFmpeg 路径"),
            self.tr("用于加载视频文件时提取音频（留空则使用系统环境变量）"),
            self.tools_group,
        )
        self._ffmpeg_path_label = PushButton(self.tr("（使用环境变量）"), self._ffmpeg_card)
        self._ffmpeg_path_label.setFont(ui_font(9))
        self._ffmpeg_path_label.setEnabled(False)
        self._ffmpeg_path_label.setMaximumWidth(260)
        self._btn_browse_ffmpeg = PushButton(self.tr("浏览"), self._ffmpeg_card)
        self._btn_browse_ffmpeg.setFont(ui_font(10))
        self._btn_browse_ffmpeg.clicked.connect(self._browse_ffmpeg)
        self._btn_clear_ffmpeg = PushButton(self.tr("清除"), self._ffmpeg_card)
        self._btn_clear_ffmpeg.setFont(ui_font(10))
        self._btn_clear_ffmpeg.clicked.connect(self._clear_ffmpeg_path)
        self._ffmpeg_card.hBoxLayout.addWidget(self._ffmpeg_path_label, 0, Qt.AlignmentFlag.AlignRight)
        self._ffmpeg_card.hBoxLayout.addWidget(self._btn_browse_ffmpeg, 0, Qt.AlignmentFlag.AlignRight)
        self._ffmpeg_card.hBoxLayout.addWidget(self._btn_clear_ffmpeg, 0, Qt.AlignmentFlag.AlignRight)
        if sys.platform in ("win32", "darwin"):
            self._btn_install_ffmpeg = PrimaryPushButton(self.tr("一键安装"), self._ffmpeg_card)
            self._btn_install_ffmpeg.setFont(ui_font(10))
            self._btn_install_ffmpeg.clicked.connect(self._install_ffmpeg)
            self._ffmpeg_card.hBoxLayout.addWidget(self._btn_install_ffmpeg, 0, Qt.AlignmentFlag.AlignRight)
        self._ffmpeg_card.hBoxLayout.addSpacing(16)
        self.tools_group.addSettingCard(self._ffmpeg_card)
        self.expandLayout.addWidget(self.about_group)
        self.expandLayout.addWidget(self.tools_group)

        # 保存/重置/KS导入按钮
        btn_widget = QWidget(self.scrollWidget)
        btn_widget.setMinimumHeight(60)
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(0, 10, 0, 24)
        self.btn_save = PrimaryPushButton(self.tr("保存设置"), btn_widget)
        self.btn_save.setIcon(FIF.SAVE)
        self.btn_save.setMinimumHeight(36)
        self.btn_save.hide()
        self.btn_import_ks = PushButton(self.tr("从KS配置导入"), btn_widget)
        self.btn_import_ks.setIcon(FIF.DOWNLOAD)
        self.btn_import_ks.setMinimumHeight(36)
        self.btn_reset = PushButton(self.tr("重置为默认设置"), btn_widget)
        self.btn_reset.setIcon(FIF.DELETE)
        self.btn_reset.setMinimumHeight(36)
        # btn_save 保留属性供外层 signal 连接，但不在 UI 中显示
        btn_layout.addWidget(self.btn_import_ks)
        btn_layout.addWidget(self.btn_reset)
        btn_layout.addStretch()
        self.expandLayout.addWidget(btn_widget)

    def connect_signals(self):
        # 语言切换由本子页面自己处理（即时落盘 + 重启提示），不冒泡到外层
        # "保存设置" 流程——避免与其它即时生效的设置混在同一个 dirty 事务里。
        self._language_card.index_changed.connect(self._on_language_changed)
        # 其它按钮回调由外层连接

    def load_settings(self, s):
        self._settings_ref = s
        embedded = getattr(s, "_provider", None) is not None

        # ── 语言卡：embedded 下隐藏（语言归宿主独占，与主题同理，见 EMBEDDING.md §5）
        # standalone 下同步当前选项；embedded 下完全不显示，避免与宿主语言冲突。
        self.language_group.setVisible(not embedded)
        if not embedded:
            current_code = s.get("ui.language", DEFAULT_LANGUAGE.code)
            try:
                idx = self._language_codes.index(current_code)
            except ValueError:
                idx = 0
            # blockSignals 防止 load 阶段误触发"语言改变"提示
            self._language_card.combo.blockSignals(True)
            self._language_card.setCurrentIndex(idx)
            self._language_card.combo.blockSignals(False)
        # embedded 模式下配置走宿主存储，没有"配置文件目录"概念：
        # 隐藏整张「配置文件位置」卡片，并避免 setContent(str(None)) 显示 "None"。
        self._path_card.setVisible(not embedded)
        if not embedded:
            self._path_card.setContent(str(s.config_paths.config))
        self.tools_group.setVisible(not embedded)
        self.btn_import_ks.setVisible(not embedded)
        ffmpeg_path = s.get("tools.ffmpeg_path", "")
        self._update_ffmpeg_label(ffmpeg_path)

    def collect_settings(self, s):
        pass  # 关于/语言页的 FFmpeg 路径与语言均在切换时即时保存，无需在此收集

    def _on_language_changed(self, idx: int):
        """重活延迟到下一轮事件循环（防抖合并连续切换）。

        qfluentwidgets 的 currentIndexChanged 在弹层（RoundMenu）自身的
        激活/关闭调用栈内同步发出，此时落盘 + apply_language
        （installTranslator 同步触发 LanguageChange → 各下拉 clear/重填，
        包括语言卡自己）会与弹层销毁竞争（快速连续切换闪退，
        同 ai_timing_dialog 模型下拉的修复）。
        """
        if self._language_apply_pending:
            return
        self._language_apply_pending = True
        QTimer.singleShot(0, self._apply_language_change)

    def _apply_language_change(self) -> None:
        self._language_apply_pending = False
        # 防抖后重读当前选中：连续切换只应用最后一次
        idx = self._language_card.currentIndex()
        if self._settings_ref is None:
            return
        # embedded 下卡片本应隐藏，但万一别处程序化触发了 index_changed，
        # 仍然直接返回——SUG 在 embedded 下绝不写自己的语言或调 translator。
        if getattr(self._settings_ref, "_provider", None) is not None:
            return
        if not (0 <= idx < len(self._language_codes)):
            return
        new_code = self._language_codes[idx]
        old_code = self._settings_ref.get("ui.language", DEFAULT_LANGUAGE.code)
        if new_code == old_code:
            return

        # 即时落盘（绕过外层 dirty 事务）
        self._settings_ref.set("ui.language", new_code)
        self._settings_ref.save()

        # **热更新**：apply_language → installTranslator → Qt 自动向所有
        # top-level widget 派发 LanguageChange 事件；override 了 changeEvent
        # 的 widget 重建/重译；没 override 的旧文本保持不变（典型为已构造的
        # 工具栏/按钮——下次重启或重新构造时刷新）。
        # 用户能立即看到的范围：主窗口标题、设置面板（about/所有 SubSetting）、
        # 因为这些 override 了 changeEvent；其他页面渐进刷新。
        localization.apply_language(new_code)

        InfoBar.success(
            title=self.tr("语言已切换"),
            content=self.tr("当前页已应用新语言；部分页面（编辑器/导出/演唱者管理）会在重新打开后刷新"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4500,
            parent=self,
        )

    def _rebuild_for_language_change(self) -> None:
        """改成精准 retranslate，不再 setWidget(new) 重建 scrollWidget。

        rebuild 路径在本页面会丢失"关于/语言"组与底部按钮（疑似 ExpandLayout 在
        滚动区域中途 addWidget 时序与 setWidget 销毁旧 widget 的析构顺序冲突，
        体现为切语言后整个 about_group + 重置按钮消失）。
        改成对每个被 tr 包过的可见字符串单独 setText/setContent，绕过整段
        rebuild——不需要保留滚动位置/焦点的副作用，也更接近 Qt 期待的 i18n
        流程。
        """
        # SettingCardGroup 标题
        if hasattr(self, "language_group"):
            self.language_group.titleLabel.setText(self.tr("语言"))
        if hasattr(self, "about_group"):
            self.about_group.titleLabel.setText(self.tr("关于"))
        if hasattr(self, "tools_group"):
            self.tools_group.titleLabel.setText(self.tr("工具配置"))

        # 各 SettingCard 标题 + 副标题
        if hasattr(self, "_language_card"):
            self._language_card.titleLabel.setText(self.tr("界面语言"))
            self._language_card.contentLabel.setText(
                self.tr("切换 UI 显示语言，更改后需重启软件生效")
            )
        if hasattr(self, "_about_card"):
            self._about_card.titleLabel.setText(self.tr("StrangeUtaGame - 歌词打轴软件"))
            self._about_card.contentLabel.setText(
                self.tr("版本 v{ver}  |  由 RhythmicaLyrics 启发").format(ver=_app_version)
            )
        if hasattr(self, "_path_card"):
            self._path_card.titleLabel.setText(self.tr("配置文件位置"))
            # contentLabel 在 load_settings 中已被设为实际路径，不在这里覆盖
        if hasattr(self, "_ffmpeg_card"):
            self._ffmpeg_card.titleLabel.setText(self.tr("FFmpeg 路径"))
            self._ffmpeg_card.contentLabel.setText(
                self.tr("用于加载视频文件时提取音频（留空则使用系统环境变量）")
            )

        # 按钮
        if hasattr(self, "_btn_open_github"):
            self._btn_open_github.setText(self.tr("打开"))
        if hasattr(self, "_btn_open_dir"):
            self._btn_open_dir.setText(self.tr("打开目录"))
        if hasattr(self, "_btn_change_dir"):
            self._btn_change_dir.setText(self.tr("更改位置"))
        if hasattr(self, "_btn_browse_ffmpeg"):
            self._btn_browse_ffmpeg.setText(self.tr("浏览"))
        if hasattr(self, "_btn_clear_ffmpeg"):
            self._btn_clear_ffmpeg.setText(self.tr("清除"))
        if hasattr(self, "_btn_install_ffmpeg"):
            self._btn_install_ffmpeg.setText(self.tr("一键安装"))
        if hasattr(self, "btn_save"):
            self.btn_save.setText(self.tr("保存设置"))
        if hasattr(self, "btn_reset"):
            self.btn_reset.setText(self.tr("重置为默认设置"))
        if hasattr(self, "btn_import_ks"):
            self.btn_import_ks.setText(self.tr("从KS配置导入"))

        # FFmpeg label：未设路径时显示「（使用环境变量）」
        if (
            hasattr(self, "_ffmpeg_path_label")
            and self._settings_ref is not None
        ):
            path = self._settings_ref.get("tools.ffmpeg_path", "") or ""
            if not path:
                self._ffmpeg_path_label.setText(self.tr("（使用环境变量）"))

        # 语言下拉项（native_name 是源数据，不走 tr——但 ComboSettingCard
        # 内部的"当前值"显示需要刷新一下）
        if hasattr(self, "_language_card") and self._settings_ref is not None:
            current_code = self._settings_ref.get("ui.language", DEFAULT_LANGUAGE.code)
            try:
                idx = self._language_codes.index(current_code)
            except ValueError:
                idx = 0
            self._language_card.combo.blockSignals(True)
            self._language_card.setCurrentIndex(idx)
            self._language_card.combo.blockSignals(False)

    def _open_github(self):
        QDesktopServices.openUrl(QUrl(self._github_url))

    def _open_config_dir(self):
        cp = self._settings_ref.config_paths.config if self._settings_ref else None
        if cp is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(cp.parent)))

    def _change_config_dir(self):
        cp = self._settings_ref.config_paths.config if self._settings_ref else None
        if cp is None:
            return
        s = self._settings_ref
        new_dir = QFileDialog.getExistingDirectory(self, self.tr("选择配置文件存储目录"), str(cp.parent))
        if not new_dir:
            return

        new_dir_path = Path(new_dir)
        # 标记文件始终定位在程序目录（app_dirs.program_dir()），不依赖
        # sys.argv[0]——后者在 pythonw/打包场景下不可靠。
        redirect_file = app_dirs.redirect_marker_path()
        program_dir = app_dirs.program_dir()

        if new_dir_path.resolve() == program_dir.resolve():
            try:
                if redirect_file.exists():
                    redirect_file.unlink()
            except Exception:
                pass
        else:
            try:
                redirect_file.write_text(str(new_dir_path), encoding="utf-8")
            except Exception as e:
                InfoBar.error(title=self.tr("更改失败"),
                    content=self.tr("无法写入重定向文件: {err}").format(err=e),
                    orient=Qt.Orientation.Horizontal, isClosable=True,
                    position=InfoBarPosition.TOP, duration=5000, parent=self)
                return

        old_path = cp
        new_path = new_dir_path / "config.json"
        if old_path.exists() and old_path != new_path:
            try:
                import shutil
                new_dir_path.mkdir(exist_ok=True)
                shutil.copy2(str(old_path), str(new_path))
                for fname in ("dictionary.json", "network_dictionary.json", "singers.json"):
                    op = old_path.parent / fname
                    np = new_dir_path / fname
                    if op.exists() and op != np:
                        shutil.copy2(str(op), str(np))
            except Exception as e:
                InfoBar.warning(title=self.tr("配置复制失败"),
                    content=self.tr("请手动复制配置文件: {err}").format(err=e),
                    orient=Qt.Orientation.Horizontal, isClosable=True,
                    position=InfoBarPosition.TOP, duration=5000, parent=self)

        # 原地切换存储路径并同步迁移共享实例缓存 key：之后 AppSettings()
        # 仍命中同一实例，避免新旧两个共享实例内存态分裂、互相覆盖。
        s.retarget_config_dir(new_dir_path)

        # 迁移旧默认备份位置下的 .temp 闪退恢复文件（随 retarget 之后执行，
        # 迁移失败记录才会写入新位置的 config.json）。
        self._migrate_temp_files(old_path.parent, new_dir_path, s)

        self._path_card.setContent(str(new_path))
        InfoBar.success(title=self.tr("配置位置已更改"),
            content=self.tr("配置文件将保存到: {path}").format(path=new_path),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=5000, parent=self)

    @staticmethod
    def _migrate_temp_files(old_config_dir: Path, new_config_dir: Path, s) -> None:
        """把旧配置目录默认备份位置下的 .temp 闪退恢复文件搬到新位置。

        备份位置默认跟随 config 目录（``<config_dir>/ProjectBackup/.temp``），
        更改配置位置后旧位置会失联。搬移 + 记录双保险：没搬干净的旧目录
        写入 ``auto_save.legacy_temp_dirs``，供闪退恢复扫描兜底
        （见 ``ProjectStore._crash_recovery_dirs``）。
        """
        import shutil
        old_temp = old_config_dir / "ProjectBackup" / ".temp"
        if not old_temp.is_dir():
            return
        new_temp = new_config_dir / "ProjectBackup" / ".temp"
        try:
            if old_temp.resolve() == new_temp.resolve():
                return
        except OSError:
            return
        migrated_all = True
        try:
            new_temp.mkdir(parents=True, exist_ok=True)
            for f in old_temp.glob(".*.sug.temp"):
                try:
                    shutil.move(str(f), str(new_temp / f.name))
                except OSError:
                    migrated_all = False
        except OSError:
            migrated_all = False
        if not migrated_all:
            try:
                legacy = [str(p) for p in (s.get("auto_save.legacy_temp_dirs", []) or [])]
                key = str(old_temp)
                if key not in legacy:
                    legacy.append(key)
                    s.set("auto_save.legacy_temp_dirs", legacy)
                    s.save()
            except Exception:
                pass

    def _update_ffmpeg_label(self, path: str):
        if path:
            label = Path(path).name
            self._ffmpeg_path_label.setText(label)
            self._ffmpeg_path_label.setToolTip(path)
        else:
            self._ffmpeg_path_label.setText(self.tr("（使用环境变量）"))
            self._ffmpeg_path_label.setToolTip("")

    @staticmethod
    def _ffmpeg_file_filter() -> str:
        if sys.platform == "win32":
            return "可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)"
        return "所有文件 (*)"

    @staticmethod
    def _ffmpeg_default_dir() -> str:
        if sys.platform != "darwin":
            return ""
        import shutil
        located = shutil.which("ffmpeg")
        if located:
            return str(Path(located).parent)
        for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            if Path(d).is_dir():
                return d
        return ""

    def _browse_ffmpeg(self):
        if self._settings_ref is not None and getattr(self._settings_ref, "_provider", None) is not None:
            return
        current = ""
        if self._settings_ref:
            current = self._settings_ref.get("tools.ffmpeg_path", "") or ""
        init_dir = str(Path(current).parent) if current else self._ffmpeg_default_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择 FFmpeg 可执行文件"), init_dir,
            self._ffmpeg_file_filter(),
        )
        if not path:
            return
        self._save_ffmpeg_path(path)

    def _clear_ffmpeg_path(self):
        if self._settings_ref is not None and getattr(self._settings_ref, "_provider", None) is not None:
            return
        self._save_ffmpeg_path("")

    def _save_ffmpeg_path(self, path: str):
        if self._settings_ref is None:
            return
        if getattr(self._settings_ref, "_provider", None) is not None:
            return
        self._settings_ref.set("tools.ffmpeg_path", path)
        self._settings_ref.save()
        self._update_ffmpeg_label(path)
        if path:
            InfoBar.success(title=self.tr("FFmpeg 路径已保存"), content=path,
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=4000, parent=self)
        else:
            InfoBar.success(title=self.tr("FFmpeg 路径已清除"),
                content=self.tr("将使用系统环境变量中的 ffmpeg"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=3000, parent=self)

    def _install_ffmpeg(self):
        if sys.platform == "win32":
            self._install_ffmpeg_windows()
            return
        if sys.platform == "darwin":
            self._install_ffmpeg_macos()
            return
        InfoBar.info(
            title=self.tr("暂不支持"),
            content=self.tr("当前平台不支持一键安装 FFmpeg，请手动安装后通过「浏览」指定路径。"),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=5000, parent=self,
        )

    def _install_ffmpeg_windows(self):
        import ctypes
        ps_args = (
            "-NoExit -Command \""
            "winget install Gyan.FFmpeg "
            "--accept-package-agreements --accept-source-agreements; "
            "Write-Host ''; "
            "Write-Host '>>> 安装完成，可关闭此窗口。<<<' -ForegroundColor Green; "
            "pause\""
        )
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell", ps_args, None, 1
        )
        if ret <= 32:
            InfoBar.error(
                title=self.tr("无法启动安装"),
                content=self.tr("ShellExecute 返回 {ret}，请检查是否拒绝了 UAC 提权。").format(ret=ret),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=6000, parent=self,
            )
            return
        InfoBar.info(
            title=self.tr("已请求管理员权限启动 FFmpeg 安装"),
            content=self.tr("安装完成后，重启软件即可通过环境变量自动使用，或点击「浏览」手动指定路径。"),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=8000, parent=self,
        )

    def _install_ffmpeg_macos(self):
        try:
            subprocess.run(
                [
                    "osascript", "-e",
                    'tell app "Terminal" to do script "brew install ffmpeg && echo && echo \'>>> 安装完成，可关闭此窗口。<<<\' && read -p \'\'"',
                ],
                check=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            InfoBar.error(
                title=self.tr("无法启动终端"),
                content=self.tr("请确认已安装 Homebrew (https://brew.sh)，然后在终端中手动运行: brew install ffmpeg"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=8000, parent=self,
            )
            return
        except FileNotFoundError:
            InfoBar.error(
                title=self.tr("无法启动终端"),
                content=self.tr("请确认已安装 Homebrew (https://brew.sh)，然后在终端中手动运行: brew install ffmpeg"),
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=8000, parent=self,
            )
            return
        InfoBar.info(
            title=self.tr("已在终端中启动 FFmpeg 安装"),
            content=self.tr("安装完成后，重启软件即可通过环境变量自动使用，或点击「浏览」手动指定路径。"),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=8000, parent=self,
        )
