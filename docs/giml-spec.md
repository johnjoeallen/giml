# GiML — Specification

Status: draft v1 for implementation with Claude Code
Name: GiML — **G**ated **I**ncrements (**ML**). Gated by test-quality tiers; each accepted upgrade step is a verified increment on the result branch; ML layers are added only where they earn their place. CLI command and identifiers use lowercase `giml`; the name appears in branch prefixes, cache paths, database prefixes and package names.

---

## 0. How to use this document (instructions to Claude Code)

- Implement in the **milestones** of section 17, in order. Each milestone ends at a **checkpoint**: stop, run the acceptance checks, summarise what exists, and wait for confirmation before starting the next one.
- Use **plan mode before each milestone**. Keep increments small and commit each coherent step separately.
- Do not build anything from a later milestone early. Do not add features not in this document without asking.
- Where this document says **CONFIRM**, ask before assuming.
- Agent instruction files at the repo root (create them in M1; drafts are supplied with this spec):
  - `principles.md` holds **all generic working instructions** (how to plan, verify, test, use git, handle secrets, communicate). Any generic rule is added here and nowhere else.
  - `CLAUDE.md` imports `principles.md` (`@principles.md`) and holds only GiML-specific facts: status, working build/test commands, layout deviations, hard rules summary, decisions log, gotchas. Update it at every checkpoint. It must not restate `principles.md`.
  - `AGENTS.md` is a thin file for other agents: it points to `principles.md`, this spec (`docs/giml-spec.md`) and `CLAUDE.md`. It must not duplicate generic rules.
  - Keep this spec at `docs/giml-spec.md`.
- The tool must itself meet its own Tier B gate (section 6) from the first milestone, and Tier A by the end of milestone 6. Treat that as a dogfooding requirement.

---

## 1. Purpose and scope

### 1.1 Problem

Dependabot-style tools propose each dependency upgrade independently and default to "latest". This causes noisy PRs, broken builds, and upgrades that ignore how dependencies constrain each other (transitive graphs, BOMs). They also have no notion of how trustworthy a project's test suite is.

### 1.2 What giml does

For a local Maven project in a git repository, giml:

1. Verifies the repository is in a safe state (clean, git-managed).
2. Assesses how trustworthy the project's tests are (**tier**), using unit coverage and PIT mutation results.
3. Plans **joint** dependency upgrades, choosing for each dependency the best version that works (not blindly the newest), balancing CVE exposure against recency.
4. Verifies each candidate state by building, running tests, and **starting the packaged application** with a designated Spring profile.
5. Leaves the result on a local **branch in a git worktree**. It never pushes and never opens a PR. The developer inspects and decides.
6. Records every attempt as a labelled outcome for later machine learning.

### 1.3 Phase 1 scope (this document)

In scope:
- Local, report-only CLI. Python engine. Maven projects, single-module or multi-module reactors (confirmed 2026-09-24).
- Data sources: OSV and Maven Central only.
- Deterministic planner with joint sets, CVE-minimal bumps, pins and bisection.
- Tier assessment (JaCoCo + PIT), startup verification, outcome logging.
- CPU-only ML layer (milestone 8+) with no AI API calls, added only if it measurably reduces builds per accepted upgrade.

Out of scope for phase 1:
- PR/MR creation, CI integration, central service, GPU training, LoRA/generative fine-tuning.
- Any call to a frontier/hosted model, at build time or run time.
- Gradle, npm or other ecosystems.
- Exploit-likelihood feeds (KEV, EPSS). OSV severity is the only risk signal.

### 1.4 Future direction (design for it, do not build it)

Later the same engine will run centrally, from CI, and open PRs. Therefore all environment-specific behaviour sits behind interfaces (section 4.2), the engine is stateless with explicit inputs and outputs, and results are cacheable by content.

---

## 2. Hard rules (non-negotiable)

1. **Never push.** No `git push`, no remote write of any kind. Enforce it in code (a wrapper around git that rejects `push`, and worktrees created without push configuration), and add a test.
2. **Never modify the developer's working tree, index or current branch.** All work happens in tool-owned worktrees.
3. **Refuse to start on a dirty repository** (section 5.1).
4. **No AI API calls.** giml never calls a hosted AI/LLM API (at build time, run time, or to generate or label data), verified by a test. Other network access is allowed; planning still reads only recorded data snapshots (section 7.3) so runs stay reproducible.
5. **Never execute commands taken from repository files.** Repo files may declare *data* (profile name, readiness hints) but never *what to run*.
6. **Never log or report secret values.** Record environment variable *names* only.
7. **Verification rules come from the base commit**, never from a candidate state, so an upgrade cannot change its own checks.
8. **Every suggestion is verified by a real build.** ML output is a prior for ordering and abstention, never a verdict.

---

## 3. Technology choices

Confirmed 2026-09-24 (was CONFIRM item 1):

