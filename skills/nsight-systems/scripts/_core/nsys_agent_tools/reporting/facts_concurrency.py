"""On-CPU concurrency facts: how wide did CPU work run, and how much core time did it cost?

A thread count alone cannot tell whether a session time range,
such as the CPU time duration while calculating a single graphics frame,
slowed from one busy thread or a wide parallel phase. This module turns complete sched-in
followed by sched-out intervals into concurrency and busy core-ms summaries,
so callers never write that SQL themselves.

It powers the ``thread_concurrency`` report fact (also ``cpu_concurrency`` / ``cpu_utilization`` / ``core_occupancy``),
over the whole run or over a single frame.

Metrics include:

- mean and maximum threads running on CPU at once;
- total busy core-ms in the window;
- for a frame, the same metrics on a session-median baseline.

Only paired sched-in/sched-out intervals count (open-ended spans are dropped),
and the OS idle thread is excluded. Results are descriptive measurements, not a saturation verdict.
"""

from __future__ import annotations

from typing import Any

from .facts_graphics_api import _resolve_frame_window
from .facts_graphics_frame import _resolve_baseline_frame_window
from .schema import TABLE_SCHED_EVENTS, TABLE_TARGET_INFO_SYSTEM_ENV
from .sql_utils import _existing_columns, _query_dicts

_REQUIRED_COLUMNS = {"start", "isSchedIn", "globalTid"}
_MAX_INTERVALS = 1_000_000


def _core_count(con: Any, tables: set[str]) -> int | None:
    if TABLE_TARGET_INFO_SYSTEM_ENV in tables:
        columns = _existing_columns(con, TABLE_TARGET_INFO_SYSTEM_ENV)
        if {"name", "value"} <= columns:
            rows = _query_dicts(
                con,
                f'SELECT value FROM "{TABLE_TARGET_INFO_SYSTEM_ENV}" '
                "WHERE name = 'CpuCores' LIMIT 1",
                max_rows=1,
            )
            if rows:
                try:
                    value = int(str(rows[0]["value"]))
                    if value > 0:
                        return value
                except (TypeError, ValueError):
                    pass

    if "cpu" not in _existing_columns(con, TABLE_SCHED_EVENTS):
        return None
    rows = _query_dicts(
        con,
        f'SELECT COUNT(DISTINCT cpu) AS n FROM "{TABLE_SCHED_EVENTS}"',
        max_rows=1,
    )
    value = int(rows[0].get("n") or 0) if rows else 0
    return value or None


def _confirmed_intervals(
    con: Any,
    start_ns: int | None,
    end_ns: int | None,
) -> list[dict[str, Any]]:
    columns = _existing_columns(con, TABLE_SCHED_EVENTS)
    if not columns >= _REQUIRED_COLUMNS:
        return []

    sched = (
        "WITH sched AS (SELECT globalTid, start, isSchedIn, "
        "LEAD(start) OVER (PARTITION BY globalTid ORDER BY start) AS next_start, "
        "LEAD(isSchedIn) OVER (PARTITION BY globalTid ORDER BY start) AS next_sched "
        f'FROM "{TABLE_SCHED_EVENTS}") '
    )
    terms = [
        "isSchedIn = 1",
        "next_sched = 0",
        "next_start > start",
        "(globalTid & 16777215) <> 0",
    ]
    interval_start = "start"
    interval_end = "next_start"
    if start_ns is not None and end_ns is not None:
        terms.extend(
            [
                f"start < {int(end_ns)}",
                f"next_start > {int(start_ns)}",
            ]
        )
        interval_start = (
            f"CASE WHEN start > {int(start_ns)} THEN start ELSE {int(start_ns)} END"
        )
        interval_end = (
            f"CASE WHEN next_start < {int(end_ns)} "
            f"THEN next_start ELSE {int(end_ns)} END"
        )

    return _query_dicts(
        con,
        sched
        + f"SELECT globalTid, {interval_start} AS interval_start, "
        f"{interval_end} AS interval_end FROM sched "
        f"WHERE {' AND '.join(terms)} ORDER BY interval_start",
        max_rows=_MAX_INTERVALS,
    )


