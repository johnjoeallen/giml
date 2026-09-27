# GiML

**G**ated **I**ncrements (**ML**) — a local, report-only CLI that plans and
verifies joint Maven dependency upgrades for a Java project.

GiML takes its idea of gated, verified upgrades from
[RedKite](https://github.com/johnjoeallen/redkite), but drops RedKite's UI in
favour of a fully automated flow: point GiML at a project and it plans,
builds, and verifies candidate upgrades itself, leaving the result on a local
branch for the developer to review. Where a joint upgrade is blocked by
dependency convergence, an ML layer is planned to help decide how to proceed
(see [Roadmap](#roadmap)) — deterministic verification is always the source
of truth, ML is only ever a prior.

GiML never pushes and never opens a pull request.

## Hard rules

Enforced by code and tests, not just convention (full detail in
`docs/giml-spec.md` section 2):

1. Never push. No remote write of any kind.
2. Never modify the developer's working tree, index or current branch. All
   work happens in tool-owned worktrees.
3. Refuse to start on a dirty repository.
4. No AI API calls, ever. Other network access is allowed; planning reads
   only recorded snapshots.
5. Never execute commands taken from repository files. Repo files declare
   data only.
6. Never log or report secret values. Record variable names only.
7. Verification rules come from the base commit, never from a candidate
   state.
8. Every suggestion is verified by a real build. ML output is a prior, never
   a verdict.

## Requirements

- Python >= 3.12
- Java + Maven (a working `mvn` on `PATH`, or a project-specific JDK
  configured per `docs/platform.md`)
- git

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Add the `ml` extra for the ML layers (scikit-learn, joblib):

```bash
.venv/bin/pip install -e '.[dev,ml]'
```

## Usage

Always work against a scratch `git clone` of the target project, never the
original checkout.

```bash
# Pull CVE data
.venv/bin/giml --state-dir <dir> sync --osv

# See what GiML would propose, without building anything
.venv/bin/giml --state-dir <dir> plan --dry-run <project>

# Assess a project's current test quality (unit tests, coverage, mutation score)
.venv/bin/giml --state-dir <dir> assess <project>

# Plan and verify upgrades for real (builds in a worktree, commits passing steps)
.venv/bin/giml --state-dir <dir> plan <project> [--strategy conservative|latest] [--scope cve|general]

# Inspect and tidy up GiML's own state
.venv/bin/giml --state-dir <dir> status
.venv/bin/giml --state-dir <dir> clean <project> [--branches]
```

Every command that touches a real project takes a `--state-dir` pointing at
a throwaway directory GiML uses for snapshots, its own database, worktrees
and reports. See `CLAUDE.md` for the full, currently-working command list
and evidence from real runs.

## Development

```bash
.venv/bin/pytest                       # fast tests
.venv/bin/pytest -m slow               # slow tests (real Maven/Postgres, ~5 min)
.venv/bin/python scripts/selfgate.py --flake-runs 5   # GiML's own quality gate
```

## Documentation

- `docs/giml-spec.md` — the full specification; source of truth for scope,
  behaviour, milestones and acceptance criteria.
- `CLAUDE.md` — current milestone status, working commands, decisions log
  and known gotchas.
- `principles.md` — generic working principles for anyone (human or agent)
  contributing to this repo.
- `docs/platform.md` — platform requirements and how GiML stays out of the
  developer's checkout.

## Roadmap

GiML's deterministic engine (resolve, verify, commit) is the source of
correctness. An ML layer is being added on top as an optimisation and
triage aid, never a precondition:

- a failure classifier over build logs (implemented, not yet wired into any
  decision — see `CLAUDE.md` current status),
- a skip-ahead / pass-fail predictor to reduce the number of builds needed
  per accepted upgrade,
- assistance when a joint upgrade is blocked by dependency convergence:
  suggesting which side of the conflict to move, informed by past
  transitions, always verified by a real build before being kept.

## License

Apache License 2.0 — see `LICENSE`.
