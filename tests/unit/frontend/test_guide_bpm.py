from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtWidgets import QWidget

from strange_uta_game.backend.domain import Character, Project, Sentence
from strange_uta_game.frontend.editor.timing import guide_bpm, task_runner
from strange_uta_game.frontend.editor.timing.auto_guide import (
    AutoGuideParams,
    apply_auto_guide_candidates,
    candidate_preflight,
    scan_auto_guide_candidates,
)
from strange_uta_game.frontend.editor.timing.dialogs import (
    InsertGuideSymbolDialog,
    insert_guide_before,
)


def _project(*times):
    project = Project()
    singer = project.get_default_singer().id
    project.sentences = [
        Sentence(singer_id=singer, characters=[Character(
            char="歌", singer_id=singer, timestamps=[time], needs_guide=True,
            is_sentence_end=True, sentence_end_ts=time + 500,
        )])
        for time in times
    ]
    return project


@pytest.mark.parametrize("bpm, expected", [
    (120, [8000, 8500, 9000, 9500]),
    (60, [6000, 7000, 8000, 9000]),
    (137, [8248, 8686, 9124, 9562]),
])
def test_single_insert_uses_complete_beat_offsets(bpm, expected):
    project = _project(10_000)
    sentence = project.sentences[0]
    result = insert_guide_before(sentence, 0, "●", 4, 1000, False, False, bpm=bpm)
    assert result["ok"]
    assert [c.timestamps[0] for c in sentence.characters[:-1]] == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_bpm_zero_boundary_and_reverse_tail_are_checked_before_mutation(reverse):
    # Reverse mode has one extra timestamp for its trailing end marker.
    minimum = 2500 if reverse else 2000
    project = _project(minimum)
    sentence = project.sentences[0]
    target = sentence.characters[0]
    result = insert_guide_before(sentence, 0, "●", 4, 1000, reverse, False, bpm=120)
    assert result["ok"]
    assert min(c.timestamps[0] for c in sentence.characters[:-1]) >= 0
    if reverse:
        assert sentence.characters[-2].sentence_end_ts == 0
    else:
        assert sentence.characters[0].timestamps == [0]
    assert sentence.characters[-1] is target

    project = _project(minimum - 1)
    sentence = project.sentences[0]
    target = sentence.characters[0]
    result = insert_guide_before(sentence, 0, "●", 4, 1000, reverse, False, bpm=120)
    assert result == {"ok": False, "reason": "before_zero"}
    assert sentence.characters == [target]
    assert target.needs_guide


@pytest.mark.parametrize("bpm", [0, -120, 601, float("nan"), float("inf")])
def test_invalid_bpm_does_not_change_lyrics(bpm):
    sentence = _project(10_000).sentences[0]
    target = sentence.characters[0]
    assert not insert_guide_before(sentence, 0, "●", 4, 1000, False, False, bpm=bpm)["ok"]
    assert sentence.characters == [target]


def test_overlap_with_previous_sentence_is_reported_and_inserted():
    project = _project(7000, 8000)
    sentence = project.sentences[1]
    result = insert_guide_before(
        sentence, 0, "●", 4, 1000, False, False, bpm=120, project=project,
    )
    assert result["ok"]
    assert result["overlap_ms"] == 1500
    assert [c.timestamps[0] for c in sentence.characters[:-1]] == [6000, 6500, 7000, 7500]


def test_auto_batch_skips_negative_times_preserves_todo_and_allows_overlap():
    project = _project(1000, 7000, 8000)
    candidates = scan_auto_guide_candidates(project, 0)
    params = AutoGuideParams(count=4, bpm=120)
    assert candidate_preflight(candidates[0], params)["reason"] == "before_zero"
    assert candidate_preflight(candidates[-1], params)["executable"]
    assert candidate_preflight(candidates[-1], params)["overrun_ms"] == 1500
    result = apply_auto_guide_candidates(project, [(c, params) for c in candidates])
    assert result["inserted"] == 2
    assert result["skipped"] == 1
    assert project.sentences[0].characters[0].needs_guide
    assert not project.sentences[1].characters[-1].needs_guide
    assert [c.timestamps[0] for c in project.sentences[-1].characters[:-1]] == [6000, 6500, 7000, 7500]


def test_auto_and_single_bpm_reverse_match_including_tail_and_graphemes():
    projects = [_project(10_000), _project(10_000)]
    single = projects[0].sentences[0]
    insert_guide_before(single, 0, "⭐️", 4, 1000, True, False, bpm=137)
    candidate = scan_auto_guide_candidates(projects[1], 0)[0]
    params = AutoGuideParams(symbol="⭐️", count=4, reverse=True, bpm=137)
    apply_auto_guide_candidates(projects[1], [(candidate, params)])
    auto = projects[1].sentences[0]
    assert [(c.timestamps, c.sentence_end_ts) for c in auto.characters] == [
        (c.timestamps, c.sentence_end_ts) for c in single.characters
    ]
    assert single.characters[-2].sentence_end_ts == 7810


