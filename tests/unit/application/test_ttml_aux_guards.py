"""TTML 副轴行不参与自动清理/分析的回归测试。"""

from __future__ import annotations

from strange_uta_game.backend.application.auto_check_service import (
    delete_checkpoints_by_type_names,
)
from strange_uta_game.backend.domain import Project, Sentence


def test_delete_checkpoints_by_type_skips_aux_background():
    project = Project()
    singer = project.singers[0]
    main = Sentence.from_text("あ", singer.id)
    main.characters[0].add_timestamp(1000)
    main.characters[0].set_check_count(1, force=True)
    project.add_sentence(main)

    aux = Sentence.from_text("(bg)", singer.id)
    aux.aux_role = "x-bg"
    aux.aux_begin = 1000
    aux.aux_end = 2000
    aux.characters[0].add_timestamp(1000)
    aux.characters[-1].set_sentence_end_ts(2000)
    project.add_sentence(aux)

    removed = delete_checkpoints_by_type_names(project, ["hiragana"])
    assert removed == 1
    assert aux.characters[0].timestamps == [1000]
    assert aux.characters[-1].sentence_end_ts == 2000
