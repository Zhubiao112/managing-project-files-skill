# Deliverables and cleanup contracts

## Contents

- Deliverables directory contract
- Manifest schema
- Artifact selection contract
- Current review report contract
- Cleanup candidate table
- Continuous-management surfaces
- Example application contract

## Deliverables directory contract

Use this default unless the project already has an equivalent clearly designated user-facing surface:

```text
deliverables/
├── README.md                  # sole review entry
├── MANIFEST.csv              # status and source mapping
├── MAINTENANCE_STATUS.md      # one current reconciliation status
├── CLEANUP_CANDIDATES.csv     # current review-only cleanup surface
├── reports/                   # current readable reports and navigation wrappers
├── figures/                   # selected final figures only
├── tables/                    # small decision/index/claim-support tables
└── archive/                   # superseded but still useful user-facing artifacts
```

Do not store logs, raw data, trajectories, checkpoints, caches, databases, environments, source render layers, or large run trees here.

## Manifest schema

Required columns:

```text
id,category,title,date,status,deliverable_path,source_path,notes
```

This is the default for a new hub. Preserve an existing equivalent project schema when it already records the same identity, category, title, date, status, deliverable path, canonical source, and notes semantics and has a working validator.

The bundled continuous manager writes this exact default schema. Keep a project with an equivalent but different schema in audit mode until an adapter or project-native manager is verified; do not silently rewrite its manifest.

Rules:

- `id`: unique stable identifier such as `R001`, `F001`, `T001`.
- `category`: `report`, `figure`, `table`, or another small user-facing type.
- `title`: human-readable title.
- `date`: ISO `YYYY-MM-DD` when known.
- `status`: exactly `current`, `limited`, `pending`, `superseded`, or `archive`.
- `deliverable_path`: project-relative path inside `deliverables/`.
- `source_path`: canonical project-relative source when the item is promoted; blank only for a newly synthesized navigation/report artifact.
- `notes`: short evidence boundary, audience, or promotion reason.

`superseded` does not authorize deletion. `pending` must remain visibly pending. Preserve `MISSING` inside reports and inventories rather than manufacturing a manifest row for a nonexistent output.

## Artifact selection contract

Select artifacts in this order:

1. Explicit current pointers in README, project log, manuscript, runbook, or maintained index.
2. Validated final outputs referenced by current reports or workflows.
3. The newest completed output inside the same analysis family when no authoritative pointer exists.
4. User-confirmed artifacts.

Do not set arbitrary limits such as “five reports” or “one figure per conclusion.” Each promoted item must have a distinct review purpose; the resulting set may be larger when the project genuinely has multiple current workstreams.

Exclude from promotion:

- failed/incomplete runs without a useful failure report;
- handoff inputs presented as completed evidence;
- raw/local/overview renders when a validated composite exists;
- frame/run-level long tables when a summary/index table is sufficient;
- every file selected only because its extension is readable.

## Current review report contract

Use this compact shape:

```markdown
# Current project review

## One-page outcome
Current supported conclusion or project state.

## Evidence/status table
Current, limited, pending, MISSING, and cannot-claim boundaries.

## Read first
A short, annotated list of authoritative reports/assets, sized to the project's actual current workstreams.

## Safe statements / current decisions
Evidence-backed statements only.

## Unsupported or pending statements
Explicit limitations and missing evidence.

## Cleanup summary
Deleted paths/categories, bytes reclaimed, protected paths, deferred candidates.
```

## Cleanup candidate table

Use a small CSV with:

```text
id,path,size_bytes,size_human,risk,status,recommendation,evidence_required,reason
```

Recommended statuses: `safe_cache`, `review_required`, `keep`, `protected`, `deleted`.

## Continuous-management surfaces

Keep versioned decisions and untracked machine state under `.codex/`, not in the human-facing hub:

```text
.codex/project-files-policy.json   # track when project policy belongs in Git
.codex/project-files-state.json    # ignored observations/pending/applied state
.codex/project-files-plan.json     # ignored current plan
.codex/project-files.lock          # ignored scan/apply operation lock
.codex/project-files-maintain.lock # ignored whole-maintain lock
```

Maintain one `MAINTENANCE_STATUS.md` and one `CLEANUP_CANDIDATES.csv`. Do not accumulate dated scan logs, event streams, receipts, per-file metadata, or quarantine directories.

## Example application contract

Given a project with many reports, 1,000 logs, large raw data, a dirty Git tree, and broken reviewability, a compliant result has:

- one `deliverables/README.md` entry;
- one concise current review rather than a mirror of all reports;
- a manifest mapping promoted lightweight artifacts to canonical sources;
- no logs, raw data, or large runtime files under `deliverables/`;
- automatic deletion limited to strict safe-cache files; reconstructible intermediates remain review-only until exact authorization and the safety gate are satisfied;
- a candidate table for logs, archives, duplicate-looking results, and raw/run trees;
- existing technical paths and unrelated Git changes preserved;
- validator, project checks, and before/after measurements recorded.
