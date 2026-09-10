"""CPU callstack facts: what code was a thread running, or stuck in?

This module reports what a thread was actually doing by
counting Nsight's periodic stack samples so callers never write that SQL
themselves. It powers the ``callstack_summary`` report fact (also ``hotspots``),
for all threads or one thread (pass its ``global_tid``):

- ``hotspots``: the functions where the thread spent its running time;
- ``blocked_stacks``: the exact call the thread was sitting in when it went to
  sleep (e.g. a fence or wait).
"""

from __future__ import annotations

import ntpath
from typing import Any

from .facts_cuda_common import _first_existing_column, _key_rows, _string_expr
from .facts_scheduling import _thread_names
from .frame_utils import FRAME_ERROR_KEY, _resolve_frame_window
from .schema import (
    TABLE_COMPOSITE_EVENTS,
    TABLE_SAMPLING_CALLCHAINS,
    TABLE_STRING_IDS,
)
from .sql_utils import _existing_columns, _query_dicts

# When leaf samples are mostly unresolved, hotspot rows are hex addresses that add little value.
# If unresolved_share is at or above _UNRESOLVED_SYMBOL_SHRINK_THRESHOLD, trim hotspots to
# _UNRESOLVED_SYMBOL_ROWS so the payload still shows that shape without crowding out hotspot_modules
_UNRESOLVED_SYMBOL_SHRINK_THRESHOLD = 0.95
_UNRESOLVED_SYMBOL_ROWS = 3


def _module_basename(path: str) -> str:
    # A sanitized path still carries directories inside the wrapper, so unwrap it
    # before taking the basename or the same module aggregates under two names.
    if path.startswith("<local-path:") and path.endswith(">"):
        path = path[len("<local-path:") : -1]
    return ntpath.basename(path) or path


def _event_filter_terms(
    composite_columns: set[str],
    *,
    cpu_cycles: int | None,
    gid: int | None,
    start_ns: int | None,
    end_ns: int | None,
) -> list[str]:
    """Build filters shared by callstack queries over COMPOSITE_EVENTS."""

    terms: list[str] = []
    if cpu_cycles is not None and "cpuCycles" in composite_columns:
        terms.append(f"ce.cpuCycles = {int(cpu_cycles)}")
    if gid is not None and "globalTid" in composite_columns:
        terms.append(f"ce.globalTid = {int(gid)}")
    if start_ns is not None and end_ns is not None:
        terms.append(f"ce.start >= {int(start_ns)} AND ce.start < {int(end_ns)}")
    return terms


