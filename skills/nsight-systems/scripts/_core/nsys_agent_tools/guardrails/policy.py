"""Reviewed lexical signals for response guardrails.

This module intentionally contains only data-like regexes and word sets. The
execution logic lives in sibling check modules and ``guardrails.__init__``. Keeping the
signals here makes reviewer-owned policy changes visible and avoids burying new
natural-language patterns inside control flow.
"""

from __future__ import annotations

import re

from ..tool_registry import tool_names_in_guardrail_group

_FLAG_RE = re.compile(r"(?<![\w-])--([a-z][a-z0-9-]*)")
_RECIPE_INVOCATION_RE = re.compile(r"\bnsys\s+recipe\s+([a-z][a-z0-9_]{2,60})\b")
_ENV_VAR_RE = re.compile(
    r"\$\{?([A-Z][A-Z0-9_]{2,60})\}?"
    r"|\bexport\s+([A-Z][A-Z0-9_]{2,60})\b"
    r"|\b([A-Z][A-Z0-9_]{2,60})="
)
_UNIT_NUMBER_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*"
    r"(kernels?|launches?|calls?|events?|rows?|samples?|threads?|processes?|files?|%|percent|ns|us|ms|s)\b",
    re.IGNORECASE,
)
_EXPLICIT_REPORT_WORDS = {
    "my report",
    "loaded report",
    "profiling run",
    "profile run",
    "in this report",
    "in my application",
    "this report",
    "the report",
    "loaded reports",
}
_REPORT_SUBJECT_WORDS = {
    "kernel",
    "kernels",
    "cuda api",
    "cuda runtime",
    "gpu being used",
    "gpu device",
    "nvtx ranges",
    "synchronous memory",
    "memcpy",
    "memory copy",
    "metric samples",
    "gpu metrics",
}
_REPORT_RANK_OR_MEASURE_WORDS = {
    "longest",
    "slowest",
    "highest",
    "most frequently",
    "unique kernels",
    "total time",
    "duration",
    "count",
    "how many",
}
_DOCS_EXPLANATION_RE = re.compile(
    r"\b(?:what\s+(?:is|are|does)|how\s+(?:do|does|can)|where\s+is|describe|"
    r"explain|used\s+for|documentation|document|after\s+opening)\b",
    re.IGNORECASE,
)
_TROUBLESHOOTING_WORDS = {
    "empty report",
    "blank report",
    "no data",
    "missing data",
    "didn't capture",
    "did not capture",
    "not capture",
    "what should i check",
    "why is my report",
    "after profiling",
    "troubleshoot",
    "troubleshooting",
}
_NO_REPORT_BOUNDARY_PHRASES = (
    "no report",
    "report is required",
    "report required",
    "provide a report",
    "load a report",
    "loaded report is required",
    "can't determine",
    "cannot determine",
    "need a loaded",
)
_CLI_WORDS = {"nsys ", "--", "cli", "command", "flag", "option"}
_RECIPE_WORDS = {"recipe", "recipes", "nsys recipe"}
# These broad question words are safe only after explicit-report and
# execution/result-output wording has been excluded by the guardrail logic.
_RECIPE_REFERENCE_QUESTION_WORDS = (
    "which",
    "what",
    "how",
    "explain",
    "help",
    "list",
    "name",
)
_RECIPE_EXECUTION_VERB_RE = re.compile(r"\b(?:run|execute)\b")
_RECIPE_RESULT_OR_OUTPUT_WORDS = (
    "result",
    "results",
    "output",
    "generated",
)
_REPORT_TOOLS = tool_names_in_guardrail_group("report_evidence")
_MEASURED_REPORT_TOOLS = tool_names_in_guardrail_group("measured_report_evidence")
_CLI_TOOLS = tool_names_in_guardrail_group("cli_evidence")
_RECIPE_TOOLS = tool_names_in_guardrail_group("recipe_evidence")
_RECIPE_EXECUTION_TOOLS = tool_names_in_guardrail_group("recipe_execution_evidence")
_RECIPE_OUTPUT_WORDS = {
    "output file",
    "output files",
    "generated file",
    "generated files",
    "what files",
    "which files",
    "file columns",
    "what columns",
    "which columns",
    "output columns",
    "schema",
}