- Engine: **Python ≥ 3.12**, packaged with `pyproject.toml`, run from a project-local virtual environment (nothing installed globally).
- Libraries: standard library first — `argparse` (CLI), `json`, `sqlite3` (state), `urllib` (sync), `subprocess` + the **git CLI** (git operations). **PyYAML** (`safe_load` only) for YAML. Every further dependency needs a stated reason.
- Dependency resolution: invoke `org.apache.maven.plugins:maven-dependency-plugin:<pinned>:tree -DoutputType=json` and read the JSON file it writes. Parse structured plugin output only, never Maven log text. The plugin coordinate and version are giml's own, never taken from the project.
- POM handling: lossless text edits located by a POM parser (section 8.4).
- Version comparison: a Python port of Maven's `ComparableVersion`, tested against the cases in Maven's own `ComparableVersionTest`.
- API-diff analysis: **japicmp** (or revapi), run as a pinned jar owned by giml — used in milestone 5 for candidate filtering.
- Coverage: **JaCoCo** XML reports; mutation: **PIT** (pitest) XML reports.
- ML (milestone 8+): PyTorch and scikit-learn, used in-process. CPU only.
- Tests for giml itself: **pytest** and **Hypothesis**. giml's own quality gate (section 0) uses **coverage.py** (line and branch) and **mutmut** (test strength and mutation coverage) as the Python equivalents of JaCoCo and PIT.
- Reuse from the user's **RedKite** project (`../redkite`, Java), translated to Python where suitable: the OSV affected-range matching logic and its fixture tests, and `maven-metadata.xml` parsing. Its live per-package OSV querying, TTL caches and custom version comparators are not reused.

Target project prerequisites (giml checks these and never installs or configures them):

- A working Maven build on a JDK the developer has installed.
- Unit tests (Surefire), **JaCoCo** producing `jacoco.xml`, and **pitest-maven** configured with XML output.
- **maven-enforcer-plugin** bound to the build with all three duplicate/convergence bans (confirmed 2026-09-24): `banDuplicateClasses` (from `org.codehaus.mojo:extra-enforcer-rules`), `banDuplicatePomDependencyVersions` and `dependencyConvergence`. These make duplicate classes and version conflicts fail the build, which the planner relies on (failure classes `duplicate_classes`, `enforcer_convergence`).
- Optional: integration tests (Failsafe / `*IT`).
- Single-module projects and multi-module reactors are both supported. The reactor is the project's `pom.xml` plus every module it declares (at top level or in profiles), recursively. A declared module whose `pom.xml` is missing is a configuration error (exit 5); a module outside the project directory is unsupported (exit 3). In a reactor the prerequisites may be declared in the parent and inherited.
- Missing prerequisites stop `assess` and `plan` with exit code 5 and name what is missing.

Repository layout (Maven multi-module):

```
giml/
  principles.md        generic agent instructions (single source)
  CLAUDE.md            project state; imports principles.md
  AGENTS.md            thin pointer for other agents
  pyproject.toml
  src/giml/
    core/              domain model, interfaces, planner, ranking
    git/               preflight, worktree lifecycle, git wrapper (no push)
    data/              OSV + Maven Central sync, snapshots
    maven/             pom editing, resolution, build runner, version ordering
    gate/              JaCoCo/PIT collectors, tier evaluator
    smoke/             startup verification, database lifecycle
    store/             SQLite state store, content-addressed cache
    report/            JSON + Markdown reports
    ml/                ML layers (milestone 8+)
    cli.py             argparse commands
  tests/               unit and integration tests; fixtures under tests/fixtures/
  config/              default gate-config.yaml
  docs/                giml-spec.md and further docs
```

---

## 4. Architecture

### 4.1 Pipeline

```
preflight -> assess gate (tier) -> baseline verify -> generate candidates
   -> plan joint sets -> verify (compile, unit, integration, startup)
   -> on failure: isolate culprit (bisect), demote/pin, retry
   -> commit accepted steps on result branch -> stop -> report
```

### 4.2 Interfaces (design for central deployment later)

Define these in `giml.core` as `typing.Protocol` classes. Implement only the local versions in phase 1.

| Interface | Phase 1 implementation | Later |
|---|---|---|
| `RepoSource` | local git working directory | clone from GitHub/GitLab |
| `BuildRunner` | local Maven invocation, isolated dirs | containerised workers |
| `StateStore` | SQLite | PostgreSQL |
| `ResultCache` | local content-addressed directory | shared cache service |
| `PublishTarget` | writes report + leaves branch | opens/updates PRs |
| `SmokeSettingsProvider` | reads `.redkite/settings.yml` | any other source |
| `RiskScorer` | deterministic heuristic | trained model |
| `AdvisorySource` | local OSV snapshot | shared advisory service |
| `ArtifactMetadataSource` | local Maven Central snapshot | shared |

The engine is **stateless**: input is (project snapshot, state, snapshots, config); output is a plan object serialised as JSON. Everything else is behind the interfaces above.

### 4.3 Determinism and reproducibility

Given the same base commit, the same data snapshots, the same config version, the same JDK and Maven versions, and the same cache contents, the planner must produce the same plan. Randomness (if any, e.g. tie-breaking) uses a seed recorded in the run.

---

## 5. Git handling

### 5.1 Preflight (hard stops)

Refuse to start (exit code 2, clear message) unless:

