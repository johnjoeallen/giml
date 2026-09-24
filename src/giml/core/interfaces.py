"""Seams for environment-specific behaviour (spec section 4.2).

Phase 1 implements only local versions. Signatures of interfaces implemented in later milestones
are provisional and are finalised in the milestone named in their docstring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from giml.core.model import Coordinate, Finding, SnapshotInfo, VersionRelease


class StateStore(Protocol):
    """Persistent run state. Phase 1: SQLite (M1). Later: PostgreSQL."""

    def record_snapshot(self, snapshot: SnapshotInfo) -> None:
        """Record a newly written data snapshot."""

    def latest_snapshot(self, source: str) -> SnapshotInfo | None:
        """The most recently fetched snapshot for a source, if any."""

    def list_snapshots(self) -> list[SnapshotInfo]:
        """All recorded snapshots, newest first."""


class ResultCache(Protocol):
    """Content-addressed outcome cache. Phase 1: local directory (M1). Later: shared service."""

    def key(self, parts: dict[str, Any]) -> str:
        """Stable content hash of the key parts."""

    def get(self, key: str) -> dict[str, Any] | None:
        """The stored value for a key, or None on a miss."""

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Store a value under a key."""


class AdvisorySource(Protocol):
    """Vulnerability advisories. Phase 1: local OSV snapshot (M1). Later: shared service."""

    @property
    def snapshot_id(self) -> str:
        """The snapshot every answer comes from, recorded with each run."""

    def affecting(self, coordinate: Coordinate, version: str) -> list[Finding]:
        """Advisories that affect one version of a coordinate."""


class ArtifactMetadataSource(Protocol):
    """Published versions and release dates. Phase 1: local Central snapshot (M1)."""

    @property
    def snapshot_id(self) -> str:
        """The snapshot every answer comes from, recorded with each run."""

    def versions(self, coordinate: Coordinate) -> list[VersionRelease] | None:
        """All published versions in Maven order, or None if the coordinate was not synced."""


class RepoSource(Protocol):
    """Where the project comes from. Phase 1: local git working directory (M2)."""

    def base_commit(self) -> str:
        """The SHA the run is based on."""

    def root(self) -> Path:
        """Root of the repository checkout."""


class BuildRunner(Protocol):
    """Runs verification stages. Phase 1: local Maven in isolated dirs (M4)."""

    def run_stage(self, worktree: Path, stage: str, timeout_seconds: int) -> Any:
        """Run one verification stage and return its classified outcome."""


class PublishTarget(Protocol):
    """Where results go. Phase 1: report plus a local branch (M5). Later: pull requests."""

    def publish(self, run_id: str, plan: Any) -> None:
        """Publish a finished plan."""


class SmokeSettingsProvider(Protocol):
    """Startup-check settings. Phase 1: `.redkite/settings.yml` at the base commit (M6)."""

    def load(self, repo: Path, base_commit: str) -> Any:
        """Read and validate smoke settings from the base commit."""


class RiskScorer(Protocol):
    """Orders candidate states. Phase 1: deterministic heuristic (M5). Later: trained model."""

    def score(self, candidate: Any) -> float:
        """Estimated breakage risk; lower is tried first. A prior, never a verdict."""
