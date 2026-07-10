# Continuous management contract

## Contents

- Purpose and boundaries
- Project surfaces
- Modes and command lifecycle
- Policy schema
- Promotion rules
- Cleanup rules
- Stability, concurrency, and failure behavior
- Task-end and scheduled integration
- Acceptance checks

## Purpose and boundaries

Continuous management is opt-in reconciliation, not a filesystem daemon. Run it after a task produces user-facing artifacts and periodically as a fallback. It must converge when run repeatedly, preserve canonical sources, and make no semantic guesses without project policy.

The bundled manager uses only the Python standard library. It performs metadata scans only within configured roots. SHA-256 is limited to bounded `copy` sources and strict-cache candidates already eligible for automatic deletion; oversized caches are review-only and are not hashed.

Do not use this manager for a project that has a different established manifest schema until an adapter or equivalent project-native manager is verified.

## Project surfaces

Keep machine state separate from human outputs:

```text
.codex/
├── .gitignore                    # ignores runtime state, not policy
├── project-files-policy.json     # versioned project decisions
├── project-files-state.json      # untracked observations, pending intent, applied state
├── project-files-plan.json       # untracked current plan
├── project-files.lock            # transient scan/apply operation lock
└── project-files-maintain.lock   # transient whole-maintain lock

deliverables/
├── README.md                     # managed current-artifact block plus user prose
├── MANIFEST.csv                  # canonical source mapping
├── MAINTENANCE_STATUS.md         # one current status, never a log series
├── CLEANUP_CANDIDATES.csv        # current review surface
├── reports/
├── figures/
├── tables/
└── archive/
```

Never add event streams, receipt directories, SQLite databases, per-file sidecars, quarantine trees, or accumulated maintenance logs by default.

## Modes and command lifecycle

Initialize audit mode when permission is not explicit:

```bash
python3 scripts/manage_project_files.py init --project-root /path/to/project --mode audit
```

Use semi-auto only after the user authorizes that mode for the project:

```bash
python3 scripts/manage_project_files.py init --project-root /path/to/project --mode semi-auto
```

If policy already exists, `init` preserves it. Change its `mode` deliberately instead of expecting re-initialization to overwrite decisions.

Commands:

| Command | Mutation boundary |
|---|---|
| `init` | Under the operation lock, creates missing policy, state, hub, and runtime ignore entries with no-clobber publication; preserves existing policy and concurrent user README edits. |
| `scan` | Updates observations, current plan, candidate CSV, and current status only. Never promotes or deletes. |
| `apply` | Applies an already-scanned plan after revalidating policy, root, fingerprints, Git state, and lock. |
| `maintain` | Runs `scan`; in audit mode stops there, in semi-auto mode applies permitted actions. |
| `status` | Reads current policy/state/plan counts without scanning. |

## Policy schema

The policy is explicit JSON so the manager needs no YAML dependency. Start with narrow roots and rules:

```json
{
  "version": 1,
  "mode": "semi-auto",
  "deliverables_dir": "deliverables",
  "stability_seconds": 60,
  "max_copy_bytes": 20971520,
  "max_cache_delete_bytes": 20971520,
  "cleanup_roots": ["reports", "results", "scratch"],
  "ignored_globs": [".git/**", ".codex/**", "deliverables/**"],
  "protected_globs": [
    "raw/**",
    "raw_data/**",
    "data/raw/**",
    "**/staging/**",
    "**/uploads/**"
  ],
  "review_candidate_globs": [
    "**/*.log",
    "**/*.out",
    "**/*.err",
    "**/*.tmp",
    "**/*.bak"
  ],
  "artifact_rules": [
    {
      "name": "final-reports",
      "category": "report",
      "include": ["reports/final/**/*.md", "reports/final/**/*.pdf"],
      "exclude": ["reports/final/drafts/**"],
      "destination": "reports",
      "promotion": "wrapper",
      "status": "current"
    },
    {
      "name": "final-figures",
      "category": "figure",
      "include": ["results/figures/final/*.svg"],
      "exclude": [],
      "destination": "figures",
      "promotion": "copy",
      "status": "current"
    }
  ]
}
```

`stability_seconds` is a write-settling gate, not a retention or deletion rule. A file is stable when its modification time is older than the gate or unchanged observations span the gate. Set it from the producer’s write behavior; do not use it to infer scientific completion.

The generated default is an empty `cleanup_roots` list, so no cleanup tree is scanned until the project policy names one. Configure narrow roots for large or HPC-backed projects; never use `.` as a shortcut for a multi-terabyte run tree or scan heavy storage from a login node.

## Promotion rules

Promotion requires every condition:

- source matches an explicit artifact rule;
- source is a regular non-symlink file inside the project;
- source is outside ignored and protected paths;
- stability gate passes;
- policy status and promotion mode are valid;
- `copy` source is within `max_copy_bytes`;
- destination does not belong to another canonical source;
- source fingerprint still matches at apply time;
- bounded `copy` sources still match their scan-time SHA-256 digest;
- `MANIFEST.csv` and `README.md` are not dirty tracked files.

Use `wrapper` for reports or large assets: it creates a small Markdown navigation artifact linking to the canonical source. Use `copy` only for bounded, final, portable figures or tables; the manager verifies source identity and SHA-256 while copying from one pinned file descriptor. Duplicate basenames receive deterministic source-derived suffixes. Links are relative to the actual hub location and percent-encode URI delimiters such as spaces, `#`, `?`, and parentheses.

