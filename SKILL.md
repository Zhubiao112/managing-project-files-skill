---
name: managing-project-files
description: Use when a project has scattered reports or figures, mixed agent logs and intermediate files, unclear current-versus-obsolete outputs, poor reviewability, or storage pressure requiring safe cleanup without losing raw data or provenance.
---

# Managing Project Files

## Overview

Build a small, stable human-facing `deliverables/` layer while preserving canonical technical paths and scientific provenance. Treat cleanup as an evidence-and-authority decision, never a filename or age heuristic.

## Non-negotiable rules

1. Default to an additive curated hub; do not bulk-move or mirror every readable artifact.
2. Preserve raw data, active uploads, dirty user work, unique results, and unresolved provenance.
3. Never treat `old`, `tmp`, `backup`, untracked state, age, or “not open” as deletion proof.
4. Do not replace clutter with `.ops`, quarantine, hard-link, checksum, or log-archive clutter.
5. Before any deletion, compression, deduplication, or large move, **MUST read** [references/cleanup-safety.md](references/cleanup-safety.md).
6. When creating the hub or manifest, **MUST read** [references/contracts.md](references/contracts.md).

## Workflow

Copy this checklist and track it:

```text
Project file governance:
- [ ] Read project rules and authoritative docs
- [ ] Inspect Git state and path references
- [ ] Measure file counts and storage drivers
- [ ] Select current user-facing artifacts
- [ ] Create/update the curated deliverables hub
- [ ] Classify cleanup candidates by evidence tier
- [ ] Execute only authorized exact-path cleanup
- [ ] Validate links, manifest, project checks, and reclaimed space
```

### 1. Read before designing

Inspect available `AGENTS.md`, `README*`, project log, runbook, `docs/`, scripts, manifests, `.gitignore`, and Git status. Use project evidence instead of imposing a generic hierarchy. Search references to report/result paths before proposing moves.

### 2. Audit without heavy local work

Measure top-level sizes, relevant file counts, storage drivers, and tracked/untracked/ignored state. Use `rg` for discovery. Avoid whole-project hashing or compression; route heavy I/O to the project-approved compute environment.

### 3. Curate, do not mirror

Select a small current set using authoritative pointers, validated completion state, audience relevance, and evidence boundaries. Modification time is only a tie-breaker inside one output family. Keep `current`, `limited`, `pending`, `superseded`, `archive`, and explicit `MISSING` distinct.

Do not impose an arbitrary artifact-count cap. “Curated” means every promoted item has a distinct review purpose and source mapping; include all necessary current items, but never mirror a directory merely because its files are readable.

Prefer navigation wrappers and lightweight copies of selected final assets. Keep canonical sources in place. Avoid hard links and absolute symlinks: they couple edits or reduce portability.

### 4. Classify cleanup candidates

Apply the safety reference's three tiers: safe cache, reconstructible intermediate, and high-risk/protected. Logs, staging, archives, processed duplicates, databases, environments, trajectories, and scientific run trees are never “safe cache.”

### 5. Validate and report

Run the bundled validator from this skill directory:

```bash
python3 scripts/validate_deliverables.py --project-root /path/to/project
```

Use this default validator for new hubs. If a project already has an equivalent schema and project-native validator, preserve it and verify that it enforces the same source/status/link/runtime-file invariants. Do not copy the bundled script into every project unless the user asks to integrate it.

Run project-native checks, `git diff --check`, scoped Git status, link/source-copy checks, and before/after measurements. Report exact deletions, bytes reclaimed, protected paths, and deferred candidates. Do not stage, commit, or push unless requested.

## Quick reference

| Symptom | Required response |
|---|---|
| Reports scattered | Curate one entry and manifest; preserve canonical paths. |
| Hundreds of readable outputs | Select by review purpose; do not mirror all or impose a numeric cap. |
| Disk nearly full | Measure first; do not use urgency as deletion evidence. |
| Many HPC logs | Preserve until job/provenance policy is verified; avoid heavy local compression. |
| Duplicate-looking results | Require identity, a named canonical survivor, and rebuild/recovery proof. |
| Dirty Git tree | Preserve user changes; no stash/reset/cleanup or unrelated staging. |
| Active upload/staging | Protect until activity and recovery are verified. |

## Red flags

Stop destructive work when considering:

- mirroring every readable artifact or enforcing an invented item cap;
- deleting by name, age, ignored state, or no-open-handle alone;
- hashing/compressing large trees locally or on an HPC login node;
- moving duplicate-looking files or deleting mixed-eligibility directories;
- treating dirty, `pending`, handoff, or `MISSING` assets as obsolete.

When any red flag appears, return to audit and produce a candidate list instead of deleting.

## Common mistakes

- Creating another mirror instead of a curated review surface.
- Selecting “latest” without completion evidence or scanning references.
- Omitting canonical source/status fields from the manifest.
- Reporting savings or completion without fresh validation.
