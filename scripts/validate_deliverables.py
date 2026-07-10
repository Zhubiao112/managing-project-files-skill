#!/usr/bin/env python3
"""Validate a curated project deliverables hub."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import unquote


REQUIRED_COLUMNS = (
    "id",
    "category",
    "title",
    "date",
    "status",
    "deliverable_path",
    "source_path",
    "notes",
)
REQUIRED_DIRECTORIES = ("reports", "figures", "tables", "archive")
ALLOWED_STATUSES = {"current", "limited", "pending", "superseded", "archive"}
FORBIDDEN_SUFFIXES = {
    ".log",
    ".out",
    ".err",
    ".tmp",
    ".bak",
    ".pyc",
    ".jsonl",
    ".dcd",
    ".nc",
    ".mdcrd",
    ".xtc",
    ".trr",
    ".prmtop",
    ".rst7",
    ".wfn",
    ".wfx",
    ".chk",
    ".cube",
    ".gbw",
}
FORBIDDEN_FILENAMES = {".DS_Store"}
FORBIDDEN_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "raw",
    "tmp",
    "cache",
    "logs",
    "scratch",
}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _project_path(project_root: Path, raw_path: str) -> Optional[Path]:
    path = Path(raw_path)
    if path.is_absolute():
        return None
    resolved_root = project_root.resolve()
    resolved_path = (resolved_root / path).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved_path


def _markdown_targets(markdown_file: Path) -> Iterable[str]:
    text = markdown_file.read_text(encoding="utf-8")
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip()
        if target.startswith("<") and ">" in target:
            target = target[1 : target.index(">")]
        elif " \"" in target:
            target = target.split(" \"", 1)[0]
        elif " '" in target:
            target = target.split(" '", 1)[0]
        yield unquote(target)


def validate_deliverables(
    project_root: Path, deliverables_name: str = "deliverables"
) -> List[str]:
    """Return validation errors; return an empty list when the hub is valid."""

    project_root = project_root.resolve()
    deliverables = project_root / deliverables_name
    errors: List[str] = []

    for directory_name in REQUIRED_DIRECTORIES:
        directory = deliverables / directory_name
        if not directory.is_dir():
            errors.append(f"Missing required directory: {deliverables_name}/{directory_name}")

    manifest = deliverables / "MANIFEST.csv"
    if not manifest.is_file():
        errors.append(f"Missing manifest: {deliverables_name}/MANIFEST.csv")
        return errors

    seen_ids = set()
    with manifest.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        missing_columns = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing_columns:
            errors.append(
                "Manifest missing required columns: " + ", ".join(missing_columns)
            )
        else:
            for row_number, row in enumerate(reader, start=2):
                artifact_id = row["id"].strip()
                if artifact_id in seen_ids:
                    errors.append(f"Duplicate manifest ID at row {row_number}: {artifact_id}")
                seen_ids.add(artifact_id)

                status = row["status"].strip()
                if status not in ALLOWED_STATUSES:
                    errors.append(f"Invalid status at row {row_number}: {status}")

                deliverable_raw = row["deliverable_path"].strip()
                deliverable_path = _project_path(project_root, deliverable_raw)
                expected_prefix = deliverables_name.rstrip("/") + "/"
                if (
                    deliverable_path is None
                    or not deliverable_raw.startswith(expected_prefix)
                ):
                    errors.append(
                        f"Invalid deliverable path at row {row_number}: {deliverable_raw}"
                    )
                elif not deliverable_path.exists():
                    errors.append(f"Missing deliverable: {deliverable_raw}")

                source_raw = row["source_path"].strip()
                if source_raw:
                    source_path = _project_path(project_root, source_raw)
                    if source_path is None:
                        errors.append(
                            f"Invalid source path at row {row_number}: {source_raw}"
                        )
                    elif not source_path.exists():
                        errors.append(f"Missing source: {source_raw}")

    if deliverables.is_dir():
        for path in deliverables.rglob("*"):
            relative = path.relative_to(deliverables)
            if any(part in FORBIDDEN_DIRECTORY_NAMES for part in relative.parts):
                errors.append(f"Forbidden artifact path in deliverables: {relative}")
                continue
            if path.is_file() and (
                path.name in FORBIDDEN_FILENAMES
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
            ):
                errors.append(f"Forbidden artifact in deliverables: {relative}")

        for markdown_file in deliverables.rglob("*.md"):
            for target in _markdown_targets(markdown_file):
                if (
                    not target
                    or target.startswith("#")
                    or target.startswith("http://")
                    or target.startswith("https://")
                    or target.startswith("mailto:")
                ):
                    continue
                local_target = target.split("#", 1)[0]
                if not local_target:
                    continue
                resolved_target = (markdown_file.parent / local_target).resolve()
                if not resolved_target.exists():
                    relative_markdown = markdown_file.relative_to(project_root)
                    errors.append(
                        f"Broken Markdown link in {relative_markdown}: {target}"
                    )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root containing the deliverables directory (default: cwd)",
    )
    parser.add_argument(
        "--deliverables-name",
        default="deliverables",
        help="User-facing directory name relative to project root",
    )
    args = parser.parse_args()

    errors = validate_deliverables(args.project_root, args.deliverables_name)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OK: deliverables manifest and links are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