def _leaf_symbol_rows(
    con: Any,
    tables: set[str],
    *,
    cpu_cycles: int | None,
    gid: int | None,
    start_ns: int | None,
    end_ns: int | None,
    max_rows: int,
) -> list[dict[str, Any]] | None:
    """Top leaf (depth 0) symbols for a stack type; None if the shape is wrong."""

    sc_columns = _existing_columns(con, TABLE_SAMPLING_CALLCHAINS)
    cc_columns = _existing_columns(con, TABLE_COMPOSITE_EVENTS)
    # Native nsys exports name these `symbol`/`stackDepth`; accept the older
    # `symbolId`/`depth` spelling too so both schema variants resolve.
    symbol_col = _first_existing_column(sc_columns, "symbol", "symbolId")
    depth_col = _first_existing_column(sc_columns, "stackDepth", "depth")
    if symbol_col is None or "id" not in sc_columns or "id" not in cc_columns:
        return None
    name_expr, join = _string_expr(con, TABLE_SAMPLING_CALLCHAINS, "sc", symbol_col, tables)
    terms = _event_filter_terms(
        cc_columns,
        cpu_cycles=cpu_cycles,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    if depth_col is not None:
        terms.append(f"sc.{depth_col} = 0")
    where = (" WHERE " + " AND ".join(terms)) if terms else ""
    # GROUP BY position avoids ambiguity when symbol_col is literally `symbol`
    # (the SELECT alias would otherwise collide with the base column).
    sql = (
        f"SELECT {name_expr} AS symbol, COUNT(*) AS n "
        f'FROM "{TABLE_SAMPLING_CALLCHAINS}" sc '
        f'JOIN "{TABLE_COMPOSITE_EVENTS}" ce ON sc.id = ce.id'
        f"{join}{where} GROUP BY 1 ORDER BY n DESC"
    )
    return _query_dicts(con, sql, max_rows=max(1, max_rows))


def _module_sample_counts(
    con: Any,
    tables: set[str],
    *,
    cpu_cycles: int | None,
    gid: int | None,
    start_ns: int | None,
    end_ns: int | None,
    max_rows: int,
) -> tuple[list[dict[str, Any]], int] | None:
    """Query per-module and total sample counts for the requested scope."""

    sc_columns = _existing_columns(con, TABLE_SAMPLING_CALLCHAINS)
    cc_columns = _existing_columns(con, TABLE_COMPOSITE_EVENTS)
    module_col = _first_existing_column(sc_columns, "module", "moduleId")
    if module_col is None or "id" not in sc_columns or "id" not in cc_columns:
        return None

    name_expr, join = _string_expr(
        con,
        TABLE_SAMPLING_CALLCHAINS,
        "sc",
        module_col,
        tables,
    )
    terms = _event_filter_terms(
        cc_columns,
        cpu_cycles=cpu_cycles,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    where = (" WHERE " + " AND ".join(terms)) if terms else ""
    from_clause = (
        f'FROM "{TABLE_SAMPLING_CALLCHAINS}" sc '
        f'JOIN "{TABLE_COMPOSITE_EVENTS}" ce ON sc.id = ce.id'
    )
    totals = _query_dicts(
        con,
        f"SELECT COUNT(DISTINCT sc.id) AS n {from_clause}{where}",
        max_rows=1,
    )
    rows = _query_dicts(
        con,
        f"SELECT {name_expr} AS module_path, COUNT(DISTINCT sc.id) AS n "
        f"{from_clause}{join}{where} GROUP BY 1 ORDER BY n DESC",
        max_rows=max(100, max_rows),
    )
    if not rows:
        return [], 0

    total_samples = int(totals[0].get("n") or 0) if totals else 0
    return rows, total_samples


def _module_rows(
    con: Any,
    tables: set[str],
    *,
    cpu_cycles: int | None,
    gid: int | None,
    start_ns: int | None,
    end_ns: int | None,
    max_rows: int,
) -> list[dict[str, Any]] | None:
    """Format modules found anywhere in each stack, counting a sample once."""

    counts = _module_sample_counts(
        con,
        tables,
        cpu_cycles=cpu_cycles,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
        max_rows=max_rows,
    )
    if counts is None:
        return None

    rows, total_samples = counts
    paths_by_basename: dict[str, set[str]] = {}
    for row in rows:
        path = str(row.get("module_path") or "<unknown>")
        paths_by_basename.setdefault(_module_basename(path), set()).add(path)

    modules: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        path = str(row.get("module_path") or "<unknown>")
        basename = _module_basename(path)
        samples = int(row.get("n") or 0)
        module = {
            "module": basename,
            "samples": samples,
            "sample_share_pct": (
                round(samples / total_samples * 100, 1) if total_samples > 0 else 0.0
            ),
        }
        if len(paths_by_basename[basename]) > 1:
            module["path"] = path
        modules.append(module)
    return modules


def _symbol_resolution(
    con: Any,
    *,
    cpu_cycles: int | None,
    gid: int | None,
    start_ns: int | None,
    end_ns: int | None,
) -> dict[str, Any] | None:
    """Share of leaf samples whose symbols are unresolved."""

    sc_columns = _existing_columns(con, TABLE_SAMPLING_CALLCHAINS)
    cc_columns = _existing_columns(con, TABLE_COMPOSITE_EVENTS)
    depth_col = _first_existing_column(sc_columns, "stackDepth", "depth")
    if "unresolved" not in sc_columns or "id" not in sc_columns or "id" not in cc_columns:
        return None

    terms = _event_filter_terms(
        cc_columns,
        cpu_cycles=cpu_cycles,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    if depth_col is not None:
        terms.append(f"sc.{depth_col} = 0")
    where = (" WHERE " + " AND ".join(terms)) if terms else ""
    rows = _query_dicts(
        con,
        "SELECT COUNT(DISTINCT sc.id) AS total_samples, "
        "COUNT(DISTINCT CASE WHEN sc.unresolved THEN sc.id END) AS unresolved_samples "
        f'FROM "{TABLE_SAMPLING_CALLCHAINS}" sc '
        f'JOIN "{TABLE_COMPOSITE_EVENTS}" ce ON sc.id = ce.id{where}',
        max_rows=1,
    )
    if not rows:
        return None

    total = int(rows[0].get("total_samples") or 0)
    unresolved = int(rows[0].get("unresolved_samples") or 0)
    return {
        "total_samples": total,
        "unresolved_samples": unresolved,
        "unresolved_pct": round(unresolved / total * 100, 1) if total > 0 else 0.0,
    }


def _callstack_summary(
    con: Any,
    tables: set[str],
    *,
    multi_report: bool,
    metric: str,
    frame: int | None,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    """Leaf-symbol hotspots (cpuCycles=1) and blocked stacks (cpuCycles=0)."""

    if multi_report:
        return _single_report_note(fact_type)
    if TABLE_COMPOSITE_EVENTS not in tables or TABLE_STRING_IDS not in tables:
        return _no_samples_note(fact_type)
    gid = int(metric) if metric.isdigit() else None
    cc_columns = _existing_columns(con, TABLE_COMPOSITE_EVENTS)
    has_cpu_cycles = "cpuCycles" in cc_columns
    window: dict[str, Any] | None = None
    if frame is not None:
        window = _resolve_frame_window(con, tables, frame)
        if FRAME_ERROR_KEY in window:
            return {"ok": True, "fact_type": fact_type, **window}
    frame_applied = window is not None
    start_ns = int(window["start_ns"]) if frame_applied else None
    end_ns = int(window["end_ns"]) if frame_applied else None
    # A gid only narrows results when COMPOSITE_EVENTS actually has globalTid;
    # otherwise the filter is dropped and symbols still mix across threads.
    gid_applied = gid is not None and "globalTid" in cc_columns
    hotspots = _leaf_symbol_rows(
        con,
        tables,
        cpu_cycles=1 if has_cpu_cycles else None,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
        max_rows=max_rows,
    )
    if hotspots is None:
        return _no_samples_note(fact_type)
    hotspot_modules = _module_rows(
        con,
        tables,
        cpu_cycles=1 if has_cpu_cycles else None,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
        max_rows=max_rows,
    )
    symbol_resolution = _symbol_resolution(
        con,
        cpu_cycles=1 if has_cpu_cycles else None,
        gid=gid,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    blocked = (
        _leaf_symbol_rows(
            con,
            tables,
            cpu_cycles=0,
            gid=gid,
            start_ns=start_ns,
            end_ns=end_ns,
            max_rows=max_rows,
        )
        if has_cpu_cycles
        else None
    )
    if gid is not None and not gid_applied:
        target = f"all_threads (requested thread:{gid}, but COMPOSITE_EVENTS has no globalTid column)"
    elif gid is not None:
        target = f"thread:{gid}"
    else:
        target = "all_threads"
    if frame_applied:
        target = f"{target} in frame {frame}"
    elif frame is not None:
        target = (
            f"{target} session-wide (requested frame {frame}, but failed to set time window)"
        )
    scope_note = (
        f" Samples are restricted to frame {frame}'s [start_ns, end_ns) window using "
        f"COMPOSITE_EVENTS.start."
        if frame_applied
        else (
            f" Frame {frame} was requested, but failed to set time window;"
            "results remain session-wide."
            if frame is not None
            else ""
        )
    )
    unresolved_share = (
        symbol_resolution["unresolved_pct"] / 100
        if symbol_resolution and symbol_resolution["total_samples"] > 0
        else 0.0
    )
    hotspot_rows = hotspots
    if unresolved_share >= _UNRESOLVED_SYMBOL_SHRINK_THRESHOLD:
        hotspot_rows = hotspots[:_UNRESOLVED_SYMBOL_ROWS]

    payload: dict[str, Any] = {
        "ok": True,
        "fact_type": fact_type,
        "target": target,
        "scope": "frame" if frame_applied else "session",
        "thread_name": _thread_names(con, tables).get(gid) if gid_applied else None,
        "cpu_cycles_split": has_cpu_cycles,
        "hotspots": _key_rows(
            [{"symbol": row["symbol"], "samples": int(row["n"])} for row in hotspot_rows],
            "callstack-hotspot",
            "symbol",
        ),
        "note": (
            "hotspots are leaf symbols of periodic CPU samples (cpuCycles=1) ranked by sample "
            "count -- time/where-it-runs attribution. blocked_stacks are leaf symbols of scheduling "
            "event callstacks (cpuCycles=0) -- why a thread came off CPU (e.g. a fence/wait call). "
            "Pass --metric <globalTid> (from thread_scheduling) to target one thread; without it "
            f"symbols mix across threads. Counts are evidence, not a verdict.{scope_note}"
        ),
    }
    if hotspot_modules is not None:
        payload["hotspot_modules"] = _key_rows(
            hotspot_modules,
            "callstack-module",
            "module",
        )
        if has_cpu_cycles:
            payload["note"] += (
                " hotspot_modules counts each periodic sample once per module across all stack "
                "depths; sample_share_pct is the share of periodic samples containing the module."
            )
        else:
            payload["note"] += (
                " hotspot_modules counts each sample once per module across all stack depths; "
                "without cpuCycles those samples mix periodic and scheduling stacks, so "
                "sample_share_pct is the share of both stack types containing the module."
            )
    else:
        payload["hotspot_modules_note"] = (
            "SAMPLING_CALLCHAINS has no module column; module attribution is unavailable."
        )
    if symbol_resolution is not None:
        payload["symbol_resolution"] = symbol_resolution

    if unresolved_share >= _UNRESOLVED_SYMBOL_SHRINK_THRESHOLD:
        payload["hotspots_truncated_for_unresolved_symbols"] = len(hotspot_rows) < len(hotspots)
    if frame_applied:
        payload["window"] = window
    if blocked is not None:
        payload["blocked_stacks"] = _key_rows(
            [{"symbol": row["symbol"], "events": int(row["n"])} for row in blocked],
            "callstack-blocked",
            "symbol",
        )
    else:
        payload["blocked_stacks_note"] = (
            "COMPOSITE_EVENTS has no cpuCycles column; cannot separate periodic samples from "
            "scheduling event callstacks, so hotspots may mix both stack types."
        )
    if frame_applied and not hotspots and not blocked:
        payload["limitations"] = ["no_frame_local_samples_for_target"]
        payload["note"] += (
            " This target has no frame-local callstack samples; try another "
            "thread_scheduling candidate before concluding that blocked attribution is unavailable."
        )
    return payload


#TODO: This is a temporary constraint - we should be able to run the analysis on multiple reports. See DTSP-22718.
def _single_report_note(fact_type: str) -> dict[str, Any]:
    return {
        "ok": True,
        "fact_type": fact_type,
        "target": "multi_report_unsupported",
        "note": "Callstack analysis applies to a single report; load one .nsys-rep, not a directory.",
    }


def _no_samples_note(fact_type: str) -> dict[str, Any]:
    return {
        "ok": True,
        "fact_type": fact_type,
        "target": "none",
        "note": (
            "No usable CPU sampling callstacks (need COMPOSITE_EVENTS + SAMPLING_CALLCHAINS + "
            "StringIds); recapture with CPU sampling enabled to attribute thread callstacks."
        ),
    }
