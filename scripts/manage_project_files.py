#!/usr/bin/env python3
"""Maintain a curated project deliverables hub from an explicit policy."""

from __future__ import annotations

import argparse
import csv
import ctypes
import fcntl
import fnmatch
import hashlib
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote


MANIFEST_FIELDS = (
    "id",
    "category",
    "title",
    "date",
    "status",
    "deliverable_path",
    "source_path",
    "notes",
)
CANDIDATE_FIELDS = (
    "id",
    "path",
    "size_bytes",
    "size_human",
    "risk",
    "status",
    "recommendation",
    "evidence_required",
    "reason",
)
ALLOWED_STATUSES = {"current", "limited", "pending", "superseded", "archive"}
ALLOWED_MODES = {"audit", "semi-auto"}
ALLOWED_PROMOTIONS = {"wrapper", "copy"}
MANAGED_START = "<!-- project-files:managed-start -->"
MANAGED_END = "<!-- project-files:managed-end -->"
POLICY_RELATIVE = Path(".codex/project-files-policy.json")
STATE_RELATIVE = Path(".codex/project-files-state.json")
PLAN_RELATIVE = Path(".codex/project-files-plan.json")
LOCK_RELATIVE = Path(".codex/project-files.lock")
MAINTAIN_LOCK_RELATIVE = Path(".codex/project-files-maintain.lock")


class GovernanceError(RuntimeError):
    """Raised when governance cannot continue without risking project state."""


def default_policy(mode: str = "audit") -> Dict[str, object]:
    if mode not in ALLOWED_MODES:
        raise GovernanceError(f"Unsupported mode: {mode}")
    return {
        "version": 1,
        "mode": mode,
        "deliverables_dir": "deliverables",
        "stability_seconds": 60,
        "max_copy_bytes": 20 * 1024 * 1024,
        "max_cache_delete_bytes": 20 * 1024 * 1024,
        "cleanup_roots": [],
        "ignored_globs": [".git/**", ".codex/**", "deliverables/**"],
        "protected_globs": [
            "raw/**",
            "raw_data/**",
            "data/raw/**",
            "**/staging/**",
            "**/uploads/**",
        ],
        "review_candidate_globs": [
            "**/*.log",
            "**/*.out",
            "**/*.err",
            "**/*.tmp",
            "**/*.bak",
        ],
        "artifact_rules": [],
    }


def _timestamp(now: Optional[float] = None) -> str:
    value = time.time() if now is None else now
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _replacement_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return 0o644


