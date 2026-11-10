"""standalone 导出 × 轴分组拆分测试。

覆盖导出页「轴分组设置...」写回后，普通「导出」按钮的按组拆分行为：
- 多组：按组导出多个文件，文件名追加 ``_分组名``；
- 每组文件按该组演唱者过滤内容；
- @Emoji 标签按该组实际演唱者的触发词过滤；
- 主分组文件携带完整标签信息（@Title / 非 @Emoji custom），非主分组
  只带本组 @Emoji（计时字段保留）。
"""

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt

from strange_uta_game.backend.domain import (
    Project,
    Singer,
    Sentence,
    Character,
    AxisGroup,
)
from strange_uta_game.backend.infrastructure.parsers.ttml_parser import (
    TTMLParser,
)
from strange_uta_game.frontend.export.export_interface import ExportInterface
from strange_uta_game.frontend.settings.settings_interface import AppSettings

FMT_NICOKARA_RUBY = "Nicokara (带注音)"


def _drain_export(page, qapp, timeout_s: float = 30.0) -> None:
    """等待后台导出线程完成并处理完回城信号（导出已移入 QThread）。"""
    import time

    deadline = time.monotonic() + timeout_s
    while getattr(page, "_exporting", False) and time.monotonic() < deadline:
        qapp.processEvents()
        thread = getattr(page, "_export_thread", None)
        if thread is not None:
            thread.wait(50)
    qapp.processEvents()
    thread = getattr(page, "_export_thread", None)
    if thread is not None:
        thread.wait(5000)
    assert getattr(page, "_exporting", False) is False, "导出线程超时未完成"


@pytest.fixture(autouse=True)
def _restore_nicokara_tags():
    """本文件会改写 AppSettings 的 nicokara_tags；会话级隔离目录不隔离
    单个测试，必须还原，避免泄漏给同会话后续的 exporter 测试。"""
    settings = AppSettings()
    original = settings.get("nicokara_tags")
    yield
    settings.set("nicokara_tags", original if original is not None else {})
    settings.save()


def _timed_sentence(text: str, singer_id: str) -> Sentence:
    sentence = Sentence.from_text(text, singer_id)
    for i, ch in enumerate(sentence.characters):
        ch.singer_id = singer_id
        ch.set_check_count(1, force=True)
        ch.add_timestamp(1000 + i * 500)
    sentence.characters[-1].set_sentence_end_ts(1000 + len(sentence.characters) * 500)
    return sentence


def _make_page(qapp, tmp_path) -> tuple:
    project = Project(
        singers=[
            Singer(name="A", color="#FF0000", is_default=True),
            Singer(name="B", color="#00FF00"),
        ]
    )
    a, b = project.singers[0].id, project.singers[1].id
    project.add_sentence(_timed_sentence("あいう", a))
    project.add_sentence(_timed_sentence("えお", b))

    page = ExportInterface()
    page.set_project(project)
    page._refresh_axis_group_summary()
    page._store = None
    page.line_output.setText(str(tmp_path))
    page.line_filename.setText("song")

    # 选中 Nicokara（带注音）
    for i in range(page.format_list.count()):
        item = page.format_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == FMT_NICOKARA_RUBY:
            page.format_list.setCurrentRow(i)
            break
    else:
        pytest.fail(f"找不到格式 {FMT_NICOKARA_RUBY}")

    # 配置 Nicokara 标签：元数据 + 两位演唱者的 @Emoji + 一条非 @Emoji custom
    settings = AppSettings()
    settings.set(
        "nicokara_tags",
        {
            "title": "曲名",
            "artist": "歌手",
            "tagging_by": "打轴人",
            "silence_ms": 500,
            "offset": 120,
            "custom": [
                "@Emoji=【A】,透明画像1x1.png,,Zoom=1,NoDecor",
                "@Emoji=【B】,透明画像1x1.png,,Zoom=1,NoDecor",
                "@Custom=通用装饰",
            ],
        },
    )
    settings.save()
    return page, project, (a, b)