@pytest.fixture
def settings(monkeypatch):
    from strange_uta_game.frontend.settings.app_settings import AppSettings

    values = {}
    monkeypatch.setattr(AppSettings, "get", lambda _s, key, default=None: values.get(key, default))
    monkeypatch.setattr(AppSettings, "set", lambda _s, key, value: values.__setitem__(key, value))
    monkeypatch.setattr(AppSettings, "save", lambda _s: None)
    return values


@pytest.fixture
def fake_detection(monkeypatch):
    tasks = []

    def start(owner, worker, **callbacks):
        relay = task_runner.TaskRelay(
            owner, lambda: callbacks["is_current"](worker),
            on_finished=callbacks["on_finished"], on_error=callbacks["on_error"],
        )
        tasks.append((worker, relay))
        return relay

    monkeypatch.setattr(task_runner, "start_task", start)
    return tasks


def _editor(qtbot):
    editor = QWidget()
    qtbot.addWidget(editor)
    editor._audio_file_path = "song.wav"
    editor.samples = np.zeros(1000, dtype=np.float32)
    editor._timing_service = SimpleNamespace(
        get_mono_samples=lambda: editor.samples,
        get_audio_info=lambda: SimpleNamespace(sample_rate=44_100),
    )
    return editor


@pytest.mark.parametrize("detected", [60, 90, 120, 137, 140, 175, 200])
def test_detection_requires_selection_and_manual_bpm_stays_until_selected(qtbot, fake_detection, detected):
    editor = _editor(qtbot)
    control = guide_bpm.GuideBpmControl(editor, bpm=95, parent=editor)
    control.activate()
    worker, relay = fake_detection[-1]
    relay._finished({"bpm": detected, "confidence": 0.8})
    assert control.bpm() == 95
    assert control.candidates.count() == 4
    assert [control.candidates.itemData(i) for i in range(1, 4)] == [detected / 2, detected, detected * 2]
    control.candidates.setCurrentIndex(1)
    assert control.bpm() == detected / 2
    control.value.setValue(62.5)
    assert control.bpm() == 62.5


@pytest.mark.parametrize("change", ["samples", "path", "loading", "cancel", "restart"])
def test_late_detection_results_are_discarded(qtbot, fake_detection, change):
    editor = _editor(qtbot)
    control = guide_bpm.GuideBpmControl(editor, bpm=95, parent=editor)
    control.activate()
    worker, relay = fake_detection[-1]
    if change == "samples":
        editor.samples = editor.samples.copy()
    elif change == "path":
        editor._audio_file_path = "other.wav"
    elif change == "loading":
        editor._audio_loading = True
    elif change == "cancel":
        control.cancel()
    else:
        control.start_detection()
    relay._finished({"bpm": 120, "confidence": 0.8})
    assert control.bpm() == 95
    assert control.candidates.count() == 1
    assert worker._cancelled


def test_completed_candidates_cannot_be_applied_after_changing_audio(qtbot, fake_detection):
    editor = _editor(qtbot)
    control = guide_bpm.GuideBpmControl(editor, bpm=95, parent=editor)
    control.activate()
    fake_detection[-1][1]._finished({"bpm": 120, "confidence": 0.8})
    editor._audio_file_path = "other.wav"
    control.candidates.setCurrentIndex(1)
    assert control.bpm() == 95
    assert control.candidates.currentIndex() == 0


def test_real_background_detection_returns_candidates_without_changing_bpm(qtbot):
    editor = _editor(qtbot)
    editor.samples = np.zeros(44_100 * 30, dtype=np.float32)
    pulse = np.hanning(512).astype(np.float32)
    for start in range(0, len(editor.samples) - len(pulse), 22_050):
        editor.samples[start:start + len(pulse)] = pulse
    control = guide_bpm.GuideBpmControl(editor, bpm=95, parent=editor)
    control.activate()
    qtbot.waitUntil(lambda: control.candidates.count() > 1, timeout=15_000)
    assert control.candidates.itemData(2) == pytest.approx(120, abs=2)
    assert control.bpm() == 95
    control.candidates.setCurrentIndex(2)
    assert control.bpm() == pytest.approx(120, abs=2)
    qtbot.waitUntil(lambda: task_runner.running_tasks() == 0, timeout=5000)


@pytest.mark.parametrize("method", ["close", "reject"])
def test_auto_dialog_close_cancels_detection(qtbot, settings, fake_detection, method):
    from strange_uta_game.frontend.editor.timing.auto_guide_dialog import AutoGuideDialog

    editor = _editor(qtbot)
    dialog = AutoGuideDialog(_project(10_000), editor)
    qtbot.addWidget(dialog)
    worker, relay = fake_detection[-1]
    getattr(dialog, method)()
    assert worker._cancelled


def test_auto_dialog_excludes_and_counts_negative_bpm_candidates(qtbot, settings):
    from strange_uta_game.frontend.editor.timing.auto_guide_dialog import AutoGuideDialog

    project = _project(1000, 10_000)
    dialog = AutoGuideDialog(project)
    qtbot.addWidget(dialog)
    for row in dialog.rows:
        row.count.setText("4")
    dialog.apply_bpm_btn.click()
    assert dialog.before_zero_count() == 1
    assert len(dialog.selected_items()) == 1
    assert project.sentences[0].characters[0].needs_guide
    dialog.rows[1].check.setChecked(False)
    dialog._emit_execute()
    assert "1 处候选导唱早于 0 秒" in dialog.status.text()
    dialog.close()