- The path is inside a git repository with a resolvable `HEAD`.
- `HEAD` is on a branch (detached HEAD is refused unless `--allow-detached` is passed).
- The working tree is clean: no staged changes, no unstaged changes, no untracked files that are not gitignored.
- No merge, rebase, cherry-pick or bisect is in progress.
- No other giml run holds the repository lock (file lock in the tool's state directory).
- Submodules: if present, report "unsupported in phase 1" and stop (confirmed 2026-09-24).
- Git LFS is **allowed** (confirmed 2026-09-24), but giml never downloads LFS content: every git call disables the LFS filter, so LFS files in giml's worktrees are pointer files. A build that needs real LFS content fails, and the report names the LFS paths involved.

Unpushed local commits are **allowed** (confirmed 2026-09-24: "no outstanding commits" means no uncommitted changes). Record the base SHA; the result branch builds on it.

### 5.2 Worktrees and branches

- State directory: `~/.giml/` (configurable). Worktrees under `~/.giml/worktrees/<project>/<run-id>/`.
- **Result branch**: `giml/<base-sha-short>/<UTC-timestamp>`, created from the base commit in its own worktree.
- **Trial worktrees**: throwaway worktrees (or reused scratch directories) for candidate builds. Failed attempts leave no git history.
- **Commits on the result branch**: one commit per *accepted step*, each verified to pass all required stages. The tip is the best verified state; earlier commits are fallback points.
- Commit content: only version edits to POM files (section 8.4), except the synthetic rewind commit of section 5.3. Commit message includes: what changed, CVEs cleared, remaining CVEs, tier, evidence reference (run id).
- Git hooks are **disabled** in tool-owned worktrees (`core.hooksPath` set to an empty directory).
- Cleanup: `giml clean` removes worktrees and (optionally, with `--branches`) result branches. Stale worktrees from crashed runs are detected at startup and reported.

### 5.3 Rewind mode (testing and training)

To create realistic "behind on dependencies" states from a project's own history, `giml plan --rewind-to <commit>` starts the run from the reactor's older `pom.xml` files:

1. Preflight (section 5.1) runs as normal on the developer's repository. `<commit>` must resolve, must be an ancestor of the base commit, and must contain the project's root `pom.xml`; otherwise exit 5.
2. The result branch is named `giml/rewind/<base-sha-short>/<UTC-timestamp>` so it can never be mistaken for a normal result.
3. In the result worktree, every reactor `pom.xml` (section 3, as discovered at the base commit) that also exists at `<commit>` is replaced by its content there; a module `pom.xml` that did not exist yet keeps its base content and is listed in the commit message and the report. All other files stay at the base commit. This is committed as the first commit on the result branch, with a message starting `[giml-rewind]` that names the rewind commit and states it is synthetic.
4. The rewound state is the run's **baseline** (section 9.1). Verification rules (gate config, `.redkite/settings.yml`, tier) still come from the base commit (hard rule 7); only `pom.xml` files are rewound. If build or unit tests fail at the rewound baseline, the run stops with stop reason `rewind_baseline_failed` (the base code does not work with the old POM), and this is recorded as an unusable rewind point.
5. If the rewound `pom.xml` lacks plugins required by the prerequisites (section 3), the run stops with exit 5 and names them.
6. Planning then proceeds forward from the rewound state as in a normal run.
7. The base commit's own `pom.xml` is a known-good reference. The report compares, per dependency: rewound version, giml's result, and the base commit's version, with the CVE exposure of each state.

Rewind mode uses git history only. The developer's working tree is never touched, and nothing is pushed (hard rules 1 and 2).

---

## 6. Tiers and quality gate

Verification is only as strong as the project's tests. The **tier** is a measure of test quality (unit coverage and PIT levels) and nothing else. It decides how much weight results carry and, through the separate `autonomy` mapping, how much autonomy the tool has.

A tier never changes *what* a good upgrade is. The CVE criteria (`cve_minimal` candidates, section 8.2) and the best-update ranking (section 8.5) apply identically to every tier; a higher tier cannot relax them and a lower tier cannot tighten them (confirmed 2026-09-24).

### 6.1 Central configuration (versioned)

`config/gate-config.yaml`:

```yaml
version: 3

tiers:                            # test-quality levels only (unit coverage and PIT)
  A:                              # strongest oracle
    unit_line_coverage: 90
    unit_branch_coverage: 80
    pit_test_strength: 92         # killed / mutants in covered code
    pit_mutation_coverage: 90     # killed / all mutants
    max_excluded_share: 10        # percent of code excluded from metrics
  B:
    unit_line_coverage: 80
    unit_branch_coverage: 70
    pit_test_strength: 85
    pit_mutation_coverage: 80
    max_excluded_share: 15
  # below B: no tier (report outdated/vulnerable only, propose nothing)

autonomy:                         # what giml may do with a verified result, per earned tier
  A: auto_apply                   # future; phase 1 never applies anything itself
  B: suggest_only

verification:                     # stages for every tier; not part of any tier
  integration_tests: when_present # run Failsafe/*IT tests when the project has them (or off)
  startup_check: when_configured  # run the smoke check when settings configure it (or off)

shared:
  touchpoint_thresholds: same_as_tier
  flake_check_runs: 5
  max_result_age_days: 30

planning:
  release_cooldown_days: 7        # ignore artifact versions younger than this
  max_builds: 60
  max_wall_minutes: 120
  objective_profile: cve_first    # see section 8.5
```

All numbers are **placeholders** to be calibrated after the survey run (milestone 3). They are configurable, and projects **choose a tier**, they do not set numbers. Changing a tier's numbers (or the config's structure) bumps `version` and triggers re-evaluation of existing results.

