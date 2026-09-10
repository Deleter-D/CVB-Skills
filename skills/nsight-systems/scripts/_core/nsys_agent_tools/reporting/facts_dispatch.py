"""Dispatch model-selected report fact types to deterministic fact helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .connection import connect_session
from .errors import _safe_error_text
from .evidence import report_evidence
from .facts_callstack import _callstack_summary
from .facts_concurrency import _thread_concurrency_summary
from .facts_cuda_activity import _activity_summary
from .facts_cuda_kernel import _kernel_summary, _kernel_variance
from .facts_cuda_memory import _memory_summary
from .facts_cuda_ncu import _nsight_compute_handoff_candidates
from .facts_cuda_runtime import _runtime_summary
from .facts_cuda_timeline import _timeline_summary
from .facts_gpu import _gpu_device_fact
from .facts_graphics_api import (
    _graphics_api_distribution,
    _graphics_api_summary,
    _graphics_api_timeline,
)
from .facts_graphics_frame import _frame_scan, _frame_summary
from .facts_nccl import _nccl_distribution
from .facts_scheduling import (
    _is_unconfirmed_full_window_blocker,
    _thread_scheduling_summary,
)
from .frame_utils import FRAME_ERROR_KEY
from .load import load_native_report_duckdb
from .multi_report import load_multi_report_duckdb
from .schema import (
    NCCL_TABLE_PATTERN,
    TABLE_COMPOSITE_EVENTS,
    TABLE_CUDA_GRAPH_TRACE,
    TABLE_CUDA_KERNEL,
    TABLE_CUDA_MEMCPY,
    TABLE_CUDA_MEMSET,
    TABLE_CUDA_RUNTIME,
    TABLE_DX12_API,
    TABLE_DX12_WORKLOAD,
    TABLE_DXGI_API,
    TABLE_ENUM_CUDA_MEM_KIND,
    TABLE_ENUM_CUDA_MEMCPY_OPER,
    TABLE_ENUM_SCHEDULING_THREAD_BLOCK,
    TABLE_ETW_EVENTS,
    TABLE_GENERIC_EVENT_TYPES,
    TABLE_GPU_CONTEXT_SWITCH,
    TABLE_GPU_METRICS,
    TABLE_META_DATA_EXPORT,
    TABLE_MPI_COLLECTIVES,
    TABLE_NVTX_EVENTS,
    TABLE_OPENGL_API,
    TABLE_OPENGL_WORKLOAD,
    TABLE_OSRT_API,
    TABLE_SAMPLING_CALLCHAINS,
    TABLE_SCHED_EVENTS,
    TABLE_STRING_IDS,
    TABLE_TARGET_INFO_CUDA_DEVICE,
    TABLE_TARGET_INFO_GPU,
    TABLE_TARGET_INFO_GPU_METRICS,
    TABLE_TARGET_INFO_SYSTEM_ENV,
    TABLE_THREAD_NAMES,
    TABLE_VULKAN_API,
    TABLE_VULKAN_WORKLOAD,
    TABLE_WDDM_DMA_PACKET_START,
    TABLE_WDDM_EVICT_ALLOCATION,
    TABLE_WDDM_PAGING_QUEUE_PACKET_INFO,
)
from .sql_utils import _install_query_timeout, _query_dicts
from .table_shape import attach_table_view
from .types import ReportSession

if TYPE_CHECKING:
    from .runtime import ReportRuntime

# Caps how many ranked rows a handler returns (kernels, API calls, threads, hotspots, and so on).
# Values above the max are clamped before dispatch.
_FACT_MAX_ROWS = 50


type FactHandler = (
    Callable[[Any, set[str], ReportSession, bool, str, int, str], dict[str, Any]]
    | Callable[
        [Any, set[str], ReportSession, bool, str, int, str, int | None],
        dict[str, Any],
    ]
)

# Group of Regex patterns (case-insensitive re.fullmatch).
# Group is satisfied if each pattern in it matches with at least one schema table name
# Example: (".*NCCL.*", TABLE_STRING_IDS) needs an NCCL table and STRING_IDS.
type TableNamePatterns = tuple[str, ...]

# Multiple groups of TableNamePatterns. Used to specify multiple requirements
# Example: ((TABLE_DX12_API, TABLE_STRING_IDS), (".*_API",))
type TableNameGroups = tuple[TableNamePatterns, ...]


@dataclass(frozen=True)
class FactSpec:
    """Declarative mapping from fact type aliases to deterministic handlers."""

    fact_type_aliases: tuple[str, ...]
    handler: FactHandler
    canonical_fact_type: str
    guidance: str = ""
    export_tables: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    # Use to specify all the tables that must be available to use this fact.
    # Can use direct names or patterns to match with available tables. () means no constraint.
    # Example: ((TABLE_CUDA_KERNEL,), (TABLE_CUDA_MEMSET,)) — needs CUDA_KERNEL AND CUDA_MEMSET.
    required_tables_all: TableNameGroups = ()
    # Use to specify tables of which at least one must be available to use this fact.
    # Can use direct names or patterns to match with available tables. () means no constraint.
    # Example: ((TABLE_CUDA_MEMCPY,), (TABLE_CUDA_MEMSET,)) — needs CUDA_MEMCPY OR CUDA_MEMSET.
    required_tables_any: TableNameGroups = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(
            fact_type
            for fact_type in self.fact_type_aliases
            if fact_type != self.canonical_fact_type
        )

    @staticmethod
    def _pattern_matches_any_table(pattern: str, tables: set[str]) -> bool:
        return any(re.fullmatch(pattern, table, flags=re.IGNORECASE) for table in tables)

    def _all_patterns_match_tables(self, patterns: TableNamePatterns, tables: set[str]) -> bool:
        return all(self._pattern_matches_any_table(pattern, tables) for pattern in patterns)

    def _table_groups_satisfied(
        self,
        table_groups: TableNameGroups,
        tables: set[str],
        *,
        require_all_groups: bool,
    ) -> bool:
        if not table_groups:
            return True
        matches = (self._all_patterns_match_tables(group, tables) for group in table_groups)
        return all(matches) if require_all_groups else any(matches)

    def _missing_patterns_by_group(
        self,
        table_groups: TableNameGroups,
        tables: set[str],
    ) -> list[list[str]]:
        return [
            [pattern for pattern in group if not self._pattern_matches_any_table(pattern, tables)]
            for group in table_groups
        ]

    def table_requirements_satisfied(self, tables: set[str]) -> bool:
        return (
            self._table_groups_satisfied(self.required_tables_all, tables, require_all_groups=True)
            and self._table_groups_satisfied(self.required_tables_any, tables, require_all_groups=False)
        )

    def availability_requirements(
        self,
        tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return declarative table requirements, optionally with confirmed gaps."""

        requirements: list[dict[str, Any]] = []
        for table_groups, kind, require_all_groups in (
            (self.required_tables_all, "all_complete_groups", True),
            (self.required_tables_any, "any_complete_group", False),
        ):
            if not table_groups:
                continue
            if tables is not None and self._table_groups_satisfied(
                table_groups,
                tables,
                require_all_groups=require_all_groups,
            ):
                continue
            requirement: dict[str, Any] = {
                "kind": kind,
                "table_groups": [list(group) for group in table_groups],
            }
            if tables is not None:
                requirement["missing_by_group"] = self._missing_patterns_by_group(table_groups, tables)
            requirements.append(requirement)
        return requirements