def _concurrency_metrics(
    intervals: list[dict[str, Any]],
    start_ns: int,
    end_ns: int,
    core_count: int | None,
) -> dict[str, Any]:
    window_ns = max(0, end_ns - start_ns)
    busy_ns = 0
    events: list[tuple[int, int]] = []
    for interval in intervals:
        interval_start = int(interval["interval_start"])
        interval_end = int(interval["interval_end"])
        if interval_end <= interval_start:
            continue
        busy_ns += interval_end - interval_start
        events.append((interval_start, 1))
        events.append((interval_end, -1))

    concurrent = 0
    maximum = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        concurrent += delta
        maximum = max(maximum, concurrent)

    mean = busy_ns / window_ns if window_ns > 0 else 0.0
    return {
        "window_ms": round(window_ns / 1e6, 3),
        "core_count": core_count,
        "confirmed_intervals": len(intervals),
        "mean_concurrent_threads": round(mean, 3),
        "max_concurrent_threads": maximum,
        "total_busy_core_ms": round(busy_ns / 1e6, 3),
        "work_per_ms": round(mean, 3),
    }


def _thread_concurrency_summary(
    con: Any,
    tables: set[str],
    *,
    multi_report: bool,
    frame: int | None,
    fact_type: str,
) -> dict[str, Any]:
    """Measure confirmed on-CPU work over a frame or the captured session."""

    if multi_report:
        return {
            "ok": True,
            "fact_type": fact_type,
            "scope": "multi_report_unsupported",
            "note": "Thread-concurrency analysis applies to one report at a time.",
        }
    if (
        TABLE_SCHED_EVENTS not in tables
        or not _existing_columns(con, TABLE_SCHED_EVENTS) >= _REQUIRED_COLUMNS
    ):
        return {
            "ok": False,
            "fact_type": fact_type,
            "error": "SCHED_EVENTS with start, isSchedIn, and globalTid is required.",
        }

    core_count = _core_count(con, tables)
    if frame is not None:
        window = _resolve_frame_window(con, tables, frame)
        if "requested_frame_error" in window:
            return {"ok": True, "fact_type": fact_type, **window}

        start_ns = int(window["start_ns"])
        end_ns = int(window["end_ns"])
        payload: dict[str, Any] = {
            "ok": True,
            "fact_type": fact_type,
            "scope": "frame",
            "window": window,
            "window_metrics": _concurrency_metrics(
                _confirmed_intervals(con, start_ns, end_ns),
                start_ns,
                end_ns,
                core_count,
            ),
            "note": (
                "window_metrics and baseline_metrics report mean/max concurrent threads and "
                "total busy core-ms from paired sched-in/sched-out intervals (OS idle excluded). "
                "Compare them for relative fan-out and CPU work; they are measurements, not a "
                "saturation verdict."
            ),
        }
        baseline = _resolve_baseline_frame_window(
            con,
            tables,
            frame,
            "session_median",
        )
        if "baseline_frame_error" in baseline:
            payload["baseline_frame_error"] = baseline["baseline_frame_error"]
        else:
            baseline_start = int(baseline["start_ns"])
            baseline_end = int(baseline["end_ns"])
            payload["baseline_frame"] = baseline["baseline_frame"]
            payload["baseline_metrics"] = _concurrency_metrics(
                _confirmed_intervals(con, baseline_start, baseline_end),
                baseline_start,
                baseline_end,
                core_count,
            )
        return payload

    rows = _confirmed_intervals(con, None, None)
    if not rows:
        return {
            "ok": True,
            "fact_type": fact_type,
            "scope": "session",
            "note": "No confirmed sched-in/sched-out intervals are available.",
        }
    start_ns = min(int(row["interval_start"]) for row in rows)
    end_ns = max(int(row["interval_end"]) for row in rows)
    return {
        "ok": True,
        "fact_type": fact_type,
        "scope": "session",
        "session_metrics": _concurrency_metrics(rows, start_ns, end_ns, core_count),
        "note": (
            "session_metrics report mean/max concurrent threads and total busy core-ms from "
            "paired sched-in/sched-out intervals (OS idle excluded). They describe observed "
            "concurrency and CPU work, not a saturation verdict."
        ),
    }
