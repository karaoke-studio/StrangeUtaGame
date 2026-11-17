import pytest

from strange_uta_game.backend.domain import Character, Project, Sentence
from strange_uta_game.backend.infrastructure.parsers.annotated_text import (
    parse_timed_line,
    sentence_to_timed_line,
)
from strange_uta_game.frontend.editor.timing.auto_guide import (
    AutoGuideParams,
    apply_auto_guide_candidates,
    candidate_preflight,
    scan_auto_guide_candidates,
)


def _ch(text, ts=None, *, end=False, end_ts=None, todo=False, guide=False):
    return Character(
        char=text,
        timestamps=[] if ts is None else [ts],
        check_count=0 if ts is None else 1,
        is_sentence_end=end,
        sentence_end_ts=end_ts,
        needs_guide=todo,
        is_guide=guide,
    )


def _project(chars):
    project = Project()
    singer_id = project.get_default_singer().id
    for char in chars:
        char.singer_id = singer_id
    project.sentences = [Sentence(singer_id=singer_id, characters=chars)]
    return project


@pytest.mark.parametrize("selected", [(), (2,), (1, 3)])
def test_copy_settings_uses_first_checked_candidate_only(qtbot, monkeypatch, selected):
    from types import SimpleNamespace
    from strange_uta_game.frontend.editor.timing import auto_guide_dialog

    monkeypatch.setattr(auto_guide_dialog, "AppSettings", lambda: SimpleNamespace(
        get=lambda _key, default=None: default,
        set=lambda *_args: None,
        save=lambda: None,
    ))
    chars = []
    for i in range(4):
        chars.extend([
            _ch("●", 4000 + i * 5000, guide=True),
            _ch(str(i), 5000 + i * 5000, end=True, end_ts=6000 + i * 5000, todo=True),
        ])
    chars.append(_ch("未打轴", todo=True))
    dialog = auto_guide_dialog.AutoGuideDialog(_project(chars))
    qtbot.addWidget(dialog)
    assert len(dialog.rows) == 5
    for i, row in enumerate(dialog.rows):
        row.symbol.setText(f"符{i}")
        row.count.setText(str(i + 1))
        row.duration.setText(str(700 + i * 100))
        row.check.setChecked(i in selected)
    if selected:
        source = dialog.rows[selected[0]]
        source.mode.setCurrentIndex(1)
        source.reverse.setChecked(True)
        source.new_line.setChecked(True)
        source.existing_action.setCurrentIndex(1)
    # Disabled candidates must be excluded even if still programmatically checked.
    disabled = dialog.rows[-1]
    assert not disabled.check.isEnabled()
    disabled.check.setChecked(True)
    before = [row.params() for row in dialog.rows]
    checks = [row.check.isChecked() for row in dialog.rows]

    dialog.copy_first_btn.click()

    for i, row in enumerate(dialog.rows):
        expected = before[selected[0]] if i in selected else before[i]
        assert row.params() == expected
    assert [row.check.isChecked() for row in dialog.rows] == checks


def test_scan_uses_continuous_state_and_sentence_end_boundary():
    project = _project(
        [
            _ch("a", 1000),
            _ch("b", 1500, end=True, end_ts=2000),
            _ch("c", 6001),
            _ch("d", 6500),
        ]
    )
    found = scan_auto_guide_candidates(project, 3000)
    assert [c.target.char for c in found] == ["a", "c"]
    assert found[0].left_ms == 0
    assert found[0].is_first is True
    assert found[1].left_ms == 2000
    assert found[1].gap_ms == 4001


def test_todo_is_forced_and_missing_timestamp_is_not_executable():
    project = _project([_ch("a", 1000), _ch("x", todo=True)])
    found = scan_auto_guide_candidates(project, 30_000)
    assert [c.target.char for c in found] == ["a", "x"]
    todo = found[1]
    assert todo.forced_by_todo is True
    assert candidate_preflight(todo, AutoGuideParams(symbol="●"))["reason"] == "missing_target"


def test_explicit_guides_do_not_trigger_or_shorten_boundary():
    project = _project(
        [
            _ch("a", 1000, end=True, end_ts=2000),
            _ch("●", 3000, guide=True),
            _ch("b", 6001),
        ]
    )
    found = scan_auto_guide_candidates(project, 3000)
    assert [c.target.char for c in found] == ["a", "b"]
    assert found[1].left_ms == 2000
    assert found[1].existing_count == 1


def test_apply_replace_marks_chars_and_clears_todo():
    old = _ch("○", 3000, guide=True)
    target = _ch("歌", 5000, todo=True)
    project = _project([old, target])
    candidate = scan_auto_guide_candidates(project, 999_999)[0]
    assert candidate.target is target

    result = apply_auto_guide_candidates(
        project,
        [(candidate, AutoGuideParams(symbol="●", count=2, duration_ms=1000))],
    )
    chars = project.sentences[0].characters
    assert result == {
        "inserted": 1,
        "inserted_chars": 2,
        "replaced": 1,
        "cleared_todos": 1,
        "skipped": 0,
    }
    assert [c.char for c in chars] == ["●", "●", "歌"]
    assert [c.timestamps for c in chars[:2]] == [[3000], [4000]]
    assert all(c.is_guide for c in chars[:2])
    assert target.needs_guide is False


