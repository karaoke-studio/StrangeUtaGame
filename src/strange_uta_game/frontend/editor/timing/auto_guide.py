"""根据已打时间戳扫描并批量插入导唱符的纯逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field

from strange_uta_game.backend.domain import Character, Sentence


@dataclass
class AutoGuideParams:
    symbol: str = "●"
    count: int = 1
    duration_ms: int = 1000
    fill_gap: bool = False
    reverse: bool = False
    new_line: bool = False
    existing_action: str = "replace"  # replace / append


@dataclass
class AutoGuideCandidate:
    sentence_idx: int
    char_idx: int
    target: Character = field(repr=False)
    target_ms: int | None = None
    left_ms: int | None = None
    is_first: bool = False
    forced_by_todo: bool = False
    existing_count: int = 0

    @property
    def key(self) -> int:
        """窗口存活期间稳定的候选键。"""
        return id(self.target)

    @property
    def gap_ms(self) -> int | None:
        if self.target_ms is None or self.left_ms is None:
            return None
        return self.target_ms - self.left_ms


def _last_non_guide_timestamp(ch: Character) -> int | None:
    if ch.is_guide:
        return None
    values = list(ch.timestamps)
    if ch.sentence_end_ts is not None:
        values.append(ch.sentence_end_ts)
    return max(values) if values else None


def _existing_guide_count(characters: list[Character], target_idx: int) -> int:
    count = 0
    i = target_idx - 1
    while i >= 0 and characters[i].is_guide:
        count += 1
        i -= 1
    return count


def scan_auto_guide_candidates(project, min_gap_ms: int) -> list[AutoGuideCandidate]:
    """按演唱连续状态扫描整项目；needs_guide 作为强制候选并自动去重。"""
    candidates: list[AutoGuideCandidate] = []
    continuous = False
    first_normal_seen = False
    last_boundary: int | None = None

    for si, sentence in enumerate(project.sentences):
        for ci, ch in enumerate(sentence.characters):
            # 明确导唱不触发连续状态，也不缩短原始歌词空隙。
            if ch.is_guide:
                continue

            target_ms = ch.timestamps[0] if ch.timestamps else None
            automatic_trigger = not continuous and target_ms is not None
            is_first = automatic_trigger and not first_normal_seen
            forced = bool(ch.needs_guide)

            if automatic_trigger:
                continuous = True
                first_normal_seen = True

            left_ms = 0 if is_first else last_boundary
            gap_ok = (
                automatic_trigger
                and (
                    is_first
                    or (
                        left_ms is not None
                        and target_ms is not None
                        and target_ms - left_ms > min_gap_ms
                    )
                )
            )
            if forced or gap_ok:
                candidates.append(
                    AutoGuideCandidate(
                        sentence_idx=si,
                        char_idx=ci,
                        target=ch,
                        target_ms=target_ms,
                        left_ms=left_ms,
                        is_first=is_first,
                        forced_by_todo=forced,
                        existing_count=_existing_guide_count(sentence.characters, ci),
                    )
                )

            boundary = _last_non_guide_timestamp(ch)
            if boundary is not None:
                last_boundary = boundary

            # 当前字符既可作为入口，又可立即结束这一段连续演唱。
            if ch.is_sentence_end:
                continuous = False

    return candidates


def candidate_preflight(candidate: AutoGuideCandidate, params: AutoGuideParams) -> dict:
    """返回候选可执行性、越界量和 0ms 钳制数量。"""
    if not params.symbol:
        return {"executable": False, "reason": "empty_symbol"}
    if params.count < 1:
        return {"executable": False, "reason": "invalid_count"}
    if candidate.target_ms is None:
        return {"executable": False, "reason": "missing_target"}
    if params.fill_gap and candidate.left_ms is None:
        return {"executable": False, "reason": "missing_left"}

    if params.fill_gap:
        assert candidate.left_ms is not None
        if candidate.left_ms >= candidate.target_ms:
            return {"executable": False, "reason": "invalid_gap"}
        duration_ms = (candidate.target_ms - candidate.left_ms) // params.count
    else:
        duration_ms = max(100, params.duration_ms)

    raw_times = []
    for i in range(params.count):
        if params.reverse:
            raw_times.append(candidate.target_ms - duration_ms * (i + 1))
        else:
            raw_times.append(
                candidate.target_ms - duration_ms * (params.count - i)
            )
    first_time = min(raw_times) if raw_times else candidate.target_ms
    overrun_ms = (
        max(0, candidate.left_ms - first_time)
        if candidate.left_ms is not None
        else None
    )
    return {
        "executable": True,
        "reason": None,
        "duration_ms": duration_ms,
        "first_time_ms": first_time,
        "overrun_ms": overrun_ms,
        "clamped_count": sum(1 for ts in raw_times if ts < 0),
    }


def _build_chars(candidate: AutoGuideCandidate, params: AutoGuideParams) -> list[Character]:
    from strange_uta_game.backend.domain.graphemes import split_graphemes

    check = candidate_preflight(candidate, params)
    if not check["executable"]:
        return []
    duration_ms = check["duration_ms"]
    result: list[Character] = []
    # 符号按字素簇拆分（emoji＋变体选择符 / 多码点符号是一个字符）
    symbol_units = split_graphemes(params.symbol)
    for i in range(params.count):
        for j, text_char in enumerate(symbol_units):
            first_in_symbol = j == 0
            last = i == params.count - 1 and j == len(symbol_units) - 1
            new_ch = Character(
                char=text_char,
                ruby=None,
                check_count=1 if first_in_symbol else 0,
                singer_id=candidate.target.singer_id,
                linked_to_next=not last,
                is_guide=True,
            )
            if first_in_symbol:
                if params.reverse:
                    ts = candidate.target_ms - duration_ms * (i + 1)
                else:
                    ts = candidate.target_ms - duration_ms * (params.count - i)
                new_ch.add_timestamp(max(0, ts))
            result.append(new_ch)
    # 反向导唱的尾部 [>…] 标记：外部渲染软件要求在最后一个导唱字符上
    # 附上停顿点时间戳——继续等差数列再往下推一步（末个导唱时间戳再减
    # 一个间隔 = target_ms - (count+1)*duration），非反向不插入。
    if params.reverse and result:
        end_ts = max(0, candidate.target_ms - duration_ms * (params.count + 1))
        last_ch = result[-1]
        last_ch.is_sentence_end = True
        last_ch.set_sentence_end_ts(end_ts)
    return result


def apply_auto_guide_candidates(
    project,
    items: list[tuple[AutoGuideCandidate, AutoGuideParams]],
) -> dict:
    """按项目倒序应用候选，避免前方插入影响尚未处理的位置。"""
    inserted_positions = 0
    inserted_chars = 0
    replaced_blocks = 0
    cleared_todos = 0
    skipped = 0

    ordered = sorted(
        items,
        key=lambda item: (item[0].sentence_idx, item[0].char_idx),
        reverse=True,
    )
    for candidate, params in ordered:
        check = candidate_preflight(candidate, params)
        if not check["executable"]:
            skipped += 1
            continue
        located = next(
            (
                (si, sentence, ci)
                for si, sentence in enumerate(project.sentences)
                for ci, ch in enumerate(sentence.characters)
                if ch is candidate.target
            ),
            None,
        )
        if located is None:
            skipped += 1
            continue
        sentence_idx, sentence, target_idx = located

        if params.existing_action == "replace":
            start = target_idx
            while start > 0 and sentence.characters[start - 1].is_guide:
                start -= 1
            if start < target_idx:
                del sentence.characters[start:target_idx]
                target_idx = start
                replaced_blocks += 1

        chars = _build_chars(candidate, params)
        if not chars:
            skipped += 1
            continue
        if params.new_line:
            chars[-1].is_line_end = True
            guide_sentence = Sentence(
                singer_id=candidate.target.singer_id or sentence.singer_id,
                characters=chars,
            )
            if target_idx == 0:
                project.sentences.insert(sentence_idx, guide_sentence)
            else:
                right_chars = sentence.characters[target_idx:]
                del sentence.characters[target_idx:]
                sentence.characters[-1].is_line_end = True
                sentence.characters[-1].linked_to_next = False
                right_chars[-1].is_line_end = True
                right_sentence = Sentence(
                    singer_id=candidate.target.singer_id or sentence.singer_id,
                    characters=right_chars,
                    song_part=sentence.song_part,
                )
                project.sentences.insert(sentence_idx + 1, guide_sentence)
                project.sentences.insert(sentence_idx + 2, right_sentence)
        else:
            sentence.characters[target_idx:target_idx] = chars
        inserted_positions += 1
        inserted_chars += len(chars)
        if candidate.target.needs_guide:
            candidate.target.needs_guide = False
            cleared_todos += 1

    return {
        "inserted": inserted_positions,
        "inserted_chars": inserted_chars,
        "replaced": replaced_blocks,
        "cleared_todos": cleared_todos,
        "skipped": skipped,
    }