class TestAxisGroupStandaloneExport:
    def test_multi_group_export_splits_files_with_suffix(self, qapp, tmp_path):
        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )

        page._on_export()
        _drain_export(page, qapp)

        f1 = tmp_path / "song_轴1.lrc"
        f2 = tmp_path / "song_轴2.lrc"
        assert f1.is_file() and f2.is_file()
        assert not (tmp_path / "song.lrc").exists()

        text1 = f1.read_text(encoding="utf-8")
        text2 = f2.read_text(encoding="utf-8")
        # 内容按组过滤（字符间有时间戳，按单字断言）
        assert "あ" in text1 and "う" in text1 and "え" not in text1
        assert "え" in text2 and "お" in text2 and "あ" not in text2
        # @Emoji 按本组实际演唱者的触发词过滤
        assert "@Emoji=【A】" in text1 and "@Emoji=【B】" not in text1
        assert "@Emoji=【B】" in text2 and "@Emoji=【A】" not in text2
        # 主分组（轴1）携带完整标签信息；非主分组只留 @Emoji + 计时字段
        assert "@Title=曲名" in text1 and "@Artist=歌手" in text1
        assert "@TaggingBy=打轴人" in text1 and "@Custom=通用装饰" in text1
        assert "@Title" not in text2 and "@Artist" not in text2
        assert "@TaggingBy" not in text2 and "@Custom=通用装饰" not in text2
        # 计时字段两组都保留（缺了会破坏时间轴）
        assert "@SilencemSec=500" in text1 and "@SilencemSec=500" in text2
        assert "@Offset=+120" in text1 and "@Offset=+120" in text2

    def test_single_group_export_keeps_filename(self, qapp, tmp_path):
        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups([AxisGroup(name="轴1", singer_ids=[a])])

        page._on_export()
        _drain_export(page, qapp)

        f1 = tmp_path / "song.lrc"
        assert f1.is_file()
        assert not (tmp_path / "song_轴1.lrc").exists()
        text = f1.read_text(encoding="utf-8")
        # 单组：按该组过滤 + 完整标签（该组即主分组）
        assert "あ" in text and "え" not in text
        assert "@Title=曲名" in text

    def test_no_groups_exports_single_file_unfiltered(self, qapp, tmp_path):
        page, project, _ids = _make_page(qapp, tmp_path)

        page._on_export()
        _drain_export(page, qapp)

        f = tmp_path / "song.lrc"
        assert f.is_file()
        text = f.read_text(encoding="utf-8")
        assert "あ" in text and "え" in text

    def test_overwrite_prompt_merged_into_one_dialog(self, qapp, tmp_path, monkeypatch):
        """多个目标文件已存在时覆盖确认只弹一次，确认后全部覆盖。"""
        import strange_uta_game.frontend.export.export_interface as ei_mod

        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )
        # 预置两个同名目标文件
        for name in ("song_轴1.lrc", "song_轴2.lrc"):
            (tmp_path / name).write_text("OLD", encoding="utf-8")

        prompts = []
        monkeypatch.setattr(
            ei_mod,
            "message_question",
            lambda *args, **kwargs: prompts.append(args) or True,
        )

        page._on_export()
        _drain_export(page, qapp)

        assert len(prompts) == 1, "覆盖确认应合并为一次弹出"
        # 两个文件名都在同一次提示里
        listed = prompts[0][2]
        assert "song_轴1.lrc" in listed and "song_轴2.lrc" in listed
        # 确认覆盖 → 两个文件都被重写
        assert (tmp_path / "song_轴1.lrc").read_text(encoding="utf-8") != "OLD"
        assert (tmp_path / "song_轴2.lrc").read_text(encoding="utf-8") != "OLD"

    def test_overwrite_prompt_cancel_aborts_export(self, qapp, tmp_path, monkeypatch):
        """合并提示中取消 → 整体中止，已有文件保持原样。"""
        import strange_uta_game.frontend.export.export_interface as ei_mod

        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )
        for name in ("song_轴1.lrc", "song_轴2.lrc"):
            (tmp_path / name).write_text("OLD", encoding="utf-8")

        prompts = []
        monkeypatch.setattr(
            ei_mod,
            "message_question",
            lambda *args, **kwargs: prompts.append(args) or False,
        )

        page._on_export()
        _drain_export(page, qapp)

        assert len(prompts) == 1
        assert (tmp_path / "song_轴1.lrc").read_text(encoding="utf-8") == "OLD"
        assert (tmp_path / "song_轴2.lrc").read_text(encoding="utf-8") == "OLD"

    def test_empty_group_exports_all_singers(self, qapp, tmp_path):
        """组内不勾选任何演唱者 = 该轴包含全部演唱者。"""
        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[]),  # 空 = 全部（主分组）
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )

        page._on_export()
        _drain_export(page, qapp)

        f1 = tmp_path / "song_轴1.lrc"
        f2 = tmp_path / "song_轴2.lrc"
        assert f1.is_file() and f2.is_file()
        text1 = f1.read_text(encoding="utf-8")
        # 全部演唱者的内容都在，@Emoji 两位都保留，且带完整标签（主分组）
        assert "あ" in text1 and "え" in text1
        assert "@Emoji=【A】" in text1 and "@Emoji=【B】" in text1
        assert "@Title=曲名" in text1 and "@Custom=通用装饰" in text1

    def test_non_singer_format_ignores_axis_groups(self, qapp, tmp_path):
        """不支持演唱者过滤的格式（LRC）忽略轴分组，导出单文件。"""
        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )
        for i in range(page.format_list.count()):
            item = page.format_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) and "LRC" in item.data(
                Qt.ItemDataRole.UserRole
            ):
                page.format_list.setCurrentRow(i)
                break

        page._on_export()
        _drain_export(page, qapp)

        files = sorted(p.name for p in tmp_path.glob("song*.lrc"))
        assert files == ["song.lrc"]

    def test_duplicate_sanitized_group_filenames_abort_export(self, qapp, tmp_path):
        """不同分组名 sanitize 后撞名 → 导出前中止，不写任何文件（H10）。"""
        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="A/B", singer_ids=[a]),  # sanitize → A_B
                AxisGroup(name="A_B", singer_ids=[b]),  # 撞名
            ]
        )

        page._on_export()
        _drain_export(page, qapp)

        assert page._exporting is False
        assert list(tmp_path.glob("*.lrc")) == [], "撞名应整体中止，不落任何文件"

    def test_failed_job_cleans_tmp_and_rest_continue(self, qapp, tmp_path, monkeypatch):
        """单任务失败：.tmp 被清理、正式目标不残留，其余任务继续（H3）。"""
        from strange_uta_game.backend.application.export_service import ExportResult

        page, project, (a, b) = _make_page(qapp, tmp_path)
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )

        service = page._export_service
        real_export = service.export
        state = {"n": 0}

        def fake_export(project_, format_name, file_path, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                # 模拟导出器写一半后失败：留下 .tmp 且不产生正式文件
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write("PARTIAL")
                return ExportResult(success=False, error_message="boom")
            return real_export(project_, format_name, file_path, **kwargs)

        monkeypatch.setattr(service, "export", fake_export)

        page._on_export()
        _drain_export(page, qapp)

        assert not (tmp_path / "song_轴1.lrc").exists()
        assert not (tmp_path / "song_轴1.tmp.lrc").exists(), "失败后临时文件应被清理"
        assert (tmp_path / "song_轴2.lrc").is_file(), "其余任务应继续完成"
        assert page._exporting is False

    def test_emoji_trigger_matching_supports_bare_name(self, qapp):
        """触发词按【名】或裸名匹配（手写 custom 行可能不带括号）。"""
        page, project, (a, _b) = _make_page(qapp, Path("X:/nonexistent-tmp"))
        group = AxisGroup(name="轴1", singer_ids=[a])
        page._project = project

        # 带括号触发词：本组演唱者 A 的行保留
        data = page._build_axis_tag_data(group, is_primary=False)
        assert data["custom"] == ["@Emoji=【A】,透明画像1x1.png,,Zoom=1,NoDecor"]

        # 裸名触发词同样匹配
        settings = AppSettings()
        tags = settings.get("nicokara_tags")
        tags["custom"] = [
            "@Emoji=A,透明画像1x1.png,,Zoom=1",
            "@Emoji=B,透明画像1x1.png,,Zoom=1",
        ]
        settings.save()
        data = page._build_axis_tag_data(group, is_primary=False)
        assert data["custom"] == ["@Emoji=A,透明画像1x1.png,,Zoom=1"]


def _select_format(page, name: str) -> None:
    for i in range(page.format_list.count()):
        item = page.format_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            page.format_list.setCurrentRow(i)
            return
    pytest.fail(f"找不到格式 {name}")


class TestTtmlAxisGroupExport:
    def test_ttml_multi_group_export_splits_files(self, qapp, tmp_path):
        page, project, (a, b) = _make_page(qapp, tmp_path)
        _select_format(page, "TTML")
        project.set_axis_groups(
            [
                AxisGroup(name="轴1", singer_ids=[a]),
                AxisGroup(name="轴2", singer_ids=[b]),
            ]
        )

        page._on_export()
        _drain_export(page, qapp)

        f1 = tmp_path / "song_轴1.ttml"
        f2 = tmp_path / "song_轴2.ttml"
        assert f1.is_file() and f2.is_file()
        doc1 = TTMLParser().parse_document(f1.read_text(encoding="utf-8"))
        doc2 = TTMLParser().parse_document(f2.read_text(encoding="utf-8"))
        assert "".join(line.text for line in doc1.lines) == "あいう"
        assert "".join(line.text for line in doc2.lines) == "えお"
        assert {agent.name for agent in doc1.agents.values()} == {"A"}
        assert {agent.name for agent in doc2.agents.values()} == {"B"}

    def test_ttml_axis_groups_decide_aux_merge(
        self, qapp, tmp_path, monkeypatch
    ):
        import strange_uta_game.frontend.export.export_interface as ei_mod

        # 三次导出写同一目标文件；覆盖确认统一返回 True，避免模态框挂住。
        monkeypatch.setattr(
            ei_mod, "message_question", lambda *args, **kwargs: True
        )
        page, project, (a, _b) = _make_page(qapp, tmp_path)
        _select_format(page, "TTML")
        translator = Singer(
            name="翻译", color="#45B7D1", ttml_role="x-translation"
        )
        project.add_singer(translator)
        parent = project.sentences[0]
        parent.song_part = "Verse"
        aux = Sentence.from_text("翻译", translator.id)
        aux.aux_role = "x-translation"
        aux.aux_parent_id = parent.id
        aux.aux_lang = "zh-CN"
        project.sentences.insert(1, aux)

        # 轴分组只含主唱：副轴不进入文件。
        project.set_axis_groups([AxisGroup(name="主唱", singer_ids=[a])])
        page._on_export()
        _drain_export(page, qapp)
        text = (tmp_path / "song.ttml").read_text(encoding="utf-8")
        assert 'ttm:role="x-translation"' not in text
        assert "翻译" not in text
        assert 'song-part="Verse"' in text

        # 主唱 + 翻译同组：翻译合并回主行的 x-translation span。
        project.set_axis_groups(
            [AxisGroup(name="主唱", singer_ids=[a, translator.id])]
        )
        page._on_export()
        _drain_export(page, qapp)
        text = (tmp_path / "song.ttml").read_text(encoding="utf-8")
        assert 'ttm:role="x-translation"' in text
        assert "翻译" in text

        # 只选翻译：父行不在组内，翻译单独成 p（仍保留 x-translation）。
        project.set_axis_groups(
            [AxisGroup(name="翻译", singer_ids=[translator.id])]
        )
        page._on_export()
        _drain_export(page, qapp)
        text = (tmp_path / "song.ttml").read_text(encoding="utf-8")
        assert 'ttm:role="x-translation"' in text
        assert "あいう" not in text