_GRAPHICS_API_EXPORT_TABLES = (
    TABLE_DX12_API,
    TABLE_DX12_WORKLOAD,
    TABLE_VULKAN_API,
    TABLE_VULKAN_WORKLOAD,
    TABLE_OPENGL_API,
    TABLE_OPENGL_WORKLOAD,
    TABLE_DXGI_API,
    TABLE_STRING_IDS,
)


def _gpu_devices_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del metric, fact_type
    return _gpu_device_fact(
        con,
        tables,
        max_rows,
        multi_report=multi_report,
        report_count=session.report_count or len(session.multi_reports) or 1,
        display_label=session.display_label,
    )


def _activity_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, metric, fact_type
    return _activity_summary(con, tables, max_rows)


def _kernel_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, fact_type
    return _kernel_summary(con, tables, metric or "max_single_duration", max_rows)


def _runtime_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, fact_type
    return _runtime_summary(con, tables, metric, max_rows)


def _nvtx_presence_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, metric, max_rows
    if TABLE_NVTX_EVENTS not in tables:
        return {
            "ok": True,
            "fact_type": fact_type,
            "event_count": 0,
            "note": f"{TABLE_NVTX_EVENTS} is not present in this report.",
        }
    rows = _query_dicts(
        con,
        f'SELECT COUNT(*) AS event_count FROM "{TABLE_NVTX_EVENTS}"',
        max_rows=1,
    )
    return {"ok": True, "fact_type": fact_type, **(rows[0] if rows else {"event_count": 0})}


def _memory_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, fact_type
    return _memory_summary(con, tables, max_rows, metric)


def _timeline_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, metric, max_rows
    return _timeline_summary(con, tables, fact_type=fact_type, multi_report=multi_report)


def _kernel_variance_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, metric, max_rows, fact_type
    return _kernel_variance(con, tables)


def _nccl_distribution_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, metric, max_rows, fact_type
    return _nccl_distribution(con, tables, multi_report=multi_report)


def _ncu_handoff_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session, multi_report, metric, fact_type
    return _nsight_compute_handoff_candidates(con, tables, max_rows)


def _frame_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session, metric
    return _frame_summary(
        con, tables, multi_report=multi_report, frame=frame, max_rows=max_rows, fact_type=fact_type
    )


def _frame_scan_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session

    # The metric argument selects which frame to compare with; see the fact spec options.
    return _frame_scan(
        con,
        tables,
        multi_report=multi_report,
        frame=frame,
        baseline_strategy=metric or "neighborhood_median",
        max_rows=max_rows,
        fact_type=fact_type,
    )


