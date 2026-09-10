"""Per-thread CPU scheduling facts: is a thread working, or stuck waiting?

Every thread is either running on a CPU or parked waiting for something. This
module turns the OS scheduling log into a simple per-thread summary so callers
never write that SQL themselves. It powers the ``thread_scheduling`` report fact
(also ``blocking_waits`` / ``sched_summary``), over the whole run or a single
frame, reporting per thread:

- upper bounds on how long it spent running vs. blocked, with confidence ratios,
  real thread IDs, and names;
- why it usually goes to sleep -- a voluntary wait, lock contention, or being
  preempted because more threads are busy than there are CPU cores;
- the specific OS wait calls (e.g. `WaitForSingleObjectEx` on Windows,
  `pthread_cond_wait` on Linux) it lost the most time to.
"""

from __future__ import annotations

from typing import Any

from .evidence import add_key
from .facts_cuda_common import _key_rows, _string_expr
from .facts_graphics_frame import _resolve_activity_baseline_frame_window
from .frame_utils import FRAME_ERROR_KEY, _resolve_frame_window
from .schema import (
    TABLE_DXGI_API,
    TABLE_ENUM_SCHEDULING_THREAD_BLOCK,
    TABLE_OPENGL_API,
    TABLE_OSRT_API,
    TABLE_SCHED_EVENTS,
    TABLE_STRING_IDS,
    TABLE_THREAD_NAMES,
    TABLE_VULKAN_API,
)
from .sql_utils import _existing_columns, _query_dicts

_MAX_QUERY_FETCH_SIZE = 100_000
_REQUIRED_SCHED_COLUMNS = {"start", "isSchedIn", "globalTid"}

# A thread's off-CPU time is often split across several block reasons. Only Keep the reasons that
# are a material share of the thread's off-CPU time.  If none pass this threshold,
# fall back to the top _TOP_MINOR_BLOCK_REASONS_COUNT reasons and let the caller see that none dominates.
_THREAD_TIME_FRACTION_THRESHOLD = 0.25
_TOP_MINOR_BLOCK_REASONS_COUNT = 4
_FRAME_BOUNDARY_API_TABLES = (
    TABLE_DXGI_API,
    TABLE_VULKAN_API,
    TABLE_OPENGL_API,
)


def _window_clause(column: str, start_ns: int | None, end_ns: int | None) -> str:
    if start_ns is None or end_ns is None:
        return ""
    return f" {column} >= {int(start_ns)} AND {column} < {int(end_ns)}"


