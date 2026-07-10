# Cleanup safety contract

## Contents

- Evidence hierarchy
- Rationalizations to reject
- Risk matrix
- Insufficient deletion evidence
- Exact deletion gate
- Continuous-automation boundary
- Logs and HPC-heavy operations
- Active staging and dirty worktrees
- Verification contract

## Evidence hierarchy

Use the strongest available evidence:

1. Project rules and explicit user scope.
2. Current manifests, project logs, runbooks, job records, and maintained indexes.
3. Generator scripts, exact inputs/config/environment, validated successor outputs, and inbound-reference scans.
4. Remote/archive recovery proof and checksums when required.
5. Names, timestamps, extensions, ignored state, and directory labels only as discovery hints.

Never promote level 5 hints into deletion authority.

## Rationalizations to reject

| Rationalization | Reality |
|---|---|
| “Hard links are zero-copy.” | They still mirror noise and couple edits to canonical files. |
| “Compression keeps the data.” | It mutates paths/formats, consumes I/O, and may break consumers. |
| “It is untracked and not open.” | That does not prove rebuildability or downstream irrelevance. |
| “It is older than 30 days.” | Age is not scientific or operational obsolescence. |
| “Quarantine is reversible.” | Moving can break paths and consumes the same filesystem. |
| “Management authorized reproducible files.” | Demonstrate reproducibility and exact deletion authority first. |
| “Curated means at most 3–6 files.” | Curate by distinct review purpose, not an invented limit. |

## Risk matrix

| Class | Examples | Default action |
|---|---|---|
| Safe cache | `.DS_Store`, `*.pyc`, empty `__pycache__`, tool cache proven disposable | Continuous auto-delete is limited to the first three; other tool caches need exact authorization. |
| Reconstructible intermediate | raw/local render layers, derived scratch tables, failed-run fragments | Delete only after the exact gate below is satisfied. |
| High risk | non-empty logs, processed duplicates, migration archives, staging, databases, environments | List; require project-specific provenance and explicit approval. |
| Protected | raw experimental data, unique trajectories/poses, active uploads, dirty user files, source evidence, unresolved handoffs | Do not delete or relocate. |

Generic directories named `cache`, `backup`, `old`, `tmp`, or `scratch` may contain expensive databases, environments, active staging, or unique results. Inspect contents and provenance before classification.

## Insufficient deletion evidence

None of these alone is sufficient:

- old modification time;
- untracked or ignored by Git;
- not currently open by a process;
- directory name suggests temporary content;
- another file has a similar name;
- the file can be compressed;
- a quarantine move is reversible;
- an authority figure says “anything reproducible” without proof;
- exact duplicate bytes without a named canonical survivor and recovery/provenance decision.

## Exact deletion gate

Before deleting any reconstructible intermediate, require all applicable checks:

- exact candidate path list;
- path containment under the confirmed project root;
- every child eligible when deleting a directory;
- Git tracked/dirty state reviewed;
- no active job/upload/process dependency;
- inbound references reviewed;
- exact generator/workflow available;
- complete inputs, configuration, and required environment available;
- validated successor/final output exists;
- candidate is not unique provenance or a required failure diagnostic;
- size measured before deletion;
- user already authorized this exact path/category, otherwise request approval.

After deletion, re-run project checks, source/link checks, Git status, and size measurement. Record exact paths and bytes reclaimed.

## Continuous-automation boundary

Semi-automatic maintenance may delete only unchanged, untracked, non-dirty, non-symlink, unprotected `.DS_Store` and `*.pyc` files, followed by an empty `__pycache__` directory. This is a closed hard-coded set, not an example list.

Everything else is review-only. A policy glob may discover logs or intermediates, but it cannot turn them into automatic deletion targets. “Reconstructible,” “older than the retention period,” “disk above a threshold,” “job completed,” or “management wants it automated” do not expand authority.

Do not install a watcher or background daemon as a shortcut. Prefer task-end reconciliation plus a project-scoped periodic audit. Do not invent retention windows, disk thresholds, or log-compression jobs without current project evidence, explicit authorization, and the approved compute environment.

## Logs and HPC-heavy operations

Treat logs as run evidence until the project records job identity, exit state, errors, resource use, outputs, and recovery policy. Empty logs may still document successful absence of errors.

Do not compress, hash, archive, or deduplicate hundreds of logs or TB-scale trees locally merely because disk pressure is high. Follow the project's compute policy. On HPC systems, never run large checksum/compression/dedup jobs on the login node; use a compute-node job after estimating I/O, time, memory, and output size.

Compression is a path and format mutation, not a harmless read-only action. Confirm downstream consumers and retention rules first.

## Active staging and dirty worktrees

- Protect active upload/submission staging until process state and reconstruction are verified.
- Do not use `git reset`, `git clean`, stash, or unrelated commits to simplify cleanup.
- Preserve user changes and distinguish them from newly created governance artifacts.
- Do not move canonical paths during an emergency review window.

## Verification contract

At minimum verify:

```text
- deliverables manifest valid
- Markdown/local links resolve
- promoted lightweight copies match canonical sources
- forbidden runtime artifacts absent from deliverables
- project-native tests/checks pass
- git diff --check passes
- raw/protected paths unchanged
- removed paths absent and retained successors present
- before/after counts and bytes recorded
```
