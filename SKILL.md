---
name: managing-project-files
description: Use when a project has scattered or recurring reports and figures, accumulating logs, caches, or intermediates, deliverables drift, unclear current outputs, task-end or scheduled maintenance needs, or storage pressure requiring safe cleanup without losing raw data or provenance.
---

# Managing Project Files

## Overview

Maintain a small human-facing `deliverables/` layer while preserving canonical technical paths and provenance. Treat promotion as explicit policy and cleanup as an evidence-and-authority decision.

## Non-negotiable rules

1. Keep canonical sources in place; never bulk-move or mirror readable directories.
2. Preserve raw data, active uploads, dirty user work, unique results, and unresolved provenance.
3. Never use names, age, ignored/untracked state, or “not open” as deletion proof.
4. Do not create watcher, quarantine, checksum, receipt, log-archive, or sidecar-file clutter.
5. Do not invent retention periods, disk thresholds, artifact patterns, or completion markers.
6. In semi-automatic mode, automatic deletion is limited to bounded, untracked, non-dirty, unprotected `.DS_Store` and `*.pyc`; oversized strict caches are review-only, and `__pycache__` is removed only when empty.
7. Before any broader deletion, compression, deduplication, or large move, **MUST read** [references/cleanup-safety.md](references/cleanup-safety.md).
8. Before creating or continuously maintaining a hub, **MUST read** [references/contracts.md](references/contracts.md).

## Workflow

```text
Project file governance:
- [ ] Read project rules and authoritative docs
- [ ] Inspect Git state and path references
- [ ] Choose one-time audit or opt-in continuous management
- [ ] Measure file counts and storage drivers
- [ ] Curate current user-facing artifacts
- [ ] Classify cleanup candidates
- [ ] Execute only policy-authorized exact actions
- [ ] Validate links, manifest, protected paths, and reclaimed bytes
```

### One-time audit

Read available `AGENTS.md`, `README*`, project logs, runbooks, `docs/`, scripts, manifests, `.gitignore`, and Git status. Measure first; avoid whole-project hashing or compression. Select artifacts using authoritative pointers, validated completion state, audience relevance, and explicit evidence boundaries. Keep `current`, `limited`, `pending`, `superseded`, `archive`, and `MISSING` distinct.

Prefer navigation wrappers and bounded lightweight copies. Use modification time only as a tie-breaker inside one output family. Apply the safety reference’s cache, review-required, and protected classifications. If deletion authority is incomplete, return a candidate table.

### Continuous management

For recurring drift, task-end maintenance, scheduled audits, or automatic collection of new outputs, **MUST read** [references/continuous-management.md](references/continuous-management.md). Use the bundled standard-library manager:

```bash
python3 scripts/manage_project_files.py init --project-root /path/to/project --mode audit
python3 scripts/manage_project_files.py scan --project-root /path/to/project
python3 scripts/manage_project_files.py maintain --project-root /path/to/project
python3 scripts/manage_project_files.py status --project-root /path/to/project
```

`init --mode semi-auto` is opt-in per project. Configure explicit artifact rules before expecting promotion. `scan` writes a plan and candidate/status surfaces but never promotes or deletes. `maintain` applies promotions and strict safe-cache deletion only when policy mode is `semi-auto`. Never install an always-on watcher by default.

Promotion is no-clobber. Bounded copies must retain their scan-time SHA-256 through publication; changed generated content receives a deterministic versioned destination instead of replacing an existing file. Pending intent allows exact recovery after interruption. Existing governance files are identity/content checked through pinned file descriptors.

Before strict-cache deletion, isolate and keep open the exact inode within a pinned parent directory, then recheck current policy, Git state, protection rules, and post-unlink parent identity; restore on mismatch. Governance rewrites require atomic rename-exchange CAS plus a two-file recovery journal. Initialization and lock cleanup use pinned `.codex` directory descriptors and must fail closed on concurrent edits.

Execute the bundled scripts from the installed skill. Do not vendor copies into each project unless the user explicitly requests a pinned project integration. Do not invent a schedule cadence; reuse an existing project cadence or obtain approval before creating an automation.

### Validate

Run:

```bash
python3 scripts/validate_deliverables.py --project-root /path/to/project
```

Then run project-native checks, `git diff --check`, scoped Git status, source/copy checks, and before/after measurements. Do not stage, commit, push, or install a scheduler unless requested.

## Quick reference

| Symptom | Required response |
|---|---|
| New outputs keep appearing | Use explicit rules plus task-end/periodic `maintain`; do not guess. |
| File may still be written | Defer until the stability gate passes. |
| Duplicate basenames | Use canonical source identity and deterministic destinations. |
| Many logs/intermediates | List for review; never auto-delete or auto-compress. |
| Strict cache found | Auto-delete only in per-project semi-auto mode after Git/protection checks. |
| Dirty governance file or active lock | Do not overwrite it; stop and report the conflict. |
| Large HPC tree | Scope configured roots; route heavy work to approved compute nodes. |

## Red flags

Stop automatic work when considering a daemon, arbitrary retention, broad “reconstructible” deletion, login-node compression, canonical-path moves, or overwriting dirty governance files. Return to scan/audit mode.

## Common mistakes

- Initializing semi-auto without project-specific artifact and protected-path rules.
- Treating a filesystem event or job exit as completion evidence.
- Copying large reports instead of creating navigation wrappers.
- Confusing `superseded` with deletion authority.
- Accumulating maintenance logs instead of maintaining one current status and candidate table.
- Copying manager scripts into every project or inventing a polling interval without approval.