def _graphics_api_summary_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session, metric
    return _graphics_api_summary(
        con, tables, multi_report=multi_report, frame=frame, max_rows=max_rows, fact_type=fact_type
    )


def _graphics_api_distribution_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session
    return _graphics_api_distribution(
        con, tables, multi_report=multi_report, metric=metric, max_rows=max_rows, fact_type=fact_type
    )


def _graphics_api_timeline_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
) -> dict[str, Any]:
    del session
    return _graphics_api_timeline(
        con, tables, multi_report=multi_report, metric=metric, max_rows=max_rows, fact_type=fact_type
    )


def _thread_scheduling_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session, metric
    return _thread_scheduling_summary(
        con, tables, multi_report=multi_report, frame=frame, max_rows=max_rows, fact_type=fact_type
    )


def _thread_concurrency_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session, metric, max_rows
    return _thread_concurrency_summary(
        con,
        tables,
        multi_report=multi_report,
        frame=frame,
        fact_type=fact_type,
    )


def _callstack_handler(
    con: Any,
    tables: set[str],
    session: ReportSession,
    multi_report: bool,
    metric: str,
    max_rows: int,
    fact_type: str,
    frame: int | None = None,
) -> dict[str, Any]:
    del session
    return _callstack_summary(
        con,
        tables,
        multi_report=multi_report,
        metric=metric,
        frame=frame,
        max_rows=max_rows,
        fact_type=fact_type,
    )


