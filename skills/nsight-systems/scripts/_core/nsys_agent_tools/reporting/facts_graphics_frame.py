"""Graphics frame facts: locate a CPU frame's window and scan it for evidence."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .evidence import add_key
from .frame_utils import (
    _MAX_PRESENT_CALLS,
    _frame_index_error,
    _frame_present_timestamps,
    _frame_row,
    _is_frame_in_range,
    _neighbor_frames,
)
from .schema import (
    TABLE_ETW_EVENTS,
    TABLE_GENERIC_EVENT_TYPES,
    TABLE_GPU_CONTEXT_SWITCH,
    TABLE_STRING_IDS,
    TABLE_WDDM_DMA_PACKET_START,
    TABLE_WDDM_EVICT_ALLOCATION,
    TABLE_WDDM_PAGING_QUEUE_PACKET_INFO,
)
from .sql_utils import _existing_columns, _query_dicts

type EventStats = list[dict[str, Any]]

_NEIGHBORHOOD = 5

# Thresholds for detecting significant alternating long-short frame-time patterns.
# Should be high enough to avoid false positives from ordinary per-frame jitter,
# yet sensitive enough to detect a real flip-flop.
_FRAME_TIME_MIN_ABSOLUTE_DIFFERENCE_MS = 5.0
_FRAME_TIME_MIN_RELATIVE_DIFFERENCE = 1.0
_OSCILLATION_MIN_DENSITY_PCT = 25.0
_FRAME_TIME_PATTERN_MIN_FRAME_COUNT = 4

# A slow-frame episode spans consecutive frames above a trailing baseline, which separates a
# recurring frame rate plateau from a one-off spike. The baseline is the median of recent frames
# that were not themselves slow, so it tracks the pace the application otherwise sustains.
_SLOW_EPISODE_BASELINE_NORMAL_FRAMES = 240
_SLOW_EPISODE_THRESHOLD_MULTIPLE = 1.8
_SLOW_EPISODE_MIN_FRAMES = 3
_SLOW_EPISODE_OUTLIER_MULTIPLE = 3.0
_SLOW_EPISODE_CATCH_UP_FRACTION = 0.5

_BASELINE_FRAME_STRATEGIES = ("neighborhood_median", "session_median", "previous", "next")

# Bounds tolerance in both directions: frame rate will be diagnosed as elevated or reduced
# when its rate_ratio differs from the baseline frame's by a factor of 1.25.
# Anything between the bounds reads as unchanged, absorbing the single baseline frame's own jitter.
_FRAME_SCAN_ELEVATED_RATE_RATIO = 1.25
_FRAME_SCAN_REDUCED_RATE_RATIO = 0.8

# Cause-family tables: WDDM paging/scheduling events (not in the ETW stream) whose
# in-window rate rises during a stutter. Tuple is (table, time column, label);
# the WDDM packet tables key on start, GPU_CONTEXT_SWITCH on timestamp.
_WDDM_CAUSE_TABLES = (
    (TABLE_WDDM_EVICT_ALLOCATION, "start", "evictions"),
    (TABLE_WDDM_PAGING_QUEUE_PACKET_INFO, "start", "paging_packets"),
    (TABLE_WDDM_DMA_PACKET_START, "start", "dma_packets"),
    (TABLE_GPU_CONTEXT_SWITCH, "timestamp", "gpu_context_switches"),
)


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    rank = fraction * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    return round(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low), 3)


def _frame_time_summary(frame_durations: list[tuple[int, float]]) -> dict[str, Any]:
    values = sorted(duration for _, duration in frame_durations)
    return {
        "count": len(values),
        "median_ms": _percentile(values, 0.5),
    }


def _is_frame_time_difference_significant(first_ms: float, second_ms: float) -> bool:
    difference_ms = abs(first_ms - second_ms)
    baseline_ms = max(min(first_ms, second_ms), 1e-9)
    return (
        difference_ms > _FRAME_TIME_MIN_ABSOLUTE_DIFFERENCE_MS
        and difference_ms / baseline_ms > _FRAME_TIME_MIN_RELATIVE_DIFFERENCE
    )


def _frame_time_pattern_summary(
    frame_durations: list[tuple[int, float]],
) -> tuple[dict[str, Any], set[int]]:
    pattern: dict[str, Any] = {
        "oscillation_detected": False,
        "even_odd_difference_detected": False,
    }
    oscillating_frames: set[int] = set()

    if len(frame_durations) >= _FRAME_TIME_PATTERN_MIN_FRAME_COUNT:
        transitions: list[tuple[int, int] | None] = []

        for index in range(1, len(frame_durations)):
            previous_duration = frame_durations[index - 1][1]
            current_duration = frame_durations[index][1]

            if not _is_frame_time_difference_significant(previous_duration, current_duration):
                transitions.append(None)
                continue

            direction = 1 if current_duration > previous_duration else -1
            transitions.append((index, direction))

        for index in range(1, len(transitions)):
            previous_transition = transitions[index - 1]
            current_transition = transitions[index]

            if (
                previous_transition is None
                or current_transition is None
                or previous_transition[1] == current_transition[1]
            ):
                continue

            center = current_transition[0] - 1
            oscillating_frames.update(
                frame_num for frame_num, _ in frame_durations[center - 1 : center + 2]
            )

        density_pct = round(len(oscillating_frames) / len(frame_durations) * 100, 1)

        if density_pct > _OSCILLATION_MIN_DENSITY_PCT:
            pattern["oscillation_detected"] = True
            pattern["oscillation_density_pct"] = density_pct

    even_frames = [item for item in frame_durations if item[0] % 2 == 0]
    odd_frames = [item for item in frame_durations if item[0] % 2 == 1]

    if len(even_frames) >= 2 and len(odd_frames) >= 2:
        even_summary = _frame_time_summary(even_frames)
        odd_summary = _frame_time_summary(odd_frames)
        even_median = even_summary["median_ms"]
        odd_median = odd_summary["median_ms"]

        if (
            isinstance(even_median, float)
            and isinstance(odd_median, float)
            and _is_frame_time_difference_significant(even_median, odd_median)
        ):
            pattern["even_odd_difference_detected"] = True
            pattern["even_frames"] = even_summary
            pattern["odd_frames"] = odd_summary

    return pattern, oscillating_frames


def _requested_frame_time_pattern(
    frame_index: int,
    frame_durations: list[tuple[int, float]],
) -> dict[str, Any]:
    pattern, oscillating_frames = _frame_time_pattern_summary(frame_durations)

    if frame_index not in oscillating_frames:
        return pattern

    duration_by_frame = dict(frame_durations)
    frame_duration = duration_by_frame[frame_index]
    candidates = [
        (neighbor, duration_by_frame[neighbor])
        for neighbor in (frame_index - 1, frame_index + 1)
        if neighbor in duration_by_frame
        and _is_frame_time_difference_significant(frame_duration, duration_by_frame[neighbor])
    ]

    if not candidates:
        return pattern

    paired_frame, paired_duration = max(
        candidates,
        key=lambda item: abs(frame_duration - item[1]),
    )
    frame_is_long = frame_duration > paired_duration
    pattern["role"] = "long_half" if frame_is_long else "short_half"
    pattern["paired_neighbor"] = {
        "frame_num": paired_frame,
        "frame_ms": round(paired_duration, 3),
        "role": "short_half" if frame_is_long else "long_half",
    }
    return pattern


def _slow_frame_episode_candidates(
    frame_durations: list[tuple[int, float]],
) -> list[tuple[int, float, float, bool]]:
    """Mark each frame slow when it exceeds a trailing rolling-median baseline."""

    session_median = _percentile(
        sorted(duration for _, duration in frame_durations),
        0.5,
    )
    candidates: list[tuple[int, float, float, bool]] = []
    history: deque[float] = deque(maxlen=_SLOW_EPISODE_BASELINE_NORMAL_FRAMES)

    for frame_num, duration_ms in frame_durations:
        rolling_baseline_ms = (
            _percentile(sorted(history), 0.5)
            if history
            else session_median
        )

        # Floor the local baseline at the session median so a locally fast stretch cannot
        # make its own ordinary frames look like an episode.
        baseline_ms = max(session_median, rolling_baseline_ms)
        is_slow = duration_ms >= baseline_ms * _SLOW_EPISODE_THRESHOLD_MULTIPLE
        candidates.append((frame_num, duration_ms, baseline_ms, is_slow))

        # Slow frames stay out of the history. A plateau outlasting the history would
        # otherwise fill it with its own frame time and stop detecting itself.
        if not is_slow:
            history.append(duration_ms)

    return candidates


def _group_slow_frame_episodes(
    candidates: list[tuple[int, float, float, bool]],
    present_timestamps: list[int],
) -> list[dict[str, Any]]:
    """Collapse consecutive slow candidates into episodes.

    A fast frame between two slow ones stays inside the open episode, because it is the
    short half of an oscillation catching up rather than a recovery. A frame far slower
    than the episode so far points at a different mechanism, so it starts a new one.
    """

    episodes: list[dict[str, Any]] = []
    episode: list[tuple[int, float, float, bool]] = []

    def finish_episode() -> None:
        if len(episode) < _SLOW_EPISODE_MIN_FRAMES:
            episode.clear()
            return

        durations = [entry[1] for entry in episode]
        baselines = sorted(entry[2] for entry in episode)
        start_frame = episode[0][0]
        end_frame = episode[-1][0]
        episodes.append(
            add_key(
                {
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "start_ns": present_timestamps[start_frame - 1],
                    "end_ns": present_timestamps[end_frame],
                    "frame_count": len(episode),
                    "slow_frame_count": sum(entry[3] for entry in episode),
                    "duration_ms": round(sum(durations), 3),
                    "mean_frame_ms": round(sum(durations) / len(durations), 3),
                    "baseline_ms": _percentile(baselines, 0.5),
                    "threshold_multiple": _SLOW_EPISODE_THRESHOLD_MULTIPLE,
                },
                "slow-frame-episode",
                start_frame,
                end_frame,
            )
        )
        episode.clear()

    for index, candidate in enumerate(candidates):
        frame_num, duration_ms, _, is_slow = candidate
        if not is_slow:
            next_is_slow = (
                index + 1 < len(candidates)
                and candidates[index + 1][3]
                and candidates[index + 1][0] == frame_num + 1
            )
            if (
                episode
                and next_is_slow
                and duration_ms
                < _percentile(sorted(entry[1] for entry in episode), 0.5)
                * _SLOW_EPISODE_CATCH_UP_FRACTION
            ):
                episode.append(candidate)
                continue
            finish_episode()
            continue
        if episode and frame_num != episode[-1][0] + 1:
            finish_episode()
        if (
            len(episode) >= _SLOW_EPISODE_MIN_FRAMES
            and duration_ms
            > _percentile(sorted(entry[1] for entry in episode), 0.5)
            * _SLOW_EPISODE_OUTLIER_MULTIPLE
        ):
            finish_episode()
        episode.append(candidate)
    finish_episode()
    return episodes


def _slow_frame_episodes(
    frame_durations: list[tuple[int, float]],
    present_timestamps: list[int],
) -> list[dict[str, Any]]:
    """Sustained slow-frame episodes, in frame order."""

    if not frame_durations:
        return []

    return _group_slow_frame_episodes(_slow_frame_episode_candidates(frame_durations), present_timestamps)


def _window_event_stats(
    con: Any,
    start_ns: int,
    end_ns: int,
    max_rows: int,
    *,
    event_names: Sequence[str] | None = None,
) -> EventStats:
    """ETW event counts and rates within a window, ranked by count.

    Pass event_names to count exactly those events instead of the window's own highest-count rows.
    The names come from StringIds at runtime, so they are bound as query parameters.
    """

    duration_ms = (end_ns - start_ns) / 1e6
    name_filter = ""
    params: tuple[Any, ...] = (start_ns, end_ns)

    if event_names is not None:
        if not event_names:
            return []

        name_filter = f"AND s.value IN ({', '.join('?' for _ in event_names)}) "
        params = (*params, *event_names)

    rows = _query_dicts(
        con,
        f'SELECT s.value AS event_name, COUNT(*) AS cnt FROM "{TABLE_ETW_EVENTS}" e '
        f'JOIN "{TABLE_GENERIC_EVENT_TYPES}" t ON e.typeId = t.typeId '
        f'JOIN "{TABLE_STRING_IDS}" s ON t.nameId = s.id '
        "WHERE e.timestamp >= ? AND e.timestamp < ? "
        f"{name_filter}"
        "GROUP BY s.value ORDER BY cnt DESC",
        max_rows=max(1, max_rows),
        params=params,
        suppress_errors=False,
    )
    return [
        {
            "event_name": row["event_name"],
            "count": int(row["cnt"]),
            "per_ms": round(int(row["cnt"]) / duration_ms, 4) if duration_ms > 0 else None,
        }
        for row in rows
    ]


def _rate_comparison(
    per_ms: float | None,
    baseline_per_ms: float | None,
) -> dict[str, Any]:
    """The window rate read against the baseline frame's rate for the same row.

    Raw counts scale with the window, so a slow frame shows more of nearly everything.
    Only the rate says whether the activity itself intensified.
    """

    if per_ms is None:
        return {}

    if not baseline_per_ms:
        return {
            "baseline_per_ms": 0.0,
            "rate_ratio": None,
            "rate_vs_baseline": "baseline_absent",
        }

    rate_ratio = round(per_ms / baseline_per_ms, 3)

    if rate_ratio >= _FRAME_SCAN_ELEVATED_RATE_RATIO:
        verdict = "elevated"
    elif rate_ratio <= _FRAME_SCAN_REDUCED_RATE_RATIO:
        verdict = "reduced"
    else:
        verdict = "unchanged"

    return {
        "baseline_per_ms": baseline_per_ms,
        "rate_ratio": rate_ratio,
        "rate_vs_baseline": verdict,
    }


def _annotate_rates_against_baseline(
    pairs: Iterable[tuple[dict[str, Any], Mapping[str, Any] | None]],
) -> int:
    """Annotate each row in place and report how many came out elevated."""

    elevated_count = 0

    for row, baseline_row in pairs:
        comparison = _rate_comparison(
            row.get("per_ms"),
            baseline_row.get("per_ms") if baseline_row else None,
        )
        row.update(comparison)

        if comparison.get("rate_vs_baseline") == "elevated":
            elevated_count += 1

    return elevated_count


def _window_wddm_summary(con: Any, tables: set[str], start_ns: int, end_ns: int) -> dict[str, dict[str, Any]]:
    duration_ms = (end_ns - start_ns) / 1e6
    summary: dict[str, dict[str, Any]] = {}
    for table, time_col, label in _WDDM_CAUSE_TABLES:
        columns = _existing_columns(con, table) if table in tables else set()
        if time_col not in columns:
            continue
        rows = _query_dicts(
            con,
            f'SELECT COUNT(*) AS event_count FROM "{table}" '
            f"WHERE {time_col} >= {int(start_ns)} AND {time_col} < {int(end_ns)}",
            max_rows=1,
        )
        count = int(rows[0].get("event_count") or 0) if rows else 0
        summary[label] = {
            "count": count,
            "per_ms": round(count / duration_ms, 4) if duration_ms > 0 else None,
        }
        # If possible, calculate and add the total and max event durations
        if {"start", "end"} <= columns:
            duration_rows = _query_dicts(
                con,
                f'SELECT SUM(CASE WHEN "end" > start THEN '
                f'CASE WHEN "end" < {int(end_ns)} THEN "end" ELSE {int(end_ns)} END - start '
                f'ELSE 0 END) AS total_ns, '
                f'MAX(CASE WHEN "end" > start THEN '
                f'CASE WHEN "end" < {int(end_ns)} THEN "end" ELSE {int(end_ns)} END - start '
                f'ELSE 0 END) AS max_ns FROM "{table}" '
                f"WHERE start >= {int(start_ns)} AND start < {int(end_ns)}",
                max_rows=1,
            )
            total_ns = int(duration_rows[0].get("total_ns") or 0) if duration_rows else 0
            max_ns = int(duration_rows[0].get("max_ns") or 0) if duration_rows else 0
            summary[label]["total_duration_ms"] = round(total_ns / 1e6, 3)
            summary[label]["max_duration_ms"] = round(max_ns / 1e6, 3)
    return summary


def _frame_summary(
    con: Any,
    tables: set[str],
    *,
    multi_report: bool,
    frame: int | None,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    """Locate graphics frames and their time windows; return per-frame CPU timing."""

    if multi_report:
        return _single_report_note(fact_type)
    source, present_call, present_timestamps, _ = _frame_present_timestamps(con, tables)
    if len(present_timestamps) < 2:
        return _no_frames_note(fact_type, single_present=len(present_timestamps) == 1)
    total_frames = len(present_timestamps) - 1
    frame_durations = [
        (i, (present_timestamps[i] - present_timestamps[i - 1]) / 1e6)
        for i in range(1, total_frames + 1)
    ]
    durations_ms = sorted(duration for _, duration in frame_durations)
    median = _percentile(durations_ms, 0.5)
    slowest = sorted(
        frame_durations,
        key=lambda pair: pair[1],
        reverse=True,
    )
    episodes = sorted(
        _slow_frame_episodes(frame_durations, present_timestamps),
        key=lambda episode: (
            episode["frame_count"],
            episode["duration_ms"],
        ),
        reverse=True,
    )
    payload: dict[str, Any] = {
        "ok": True,
        "fact_type": fact_type,
        "source": source,
        "present_call": present_call,
        "total_frames": total_frames,
        "frame_time_ms": {
            "min": round(durations_ms[0], 3) if durations_ms else None,
            "median": median,
            "p95": _percentile(durations_ms, 0.95),
            "p99": _percentile(durations_ms, 0.99),
            "max": round(durations_ms[-1], 3) if durations_ms else None,
        },
        "frame_time_pattern": _frame_time_pattern_summary(frame_durations)[0],
        "slowest_frames": [_frame_row(i, present_timestamps) for i, _ in slowest[: max(1, max_rows)]],
        "slow_frame_episodes": episodes[: max(1, max_rows)],
        "slow_frame_episode_count": len(episodes),
        "slow_frame_episodes_truncated": len(episodes) > max(1, max_rows),
        "note": (
            "frame_num is 1-based. Each frame is the interval [start_ns, end_ns) between two "
            "consecutive present timestamps (API present-return, or DxgKrnl ETW Present point when "
            "no API rows exist), so N frames come from N+1 presents. "
            "A systematic long-short alternation (frame_time_pattern.oscillation_detected) may "
            "indicate CPU-GPU swap-chain desynchronization, also called render queue flip-flop, "
            "where the long half waits on the GPU or a full present queue. If DLSS Frame Generation "
            "is active, the same cadence instead reflects bunched presents. Confirm either against "
            "a fence or Present wait on the submit thread before naming a cause. "
            "slow_frame_episodes groups sustained runs above a trailing rolling-median "
            "baseline; use an episode frame for a recurring hitch instead of assuming "
            "slowest_frames[0] represents the same mechanism. "
            "Read stutter_analysis.md for further instructions. "
            "Scan a frame's window with frame_scan for more evidence."
        ),
    }
    if median:
        short_frames = sum(1 for duration in durations_ms if duration < 0.5 * median)
        if short_frames:
            payload["short_frame_count"] = short_frames
    frame_index = frame
    if frame_index is not None and _is_frame_in_range(frame_index, total_frames):
        neighbor_indices = _neighbor_frames(frame_index, total_frames, radius=_NEIGHBORHOOD)
        payload["requested_frame"] = _frame_row(frame_index, present_timestamps)
        payload["neighbors"] = [_frame_row(i, present_timestamps) for i in neighbor_indices]
        neighborhood_durations = [
            (i, (present_timestamps[i] - present_timestamps[i - 1]) / 1e6)
            for i in neighbor_indices
        ]
        payload["requested_frame_time_pattern"] = _requested_frame_time_pattern(
            frame_index,
            neighborhood_durations,
        )
    elif frame_index is not None:
        payload.update(_frame_index_error(frame_index, total_frames))
    if len(present_timestamps) >= _MAX_PRESENT_CALLS:
        payload["truncated"] = True
    return payload


def _select_baseline_frame_index(
    frame_index: int,
    present_timestamps: list[int],
    baseline_strategy: str,
) -> int | None:
    """Pick the frame index to compare the target frame against."""

    total_frames = len(present_timestamps) - 1

    if baseline_strategy == "previous":
        return frame_index - 1 if frame_index - 1 >= 1 else None

    if baseline_strategy == "next":
        return frame_index + 1 if frame_index + 1 <= total_frames else None

    # median baseline strategy (session_median and neighborhood_median)
    durations = [(i, present_timestamps[i] - present_timestamps[i - 1]) for i in range(1, total_frames + 1)]
    sorted_durations = sorted(duration for _, duration in durations)
    median = sorted_durations[len(sorted_durations) // 2] if sorted_durations else 0
    if baseline_strategy == "session_median":
        candidates = [(i, duration) for i, duration in durations if i != frame_index]
        return (
            min(candidates, key=lambda pair: abs(pair[1] - median))[0]
            if candidates
            else None
        )

    # neighborhood_median baseline strategy
    neighbors = [
        (i, present_timestamps[i] - present_timestamps[i - 1])
        for i in _neighbor_frames(frame_index, total_frames, radius=_NEIGHBORHOOD)
        if i != frame_index
    ]

    if not neighbors:
        return None

    return min(neighbors, key=lambda pair: abs(pair[1] - median))[0]


def _resolve_baseline_frame_window(
    con: Any,
    tables: set[str],
    frame: int,
    baseline_strategy: str = "neighborhood_median",
) -> dict[str, Any]:
    """Resolve the comparison frame window used by frame-scoped facts."""

    if baseline_strategy not in _BASELINE_FRAME_STRATEGIES:
        return {
            "baseline_frame_error": (
                f"Unsupported baseline strategy {baseline_strategy!r}; "
                f"choose one of {', '.join(_BASELINE_FRAME_STRATEGIES)}."
            )
        }

    _, _, present_timestamps, _ = _frame_present_timestamps(con, tables)
    if len(present_timestamps) < 2:
        return {
            "baseline_frame_error": (
                "Need at least two graphics present calls to derive a baseline window."
            )
        }
    total_frames = len(present_timestamps) - 1
    if not _is_frame_in_range(frame, total_frames):
        return {"baseline_frame_error": f"frame {frame} is out of range (1..{total_frames})."}

    baseline_index = _select_baseline_frame_index(frame, present_timestamps, baseline_strategy)
    if baseline_index is None:
        return {
            "baseline_strategy": baseline_strategy,
            "baseline_frame_error": f"frame {frame} has no {baseline_strategy} baseline frame.",
        }

    return {
        "baseline_strategy": baseline_strategy,
        "baseline_frame": _frame_row(baseline_index, present_timestamps),
        "start_ns": present_timestamps[baseline_index - 1],
        "end_ns": present_timestamps[baseline_index],
    }


def _resolve_activity_baseline_frame_window(
    con: Any,
    tables: set[str],
    frame: int,
) -> dict[str, Any]:
    """Auto-pick neighborhood_median, or session_median inside a slow episode.

    frame_scan still exposes both strategies through --metric; this helper is the
    automatic choice for threads_newly_active when no strategy is passed in.
    """

    _, _, present_timestamps, _ = _frame_present_timestamps(con, tables)
    total_frames = len(present_timestamps) - 1
    if len(present_timestamps) < 2 or not _is_frame_in_range(frame, total_frames):
        return _resolve_baseline_frame_window(con, tables, frame)

    frame_durations = [
        (index, (present_timestamps[index] - present_timestamps[index - 1]) / 1e6)
        for index in range(1, total_frames + 1)
    ]
    in_slow_episode = any(
        episode["start_frame"] <= frame <= episode["end_frame"]
        for episode in _slow_frame_episodes(frame_durations, present_timestamps)
    )
    return _resolve_baseline_frame_window(
        con,
        tables,
        frame,
        "session_median" if in_slow_episode else "neighborhood_median",
    )


def _frame_scan(
    con: Any,
    tables: set[str],
    *,
    multi_report: bool,
    frame: int | None,
    baseline_strategy: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    """Scan a frame's window for ETW event evidence. Compare the event counts and rates to those
    of a baseline neighbor frame.
    """

    if multi_report:
        return _single_report_note(fact_type)
    if frame is None:
        return {
            "ok": True,
            "fact_type": fact_type,
            "note": "Pass --frame <N> (a frame number from frame_summary) to scan that frame's window.",
        }
    if baseline_strategy not in _BASELINE_FRAME_STRATEGIES:
        return {
            "ok": False,
            "fact_type": fact_type,
            "error": (
                f"Unsupported baseline strategy {baseline_strategy!r}; "
                f"choose one of {', '.join(_BASELINE_FRAME_STRATEGIES)}."
            ),
        }
    if not {TABLE_ETW_EVENTS, TABLE_GENERIC_EVENT_TYPES, TABLE_STRING_IDS} <= tables:
        return {
            "ok": True,
            "fact_type": fact_type,
            "source": "no_etw",
            "note": "No ETW generic events to scan; this cause-scan covers ETW driver/kernel events.",
        }
    source, _, present_timestamps, _ = _frame_present_timestamps(con, tables)
    if len(present_timestamps) < 2:
        return _no_frames_note(fact_type, single_present=len(present_timestamps) == 1)
    frame_index = frame
    total_frames = len(present_timestamps) - 1
    if not _is_frame_in_range(frame_index, total_frames):
        return {"ok": True, "fact_type": fact_type, **_frame_index_error(frame_index, total_frames)}
    win_start, win_end = present_timestamps[frame_index - 1], present_timestamps[frame_index]
    baseline_window = _resolve_baseline_frame_window(
        con,
        tables,
        frame_index,
        baseline_strategy,
    )
    window_event_stats = _window_event_stats(con, win_start, win_end, max_rows)
    window_wddm = _window_wddm_summary(con, tables, win_start, win_end)
    baseline_frame_error = baseline_window.get("baseline_frame_error")
    baseline_event_stats: EventStats = []
    baseline_wddm: dict[str, dict[str, Any]] = {}
    elevated_counts: dict[str, int] = {}

    if baseline_frame_error is None:
        base_start = int(baseline_window["start_ns"])
        base_end = int(baseline_window["end_ns"])
        baseline_event_stats = _window_event_stats(con, base_start, base_end, max_rows)
        baseline_wddm = _window_wddm_summary(con, tables, base_start, base_end)
        # window_event_stats and baseline_event_stats are independently truncated to max_rows,
        # so look each window event up in the baseline window itself.
        # Otherwise an event ranked just outside the baseline's rows reads as one it never saw.
        baseline_counts_by_event = {
            row["event_name"]: row
            for row in _window_event_stats(
                con,
                base_start,
                base_end,
                len(window_event_stats),
                event_names=[row["event_name"] for row in window_event_stats],
            )
        }
        elevated_counts = {
            "elevated_event_count": _annotate_rates_against_baseline(
                (row, baseline_counts_by_event.get(row["event_name"]))
                for row in window_event_stats
            ),
            "elevated_wddm_count": _annotate_rates_against_baseline(
                (entry, baseline_wddm.get(label))
                for label, entry in window_wddm.items()
            ),
        }

    payload: dict[str, Any] = {
        "ok": True,
        "fact_type": fact_type,
        "source": source,
        "baseline_strategy": baseline_strategy,
        "frame": _frame_row(frame_index, present_timestamps),
        "window_events": window_event_stats,
        "window_wddm": window_wddm,
        **elevated_counts,
        "note": (
            "Counts and per_ms rates are evidence, not a verdict: on their own they cannot "
            "establish a duration-based magnitude match. Each window row carries "
            "baseline_per_ms, rate_ratio, and a rate_vs_baseline verdict of elevated, "
            "unchanged, reduced, or baseline_absent, so read those rather than comparing "
            "counts by hand: a slow frame shows more of nearly everything, and a higher "
            "count with a lower rate is not elevated activity. elevated_event_count and "
            "elevated_wddm_count give how many rows came out elevated, and every "
            "comparison field is absent when no baseline frame resolved. baseline_absent "
            "means this one baseline frame recorded none of that event, which is a weak "
            "signal rather than proof of a new event. "
            "window_events lists ETW event rates; window_wddm lists paging, eviction, DMA, and "
            "context-switch rates plus total/max durations when start/end are available. "
            "Confirm a duration-bearing magnitude, temporal order, and "
            "entity attribution (stutter_analysis.md)."
        ),
    }

    if baseline_frame_error is None:
        payload["baseline_frame"] = baseline_window["baseline_frame"]
        payload["baseline_events"] = baseline_event_stats
        payload["baseline_wddm"] = baseline_wddm
    else:
        payload["baseline_frame_error"] = baseline_frame_error

    return payload

#TODO: This is a temporary constraint - we should be able to run the analysis on multiple reports. See DTSP-22718.
def _single_report_note(fact_type: str) -> dict[str, Any]:
    return {
        "ok": True,
        "fact_type": fact_type,
        "source": "multi_report_unsupported",
        "total_frames": 0,
        "note": "Frame analysis applies to a single report; load one .nsys-rep, not a directory.",
    }


def _no_frames_note(fact_type: str, *, single_present: bool) -> dict[str, Any]:
    if single_present:
        note = (
            "Only one graphics present call found; at least two are needed to form a frame. "
            "Recapture a longer trace."
        )
    else:
        note = (
            "No graphics API present calls (DXGI/Vulkan/OpenGL) and no DxgKrnl ETW Present "
            "(etwEventId=184) found; recapture with a graphics API or ETW present trace."
        )
    return {
        "ok": True,
        "fact_type": fact_type,
        "source": "none",
        "total_frames": 0,
        "note": note,
    }
