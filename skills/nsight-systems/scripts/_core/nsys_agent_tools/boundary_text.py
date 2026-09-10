"""Shared text for cross-layer Nsight Systems product boundaries.

Keep reusable wording and broad signal regex fragments here; report tooling
returns this user-facing boundary text.
"""

from __future__ import annotations

from .domain_semantics import recipe_domain_question_terms_re

LOCAL_PATH_LEAK_MESSAGE = "Internal cache paths must not be exposed in final answers."

DERIVED_ANALYSIS_VERBS_RE = r"(?:compute|calculate|measure|quantify|report|determine|find|analy[sz]e)"
RECIPE_DOMAIN_SEMANTIC_TERMS_RE = recipe_domain_question_terms_re()
RECIPE_DOMAIN_SEMANTICS_MESSAGE = (
    "Recipe/domain-owned performance semantics should not be reported as "
    "validated results from ad-hoc raw report SQL."
)