def _atomic_write_text(path: Path, text: str) -> None:
    absolute = path.absolute()
    parent_parts = Path(*absolute.parent.parts[1:])
    data = text.encode("utf-8")
    target_mode = _replacement_mode(absolute)
    temporary_name = f".project-files-{secrets.token_hex(12)}.tmp"
    with _open_directory_chain(Path(absolute.anchor), parent_parts) as directory_fd:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(
            temporary_name, flags, target_mode, dir_fd=directory_fd
        )
        try:
            _write_all(descriptor, data)
            os.fchmod(descriptor, target_mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(
                temporary_name,
                absolute.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _csv_text(fields: Sequence[str], rows: Iterable[Dict[str, object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_json(path: Path, value: object) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path, label: str) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GovernanceError(f"Missing {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GovernanceError(f"Invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"{label} must be a JSON object: {path}")
    return value


def _safe_relative(value: str, label: str, allow_dot: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise GovernanceError(f"{label} escapes the project root: {value}")
    if path == Path(".") and not allow_dot:
        raise GovernanceError(f"{label} cannot be the project root")
    return path


def _contained(project_root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(project_root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _guarded_path(root: Path, relative: Path, label: str) -> Path:
    root = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise GovernanceError(f"{label} contains a symlink: {current}")
    if not _contained(root, current):
        raise GovernanceError(f"{label} escapes its allowed root: {current}")
    return current


def _codex_dir(project_root: Path) -> Path:
    return _guarded_path(project_root, Path(".codex"), ".codex directory")


def _deliverables_path(project_root: Path, policy: Dict[str, object]) -> Path:
    relative = _safe_relative(str(policy["deliverables_dir"]), "deliverables_dir")
    return _guarded_path(project_root, relative, "deliverables directory")


def _matches(relative: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(relative, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]):
            return True
    return False


def _validate_string_list(value: object, label: str, allow_empty: bool = True) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GovernanceError(f"{label} must be a list of strings")
    if not allow_empty and not value:
        raise GovernanceError(f"{label} cannot be empty")
    return list(value)


def _validate_glob(pattern: str, label: str) -> None:
    if not pattern.strip():
        raise GovernanceError(f"{label} cannot be empty")
    path = Path(pattern)
    if path.is_absolute() or ".." in path.parts:
        raise GovernanceError(f"{label} escapes the project root: {pattern}")


def validate_policy(policy: Dict[str, object]) -> Dict[str, object]:
    if policy.get("version") != 1:
        raise GovernanceError("Policy version must be 1")
    if policy.get("mode") not in ALLOWED_MODES:
        raise GovernanceError("Policy mode must be audit or semi-auto")
    deliverables_relative = _safe_relative(
        str(policy.get("deliverables_dir", "")), "deliverables_dir"
    )
    if any(part in {".git", ".codex"} for part in deliverables_relative.parts):
        raise GovernanceError("deliverables_dir cannot use reserved .git or .codex paths")
    stability = policy.get("stability_seconds")
    if not isinstance(stability, int) or isinstance(stability, bool) or stability < 0:
        raise GovernanceError("stability_seconds must be a non-negative integer")
    max_bytes = policy.get("max_copy_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise GovernanceError("max_copy_bytes must be a positive integer")
    max_cache_bytes = policy.get("max_cache_delete_bytes", 20 * 1024 * 1024)
    if (
        not isinstance(max_cache_bytes, int)
        or isinstance(max_cache_bytes, bool)
        or max_cache_bytes <= 0
    ):
        raise GovernanceError("max_cache_delete_bytes must be a positive integer")
    policy["max_cache_delete_bytes"] = max_cache_bytes
    for key in (
        "cleanup_roots",
        "ignored_globs",
        "protected_globs",
        "review_candidate_globs",
    ):
        values = _validate_string_list(policy.get(key), key)
        if key == "cleanup_roots":
            for item in values:
                _safe_relative(item, "cleanup root", allow_dot=True)
        else:
            for item in values:
                _validate_glob(item, key)

    rules = policy.get("artifact_rules")
    if not isinstance(rules, list):
        raise GovernanceError("artifact_rules must be a list")
    for index, rule in enumerate(rules):
        label = f"artifact_rules[{index}]"
        if not isinstance(rule, dict):
            raise GovernanceError(f"{label} must be an object")
        if not isinstance(rule.get("name"), str) or not str(rule["name"]).strip():
            raise GovernanceError(f"{label}.name must be non-empty")
        if not isinstance(rule.get("category"), str) or not str(rule["category"]).strip():
            raise GovernanceError(f"{label}.category must be non-empty")
        includes = _validate_string_list(
            rule.get("include"), f"{label}.include", allow_empty=False
        )
        excludes = _validate_string_list(rule.get("exclude", []), f"{label}.exclude")
        for item in includes:
            _validate_glob(item, f"{label}.include")
        for item in excludes:
            _validate_glob(item, f"{label}.exclude")
        _safe_relative(str(rule.get("destination", "")), f"{label}.destination")
        if rule.get("promotion") not in ALLOWED_PROMOTIONS:
            raise GovernanceError(f"{label}.promotion must be wrapper or copy")
        if rule.get("status") not in ALLOWED_STATUSES:
            raise GovernanceError(f"{label}.status is invalid")
    return policy


def load_policy(project_root: Path) -> Dict[str, object]:
    project_root = project_root.resolve()
    _codex_dir(project_root)
    policy = _read_json(project_root / POLICY_RELATIVE, "project file policy")
    return validate_policy(policy)


def _policy_hash(policy: Dict[str, object]) -> str:
    payload = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _empty_state() -> Dict[str, object]:
    return {
        "version": 1,
        "observed": {},
        "applied": {},
        "pending": {},
        "governance_pending": None,
        "last_scan": None,
        "last_apply": None,
    }


def _read_state(project_root: Path) -> Dict[str, object]:
    path = project_root / STATE_RELATIVE
    if not path.exists():
        return _empty_state()
    state = _read_json(path, "project file state")
    if state.get("version") != 1:
        raise GovernanceError("State version must be 1")
    if not isinstance(state.get("observed"), dict) or not isinstance(state.get("applied"), dict):
        raise GovernanceError("State observed/applied fields must be objects")
    if "pending" not in state:
        state["pending"] = {}
    if not isinstance(state.get("pending"), dict):
        raise GovernanceError("State pending field must be an object")
    if "governance_pending" not in state:
        state["governance_pending"] = None
    if state.get("governance_pending") is not None and not isinstance(
        state.get("governance_pending"), dict
    ):
        raise GovernanceError("State governance_pending must be null or an object")
    return state


def _write_csv(path: Path, fields: Sequence[str], rows: Iterable[Dict[str, object]]) -> None:
    _atomic_write_text(path, _csv_text(fields, rows))


def _read_manifest(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    return _parse_manifest_text(path.read_text(encoding="utf-8"), str(path))


def _parse_manifest_text(text: str, label: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
        raise GovernanceError(f"Manifest fields do not match the required schema: {label}")
    return [dict(row) for row in reader]


def _read_candidate_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CANDIDATE_FIELDS:
            raise GovernanceError(f"Cleanup candidate fields do not match the required schema: {path}")
        return [dict(row) for row in reader]


def _managed_readme(
    existing: str,
    rows: Sequence[Dict[str, str]],
    project_root: Path,
    readme_path: Path,
) -> str:
    lines = [MANAGED_START, "## Managed current artifacts", ""]
    current = [row for row in rows if row.get("status") in {"current", "limited", "pending"}]
    if current:
        for row in sorted(current, key=lambda item: (item.get("category", ""), item.get("title", ""))):
            target = project_root / row["deliverable_path"]
            relative = os.path.relpath(str(target), str(readme_path.parent))
            lines.append(
                f"- {_markdown_link(row['title'], relative)} — `{row['status']}`"
            )
    else:
        lines.append("- No managed artifacts yet.")
    lines.extend(["", MANAGED_END])
    block = "\n".join(lines)
    if MANAGED_START in existing and MANAGED_END in existing:
        prefix, remainder = existing.split(MANAGED_START, 1)
        _, suffix = remainder.split(MANAGED_END, 1)
        return prefix.rstrip() + "\n\n" + block + suffix.rstrip() + "\n"
    base = existing.rstrip() or "# Project deliverables"
    return base + "\n\n" + block + "\n"


def initialize_project(project_root: Path, mode: str = "audit") -> Dict[str, object]:
    project_root = project_root.resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    with _open_directory_chain(project_root, Path(".codex"), create=True):
        pass
    with _exclusive_lock(project_root):
        return _initialize_project_unlocked(project_root, mode)


def _initialize_project_unlocked(
    project_root: Path, mode: str
) -> Dict[str, object]:
    policy_path = project_root / POLICY_RELATIVE
    state_path = project_root / STATE_RELATIVE
    if policy_path.exists():
        policy = load_policy(project_root)
    else:
        policy = default_policy(mode)
        _write_new_text_no_clobber(
            project_root,
            policy_path,
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
        )
    if not state_path.exists():
        _write_new_text_no_clobber(
            project_root,
            state_path,
            json.dumps(_empty_state(), ensure_ascii=False, indent=2) + "\n",
        )
    codex_ignore = project_root / ".codex/.gitignore"
    ignore_snapshot: Optional[
        Tuple[str, str, Tuple[int, int, int, int, int, int]]
    ] = None
    if codex_ignore.exists():
        ignore_snapshot = _read_project_text_snapshot(
            project_root, codex_ignore.relative_to(project_root)
        )
        ignore_lines = ignore_snapshot[0].splitlines()
    else:
        ignore_lines = []
    required_ignores = (
        "project-files-state.json",
        "project-files-plan.json",
        "project-files.lock",
        "project-files-maintain.lock",
    )
    missing_ignores = [entry for entry in required_ignores if entry not in ignore_lines]
    if missing_ignores:
        updated = [*ignore_lines]
        if updated and updated[-1] != "":
            updated.append("")
        updated.extend(["# managing-project-files runtime", *missing_ignores])
        ignore_text = "\n".join(updated).rstrip() + "\n"
        if ignore_snapshot is None:
            _write_new_text_no_clobber(project_root, codex_ignore, ignore_text)
        else:
            _rewrite_owned_text(
                project_root,
                codex_ignore.relative_to(project_root),
                ignore_text,
                ignore_snapshot[1],
                ignore_snapshot[2],
            )

    deliverables = _deliverables_path(project_root, policy)
    with _open_directory_chain(
        project_root, deliverables.relative_to(project_root), create=True
    ):
        pass
    for name in ("reports", "figures", "tables", "archive"):
        directory = _guarded_path(deliverables, Path(name), f"deliverables/{name}")
        with _open_directory_chain(
            project_root, directory.relative_to(project_root), create=True
        ):
            pass
    manifest = deliverables / "MANIFEST.csv"
    if not manifest.exists():
        _write_new_text_no_clobber(
            project_root, manifest, _csv_text(MANIFEST_FIELDS, [])
        )
    manifest_text, _, _ = _read_project_text_snapshot(
        project_root, manifest.relative_to(project_root)
    )
    manifest_rows = _parse_manifest_text(manifest_text, str(manifest))
    candidates = deliverables / "CLEANUP_CANDIDATES.csv"
    if not candidates.exists():
        _write_new_text_no_clobber(
            project_root, candidates, _csv_text(CANDIDATE_FIELDS, [])
        )
    readme = deliverables / "README.md"
    readme_relative = readme.relative_to(project_root)
    if readme.exists():
        existing, readme_digest, readme_identity = _read_project_text_snapshot(
            project_root, readme_relative
        )
    else:
        existing = ""
        readme_digest = ""
        readme_identity = (0, 0, 0, 0, 0, 0)
    readme_text = _managed_readme(
        existing,
        manifest_rows,
        project_root,
        readme,
    )
    if readme.exists():
        _rewrite_owned_text(
            project_root,
            readme_relative,
            readme_text,
            readme_digest,
            readme_identity,
        )
    else:
        _write_new_text_no_clobber(project_root, readme, readme_text)
    status = deliverables / "MAINTENANCE_STATUS.md"
    if not status.exists():
        _write_new_text_no_clobber(
            project_root,
            status,
            "# Project file maintenance status\n\nNo scan has run yet.\n",
        )
    return policy


def _fingerprint(path: Path) -> Dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _file_identity(path: Path) -> Tuple[int, int, int, int, int, int]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise GovernanceError(f"Expected a regular non-symlink file: {path}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _restore_delete_tombstone(
    directory_fd: int, tombstone_name: str, original_name: str
) -> None:
    try:
        os.link(
            tombstone_name,
            original_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise GovernanceError(
            "Cache candidate changed during deletion; preserved the displaced file as "
            f"{tombstone_name} because {original_name} is occupied"
        ) from exc
    try:
        os.unlink(tombstone_name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    os.fsync(directory_fd)


def _assert_directory_still_at_project_path(
    project_root: Path, relative: Path, pinned_fd: int
) -> None:
    pinned = os.fstat(pinned_fd)
    with _open_directory_chain(project_root, relative) as current_fd:
        current = os.fstat(current_fd)
    if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
        raise GovernanceError(
            f"Cleanup directory moved during deletion: {relative}"
        )


def _restore_unlinked_file_from_fd(
    directory_fd: int, source_fd: int, original_name: str, mode: int
) -> str:
    recovery_name = original_name
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        destination_fd = os.open(
            recovery_name, flags, stat.S_IMODE(mode), dir_fd=directory_fd
        )
    except FileExistsError:
        recovery_name = f".project-files-recovered-{secrets.token_hex(12)}-{original_name}"
        destination_fd = os.open(
            recovery_name, flags, stat.S_IMODE(mode), dir_fd=directory_fd
        )
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    os.fsync(directory_fd)
    return recovery_name


def _safe_unlink_cache(
    project_root: Path,
    relative: Path,
    expected_identity: Tuple[int, int, int, int, int, int],
    expected_digest: str,
    policy: Dict[str, object],
) -> None:
    with _open_directory_chain(project_root, relative.parent) as directory_fd:
        try:
            current = os.stat(
                relative.name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError as exc:
            raise GovernanceError(f"Safe-cache candidate disappeared: {relative}") from exc
        if _stat_identity(current) != expected_identity or not stat.S_ISREG(
            current.st_mode
        ):
            raise GovernanceError(f"Safe-cache candidate changed: {relative}")

        tombstone_name = f".project-files-delete-{secrets.token_hex(12)}.hold"
        try:
            os.rename(
                relative.name,
                tombstone_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise GovernanceError(f"Cannot isolate safe-cache candidate: {relative}") from exc

        isolated_present = True
        try:
            isolated = os.stat(
                tombstone_name, dir_fd=directory_fd, follow_symlinks=False
            )
            isolated_identity = _stat_identity(isolated)
            if isolated_identity[:5] != expected_identity[:5] or not stat.S_ISREG(
                isolated.st_mode
            ):
                raise GovernanceError(
                    f"Safe-cache candidate was replaced during isolation: {relative}"
                )

            _assert_directory_still_at_project_path(
                project_root, relative.parent, directory_fd
            )

            current_policy = load_policy(project_root)
            if _policy_hash(current_policy) != _policy_hash(policy):
                raise GovernanceError("Project file policy changed during cache deletion")
            protected = list(current_policy["protected_globs"])
            ignored = list(current_policy["ignored_globs"])
            dynamic_ignore = f"{current_policy['deliverables_dir']}/**"
            if dynamic_ignore not in ignored:
                ignored.append(dynamic_ignore)
            relative_text = relative.as_posix()
            tracked, dirty = _git_protected_paths(project_root)
            if (
                current_policy["mode"] != "semi-auto"
                or not _strict_cache(relative)
                or _matches(relative_text, protected)
                or _matches(relative_text, ignored)
                or relative_text in tracked
                or relative_text in dirty
            ):
                raise GovernanceError(
                    f"Safe-cache candidate became protected before deletion: {relative}"
                )
            _assert_directory_still_at_project_path(
                project_root, relative.parent, directory_fd
            )
            file_flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            isolated_fd = os.open(
                tombstone_name, file_flags, dir_fd=directory_fd
            )
            try:
                isolated_open = os.fstat(isolated_fd)
                if (
                    _stat_identity(isolated_open)[:5] != expected_identity[:5]
                    or _digest_open_fd(isolated_fd) != expected_digest
                ):
                    raise GovernanceError(
                        f"Safe-cache candidate changed before final unlink: {relative}"
                    )
                os.unlink(tombstone_name, dir_fd=directory_fd)
                isolated_present = False
                after_unlink = os.fstat(isolated_fd)
                if (
                    _stat_identity(after_unlink)[:5] != expected_identity[:5]
                    or _digest_open_fd(isolated_fd) != expected_digest
                ):
                    recovered_name = _restore_unlinked_file_from_fd(
                        directory_fd,
                        isolated_fd,
                        relative.name,
                        after_unlink.st_mode,
                    )
                    raise GovernanceError(
                        "Safe-cache content changed during final unlink; restored it as "
                        f"{recovered_name}"
                    )
                try:
                    _assert_directory_still_at_project_path(
                        project_root, relative.parent, directory_fd
                    )
                except GovernanceError as exc:
                    recovered_name = _restore_unlinked_file_from_fd(
                        directory_fd,
                        isolated_fd,
                        relative.name,
                        isolated_open.st_mode,
                    )
                    raise GovernanceError(
                        "Cleanup directory moved during final unlink; restored the file as "
                        f"{recovered_name}"
                    ) from exc
                os.fsync(directory_fd)
            finally:
                os.close(isolated_fd)
        except Exception:
            if isolated_present:
                try:
                    _restore_delete_tombstone(
                        directory_fd, tombstone_name, relative.name
                    )
                except GovernanceError:
                    raise
            raise


def _stat_identity(metadata: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _atomic_exchange(
    directory_fd: int, first_name: str, second_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    first = os.fsencode(first_name)
    second = os.fsencode(second_name)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(directory_fd, first, directory_fd, second, 0x00000002)
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(directory_fd, first, directory_fd, second, 0x00000002)
    else:
        raise GovernanceError(
            "This platform lacks atomic rename-exchange required for safe governance updates"
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise GovernanceError(
            f"Atomic governance exchange failed: {os.strerror(error_number)}"
        )


@contextmanager
def _open_directory_chain(root: Path, relative: Path, create: bool = False):
    root = root.resolve()
    try:
        descriptor = os.open(str(root), _directory_open_flags())
    except OSError as exc:
        raise GovernanceError(f"Cannot safely open directory root: {root}") from exc
    try:
        for part in relative.parts:
            if part in ("", ".", ".."):
                if part in ("", "."):
                    continue
                raise GovernanceError(f"Unsafe directory component: {relative}")
            try:
                child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise GovernanceError(f"Missing managed directory: {root / relative}")
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                try:
                    child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                except OSError as exc:
                    raise GovernanceError(
                        f"Cannot safely create managed directory: {root / relative}"
                    ) from exc
            except OSError as exc:
                raise GovernanceError(f"Unsafe managed directory: {root / relative}") from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_project_file(project_root: Path, relative: Path):
    with _open_directory_chain(project_root, relative.parent) as directory_fd:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(relative.name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise GovernanceError(f"Cannot safely open project file: {relative}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise GovernanceError(f"Expected a regular project file: {relative}")
            yield descriptor
        finally:
            os.close(descriptor)


def _hash_project_file(
    project_root: Path, relative: Path
) -> Tuple[str, Dict[str, int], Tuple[int, int, int, int, int, int]]:
    with _open_project_file(project_root, relative) as descriptor:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise GovernanceError(f"Project file changed while hashing: {relative}")
        return (
            digest.hexdigest(),
            {"size": after.st_size, "mtime_ns": after.st_mtime_ns},
            _stat_identity(after),
        )


def _read_project_bytes_snapshot(
    project_root: Path, relative: Path
) -> Tuple[bytes, str, Tuple[int, int, int, int, int, int]]:
    with _open_project_file(project_root, relative) as descriptor:
        before = os.fstat(descriptor)
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise GovernanceError(f"Managed text changed while reading: {relative}")
        data = b"".join(chunks)
        return data, hashlib.sha256(data).hexdigest(), _stat_identity(after)


def _read_project_text_snapshot(
    project_root: Path, relative: Path
) -> Tuple[str, str, Tuple[int, int, int, int, int, int]]:
    data, digest, identity = _read_project_bytes_snapshot(project_root, relative)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GovernanceError(f"Managed text is not UTF-8: {relative}") from exc
    return text, digest, identity


def _digest_open_fd(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _remove_or_preserve_displaced_file(
    directory_fd: int, temporary_name: str, manager_digest: str
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary_name, flags, dir_fd=directory_fd)
    try:
        digest = _digest_open_fd(descriptor)
    finally:
        os.close(descriptor)
    if digest == manager_digest:
        os.unlink(temporary_name, dir_fd=directory_fd)
        return
    displaced_name = f".project-files-displaced-{secrets.token_hex(12)}.hold"
    os.rename(
        temporary_name,
        displaced_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    raise GovernanceError(
        f"Concurrent governance content was preserved as {displaced_name}"
    )


def _manager_target_matches(
    directory_fd: int,
    name: str,
    expected_identity: Tuple[int, int, int, int, int, int],
    expected_digest: str,
) -> bool:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    except OSError:
        return False
    try:
        current = os.fstat(descriptor)
        return not (
            _stat_identity(current)[:5] != expected_identity[:5]
            or _digest_open_fd(descriptor) != expected_digest
        )
    finally:
        os.close(descriptor)


def _assert_manager_target(
    directory_fd: int,
    name: str,
    expected_identity: Tuple[int, int, int, int, int, int],
    expected_digest: str,
) -> None:
    if not _manager_target_matches(
        directory_fd, name, expected_identity, expected_digest
    ):
        raise GovernanceError(
            f"Managed governance target changed during commit: {name}"
        )


def _create_synced_governance_temp(
    directory_fd: int, name: str, data: bytes, mode: int
) -> Tuple[int, int, int, int, int, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(
            name, flags, stat.S_IMODE(mode), dir_fd=directory_fd
        )
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
            return _stat_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
    except BaseException:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _rewrite_owned_text(
    project_root: Path,
    relative: Path,
    text: str,
    expected_digest: str,
    expected_identity: Tuple[int, int, int, int, int, int],
) -> None:
    data = text.encode("utf-8")
    manager_digest = hashlib.sha256(data).hexdigest()
    with _open_directory_chain(project_root, relative.parent) as directory_fd:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(relative.name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise GovernanceError(f"Cannot safely update managed file: {relative}") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GovernanceError(f"Managed file is locked by another process: {relative}") from exc
            before = os.fstat(descriptor)
            if _stat_identity(before) != expected_identity or not stat.S_ISREG(
                before.st_mode
            ):
                raise GovernanceError(f"Managed file identity changed: {relative}")
            if _digest_open_fd(descriptor) != expected_digest:
                raise GovernanceError(f"Managed file content changed: {relative}")
            temporary_name = f".project-files-governance-{secrets.token_hex(12)}.tmp"
            create_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            manager_identity = _create_synced_governance_temp(
                directory_fd,
                temporary_name,
                data,
                before.st_mode,
            )

            exchanged = False
            old_link_removed = False
            displaced_fd: Optional[int] = None
            try:
                _assert_directory_still_at_project_path(
                    project_root, relative.parent, directory_fd
                )
                _atomic_exchange(directory_fd, temporary_name, relative.name)
                exchanged = True
                _assert_directory_still_at_project_path(
                    project_root, relative.parent, directory_fd
                )
                displaced_fd = os.open(
                    temporary_name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                after_exchange = os.fstat(displaced_fd)
                if (
                    _stat_identity(after_exchange)[:5] != expected_identity[:5]
                    or _digest_open_fd(displaced_fd) != expected_digest
                ):
                    raise GovernanceError(
                        f"Managed file changed before atomic commit: {relative}"
                    )
                _assert_manager_target(
                    directory_fd,
                    relative.name,
                    manager_identity,
                    manager_digest,
                )
                _assert_directory_still_at_project_path(
                    project_root, relative.parent, directory_fd
                )
                _assert_manager_target(
                    directory_fd,
                    relative.name,
                    manager_identity,
                    manager_digest,
                )
                os.unlink(temporary_name, dir_fd=directory_fd)
                old_link_removed = True
                try:
                    _assert_directory_still_at_project_path(
                        project_root, relative.parent, directory_fd
                    )
                    _assert_manager_target(
                        directory_fd,
                        relative.name,
                        manager_identity,
                        manager_digest,
                    )
                except GovernanceError as exc:
                    recovery_name = f".project-files-rollback-{secrets.token_hex(12)}.tmp"
                    recovery_fd = os.open(
                        recovery_name,
                        create_flags,
                        stat.S_IMODE(before.st_mode),
                        dir_fd=directory_fd,
                    )
                    try:
                        os.lseek(displaced_fd, 0, os.SEEK_SET)
                        while True:
                            chunk = os.read(displaced_fd, 1024 * 1024)
                            if not chunk:
                                break
                            _write_all(recovery_fd, chunk)
                        os.fsync(recovery_fd)
                    finally:
                        os.close(recovery_fd)
                    _atomic_exchange(directory_fd, recovery_name, relative.name)
                    _remove_or_preserve_displaced_file(
                        directory_fd, recovery_name, manager_digest
                    )
                    raise GovernanceError(
                        f"Governance directory moved during commit; restored {relative}"
                    ) from exc
                os.fsync(directory_fd)
            except BaseException:
                if not old_link_removed:
                    try:
                        rollback_fd = os.open(
                            temporary_name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        rollback_fd = None
                    if rollback_fd is not None:
                        try:
                            rollback_identity = _stat_identity(os.fstat(rollback_fd))
                            rollback_digest = _digest_open_fd(rollback_fd)
                        finally:
                            os.close(rollback_fd)
                        temp_is_unexchanged_manager = (
                            rollback_identity[:5] == manager_identity[:5]
                            and rollback_digest == manager_digest
                        )
                        if temp_is_unexchanged_manager:
                            os.unlink(temporary_name, dir_fd=directory_fd)
                        elif _manager_target_matches(
                            directory_fd,
                            relative.name,
                            manager_identity,
                            manager_digest,
                        ):
                            _atomic_exchange(
                                directory_fd, temporary_name, relative.name
                            )
                            _remove_or_preserve_displaced_file(
                                directory_fd, temporary_name, manager_digest
                            )
                        elif (
                            rollback_identity[:5] == expected_identity[:5]
                            and rollback_digest == expected_digest
                        ):
                            os.unlink(temporary_name, dir_fd=directory_fd)
                        else:
                            displaced_name = (
                                f".project-files-displaced-{secrets.token_hex(12)}.hold"
                            )
                            os.rename(
                                temporary_name,
                                displaced_name,
                                src_dir_fd=directory_fd,
                                dst_dir_fd=directory_fd,
                            )
                raise
            finally:
                if displaced_fd is not None:
                    os.close(displaced_fd)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _looks_like_interrupted_rewrite(
    current: bytes, old: bytes, new: bytes
) -> bool:
    if current in (old, new):
        return True
    if len(current) == len(old):
        differing = [index for index, value in enumerate(current) if value != old[index]]
        split = (max(differing) + 1) if differing else 0
        return (
            split <= len(new)
            and current[:split] == new[:split]
            and current[split:] == old[split:]
        )
    if len(current) > len(old):
        return len(current) <= len(new) and current == new[: len(current)]
    return False


def _recover_governance_transaction(
    project_root: Path, deliverables: Path
) -> None:
    state = _read_state(project_root)
    transaction = state.get("governance_pending")
    if transaction is None:
        return
    if transaction.get("version") != 1 or not isinstance(
        transaction.get("files"), list
    ):
        raise GovernanceError("Invalid pending governance transaction")
    allowed = {
        (deliverables / "MANIFEST.csv").relative_to(project_root).as_posix(),
        (deliverables / "README.md").relative_to(project_root).as_posix(),
    }
    for entry_value in transaction["files"]:
        if not isinstance(entry_value, dict):
            raise GovernanceError("Invalid governance transaction file entry")
        relative_text = str(entry_value.get("path", ""))
        if relative_text not in allowed:
            raise GovernanceError(
                f"Pending governance transaction targets an unsafe path: {relative_text}"
            )
        old_text = entry_value.get("old_text")
        new_text = entry_value.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise GovernanceError("Governance transaction text must be strings")
        relative = Path(relative_text)
        current, current_digest, current_identity = _read_project_bytes_snapshot(
            project_root, relative
        )
        new_bytes = new_text.encode("utf-8")
        new_digest = hashlib.sha256(new_bytes).hexdigest()
        if current_digest == new_digest:
            continue
        old_bytes = old_text.encode("utf-8")
        if not _looks_like_interrupted_rewrite(current, old_bytes, new_bytes):
            raise GovernanceError(
                f"Governance file changed outside the pending transaction: {relative_text}"
            )
        _rewrite_owned_text(
            project_root,
            relative,
            new_text,
            current_digest,
            current_identity,
        )
    state["governance_pending"] = None
    _write_json(project_root / STATE_RELATIVE, state)


def _same_fingerprint(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return left.get("size") == right.get("size") and left.get("mtime_ns") == right.get("mtime_ns")


def _stable_observation(
    relative: str,
    fingerprint: Dict[str, int],
    observed: Dict[str, object],
    now: float,
    stability_seconds: int,
) -> Tuple[bool, Dict[str, object]]:
    previous = observed.get(relative)
    mtime_seconds = fingerprint["mtime_ns"] / 1_000_000_000
    if isinstance(previous, dict) and _same_fingerprint(previous, fingerprint):
        first_seen = float(previous.get("first_seen_at", now))
    else:
        first_seen = now
    previously_stable = isinstance(previous, dict) and previous.get("stable") is True
    stable = previously_stable or (now - mtime_seconds >= stability_seconds) or (
        now - first_seen >= stability_seconds
    )
    return stable, {
        "size": fingerprint["size"],
        "mtime_ns": fingerprint["mtime_ns"],
        "first_seen_at": first_seen,
        "last_seen_at": now,
        "stable": stable,
    }


def _git_paths(project_root: Path, args: Sequence[str]) -> Set[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GovernanceError("Git is required for safe project-state checks") from exc
    if result.returncode != 0:
        raise GovernanceError(
            f"Git protection query failed in {project_root}: git {' '.join(args)}"
        )
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def _git_toplevel(path: Path) -> Optional[Path]:
    directory = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GovernanceError("Git is required for safe project-state checks") from exc
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _git_protected_paths(project_root: Path) -> Tuple[Set[str], Set[str]]:
    repository_root = _git_toplevel(project_root)
    if repository_root is None:
        if os.path.lexists(str(project_root / ".git")):
            raise GovernanceError(f"Invalid or unreadable Git metadata: {project_root / '.git'}")
        return set(), set()
    try:
        project_prefix = project_root.resolve().relative_to(repository_root)
    except ValueError as exc:
        raise GovernanceError("Git root does not contain the project root") from exc

    def project_relative(paths: Set[str]) -> Set[str]:
        converted: Set[str] = set()
        for value in paths:
            path = Path(value)
            try:
                relative = path.relative_to(project_prefix) if project_prefix.parts else path
            except ValueError:
                continue
            converted.add(relative.as_posix())
        return converted

    tracked = project_relative(_git_paths(repository_root, ["ls-files", "-z"]))
    dirty_values = _git_paths(repository_root, ["diff", "--name-only", "-z", "--"])
    dirty_values.update(
        _git_paths(repository_root, ["diff", "--cached", "--name-only", "-z", "--"])
    )
    return tracked, project_relative(dirty_values)


def _source_id(category: str, source_path: str) -> str:
    prefix = {"report": "R", "figure": "F", "table": "T"}.get(category, "A")
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def _candidate_id(path: str) -> str:
    return "C-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:10].upper()


def _artifact_title(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip().title() or path.name


def _destination_for(rule: Dict[str, object], source: Path, deliverables_dir: str) -> str:
    suffix = ".md" if rule["promotion"] == "wrapper" else source.suffix
    return (Path(deliverables_dir) / str(rule["destination"]) / f"{source.stem}{suffix}").as_posix()


def _destination_is_compatible(candidate: str, policy_destination: str) -> bool:
    candidate_path = Path(candidate)
    policy_path = Path(policy_destination)
    return (
        candidate_path.parent == policy_path.parent
        and candidate_path.suffix == policy_path.suffix
    )


def _strict_cache(path: Path) -> bool:
    return path.name == ".DS_Store" or path.suffix.lower() == ".pyc"


def _matches_tree(relative: str, patterns: Sequence[str]) -> bool:
    return _matches(relative, patterns) or _matches(relative.rstrip("/") + "/__tree__", patterns)


def _iter_cleanup_files(
    project_root: Path,
    roots: Sequence[str],
    ignored: Sequence[str],
    protected: Sequence[str],
) -> Iterable[Path]:
    seen: Set[str] = set()
    project_git_root = _git_toplevel(project_root)
    for relative_root in roots:
        root = (project_root / relative_root).resolve()
        if not _contained(project_root, root) or not root.exists():
            continue
        cleanup_git_root = _git_toplevel(root)
        if cleanup_git_root is not None and cleanup_git_root != project_git_root:
            continue
        if root.is_file():
            relative = root.relative_to(project_root).as_posix()
            if not _matches_tree(relative, ignored) and not _matches_tree(relative, protected):
                yield root
            continue
        if root != project_root and os.path.lexists(str(root / ".git")):
            continue
        root_relative = root.relative_to(project_root).as_posix()
        if root_relative != "." and (
            _matches_tree(root_relative, ignored)
            or _matches_tree(root_relative, protected)
        ):
            continue
        for current_text, directory_names, file_names in os.walk(
            str(root), topdown=True, followlinks=False
        ):
            current = Path(current_text)
            if current != project_root and current != root and os.path.lexists(str(current / ".git")):
                directory_names[:] = []
                continue
            kept_directories: List[str] = []
            for name in directory_names:
                child = current / name
                relative = child.relative_to(project_root).as_posix()
                if (
                    child.is_symlink()
                    or name == ".git"
                    or os.path.lexists(str(child / ".git"))
                    or _matches_tree(relative, ignored)
                    or _matches_tree(relative, protected)
                ):
                    continue
                kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in file_names:
                path = current / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(project_root).as_posix()
                if (
                    relative in seen
                    or _matches_tree(relative, ignored)
                    or _matches_tree(relative, protected)
                ):
                    continue
                seen.add(relative)
                yield path


def _status_text(plan: Dict[str, object], mode: str) -> str:
    return (
        "# Project file maintenance status\n\n"
        f"- Generated: `{plan['generated_at']}`\n"
        f"- Mode: `{mode}`\n"
        f"- Proposed promotions: `{len(plan['promotions'])}`\n"
        f"- Safe-cache deletions authorized by policy: `{len(plan['safe_cache_deletions'])}`\n"
        f"- Review candidates: `{len(plan['review_candidates'])}`\n"
        f"- Deferred artifacts: `{len(plan['deferred'])}`\n"
    )


def _desired_signature(action: Dict[str, object]) -> str:
    fields = {
        key: action[key]
        for key in (
            "id",
            "category",
            "title",
            "date",
            "status",
            "source_path",
            "deliverable_path",
            "promotion",
            "rule",
            "content_sha256",
        )
    }
    if "source_sha256" in action:
        fields["source_sha256"] = action["source_sha256"]
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _action_content_sha256(project_root: Path, action: Dict[str, object]) -> str:
    if action["promotion"] == "copy":
        digest = action.get("source_sha256")
        if not isinstance(digest, str):
            raise GovernanceError(f"Copy action lacks a source digest: {action['source_path']}")
        return digest
    if action["promotion"] == "wrapper":
        return hashlib.sha256(_wrapper_text(project_root, action).encode("utf-8")).hexdigest()
    raise GovernanceError(f"Unsupported promotion mode: {action['promotion']}")


def _record_owns_destination(
    record: object, destination_relative: str, destination_digest: str
) -> bool:
    return (
        isinstance(record, dict)
        and record.get("deliverable_path") == destination_relative
        and record.get("destination_sha256") == destination_digest
    )


def _pending_owns_destination(
    record: object, destination_relative: str, destination_digest: str
) -> bool:
    return (
        isinstance(record, dict)
        and record.get("deliverable_path") == destination_relative
        and record.get("content_sha256") == destination_digest
    )


def _owned_prior_content_digest(
    previous: object,
    pending_record: object,
    destination_relative: str,
    destination_digest: Optional[str],
) -> Optional[str]:
    if destination_digest is None:
        return None
    if _record_owns_destination(
        previous, destination_relative, destination_digest
    ):
        return destination_digest
    if _pending_owns_destination(
        pending_record, destination_relative, destination_digest
    ):
        return destination_digest
    return None


def _manifest_matches_action(row: Optional[Dict[str, str]], action: Dict[str, object]) -> bool:
    if row is None:
        return False
    expected = {
        "id": str(action["id"]),
        "category": str(action["category"]),
        "title": str(action["title"]),
        "date": str(action["date"]),
        "status": str(action["status"]),
        "deliverable_path": str(action["deliverable_path"]),
        "source_path": str(action["source_path"]),
        "notes": f"Managed by {action['rule']}; canonical source preserved",
    }
    return all(row.get(key) == value for key, value in expected.items())


def _assert_action_destination(
    project_root: Path,
    deliverables: Path,
    destination: Path,
    action: Dict[str, object],
    applied: Dict[str, object],
    pending: Dict[str, object],
    manifest_by_destination: Dict[str, str],
) -> None:
    destination_relative = str(action["deliverable_path"])
    source_relative = str(action["source_path"])
    if not _contained(deliverables, destination):
        raise GovernanceError(
            f"Promotion destination is outside deliverables: {destination_relative}"
        )
    _guarded_path(
        deliverables,
        destination.relative_to(deliverables),
        "promotion destination",
    )
    if not destination.exists():
        if action.get("write_required") is not True:
            raise GovernanceError(
                f"Managed destination disappeared before reconciliation: {destination_relative}"
            )
        return
    destination_digest, _, _ = _hash_project_file(
        project_root, Path(destination_relative)
    )
    owner = manifest_by_destination.get(destination_relative)
    owned = (
        _record_owns_destination(
            applied.get(source_relative), destination_relative, destination_digest
        )
        or _pending_owns_destination(
            pending.get(source_relative), destination_relative, destination_digest
        )
        or owner == source_relative
    )
    if owner not in (None, "", source_relative) or not owned:
        raise GovernanceError(
            f"Promotion destination belongs to another source: {destination_relative}"
        )
    if (
        destination_digest != action.get("content_sha256")
        or action.get("write_required") is True
    ):
        raise GovernanceError(
            f"Existing promotion destination is unmanaged or modified: {destination_relative}"
        )


def _scan_project_unlocked(project_root: Path, now: Optional[float] = None) -> Dict[str, object]:
    project_root = project_root.resolve()
    policy = load_policy(project_root)
    scan_time = time.time() if now is None else now
    deliverables_dir = str(policy["deliverables_dir"])
    deliverables = _deliverables_path(project_root, policy)
    _recover_governance_transaction(project_root, deliverables)
    manifest_path = deliverables / "MANIFEST.csv"
    manifest_relative = manifest_path.relative_to(project_root)
    manifest_text, _, _ = _read_project_text_snapshot(
        project_root, manifest_relative
    )
    manifest_rows = _parse_manifest_text(manifest_text, str(manifest_path))
    manifest_by_source = {row["source_path"]: row for row in manifest_rows if row.get("source_path")}
    state = _read_state(project_root)
    observed = dict(state.get("observed", {}))
    applied = dict(state.get("applied", {}))
    pending = dict(state.get("pending", {}))
    ignored = list(policy["ignored_globs"])
    dynamic_ignore = f"{deliverables_dir}/**"
    if dynamic_ignore not in ignored:
        ignored.append(dynamic_ignore)
    protected = list(policy["protected_globs"])
    stability_seconds = int(policy["stability_seconds"])
    max_copy_bytes = int(policy["max_copy_bytes"])
    max_cache_delete_bytes = int(policy["max_cache_delete_bytes"])

    raw_promotions: List[Dict[str, object]] = []
    deferred: List[Dict[str, object]] = []
    seen_sources: Set[str] = set()
    for rule_value in policy["artifact_rules"]:
        rule = dict(rule_value)
        for pattern in rule["include"]:
            try:
                matches = project_root.glob(pattern)
            except (ValueError, OSError) as exc:
                raise GovernanceError(f"Invalid artifact glob {pattern}: {exc}") from exc
            for source in matches:
                if not source.is_file() or source.is_symlink() or not _contained(project_root, source):
                    continue
                relative = source.relative_to(project_root).as_posix()
                if relative in seen_sources:
                    continue
                if _matches(relative, ignored) or _matches(relative, rule.get("exclude", [])):
                    continue
                if _matches(relative, protected):
                    deferred.append({"source_path": relative, "reason": "protected"})
                    continue
                seen_sources.add(relative)
                fingerprint = _fingerprint(source)
                stable, observation = _stable_observation(
                    relative, fingerprint, observed, scan_time, stability_seconds
                )
                observed[relative] = observation
                if not stable:
                    deferred.append({"source_path": relative, "reason": "unstable"})
                    continue
                if rule["promotion"] == "copy" and fingerprint["size"] > max_copy_bytes:
                    deferred.append({"source_path": relative, "reason": "copy_size_limit"})
                    continue
                source_sha256: Optional[str] = None
                if rule["promotion"] == "copy":
                    source_sha256, hashed_fingerprint, _ = _hash_project_file(
                        project_root, Path(relative)
                    )
                    if not _same_fingerprint(fingerprint, hashed_fingerprint):
                        deferred.append(
                            {"source_path": relative, "reason": "changed_during_scan"}
                        )
                        continue
                    fingerprint = hashed_fingerprint
                policy_destination = _destination_for(
                    rule, source, deliverables_dir
                )
                destination = policy_destination
                existing_applied = applied.get(relative)
                existing_pending = pending.get(relative)
                existing_manifest = manifest_by_source.get(relative)
                if (
                    existing_manifest
                    and _destination_is_compatible(
                        existing_manifest["deliverable_path"], policy_destination
                    )
                    and (project_root / existing_manifest["deliverable_path"]).exists()
                ):
                    destination = existing_manifest["deliverable_path"]
                elif isinstance(existing_applied, dict):
                    applied_destination = str(existing_applied.get("deliverable_path", ""))
                    if (
                        applied_destination
                        and _destination_is_compatible(
                            applied_destination, policy_destination
                        )
                        and (project_root / applied_destination).exists()
                    ):
                        destination = applied_destination
                if isinstance(existing_pending, dict):
                    pending_destination = str(
                        existing_pending.get("deliverable_path", "")
                    )
                    if (
                        pending_destination
                        and _destination_is_compatible(
                            pending_destination, policy_destination
                        )
                        and (project_root / pending_destination).exists()
                    ):
                        destination = pending_destination
                action: Dict[str, object] = {
                        "id": _source_id(str(rule["category"]), relative),
                        "category": rule["category"],
                        "title": _artifact_title(source),
                        "date": datetime.fromtimestamp(
                            fingerprint["mtime_ns"] / 1_000_000_000, timezone.utc
                        ).date().isoformat(),
                        "status": rule["status"],
                        "source_path": relative,
                        "deliverable_path": destination,
                        "promotion": rule["promotion"],
                        "fingerprint": fingerprint,
                        "rule": rule["name"],
                    }
                if source_sha256 is not None:
                    action["source_sha256"] = source_sha256
                raw_promotions.append(action)

    grouped: Dict[str, List[Dict[str, object]]] = {}
    for action in raw_promotions:
        grouped.setdefault(str(action["deliverable_path"]), []).append(action)
    promotions: List[Dict[str, object]] = []
    occupied = {
        row["deliverable_path"]: row.get("source_path", "")
        for row in manifest_rows
        if row.get("deliverable_path")
    }
    for source_path, previous_value in applied.items():
        if isinstance(previous_value, dict) and previous_value.get("deliverable_path"):
            occupied.setdefault(str(previous_value["deliverable_path"]), source_path)
    for source_path, pending_value in pending.items():
        if isinstance(pending_value, dict) and pending_value.get("deliverable_path"):
            occupied.setdefault(str(pending_value["deliverable_path"]), source_path)
    for destination, actions in sorted(grouped.items()):
        action_sources = {str(action["source_path"]) for action in actions}
        existing_owner = occupied.get(destination)
        collision = len(actions) > 1 or (
            (project_root / destination).exists()
            and (existing_owner is None or existing_owner not in action_sources)
        )
        for action in sorted(actions, key=lambda item: str(item["source_path"])):
            if collision:
                path = Path(destination)
                digest = hashlib.sha256(str(action["source_path"]).encode("utf-8")).hexdigest()[:8]
                action["deliverable_path"] = (path.parent / f"{path.stem}-{digest}{path.suffix}").as_posix()
            source_path = str(action["source_path"])
            previous = applied.get(source_path)
            pending_record = pending.get(source_path)
            destination_relative = str(action["deliverable_path"])
            destination_path = project_root / destination_relative
            action["content_sha256"] = _action_content_sha256(project_root, action)
            initial_destination_digest = (
                _hash_project_file(project_root, Path(destination_relative))[0]
                if destination_path.exists()
                else None
            )

            prior_content_digest = _owned_prior_content_digest(
                previous,
                pending_record,
                destination_relative,
                initial_destination_digest,
            )
            if (
                prior_content_digest is not None
                and prior_content_digest != action["content_sha256"]
            ):
                path = Path(destination_relative)
                revision = str(action["content_sha256"])[:8]
                action["deliverable_path"] = (
                    path.parent / f"{path.stem}-{revision}{path.suffix}"
                ).as_posix()
                destination_relative = str(action["deliverable_path"])
                destination_path = project_root / destination_relative
                action["content_sha256"] = _action_content_sha256(
                    project_root, action
                )

            destination_exists = destination_path.exists()
            destination_digest = (
                _hash_project_file(project_root, Path(destination_relative))[0]
                if destination_exists
                else None
            )
            manifest_owner = occupied.get(destination_relative)
            owned_exactly = bool(
                destination_exists
                and destination_digest == action["content_sha256"]
                and (
                    _record_owns_destination(
                        previous, destination_relative, str(destination_digest)
                    )
                    or _pending_owns_destination(
                        pending_record, destination_relative, str(destination_digest)
                    )
                    or manifest_owner == source_path
                )
            )
            action["write_required"] = not owned_exactly
            action["desired_signature"] = _desired_signature(action)
            if (
                _same_fingerprint(previous, action["fingerprint"])
                and isinstance(previous, dict)
                and previous.get("desired_signature") == action["desired_signature"]
                and owned_exactly
                and _manifest_matches_action(manifest_by_source.get(source_path), action)
            ):
                continue
            promotions.append(action)

    tracked, dirty = _git_protected_paths(project_root)
    safe_deletions: List[Dict[str, object]] = []
    review_candidates: List[Dict[str, object]] = []
    candidate_rows: List[Dict[str, object]] = []
    for path in _iter_cleanup_files(
        project_root,
        list(policy["cleanup_roots"]),
        ignored,
        protected,
    ):
        relative = path.relative_to(project_root).as_posix()
        if _matches(relative, ignored):
            continue
        is_protected = _matches(relative, protected)
        fingerprint = _fingerprint(path)
        if _strict_cache(path):
            if is_protected or relative in tracked or relative in dirty:
                status = "protected"
                recommendation = "keep"
                reason = "strict safe-cache filename but protected by policy or Git"
            elif fingerprint["size"] > max_cache_delete_bytes:
                status = "review_required"
                recommendation = "review_only"
                reason = "strict cache exceeds the automatic hash/delete size bound"
            else:
                status = "safe_cache"
                recommendation = "delete_in_semi_auto"
                reason = "strict safe-cache filename within the automatic size bound"
            row = {
                "id": _candidate_id(relative),
                "path": relative,
                "size_bytes": fingerprint["size"],
                "size_human": _human_bytes(fingerprint["size"]),
                "risk": "protected" if status == "protected" else ("review" if status == "review_required" else "low"),
                "status": status,
                "recommendation": recommendation,
                "evidence_required": "tracked/dirty/protected review" if status == "protected" else ("size-bound review" if status == "review_required" else "strict cache identity and SHA-256"),
                "reason": reason,
            }
            candidate_rows.append(row)
            if status == "review_required":
                review_candidates.append(row)
            if status == "safe_cache" and policy["mode"] == "semi-auto":
                cache_digest, hashed_fingerprint, _ = _hash_project_file(
                    project_root, Path(relative)
                )
                if not _same_fingerprint(fingerprint, hashed_fingerprint):
                    raise GovernanceError(
                        f"Safe-cache candidate changed while hashing: {relative}"
                    )
                safe_deletions.append(
                    {
                        "path": relative,
                        "fingerprint": fingerprint,
                        "sha256": cache_digest,
                    }
                )
            continue
        if _matches(relative, list(policy["review_candidate_globs"])):
            row = {
                "id": _candidate_id(relative),
                "path": relative,
                "size_bytes": fingerprint["size"],
                "size_human": _human_bytes(fingerprint["size"]),
                "risk": "protected" if is_protected else "review",
                "status": "protected" if is_protected else "review_required",
                "recommendation": "keep" if is_protected else "review_only",
                "evidence_required": "project-specific provenance and exact authorization",
                "reason": "matches review-candidate policy; never auto-delete",
            }
            candidate_rows.append(row)
            review_candidates.append(row)

    plan: Dict[str, object] = {
        "version": 1,
        "generated_at": _timestamp(scan_time),
        "project_root": str(project_root),
        "policy_hash": _policy_hash(policy),
        "promotions": promotions,
        "safe_cache_deletions": sorted(safe_deletions, key=lambda item: str(item["path"])),
        "review_candidates": sorted(review_candidates, key=lambda item: str(item["path"])),
        "deferred": sorted(deferred, key=lambda item: str(item["source_path"])),
    }
    state["observed"] = observed
    state["last_scan"] = plan["generated_at"]
    _write_json(project_root / STATE_RELATIVE, state)
    _write_json(project_root / PLAN_RELATIVE, plan)
    _write_csv(
        deliverables / "CLEANUP_CANDIDATES.csv",
        CANDIDATE_FIELDS,
        sorted(candidate_rows, key=lambda item: str(item["path"])),
    )
    _atomic_write_text(deliverables / "MAINTENANCE_STATUS.md", _status_text(plan, str(policy["mode"])))
    return plan


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise GovernanceError("Short write while creating a managed artifact")
        offset += written


def _publish_temporary_no_clobber(
    project_root: Path,
    parent_relative: Path,
    directory_fd: int,
    temporary_name: str,
    destination_name: str,
) -> None:
    linked = False
    try:
        _assert_directory_still_at_project_path(
            project_root, parent_relative, directory_fd
        )
        os.link(
            temporary_name,
            destination_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        _assert_directory_still_at_project_path(
            project_root, parent_relative, directory_fd
        )
    except FileExistsError as exc:
        raise GovernanceError(
            f"Refusing to overwrite an existing managed destination: {destination_name}"
        ) from exc
    except GovernanceError:
        if linked:
            temporary_stat = os.stat(
                temporary_name, dir_fd=directory_fd, follow_symlinks=False
            )
            destination_stat = os.stat(
                destination_name, dir_fd=directory_fd, follow_symlinks=False
            )
            if (temporary_stat.st_dev, temporary_stat.st_ino) == (
                destination_stat.st_dev,
                destination_stat.st_ino,
            ):
                os.unlink(destination_name, dir_fd=directory_fd)
        raise
    except OSError as exc:
        raise GovernanceError(
            f"Cannot atomically publish managed destination: {destination_name}"
        ) from exc
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    os.fsync(directory_fd)


def _write_new_text_no_clobber(
    project_root: Path, destination: Path, text: str
) -> None:
    relative = destination.relative_to(project_root)
    data = text.encode("utf-8")
    with _open_directory_chain(project_root, relative.parent, create=True) as directory_fd:
        temporary_name = f".project-files-{secrets.token_hex(12)}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary_name, flags, 0o644, dir_fd=directory_fd)
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _publish_temporary_no_clobber(
            project_root,
            relative.parent,
            directory_fd,
            temporary_name,
            relative.name,
        )


def _copy_new_no_clobber(
    project_root: Path,
    source_relative: Path,
    destination: Path,
    expected_fingerprint: Dict[str, int],
    expected_digest: str,
) -> None:
    destination_relative = destination.relative_to(project_root)
    with _open_project_file(project_root, source_relative) as source_fd:
        source_before = os.fstat(source_fd)
        source_fingerprint = {
            "size": source_before.st_size,
            "mtime_ns": source_before.st_mtime_ns,
        }
        if not _same_fingerprint(source_fingerprint, expected_fingerprint):
            raise GovernanceError(f"Copy source changed before publication: {source_relative}")
        with _open_directory_chain(
            project_root, destination_relative.parent, create=True
        ) as directory_fd:
            temporary_name = f".project-files-{secrets.token_hex(12)}.tmp"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            destination_fd = os.open(
                temporary_name,
                flags,
                stat.S_IMODE(source_before.st_mode),
                dir_fd=directory_fd,
            )
            digest = hashlib.sha256()
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    _write_all(destination_fd, chunk)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            source_after = os.fstat(source_fd)
            if (
                _stat_identity(source_before) != _stat_identity(source_after)
                or digest.hexdigest() != expected_digest
            ):
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                raise GovernanceError(f"Copy source changed during publication: {source_relative}")
            _publish_temporary_no_clobber(
                project_root,
                destination_relative.parent,
                directory_fd,
                temporary_name,
                destination_relative.name,
            )


def _escape_markdown_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _markdown_link(label: object, target: str) -> str:
    encoded = quote(target.replace(os.sep, "/"), safe="/.:@+~!$&'*,;=-_")
    return f"[{_escape_markdown_label(label)}](<{encoded}>)"


def _wrapper_text(project_root: Path, action: Dict[str, object]) -> str:
    destination = project_root / str(action["deliverable_path"])
    source = project_root / str(action["source_path"])
    relative_link = os.path.relpath(str(source), str(destination.parent))
    return (
        f"# {_escape_markdown_label(action['title'])}\n\n"
        f"- Status: `{action['status']}`\n"
        f"- Canonical source: {_markdown_link(action['source_path'], relative_link)}\n"
        f"- Managed by rule: `{action['rule']}`\n"
    )


@contextmanager
def _exclusive_lock(project_root: Path, lock_relative: Path = LOCK_RELATIVE):
    parent_relative = lock_relative.parent
    if parent_relative != Path(".codex") or not lock_relative.name:
        raise GovernanceError(f"Unsafe lock path: {lock_relative}")
    lock = project_root / lock_relative
    with _open_directory_chain(project_root, parent_relative) as directory_fd:
        try:
            descriptor = os.open(
                lock_relative.name,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError as exc:
            raise GovernanceError(f"Another maintenance run holds the lock: {lock}") from exc
        except OSError as exc:
            raise GovernanceError(f"Cannot safely create maintenance lock: {lock}") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()} started={_timestamp()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_fd)
            yield
        finally:
            try:
                os.unlink(lock_relative.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass


def scan_project(project_root: Path, now: Optional[float] = None) -> Dict[str, object]:
    project_root = project_root.resolve()
    with _exclusive_lock(project_root):
        return _scan_project_unlocked(project_root, now=now)


def _validate_plan(project_root: Path, policy: Dict[str, object], plan: Dict[str, object]) -> None:
    if plan.get("version") != 1:
        raise GovernanceError("Plan version must be 1")
    if plan.get("project_root") != str(project_root):
        raise GovernanceError("Plan project root does not match the requested project")
    if plan.get("policy_hash") != _policy_hash(policy):
        raise GovernanceError("Policy changed after the plan was generated; scan again")
    for key in ("promotions", "safe_cache_deletions", "review_candidates", "deferred"):
        if not isinstance(plan.get(key), list):
            raise GovernanceError(f"Plan field {key} must be a list")


def _plans_have_same_actions(left: Dict[str, object], right: Dict[str, object]) -> bool:
    keys = ("promotions", "safe_cache_deletions", "review_candidates", "deferred")
    return all(left.get(key) == right.get(key) for key in keys)


def apply_plan(project_root: Path, plan: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    project_root = project_root.resolve()
    policy = load_policy(project_root)
    if policy["mode"] != "semi-auto":
        raise GovernanceError("Audit mode cannot apply a plan; change policy mode explicitly")
    if plan is None:
        plan = _read_json(project_root / PLAN_RELATIVE, "project file plan")
    _validate_plan(project_root, policy, plan)
    deliverables = _deliverables_path(project_root, policy)
    max_copy_bytes = int(policy["max_copy_bytes"])
    with _exclusive_lock(project_root):
        policy = load_policy(project_root)
        if policy["mode"] != "semi-auto":
            raise GovernanceError("Audit mode cannot apply a plan; change policy mode explicitly")
        _validate_plan(project_root, policy, plan)
        current_plan = _scan_project_unlocked(project_root)
        if not _plans_have_same_actions(plan, current_plan):
            raise GovernanceError("Plan actions no longer match current policy and project state; rescan")
        plan = current_plan
        deliverables = _deliverables_path(project_root, policy)
        max_copy_bytes = int(policy["max_copy_bytes"])
        tracked, dirty = _git_protected_paths(project_root)
        state = _read_state(project_root)
        applied = dict(state.get("applied", {}))
        pending = dict(state.get("pending", {}))
        manifest_path = deliverables / "MANIFEST.csv"
        manifest_relative = manifest_path.relative_to(project_root)
        (
            manifest_text,
            manifest_digest,
            manifest_identity,
        ) = _read_project_text_snapshot(project_root, manifest_relative)
        readme_path = deliverables / "README.md"
        readme_relative = readme_path.relative_to(project_root)
        (
            existing_readme,
            readme_digest,
            readme_identity,
        ) = _read_project_text_snapshot(project_root, readme_relative)
        governance_paths = {
            manifest_path.relative_to(project_root).as_posix(),
            (deliverables / "README.md").relative_to(project_root).as_posix(),
        }
        dirty_governance = sorted(governance_paths.intersection(dirty))
        if dirty_governance:
            raise GovernanceError(
                "Refusing to overwrite dirty governance files: "
                + ", ".join(dirty_governance)
            )
        manifest_rows = _parse_manifest_text(manifest_text, str(manifest_path))
        unmapped_manifest = [row for row in manifest_rows if not row.get("source_path")]
        manifest_by_source = {row["source_path"]: row for row in manifest_rows if row.get("source_path")}
        manifest_by_destination = {
            row["deliverable_path"]: row.get("source_path", "")
            for row in manifest_rows
            if row.get("deliverable_path")
        }
        prepared_promotions: List[Tuple[Dict[str, object], Path, Path, Dict[str, int]]] = []
        for action_value in plan["promotions"]:
            action = dict(action_value)
            source_relative = str(action["source_path"])
            destination_relative = str(action["deliverable_path"])
            source = project_root / _safe_relative(source_relative, "plan source")
            destination = project_root / _safe_relative(destination_relative, "plan destination")
            if source.is_symlink() or not source.is_file() or not _contained(project_root, source):
                raise GovernanceError(f"Promotion source is unavailable or unsafe: {source_relative}")
            _assert_action_destination(
                project_root,
                deliverables,
                destination,
                action,
                applied,
                pending,
                manifest_by_destination,
            )
            current_fingerprint = _fingerprint(source)
            if not _same_fingerprint(current_fingerprint, action.get("fingerprint")):
                raise GovernanceError(f"Source changed after scan: {source_relative}")
            if destination_relative in dirty:
                raise GovernanceError(f"Refusing to overwrite dirty deliverable: {destination_relative}")
            if action["promotion"] == "copy" and current_fingerprint["size"] > max_copy_bytes:
                raise GovernanceError(f"Copy exceeds policy size limit: {source_relative}")
            if action["promotion"] not in ALLOWED_PROMOTIONS:
                raise GovernanceError(f"Unsupported promotion mode: {action['promotion']}")
            if action["promotion"] == "copy":
                source_digest, hashed_fingerprint, _ = _hash_project_file(
                    project_root, Path(source_relative)
                )
                if (
                    not _same_fingerprint(hashed_fingerprint, current_fingerprint)
                    or source_digest != action.get("source_sha256")
                ):
                    raise GovernanceError(
                        f"Copy source content changed after scan: {source_relative}"
                    )
            if action.get("content_sha256") != _action_content_sha256(
                project_root, action
            ):
                raise GovernanceError(f"Promotion content signature is invalid: {source_relative}")
            if action.get("desired_signature") != _desired_signature(action):
                raise GovernanceError(f"Promotion signature is invalid: {source_relative}")
            prepared_promotions.append((action, source, destination, current_fingerprint))

        protected_globs = list(policy["protected_globs"])
        ignored_globs = list(policy["ignored_globs"])
        dynamic_ignore = f"{policy['deliverables_dir']}/**"
        if dynamic_ignore not in ignored_globs:
            ignored_globs.append(dynamic_ignore)
        prepared_deletions: List[
            Tuple[Path, int, Tuple[int, int, int, int, int, int], str]
        ] = []
        for deletion_value in plan["safe_cache_deletions"]:
            deletion = dict(deletion_value)
            relative = str(deletion["path"])
            path = project_root / _safe_relative(relative, "cache deletion path")
            if not path.exists():
                continue
            if (
                not path.is_file()
                or path.is_symlink()
                or not _contained(project_root, path)
                or not _strict_cache(path)
                or _matches(relative, protected_globs)
                or _matches(relative, ignored_globs)
                or relative in tracked
                or relative in dirty
            ):
                continue
            current_fingerprint = _fingerprint(path)
            if not _same_fingerprint(current_fingerprint, deletion.get("fingerprint")):
                raise GovernanceError(f"Safe-cache candidate changed after scan: {relative}")
            current_digest, hashed_fingerprint, _ = _hash_project_file(
                project_root, Path(relative)
            )
            if (
                not _same_fingerprint(current_fingerprint, hashed_fingerprint)
                or current_digest != deletion.get("sha256")
            ):
                raise GovernanceError(
                    f"Safe-cache content changed after scan: {relative}"
                )
            prepared_deletions.append(
                (
                    path,
                    current_fingerprint["size"],
                    _file_identity(path),
                    current_digest,
                )
            )

        promoted = 0
        for action, source, destination, current_fingerprint in prepared_promotions:
            source_relative = str(action["source_path"])
            destination_relative = str(action["deliverable_path"])
            _assert_action_destination(
                project_root,
                deliverables,
                destination,
                action,
                applied,
                pending,
                manifest_by_destination,
            )
            if action.get("write_required") is True:
                pending[source_relative] = {
                    "deliverable_path": destination_relative,
                    "content_sha256": action["content_sha256"],
                    "desired_signature": action["desired_signature"],
                    "fingerprint": action["fingerprint"],
                }
                state["pending"] = pending
                _write_json(project_root / STATE_RELATIVE, state)
                if action["promotion"] == "copy":
                    _copy_new_no_clobber(
                        project_root,
                        Path(source_relative),
                        destination,
                        current_fingerprint,
                        str(action["source_sha256"]),
                    )
                elif action["promotion"] == "wrapper":
                    _write_new_text_no_clobber(
                        project_root,
                        destination,
                        _wrapper_text(project_root, action),
                    )
            destination_digest, destination_fingerprint, _ = _hash_project_file(
                project_root, Path(destination_relative)
            )
            if destination_digest != action["content_sha256"]:
                raise GovernanceError(
                    f"Published destination checksum mismatch: {destination_relative}"
                )
            row = {
                "id": str(action["id"]),
                "category": str(action["category"]),
                "title": str(action["title"]),
                "date": str(action["date"]),
                "status": str(action["status"]),
                "deliverable_path": destination_relative,
                "source_path": source_relative,
                "notes": f"Managed by {action['rule']}; canonical source preserved",
            }
            manifest_by_source[source_relative] = row
            applied[source_relative] = {
                **current_fingerprint,
                "deliverable_path": destination_relative,
                "desired_signature": action["desired_signature"],
                "destination_size": destination_fingerprint["size"],
                "destination_sha256": destination_digest,
                "applied_at": _timestamp(),
            }
            pending.pop(source_relative, None)
            state["applied"] = applied
            state["pending"] = pending
            _write_json(project_root / STATE_RELATIVE, state)
            promoted += 1

        ordered_manifest = [
            *unmapped_manifest,
            *sorted(
                manifest_by_source.values(),
                key=lambda row: (row["category"], row["id"]),
            ),
        ]
        new_manifest_text = _csv_text(MANIFEST_FIELDS, ordered_manifest)
        new_readme_text = _managed_readme(
            existing_readme,
            ordered_manifest,
            project_root,
            readme_path,
        )
        state["governance_pending"] = {
            "version": 1,
            "files": [
                {
                    "path": manifest_relative.as_posix(),
                    "old_text": manifest_text,
                    "new_text": new_manifest_text,
                },
                {
                    "path": readme_relative.as_posix(),
                    "old_text": existing_readme,
                    "new_text": new_readme_text,
                },
            ],
        }
        _write_json(project_root / STATE_RELATIVE, state)
        _rewrite_owned_text(
            project_root,
            manifest_relative,
            new_manifest_text,
            manifest_digest,
            manifest_identity,
        )
        _rewrite_owned_text(
            project_root,
            readme_relative,
            new_readme_text,
            readme_digest,
            readme_identity,
        )
        state["governance_pending"] = None
        _write_json(project_root / STATE_RELATIVE, state)

        deleted = 0
        reclaimed = 0
        for path, size, expected_identity, expected_digest in prepared_deletions:
            try:
                relative_path = path.relative_to(project_root)
            except ValueError as exc:
                raise GovernanceError(f"Cache deletion escaped the project: {path}") from exc
            _guarded_path(project_root, relative_path, "cache deletion path")
            if (
                not _contained(project_root, path)
                or not _strict_cache(path)
                or _file_identity(path) != expected_identity
            ):
                raise GovernanceError(f"Safe-cache candidate changed before deletion: {path}")
            _safe_unlink_cache(
                project_root,
                relative_path,
                expected_identity,
                expected_digest,
                policy,
            )
            if path.parent.name == "__pycache__":
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            deleted += 1
            reclaimed += size

        candidate_path = deliverables / "CLEANUP_CANDIDATES.csv"
        retained_candidates: List[Dict[str, str]] = []
        for row in _read_candidate_rows(candidate_path):
            try:
                candidate = project_root / _safe_relative(row["path"], "candidate path")
            except GovernanceError:
                retained_candidates.append(row)
                continue
            if candidate.exists():
                retained_candidates.append(row)
        _write_csv(candidate_path, CANDIDATE_FIELDS, retained_candidates)

        state["applied"] = applied
        state["pending"] = pending
        state["last_apply"] = _timestamp()
        _write_json(project_root / STATE_RELATIVE, state)
        summary = {
            "mode": policy["mode"],
            "promoted": promoted,
            "deleted_safe_cache": deleted,
            "reclaimed_bytes": reclaimed,
            "review_required": len(plan["review_candidates"]),
            "deferred": len(plan["deferred"]),
        }
        _atomic_write_text(
            deliverables / "MAINTENANCE_STATUS.md",
            _status_text(plan, str(policy["mode"]))
            + f"- Applied promotions: `{promoted}`\n"
            + f"- Deleted strict safe-cache files: `{deleted}`\n"
            + f"- Reclaimed bytes: `{reclaimed}`\n",
        )
        return summary


def maintain_project(project_root: Path, now: Optional[float] = None) -> Dict[str, object]:
    project_root = project_root.resolve()
    with _exclusive_lock(project_root, MAINTAIN_LOCK_RELATIVE):
        policy = load_policy(project_root)
        plan = scan_project(project_root, now=now)
        if policy["mode"] == "audit":
            return {
                "mode": "audit",
                "promoted": 0,
                "deleted_safe_cache": 0,
                "reclaimed_bytes": 0,
                "review_required": len(plan["review_candidates"]),
                "deferred": len(plan["deferred"]),
            }
        return apply_plan(project_root, plan=plan)


def project_status(project_root: Path) -> Dict[str, object]:
    project_root = project_root.resolve()
    policy = load_policy(project_root)
    state = _read_state(project_root)
    plan_path = project_root / PLAN_RELATIVE
    plan = _read_json(plan_path, "project file plan") if plan_path.exists() else None
    if plan is not None:
        _validate_plan(project_root, policy, plan)
    return {
        "mode": policy["mode"],
        "last_scan": state.get("last_scan"),
        "last_apply": state.get("last_apply"),
        "proposed_promotions": len(plan["promotions"]) if plan else 0,
        "safe_cache_deletions": len(plan["safe_cache_deletions"]) if plan else 0,
        "review_candidates": len(plan["review_candidates"]) if plan else 0,
        "deferred": len(plan["deferred"]) if plan else 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "apply", "maintain", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("--project-root", type=Path, default=Path.cwd())
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    init_parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), default="audit")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "init":
            result = initialize_project(args.project_root, mode=args.mode)
        elif args.command == "scan":
            result = scan_project(args.project_root)
        elif args.command == "apply":
            result = apply_plan(args.project_root)
        elif args.command == "maintain":
            result = maintain_project(args.project_root)
        else:
            result = project_status(args.project_root)
    except GovernanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
