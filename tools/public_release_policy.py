"""Exact exceptions for the intentionally public single-endpoint R engine.

The remaining skill directories and all skill instructions stay excluded.
This list is an independent boundary, not a recursive directory permission.
"""

from __future__ import annotations

from pathlib import PurePosixPath


PUBLIC_ENGINE_FILES = frozenset({
    "vera-experiment-designing/LICENSE.txt",
    "vera-experiment-designing/README.md",
    "vera-experiment-designing/scripts/R/config.R",
    "vera-experiment-designing/scripts/R/sample_size.R",
    "vera-experiment-designing/scripts/R/bayesian.R",
    "vera-experiment-designing/scripts/R/frequentist.R",
    "vera-experiment-designing/scripts/R/ppos.R",
    "vera-experiment-designing/scripts/R/run_framework.R",
    "vera-experiment-designing/scripts/R/validate_framework.R",
    "vera-experiment-designing/scripts/R/examples.R",
    "vera-experiment-designing/scripts/tests/run_tests.R",
    "vera-experiment-designing/scripts/tests/public_packaging.R",
    "vera-experiment-designing/examples/analysis-template.R",
    "vera-experiment-designing/examples/study-planning.R",
})


def public_path_error(relative: str) -> str | None:
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or ".." in path.parts
            or str(path) != relative or "\\" in relative):
        return "unsafe public path"
    lowered = path.name.casefold()
    if (lowered == "skill.md" or lowered.endswith((".skill", ".skill.enc"))):
        return "plaintext or encrypted skill bundles and instructions are excluded"
    if any(part.casefold().startswith("vera-") for part in path.parts):
        if relative not in PUBLIC_ENGINE_FILES:
            return "vera-* file is outside the exact public-engine allowlist"
    return None
