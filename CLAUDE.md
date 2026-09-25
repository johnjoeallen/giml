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

- Milestone: **M3 (Gate assessment) complete, awaiting review** (2026-09-24). `giml assess`, the survey script and per-project JDK choice are done; the survey ran on redkite, arete and grip (all below Tier B; grip closest at line 86.7%, strength 78.6%); Q7 and Q8 answered. Next: M4, build runner, cache and outcome logging.
- M1 accepted 2026-09-24. M2 checkpoint recorded (dff9705). M3 accepted 2026-09-25 (survey: redkite Tier A at 970df7f, grip and arete no tier yet).
- Self-gate during M3: Tier B PASS (line 99.4%, branch 97.2%, test strength 90.4%, mutation coverage 89.9%, excluded share 2.6%, 0 flaky of 5 runs).
- Current work (2026-09-25): **M5a, the analysis half of M5 (`giml plan --dry-run`)**, done before M4 (spec §17): resolve, declarations, exposure, candidates, report and the CLI are in and work on real projects. Parent and BOM upgrades are evaluated by resolving each candidate version (spec §8.2). Remaining gaps: enforcer violations with their offending artifacts (M4 baseline), `--rewind-to` with `--dry-run`, general updates of parents. M3 accepted 2026-09-25. redkite earned Tier A at `970df7f` (line 97.1, branch 91.5, strength 92.8, mutation 91.3).
- Update this line and the log below at each checkpoint.

## Build and test commands

Record here only commands that have actually been run successfully. Verified with Python 3.13.5.

- Setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- Fast tests: `.venv/bin/pytest` (Hypothesis derandomised; `HYPOTHESIS_PROFILE=explore` for a wider random search)
- Slow tests: `.venv/bin/pytest -m slow` (version differential test; needs `java` and Maven 3.9.11's `lib/maven-artifact-3.9.11.jar`, found via `mvn` on PATH or `GIML_MAVEN_ARTIFACT_JAR`)
- CLI: `.venv/bin/giml --version`
- Self-gate (Tier B): `.venv/bin/python scripts/selfgate.py --flake-runs 5` (about 25 s; runs coverage, mutmut and 5 test runs; `--skip-mutation` for quick coverage only)
- Workspace commands (M2): `.venv/bin/giml --state-dir <dir> plan <project> [--rewind-to <commit>]`, `giml status`, `giml clean <project> [--branches]`. Try them on a scratch `git clone` of a real project, never the original.
- Assess (M3): `.venv/bin/giml --state-dir <dir> [--config <global.yml>] assess <project> [--declared-tier B]`. Runs Maven, JaCoCo and PIT; a real project takes minutes.
- Survey (M3): `.venv/bin/python scripts/survey.py --state-dir <dir> [--config <global.yml>] <clone> [<clone> ...]` on scratch clones; writes `<dir>/reports/survey-<UTC>.md`.
- Dry-run analysis (M5a): `.venv/bin/giml --state-dir <dir> sync --osv`, then `.venv/bin/giml --state-dir <dir> plan --dry-run <project> [--strategy conservative|latest] [--scope cve|general] [--major-updates disallowed|allowed|ml]`. Builds nothing (Maven only resolves trees, seconds). The report names the coordinates that need `giml sync --central --coordinate ...`; run that and plan again. Reports land in `<dir>/reports/<run-id>/report.{json,md}`. Without a valid assessment (`giml assess`) it lists findings only and proposes nothing.
- Real sync into a throwaway state dir: `.venv/bin/giml --state-dir <dir> sync --coordinate com.fasterxml.jackson.core:jackson-databind` then `.venv/bin/giml --state-dir <dir> status` (about 6 s; OSV zip about 10 MB)

## Layout

Python package under `src/giml/` with tests under `tests/`; see spec section 3 for the layout. Record deviations from the spec layout here.

- Deviation: the default gate config lives in the package, `src/giml/gate/gate-config.yaml` (not `config/`), so an installed giml can find it; `giml.core.config.default_gate_config_path()`. GiML-pinned tool versions are in `src/giml/gate/tooling.yaml`.
- `scripts/selfgate.py` is giml's own quality gate (not part of the package).
- Snapshots live in `<state>/snapshots/<source>/<UTC-ts>-<hash12>/` with `manifest.json`. OSV has a derived `index.sqlite`; Central stores only its raw `central.json` (small enough to load whole, so no index).
- State DB: `<state>/state.db`; migrations 0001 (`snapshot`) and 0002 (`project`, `run` with only the columns used so far). Other spec §11 tables and columns arrive with their milestones.
- `src/giml/git/` holds the git wrapper, preflight, lock, worktrees and rewind; `src/giml/workspace.py` orchestrates `plan` (M2 stub), `clean` and crashed-run detection. `src/giml/maven/project.py` discovers the reactor (root pom plus modules, including profile modules).
- `src/giml/gate/` holds tooling setup, report collectors, tiers and `assess`; `src/giml/maven/runner.py` runs Maven; `src/giml/maven/jdk.py` chooses the JDK (spec §3.1). `scripts/survey.py` tabulates assessments across projects.
- Deviation: planner code lives in `src/giml/plan/` (exposure now; candidates, analysis and report next), not `core/`, so it can import `maven/` and `data/` without a cycle through `core/`.
- M4 modules: `src/giml/maven/isolation.py` (per-run temp dir, disk check), `failures.py` (class and signature from a Maven log), `build.py` (`MavenBuildRunner`: stages compile, unit_test, enforcer), `cache.py` (`CachingBuildRunner`: content-addressed stage outcomes, per-stage hit/miss/time metrics). Real Maven logs of five failing projects are in `tests/fixtures/logs` (two copies each, run from different directories).
- Parent/BOM units: `src/giml/plan/parents.py` writes each candidate version into the throwaway worktree's POM (`with_version`, always restored), resolves, and compares exposure; at most 40 versions per unit, nearest first. Real result: grip's Boot 3.3.5 to 3.5.15 would clear 70 of 75 advisories but is blocked because Boot 3.5 manages `org.hamcrest:hamcrest` 3.x (test scope); the reason line names it.
- M5a analysis modules: `src/giml/maven/tree.py` (resolved trees from the pinned plugin's JSON) and `src/giml/maven/declarations.py` (where each version is declared: literal, property, managed, external parent/BOM; read-only, records the editable text span). Fixtures: `tests/fixtures/trees/`, `tests/fixtures/poms/redkite/` (real).
- Config files: gate config (package), global `~/.giml/config.yml` (`jdks` only), project `.giml/settings.yml` (`jdk` or `java_home`, `allow_exclusions`), all parsed in `src/giml/core/config.py`.
- Platform requirements and how giml stays out of the developer's checkout: `docs/platform.md`.

## Decisions log

Newest first. One line each: date, decision, reason.

- 2026-09-25: ML is reframed (spec §16): under the default `conservative` strategy correctness comes from the deterministic engine; ML is an optimisation and triage layer on top, never a precondition. Layers: failure classifier (unchanged); skip-ahead / pass-fail predictor (replaces the candidate ranker: skips or escalates past steps very likely to fail, shadow mode first, never leaves a CVE open); retrieval feeds the predictor with similar past failures; abstention plus a separate review-worthiness signal on passing builds. Headline measures: builds per accepted upgrade, builds skipped correctly/incorrectly, false-fix rate, abstention rate. Spec §2 was left untouched as instructed (rule 8 still says "a prior for ordering and abstention", which under-describes skipping); `core/interfaces.py` still has the old `RiskScorer` docstring. User instruction.
- 2026-09-25: M4 is under way (plan agreed in chat: run isolation, build stage runner with failure classes and error signatures, content-addressed cache with hit/miss metrics, baseline incl. the enforcer reference set and rewound baseline, outcome logging, checkpoint). Step 1 done: every run has its own temp directory via `TMPDIR` and `JAVA_TOOL_OPTIONS`, removed at the end, plus a free-space and inode check before a run (spec §9.2, `src/giml/maven/isolation.py`).
- 2026-09-25: `planning.major_updates_test_scope` (`disallowed` default, or `allowed`): a major change in a dependency that only has test scope may pass the major-update gate, for a dependency's own fix and for majors through a parent/BOM (spec §8.7). Found on grip, where Boot 3.5.x (clearing 70 of 75 advisories) was blocked only by hamcrest 3.x. User choice, default strict.
- 2026-09-25: Major updates are gated by `planning.major_updates`: `disallowed` (default), `allowed`, or `ml` (only with ML evidence that no developer code change is needed; behaves as `disallowed` until M8). Checked on the candidate's resolved tree before any build, so indirect majors (BOM, transitives) count; blocked candidates are recorded and reported (spec §8.7). Run-level setting only; repo files cannot loosen it. User instruction.
- 2026-09-25: Planning is set by two independent options (spec §8): `strategy` `conservative` (default; patch, then minor, major only for CVE fixes) or `latest`, and `scope` `cve` (default; only CVE-affected dependencies move, others only when forced) or `general` (CVE first, then everything else). They replace the single `objective_profile` (`conservative_patch`/`latest_first`/`cve_first`) and apply to CVE fixes and general updates alike; CVEs are always settled first. Config keys `planning.strategy` and `planning.scope` (config version stays 3: `planning` never affects an assessment). User design.
- 2026-09-25: Milestones reordered: the no-build analysis of M5 (resolve, CVE exposure, candidate ladder, report; `plan --dry-run`) is M5a and comes before M4, since redkite already meets Tier A and analysis needs no build machinery. Rewind comparison stays last. User choice.
- 2026-09-24: The planner may add `<exclusion>`s to fix enforcer violations only when the project's `.giml/settings.yml` sets `allow_exclusions: true` (default false); parsed now, used from M5 (spec §8.4). User choice.
- 2026-09-24: A clean enforcer run is required to finish a plan, not to start one. Baseline violations are recorded and become planning goals (pins per §8.4); candidates may not add violations; fewer violations ranks first in every profile; a run left with violations exits 1 with its progress on the branch. User instruction.
- 2026-09-24: Tier numbers stay at gate config version 3 (spec Q7); the survey projects were too far below Tier B to calibrate boundaries. Policy's 80% = PIT mutation coverage, 85% = test strength (Q8), as Tier B already says. User answers.
- 2026-09-24: Initial project set: redkite, arete and grip (spec Q6, M3 part), the user's own GitHub projects with unit tests; chronograf dropped. The deliberately-behind project for M5 is still open.
- 2026-09-24: JDK choice (spec §3.1): project `.giml/settings.yml` gives `jdk` (major or full version) or `java_home`; versions resolve via global `~/.giml/config.yml` `jdks`, then `~/.m2/toolchains.xml`; no settings means inherited JAVA_HOME. The global config knows nothing about projects. A repo-named `java_home` is accepted if it has `bin/javac` (a deliberate exception to hard rule 5). User choice; found when chronograf (Lombok 1.18.32) failed on JDK 25.
- 2026-09-24: A module with production code but no tests blocks every tier (PIT skips such modules, so mutation figures would be inflated); the report names them. User choice.
- 2026-09-24: Coverage is computed from one whole-reactor JaCoCo CLI report (org.jacoco.cli, giml-pinned) because JaCoCo's per-module report goal skips modules without tests. Found on the fixture reactor.
- 2026-09-24: Touchpoint metrics (spec §6.3 step 6) move to M5, where the dependencies under upgrade are known.
- 2026-09-24: giml adds missing quality tooling (JaCoCo, PIT, enforcer bans) in its worktree as a separate `[giml-setup]` commit with giml-pinned versions; a low score still leaves the branch so the developer can take the setup back and raise scores. Only a Maven build with unit tests is a hard prerequisite (spec §3). User instruction.
- 2026-09-24: Multi-module reactors are in phase 1 (so ../redkite can be used); rewind restores every reactor pom.xml; M5 edits versions in the declaring module. User choice.
- 2026-09-24: Git LFS allowed; giml disables the LFS filter on every git call so worktrees hold pointer files and nothing is downloaded. Submodules still refused. User choice.
- 2026-09-24: Quality tooling = JaCoCo, PIT and maven-enforcer with banDuplicateClasses (extra-enforcer-rules), banDuplicatePomDependencyVersions and dependencyConvergence. User instruction.
- 2026-09-24: A tier measures test quality (unit/PIT levels) only; the CVE and best-update criteria apply identically to every tier (spec §6, §8.5). User clarification.
- 2026-09-24: Tiers hold only unit/PIT thresholds plus max_excluded_share. Integration tests and the startup check are `verification` stages for every tier; autonomy is a separate tier->action mapping. Gate config bumped to version 3. User choice.
- 2026-09-24: M2 `giml plan` is a stub that stops after workspace setup (stop reason `planning_not_implemented`) until M5. User choice.
- 2026-09-24: giml's commits use the developer's git identity plus a `Generated-by: giml <version>` trailer; never GPG-signed. User choice (identity); signing off so runs cannot block.
- 2026-09-24: Project key = `<repo name>-<sha256(repo root + subdir)[:8]>`; projects may live in a repo subdirectory.
- 2026-09-24: Unpushed local commits are allowed; submodules and Git LFS are refused in phase 1 (spec Q3, Q4). User answer.
- 2026-09-24: Rewind mode `plan --rewind-to <commit>` (spec §5.3): restore `pom.xml` from an ancestor commit as a marked synthetic first commit, for test/training data. Git history only, no date-based rewind. User choice.
- 2026-09-24: (Superseded the same day) Phase 1 was single-module only.
- 2026-09-24: Maven builds use the developer's `~/.m2` settings and local repository; only the smoke-launched app gets a per-trial home (spec §9.2).
- 2026-09-24: Per-version release dates come from HEAD `Last-Modified` on each version's `.pom`, cached forever (spec Q10). Search API rejected: index stale (no jackson-databind >= 2.20.0 while metadata lists 2.22.3).
- 2026-09-24: Severity uses an in-house CVSS v3.x calculator, falling back to the OSV/GHSA label; score source recorded (spec Q11).
- 2026-09-24: Spec §9.3 (sandbox posture) dropped: laptop builds must pull recent deps from whatever Maven repo is configured. User instruction.
- 2026-09-24: Network is allowed; the restriction is no AI API calls (hard rule 4 rewritten). User instruction.
- 2026-09-24: Engine is all Python (≥3.12, stdlib-first, PyYAML, pytest/Hypothesis; own gate via coverage.py + mutmut). User instruction; spec section 3 rewritten.
- 2026-09-24: (Superseded the same day) Target projects had to configure JaCoCo/PIT themselves.
- 2026-09-24: Dependency tree comes from pinned `maven-dependency-plugin` JSON output; Python has no Maven Resolver.
- 2026-09-24: Reuse source is RedKite (`../redkite`), not Arete: port OSV range matching + `maven-metadata.xml` parsing to Python; skip its live OSV queries, TTL caches, custom version comparators.
- 2026-09-24: Spec moved to `docs/giml-spec.md`; repo initialised with `git init` (branch `main`).

## Gotchas

Project-specific traps discovered while working (tool quirks, platform differences, flaky areas). One line each.

- Git worktrees share the repository config, so the developer's `user.name`/`user.email` apply to giml's commits without passing them through the environment (the wrapper scrubs `GIT_*` anyway).
- `git for-each-ref` globs do not cross `/`; use a trailing-slash prefix (`refs/heads/giml/rewind/`) to list nested branch names. `git branch --list` marks branches checked out in another worktree with `+`.
- A `ProjectLock` releases when garbage-collected (its file closes); hold a reference for the whole run.
- Rewind replaces whole `pom.xml` files (every reactor module that existed at the rewind commit): on real history (chronograf) that also rewinds the project's own groupId/artifactId/name and properties, not just dependency versions.
- LFS: the git wrapper clears the LFS filter (`smudge`, `clean`, `process`, `required=false`) only for `worktree`/`restore`/`add`/`commit`. Status checks in the developer's checkout must keep the filter, or real LFS files look modified against their pointer blobs. Verified on a RedKite clone: 134-byte pointer in the worktree, 169 MB real file in the checkout.
- mutmut injects attributes into mutated classes; Protocol declarations (`core/interfaces.py`) are in `do_not_mutate` and counted as excluded by the self-gate.
- `http.client` does not raise when a chunked read ends before Content-Length; `UrlLibFetcher.download` checks the byte count itself.
- mutmut runs tests from a copy in `mutants/`; anything tests read outside `src/` and `tests/` must be listed in `[tool.mutmut] also_copy` (currently `config/`, `scripts/`).
- mutmut mutates string literals (PIT does not), so SQL keyword case creates equivalent mutants; SQL literals carry `# pragma: no mutate` and the self-gate counts those lines as excluded.
- argparse help wraps to the terminal width; golden help tests pin `COLUMNS=100`.
- Lombok 1.18.32 does not compile on JDK 25 (`cannot find symbol builder()`); chronograf needs JDK 17. A project failing only on the default JDK is a JDK choice problem, not a giml setup bug: compile a plain clone to tell them apart.
- PIT runs every test class unless told otherwise, including Failsafe `*IT` tests that Surefire skips; giml's PIT block excludes them. PIT on arete takes about 11 minutes.
- Maven caches a failed artifact lookup and words it differently on the next run ("was not found in ... during a previous attempt"); `BanDuplicateClasses` lists offending artifacts in no fixed order. Signatures normalise both, found by testing real logs from two directories.
- The git wrapper allows `git diff` only as `diff HEAD ...` (other first arguments such as `--output=FILE` could write); the stage cache keys on `git diff HEAD --binary`.
- Tests must not depend on the disk: an autouse fixture stubs `giml.maven.isolation.check_space`; tests of the real check carry `@pytest.mark.real_space_check`.
- Maven's `ComparableVersion` is not a total order on degenerate strings (`"" < A < 0A0 < ""`) and its canonical form is not always idempotent (`0.alpha-ga -> 0.alpha -> alpha`). The port reproduces both; never assume sort stability on junk versions.

## Open CONFIRM items

Tracked in spec section 19. Move an item to the decisions log when it is answered.