def test_reverse_appends_end_marker_one_step_below_last_guide():
    target = _ch("歌", 44_010, todo=True)
    project = _project([target])
    candidate = scan_auto_guide_candidates(project, 3000)[0]

    apply_auto_guide_candidates(
        project,
        [(
            candidate,
            AutoGuideParams(symbol="●", count=3, duration_ms=1000, reverse=True),
        )],
    )

    chars = project.sentences[0].characters
    assert [c.timestamps for c in chars[:3]] == [[43_010], [42_010], [41_010]]
    assert chars[2].is_sentence_end is True
    # 尾标记继续等差数列再往下一步：末个导唱 41.01 - 1s = 40.01
    assert chars[2].sentence_end_ts == 40_010
    assert not any(c.is_sentence_end for c in chars[:2])
    assert target.is_sentence_end is False


def test_non_reverse_guides_have_no_end_marker():
    target = _ch("歌", 44_010, todo=True)
    project = _project([target])
    candidate = scan_auto_guide_candidates(project, 3000)[0]

    apply_auto_guide_candidates(
        project,
        [(candidate, AutoGuideParams(symbol="●", count=3, duration_ms=1000))],
    )

    chars = project.sentences[0].characters
    assert [c.timestamps for c in chars[:3]] == [[41_010], [42_010], [43_010]]
    assert not any(c.is_sentence_end for c in chars[:3])


def test_reverse_guide_serialization_round_trip():
    # 多个：块内最后一个导唱字符带 [>目标起始] 尾标记
    target = _ch("歌", 44_010, todo=True)
    project = _project([target])
    candidate = scan_auto_guide_candidates(project, 3000)[0]
    apply_auto_guide_candidates(
        project,
        [(
            candidate,
            AutoGuideParams(symbol="●", count=3, duration_ms=1000, reverse=True),
        )],
    )
    line, _ = sentence_to_timed_line(project.sentences[0].characters)
    assert "{●●●||[00:43.01],[00:42.01],[00:41.01][>00:40.01]}" in line

    parsed = parse_timed_line(line)[0]
    guides = [c for c in parsed if c.is_guide]
    assert guides[-1].is_sentence_end is True
    assert guides[-1].sentence_end_ts == 40_010

    # 单个：独立字符直接跟 [ts]○[>ts-间隔]
    target = _ch("歌", 45_010, todo=True)
    project = _project([target])
    candidate = scan_auto_guide_candidates(project, 3000)[0]
    apply_auto_guide_candidates(
        project,
        [(candidate, AutoGuideParams(symbol="○", duration_ms=1000, reverse=True))],
    )
    line, _ = sentence_to_timed_line(project.sentences[0].characters)
    assert "[00:44.01]○[>00:43.01]" in line

    parsed = parse_timed_line(line)[0]
    guides = [c for c in parsed if c.is_guide]
    assert guides[-1].is_sentence_end is True
    assert guides[-1].sentence_end_ts == 43_010


def test_fixed_interval_unknown_left_is_executable_but_fill_is_not():
    candidate = scan_auto_guide_candidates(_project([_ch("歌", 5000)]), 3000)[0]
    candidate.is_first = False
    candidate.left_ms = None
    fixed = candidate_preflight(candidate, AutoGuideParams(symbol="●"))
    fill = candidate_preflight(candidate, AutoGuideParams(symbol="●", fill_gap=True))
    assert fixed["executable"] is True
    assert fixed["overrun_ms"] is None
    assert fill == {"executable": False, "reason": "missing_left"}


def test_new_line_before_line_start_creates_independent_guide_sentence():
    target = _ch("歌", 5000, todo=True)
    project = _project([target])
    candidate = scan_auto_guide_candidates(project, 3000)[0]

    result = apply_auto_guide_candidates(
        project,
        [(
            candidate,
            AutoGuideParams(
                symbol="●",
                count=2,
                duration_ms=1000,
                new_line=True,
            ),
        )],
    )

    assert result["inserted"] == 1
    assert [sentence.text for sentence in project.sentences] == ["●●", "歌"]
    assert project.sentences[0].characters[-1].is_line_end is True
    assert project.sentences[1].characters[0] is target
    assert target.needs_guide is False


def test_new_line_in_middle_splits_original_sentence_before_target():
    before = _ch("前", 1000)
    target = _ch("歌", 5000, todo=True)
    after = _ch("後")
    project = _project([before, target, after])
    candidate = next(
        c for c in scan_auto_guide_candidates(project, 999_999)
        if c.target is target
    )

    apply_auto_guide_candidates(
        project,
        [(
            candidate,
            AutoGuideParams(symbol="●", new_line=True),
        )],
    )

    assert [sentence.text for sentence in project.sentences] == ["前", "●", "歌後"]
    assert all(sentence.characters[-1].is_line_end for sentence in project.sentences)
    assert project.sentences[2].characters[0] is target
