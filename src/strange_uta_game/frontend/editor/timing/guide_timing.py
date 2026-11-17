"""Shared guide-symbol timing, independent of widgets and audio analysis."""

from __future__ import annotations

import math


def beat_duration_ms(bpm: float) -> float:
    if not math.isfinite(bpm) or not 10 <= bpm <= 600:
        raise ValueError("invalid_bpm")
    return 60_000 / bpm


def guide_times(
    target_ms: int, count: int, duration_ms: int, reverse: bool,
    bpm: float | None = None,
) -> tuple[list[int], int | None]:
    """Round each complete beat offset; never accumulate rounded intervals."""
    duration = beat_duration_ms(bpm) if bpm is not None else duration_ms
    steps = range(1, count + 1) if reverse else range(count, 0, -1)
    times = [target_ms - round(k * duration) for k in steps]
    end = target_ms - round((count + 1) * duration) if reverse else None
    return times, end


def previous_lyric_boundary(sentence, char_idx: int, project=None) -> int:
    """Find the preceding timed lyric, crossing sentence boundaries if available."""
    sentences = project.sentences if project is not None else [sentence]
    previous = []
    for item in sentences:
        if item is sentence:
            previous.extend(item.characters[:char_idx])
            break
        previous.extend(item.characters)
    for ch in reversed(previous):
        if ch.is_guide:
            continue
        values = list(ch.timestamps)
        if ch.sentence_end_ts is not None:
            values.append(ch.sentence_end_ts)
        if values:
            return max(values)
    return 0
