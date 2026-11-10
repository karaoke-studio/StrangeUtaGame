"""主页界面。

提供项目创建和打开功能，替代原来的启动界面。
采用 Fluent Design 风格，集成到 MSFluentWindow 侧边栏导航中。
"""

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFileDialog,
)
from PyQt6.QtCore import QEvent, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from qfluentwidgets import (
    PushButton,
    PrimaryPushButton,
    LineEdit,
    TextEdit,
    InfoBar,
    InfoBarPosition,
    StateToolTip,
    FluentIcon as FIF,
    SimpleCardWidget,
    LargeTitleLabel,
    SubtitleLabel,
    TitleLabel,
    CaptionLabel,
)

from typing import Optional, List
from pathlib import Path

from strange_uta_game.backend.domain import Project, Sentence, Singer
from strange_uta_game.backend.application import ProjectService, AutoCheckService
from strange_uta_game.backend.infrastructure.audio.video_converter import (
    VIDEO_EXTENSIONS,
    is_embedded,
    is_ffmpeg_available,
    is_video_file,
)
from strange_uta_game.backend.infrastructure.parsers.lyric_parser import (
    LRCParser,
    NicokaraParser,
    parse_to_sentences,
    nicokara_result_to_sentences,
)
from strange_uta_game.backend.infrastructure.parsers.inline_format import (
    sentences_from_inline_text,
)
from strange_uta_game.backend.infrastructure.parsers.kasugamuki_format import (
    is_kasugamuki_content,
    sentences_from_kasugamuki,
)
from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
    is_ttml_content,
)


def _reset_nicokara_tags() -> None:
    """新建项目前重置全局 nicokara_tags 为默认值，避免上一项目残留。"""
    try:
        from strange_uta_game.frontend.settings.app_settings import AppSettings
        settings = AppSettings()
        settings.set(
            "nicokara_tags",
            dict(AppSettings.DEFAULT_SETTINGS.get("nicokara_tags", {})),
        )
        settings.save()
    except Exception:
        pass