def _thread_times(
    con: Any,
    start_ns: int | None,
    end_ns: int | None,
    max_rows: int,
    order_by: str,
    priority_global_tids: set[int] | None = None,
) -> list[dict[str, Any]] | None:
    """Per-thread on-CPU and blocked (off-CPU) nanosecond bounds; None if shape is wrong."""

    if not _REQUIRED_SCHED_COLUMNS.issubset(_existing_columns(con, TABLE_SCHED_EVENTS)):
        return None
    if order_by not in {
        "blocked_upper_bound_ns DESC",
        "on_cpu_upper_bound_ns DESC",
    }:
        raise ValueError(f"Unsupported thread scheduling order: {order_by}")

    # Put frame-boundary threads first so the bounded query cannot discard them
    # before the caller applies the frame-aware ranking.
    priority_order = ""
    if priority_global_tids:
        tid_list = ", ".join(str(int(tid)) for tid in sorted(priority_global_tids))
        priority_order = f"CASE WHEN globalTid IN ({tid_list}) THEN 0 ELSE 1 END, "

    # Attribute each interval by its starting state for the upper bounds, and
    # separately sum strict opposite-state transitions for the lower bounds.
    sched = (
        "WITH sched AS (SELECT globalTid, start, isSchedIn, "
        "LEAD(start) OVER (PARTITION BY globalTid ORDER BY start) AS next_start, "
        "LEAD(isSchedIn) OVER (PARTITION BY globalTid ORDER BY start) AS next_sched "
        f'FROM "{TABLE_SCHED_EVENTS}") '
    )
    if start_ns is not None and end_ns is not None:
        clipped_start = f"CASE WHEN start > {int(start_ns)} THEN start ELSE {int(start_ns)} END"
        clipped_end = (
            f"CASE WHEN COALESCE(next_start, {int(end_ns)}) < {int(end_ns)} "
            f"THEN COALESCE(next_start, {int(end_ns)}) ELSE {int(end_ns)} END"
        )
        duration = f"({clipped_end}) - ({clipped_start})"
        eligible = (
            f"start < {int(end_ns)} AND "
            f"COALESCE(next_start, {int(end_ns)}) > {int(start_ns)}"
        )
        on_cpu_transition = "next_sched = 0"
        blocked_transition = "next_sched = 1"
    else:
        duration = "next_start - start"
        eligible = "next_start IS NOT NULL"
        on_cpu_transition = "next_sched = 0"
        blocked_transition = "next_sched = 1"
    sql = sched + (
        "SELECT globalTid, "
        f"SUM(CASE WHEN isSchedIn = 1 THEN {duration} ELSE 0 END) AS on_cpu_upper_bound_ns, "
        f"SUM(CASE WHEN isSchedIn = 1 AND {on_cpu_transition} "
        f"THEN {duration} ELSE 0 END) AS on_cpu_lower_bound_ns, "
        f"SUM(CASE WHEN isSchedIn = 0 THEN {duration} ELSE 0 END) AS blocked_upper_bound_ns, "
        f"SUM(CASE WHEN isSchedIn = 0 AND {blocked_transition} "
        f"THEN {duration} ELSE 0 END) AS blocked_lower_bound_ns, "
        "SUM(CASE WHEN isSchedIn = 0 THEN 1 ELSE 0 END) AS block_events "
        f"FROM sched WHERE {eligible} "
        f"GROUP BY globalTid ORDER BY {priority_order}{order_by}"
    )
    return _query_dicts(con, sql, max_rows=max(2, max_rows + 1))


def _top_block_reasons_by_thread(
    con: Any, tables: set[str], start_ns: int | None, end_ns: int | None
) -> dict[int, list[dict[str, Any]]]:
    """Map globalTid to the filtered top block reasons by attributed duration."""

    columns = _existing_columns(con, TABLE_SCHED_EVENTS)
    if "threadBlock" not in columns or "isSchedIn" not in columns:
        return {}
    if TABLE_ENUM_SCHEDULING_THREAD_BLOCK in tables:
        reason_expr = "COALESCE(b.name, CAST(s.threadBlock AS TEXT))"
        join = (
            f' LEFT JOIN "{TABLE_ENUM_SCHEDULING_THREAD_BLOCK}" b '
            "ON s.threadBlock = b.id"
        )
    else:
        reason_expr = "CAST(s.threadBlock AS TEXT)"
        join = ""
    sched = (
        "WITH sched AS (SELECT globalTid, start, isSchedIn, threadBlock, "
        "LEAD(start) OVER (PARTITION BY globalTid ORDER BY start) AS next_start "
        f'FROM "{TABLE_SCHED_EVENTS}") '
    )
    if start_ns is not None and end_ns is not None:
        clipped_start = f"CASE WHEN s.start > {int(start_ns)} THEN s.start ELSE {int(start_ns)} END"
        clipped_end = (
            f"CASE WHEN COALESCE(s.next_start, {int(end_ns)}) < {int(end_ns)} "
            f"THEN COALESCE(s.next_start, {int(end_ns)}) ELSE {int(end_ns)} END"
        )
        duration = f"({clipped_end}) - ({clipped_start})"
        eligible = (
            f"s.start < {int(end_ns)} AND "
            f"COALESCE(s.next_start, {int(end_ns)}) > {int(start_ns)}"
        )
    else:
        duration = "s.next_start - s.start"
        eligible = "s.next_start IS NOT NULL"
    rows = _query_dicts(
        con,
        sched
        + f"SELECT s.globalTid AS globalTid, {reason_expr} AS reason, "
        f"COUNT(*) AS events, SUM({duration}) AS total_ns "
        f"FROM sched s{join} WHERE s.isSchedIn = 0 AND {eligible} "
        "GROUP BY globalTid, reason "
        "ORDER BY globalTid, total_ns DESC, events DESC",
        max_rows=_MAX_QUERY_FETCH_SIZE,
    )
    raw: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        gid = row.get("globalTid")
        reason = row.get("reason")
        total_ns = row.get("total_ns")
        if gid is None or reason is None or total_ns is None:
            continue
        raw.setdefault(int(gid), []).append(
            {
                "reason": str(reason),
                "events": int(row.get("events") or 0),
                "total_ns": int(total_ns),
            }
        )
    return {gid: _select_top_block_reasons(reasons) for gid, reasons in raw.items()}