Notes:
- A tier holds only test-quality thresholds (confirmed 2026-09-24). Integration tests and the startup check are verification stages that run at every tier when present or configured (`verification`), and autonomy is a separate mapping from earned tier to action (`autonomy`), so neither is a tier requirement.
- Test strength is always >= mutation coverage for the same run. The validator warns if a tier's strength threshold is not above its mutation coverage threshold.
- Exceptional cases get a **named tier** defined centrally (for example `B-legacy` with an expiry), not per-project overrides.

### 6.2 Project declaration

The project declares its target tier (in `.redkite/settings.yml`, section 12, key `tier`). The **earned tier** is what measurement supports. Behaviour follows the earned tier. The report lists what was missed for the declared tier and how far off each metric was.

### 6.3 Assessment procedure (`giml assess`)

Run on the base commit in a worktree:

1. **Unit line and branch coverage** from JaCoCo (run the project's unit tests with its own JaCoCo configuration; parse `jacoco.xml`).
2. **PIT**: run pitest, parse XML. Compute *both* test strength and mutation coverage from mutant statuses (`KILLED`, `SURVIVED`, `NO_COVERAGE`, `TIMED_OUT`, others per PIT docs). Record the PIT and JaCoCo versions.
3. **Integration tests present**: detect failsafe/`*IT` tests or a declared integration-test profile; record boolean plus the count.
4. **Flake check**: run the full unit suite `flake_check_runs` times; any inconsistent result marks flaky tests and fails the flake criterion.
5. **Excluded share**: compute share of code excluded from coverage/PIT via configuration; fail the tier if above the tier maximum; record the exclusions themselves.
6. **Touchpoints**: for the dependencies under upgrade, find usage sites (classes that import from the dependency's packages) and evaluate the four metrics on that scope only. Tier requirements apply to this scope as well as to the whole project. Evaluated per plan, cached per (commit, dependency).
7. **Startup check availability**: whether the smoke configuration exists and the baseline starts (section 12).

PIT is slow: support scoping runs to touchpoint classes and PIT's incremental history. Full-project assessment is scheduled/cached; per-plan verification uses scoped runs.

### 6.4 Assessment result

Persisted and printed as JSON:

```json
{
  "project": "example-service",
  "declared_tier": "A",
  "passed_tier": "B",
  "config_version": 2,
  "tools": {"pit": "x.y.z", "jacoco": "x.y.z", "jdk": "21"},
  "measured": {
    "unit_line_coverage": 91.2,
    "unit_branch_coverage": 78.0,
    "pit_test_strength": 93.1,
    "pit_mutation_coverage": 88.4
  },
  "integration_tests": true,
  "flaky_tests": [],
  "excluded_share": 6,
  "excluded": ["..."],
  "startup_check": "verified|not_configured|baseline_failed",
  "failed_for_declared": ["unit_branch_coverage", "pit_mutation_coverage"],
  "autonomy": "suggest_only",
  "base_sha": "...",
  "measured_at": "2026-09-24T00:00:00Z",
  "expires": "2026-10-24T00:00:00Z"
}
```

The planner consults this before doing anything. Missing, expired, or below-B results mean **no proposals** (report-only listing of outdated/vulnerable dependencies).

---

## 7. Data sources and sync

`giml sync` is the command that fetches and refreshes the data snapshots. Planning reads only those snapshots (section 7.3).

### 7.1 OSV

- Ingest OSV's bulk data for the **Maven** ecosystem (prefer bulk dumps to per-query calls).
- Store locally as a versioned snapshot: source URL, fetched-at timestamp, content hash.
- Index by `groupId:artifactId` with affected version ranges (`introduced`/`fixed`/`last_affected`), severity (CVSS v3.x vector → numeric score and rating via an in-house calculator; for advisories without a v3.x vector, fall back to the OSV/GHSA severity label; record which source each severity came from), aliases (CVE ids), and modified time.
- Version-range matching uses Maven version ordering.

### 7.2 Maven Central metadata

- For each `groupId:artifactId` the projects depend on (plus their candidates), fetch `maven-metadata.xml` version lists, and per-version release timestamps from the `Last-Modified` header of a HEAD request on each version's `.pom`, since `maven-metadata.xml` carries no per-version dates. Release dates are immutable: carry them forward between snapshots and fetch only versions not seen before. (The Central search API was considered and rejected: its index is stale, missing releases from mid-2025 onward.)
- Store snapshot with timestamps. Release dates power the cooldown and recency scoring.
- Cache BOM/parent POMs needed for resolution in a local repository.

### 7.3 Snapshot discipline

- Planning reads **only** snapshots. Each run records the snapshot ids it used, so results are reproducible and comparable.
- `giml status` shows snapshot age; planning warns when snapshots are older than a configured threshold (default 7 days).

---

## 8. Planning

### 8.1 Inputs

Effective dependency tree (via the dependency plugin's JSON output, section 3, including managed versions and transitive dependencies), the POM structure (where each version is declared), the snapshots, the tier, the config.

### 8.2 Candidate generation

For each dependency version that is *declared or managed* in the project, build an ordered candidate list from Maven Central metadata, excluding versions younger than `release_cooldown_days` and pre-releases (unless the current version is a pre-release):

1. `current` (no change).
2. `cve_minimal`: lowest version greater than current that fixes all known vulnerabilities affecting current (prefer same major).
3. `latest_in_major`: newest release within the current major version.
4. `bom_managed`: the version managed by the project's parent/BOM (e.g. Spring Boot) after any parent upgrade.
5. `latest`: newest overall release.

Parent/BOM upgrades (for example the Spring Boot parent) are first-class candidates; they change many managed versions at once and are treated as a single change unit.

### 8.3 Joint planning algorithm

A **state** maps each upgradable dependency to a chosen version. Start with the aspirational state (best per the objective, section 8.5) and search downward:

1. Build and verify the aspirational state (section 9).
2. If it passes, it becomes the result (subject to commit rules, section 5.2).
3. If it fails, **isolate culprits** with group testing (delta-debugging style): partition the changed dependencies into groups (related dependencies, e.g. same BOM/family, kept together), test subsets, and narrow to the minimal failing change(s). Use the error signature and API-diff evidence (milestone 5) to propose the culprit first, and fall back to bisection.
4. **Demote** each culprit to its next candidate (or pin it at current), and re-verify the reduced state. Repeat until a state passes or budget is exhausted.
5. After a state passes, try to **re-promote** demoted dependencies one at a time (or in small groups) to see whether they now pass in combination. Stop when no untried promotion improves the ranking.
6. Record every attempt (section 15).

Stop conditions (first that occurs): no untried candidate set improves the ranking; `max_builds` or `max_wall_minutes` exhausted (the report states the result may not be optimal); zero known CVEs remain and everything is at its latest permitted version.

### 8.4 Editing POMs

- Edit **only version values** (and add `dependencyManagement` pins when required), at the location where the version is actually declared: direct `<version>`, a property, `dependencyManagement`, or the parent version.
- Use lossless, minimal text edits so formatting and comments are untouched. Never re-serialise the whole POM.
- Multi-module reactors: edit in the module that declares the version (often the reactor parent's `dependencyManagement` or `<properties>`), resolving inheritance within the reactor. If a declaration site cannot be determined inside the reactor (for example it comes only from an external parent or BOM), report "unsupported" for that dependency and leave it unchanged, except where the external parent version itself is the declaration site.
- Every pin/deferral records a **reason** and a **re-evaluation trigger** (new release of the dependency, new advisory, POM change).

### 8.5 Ranking ("best" state)

A candidate state is only eligible if it passes all required verification stages. Eligible states are ranked lexicographically by an **objective profile** (configurable in config):

`cve_first` (default):
1. Lowest CVE exposure over the **fully resolved tree**: maximum severity remaining, then count of vulnerabilities at that severity, then total count (OSV severity data).
2. Smallest total version lag (or most dependencies at newest permitted version).
3. Smallest diff (fewest changed declarations).

Other profiles (`recency_first`, etc.) reorder these. Profiles are data in the config, not code.

The objective profile is independent of the tier: every tier is held to the same CVE and best-update criteria (section 6).

Every held-back or pinned dependency is **re-scanned** so a pin never silently leaves a CVE open; unresolved exposure is reported explicitly with the reason.

### 8.6 Cooldown and safety

Skip artifact versions newer than `release_cooldown_days` (config). Never build a candidate that adds a new *dependency coordinate* not already in the tree, unless it arrives as a normal transitive of an accepted version, and report such additions.

---

## 9. Verification pipeline

Applied to every candidate state, in order; stop at first failure and classify it:

1. **Resolve/compile** (`mvn` via `BuildRunner`).
2. **Unit tests.**
3. **Integration tests** (if configured/present).
4. **Startup verification** (section 12) when configured (`verification.startup_check`), at every tier.
5. **PIT/touchpoint check** scoped to touchpoint classes when required by the tier (may be sampled/cached).

Failure classes (used in logs and ML): `resolution`, `compile`, `enforcer_convergence`, `duplicate_classes`, `unit_test`, `integration_test`, `startup`, `migration`, `timeout`, `infrastructure` (not the candidate's fault; retry/ignore).

Extra signals (recorded as features, not gates, in phase 1): API diff (japicmp) between old and candidate versions for each changed dependency; dependency-tree diff; new WARN/ERROR log lines at startup versus baseline; CVE re-scan result.

### 9.1 Baseline

Before any candidate, run the full pipeline on the **unmodified base commit**. If any required stage fails at baseline, that stage is marked `baseline_failed` and is **not used as an oracle** in the run; the report says so. If build or unit tests fail at baseline, stop: the project is not upgradeable until fixed.

### 9.2 Isolation

Each trial has its own working directory and (via section 12) its own database and port. Maven builds use the developer's own Maven settings and local repository (`~/.m2`), so they resolve from whatever repositories and mirrors the developer has configured, plus Maven's build cache where available. Only the application launched by the startup check (section 12.2) gets its own home and temp directory per trial. Parallel trials are supported up to a configured limit.

---

## 10. Caching

Content-addressed, so stale results cannot be mistaken for current ones.

Cache key for a build outcome = SHA-256 of:
- base tree hash and the normalised set of POM edits (or the resulting POM contents),
- resolved dependency list hash,
- JDK identifier, Maven version, plugin versions,
- gate-config version and the stage name,
- for startup: the smoke settings hash (non-secret parts) and profile name.

Rules:
- A cache **hit** returns the stored outcome plus its original timing; reports show hit/miss.
- Pass results are commit-specific. Known-bad *transitions* (dependency, from → to, error signature) are stored separately as **knowledge** (cross-run, cross-project prior); they influence ordering, never verdicts.
- Persisted per-project **frontier**: last known-good state, deferred pins with reasons and triggers, so later runs only consider what changed since the last run.
- Log cache hit rate and per-stage timing on every run so "later runs are faster" is measured.

---

## 11. State store (SQLite)

Minimum tables (columns indicative; add indices as needed):

- `project(id, path, remote_url_hash, declared_tier, created_at)`
- `snapshot(id, source, fetched_at, content_hash, path)`
- `run(id, project_id, base_sha, branch, started_at, finished_at, config_version, osv_snapshot_id, central_snapshot_id, budget_json, stop_reason, seed, rewind_from_sha)` — `rewind_from_sha` is null unless the run used rewind mode (section 5.3)
- `gate_result(id, project_id, base_sha, config_version, json, measured_at, expires_at)`
- `candidate_state(id, run_id, parent_id, changes_json, rank_json, status)`
- `build_attempt(id, state_id, stage, outcome, failure_class, error_signature, cache_key, cache_hit, duration_ms, log_path)`
- `deferral(id, project_id, coordinate, held_at_version, reason, trigger_json, created_at, resolved_at)`
- `knowledge_transition(id, coordinate, from_version, to_version, failure_class, error_signature, count, last_seen)`
- `example(id, run_id, features_json, label_json, split_group, dedup_hash)` — training examples (section 15)

Schema migrations are versioned and tested.

---

## 12. Startup verification (smoke runner)

Proves the **packaged application** still boots with upgraded dependencies. Modelled on the approach used in the user's RedKite project (random port, verify startup); reuse its approach where suitable (CONFIRM). Note RedKite starts apps with `spring-boot:run` and its settings carry run arguments; giml runs the packaged artifact and its settings carry data only (hard rule 5).

### 12.1 Settings file

`.redkite/settings.yml` (committed in the repo; CONFIRM exact name and location, which may differ from the earlier `.redkite/settings.xml` mentioned):

```yaml
version: 1
tier: B                       # declared target tier
smoke:
  profile: smoke              # required for startup verification: the Spring profile to activate
  env:                        # optional: names with non-secret values or runner placeholders
    APP_DB_NAME: ${TRIAL_DB}
  ready:                      # optional
    http: /actuator/health
    contains: UP
    timeout_seconds: 60
```

Rules:
- Read from the **base commit**.
- Only declarative data: **no command field**, nothing executable. Reject unknown keys that look like commands.
- Missing file or missing `smoke.profile` → startup check `not_configured`: the stage is skipped and the report says so. It does not affect the tier.
- Validate that the profile exists in the project (`application-<profile>.yml|.properties`, or a `@Profile` reference). A profile that activates nothing must fail validation.
- Refuse if `env` contains secret-looking values (heuristics: password/secret/token/key names with literal values). Secrets are supplied from the runner's local, uncommitted environment and never written to reports, logs or commits.

### 12.2 Launch procedure

1. Build the artifact, then run **the packaged artifact** (for Spring Boot, `java -jar`), not an IDE-style classpath.
2. Args: `--spring.profiles.active=<profile>`, bind to `127.0.0.1` only, random free port (pick a free port and retry on bind failure; optionally use port 0 and parse the reported port).
3. Ephemeral working directory and home per trial.
4. Readiness: HTTP check against the declared health path (default `/actuator/health` when Actuator is present), else successful connect plus the "Started ... in N seconds" log line; for non-web apps use clean context startup or exit code 0. Hard timeout (default 60 s).
5. Kill the **entire process tree** in a `finally` path.
6. Capture stdout/stderr to the run log directory (with secret redaction).
7. Compare WARN/ERROR lines against the baseline run; record new ones as features.

### 12.3 Local PostgreSQL

- The smoke profile points at a local PostgreSQL; connection details come from environment variables that the runner injects.
- **Fresh database per trial**: create `giml_<run>_<trial>` before startup, drop it after (in `finally`). Sweep orphaned `giml_*` databases at the start of each run.
- Dedicated least-privilege role, allowed to create/drop only `giml_*` databases; never a shared dev database and never anything holding real data.
- Migrations (Flyway/Liquibase) run as part of startup; failures classify as `migration`.
- Preflight: if PostgreSQL is unreachable, mark startup check `unavailable` for the run; do **not** blame the candidate.
- Server may be a local install or container; the runner only needs host, port and credentials.

### 12.4 Optional smoke requests

Opt-in list of endpoint checks (method, path, expected status) in the settings file. Off by default.

---

## 13. CLI

```
giml sync [--osv] [--central]               fetch/refresh snapshots (network allowed)
giml status                                  snapshot ages, state dir, stale worktrees
giml assess <path> [--declared-tier X]       run gate assessment, print/store result
giml plan <path> [--profile cve_first]       full pipeline; leaves result branch + report
              [--max-builds N] [--max-minutes N] [--allow-detached] [--dry-run]
              [--rewind-to <commit>]         start from pom.xml at <commit> (section 5.3)
giml report <run-id> [--format json|md]      re-render a stored report
giml clean [--all] [--branches]              remove worktrees (and optionally branches)
```

`--dry-run` performs preflight, assessment, and planning without building (lists candidate sets and reasons).

Exit codes: 0 success (plan produced), 1 no improvement found, 2 preflight refusal, 3 ineligible (tier/gate, or unsupported project shape such as a module outside the project directory), 4 infrastructure failure, 5 configuration error.

---

## 14. Report

Written to `~/.giml/reports/<run-id>/report.json` and `report.md`, and summarised on stdout.

Contents:
- Project, base SHA, branch, worktree path, snapshot ids, config version, tier (declared vs earned).
- Result: changed dependencies (from → to) with reasons; held-back dependencies with reason and re-evaluation trigger; remaining CVEs (with severity, and why unresolved).
- Comparison with the **naive baseline** (each dependency independently bumped to latest): builds spent, pass/fail, CVEs cleared.
- Evidence per accepted step: stages passed, cache hits, timings, API-diff summary, new startup warnings.
- Budget used and stop reason (and an explicit note if optimality is not guaranteed).
- Rewind runs (section 5.3): the rewind commit, a clear "synthetic" label, and the per-dependency comparison of rewound, giml result and base-commit versions with CVE exposure.
- Hand-off commands (never executed by the tool):
  - `git diff <base>..<branch>`
  - `git push -u origin <branch>` and open a PR (developer's decision)
  - `giml clean` for cleanup

---

## 15. Outcome logging and dataset

From milestone 4, every attempt is logged as a labelled example:

- **Features**: failure class and normalised error signature, changed coordinates and version jumps (semver distance, days since release, major/minor/patch), BOM membership, API-diff summary, dependency-tree diff summary, project tier and oracle strength, new startup warnings, prior knowledge counts, whether the run is a rewind run (and the rewind commit and its date).
- **Label**: outcome (pass/fail by stage), and for failures the eventual fixing pin or demotion.
- **Normalisation**: strip paths, timestamps, line numbers, ids from error text to form the signature; hash it.
- **Deduplication**: exact dedup on (dependency, from, to, signature); near-duplicate detection (embedding similarity) for forks/vendored copies; multi-module repeat errors collapsed.
- **Splits**: by project group and by time (train on older releases, test on newer). No project or near-duplicate appears on both sides.
- **Label weighting**: by tier/oracle strength; results from projects below the use-gate may be included for *training* at reduced weight, especially where the fix is independently confirmed, but the **use gate** (section 6) alone decides where suggestions are trusted.
- Export to JSONL/Parquet for `ml/`.

---

## 16. Machine learning (milestone 8+, CPU only, no AI API calls)

Only added once logged data exists. Each layer must be judged against the deterministic baseline on **median builds per accepted upgrade** and **false-fix rate**. If a layer does not help, the report says so and it stays off.

Constraints:
- No AI API calls at train or inference time, enforced by a test. Any pretrained weights are downloaded once during setup, stored locally with a checksum, and loaded from disk.
- No frontier/hosted model calls. Do not use a hosted model to generate or label data; the build oracle provides labels.
- PyTorch for neural pieces (the project's learning goal), scikit-learn/gradient boosting for tabular models. Track experiments (MLflow) and version data (DVC) locally.

Layers, in order:
1. **Failure classifier**: TF-IDF + logistic regression baseline, then a small PyTorch model over error signatures.
2. **Retrieval**: local embeddings of error signatures + dependency context; kNN over past failures and their fixes; measure recall@k. CPU inference only.
3. **Candidate ranker / breakage-risk scorer**: MLP or gradient-boosted model over structured features (semver distance, release age, BOM membership, co-occurrence, API diff). Orders candidate states to try likeliest-to-pass first.
4. **Abstention**: calibrated confidence; below threshold → "needs human"; report the false-fix rate as a headline metric.
5. (Optional, later) small encoder fine-tune on CPU. LoRA on large models is out of scope.

Engine integration: the `RiskScorer` interface with `DeterministicRiskScorer` (default) and `ModelRiskScorer` (a trained model loaded from disk in-process). Swappable by config; the planner works with ML dependencies absent.

---

## 17. Milestones and acceptance criteria

Work in order; stop at each checkpoint.

**M1 — Foundations (est. 1–2 weeks)**
- Multi-module skeleton, CI-free local build, `CLAUDE.md`, config loader for `gate-config.yaml` with validation (strength > mutation coverage warning, unknown keys rejected).
- Interfaces from section 4.2 defined; local `StateStore` (SQLite + migrations) and file-based content-addressed `ResultCache`.
- `giml sync` for OSV (Maven bulk) and Maven Central metadata, with snapshots; `giml status`.
- Acceptance: `sync` produces timestamped, hashed snapshots; `status` reads them; unit tests for range matching using Maven version ordering; the tool's own build meets Tier B.

**M2 — Git safety (est. 1 week)**
- Preflight (section 5.1), git wrapper that rejects `push`, worktree/branch lifecycle, locking, `clean`, stale worktree detection, reactor module discovery, rewind-mode worktree setup (section 5.3, steps 1 to 3).
- Acceptance: tests prove a dirty tree is refused, the developer's checkout is untouched after a run, `push` cannot be invoked, hooks are disabled in worktrees, crashed runs are detected, reactor modules are discovered (including in profiles), `--rewind-to` creates the marked rewind commit with only `pom.xml` files changed and rejects non-ancestor commits.

**M3 — Gate assessment (est. 1 week)**
- JaCoCo and PIT collectors (strength and mutation coverage computed from raw statuses), integration-test detection, flake check, excluded share, tier evaluator, `giml assess`.
- **Survey run** across the user's real projects producing a table of the four metrics side by side, to calibrate tier numbers.
- Acceptance: assessment JSON matches section 6.4; survey report produced; thresholds reviewed with the user before locking the config version.

**M4 — Build runner, cache, outcome logging (est. 1 week)**
- `BuildRunner` with isolated dirs, shared repository cache, content-addressed caching with hit/miss and timing metrics, baseline verification (including the rewound baseline and `rewind_baseline_failed`, section 5.3), failure classification, error-signature normalisation, `example` logging.
- Acceptance: repeated identical runs show cache hits; baseline failure is handled per section 9.1; signatures are stable across path/timestamp differences.

**M5 — Deterministic planner (est. 2–3 weeks)**
- Resolution via the dependency plugin's JSON output, candidate generation (section 8.2), lossless POM edits, joint search with delta-debugging isolation and re-promotion, ranking profiles, deferrals with triggers, japicmp-based candidate filtering, dry-run mode, naive-baseline comparison, result-branch commits, report generation including the rewind comparison (section 5.3).
- Acceptance: on at least three real projects (including one deliberately behind on dependencies) the plan is produced with evidence; POM diffs of accepted steps touch only versions; at least one project is run in rewind mode and its report compares giml's result with the base commit's versions; comparison with the naive baseline shows builds, pass rate and CVEs cleared; no CVE is silently left open by a pin.

**M6 — Startup verification (est. 1–2 weeks)**
- Settings loader/validator (section 12.1), smoke runner (12.2), PostgreSQL lifecycle (12.3), baseline handling, integration into the pipeline and tier evaluation.
- Acceptance: a project with a `smoke` profile boots on a random loopback port against a fresh per-trial database; databases and process trees are always cleaned up (including on crash); unreachable PostgreSQL yields `unavailable`, not a candidate failure; secrets never appear in outputs. The tool meets Tier A by the end of this milestone.

**M7 — Hardening and definition of done for phase 1 (est. 1 week)**
- Budget handling, snapshot-age warnings, `docs/` complete, end-to-end run on all initial projects.
- Definition of done: runs end to end with no AI API calls and planning reading only snapshots; each project gets a tier and a report; measured comparison against the naive baseline; cache hit rate and timings recorded across repeated runs.

**M8+ — ML layers (section 16)**
- Each layer is its own checkpoint with an evaluation report against the deterministic baseline (builds per accepted upgrade, false-fix rate, abstention rate, by tier).

---

## 18. Testing strategy for giml itself

- Unit tests for version ordering, OSV range matching, candidate generation, ranking, signature normalisation, POM edit preservation (golden files).
- Integration tests using small fixture Maven projects committed under `tests/fixtures/` covering: clean upgrade, transitive conflict, API-breaking upgrade, startup-only failure, migration failure, unreachable database, dirty repo, detached HEAD, multi-module reactor, LFS pointer files, rewind to an older `pom.xml` (usable and `rewind_baseline_failed`).
- Property tests for the delta-debugging isolation (given a known failing subset, it must find it within a bound).
- No-AI-API test: no AI/LLM client library is a dependency, and planning and ML stages run with connections to known AI API hosts blocked; must succeed.
- Safety tests: `push` rejected; developer checkout unchanged; secrets absent from all artefacts.

---

## 19. Open questions (CONFIRM before or during the relevant milestone)

1. ~~Engine language and libraries.~~ Answered 2026-09-24: all Python; see section 3.
2. ~~What OSV/Maven Central code can be reused?~~ Answered 2026-09-24: RedKite, not Arete; see section 3.
3. ~~Unpushed commits.~~ Answered 2026-09-24: allowed; only uncommitted changes block a run.
4. ~~Submodules/LFS.~~ Answered 2026-09-24: submodules refused; LFS allowed with downloads disabled (revised the same day).
5. Exact name and location of the settings file: `.redkite/settings.yml` vs the earlier `.redkite/settings.xml`; and which keys it should carry. (M6)
6. Initial project set: which repositories, and which is deliberately behind on dependencies. (M3/M5)
7. Initial tier numbers are placeholders; lock after the M3 survey (config version 3 since 2026-09-24). (M3)
8. Metric definitions: gate on both PIT test strength and mutation coverage as specified; confirm which of the existing "80%" figures in current policy refers to which. (M3)
9. Which local PostgreSQL setup (install or container) and role provisioning. (M6)
10. ~~Source of per-version release dates.~~ Answered 2026-09-24: `Last-Modified` of each version's `.pom` (search API index found stale); see section 7.2.
11. ~~CVSS scoring.~~ Answered 2026-09-24: in-house CVSS v3.x calculator, OSV/GHSA label fallback, score source recorded; see section 7.1.