Reusing a managed destination requires compatibility with the current policy destination and promotion suffix. Switching between `copy` and `wrapper`, or changing the policy destination directory, recomputes the base destination before content versioning.

New artifacts are published with atomic no-clobber creation through securely opened directory descriptors. The manager never replaces an existing deliverable path. If the desired generated content changes, it selects a deterministic content-versioned path and updates the manifest; prior generated files remain untouched and require explicit review before removal.

The manager upserts rows by canonical `source_path` and preserves synthesized/manual rows whose source is blank. Repeated maintenance is idempotent.

## Cleanup rules

Semi-auto deletion is hard-coded to:

- `.DS_Store` files;
- `*.pyc` files;
- an empty `__pycache__` directory after eligible files are removed.

Eligible `.DS_Store`/`*.pyc` files must also be no larger than `max_cache_delete_bytes`. Larger strict caches are listed as `review_required`; they are neither hashed nor automatically deleted.

Every automatic candidate must be a regular non-symlink file, contained under the project root, unchanged since scan, untracked, non-dirty, outside ignored paths, and outside protected paths. At deletion time the manager pins the parent directory, atomically renames the candidate to a private tombstone, rechecks inode identity, current policy, Git tracked/dirty state, and protection rules, then deletes the isolated inode. A changed or newly protected file is restored without overwriting a concurrent path.

Policy `review_candidate_globs` never grants deletion authority. Logs, stdout/stderr, temporary tables, archives, duplicates, databases, environments, trajectories, checkpoints, staging, uploads, and generic `cache`/`tmp` directories go only to `CLEANUP_CANDIDATES.csv`. Use the cleanup safety contract and exact user authorization for any broader action.

## Stability, concurrency, and failure behavior

- `scan` and `apply` use an operation lock; `maintain` also holds a separate lock across its complete scan/apply cycle. Concurrent runs fail closed.
- New deliverables use complete temporary files plus atomic no-clobber publication; concurrent files are never replaced.
- Existing `MANIFEST.csv` and `README.md` use an atomic rename-exchange compare-and-swap: desired text is fully synced to a temporary file, atomically exchanged, and the displaced inode/digest is verified before commit. Concurrent edits or parent moves trigger an atomic rollback. Platforms without the required macOS/Linux rename-exchange primitive fail closed.
- A governance journal stores the complete old and desired manifest/README text before either CAS. The next scan completes a verifiable two-file interruption and preserves manual manifest rows.
- `apply` re-derives actions from current policy and project state, checks managed-destination ownership, and preflights every promotion and deletion fingerprint before writing the first deliverable.
- Strict-cache identity, policy, and Git state are checked after atomic isolation and immediately before unlinking the isolated inode. The inode stays open through unlink; a post-unlink parent-identity failure recreates the file before aborting.
- Promotion intent is persisted before publication. A retry can adopt an exact pending artifact, repair manifest drift, and converge after an interrupted run.
- Lock files are created and removed relative to a pinned `.codex` directory descriptor; path swaps cannot redirect lock writes or cleanup.
- Policy/root mismatch, path traversal, stale source, copy-size excess, dirty governance files, or unsafe destinations stop the batch.
- A failed batch never expands cleanup scope or falls back to filename/age heuristics.
- Do not break a lock based on age alone. Confirm no live maintenance process before removing a genuinely stale lock.

## Task-end and scheduled integration

After project-specific rules are validated, add a short project instruction with user approval:

```text
When a task creates or changes a final user-facing report, figure, or summary table,
run $managing-project-files continuous maintenance for this project before completion.
If maintenance fails or defers an artifact, report the reason; do not broaden cleanup.
```

Use task-end maintenance as the primary trigger. Use a project-scoped Codex automation as a periodic fallback when the user requests scheduling. The automation should run `maintain`, report proposed/promoted/deferred/review counts, and never reinterpret review candidates as deletable.

Run the manager from the installed skill directory. Do not copy or fork the scripts into every project unless the user explicitly requests a pinned project-local integration and accepts the update burden. Do not invent a five-minute, hourly, daily, or other cadence: reuse a documented project cadence or ask the user to approve one.

Do not install `launchd`, cron, watchdog, or an HPC login-node daemon by default. Filesystem events and scheduler exit states are notifications, not proof of completed artifacts.

## Acceptance checks

Verify all of the following:

```text
- no policy means no scan/apply
- audit mode never promotes or deletes
- scan never promotes or deletes
- semi-auto promotes only explicit stable rules
- automatic deletion touches only strict safe-cache files
- protected, tracked, dirty, symlink, and changed paths remain untouched
- duplicate basenames cannot overwrite each other
- canonical sources never move
- manual manifest rows and README prose survive
- repeated maintain runs do not duplicate artifacts
- concurrent or stale-plan application fails closed
- interrupted publication converges from pending state without overwriting or duplicating the planned destination
- interrupted manifest/README rewrites recover from the governance journal while retaining manual rows
- final cache deletion rechecks Git/policy and cannot follow a swapped parent directory
- edits arriving after governance preflight are preserved by CAS rollback
- `copy`/`wrapper` policy switches keep content and file extensions compatible
- deliverables validator and project-native checks pass
- current status and candidate table replace accumulated maintenance logs
```
