"""Present-source resolution and shared graphics-frame window helpers."""

from __future__ import annotations

from typing import Any

from .evidence import add_key
from .schema import (
    TABLE_DXGI_API,
    TABLE_ETW_EVENTS,
    TABLE_GENERIC_EVENT_TYPES,
    TABLE_OPENGL_API,
    TABLE_STRING_IDS,
    TABLE_VULKAN_API,
)
from .sql_utils import _existing_columns, _query_dicts, _scalar, _sql_string

# DxgKrnl Present kernel event; ids 42/43/184 exist, 184 avoids duplicates.
DXGKRNL_PRESENT_EVENT_ID = 184
# Truncation drops later presents, so present-source queries must ORDER BY time.
_MAX_PRESENT_CALLS = 200_000
_ANALYSIS_DETAILS = "ANALYSIS_DETAILS"
FRAME_ERROR_KEY = "requested_frame_error"

# (api table, preferred present-call names, StringIds LIKE pattern, source label)
_GRAPHICS_SOURCES = (
    (TABLE_DXGI_API, ("IDXGISwapChain::Present", "IDXGISwapChain1::Present1"), "%Present%", "dxgi_api"),
    (TABLE_VULKAN_API, ("vkQueuePresentKHR",), "%QueuePresent%", "vulkan_api"),
    (TABLE_OPENGL_API, ("wglSwapBuffers", "eglSwapBuffers", "glXSwapBuffers", "SwapBuffers"), "%SwapBuffers%", "opengl_api"),
)


def _present_thread_projection(con: Any, table: str, alias: str) -> str:
    """Presenting-thread column when the present source exposes it, else a NULL placeholder."""

    if "globalTid" in _existing_columns(con, table):
        return f"{alias}.globalTid AS global_tid"
    return "NULL AS global_tid"


def _detect_present_call(con: Any, table: str, preferred: tuple[str, ...], like: str) -> str | None:
    rows = _query_dicts(
        con,
        f'SELECT s.value AS name, COUNT(*) AS cnt FROM "{table}" d '
        f'JOIN "{TABLE_STRING_IDS}" s ON d.nameId = s.id '
        f"WHERE s.value LIKE {_sql_string(like)} GROUP BY s.value ORDER BY cnt DESC",
        max_rows=20,
    )
    counts = {str(row["name"]): int(row["cnt"]) for row in rows if row.get("name")}
    if not counts:
        return None
    return next((call for call in preferred if call in counts), max(counts, key=counts.__getitem__))


def _etw_present_source(con: Any, tables: set[str]) -> tuple[str | None, str | None, str | None]:
    """DxgKrnl ETW Present source, used when no graphics API present rows exist."""

    if not {TABLE_ETW_EVENTS, TABLE_GENERIC_EVENT_TYPES} <= tables:
        return None, None, None
    etw_filter = (
        f'FROM "{TABLE_ETW_EVENTS}" e JOIN "{TABLE_GENERIC_EVENT_TYPES}" t ON e.typeId = t.typeId '
        f"WHERE t.etwEventId = {DXGKRNL_PRESENT_EVENT_ID} AND e.opcode = 0"
    )
    if (_scalar(con, f"SELECT COUNT(*) {etw_filter}") or 0) <= 0:
        return None, None, None
    return (
        "etw_dxgkrnl_present",
        f"SELECT e.timestamp AS present_timestamp_ns, "
        f"{_present_thread_projection(con, TABLE_ETW_EVENTS, 'e')} {etw_filter} "
        f"ORDER BY e.timestamp",
        f"DxgKrnl Present (etwEventId={DXGKRNL_PRESENT_EVENT_ID}, opcode 0)",
    )


def _resolve_source(con: Any, tables: set[str]) -> tuple[str | None, str | None, str | None]:
    # present_timestamp_ns is the frame-edge time: API present ``end``, or the ETW
    # DxgKrnl Present point timestamp when no API rows exist.
    for table, preferred, like, label in _GRAPHICS_SOURCES:
        if table in tables:
            call = _detect_present_call(con, table, preferred, like)
            if call:
                query = (
                    f'SELECT d."end" AS present_timestamp_ns, '
                    f"{_present_thread_projection(con, table, 'd')} "
                    f'FROM "{table}" d '
                    f'JOIN "{TABLE_STRING_IDS}" s ON d.nameId = s.id '
                    f"WHERE s.value = {_sql_string(call)} "
                    f'ORDER BY d."end"'
                )
                return label, query, call
    return _etw_present_source(con, tables)