def _select_top_block_reasons(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the block reasons worth reporting from a duration-sorted list.

    Concentrated blocking keeps every reason at or above _THREAD_TIME_FRACTION_THRESHOLD
    of the thread's attributed off-CPU time. If no reason clears that threshold,
    keep the top _TOP_MINOR_BLOCK_REASONS_COUNT reasons.
    """

    total_ns = sum(reason["total_ns"] for reason in reasons)
    scored: list[tuple[float, dict[str, Any]]] = []
    for reason in reasons:
        share = reason["total_ns"] / total_ns if total_ns > 0 else 0.0
        scored.append(
            (
                share,
                {
                    "reason": reason["reason"],
                    "events": reason["events"],
                    "total_ms": round(reason["total_ns"] / 1e6, 3),
                    "pct_of_off_cpu": round(share * 100, 1),
                },
            )
        )
    significant = [entry for share, entry in scored if share >= _THREAD_TIME_FRACTION_THRESHOLD]
    if significant:
        return significant
    return [entry for _, entry in scored[:_TOP_MINOR_BLOCK_REASONS_COUNT]]


def _thread_names(con: Any, tables: set[str]) -> dict[int, str]:
    """Map globalTid -> thread name via ThreadNames joined to StringIds."""

    if TABLE_THREAD_NAMES not in tables or TABLE_STRING_IDS not in tables:
        return {}
    if not {"nameId", "globalTid"}.issubset(_existing_columns(con, TABLE_THREAD_NAMES)):
        return {}
    rows = _query_dicts(
        con,
        f'SELECT t.globalTid AS globalTid, s.value AS thread_name FROM "{TABLE_THREAD_NAMES}" t '
        f'JOIN "{TABLE_STRING_IDS}" s ON t.nameId = s.id',
        max_rows=_MAX_QUERY_FETCH_SIZE,
    )
    return {
        int(row["globalTid"]): str(row["thread_name"])
        for row in rows
        if row.get("globalTid") is not None and row.get("thread_name") is not None
    }


def _sched_events_by_thread(
    con: Any,
    start_ns: int,
    end_ns: int,
    process_ids: set[int],
) -> dict[int, dict[str, int]]:
    """Per-thread scheduling event count and first timestamp inside a window."""

    if not process_ids:
        return {}

    process_list = ", ".join(str(int(process_id)) for process_id in sorted(process_ids))
    rows = _query_dicts(
        con,
        f'SELECT globalTid, COUNT(*) AS sched_events, MIN(start) AS first_start '
        f'FROM "{TABLE_SCHED_EVENTS}" '
        f"WHERE start >= {int(start_ns)} AND start < {int(end_ns)} "
        f"AND (globalTid >> 24) IN ({process_list}) "
        "GROUP BY globalTid",
        max_rows=_MAX_QUERY_FETCH_SIZE,
    )
    return {
        int(row["globalTid"]): {
            "sched_events": int(row["sched_events"]),
            "first_start": int(row["first_start"]),
        }
        for row in rows
        if row.get("globalTid") is not None
        and row.get("sched_events") is not None
        and row.get("first_start") is not None
    }


def _newly_active_threads(
    con: Any,
    names: dict[int, str],
    graphics_process_ids: set[int],
    window: dict[str, Any],
    baseline_window: dict[str, Any],
    max_rows: int,
    on_cpu_ms_by_global_tid: dict[int, float],
) -> tuple[list[dict[str, Any]], int]:
    """Threads active in the frame but absent from its clean baseline."""

    frame_activity = _sched_events_by_thread(
        con,
        int(window["start_ns"]),
        int(window["end_ns"]),
        graphics_process_ids,
    )
    baseline_activity = _sched_events_by_thread(
        con,
        int(baseline_window["start_ns"]),
        int(baseline_window["end_ns"]),
        graphics_process_ids,
    )
    newly_active: list[dict[str, Any]] = []
    for gid, activity in frame_activity.items():
        baseline_events = baseline_activity.get(gid, {}).get("sched_events", 0)
        if baseline_events > 0 or (gid & 0xFFFFFF) == 0:
            continue

        newly_active.append(
            add_key(
                {
                    "global_tid": gid,
                    "tid": gid & 0xFFFFFF,
                    "process_id": gid >> 24,
                    "thread_name": names.get(gid),
                    # Frame activity is already filtered to the graphics processes,
                    # so every row here belongs to one.
                    "is_graphics_process": True,
                    "on_cpu_ms_upper_bound": on_cpu_ms_by_global_tid.get(gid, 0.0),
                    "frame_sched_events": activity["sched_events"],
                    "baseline_sched_events": 0,
                    "first_event_ms_into_frame": round(
                        (activity["first_start"] - int(window["start_ns"])) / 1e6,
                        3,
                    ),
                },
                "thread-scheduling-newly-active",
                gid,
            )
        )

    # Rank by on-CPU time, not switch count: a worker that churned through many
    # short slices did less of the frame's work than one that ran a long slice,
    # and only on-CPU time yields callstack samples to attribute.
    newly_active.sort(
        key=lambda thread: (
            thread["on_cpu_ms_upper_bound"],
            thread["frame_sched_events"],
            thread["thread_name"] is not None,
        ),
        reverse=True,
    )
    total = len(newly_active)
    return newly_active[:max_rows], total


def _graphics_thread_identity(
    con: Any,
    tables: set[str],
    start_ns: int | None,
    end_ns: int | None,
    present_global_tid: int | None = None,
) -> tuple[set[int], set[int]]:
    if start_ns is None or end_ns is None:
        return set(), set()

    process_ids: set[int] = set()
    frame_boundary_global_tids: set[int] = set()

    for table in _FRAME_BOUNDARY_API_TABLES:
        if table not in tables:
            continue

        columns = _existing_columns(con, table)

        if not {"start", "globalTid"} <= columns:
            continue

        # The present at end_ns closes this frame, so its thread owns the boundary.
        # The present at start_ns belongs to the previous frame.
        rows = _query_dicts(
            con,
            f'SELECT DISTINCT globalTid FROM "{table}" '
            f"WHERE start = {int(end_ns)}",
            max_rows=_MAX_QUERY_FETCH_SIZE,
        )

        frame_boundary_global_tids.update(
            int(row["globalTid"])
            for row in rows
            if row.get("globalTid") is not None
        )

    # ETW-only captures have no graphics API call table to name the presenting thread,
    # so fall back to the thread the present source itself reported for this frame.
    if not frame_boundary_global_tids and present_global_tid is not None:
        frame_boundary_global_tids.add(present_global_tid)

    process_ids.update(
        global_tid >> 24 for global_tid in frame_boundary_global_tids
    )
    return process_ids, frame_boundary_global_tids


def _blocking_waits(
    con: Any, tables: set[str], start_ns: int | None, end_ns: int | None, max_rows: int
) -> list[dict[str, Any]]:
    """Top OS-runtime wait calls (e.g. `WaitForSingleObjectEx` on Windows, `pthread_cond_wait` on Linux) ranked by total time."""

    if TABLE_OSRT_API not in tables:
        return []
    if not {"start", "end", "nameId"}.issubset(_existing_columns(con, TABLE_OSRT_API)):
        return []
    name_expr, join = _string_expr(con, TABLE_OSRT_API, "o", "nameId", tables)
    window = _window_clause("o.start", start_ns, end_ns)
    where = f" WHERE{window}" if window else ""
    rows = _query_dicts(
        con,
        f"SELECT {name_expr} AS func, COUNT(*) AS calls, "
        f'SUM(o."end" - o.start) AS total_ns, AVG(o."end" - o.start) AS avg_ns '
        f'FROM "{TABLE_OSRT_API}" o{join}{where} GROUP BY func ORDER BY total_ns DESC',
        max_rows=max(1, max_rows),
    )
    return [
        {
            "func": row["func"],
            "calls": int(row["calls"]),
            "total_ms": round(int(row["total_ns"]) / 1e6, 3),
            "avg_ms": round(float(row["avg_ns"]) / 1e6, 3),
        }
        for row in rows
        if row.get("total_ns") is not None
    ]


def _enrich_thread_rows(
    rows: list[dict[str, Any]],
    names: dict[int, str],
    top_block_reasons_by_thread: dict[int, list[dict[str, Any]]],
    graphics_process_ids: set[int],
    frame_boundary_global_tids: set[int],
    window: dict[str, Any] | None,
    key_namespace: str,
) -> list[dict[str, Any]]:
    """Add thread identity, upper bounds, confidence, reasons, and stable keys."""

    threads: list[dict[str, Any]] = []
    frame_ns = int(window["end_ns"]) - int(window["start_ns"]) if window else 0
    for row in rows:
        raw_gid = row.get("globalTid")
        if raw_gid is None:
            continue
        gid = int(raw_gid)
        on_cpu_upper_bound_ns = int(row.get("on_cpu_upper_bound_ns") or 0)
        on_cpu_lower_bound_ns = int(row.get("on_cpu_lower_bound_ns") or 0)
        blocked_upper_bound_ns = int(row.get("blocked_upper_bound_ns") or 0)
        blocked_lower_bound_ns = int(row.get("blocked_lower_bound_ns") or 0)
        top_block_reasons = top_block_reasons_by_thread.get(gid, [])
        thread = {
            "global_tid": gid,
            "tid": gid & 0xFFFFFF,
            "process_id": gid >> 24,
            "thread_name": names.get(gid),
            "on_cpu_ms_upper_bound": round(on_cpu_upper_bound_ns / 1e6, 3),
            "on_cpu_confirmed_pct": (
                round(on_cpu_lower_bound_ns / on_cpu_upper_bound_ns * 100, 1)
                if on_cpu_upper_bound_ns > 0
                else None
            ),
            "blocked_ms_upper_bound": round(blocked_upper_bound_ns / 1e6, 3),
            "blocked_confirmed_pct": (
                round(blocked_lower_bound_ns / blocked_upper_bound_ns * 100, 1)
                if blocked_upper_bound_ns > 0
                else None
            ),
            "block_events": int(row.get("block_events") or 0),
            "dominant_block_reason": (
                top_block_reasons[0]["reason"] if top_block_reasons else None
            ),
            "top_block_reasons": top_block_reasons,
        }
        if graphics_process_ids:
            thread["is_graphics_process"] = (gid >> 24) in graphics_process_ids
        if frame_boundary_global_tids:
            thread["is_frame_boundary_thread"] = gid in frame_boundary_global_tids
        if frame_ns > 0:
            thread["on_cpu_pct_of_frame"] = round(
                on_cpu_upper_bound_ns / frame_ns * 100,
                3,
            )
            thread["blocked_pct_of_frame"] = round(
                blocked_upper_bound_ns / frame_ns * 100,
                3,
            )
        threads.append(add_key(thread, key_namespace, gid))
    return threads


def _frame_boundary_thread_row(
    threads: list[dict[str, Any]],
    present_global_tid: int | None,
) -> dict[str, Any] | None:
    """The enriched row for the thread that closed the frame with its present call.

    This thread identifies the frame's owner even when it barely ran, so it is
    reported beside the rankings instead of inside them. Ranking it first hid the
    threads that actually consumed the frame behind a thread with far less
    measured time.
    """

    boundary_rows = [
        thread for thread in threads if thread.get("is_frame_boundary_thread")
    ]

    if not boundary_rows:
        return None

    # More than one present at the closing timestamp is unexpected; order the
    # tie so the reported row does not depend on query row order.
    boundary_rows.sort(
        key=lambda thread: (
            thread["global_tid"] == present_global_tid,
            thread["on_cpu_ms_upper_bound"],
        ),
        reverse=True,
    )
    row = {key: value for key, value in boundary_rows[0].items() if key != "key"}
    return add_key(row, "thread-scheduling-frame-boundary", row["global_tid"])


def _is_unconfirmed_full_window_blocker(thread: dict[str, Any]) -> bool:
    """True when blocked time is almost the whole window from one unconfirmed clip.

    A thread asleep across the frame has no in-window transition, so its off-CPU
    interval is clipped to the window and reported from a single event. That
    looks like a top blocker even though no sched-out/sched-in pair was seen.
    """

    return (
        thread["block_events"] <= 1
        and (thread["blocked_confirmed_pct"] or 0.0) == 0.0
        and thread.get("blocked_pct_of_frame", 0.0) >= 95.0
    )


def _thread_scheduling_summary(
    con: Any,
    tables: set[str],
    *,
    multi_report: bool,
    frame: int | None,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    """Per-thread on-CPU and blocked (off-CPU) nanosecond duration sums. Sched event
    sequences that express schedIn 0 followed by schedIn 0 can happen with some threads
    on Windows. Therefore, the sum of durations without these gaps, and the one with
    these gaps are more like lower/upper bound on the total time rather than exact
    blocked CPU time for the thread.
    So relying on either value may be inaccurate, and this potential inaccuracy is
    communicated to the agent. It shouldn't treat the CPU on/blocked ms as verdict,
    and should depend more on magnitude and ordering instead of precision.
    """

    # Each scheduling fact returns two independent thread-time rankings, so this
    # limit keeps the combined payload similar in scale to other facts.
    max_thread_group_rows = max(max_rows // 2, 1)
    if multi_report:
        return _single_report_note(fact_type)
    window: dict[str, Any] | None = None
    if frame is not None:
        window = _resolve_frame_window(con, tables, frame)
        if FRAME_ERROR_KEY in window:
            return {"ok": True, "fact_type": fact_type, **window}
    start_ns = window["start_ns"] if window else None
    end_ns = window["end_ns"] if window else None
    graphics_process_ids, frame_boundary_global_tids = _graphics_thread_identity(
        con,
        tables,
        start_ns,
        end_ns,
        window.get("present_global_tid") if window else None,
    )
    # Frame-aware confirmation ranking must see every thread rather than only the
    # raw-time leaders, which are often unconfirmed full-window sleepers.
    query_rows = (
        _MAX_QUERY_FETCH_SIZE
        if window
        else max_thread_group_rows
    )
    blocked_rows = _thread_times(
        con,
        start_ns,
        end_ns,
        query_rows,
        "blocked_upper_bound_ns DESC",
        frame_boundary_global_tids,
    )
    on_cpu_rows = _thread_times(
        con,
        start_ns,
        end_ns,
        query_rows,
        "on_cpu_upper_bound_ns DESC",
        frame_boundary_global_tids,
    )
    if blocked_rows is None or on_cpu_rows is None:
        return _no_sched_note(fact_type)
    threads_by_blocked_ms_upper_bound_truncated = len(blocked_rows) > max_thread_group_rows
    top_block_reasons_by_thread = _top_block_reasons_by_thread(
        con,
        tables,
        start_ns,
        end_ns,
    )
    names = _thread_names(con, tables)
    threads_by_blocked_ms_upper_bound = _enrich_thread_rows(
        blocked_rows,
        names,
        top_block_reasons_by_thread,
        graphics_process_ids,
        frame_boundary_global_tids,
        window,
        "thread-scheduling-blocked",
    )
    threads_by_on_cpu_ms_upper_bound = _enrich_thread_rows(
        on_cpu_rows,
        names,
        top_block_reasons_by_thread,
        graphics_process_ids,
        frame_boundary_global_tids,
        window,
        "thread-scheduling-on-cpu",
    )
    idle_threads = [
        thread
        for thread in threads_by_on_cpu_ms_upper_bound
        if thread["tid"] == 0
    ]
    threads_by_on_cpu_ms_upper_bound = [
        thread
        for thread in threads_by_on_cpu_ms_upper_bound
        if thread["tid"] != 0
    ]

    # The OS idle thread is reported as os_idle_context instead of being ranked, so
    # truncation has to count the rows a caller can actually see.
    threads_by_on_cpu_ms_upper_bound_truncated = (
        len(threads_by_on_cpu_ms_upper_bound) > max_thread_group_rows
    )
    demoted_full_window_blocked_count = sum(
        _is_unconfirmed_full_window_blocker(thread)
        for thread in threads_by_blocked_ms_upper_bound
    )
    threads_by_blocked_ms_upper_bound.sort(
        key=lambda thread: (
            (
                not _is_unconfirmed_full_window_blocker(thread),
                bool(thread.get("is_graphics_process"))
                and thread["blocked_ms_upper_bound"] > 0,
                thread["blocked_ms_upper_bound"],
                thread["dominant_block_reason"] is not None,
                thread["thread_name"] is not None,
            )
            if window
            else (
                thread["blocked_ms_upper_bound"],
                thread["dominant_block_reason"] is not None,
                thread["thread_name"] is not None,
            )
        ),
        reverse=True,
    )
    threads_by_on_cpu_ms_upper_bound.sort(
        key=lambda thread: (
            (
                bool(thread.get("is_graphics_process"))
                and thread["on_cpu_ms_upper_bound"] > 0,
                thread["on_cpu_ms_upper_bound"],
                thread["thread_name"] is not None,
            )
            if window
            else (
                thread["on_cpu_ms_upper_bound"],
                thread["thread_name"] is not None,
            )
        ),
        reverse=True,
    )

    # Captured before truncation: the presenting thread can now sort below
    # max_thread_group_rows, and it must still reach the payload.
    frame_boundary_thread = _frame_boundary_thread_row(
        threads_by_on_cpu_ms_upper_bound,
        window.get("present_global_tid") if window else None,
    )

    # Captured before truncation so newly-active rows can be ranked against every
    # thread's on-CPU time, not just the rows that survive max_rows.
    on_cpu_ms_by_global_tid = {
        thread["global_tid"]: thread["on_cpu_ms_upper_bound"]
        for thread in threads_by_on_cpu_ms_upper_bound
    }
    threads_by_blocked_ms_upper_bound = threads_by_blocked_ms_upper_bound[
        :max_thread_group_rows
    ]
    threads_by_on_cpu_ms_upper_bound = threads_by_on_cpu_ms_upper_bound[
        :max_thread_group_rows
    ]
    payload: dict[str, Any] = {
        "ok": True,
        "fact_type": fact_type,
        "scope": "frame" if window else "session",
        "threads_max_rows": max_rows,
        "threads_by_blocked_ms_upper_bound_returned_count": len(threads_by_blocked_ms_upper_bound),
        "threads_by_blocked_ms_upper_bound_truncated": (
            threads_by_blocked_ms_upper_bound_truncated
        ),
        "threads_by_on_cpu_ms_upper_bound_returned_count": len(threads_by_on_cpu_ms_upper_bound),
        "threads_by_on_cpu_ms_upper_bound_truncated": (threads_by_on_cpu_ms_upper_bound_truncated),
        "threads_by_blocked_ms_upper_bound": threads_by_blocked_ms_upper_bound,
        "threads_by_on_cpu_ms_upper_bound": threads_by_on_cpu_ms_upper_bound,
        **(
            {"frame_boundary_thread": frame_boundary_thread}
            if frame_boundary_thread is not None
            else {}
        ),
        "blocking_waits": _key_rows(
            _blocking_waits(con, tables, start_ns, end_ns, max_rows), "blocking-wait", "func"
        ),
        "note": (
            "threads_by_blocked_ms_upper_bound ranks blocked threads (off-CPU) by "
            "blocked_ms_upper_bound time; "
            "threads_by_on_cpu_ms_upper_bound ranks on-CPU threads by on_cpu_ms_upper_bound time. "
            "Missing thread scheduling transition events can inflate upper bounds; "
            "*_confirmed_pct is the share backed by sched-in/sched-out event pairs. "
            "Frame-spanning blocked bounds from one unconfirmed event are retained but "
            "demoted below transition-backed rows. "
            "Low confidence makes a bound useful for ranking, not exact timing. "
            "Frame percentages are not whole-system utilization, and neither candidate ranking proves "
            "the critical path or cause. "
            "dominant_block_reason is the longest attributed off-CPU reason; "
            "each top_block_reasons entry gives pct_of_off_cpu. Reasons at or above 25% are kept, "
            "or the top 4 when none reaches that threshold, so percentages need not sum to 100%. "
            "Limiter and background waits may be concurrent effects; "
            "frame-scoped rankings order by measured time within the graphics process, and the "
            "thread that closed the frame with its present call is reported as "
            "frame_boundary_thread rather than ranked ahead of larger measured times; "
            "blocking_waits ranks OSRT_API waits. With --frame <N>, results are frame-local; "
            "run the callstack follow-up before naming a cause (as detailed in stutter_analysis.md)."
        ),
    }
    if window:
        payload["window"] = window
        payload[
            "threads_by_blocked_ms_upper_bound_demoted_unconfirmed_full_window_count"
        ] = demoted_full_window_blocked_count
        baseline_window = _resolve_activity_baseline_frame_window(
            con,
            tables,
            frame or 0,
        )
        if "baseline_frame_error" in baseline_window:
            payload["threads_newly_active_error"] = baseline_window["baseline_frame_error"]
        else:
            newly_active, newly_active_total = _newly_active_threads(
                con,
                names,
                graphics_process_ids,
                window,
                baseline_window,
                max_rows,
                on_cpu_ms_by_global_tid,
            )
            payload["baseline_frame"] = baseline_window["baseline_frame"]
            payload["threads_newly_active_baseline_strategy"] = baseline_window[
                "baseline_strategy"
            ]
            payload["threads_newly_active"] = newly_active
            payload["threads_newly_active_returned_count"] = len(newly_active)
            payload["threads_newly_active_total_count"] = newly_active_total
            payload["threads_newly_active_truncated"] = len(newly_active) < newly_active_total
        if idle_threads:
            idle = max(idle_threads, key=lambda thread: thread["on_cpu_ms_upper_bound"])
            payload["os_idle_context"] = {
                "on_cpu_ms_upper_bound": idle["on_cpu_ms_upper_bound"],
                "pct_of_frame": idle["on_cpu_pct_of_frame"],
                "note": (
                    "OS idle-thread time is starvation context, not a producer or "
                    "critical-path candidate."
                ),
            }
        payload["note"] += (
            " threads_newly_active compares graphics-process scheduling activity with "
            "a representative local frame for an isolated hitch, or a session-median frame "
            "when the target belongs to a sustained slow episode, and ranks the result by "
            "on-CPU time so the leading rows are the workers worth sampling with "
            "callstack_summary. It is entity-attribution evidence and "
            "must be paired with a duration-bearing magnitude. The OS idle thread is "
            "reported separately as os_idle_context."
        )
    return payload
#TODO: This is a temporary constraint - we should be able to run the analysis on multiple reports. See DTSP-22718.
def _single_report_note(fact_type: str) -> dict[str, Any]:
    return {
        "ok": True,
        "fact_type": fact_type,
        "scope": "multi_report_unsupported",
        "threads_by_blocked_ms_upper_bound_returned_count": 0,
        "threads_by_on_cpu_ms_upper_bound_returned_count": 0,
        "note": "Thread-scheduling analysis applies to a single report; load one .nsys-rep, not a directory.",
    }


def _no_sched_note(fact_type: str) -> dict[str, Any]:
    return {
        "ok": True,
        "fact_type": fact_type,
        "scope": "none",
        "threads_by_blocked_ms_upper_bound_returned_count": 0,
        "threads_by_on_cpu_ms_upper_bound_returned_count": 0,
        "note": (
            "No usable SCHED_EVENTS (need start/isSchedIn/globalTid); recapture with CPU "
            "context-switch / scheduling tracing to get per-thread on-CPU and blocked-time bounds. "
            "The OSRT_API blocking-wait calls separately need --trace osrt."
        ),
    }