class HomeInterface(QWidget):
    """主页界面（遗留页：默认隐藏，入口保留待移除）

    提供：
    - 创建新项目（歌词输入 + 音频选择）
    - 打开已有项目

    data_changed 刷新本页只有按钮状态一处且轻量，隐藏期间无需跳过。
    """

    project_created = pyqtSignal(Project, str)  # (project, audio_path)
    project_opened = pyqtSignal(Project, str, object)  # (project, file_path, extras)
    project_save_requested = pyqtSignal()  # 请求保存当前项目
    _LYRIC_EXTENSIONS = {".lrc", ".txt", ".kra", ".krl", ".ass", ".srt", ".ttml"}
    _PROJECT_EXTENSIONS = {".sug"}
    _AUDIO_EXTENSIONS = {
        ".mp3", ".wav", ".flac", ".ogg",
        # 由 BASS 插件直接解码（无需 FFmpeg）
        ".m4a", ".m4b", ".aac", ".wma", ".opus", ".ape", ".ac3", ".wv",
        ".dsf", ".dff",
    }

    def __init__(self, parent=None):
        super().__init__(parent)

        self._project_service = ProjectService()
        self._audio_path: Optional[str] = None
        self._lyric_lines: List[Sentence] = []
        self._nicokara_singers: List[Singer] = []
        self._nicokara_singer_key_to_id: dict = {}
        self._ttml_singers: List[Singer] = []
        self._ttml_meta: dict = {}
        # 异步加载相关
        self._loading_thread: Optional[QThread] = None
        self._loading_worker = None
        self._state_tooltip = None

        self.setAcceptDrops(True)
        self._init_ui()

    def changeEvent(self, event):
        """切语言时整张首页拆掉重建——按钮/标签都是无状态的。"""
        if event.type() == QEvent.Type.LanguageChange:
            from strange_uta_game.frontend.localization import detach_layout_for_rebuild
            # 保留用户已输入的歌词文本与已选音频路径
            saved_lyrics = self.text_lyrics.toPlainText() if hasattr(self, "text_lyrics") else ""
            saved_audio = self.line_audio_path.text() if hasattr(self, "line_audio_path") else ""
            detach_layout_for_rebuild(self)
            self._init_ui()
            if saved_lyrics and hasattr(self, "text_lyrics"):
                self.text_lyrics.setPlainText(saved_lyrics)
            if saved_audio and hasattr(self, "line_audio_path"):
                self.line_audio_path.setText(saved_audio)
            # 重新同步保存按钮状态
            if hasattr(self, "_store") and self._store is not None:
                self.set_project(self._store.project)
        super().changeEvent(event)

    def set_project(self, project: Optional[Project]):
        """设置当前项目（启用/禁用保存按钮）"""
        self.btn_save.setEnabled(project is not None)

    def set_store(self, store):
        """接入 ProjectStore 统一数据中心。"""
        self._store = store
        store.data_changed.connect(self._on_data_changed)

    def _on_data_changed(self, change_type: str):
        """响应 ProjectStore 的数据变更。"""
        try:
            if change_type == "project":
                self.set_project(self._store.project)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[HomeInterface] _on_data_changed(%s) 失败: %s",
                change_type, e, exc_info=True)
            self._store.error_notify.emit("数据刷新异常", str(e))

    def dragEnterEvent(self, a0: Optional[QDragEnterEvent]):
        if a0 is None:
            return

        mime_data = a0.mimeData()
        if mime_data is None or not mime_data.hasUrls():
            a0.ignore()
            return

        file_paths = [
            url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()
        ]
        if any(self._is_supported_drop_file(path) for path in file_paths):
            a0.acceptProposedAction()
            return

        a0.ignore()

    def dropEvent(self, a0: Optional[QDropEvent]):
        if a0 is None:
            return

        mime_data = a0.mimeData()
        if mime_data is None:
            a0.ignore()
            return

        file_paths = [
            url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()
        ]
        if not file_paths:
            a0.ignore()
            return

        project_files = [path for path in file_paths if self._is_project_file(path)]
        if project_files:
            self._open_project_file(project_files[0])
            a0.acceptProposedAction()
            return

        lyric_files = [path for path in file_paths if self._is_lyric_file(path)]
        audio_files = [path for path in file_paths if self._is_audio_file(path)]

        imported_lines = 0
        for idx, file_path in enumerate(lyric_files):
            imported_lines += self._import_lyric_file(
                file_path,
                append=self.text_lyrics.toPlainText().strip() != "" or idx > 0,
                show_feedback=False,
            )

        if audio_files:
            self._set_audio_path(audio_files[0])

        if lyric_files or audio_files:
            parts = []
            if lyric_files:
                parts.append(
                    self.tr("已导入 {n} 个歌词文件，共 {lines} 行").format(
                        n=len(lyric_files), lines=imported_lines
                    )
                )
            if audio_files:
                audio_name = Path(audio_files[0]).name
                suffix = self.tr("（已使用第一个音频文件）") if len(audio_files) > 1 else ""
                parts.append(self.tr("音频: {name}{suffix}").format(
                    name=audio_name, suffix=suffix
                ))

            InfoBar.success(
                title=self.tr("导入成功"),
                content=self.tr("；").join(parts),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            a0.acceptProposedAction()
            return

        a0.ignore()

    def _init_ui(self):
        """初始化界面"""
        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(30)

        # 标题区
        title_layout = QVBoxLayout()
        title_layout.setSpacing(10)

        title = LargeTitleLabel("StrangeUtaGame")
        title_layout.addWidget(title)

        subtitle = SubtitleLabel(self.tr("歌词打轴工具"))
        title_layout.addWidget(subtitle)

        layout.addLayout(title_layout)

        # 主内容区 - 水平布局
        content_layout = QHBoxLayout()
        content_layout.setSpacing(30)

        # 左侧：创建新项目（占2份）
        create_card = self._create_new_project_card()
        content_layout.addWidget(create_card, 2)

        # 右侧：打开项目（占1份）
        open_card = self._create_open_project_card()
        content_layout.addWidget(open_card, 1)

        layout.addLayout(content_layout, 1)

    def _create_new_project_card(self) -> QWidget:
        """创建"新建项目"卡片"""
        card = SimpleCardWidget()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 30, 30, 30)
        card_layout.setSpacing(20)

        # 标题
        title = TitleLabel(self.tr("新建项目"))
        card_layout.addWidget(title)

        # 歌词输入区
        lyric_label = CaptionLabel(self.tr("歌词文本（支持粘贴或导入 LRC/ASS/SRT/TXT/TTML Beta）"))
        card_layout.addWidget(lyric_label)

        self.text_lyrics = TextEdit()
        self.text_lyrics.setPlaceholderText(self.tr(
            "在此粘贴歌词文本...\n"
            "支持格式：\n"
            "- 普通文本（每行一句）\n"
            "- LRC 格式（逐行/逐字/增强型）\n"
            "- ASS 字幕格式\n"
            "- SRT 字幕格式\n"
            "- KRA 格式\n"
            "- TTML / AMLL 格式（Beta）\n\n"
            "导入文件将自动填充此区域，创建项目时解析"
        ))
        self.text_lyrics.setMinimumHeight(200)
        card_layout.addWidget(self.text_lyrics)

        # 歌词操作按钮
        lyric_btn_layout = QHBoxLayout()
        lyric_btn_layout.setSpacing(10)

        self.btn_import_lyric = PushButton(self.tr("导入歌词文件"), self)
        self.btn_import_lyric.setIcon(FIF.FOLDER)
        self.btn_import_lyric.setToolTip(
            self.tr("支持 LRC/ASS/SRT/TXT/KRA/TTML（Beta）")
        )
        self.btn_import_lyric.clicked.connect(self._on_import_lyric)
        lyric_btn_layout.addWidget(self.btn_import_lyric)

        self.btn_clear_lyric = PushButton(self.tr("清空"), self)
        self.btn_clear_lyric.setIcon(FIF.DELETE)
        self.btn_clear_lyric.clicked.connect(self._on_clear_lyric)
        lyric_btn_layout.addWidget(self.btn_clear_lyric)

        lyric_btn_layout.addStretch()
        card_layout.addLayout(lyric_btn_layout)

        # 音频选择区
        audio_label = CaptionLabel(self.tr("音频文件（打轴需要，可后续添加）"))
        card_layout.addWidget(audio_label)

        audio_layout = QHBoxLayout()
        audio_layout.setSpacing(10)

        self.line_audio_path = LineEdit()
        self.line_audio_path.setPlaceholderText(self.tr("点击右侧按钮选择音频文件..."))
        self.line_audio_path.setReadOnly(True)
        audio_layout.addWidget(self.line_audio_path)

        self.btn_select_audio = PushButton(self.tr("选择音频"), self)
        self.btn_select_audio.setIcon(FIF.MUSIC)
        self.btn_select_audio.clicked.connect(self._on_select_audio)
        audio_layout.addWidget(self.btn_select_audio)

        card_layout.addLayout(audio_layout)

        # 创建按钮
        self.btn_create = PrimaryPushButton(self.tr("创建项目"), self)
        self.btn_create.setIcon(FIF.ADD)
        self.btn_create.setMinimumHeight(45)
        self.btn_create.clicked.connect(self._on_create_project)
        card_layout.addWidget(self.btn_create)

        return card

    def _create_open_project_card(self) -> QWidget:
        """创建"打开项目"卡片"""
        card = SimpleCardWidget()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 30, 30, 30)
        card_layout.setSpacing(20)

        # 标题
        title = TitleLabel(self.tr("打开项目"))
        card_layout.addWidget(title)

        # 说明
        desc = CaptionLabel(self.tr("打开已有的 .sug 项目文件"))
        card_layout.addWidget(desc)

        # 打开按钮
        self.btn_open = PrimaryPushButton(self.tr("打开项目"), self)
        self.btn_open.setIcon(FIF.FOLDER)
        self.btn_open.setMinimumHeight(50)
        self.btn_open.clicked.connect(self._on_open_project)
        card_layout.addWidget(self.btn_open)

        # 保存按钮
        self.btn_save = PushButton(self.tr("保存项目"), self)
        self.btn_save.setIcon(FIF.SAVE)
        self.btn_save.setMinimumHeight(45)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.project_save_requested.emit)
        card_layout.addWidget(self.btn_save)

        # 弹性空间
        card_layout.addStretch()

        # 提示信息
        tip = CaptionLabel(self.tr("提示：项目文件不包含音频，请确保音频文件可访问"))
        tip.setWordWrap(True)
        card_layout.addWidget(tip)

        return card

    def _on_import_lyric(self):
        """导入歌词文件"""
        init_dir = self._working_dir()
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择歌词文件"),
            init_dir,
            self.tr("歌词文件 (*.lrc *.txt *.kra *.krl *.ass *.srt *.ttml);;TTML（Beta） (*.ttml);;所有文件 (*.*)"),
        )

        if file_path:
            self._register_working_dir(file_path)
            self._import_lyric_file(
                file_path,
                append=self.text_lyrics.toPlainText().strip() != "",
                show_feedback=True,
            )

    def _on_clear_lyric(self):
        """清空歌词"""
        self.text_lyrics.clear()
        self._lyric_lines = []

    def _on_select_audio(self):
        """选择音频或视频文件"""
        init_dir = self._working_dir()
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择音频或视频文件"),
            init_dir,
            self.tr("音频/视频文件 (*.mp3 *.wav *.flac *.ogg *.mp4 *.mkv *.m4a *.avi *.mov *.wmv *.flv *.webm *.m4v *.mpg *.mpeg *.ts *.3gp *.vob *.mts *.m2ts *.rm *.rmvb *.asf *.f4v *.ogv *.m4b *.aac *.wma *.opus *.ape *.ac3 *.dts);;所有文件 (*.*)"),
        )

        if file_path:
            self._register_working_dir(file_path)
            if is_video_file(file_path):
                self._load_video_as_audio(file_path)
            else:
                self._set_audio_path(file_path)

    def _on_create_project(self):
        """创建项目"""
        # 获取歌词文本
        text = self.text_lyrics.toPlainText().strip()

        if not text:
            InfoBar.warning(
                title=self.tr("请输入歌词"),
                content=self.tr("歌词文本不能为空"),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return

        try:
            # 新建项目：重置全局 nicokara_tags 为默认值，避免上一项目残留。
            # _parse_text_content 中若检测到 nicokara 格式会重新写入正确的标签。
            _reset_nicokara_tags()

            # 解析文本框中的原始内容
            self._parse_text_content(text)
            if not self._lyric_lines and self._detect_format(text) == "ttml":
                InfoBar.warning(
                    title=self.tr("TTML 没有歌词"),
                    content=self.tr("没有解析到可导入的歌词行，未创建项目。"),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )
                return

            # 创建空项目
            project = self._project_service.create_project()
            default_singer = project.get_default_singer()

            # Nicokara / TTML 等带演唱者的导入：把临时 singer 映射到项目 singer。
            imported_singers = list(self._nicokara_singers) + list(self._ttml_singers)
            self._apply_imported_singers(
                project,
                self._lyric_lines,
                imported_singers,
                default_singer,
                reuse_placeholder=bool(self._ttml_singers)
                and not self._nicokara_singers,
            )

            if self._lyric_lines:
                # 使用已导入的歌词行（可能包含注音、时间标签等富数据）
                for line in self._lyric_lines:
                    project.add_sentence(line)
            else:
                # 从文本框手动输入的纯文本
                for line_text in text.split("\n"):
                    line_text = line_text.strip()
                    if line_text:
                        sentence = Sentence.from_text(line_text, default_singer.id)
                        project.add_sentence(sentence)

            # TTML/AMLL 元数据写回（仅填空字段，不覆盖用户已有信息）。
            self._apply_ttml_metadata(
                project,
                self._ttml_meta,
                set_language=True,
                set_extra=True,
            )
            warnings = self._ttml_meta.get("warnings")
            if warnings:
                InfoBar.warning(
                    title=self.tr("TTML 导入提示（Beta）"),
                    content="\n".join(str(item) for item in warnings),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=8000,
                    parent=self,
                )
            dropped_features = self._ttml_meta.get("dropped_features")
            if dropped_features:
                from strange_uta_game.frontend.editor.timing.ttml_notice import (
                    show_ttml_dropped_features,
                )

                show_ttml_dropped_features(self, dropped_features)

            # 自动分析注音并生成节奏点（仅对无注音的行）
            try:
                from strange_uta_game.frontend.settings.settings_interface import (
                    AppSettings,
                )

                app_settings = AppSettings()
                auto_check_flags = app_settings.get_all().get("auto_check", {})

                if auto_check_flags.get("auto_on_load", True):
                    # 检测是否为中文歌词（无任何假名）
                    from strange_uta_game.backend.application import (
                        is_chinese_lyrics,
                        is_korean_lyrics,
                    )
                    all_text = "".join(
                        s.text
                        for s in project.sentences
                        if not getattr(s, "aux_role", "")
                    )
                    chinese_mode = False
                    if auto_check_flags.get("chinese_lyrics_detection", True):
                        chinese_mode = is_chinese_lyrics(all_text)
                    # 韩文检测（无假名且含谚文）优先于中文分流
                    korean_mode = False
                    if auto_check_flags.get("korean_lyrics_detection", True):
                        korean_mode = is_korean_lyrics(all_text)

                    user_dict = app_settings.load_effective_dictionary()
                    annotate_katakana_with_english = app_settings.get(
                        "ruby_dictionary.annotate_katakana_with_english", False
                    )

                    llm_active = app_settings.llm_ruby_active()
                    # LLM 注音时是否仍应用用户词典（非 LLM 模式恒为 True）。
                    _apply_user_dict = (
                        app_settings.llm_apply_user_dict() if llm_active else True
                    )

                    auto_check = None
                    delete_types = auto_check_flags.get("delete_ruby_types", [])
                    if korean_mode:
                        # 韩文模式：导入即自动注音（风格 = 韩文注音风格设置）
                        from strange_uta_game.backend.infrastructure.parsers.korean_reading import (
                            create_korean_reading_analyzer,
                        )

                        auto_check = AutoCheckService(
                            auto_check_flags=auto_check_flags,
                            user_dictionary=user_dict,
                            annotate_katakana_with_english=annotate_katakana_with_english,
                            korean_mode=True,
                            korean_analyzer=create_korean_reading_analyzer(
                                auto_check_flags.get(
                                    "korean_annotation_style", "katakana"
                                )
                            ),
                        )
                        auto_check.analyze_and_apply_pipeline(
                            project, only_noruby=True, apply_user_dict=True,
                            delete_types=delete_types or None,
                        )
                    elif chinese_mode:
                        auto_check = AutoCheckService(
                            auto_check_flags=auto_check_flags,
                            user_dictionary=user_dict,
                            annotate_katakana_with_english=annotate_katakana_with_english,
                            chinese_mode=True,
                        )
                        auto_check.analyze_and_apply_pipeline(
                            project, only_noruby=True, apply_user_dict=True,
                            delete_types=delete_types or None,
                        )
                    else:
                        # LLM 注音激活时不需要本地日语 IME，跳过 WinRT 引导。
                        _proceed = True
                        if not llm_active:
                            from strange_uta_game.frontend.winrt_japanese_guide import (
                                ensure_winrt_japanese,
                            )
                            _proceed = ensure_winrt_japanese(self)
                        if _proceed:
                            # LLM 整首一次发送：只传主歌词行（副轴不参与注音）。
                            lines = [
                                s.text
                                for s in project.sentences
                                if not getattr(s, "aux_role", "")
                            ]
                            analyzer = app_settings.build_ruby_analyzer(
                                lines,
                                annotate_katakana_with_english=annotate_katakana_with_english,
                            )
                            auto_check = AutoCheckService(
                                ruby_analyzer=analyzer,
                                auto_check_flags=auto_check_flags,
                                user_dictionary=user_dict,
                                annotate_katakana_with_english=annotate_katakana_with_english,
                                korean_sub_analyzer=HomeInterface._build_korean_sub_analyzer(
                                    auto_check_flags
                                ),
                            )
                            auto_check.analyze_and_apply_pipeline(
                                project, only_noruby=True,
                                apply_user_dict=_apply_user_dict,
                                delete_types=delete_types or None,
                            )
                            if getattr(analyzer, "llm_failed", False):
                                InfoBar.warning(
                                    title=self.tr("LLM 注音失败，已回退本地引擎"),
                                    content=str(getattr(analyzer, "last_error", "") or ""),
                                    orient=Qt.Orientation.Horizontal,
                                    isClosable=True,
                                    position=InfoBarPosition.TOP,
                                    duration=5000,
                                    parent=self,
                                )
            except Exception:
                pass  # 注音分析失败不阻止项目创建

            # 发送信号（携带音频路径，可为空字符串）
            self.project_created.emit(project, self._audio_path or "")
            self._reset_form()

        except Exception as e:
            InfoBar.error(
                title=self.tr("创建失败"),
                content=str(e),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

    def _on_open_project(self):
        """打开项目"""
        init_dir = self._working_dir()
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("打开项目"),
            init_dir,
            self.tr("StrangeUtaGame 项目 (*.sug);;所有文件 (*.*)"),
        )

        if file_path:
            self._register_working_dir(file_path)
            self._open_project_file(file_path)

    def _is_supported_drop_file(self, file_path: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in (
            self._LYRIC_EXTENSIONS | self._PROJECT_EXTENSIONS | self._AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
        )

    def _is_lyric_file(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in self._LYRIC_EXTENSIONS

    def _is_project_file(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in self._PROJECT_EXTENSIONS

    def _is_audio_file(self, file_path: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in self._AUDIO_EXTENSIONS or suffix in VIDEO_EXTENSIONS

    def _import_lyric_file(
        self, file_path: str, append: bool = False, show_feedback: bool = True
    ) -> int:
        """导入歌词文件 — 仅读取原始内容显示到文本框，不进行解析

        解析延迟到点击"创建项目"时执行。
        """
        try:
            raw_content = self._read_raw_file(file_path)

            # 清除之前的解析状态（原始内容变了，旧的解析数据无效）
            if not append:
                self._lyric_lines = []
                self._nicokara_singers = []
                self._nicokara_singer_key_to_id = {}
                self._ttml_singers = []
                self._ttml_meta = {}

            self._set_lyrics_text(raw_content, append=append)

            line_count = len([l for l in raw_content.split("\n") if l.strip()])

            # 登记工作目录（拖拽 / 按钮导入歌词都会经过这里）
            self._register_working_dir(file_path)

            if show_feedback:
                InfoBar.success(
                    title=self.tr("导入成功"),
                    content=self.tr("已导入文件内容，共 {lines} 行").format(lines=line_count),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=3000,
                    parent=self,
                )

            return line_count
        except Exception as e:
            if show_feedback:
                InfoBar.error(
                    title=self.tr("导入失败"),
                    content=str(e),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                    parent=self,
                )
            return 0

    @staticmethod
    def _read_raw_file(file_path: str) -> str:
        """读取文件原始内容，支持 UTF-8 和 Shift-JIS 编码回退"""
        path = Path(file_path)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return path.read_text(encoding="shift_jis")
            except Exception as e:
                raise RuntimeError(f"无法解码文件: {e}")
        except Exception as e:
            raise RuntimeError(f"读取文件失败: {e}")

    def _detect_format(self, content: str) -> str:
        """从文本内容自动检测歌词格式

        返回格式标识: 'ttml', 'krl', 'inline', 'nicokara', 'ass', 'srt',
        'lrc', 'text'
        """
        import re

        # TTML / AMLL TTML：根元素 <tt>。
        if is_ttml_content(content):
            return "ttml"

        # KRL 也使用 {text|ruby}，必须在通用 inline 之前检测。
        if is_kasugamuki_content(content):
            return "krl"

        # 内联格式: [N|MM:SS:cc] 或 {字|...}
        if self._is_inline_format(content):
            return "inline"

        # Nicokara 格式: 【svN】 标签或 @Ruby/@Emoji
        if NicokaraParser.is_nicokara_format(content):
            return "nicokara"

        # ASS 格式: [Script Info] 或 Dialogue: 行
        if re.search(r"^\[Script Info\]", content, re.MULTILINE) or re.search(
            r"^Dialogue:\s*\d+", content, re.MULTILINE
        ):
            return "ass"

        # SRT 格式: 含有 --> 时间戳分隔符
        if re.search(
            r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", content
        ):
            return "srt"

        # LRC 格式: [mm:ss.xx] 时间标签
        if self._has_lrc_timestamps(content):
            return "lrc"

        # 纯文本
        return "text"

    @staticmethod
    def _apply_imported_singers(
        project: Project,
        lines: List[Sentence],
        imported_singers: List[Singer],
        default_singer: Singer,
        *,
        reuse_placeholder: bool = False,
    ) -> dict:
        """把导入格式的临时 Singer 映射到项目 Singer（Nicokara/TTML 共用）。

        reuse_placeholder 只给 TTML 用：新项目默认的「未命名」占位歌手直接
        复用为第一个导入歌手，避免 ghost singer。Nicokara 保持旧行为
        （保留占位歌手 + 新增导入歌手），以免改变既有导入结果。
        """
        singer_map: dict = {}
        pending_singers = list(imported_singers)
        if (
            reuse_placeholder
            and pending_singers
            and getattr(default_singer, "is_placeholder", False)
            and not getattr(pending_singers[0], "ttml_role", "")
            and not any(
                s.name == pending_singers[0].name for s in project.singers
            )
        ):
            first = pending_singers.pop(0)
            default_singer.rename(first.name)
            default_singer.change_color(first.color)
            default_singer.adopt_ttml_agent(first)
            singer_map[first.id] = default_singer.id

        for imported in pending_singers:
            existing = next(
                (s for s in project.singers if s.name == imported.name), None
            )
            if existing is not None:
                # 已有同名演唱者：保留用户数据，只补 TTML 来源信息。
                if not existing.ttml_agent_id and imported.ttml_agent_id:
                    existing.adopt_ttml_agent(imported)
                if not existing.ttml_role and imported.ttml_role:
                    existing.ttml_role = imported.ttml_role
                singer_map[imported.id] = existing.id
            else:
                singer = Singer(
                    name=imported.name,
                    color=imported.color,
                    is_default=False,
                    ttml_agent_id=imported.ttml_agent_id,
                    ttml_agent_type=imported.ttml_agent_type,
                    ttml_agent_named=imported.ttml_agent_named,
                    ttml_agent_display_name=imported.ttml_agent_display_name,
                    ttml_agent_source_name=imported.ttml_agent_source_name,
                    ttml_role=imported.ttml_role,
                )
                project.add_singer(singer)
                singer_map[imported.id] = singer.id

        if not lines:
            return singer_map
        if not singer_map:
            for line in lines:
                line.singer_id = default_singer.id
                for char in line.characters:
                    char.singer_id = default_singer.id
                    if char.ruby:
                        char.ruby.singer_id = default_singer.id
            return singer_map

        for line in lines:
            line.singer_id = singer_map.get(line.singer_id, default_singer.id)
            for char in line.characters:
                char.singer_id = singer_map.get(
                    char.singer_id, default_singer.id
                )
                if char.ruby:
                    char.ruby.singer_id = char.singer_id
        return singer_map

    @staticmethod
    def _apply_ttml_metadata(
        project: Project,
        meta: dict,
        *,
        set_language: bool = False,
        set_extra: bool = False,
    ) -> None:
        """把 TTML 的 title/artist/album/language 写回空项目元数据。"""
        if not meta or project.metadata is None:
            return
        metadata = project.metadata
        for field_name in ("title", "artist", "album"):
            value = str(meta.get(field_name) or "").strip()
            if not value:
                continue
            current = str(getattr(metadata, field_name, "") or "").strip()
            if not current or (field_name == "title" and current in ("Untitled", "未命名")):
                setattr(metadata, field_name, value)
        language = str(meta.get("language") or "").strip()
        if language and set_language:
            metadata.language = language
        extra = meta.get("extra")
        if set_extra and isinstance(extra, dict) and isinstance(metadata.extra, dict):
            metadata.extra.update(extra)

    def _parse_text_content(self, content: str) -> None:
        """根据检测到的格式解析文本内容，设置 _lyric_lines 等实例变量

        在创建项目时调用，将文本框中的原始内容解析为 Sentence 对象。
        """
        fmt = self._detect_format(content)
        self._nicokara_singers = []
        self._nicokara_singer_key_to_id = {}
        self._ttml_singers = []
        self._ttml_meta = {}
        temp_singer = Singer(name="临时", is_default=True)

        if fmt == "krl":
            self._lyric_lines = sentences_from_kasugamuki(content, temp_singer.id)
            return

        if fmt == "inline":
            self._lyric_lines = sentences_from_inline_text(content, temp_singer.id)
            return

        if fmt == "nicokara":
            self._lyric_lines = self._parse_nicokara_content(content)
            return

        if fmt == "ttml":
            from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
                TTMLParser,
                build_aux_singer_specs,
                build_ttml_sentences,
            )

            parser = TTMLParser()
            document = parser.parse_document(content)
            singer_name_to_id: dict = {}
            colors = [
                "#FF6B6B", "#4ECDC4", "#45B7D1", "#FFA07A", "#98D8C8",
                "#C9B1FF", "#F7DC6F", "#82E0AA", "#F1948A", "#85C1E9",
            ]
            # 按 head/body 中第一次出现的顺序建 Singer，保留源 agent id/type/
            # 是否命名；同名 agent 只建一个 Singer。
            seen_names: set = set()
            ordered_agents = []
            for agent in document.agents.values():
                if agent.name and agent.name not in seen_names:
                    seen_names.add(agent.name)
                    ordered_agents.append(agent)
            for idx, agent in enumerate(ordered_agents):
                singer = Singer(
                    name=agent.name,
                    color=colors[idx % len(colors)],
                    is_default=False,
                    ttml_agent_id=agent.id,
                    ttml_agent_type=agent.type,
                    ttml_agent_named=agent.named,
                    ttml_agent_display_name=agent.name,
                    ttml_agent_source_name=agent.source_name,
                )
                self._ttml_singers.append(singer)
                singer_name_to_id[agent.name] = singer.id

            # 辅助角色（翻译/音译/和声）也建 Singer，成为可分组/分轴的副轴。
            aux_specs, aux_name_by_key = build_aux_singer_specs(document)
            for offset, (name, role) in enumerate(aux_specs):
                if name in singer_name_to_id:
                    continue
                singer = Singer(
                    name=name,
                    color=colors[
                        (len(ordered_agents) + offset) % len(colors)
                    ],
                    is_default=False,
                    ttml_role=role,
                )
                self._ttml_singers.append(singer)
                singer_name_to_id[name] = singer.id

            self._lyric_lines = build_ttml_sentences(
                document,
                temp_singer.id,
                singer_name_to_id,
                aux_name_by_key=aux_name_by_key,
            )
            self._ttml_meta = parser.parse_metadata()
            if document.warnings:
                self._ttml_meta["warnings"] = list(document.warnings)
            return

        if fmt == "ass":
            from strange_uta_game.backend.infrastructure.parsers.ass_parser import (
                ASSParser,
            )

            parser = ASSParser()
            parsed_lines = parser.parse(content)
            self._lyric_lines = parse_to_sentences(parsed_lines, temp_singer.id)
            return

        if fmt == "srt":
            from strange_uta_game.backend.infrastructure.parsers.srt_parser import (
                SRTParser,
            )

            parser = SRTParser()
            parsed_lines = parser.parse(content)
            self._lyric_lines = parse_to_sentences(parsed_lines, temp_singer.id)
            return

        if fmt == "lrc":
            lrc_parser = LRCParser()
            parsed_lines = lrc_parser.parse(content)
            self._lyric_lines = parse_to_sentences(parsed_lines, temp_singer.id)
            return

        # text: 纯文本，不预解析，让 _on_create_project 中的文本分行处理
        self._lyric_lines = []

    def _parse_nicokara_content(self, content: str) -> List[Sentence]:
        """解析 Nicokara 格式内容，自动创建不存在的演唱者

        Returns:
            Sentence 列表（含 per-char singer_id）
        """
        parser = NicokaraParser()
        result = parser.parse(content)

        # SHINTA 2025 规格透明性 (差异 K)：把解析到的 @ 元数据写回 AppSettings.nicokara_tags。
        # 与打轴页"加载歌词文件"路径保持一致——见 lyric_loader._sync_nicokara_metadata_to_settings。
        # 传入 SettingsInterface 共享实例，避免新建 AppSettings() 改不到长寿命 _settings
        # 内存中的旧值（启动后续 _settings.save() 会用旧值覆盖磁盘）。
        from strange_uta_game.frontend.editor.timing.lyric_loader import (
            _sync_nicokara_metadata_to_settings,
        )
        setting_iface = None
        try:
            setting_iface = getattr(self.window(), "settingInterface", None)
        except Exception:
            pass
        _sync_nicokara_metadata_to_settings(
            result.metadata, setting_iface=setting_iface
        )

        # 收集所有出现的 singer_key
        all_singer_keys: set = set()
        for singer_key in result.singer_definitions:
            all_singer_keys.add(singer_key)
        for line in result.lines:
            if line.line_singer_key:
                all_singer_keys.add(line.line_singer_key)
            for _, sk in line.char_singer_map.items():
                all_singer_keys.add(sk)

        # 为每个 singer_key 创建 Singer 对象
        # 使用预定义颜色循环
        singer_colors = [
            "#FF6B6B",
            "#4ECDC4",
            "#45B7D1",
            "#FFA07A",
            "#98D8C8",
            "#C9B1FF",
            "#F7DC6F",
            "#82E0AA",
            "#F1948A",
            "#85C1E9",
        ]
        singer_key_to_id: dict = {}
        temp_singers: list = []
        for idx, singer_key in enumerate(sorted(all_singer_keys)):
            # 使用 singer_definitions 中的显示名，否则用 singer_key
            display_name = (
                result.singer_definitions.get(singer_key, singer_key) or singer_key
            )
            color = singer_colors[idx % len(singer_colors)]
            singer = Singer(
                name=display_name,
                color=color,
                is_default=(idx == 0),
            )
            singer_key_to_id[singer_key] = singer.id
            temp_singers.append(singer)

        # 如果没有任何演唱者定义，使用临时默认演唱者
        if not singer_key_to_id:
            temp_singer = Singer(name="临时", is_default=True)
            default_singer_id = temp_singer.id
        else:
            # 使用第一个演唱者作为默认
            default_singer_id = list(singer_key_to_id.values())[0]

        # 保存解析出的演唱者信息到实例变量，供 _on_create_project 使用
        self._nicokara_singers = temp_singers
        self._nicokara_singer_key_to_id = singer_key_to_id

        lyric_lines = nicokara_result_to_sentences(
            result, singer_key_to_id, default_singer_id
        )
        return lyric_lines

    @staticmethod
    def _has_lrc_timestamps(content: str) -> bool:
        """检测文本是否包含标准 LRC 时间标签 [mm:ss.xx]"""
        import re

        return bool(re.search(r"\[\d{1,2}:\d{2}[.:]\d{2,3}\]", content))

    @staticmethod
    def _is_inline_format(content: str) -> bool:
        """检测文本是否为 RhythmicaLyrics 内联格式。

        内联格式特征：含有 [N|MM:SS:cc] 模式（带 checkpoint 编号的时间标签），
        或含有 {漢字|...} 注音组标记。
        """
        import re

        # 检测 [N|MM:SS:cc] 模式 — 内联格式特有
        if re.search(r"\[\d+\|\d{2}:\d{2}:\d{2}\]", content):
            return True
        # 检测 {字|...} ruby 组标记
        if re.search(r"\{.+?\|.+?\}", content):
            return True
        return False

    def _create_lyric_lines_from_entries(self, line_entries: List) -> List[Sentence]:
        temp_singer = Singer(name="临時", is_default=True)
        sentences: List[Sentence] = []
        for line_data in line_entries:
            text = line_data.text.strip()
            if text:
                sentences.append(Sentence.from_text(text, temp_singer.id))
        return sentences

    def _set_lyrics_text(self, text: str, append: bool = False) -> None:
        if append and self.text_lyrics.toPlainText().strip():
            self.text_lyrics.append(text)
            return

        self.text_lyrics.setPlainText(text)

    def _set_audio_path(self, file_path: str) -> None:
        self._audio_path = file_path
        self.line_audio_path.setText(file_path)
        if hasattr(self, "_store") and self._store:
            self._store.set_audio_path(file_path)

    def _working_dir(self) -> str:
        """返回当前工作目录（用作 QFileDialog 初始路径）。"""
        if hasattr(self, "_store") and self._store:
            return self._store.working_dir or ""
        return ""

    def _register_working_dir(self, file_path: str) -> None:
        """把刚刚加载/选中的文件目录登记为全局工作目录并持久化。"""
        if not file_path:
            return
        if hasattr(self, "_store") and self._store:
            self._store.set_working_dir(file_path)

    def _load_video_as_audio(self, file_path: str) -> None:
        """异步加载视频文件，提取音频并设置路径"""
        from strange_uta_game.frontend.theme import theme

        # 检查 FFmpeg 是否可用
        if not is_ffmpeg_available():
            # embedded 下「工具配置」入口被隐藏（EMBEDDING §5），指引工作台
            if is_embedded():
                content = self.tr("未检测到 FFmpeg。嵌入式运行的 FFmpeg 由工作台统一管理，请检查工作台设置中的 FFmpeg 配置。")
            else:
                content = self.tr("未检测到 FFmpeg，请在「设置 → 关于/语言 → 工具配置」中浏览并设置 FFmpeg 路径。")
            InfoBar.error(
                title=self.tr("无法读取视频文件"),
                content=content,
                orient=Qt.Orientation.Horizontal, isClosable=True,
                position=InfoBarPosition.TOP, duration=7000,
                parent=self,
            )
            return

        # 创建状态提示
        self._state_tooltip = StateToolTip(self.tr("正在处理视频"), self.tr("正在检查 FFmpeg 环境..."), self)
        green = theme.status_complete.name()
        self._state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        self._state_tooltip.move(self._state_tooltip.getSuitablePos())
        self._state_tooltip.show()

        # 创建后台线程
        from strange_uta_game.frontend.workers import VideoExtractOnlyWorker

        self._loading_thread = QThread(self)
        self._loading_worker = VideoExtractOnlyWorker(file_path)
        self._loading_worker.moveToThread(self._loading_thread)

        # 连接信号
        self._loading_thread.started.connect(self._loading_worker.run)
        self._loading_worker.progress.connect(self._on_video_progress)
        self._loading_worker.finished.connect(lambda path: self._on_video_extracted(path, file_path))
        self._loading_worker.error.connect(self._on_video_error)
        self._loading_worker.finished.connect(self._cleanup_loading_thread)
        self._loading_worker.error.connect(self._cleanup_loading_thread)

        # 启动线程
        self._loading_thread.start()

    def _on_video_progress(self, stage: str, value: float) -> None:
        """更新视频处理进度"""
        if self._state_tooltip:
            self._state_tooltip.setContent(stage)

    def _on_video_extracted(self, temp_path: str, original_path: str) -> None:
        """视频提取完成回调"""
        if self._state_tooltip:
            self._state_tooltip.setState(True)
            self._state_tooltip.setContent(self.tr("加载完成"))
            self._state_tooltip.close()
            self._state_tooltip = None

        self._set_audio_path(temp_path)
        self._temp_audio_path = temp_path

        InfoBar.success(
            title=self.tr("音频提取成功"),
            content=self.tr("已从视频中提取音频: {name}").format(name=Path(original_path).name),
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=3000,
            parent=self,
        )

    def _on_video_error(self, error_msg: str) -> None:
        """视频处理失败回调"""
        if self._state_tooltip:
            self._state_tooltip.close()
            self._state_tooltip = None

        InfoBar.error(
            title=self.tr("视频处理失败"),
            content=error_msg,
            orient=Qt.Orientation.Horizontal, isClosable=True,
            position=InfoBarPosition.TOP, duration=5000,
            parent=self,
        )

    def _reset_form(self) -> None:
        self.text_lyrics.clear()
        self.line_audio_path.clear()
        self._audio_path = None
        self._lyric_lines = []
        self._nicokara_singers = []
        self._nicokara_singer_key_to_id = {}
        self._ttml_singers = []
        self._ttml_meta = {}

    @staticmethod
    def _build_korean_sub_analyzer(auto_check_flags: dict):
        """构造日文模式下谚文补标分析器（风格取自「韩文注音风格」设置）。

        受「韩文歌词检测」开关门控——关闭时返回 None，谚文回落日文路径。
        仅对谚文音节补标，汉字/假名/英文不动。
        """
        if not auto_check_flags.get("korean_lyrics_detection", True):
            return None
        try:
            from strange_uta_game.backend.infrastructure.parsers.korean_reading import (
                create_korean_reading_analyzer,
            )
        except ImportError:
            return None
        return create_korean_reading_analyzer(
            auto_check_flags.get("korean_annotation_style", "katakana")
        )

    def _open_project_file(self, file_path: str) -> None:
        """异步加载 .sug 项目文件"""
        from strange_uta_game.frontend.theme import theme

        # 创建状态提示
        self._state_tooltip = StateToolTip(self.tr("正在加载项目"), self.tr("正在解析项目数据..."), self)
        green = theme.status_complete.name()
        self._state_tooltip.setStyleSheet(f"""
            StateToolTip {{
                background-color: {green};
                border: 1px solid {green};
                border-radius: 8px;
            }}
            StateToolTip QLabel {{
                color: white;
            }}
        """)
        self._state_tooltip.move(self._state_tooltip.getSuitablePos())
        self._state_tooltip.show()

        # 创建后台线程
        from strange_uta_game.frontend.workers import ProjectLoadWorker

        self._loading_thread = QThread(self)
        self._loading_worker = ProjectLoadWorker(file_path)
        self._loading_worker.moveToThread(self._loading_thread)

        # 连接信号
        self._loading_thread.started.connect(self._loading_worker.run)
        self._loading_worker.finished.connect(self._on_project_load_success)
        self._loading_worker.error.connect(self._on_project_load_error)
        self._loading_worker.finished.connect(self._cleanup_loading_thread)
        self._loading_worker.error.connect(self._cleanup_loading_thread)

        # 启动线程
        self._loading_thread.start()

    def _on_project_load_success(self, project: Project, file_path: str, extras: dict = None) -> None:
        """项目加载成功回调"""
        if self._state_tooltip:
            self._state_tooltip.setState(True)
            self._state_tooltip.setContent(self.tr("加载完成"))
            self._state_tooltip.close()
            self._state_tooltip = None

        self.project_opened.emit(project, file_path, extras or {})

    def _on_project_load_error(self, error_msg: str) -> None:
        """项目加载失败回调"""
        if self._state_tooltip:
            self._state_tooltip.close()
            self._state_tooltip = None

        InfoBar.error(
            title=self.tr("打开失败"),
            content=error_msg,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self,
        )

    def _cleanup_loading_thread(self) -> None:
        """清理加载线程"""
        if self._loading_thread:
            self._loading_thread.quit()
            self._loading_thread.wait()
            self._loading_thread = None
        if self._loading_worker:
            self._loading_worker.deleteLater()
            self._loading_worker = None