FACT_SPECS: tuple[FactSpec, ...] = (
    FactSpec(
        ("gpu_devices",),
        _gpu_devices_handler,
        "gpu_devices",
        required_tables_all=((TABLE_TARGET_INFO_GPU,),),
        guidance="Use gpu_devices or activity_summary to separate visible GPUs from GPUs with activity.",
        export_tables=(
            TABLE_TARGET_INFO_GPU,
            TABLE_TARGET_INFO_CUDA_DEVICE,
            TABLE_CUDA_KERNEL,
            TABLE_STRING_IDS,
        ),
    ),
    FactSpec(
        ("activity_summary", "report_activity", "per_report_activity"),
        _activity_handler,
        "activity_summary",
        guidance="Use activity_summary for per-report category coverage and active GPU attribution.",
        export_tables=(
            TABLE_CUDA_KERNEL,
            TABLE_CUDA_RUNTIME,
            TABLE_NVTX_EVENTS,
            TABLE_TARGET_INFO_GPU,
            TABLE_TARGET_INFO_CUDA_DEVICE,
            TABLE_STRING_IDS,
            TABLE_MPI_COLLECTIVES,
        ),
        required_tables_any=(
            (TABLE_CUDA_KERNEL,),
            (TABLE_CUDA_RUNTIME,),
            (TABLE_NVTX_EVENTS,),
            (TABLE_TARGET_INFO_GPU,),
            (TABLE_MPI_COLLECTIVES,),
        ),
    ),
    FactSpec(
        ("kernel_summary",),
        _kernel_summary_handler,
        "kernel_summary",
        required_tables_all=((TABLE_CUDA_KERNEL,),),
        guidance=(
            "Use kernel_summary for launch counts, unique kernel names, total time, mean time, "
            "and longest single launch; use --metric max_single_duration for the longest launch."
        ),
        export_tables=(TABLE_CUDA_KERNEL, TABLE_STRING_IDS),
        options=(("metric", "optional; use max_single_duration for the longest launch"),),
    ),
    FactSpec(
        ("cuda_api_summary",),
        _runtime_summary_handler,
        "cuda_api_summary",
        required_tables_all=((TABLE_CUDA_RUNTIME,),),
        guidance="Use an unqualified cuda_api_summary for ambiguous API timing so total, mean, and longest-event interpretations stay visible.",
        export_tables=(TABLE_CUDA_RUNTIME, TABLE_STRING_IDS),
        options=(("metric", "optional; omit to preserve total, mean, and maximum interpretations"),),
    ),
    FactSpec(
        ("nvtx_presence",),
        _nvtx_presence_handler,
        "nvtx_presence",
        guidance="Use nvtx_presence for the exact NVTX_EVENTS row count.",
        export_tables=(TABLE_NVTX_EVENTS, TABLE_META_DATA_EXPORT),
    ),
    FactSpec(
        ("memcpy_summary",),
        _memory_summary_handler,
        "memcpy_summary",
        guidance="Use memcpy_summary with metric total_bytes for largest memory operation by byte volume.",
        export_tables=(
            TABLE_CUDA_MEMCPY,
            TABLE_CUDA_MEMSET,
            TABLE_ENUM_CUDA_MEMCPY_OPER,
            TABLE_ENUM_CUDA_MEM_KIND,
        ),
        options=(("metric", "optional; use total_bytes for byte-volume ranking"),),
        required_tables_any=((TABLE_CUDA_MEMCPY,), (TABLE_CUDA_MEMSET,)),
    ),
    FactSpec(
        ("timeline_summary", "gpu_timeline", "idle_gaps", "gpu_utilization"),
        _timeline_summary_handler,
        "timeline_summary",
        required_tables_all=((TABLE_CUDA_KERNEL,),),
        guidance="Use timeline_summary for bounded kernel timeline coverage and idle-gap facts.",
        export_tables=(
            TABLE_CUDA_KERNEL,
            TABLE_GPU_METRICS,
            TABLE_TARGET_INFO_GPU_METRICS,
            TABLE_CUDA_GRAPH_TRACE,
        ),
    ),
    FactSpec(
        ("frame_summary", "frame_window", "graphics_frame_summary"),
        _frame_summary_handler,
        "frame_summary",
        guidance=(
            "Use frame_summary to locate a graphics frame's time window and per-frame CPU "
            "frame time (DXGI/Vulkan/OpenGL present rows, else DxgKrnl ETW Present); "
            "its cadence fields flag significant long-short oscillation and even-odd timing "
            "differences. Stutter is one use, but per-frame timing answers any frame-time question."
        ),
        export_tables=(
            TABLE_DXGI_API,
            TABLE_VULKAN_API,
            TABLE_OPENGL_API,
            TABLE_ETW_EVENTS,
            TABLE_GENERIC_EVENT_TYPES,
            TABLE_STRING_IDS,
        ),
        options=(("frame", "optional frame index"),),
        required_tables_any=(
            (TABLE_DXGI_API, TABLE_STRING_IDS),
            (TABLE_VULKAN_API, TABLE_STRING_IDS),
            (TABLE_OPENGL_API, TABLE_STRING_IDS),
            (TABLE_ETW_EVENTS, TABLE_GENERIC_EVENT_TYPES),
        ),
    ),
    FactSpec(
        ("frame_scan", "graphics_frame_scan"),
        _frame_scan_handler,
        "frame_scan",
        guidance=(
            "Use frame_scan --frame <N> to scan a frame's window for ETW and WDDM event evidence "
            "against a baseline frame; it returns event counts (not a cause verdict)."
        ),
        export_tables=(
            TABLE_DXGI_API,
            TABLE_VULKAN_API,
            TABLE_OPENGL_API,
            TABLE_ETW_EVENTS,
            TABLE_GENERIC_EVENT_TYPES,
            TABLE_STRING_IDS,
            TABLE_WDDM_EVICT_ALLOCATION,
            TABLE_WDDM_PAGING_QUEUE_PACKET_INFO,
            TABLE_WDDM_DMA_PACKET_START,
            TABLE_GPU_CONTEXT_SWITCH,
        ),
        options=(
            ("frame", "required frame index from frame_summary"),
            (
                "metric",
                "optional baseline frame selection strategy: neighborhood_median (=default); "
                "session_median; previous; next",
            ),
        ),
        required_tables_any=((TABLE_ETW_EVENTS, TABLE_GENERIC_EVENT_TYPES, TABLE_STRING_IDS),),
    ),
    FactSpec(
        ("graphics_api_summary", "gfx_api_summary"),
        _graphics_api_summary_handler,
        "graphics_api_summary",
        guidance=(
            "Use graphics_api_summary for per-API DX12/Vulkan/OpenGL call timing (count, total/avg/"
            "max ms) ranked by total time; add --frame <N> to scope to a frame window (no nanosecond "
            "pasting) with each API's pct_of_frame, plus the paired GPU workload table."
        ),
        export_tables=_GRAPHICS_API_EXPORT_TABLES,
        options=(("frame", "optional frame index"),),
        required_tables_any=(
            (TABLE_DX12_API, TABLE_STRING_IDS),
            (TABLE_VULKAN_API, TABLE_STRING_IDS),
            (TABLE_OPENGL_API, TABLE_STRING_IDS),
        ),
    ),
    FactSpec(
        ("graphics_api_distribution", "gfx_api_distribution"),
        _graphics_api_distribution_handler,
        "graphics_api_distribution",
        guidance=(
            "Use graphics_api_distribution for session-wide per-API call-duration percentiles "
            "(min/median/p95/p99/max) to separate steady-state cost from the tail; --metric "
            "<api_name> narrows to one API."
        ),
        export_tables=_GRAPHICS_API_EXPORT_TABLES,
        options=(("metric", "optional graphics API name"),),
        required_tables_any=(
            (TABLE_DX12_API, TABLE_STRING_IDS),
            (TABLE_VULKAN_API, TABLE_STRING_IDS),
            (TABLE_OPENGL_API, TABLE_STRING_IDS),
        ),
    ),
    FactSpec(
        ("graphics_api_timeline", "gfx_api_timeline"),
        _graphics_api_timeline_handler,
        "graphics_api_timeline",
        guidance=(
            "Use graphics_api_timeline --metric <api_name> to detect back-to-back serialized "
            "chaining of one API's calls (the in-order command-queue pattern behind tiled-resource "
            "stutter)."
        ),
        export_tables=_GRAPHICS_API_EXPORT_TABLES,
        options=(("metric", "required graphics API name"),),
        required_tables_any=(
            (TABLE_DX12_API, TABLE_STRING_IDS),
            (TABLE_VULKAN_API, TABLE_STRING_IDS),
            (TABLE_OPENGL_API, TABLE_STRING_IDS),
        ),
    ),
    FactSpec(
        ("thread_scheduling", "blocking_waits", "sched_summary"),
        _thread_scheduling_handler,
        "thread_scheduling",
        required_tables_all=((TABLE_SCHED_EVENTS,),),
        guidance=(
            "Use thread_scheduling for per-thread on-CPU and blocked-time upper bounds, with "
            "`*_confirmed_pct` as confidence in each bound: near 100% means confirmed "
            "sched-in/sched-out pairs, while a low value means missing transitions inflated "
            "the upper bound. It also reports the dominant block reason (the fence-wait / "
            "lock-contention / preemption distinction) and top OSRT_API blocking waits (e.g. "
            "`WaitForSingleObjectEx` on Windows, `pthread_cond_wait` on Linux); add --frame <N> "
            "to scope to one frame window and identify its frame-boundary thread for callstack follow-up."
        ),
        export_tables=(
            TABLE_SCHED_EVENTS,
            TABLE_ENUM_SCHEDULING_THREAD_BLOCK,
            TABLE_OSRT_API,
            TABLE_THREAD_NAMES,
            TABLE_STRING_IDS,
            TABLE_DXGI_API,
            TABLE_VULKAN_API,
            TABLE_OPENGL_API,
            TABLE_ETW_EVENTS,
            TABLE_GENERIC_EVENT_TYPES,
        ),
        options=(("frame", "optional frame index"),),
    ),
    FactSpec(
        ("thread_concurrency", "cpu_concurrency", "cpu_utilization", "core_occupancy"),
        _thread_concurrency_handler,
        "thread_concurrency",
        required_tables_all=((TABLE_SCHED_EVENTS,),),
        guidance=(
            "Use thread_concurrency to summarize confirmed on-CPU fan-out and total busy core-ms"
            "Optionally comparing the summary to the respective values of reference session time range"
            "such as a clean baseline frame. This fact reports measurements, not a saturation verdict."
        ),
        export_tables=(
            TABLE_SCHED_EVENTS,
            TABLE_TARGET_INFO_SYSTEM_ENV,
            TABLE_DXGI_API,
            TABLE_VULKAN_API,
            TABLE_OPENGL_API,
            TABLE_ETW_EVENTS,
            TABLE_GENERIC_EVENT_TYPES,
            TABLE_STRING_IDS,
        ),
        options=(("frame", "optional frame index"),),
    ),
    FactSpec(
        ("callstack_summary", "thread_callstack", "hotspots"),
        _callstack_handler,
        "callstack_summary",
        required_tables_all=((TABLE_SAMPLING_CALLCHAINS,),),
        guidance=(
            "Use callstack_summary for leaf-symbol hotspots (cpuCycles=1 periodic samples = where "
            "time goes) and blocked_stacks (cpuCycles=0 scheduling event callstacks = why a thread "
            "came off CPU, e.g. a fence/wait call). Pass --metric <globalTid> (from thread_scheduling) "
            "to attribute one thread and --frame <N> to restrict samples to that frame window; "
            "without a metric symbols mix across threads."
        ),
        export_tables=(
            TABLE_SAMPLING_CALLCHAINS,
            TABLE_COMPOSITE_EVENTS,
            TABLE_STRING_IDS,
            TABLE_THREAD_NAMES,
        ),
        options=(
            ("metric", "optional globalTid returned by thread_scheduling"),
            ("frame", "optional frame index"),
        ),
        required_tables_any=(
            (TABLE_SAMPLING_CALLCHAINS, TABLE_COMPOSITE_EVENTS, TABLE_STRING_IDS),
        ),
    ),
    FactSpec(
        ("kernel_variance", "duration_variance"),
        _kernel_variance_handler,
        "kernel_variance",
        required_tables_all=((TABLE_CUDA_KERNEL,),),
        guidance="Use kernel_variance for kernel-duration spread and outlier evidence.",
        export_tables=(TABLE_CUDA_KERNEL,),
    ),
    FactSpec(
        ("nccl_distribution", "nccl_summary"),
        _nccl_distribution_handler,
        "nccl_distribution",
        guidance="Use nccl_distribution to distinguish NCCL event-table evidence from CUDA kernel names that contain nccl.",
        export_tables=(NCCL_TABLE_PATTERN, TABLE_CUDA_KERNEL, TABLE_STRING_IDS),
        required_tables_any=((TABLE_CUDA_KERNEL,), (NCCL_TABLE_PATTERN,)),
    ),
    FactSpec(
        ("nsight_compute_handoff", "nsight_compute_candidates"),
        _ncu_handoff_handler,
        "nsight_compute_handoff",
        required_tables_all=((TABLE_CUDA_KERNEL,),),
        guidance="Use nsight_compute_handoff for candidate kernels for separate Nsight Compute inspection.",
        export_tables=(TABLE_CUDA_KERNEL, TABLE_STRING_IDS),
    ),
)

