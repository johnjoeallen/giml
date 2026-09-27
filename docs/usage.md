# Using giml

giml plans and verifies joint Maven dependency upgrades for a git project and leaves the result on a local
branch in a worktree for you to review. It never pushes, never opens a pull request, never touches your working
tree, index or current branch, and makes no AI API calls. The full behaviour is in `docs/giml-spec.md`.

## Set up

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/giml --version
```

Needs Java, Maven on `PATH`, git, and (for the startup check with a database) `psql`. Everything giml keeps
(snapshots, the state database, worktrees, logs, reports, the build cache) lives in the state directory,
`--state-dir DIR` (default under your home). Try it on a scratch `git clone` first.

## The commands

| command | what it does |
| --- | --- |
| `giml sync [--osv] [--central --coordinate G:A ...]` | fetch the advisory (OSV) snapshot and Maven Central version metadata; planning reads only these recorded snapshots |
| `giml assess PROJECT [--declared-tier B]` | measure test quality (JaCoCo, PIT, flakiness) and record the earned tier; takes minutes to tens of minutes |
| `giml plan --dry-run PROJECT` | the analysis only: resolved trees, CVE exposure, candidates and a reason line per dependency; builds nothing |
| `giml plan PROJECT` | verify the baseline, search for the best joint upgrade, commit each accepted step on a result branch, write the report |
| `giml status` | snapshots and crashed runs |
| `giml clean PROJECT [--branches]` | remove giml's worktrees (and result branches) |
| `giml export-examples --out FILE` | export the labelled build outcomes as JSONL |

`giml plan` options: `--strategy conservative|latest`, `--scope cve|general`,
`--major-updates disallowed|allowed|ml`, `--major-updates-test-scope disallowed|allowed`,
`--compare-naive` (also build each dependency's bump to its newest release on its own, to compare),
`--rewind-to COMMIT` (start from the `pom.xml` files of an older commit, for testing), `--gate-config FILE`.

Typical run: `giml sync --osv`, then `giml plan --dry-run PROJECT`, `giml sync --central --coordinate ...` for what it
lists, `giml assess PROJECT`, then `giml plan PROJECT` and review with `git diff <base>..<result branch>`.

## What a plan verifies

Every candidate is verified by a real build, in this order: a vulnerability check on the resolved tree (a change
that makes the exposure worse fails without a build), compile, the enforcer (convergence, duplicate classes),
unit tests, integration tests when the project has them, the startup check when configured, and PIT once on the
state that will be kept. A candidate that fails any of them is not committed. What could not be moved is reported
with the reason and the trigger that would justify trying again.

## Configuration

* Gate config (`src/giml/gate/gate-config.yaml`, or `--gate-config`): tier thresholds, verification stages
  (`integration_tests`, `startup_check`, `pit`) and planning defaults (`strategy`, `scope`, `major_updates`,
  cooldown, build and time budgets, snapshot age limit).
* Global `~/.giml/config.yml`: `jdks`, the JDKs giml may use. It knows nothing about projects.
* Project `.giml/settings.yml`, read from the base commit: `jdk` or `java_home`, `allow_exclusions`, and the
  `smoke` section of the startup check (spec section 12): `artifact`, `profile`, `properties`, `env`, `ready`,
  and an optional `database` given as the *names* of your environment variables. Settings are data only; a
  command-like key or a literal secret is refused.

## Exit codes

0 a plan was produced and its enforcer state is clean; 1 no improvement or the enforcer could not be made clean
(verified progress is still on the branch); 2 refused to start (dirty repository, detached HEAD, lock held);
3 ineligible project; 4 infrastructure failure (network, disk, memory); 5 configuration error, or the build or unit
tests fail at the base commit.

## Where things go

`<state>/reports/<run-id>/` holds `baseline.json`, `plan.json` and `plan.md`; `<state>/runs/<run-id>/logs/` the
build logs; `<state>/cache/` the content-addressed stage cache (a repeated identical run is answered from it).
`docs/platform.md` explains how giml stays out of your checkout.

**Logs persist.** Every stage of every attempt — the baseline and every trial the planner built, one build
per candidate the search actually tried — writes its own log file under `<state>/runs/<run-id>/logs/`, and
giml never deletes them: not on a cache hit (which writes its own small pointer log instead of skipping the
file), not when `giml clean` removes the worktree that produced them, and not when a later run reuses the
same project. A stage that failed for an infrastructure reason and was retried still leaves its own log on
disk, even though the retried attempt is not logged as a training example (spec 15: it says nothing about the
project). This is deliberate: collecting real failures (for example from projects with known CVEs or known
build issues) across many runs is how the ML layers (spec section 16) get a corpus bigger than giml's own
fixture logs. `giml export-examples --out FILE` exports one JSON line per logged example; its `features.log_path`
points at the exact log file the example came from. giml never prunes the state directory itself, so its size
is yours to manage.

## Testing giml

`.venv/bin/pytest` (fast), `.venv/bin/pytest -m slow` (real Maven, javac, a JVM and, with docker, a throwaway
PostgreSQL), `.venv/bin/python scripts/selfgate.py --flake-runs 5` (giml's own Tier B gate).
