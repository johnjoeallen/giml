# CLAUDE.md

@principles.md

Generic working instructions live in `principles.md` (imported above). Do not restate them here. This file holds only GiML-specific facts and is a living document: update it at every checkpoint.

## Project

GiML (**G**ated **I**ncrements (**ML**)) is a local, report-only CLI that plans and verifies joint Maven dependency upgrades for a git project and leaves the result on a local branch in a worktree for the developer to review. It never pushes and never opens a PR.

The full specification is `docs/giml-spec.md`. It is the source of truth for scope, behaviour, milestones and acceptance criteria. Read it before planning any milestone.

## Hard rules (source of truth: spec section 2)

These are enforced by code and tests, not just convention:

1. Never push. No remote write of any kind.
2. Never modify the developer's working tree, index or current branch. All work happens in tool-owned worktrees.
3. Refuse to start on a dirty repository.
4. No AI API calls, ever. Other network access is allowed; planning reads only recorded snapshots.
5. Never execute commands taken from repository files. Repo files declare data only.
6. Never log or report secret values. Record variable names only.
7. Verification rules come from the base commit, never from a candidate state.
8. Every suggestion is verified by a real build. ML output is a prior, never a verdict.

## Current status

- Milestone: **not started** (next: M1, Foundations)
- Update this line and the log below at each checkpoint.

## Build and test commands

To be filled in during M1 once the skeleton builds. Record here only commands that have actually been run successfully.

## Layout

Multi-module Maven project; see spec section 3 for the module list and responsibilities. Record deviations from the spec layout here.

## Decisions log

Newest first. One line each: date, decision, reason.

- 2026-09-24: Rewind mode `plan --rewind-to <commit>` (spec §5.3): restore `pom.xml` from an ancestor commit as a marked synthetic first commit, for test/training data. Git history only, no date-based rewind. User choice.
- 2026-09-24: Phase 1 supports single-module projects only; multi-module refused with exit 3 and deferred to a later phase. User instruction.
- 2026-09-24: Maven builds use the developer's `~/.m2` settings and local repository; only the smoke-launched app gets a per-trial home (spec §9.2).
- 2026-09-24: Per-version release dates come from the Maven Central search API (`core=gav`); `maven-metadata.xml` has none (spec Q10).
- 2026-09-24: Severity uses an in-house CVSS v3.x calculator, falling back to the OSV/GHSA label; score source recorded (spec Q11).
- 2026-09-24: Spec §9.3 (sandbox posture) dropped: laptop builds must pull recent deps from whatever Maven repo is configured. User instruction.
- 2026-09-24: Network is allowed; the restriction is no AI API calls (hard rule 4 rewritten). User instruction.
- 2026-09-24: Engine is all Python (≥3.12, stdlib-first, PyYAML, pytest/Hypothesis; own gate via coverage.py + mutmut). User instruction; spec section 3 rewritten.
- 2026-09-24: Target projects must already have unit tests, JaCoCo, PIT and optionally Failsafe configured; giml checks, never installs (exit 5). User instruction.
- 2026-09-24: Dependency tree comes from pinned `maven-dependency-plugin` JSON output; Python has no Maven Resolver.
- 2026-09-24: Reuse source is RedKite (`../redkite`), not Arete: port OSV range matching + `maven-metadata.xml` parsing to Python; skip its live OSV queries, TTL caches, custom version comparators.
- 2026-09-24: Spec moved to `docs/giml-spec.md`; repo initialised with `git init` (branch `main`).

## Gotchas

Project-specific traps discovered while working (tool quirks, platform differences, flaky areas). One line each.

- (none yet)

## Open CONFIRM items

Tracked in spec section 19. Move an item to the decisions log when it is answered.