_FACT_BY_FACT_TYPE = {fact_type: spec for spec in FACT_SPECS for fact_type in spec.fact_type_aliases}

_GRAPHICS_WORKFLOW_SCHEMA = "nsys-graphics-convergence-v1"
_GRAPHICS_WORKFLOW_ID = "graphics_frame_root_cause"
_GRAPHICS_CONVERGENCE_REQUIREMENTS = (
    "magnitude_match",
    "temporal_order",
    "entity_attribution",
)
_GRAPHICS_WORKFLOW_ROLES = {
    "frame_summary": "frame_selection",
    "frame_scan": "corroborating",
    "graphics_api_summary": "magnitude_evidence",
    "thread_scheduling": "thread_mechanism",
    "thread_concurrency": "thread_mechanism",
    "callstack_summary": "entity_attribution",
}
_GRAPHICS_WORKFLOW_REMAINDER = {
    "frame_summary": (
        "graphics_api_summary",
        "thread_scheduling",
        "thread_concurrency",
        "callstack_summary",
    ),
    "frame_scan": (
        "graphics_api_summary",
        "thread_scheduling",
        "thread_concurrency",
        "callstack_summary",
    ),
    "graphics_api_summary": ("thread_scheduling", "thread_concurrency", "callstack_summary"),
    "thread_scheduling": ("thread_concurrency", "callstack_summary"),
    "thread_concurrency": ("callstack_summary",),
    "callstack_summary": (),
}
_CALLSTACK_CANDIDATE_LIMIT = 4