def _analysis_window_end(con: Any) -> int | None:
    """Valid analysis-window end (ns); excludes warm-up and sentinel timestamps."""

    for sql in (
        f'SELECT duration FROM "{_ANALYSIS_DETAILS}" LIMIT 1',
        f'SELECT TraceDurationInNs FROM "{_ANALYSIS_DETAILS}" LIMIT 1',
        f"SELECT CAST(value AS BIGINT) FROM \"{_ANALYSIS_DETAILS}\" WHERE key = 'TraceDurationInNs' LIMIT 1",
    ):
        value = _scalar(con, sql)
        if value is not None:
            return int(value)
    return None


def _frame_present_timestamps(
    con: Any, tables: set[str]
) -> tuple[str | None, str | None, list[int], dict[int, int]]:
    """Resolve the present source and return sorted, in-window present timestamps and their thread."""

    source, query, present_call = _resolve_source(con, tables)
    if query is None:
        return None, None, [], {}
    window_end = _analysis_window_end(con)
    present_timestamps: list[int] = []
    present_thread_by_timestamp: dict[int, int] = {}
    for row in _query_dicts(con, query, max_rows=_MAX_PRESENT_CALLS, suppress_errors=False):
        if row.get("present_timestamp_ns") is None:
            continue
        present_timestamp_ns = int(row["present_timestamp_ns"])
        if present_timestamp_ns < 0 or (
            window_end is not None and present_timestamp_ns > window_end
        ):
            continue
        present_timestamps.append(present_timestamp_ns)
        if row.get("global_tid") is not None:
            present_thread_by_timestamp.setdefault(
                present_timestamp_ns, int(row["global_tid"])
            )
    present_timestamps.sort()
    return source, present_call, present_timestamps, present_thread_by_timestamp


def _is_frame_in_range(frame: int, total_frames: int) -> bool:
    """True when frame is a valid 1-based frame number."""

    return 1 <= frame <= total_frames


def _frame_index_error(frame: int, total_frames: int) -> dict[str, Any]:
    return {FRAME_ERROR_KEY: f"frame {frame} is out of range (1..{total_frames})."}


def _no_frames_error() -> dict[str, Any]:
    return {FRAME_ERROR_KEY: "Need at least two graphics present calls to derive a frame window."}


def _neighbor_frames(frame: int, total_frames: int, *, radius: int) -> range:
    """The frames up to ``radius`` on each side of ``frame``, staying within 1..total_frames."""

    return range(max(1, frame - radius), min(total_frames + 1, frame + radius + 1))


def _frame_fields(frame_num: int, present_timestamps: list[int]) -> dict[str, Any]:
    start_ns, end_ns = present_timestamps[frame_num - 1], present_timestamps[frame_num]
    return {
        "frame_num": frame_num,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "frame_ms": round((end_ns - start_ns) / 1e6, 3),
    }


def _frame_row(frame_num: int, present_timestamps: list[int]) -> dict[str, Any]:
    return add_key(_frame_fields(frame_num, present_timestamps), "graphics-frame", frame_num)


def _resolve_frame_window(con: Any, tables: set[str], frame: int) -> dict[str, Any]:
    """Frame N window [start_ns, end_ns) from the present source, or an error dict."""

    _, _, present_timestamps, present_thread_by_timestamp = _frame_present_timestamps(
        con, tables
    )
    if len(present_timestamps) < 2:
        return _no_frames_error()
    total_frames = len(present_timestamps) - 1
    if not _is_frame_in_range(frame, total_frames):
        return _frame_index_error(frame, total_frames)
    fields = _frame_fields(frame, present_timestamps)
    # The present at end_ns closes this frame, so its thread is the frame-boundary
    # thread even when the frame source is ETW rather than an API table.
    present_global_tid = present_thread_by_timestamp.get(fields["end_ns"])
    if present_global_tid is not None:
        fields["present_global_tid"] = present_global_tid
    return fields
