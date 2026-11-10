"""ProjectLoadWorker：后台加载 .sug 的信号契约（finished 携带 extras）。"""

from strange_uta_game.backend.domain import Project, Sentence
from strange_uta_game.backend.infrastructure.persistence.sug_io import SugProjectParser
from strange_uta_game.frontend.workers import ProjectLoadWorker


def _write_project(tmp_path, **extras_kwargs):
    project = Project()
    singer = project.get_default_singer()
    project.add_sentence(Sentence.from_text("测试歌词", singer.id))

    file_path = tmp_path / "proj.sug"
    SugProjectParser.save(project, str(file_path), **extras_kwargs)
    return project, file_path


def test_worker_emits_project_and_extras(tmp_path):
    """finished 一次带回 (Project, file_path, extras)，extras 单次解析产出"""
    project, file_path = _write_project(
        tmp_path,
        nicokara_tags={"tag1": {"enabled": True}},
        media_path="X:/media/song.mp3",
    )

    results, errors = [], []
    worker = ProjectLoadWorker(str(file_path))
    worker.finished.connect(lambda p, fp, ex: results.append((p, fp, ex)))
    worker.error.connect(errors.append)

    worker.run()  # 直接调用：同步发射信号，无需事件循环

    assert not errors
    loaded, fp, extras = results[0]
    assert loaded.id == project.id
    assert fp == str(file_path)
    # media_exists 是 worker 在后台线程顺带探测的存在性结果（UNC 媒体的
    # exists() 在主线程会阻塞二十余秒，消费侧只认布尔值，不再 stat）。
    # X:/media/song.mp3 不存在 → False。
    assert extras == {
        "nicokara_tags": {"tag1": {"enabled": True}},
        "media_path": "X:/media/song.mp3",
        "media_exists": False,
    }


def test_worker_emits_empty_extras_for_plain_project(tmp_path):
    """无附加字段的 .sug：extras 为空 dict 而非 None"""
    _, file_path = _write_project(tmp_path)

    results, errors = [], []
    worker = ProjectLoadWorker(str(file_path))
    worker.finished.connect(lambda p, fp, ex: results.append((p, fp, ex)))
    worker.error.connect(errors.append)

    worker.run()

    assert not errors
    assert results[0][2] == {}


def test_worker_emits_error_on_bad_file(tmp_path):
    bad = tmp_path / "bad.sug"
    bad.write_text("not valid json", encoding="utf-8")

    results, errors = [], []
    worker = ProjectLoadWorker(str(bad))
    worker.finished.connect(lambda *args: results.append(args))
    worker.error.connect(errors.append)

    worker.run()

    assert not results
    assert len(errors) == 1
    assert "JSON" in errors[0]