def supported_fact_types() -> tuple[str, ...]:
    """Return stable fact types accepted by report-fact adapters."""

    return ("report_inventory", *sorted(_FACT_BY_FACT_TYPE))


def fact_catalog(tables: set[str] | None = None) -> list[dict[str, Any]]:
    """Return canonical, model-facing fact routes generated from ``FACT_SPECS``."""

    catalog: list[dict[str, Any]] = [
        {
            "fact_type": "report_inventory",
            "aliases": [],
            "options": {},
            "guidance": "Use report_inventory for report labels, tables, and available capabilities.",
        }
    ]
    for spec in FACT_SPECS:
        if tables is not None and not spec.table_requirements_satisfied(tables):
            continue
        catalog.append(
            {
                "fact_type": spec.canonical_fact_type,
                "aliases": list(spec.aliases),
                "options": dict(spec.options),
                "guidance": spec.guidance,
            }
        )
    return catalog


def available_fact_types(tables: set[str]) -> list[str]:
    """Return canonical fact types supported by the loaded report tables."""

    return [entry["fact_type"] for entry in fact_catalog(tables)]


def fact_prompt_guidance() -> str:
    """Return compact model-facing guidance generated from fact specs."""

    return " ".join(spec.guidance for spec in FACT_SPECS if spec.guidance)


def fact(
    runtime: ReportRuntime,
    session: ReportSession,
    *,
    fact_type: str,
    metric: str = "",
    max_rows: int = 10,
    frame: int | None = None,
) -> dict[str, Any]:
    """Return deterministic common report facts."""

    max_rows = max(1, min(int(max_rows or 10), _FACT_MAX_ROWS))
    fact_type = fact_type.strip().lower()
    metric = metric.strip().lower()
    multi_report_input = bool(session.multi_reports)
    evidence_session = session
    if fact_type == "report_inventory":
        payload = {"ok": True, "fact_type": fact_type, **runtime.context(session)}
        payload.setdefault(
            "evidence",
            report_evidence(evidence_session, command="nsys_report_fact", fact_type=fact_type),
        )
        return payload
    spec = _FACT_BY_FACT_TYPE.get(fact_type)
    if spec is None:
        return {"ok": False, "fact_type": fact_type, "metric": metric, "error": "Unsupported report fact type."}
    if session.multi_reports:
        session = load_multi_report_duckdb(runtime, session, table_patterns=spec.export_tables)
        evidence_session = session
    elif session.source == "native_report":
        session = load_native_report_duckdb(
            runtime,
            session.input_path,
            table_patterns=spec.export_tables,
        )
        evidence_session = session
    tables = set(runtime.tables(session))
    missing_required = [
        pattern
        for group in spec.required_tables_all
        for pattern in group
        if not spec._pattern_matches_any_table(pattern, tables)
    ]
    if missing_required:
        missing_label = ", ".join(missing_required)
        verb = "are" if len(missing_required) > 1 else "is"
        return {
            "ok": False,
            "fact_type": fact_type,
            "error": f"{missing_label} {verb} not present in this report.",
        }
    try:
        with connect_session(session) as con:
            _install_query_timeout(con, 10.0)
            handler_args = (
                con,
                tables,
                session,
                multi_report_input or session.report_count > 1,
                metric,
                max_rows,
                fact_type,
            )
            handles_frame = any(name == "frame" for name, _ in spec.options)
            if frame is None or not handles_frame:
                payload = spec.handler(*handler_args)
            else:
                payload = spec.handler(*handler_args, frame=frame)
            return _with_fact_evidence(
                payload,
                evidence_session,
                fact_type=spec.canonical_fact_type,
                requested_frame=frame,
                available_tables=tables,
                inventory_complete=_table_inventory_complete(evidence_session),
            )
    except Exception as exc:  # noqa: BLE001 - return to model for repair/fallback
        return {"ok": False, "fact_type": fact_type, "metric": metric, "error": _safe_error_text(exc)}