def test_single_dialog_modes_preview_overlap_and_close_cancels_detection(qtbot, settings, fake_detection):
    editor = _editor(qtbot)
    editor._project = _project(7000, 8000)
    dialog = InsertGuideSymbolDialog(editor._project.sentences[1], 0, editor)
    qtbot.addWidget(dialog)
    dialog.edit_symbol.setText("●")
    dialog.edit_count.setText("4")
    dialog.chk_fill_gap.setChecked(True)
    dialog.chk_bpm.setChecked(True)
    assert not dialog.chk_fill_gap.isChecked()
    assert not dialog.edit_duration.isEnabled()
    assert "重叠 1500ms" in dialog.timing_preview.text()
    worker, _ = fake_detection[-1]
    dialog.reject()
    assert worker._cancelled
    assert not dialog.was_modified()


def test_single_dialog_negative_times_are_not_inserted(qtbot, monkeypatch, settings):
    from strange_uta_game.frontend.editor.timing import dialogs

    monkeypatch.setattr(dialogs.InfoBar, "warning", lambda **_kwargs: None)
    sentence = _project(1000).sentences[0]
    dialog = InsertGuideSymbolDialog(sentence, 0)
    qtbot.addWidget(dialog)
    dialog.edit_symbol.setText("●")
    dialog.edit_count.setText("4")
    dialog.chk_bpm.setChecked(True)
    dialog._on_execute()
    assert len(sentence.characters) == 1
    assert not dialog.was_modified()
    assert "早于 0" in dialog.timing_preview.text()


def test_fill_all_request_keeps_bpm_and_is_exclusive_with_fill_gap(qtbot, settings):
    sentence = _project(10_000).sentences[0]
    dialog = InsertGuideSymbolDialog(sentence, 0)
    qtbot.addWidget(dialog)
    dialog.edit_symbol.setText("●")
    dialog.chk_bpm.setChecked(True)
    dialog.bpm_control.value.setValue(137)
    dialog._on_fill_all()
    assert dialog.fill_all_params()["bpm"] == 137
    assert not dialog.fill_all_params()["fill_gap"]
    assert settings["timing.guide_use_bpm"]
    assert len(sentence.characters) == 1


def test_auto_dialog_applies_confirmed_bpm_and_copies_only_selected_rows(qtbot, settings):
    from strange_uta_game.frontend.editor.timing.auto_guide_dialog import AutoGuideDialog

    dialog = AutoGuideDialog(_project(10_000, 20_000, 30_000))
    qtbot.addWidget(dialog)
    dialog.rows[-1].check.setChecked(False)
    dialog.bpm_control.value.setValue(60)
    dialog.apply_bpm_btn.click()
    assert [row.params().bpm for row in dialog.rows] == [60, 60, None]
    assert all(row.params().fill_gap is False for row in dialog.rows)
    dialog.rows[0].bpm.setValue(137)
    dialog._copy_first()
    assert [row.params().bpm for row in dialog.rows] == [137, 137, None]
    assert "437.956ms" in dialog.rows[0].warning.text()
    dialog.remember_first_executed(dialog.selected_items())
    assert settings["auto_guide.defaults"]["bpm"] == 137
    dialog.close()


def test_fill_all_bpm_is_one_undo_preserves_skipped_todos_and_reports_overlap(qtbot, monkeypatch):
    from strange_uta_game.backend.application.command_manager import CommandManager
    from strange_uta_game.frontend.editor import timing_interface

    project = _project(1000, 7000, 8000)
    manager = CommandManager()
    messages = []
    monkeypatch.setattr(timing_interface.InfoBar, "success", lambda **kwargs: messages.append(kwargs["content"]))
    owner = SimpleNamespace(
        _project=project, _timing_service=SimpleNamespace(
            command_manager=manager, rebuild_global_checkpoints=lambda: None,
        ),
        _current_line_idx=0, preview=SimpleNamespace(_current_char_idx=0),
        _reapply_global_offset=lambda: None, refresh_lyric_display=lambda: None,
        _update_time_tags_display=lambda: None, _update_status=lambda: None,
        tr=lambda value: value,
    )
    timing_interface.EditorInterface._fill_all_todo_guides(owner, {
        "symbol": "●", "count": 4, "duration_ms": 1000,
        "reverse": False, "fill_gap": False, "bpm": 120,
    })
    assert [len(s.characters) for s in project.sentences] == [1, 5, 5]
    assert project.sentences[0].characters[0].needs_guide
    assert "1 处导唱时间早于 0 秒" in messages[0]
    assert "1 处与前面的歌词重叠" in messages[0]
    manager.undo()
    assert [len(s.characters) for s in project.sentences] == [1, 1, 1]
    assert all(s.characters[0].needs_guide for s in project.sentences)
    assert manager.undo() is None
    manager.redo()
    assert [len(s.characters) for s in project.sentences] == [1, 5, 5]
