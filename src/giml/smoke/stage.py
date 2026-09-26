"""The ``startup`` stage as a build runner: package the artifact, then boot it (spec sections 9 and 12)."""

from __future__ import annotations

import dataclasses
import itertools
from pathlib import Path

from giml.core.interfaces import BuildRunner
from giml.maven.build import StageOutcome
from giml.smoke.runner import SmokeRunner


class StartupAwareRunner:
    """Delegates every stage to ``inner`` except ``startup``, which is ``package`` (in ``inner``) followed by a smoke boot.

    A packaging failure is the candidate's: it is reported as the ``startup`` stage with the packaging failure's own class.
    """

    def __init__(self, inner: BuildRunner, smoke: SmokeRunner | None) -> None:
        self.inner, self.smoke = inner, smoke
        self._trials = itertools.count(1)

    def run_stage(self, worktree: Path, stage: str, timeout_seconds: float) -> StageOutcome:
        if stage != "startup":
            return self.inner.run_stage(worktree, stage, timeout_seconds)
        if self.smoke is None:
            raise ValueError("the startup stage needs smoke settings")
        packaged = self.inner.run_stage(worktree, "package", timeout_seconds)
        if not packaged.passed:
            return dataclasses.replace(packaged, stage="startup")
        return self.smoke.run(worktree, next(self._trials))