def _table_inventory_complete(session: ReportSession) -> bool:
    return not any(bool(event.get("scoped")) for event in session.cache_events)


def _with_fact_evidence(
    payload: dict[str, Any],
    session: ReportSession,
    *,
    fact_type: str,
    requested_frame: int | None = None,
    available_tables: set[str] | None = None,
    inventory_complete: bool = True,
) -> dict[str, Any]:
    """Attach the common report-evidence block without changing failure text."""

    if payload.get("ok"):
        attach_table_view(payload)
        payload.setdefault(
            "evidence",
            report_evidence(
                session,
                command="nsys_report_fact",
                fact_type=fact_type,
            ),
        )
        workflow = _graphics_convergence_metadata(
            payload,
            fact_type=fact_type,
            requested_frame=requested_frame,
            available_tables=available_tables or set(),
            inventory_complete=inventory_complete,
        )
        if workflow is not None:
            payload.setdefault("analysis_workflow", workflow)
        elif capability := _fact_evidence_capability(payload, fact_type=fact_type):
            payload.setdefault("evidence_capability", capability)
    return payload


def _graphics_convergence_metadata(
    payload: dict[str, Any],
    *,
    fact_type: str,
    requested_frame: int | None,
    available_tables: set[str],
    inventory_complete: bool,
) -> dict[str, Any] | None:
    """Describe how one fact contributes to a graphics-frame root-cause chain."""

    role = _GRAPHICS_WORKFLOW_ROLES.get(fact_type)
    if role is None or not _graphics_workflow_active(
        fact_type,
        payload,
        requested_frame=requested_frame,
    ):
        return None
    frame = _workflow_frame(payload, requested_frame)
    followups = [
        _graphics_followup(
            next_fact_type,
            frame=frame,
            payload=payload,
            available_tables=available_tables,
            inventory_complete=inventory_complete,
        )
        for next_fact_type in _GRAPHICS_WORKFLOW_REMAINDER[fact_type]
    ]
    inputs: dict[str, int] = {}
    if frame is not None:
        inputs["frame"] = frame
    return {
        "schema": _GRAPHICS_WORKFLOW_SCHEMA,
        "workflow_id": _GRAPHICS_WORKFLOW_ID,
        "stage": fact_type,
        "evidence_role": role,
        "causal_verdict": "insufficient",
        "inputs": inputs,
        "required_followups": followups,
        "convergence_requirements": list(_GRAPHICS_CONVERGENCE_REQUIREMENTS),
    }


def _graphics_workflow_active(
    fact_type: str,
    payload: dict[str, Any],
    *,
    requested_frame: int | None,
) -> bool:
    if fact_type in {"frame_summary", "frame_scan"}:
        return True
    if fact_type in {"graphics_api_summary", "thread_scheduling", "thread_concurrency"}:
        return requested_frame is not None or isinstance(payload.get("window"), dict)
    return False


def _fact_evidence_capability(
    payload: dict[str, Any],
    *,
    fact_type: str,
) -> dict[str, Any] | None:
    role = _GRAPHICS_WORKFLOW_ROLES.get(fact_type)
    if role is None:
        return None
    capability: dict[str, Any] = {
        "schema": "nsys-fact-evidence-capability-v1",
        "role": role,
        "scope": payload.get("scope", "session"),
        "causal_verdict": "insufficient",
    }
    if fact_type == "callstack_summary" and payload.get("scope") != "frame":
        capability["limitations"] = [
            "does_not_establish_frame_local_temporal_order",
        ]
    return capability


def _workflow_frame(payload: dict[str, Any], requested_frame: int | None) -> int | None:
    if requested_frame is not None and FRAME_ERROR_KEY not in payload:
        return requested_frame
    for key in ("requested_frame", "frame", "window"):
        value = payload.get(key)
        if isinstance(value, dict) and isinstance(value.get("frame_num"), int):
            return int(value["frame_num"])
    return None


def _callstack_candidate_rank(thread: dict[str, Any]) -> tuple[Any, ...]:
    """Order scheduling candidates by how likely their stacks explain the frame.

    Prefer on-CPU share because periodic samples only exist while a thread runs;
    a parked thread yields almost none. Keep the frame-boundary thread first: its
    blocked stacks name the wait even when it barely ran. Demote unconfirmed
    full-window sleepers so they do not consume an empty candidate slot.
    """

    return (
        not _is_unconfirmed_full_window_blocker(thread),
        bool(thread.get("is_frame_boundary_thread")),
        bool(thread.get("is_graphics_process")),
        not str(thread.get("thread_name") or "").startswith("[NSys"),
        float(thread.get("on_cpu_pct_of_frame") or 0.0),
    )


def _callstack_candidate_args(
    payload: dict[str, Any],
    *,
    frame: int | None,
) -> list[dict[str, Any]]:
    """Threads whose callstacks are worth sampling for this frame.

    A wide worker fan-out can carry the frame's CPU work without any single
    worker topping the scheduling rankings, so one slot is reserved for a
    newly-active worker whenever the cohort is not already represented.
    """

    ranked = [
        thread
        for field in (
            "threads_by_on_cpu_ms_upper_bound",
            "threads_by_blocked_ms_upper_bound",
        )
        for thread in payload.get(field, [])
        if isinstance(thread, dict) and isinstance(thread.get("global_tid"), int)
    ]
    # thread_scheduling reports the presenting thread beside the rankings, so take
    # it from there as well: a truncated ranking can omit it entirely.
    boundary_thread = payload.get("frame_boundary_thread")

    if isinstance(boundary_thread, dict) and isinstance(
        boundary_thread.get("global_tid"), int
    ):
        ranked.append(boundary_thread)

    ranked.sort(key=_callstack_candidate_rank, reverse=True)
    # One thread reaches this list from up to three sources, so collapse it to its
    # best-ranked entry before the reserved slot below is counted against it.
    best_ranked_by_global_tid: dict[int, dict[str, Any]] = {}

    for thread in ranked:
        best_ranked_by_global_tid.setdefault(int(thread["global_tid"]), thread)

    ranked = list(best_ranked_by_global_tid.values())
    # thread_scheduling already ranks this cohort by on-CPU time, so its own order
    # names the worker most likely to carry samples.
    cohort = [
        thread
        for thread in payload.get("threads_newly_active", [])
        if isinstance(thread, dict) and isinstance(thread.get("global_tid"), int)
    ]
    cohort_global_tids = {int(thread["global_tid"]) for thread in cohort}
    reserved = _CALLSTACK_CANDIDATE_LIMIT - (1 if cohort else 0)

    candidates: list[dict[str, Any]] = []
    seen_global_tids: set[int] = set()
    for thread in (*ranked[:reserved], *cohort[:1], *ranked):
        global_tid = int(thread["global_tid"])

        if global_tid in seen_global_tids:
            continue

        seen_global_tids.add(global_tid)
        candidates.append(
            {
                "global_tid": global_tid,
                "process_id": thread.get("process_id"),
                "thread_name": thread.get("thread_name"),
                "is_frame_boundary_thread": bool(thread.get("is_frame_boundary_thread")),
                "is_graphics_process": bool(thread.get("is_graphics_process")),
                "dominant_block_reason": thread.get("dominant_block_reason"),
                "selection_reason": (
                    "frame_boundary_thread"
                    if thread.get("is_frame_boundary_thread")
                    else "newly_active_worker"
                    if global_tid in cohort_global_tids
                    else "scheduling_ranking"
                ),
                "args": {
                    "metric": str(global_tid),
                    **({"frame": frame} if frame is not None else {}),
                },
            }
        )

        if len(candidates) >= _CALLSTACK_CANDIDATE_LIMIT:
            break

    return candidates


def _graphics_followup(
    fact_type: str,
    *,
    frame: int | None,
    payload: dict[str, Any],
    available_tables: set[str],
    inventory_complete: bool,
) -> dict[str, Any]:
    followup: dict[str, Any] = {"fact_type": fact_type}
    if fact_type in {"graphics_api_summary", "thread_scheduling", "thread_concurrency"}:
        if frame is not None:
            followup["args"] = {"frame": frame}
        else:
            followup["arg_sources"] = {
                "frame": "frame_summary.slowest_frames[].frame_num"
            }
    elif fact_type == "callstack_summary":
        if frame is not None:
            followup["args"] = {"frame": frame}
        followup["arg_sources"] = {
            "metric": (
                "thread_scheduling.threads_by_on_cpu_ms_upper_bound[].global_tid or "
                "thread_scheduling.threads_by_blocked_ms_upper_bound[].global_tid or "
                "thread_scheduling.threads_newly_active[].global_tid or "
                "thread_scheduling.frame_boundary_thread.global_tid"
            ),
        }
        candidates = _callstack_candidate_args(payload, frame=frame)
        if candidates:
            followup["candidate_args"] = candidates
    availability, requirements = _graphics_followup_availability(
        fact_type,
        available_tables,
        inventory_complete=inventory_complete,
    )
    followup["availability"] = availability
    if availability == "unavailable":
        followup["missing_requirements"] = requirements
        followup["on_unavailable"] = {
            "action": "state_missing_capture_data",
            "allow_ad_hoc_sql_fallback": False,
        }
    elif availability == "unknown":
        followup["availability_requirements"] = requirements
        followup["on_unknown"] = {
            "action": "run_followup_fact_directly",
        }
    return followup


def _graphics_followup_availability(
    fact_type: str,
    available_tables: set[str],
    *,
    inventory_complete: bool,
) -> tuple[str, list[dict[str, Any]]]:
    spec = _FACT_BY_FACT_TYPE[fact_type]
    if spec.table_requirements_satisfied(available_tables):
        return "available", []
    if not inventory_complete:
        return "unknown", spec.availability_requirements()
    return "unavailable", spec.availability_requirements(available_tables)
