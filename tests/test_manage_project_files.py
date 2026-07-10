import csv
import importlib.util
import json
import os
import stat
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "manage_project_files.py"
SPEC = importlib.util.spec_from_file_location("manage_project_files", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ManageProjectFilesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        # Public manager entry points canonicalize the project root before use.
        self.root = Path(self.tempdir.name).resolve()

    def tearDown(self):
        self.tempdir.cleanup()

    def initialize(self, mode="semi-auto"):
        MODULE.initialize_project(self.root, mode=mode)

    def update_policy(self, **updates):
        path = self.root / ".codex" / "project-files-policy.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy.update(updates)
        path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def add_rule(
        self,
        include,
        *,
        category="report",
        destination="reports",
        promotion="wrapper",
        status="current",
    ):
        path = self.root / ".codex" / "project-files-policy.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy["artifact_rules"] = [
            {
                "name": "test-rule",
                "category": category,
                "include": include,
                "exclude": [],
                "destination": destination,
                "promotion": promotion,
                "status": status,
            }
        ]
        path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_stable_file(self, relative, content="content\n", now=1_800_000_000):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.utime(path, (now - 120, now - 120))
        return path

    def manifest_rows(self):
        with (self.root / "deliverables" / "MANIFEST.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            return list(csv.DictReader(handle))

    def test_initialize_creates_separated_machine_and_human_surfaces(self):
        result = MODULE.initialize_project(self.root, mode="semi-auto")
        self.assertEqual(result["mode"], "semi-auto")
        self.assertTrue((self.root / ".codex/project-files-policy.json").is_file())
        self.assertTrue((self.root / ".codex/project-files-state.json").is_file())
        ignore_text = (self.root / ".codex/.gitignore").read_text(encoding="utf-8")
        self.assertIn("project-files-state.json", ignore_text)
        self.assertIn("project-files-plan.json", ignore_text)
        self.assertIn("project-files.lock", ignore_text)
        self.assertIn("project-files-maintain.lock", ignore_text)
        self.assertNotIn("project-files-policy.json", ignore_text)
        self.assertTrue((self.root / "deliverables/README.md").is_file())
        self.assertTrue((self.root / "deliverables/MANIFEST.csv").is_file())
        self.assertTrue((self.root / "deliverables/CLEANUP_CANDIDATES.csv").is_file())
        self.assertTrue((self.root / "deliverables/MAINTENANCE_STATUS.md").is_file())

    def test_default_policy_has_no_cleanup_roots(self):
        self.initialize()
        policy = json.loads(
            (self.root / ".codex/project-files-policy.json").read_text(encoding="utf-8")
        )
        self.assertEqual(policy["cleanup_roots"], [])
        self.assertEqual(policy["max_cache_delete_bytes"], 20 * 1024 * 1024)

    def test_atomic_governance_writes_use_readable_and_preserved_modes(self):
        self.initialize()
        policy_path = self.root / ".codex/project-files-policy.json"
        manifest_path = self.root / "deliverables/MANIFEST.csv"
        self.assertEqual(stat.S_IMODE(policy_path.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o644)

        policy_path.chmod(0o640)
        MODULE._atomic_write_text(policy_path, "{}\n")
        self.assertEqual(stat.S_IMODE(policy_path.stat().st_mode), 0o640)

    def test_scan_is_plan_only_and_apply_promotes_wrapper(self):
        self.initialize()
        self.add_rule(["reports/**/*.md"])
        source = self.write_stable_file("reports/final/report.md")

        plan = MODULE.scan_project(self.root, now=1_800_000_000)

        self.assertEqual(len(plan["promotions"]), 1)
        deliverable = self.root / plan["promotions"][0]["deliverable_path"]
        self.assertFalse(deliverable.exists())
        summary = MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual(summary["promoted"], 1)
        self.assertTrue(deliverable.is_file())
        self.assertEqual(self.manifest_rows()[0]["source_path"], source.relative_to(self.root).as_posix())

    def test_maintain_copies_lightweight_figure_and_is_idempotent(self):
        self.initialize()
        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        self.write_stable_file("results/figures/model.svg", "<svg/>\n")

        first = MODULE.maintain_project(self.root, now=1_800_000_000)
        second = MODULE.maintain_project(self.root, now=1_800_000_100)

        self.assertEqual(first["promoted"], 1)
        self.assertEqual(second["promoted"], 0)
        self.assertEqual(len(self.manifest_rows()), 1)
        self.assertEqual(
            (self.root / "deliverables/figures/model.svg").read_text(encoding="utf-8"),
            "<svg/>\n",
        )

    def test_copy_reconciles_content_change_with_same_size_and_mtime(self):
        self.initialize()
        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        source = self.write_stable_file("results/figures/model.svg", "AAAA\n")
        original_mtime_ns = source.stat().st_mtime_ns
        MODULE.maintain_project(self.root, now=1_800_000_000)

        source.write_text("BBBB\n", encoding="utf-8")
        os.utime(source, ns=(original_mtime_ns, original_mtime_ns))
        summary = MODULE.maintain_project(self.root, now=1_800_000_100)

        self.assertEqual(summary["promoted"], 1)
        destination = self.root / self.manifest_rows()[0]["deliverable_path"]
        self.assertEqual(destination.read_text(encoding="utf-8"), "BBBB\n")

    def test_copy_refuses_source_replaced_after_preflight(self):
        self.initialize()
        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        source = self.write_stable_file("results/figures/model.svg", "AAAA\n")
        original_mtime_ns = source.stat().st_mtime_ns
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destination = self.root / plan["promotions"][0]["deliverable_path"]
        original_copy = MODULE._copy_new_no_clobber

        def replace_then_copy(*args, **kwargs):
            source.write_text("BBBB\n", encoding="utf-8")
            os.utime(source, ns=(original_mtime_ns, original_mtime_ns))
            return original_copy(*args, **kwargs)

        with mock.patch.object(
            MODULE, "_copy_new_no_clobber", side_effect=replace_then_copy
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertFalse(destination.exists())

    def test_semi_auto_deletes_only_strict_untracked_cache(self):
        self.initialize()
        self.update_policy(cleanup_roots=["."])
        safe_ds = self.write_stable_file("build/.DS_Store")
        safe_pyc = self.write_stable_file("build/__pycache__/module.pyc")
        log = self.write_stable_file("build/job.log")
        protected = self.write_stable_file("raw/.DS_Store")

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["deleted_safe_cache"], 2)
        self.assertFalse(safe_ds.exists())
        self.assertFalse(safe_pyc.exists())
        self.assertTrue(log.exists())
        self.assertTrue(protected.exists())
        candidates = (self.root / "deliverables/CLEANUP_CANDIDATES.csv").read_text(
            encoding="utf-8"
        )
        self.assertIn("build/job.log", candidates)
        self.assertNotIn("build/.DS_Store", candidates)
        self.assertNotIn("build/__pycache__/module.pyc", candidates)

    def test_cache_tracked_after_preflight_is_restored(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/x.pyc")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_unlink = MODULE._safe_unlink_cache

        def track_then_unlink(project_root, relative, identity, digest, policy):
            subprocess.run(["git", "add", str(relative)], cwd=self.root, check=True)
            return original_unlink(project_root, relative, identity, digest, policy)

        with mock.patch.object(
            MODULE, "_safe_unlink_cache", side_effect=track_then_unlink
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertTrue(cache.is_file())
        tracked = subprocess.run(
            ["git", "ls-files", "build/x.pyc"],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        self.assertEqual(tracked.strip(), "build/x.pyc")

    def test_cache_replaced_at_final_delete_is_preserved(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/x.pyc", "old cache\n")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_unlink = MODULE._safe_unlink_cache

        def replace_then_unlink(project_root, relative, identity, digest, policy):
            cache.unlink()
            cache.write_text("new replacement\n", encoding="utf-8")
            return original_unlink(project_root, relative, identity, digest, policy)

        with mock.patch.object(
            MODULE, "_safe_unlink_cache", side_effect=replace_then_unlink
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(cache.read_text(encoding="utf-8"), "new replacement\n")

    def test_cache_parent_symlink_swap_cannot_delete_outside(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/x.pyc")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        build = self.root / "build"
        original_build = self.root / "build-original"
        outside = self.root.parent / f"{self.root.name}-cache-outside"
        outside.mkdir()
        outside_cache = outside / "x.pyc"
        outside_cache.write_text("outside data\n", encoding="utf-8")
        original_unlink = MODULE._safe_unlink_cache

        def swap_then_unlink(project_root, relative, identity, digest, policy):
            build.rename(original_build)
            build.symlink_to(outside, target_is_directory=True)
            return original_unlink(project_root, relative, identity, digest, policy)

        try:
            with mock.patch.object(
                MODULE, "_safe_unlink_cache", side_effect=swap_then_unlink
            ):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertEqual(outside_cache.read_text(encoding="utf-8"), "outside data\n")
        finally:
            if build.is_symlink():
                build.unlink()
            if original_build.exists():
                original_build.rename(build)
            outside_cache.unlink()
            outside.rmdir()

    def test_cache_parent_moved_during_final_unlink_is_restored(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/x.pyc", "cache data\n")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        build = self.root / "build"
        moved = self.root.parent / f"{self.root.name}-moved-build"
        outside = self.root.parent / f"{self.root.name}-late-cache-outside"
        outside.mkdir()
        outside_cache = outside / "x.pyc"
        outside_cache.write_text("outside data\n", encoding="utf-8")
        original_unlink = MODULE.os.unlink
        original_rename = MODULE.os.rename
        swapped = False

        def swap_during_unlink(path, *args, **kwargs):
            nonlocal swapped
            if str(path).startswith(".project-files-delete-") and not swapped:
                original_rename(str(build), str(moved))
                build.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_unlink(path, *args, **kwargs)

        try:
            with mock.patch.object(MODULE.os, "unlink", side_effect=swap_during_unlink):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertEqual(
                (moved / "x.pyc").read_text(encoding="utf-8"), "cache data\n"
            )
            self.assertEqual(outside_cache.read_text(encoding="utf-8"), "outside data\n")
        finally:
            if build.is_symlink():
                build.unlink()
            if moved.exists():
                original_rename(str(moved), str(build))
            outside_cache.unlink()
            outside.rmdir()

    def test_cache_content_changed_inside_final_unlink_is_restored(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/x.pyc", "OLD-CACHE")
        original_mtime_ns = cache.stat().st_mtime_ns
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_unlink = MODULE.os.unlink
        changed = False

        def change_during_unlink(path, *args, **kwargs):
            nonlocal changed
            if str(path).startswith(".project-files-delete-") and not changed:
                directory_fd = kwargs["dir_fd"]
                descriptor = MODULE.os.open(
                    path, MODULE.os.O_WRONLY, dir_fd=directory_fd
                )
                try:
                    MODULE.os.write(descriptor, b"NEW-VALUE")
                    MODULE.os.fsync(descriptor)
                finally:
                    MODULE.os.close(descriptor)
                MODULE.os.utime(
                    path,
                    ns=(original_mtime_ns, original_mtime_ns),
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                changed = True
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(MODULE.os, "unlink", side_effect=change_during_unlink):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(cache.read_text(encoding="utf-8"), "NEW-VALUE")

    def test_large_strict_cache_is_review_only_without_hashing(self):
        self.initialize()
        self.update_policy(
            cleanup_roots=["build"],
            max_cache_delete_bytes=4,
        )
        cache = self.write_stable_file("build/large.pyc", "too large\n")

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["deleted_safe_cache"], 0)
        self.assertEqual(summary["review_required"], 1)
        self.assertTrue(cache.is_file())
        candidates = (
            self.root / "deliverables/CLEANUP_CANDIDATES.csv"
        ).read_text(encoding="utf-8")
        self.assertIn("strict cache exceeds", candidates)

    def test_tracked_cache_is_preserved(self):
        self.initialize()
        self.update_policy(cleanup_roots=["."])
        tracked = self.write_stable_file("tracked.pyc")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "tracked.pyc"], cwd=self.root, check=True)

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["deleted_safe_cache"], 0)
        self.assertTrue(tracked.exists())

    def test_unstable_artifact_is_deferred_until_two_observations(self):
        self.initialize()
        self.update_policy(stability_seconds=60)
        self.add_rule(["reports/*.md"])
        source = self.root / "reports/current.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Current\n", encoding="utf-8")
        os.utime(source, (1_800_000_000, 1_800_000_000))

        first = MODULE.scan_project(self.root, now=1_800_000_010)
        second = MODULE.scan_project(self.root, now=1_800_000_071)

        self.assertEqual(first["promotions"], [])
        self.assertTrue(any(item["reason"] == "unstable" for item in first["deferred"]))
        self.assertEqual(len(second["promotions"]), 1)

    def test_apply_rejects_source_changed_after_scan(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        source = self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        source.write_text("changed\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual(self.manifest_rows(), [])

    def test_duplicate_basenames_receive_distinct_deterministic_destinations(self):
        self.initialize()
        self.add_rule(["analysis*/report.md"])
        self.write_stable_file("analysis-a/report.md")
        self.write_stable_file("analysis-b/report.md")

        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destinations = [item["deliverable_path"] for item in plan["promotions"]]

        self.assertEqual(len(destinations), 2)
        self.assertEqual(len(set(destinations)), 2)
        self.assertTrue(all("report-" in Path(path).stem for path in destinations))

    def test_existing_lock_fails_closed(self):
        self.initialize()
        plan = MODULE.scan_project(self.root)
        lock = self.root / ".codex/project-files.lock"
        lock.write_text("other-run\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)

    def test_scan_refuses_existing_lock(self):
        self.initialize()
        lock = self.root / ".codex/project-files.lock"
        lock.write_text("other-run\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.scan_project(self.root)

    def test_maintain_refuses_existing_maintain_lock(self):
        self.initialize()
        lock = self.root / ".codex/project-files-maintain.lock"
        lock.write_text("other-maintain-run\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.maintain_project(self.root)

    def test_lock_cleanup_uses_pinned_codex_directory(self):
        self.initialize()
        codex = self.root / ".codex"
        original_codex = self.root / ".codex-original"
        outside = self.root.parent / f"{self.root.name}-lock-outside"
        outside.mkdir()
        outside_lock = outside / "project-files.lock"
        outside_lock.write_text("valuable external lock\n", encoding="utf-8")

        try:
            with MODULE._exclusive_lock(self.root):
                codex.rename(original_codex)
                codex.symlink_to(outside, target_is_directory=True)
            self.assertEqual(
                outside_lock.read_text(encoding="utf-8"),
                "valuable external lock\n",
            )
            self.assertFalse((original_codex / "project-files.lock").exists())
        finally:
            if codex.is_symlink():
                codex.unlink()
            if original_codex.exists():
                original_codex.rename(codex)
            outside_lock.unlink()
            outside.rmdir()

    def test_policy_path_traversal_is_rejected(self):
        self.initialize()
        self.update_policy(deliverables_dir="../outside")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.scan_project(self.root)

    def test_initialize_rejects_symlinked_codex_directory(self):
        external = self.root.parent / f"{self.root.name}-external-codex"
        external.mkdir()
        (self.root / ".codex").symlink_to(external, target_is_directory=True)
        self.addCleanup(shutil.rmtree, external, True)

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.initialize_project(self.root, mode="semi-auto")
        self.assertFalse((external / "project-files-policy.json").exists())

    def test_initialize_preserves_concurrent_readme_edit(self):
        self.initialize()
        readme = self.root / "deliverables/README.md"
        original_rewrite = MODULE._rewrite_owned_text
        edited = False

        def edit_then_rewrite(project_root, relative, text, digest, identity):
            nonlocal edited
            if Path(relative) == Path("deliverables/README.md") and not edited:
                readme.write_text("valuable concurrent edit\n", encoding="utf-8")
                edited = True
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        with mock.patch.object(
            MODULE, "_rewrite_owned_text", side_effect=edit_then_rewrite
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.initialize_project(self.root, mode="semi-auto")

        self.assertEqual(
            readme.read_text(encoding="utf-8"), "valuable concurrent edit\n"
        )

    def test_initialize_cas_preserves_edit_after_initial_validation(self):
        self.initialize()
        readme = self.root / "deliverables/README.md"
        original_write_all = MODULE._write_all
        edited = False

        def edit_during_temp_write(descriptor, data):
            nonlocal edited
            if not edited:
                readme.write_text("valuable late edit\n", encoding="utf-8")
                edited = True
            return original_write_all(descriptor, data)

        with mock.patch.object(
            MODULE, "_write_all", side_effect=edit_during_temp_write
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.initialize_project(self.root, mode="semi-auto")

        self.assertEqual(readme.read_text(encoding="utf-8"), "valuable late edit\n")

    def test_initialize_keyboard_interrupt_removes_governance_temp(self):
        self.initialize()
        original_readme = (self.root / "deliverables/README.md").read_text(
            encoding="utf-8"
        )

        with mock.patch.object(
            MODULE, "_write_all", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt):
                MODULE.initialize_project(self.root, mode="semi-auto")

        self.assertEqual(
            (self.root / "deliverables/README.md").read_text(encoding="utf-8"),
            original_readme,
        )
        self.assertEqual(
            list((self.root / "deliverables").glob(".project-files-governance-*.tmp")),
            [],
        )
        MODULE.initialize_project(self.root, mode="semi-auto")

    def test_initialize_refuses_existing_operation_lock(self):
        self.initialize()
        lock = self.root / ".codex/project-files.lock"
        lock.write_text("other initialization\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.initialize_project(self.root, mode="semi-auto")

    def test_scan_rejects_symlinked_deliverables_directory(self):
        self.initialize()
        external_root = self.root.parent / f"{self.root.name}-external-deliverables"
        external = external_root / "deliverables"
        external_root.mkdir()
        (self.root / "deliverables").rename(external)
        (self.root / "deliverables").symlink_to(external, target_is_directory=True)
        self.addCleanup(shutil.rmtree, external_root, True)

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.scan_project(self.root)

    def test_apply_rejects_deliverables_replaced_by_symlink(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        external_root = self.root.parent / f"{self.root.name}-external-apply"
        external = external_root / "deliverables"
        external_root.mkdir()
        (self.root / "deliverables").rename(external)
        (self.root / "deliverables").symlink_to(external, target_is_directory=True)
        self.addCleanup(shutil.rmtree, external_root, True)

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)

    def test_reserved_deliverables_directory_is_rejected(self):
        self.initialize()
        self.update_policy(deliverables_dir=".codex/output")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.scan_project(self.root)

    def test_artifact_glob_path_traversal_is_rejected(self):
        self.initialize()
        self.add_rule(["../*.md"])

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.scan_project(self.root)

    def test_audit_mode_does_not_apply_or_delete(self):
        self.initialize(mode="audit")
        self.update_policy(cleanup_roots=["."])
        self.add_rule(["reports/*.md"])
        source = self.write_stable_file("reports/current.md")
        cache = self.write_stable_file("cache.pyc")

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["promoted"], 0)
        self.assertEqual(summary["deleted_safe_cache"], 0)
        self.assertTrue(source.exists())
        self.assertTrue(cache.exists())
        self.assertEqual(self.manifest_rows(), [])

    def test_nested_git_repository_is_pruned_from_cleanup(self):
        self.initialize()
        self.update_policy(cleanup_roots=["vendor"])
        nested = self.root / "vendor"
        nested.mkdir()
        tracked = nested / "tracked.pyc"
        tracked.write_text("nested repo artifact\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
        subprocess.run(["git", "add", "tracked.pyc"], cwd=nested, check=True)

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["deleted_safe_cache"], 0)
        self.assertTrue(tracked.exists())

    def test_cleanup_root_inside_nested_git_repository_is_pruned(self):
        self.initialize()
        nested = self.root / "vendor/repo"
        build = nested / "build"
        build.mkdir(parents=True)
        tracked = build / "tracked.pyc"
        tracked.write_text("nested tracked cache\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
        subprocess.run(["git", "add", "build/tracked.pyc"], cwd=nested, check=True)
        self.update_policy(cleanup_roots=["vendor/repo/build"])

        summary = MODULE.maintain_project(self.root, now=1_800_000_000)

        self.assertEqual(summary["deleted_safe_cache"], 0)
        self.assertTrue(tracked.exists())

    def test_invalid_git_metadata_fails_closed(self):
        self.initialize()
        self.update_policy(cleanup_roots=["."])
        (self.root / ".git").write_text("not valid git metadata\n", encoding="utf-8")
        cache = self.write_stable_file("cache.pyc")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.maintain_project(self.root, now=1_800_000_000)
        self.assertTrue(cache.exists())

    def test_missing_git_binary_fails_with_governance_error(self):
        self.initialize()

        with mock.patch.object(
            MODULE.subprocess, "run", side_effect=FileNotFoundError("git")
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.scan_project(self.root, now=1_800_000_000)

    def test_status_reports_invalid_plan_as_governance_error(self):
        self.initialize()
        plan_path = self.root / ".codex/project-files-plan.json"
        plan_path.write_text('{"version": 1}\n', encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.project_status(self.root)

    def test_apply_refuses_audit_mode_plan(self):
        self.initialize(mode="audit")
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual(self.manifest_rows(), [])

    def test_apply_preserves_manual_manifest_rows(self):
        self.initialize()
        manual = self.root / "deliverables/reports/manual.md"
        manual.write_text("# Manual\n", encoding="utf-8")
        with (self.root / "deliverables/MANIFEST.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=MODULE.MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow(
                {
                    "id": "R-MANUAL",
                    "category": "report",
                    "title": "Manual navigation report",
                    "date": "2026-07-10",
                    "status": "current",
                    "deliverable_path": "deliverables/reports/manual.md",
                    "source_path": "",
                    "notes": "User-maintained",
                }
            )
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")

        MODULE.maintain_project(self.root, now=1_800_000_000)

        rows = self.manifest_rows()
        self.assertEqual({row["id"] for row in rows}, {"R-MANUAL", rows[1]["id"]})
        self.assertTrue(any(row["id"] == "R-MANUAL" for row in rows))

    def test_new_source_does_not_overwrite_destination_owned_by_other_source(self):
        self.initialize()
        existing = self.root / "deliverables/reports/report.md"
        existing.write_text("# Existing\n", encoding="utf-8")
        with (self.root / "deliverables/MANIFEST.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=MODULE.MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow(
                {
                    "id": "R-OLD",
                    "category": "report",
                    "title": "Existing",
                    "date": "2026-07-10",
                    "status": "current",
                    "deliverable_path": "deliverables/reports/report.md",
                    "source_path": "old/report.md",
                    "notes": "Existing owner",
                }
            )
        self.add_rule(["new/report.md"])
        self.write_stable_file("new/report.md")

        plan = MODULE.scan_project(self.root, now=1_800_000_000)

        self.assertNotEqual(plan["promotions"][0]["deliverable_path"], "deliverables/reports/report.md")
        self.assertEqual(existing.read_text(encoding="utf-8"), "# Existing\n")

    def test_apply_refuses_unmanaged_destination_created_after_scan(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destination = self.root / plan["promotions"][0]["deliverable_path"]
        destination.write_text("manual file created after scan\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "manual file created after scan\n",
        )

    def test_apply_rejects_action_not_derived_from_policy(self):
        self.initialize()
        source = self.write_stable_file("reports/secret.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        fingerprint = MODULE._fingerprint(source)
        plan["promotions"].append(
            {
                "id": "R-FORGED",
                "category": "report",
                "title": "Forged",
                "date": "2026-07-10",
                "status": "current",
                "source_path": "reports/secret.md",
                "deliverable_path": "deliverables/reports/forged.md",
                "promotion": "wrapper",
                "fingerprint": fingerprint,
                "rule": "missing-rule",
            }
        )

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertFalse((self.root / "deliverables/reports/forged.md").exists())

    def test_policy_status_change_reconciles_existing_manifest(self):
        self.initialize()
        self.add_rule(["reports/*.md"], status="current")
        self.write_stable_file("reports/current.md")
        MODULE.maintain_project(self.root, now=1_800_000_000)
        self.add_rule(["reports/*.md"], status="superseded")

        summary = MODULE.maintain_project(self.root, now=1_800_000_100)

        self.assertEqual(summary["promoted"], 1)
        self.assertEqual(self.manifest_rows()[0]["status"], "superseded")

    def test_copy_wrapper_policy_switch_uses_compatible_extensions(self):
        self.initialize()
        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        self.write_stable_file("results/figures/model.svg", "<svg/>\n")
        MODULE.maintain_project(self.root, now=1_800_000_000)

        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="wrapper",
        )
        wrapper_summary = MODULE.maintain_project(self.root, now=1_800_000_100)
        wrapper_path = self.root / self.manifest_rows()[0]["deliverable_path"]

        self.assertEqual(wrapper_summary["promoted"], 1)
        self.assertEqual(wrapper_path.suffix, ".md")
        self.assertIn("Canonical source", wrapper_path.read_text(encoding="utf-8"))

        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        copy_summary = MODULE.maintain_project(self.root, now=1_800_000_200)
        copy_path = self.root / self.manifest_rows()[0]["deliverable_path"]

        self.assertEqual(copy_summary["promoted"], 1)
        self.assertEqual(copy_path.suffix, ".svg")
        self.assertEqual(copy_path.read_text(encoding="utf-8"), "<svg/>\n")

    def test_modified_managed_destination_fails_closed(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        MODULE.maintain_project(self.root, now=1_800_000_000)
        row = self.manifest_rows()[0]
        destination = self.root / row["deliverable_path"]
        destination.write_text("manual deliverable edit\n", encoding="utf-8")

        plan = MODULE.scan_project(self.root, now=1_800_000_100)
        self.assertEqual(len(plan["promotions"]), 1)
        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual(destination.read_text(encoding="utf-8"), "manual deliverable edit\n")

    def test_missing_manifest_row_is_repaired(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        MODULE.maintain_project(self.root, now=1_800_000_000)
        with (self.root / "deliverables/MANIFEST.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=MODULE.MANIFEST_FIELDS)
            writer.writeheader()

        summary = MODULE.maintain_project(self.root, now=1_800_000_100)

        self.assertEqual(summary["promoted"], 1)
        self.assertEqual(len(self.manifest_rows()), 1)

    def test_apply_refuses_dirty_manifest_or_readme(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "deliverables/README.md", "deliverables/MANIFEST.csv"], cwd=self.root, check=True)
        (self.root / "deliverables/README.md").write_text("user edit\n", encoding="utf-8")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)
        self.assertEqual((self.root / "deliverables/README.md").read_text(encoding="utf-8"), "user edit\n")

    def test_apply_preflights_every_source_before_writing(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        first = self.write_stable_file("reports/a.md")
        second = self.write_stable_file("reports/b.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        second.write_text("changed\n", encoding="utf-8")

        with self.assertRaises(MODULE.GovernanceError):
            MODULE.apply_plan(self.root, plan=plan)

        destinations = [self.root / item["deliverable_path"] for item in plan["promotions"]]
        self.assertTrue(first.exists())
        self.assertTrue(all(not path.exists() for path in destinations))

    def test_apply_refuses_cache_replaced_after_preflight(self):
        self.initialize()
        self.update_policy(cleanup_roots=["build"])
        cache = self.write_stable_file("build/.DS_Store", "old cache\n")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_rewrite = MODULE._rewrite_owned_text
        replacement_written = False

        def replace_before_delete(project_root, relative, text, digest, identity):
            nonlocal replacement_written
            if (
                Path(relative) == Path("deliverables/README.md")
                and not replacement_written
            ):
                cache.unlink()
                cache.write_text("replacement cache\n", encoding="utf-8")
                replacement_written = True
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        with mock.patch.object(
            MODULE, "_rewrite_owned_text", side_effect=replace_before_delete
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertTrue(cache.is_file())
        self.assertEqual(cache.read_text(encoding="utf-8"), "replacement cache\n")

    def test_late_cache_race_is_recoverable_after_promotion(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.update_policy(cleanup_roots=["build"])
        self.write_stable_file("reports/current.md")
        cache = self.write_stable_file("build/.DS_Store", "old cache\n")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_rewrite = MODULE._rewrite_owned_text
        replacement_written = False

        def replace_before_delete(project_root, relative, text, digest, identity):
            nonlocal replacement_written
            if (
                Path(relative) == Path("deliverables/README.md")
                and not replacement_written
            ):
                cache.unlink()
                cache.write_text("replacement cache\n", encoding="utf-8")
                replacement_written = True
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        with mock.patch.object(
            MODULE, "_rewrite_owned_text", side_effect=replace_before_delete
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        recovered = MODULE.maintain_project(self.root, now=1_800_000_100)
        steady = MODULE.maintain_project(self.root, now=1_800_000_200)

        self.assertEqual(recovered["promoted"], 0)
        self.assertEqual(recovered["deleted_safe_cache"], 1)
        self.assertEqual(steady["promoted"], 0)
        self.assertEqual(len(self.manifest_rows()), 1)

    def test_pending_promotion_is_adopted_after_state_write_failure(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_write_json = MODULE._write_json
        failed_once = False

        def fail_after_publish(path, value):
            nonlocal failed_once
            if (
                Path(path).name == "project-files-state.json"
                and value.get("applied")
                and not value.get("pending")
                and not failed_once
            ):
                failed_once = True
                raise MODULE.GovernanceError("injected post-publish state failure")
            return original_write_json(path, value)

        with mock.patch.object(MODULE, "_write_json", side_effect=fail_after_publish):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        recovered = MODULE.maintain_project(self.root, now=1_800_000_100)
        steady = MODULE.maintain_project(self.root, now=1_800_000_200)

        self.assertEqual(recovered["promoted"], 1)
        self.assertEqual(steady["promoted"], 0)
        self.assertEqual(len(self.manifest_rows()), 1)
        self.assertEqual(
            self.manifest_rows()[0]["deliverable_path"],
            plan["promotions"][0]["deliverable_path"],
        )
        self.assertEqual(
            len(list((self.root / "deliverables/reports").glob("current*.md"))),
            1,
        )

    def test_stale_pending_copy_versions_new_source_content(self):
        self.initialize()
        self.add_rule(
            ["results/figures/*.svg"],
            category="figure",
            destination="figures",
            promotion="copy",
        )
        source = self.write_stable_file("results/figures/model.svg", "AAAA\n")
        original_mtime_ns = source.stat().st_mtime_ns
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_destination = self.root / plan["promotions"][0]["deliverable_path"]
        original_write_json = MODULE._write_json
        failed_once = False

        def fail_after_publish(path, value):
            nonlocal failed_once
            if (
                Path(path).name == "project-files-state.json"
                and value.get("applied")
                and not value.get("pending")
                and not failed_once
            ):
                failed_once = True
                raise MODULE.GovernanceError("injected post-publish state failure")
            return original_write_json(path, value)

        with mock.patch.object(MODULE, "_write_json", side_effect=fail_after_publish):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        source.write_text("BBBB\n", encoding="utf-8")
        os.utime(source, ns=(original_mtime_ns, original_mtime_ns))
        recovered = MODULE.maintain_project(self.root, now=1_800_000_100)
        steady = MODULE.maintain_project(self.root, now=1_800_000_200)
        current_destination = self.root / self.manifest_rows()[0]["deliverable_path"]

        self.assertEqual(recovered["promoted"], 1)
        self.assertEqual(steady["promoted"], 0)
        self.assertNotEqual(current_destination, original_destination)
        self.assertEqual(original_destination.read_text(encoding="utf-8"), "AAAA\n")
        self.assertEqual(current_destination.read_text(encoding="utf-8"), "BBBB\n")

    def test_apply_rechecks_each_destination_immediately_before_write(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/a.md")
        self.write_stable_file("reports/b.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destinations = {
            item["source_path"]: self.root / item["deliverable_path"]
            for item in plan["promotions"]
        }
        first = destinations["reports/a.md"]
        second = destinations["reports/b.md"]
        original_new_text = MODULE._write_new_text_no_clobber
        manual_created = False

        def create_conflict_after_preflight(path, text):
            nonlocal manual_created
            if Path(path).resolve() == first.resolve() and not manual_created:
                second.parent.mkdir(parents=True, exist_ok=True)
                second.write_text("manual destination\n", encoding="utf-8")
                manual_created = True
            return original_new_text(self.root.resolve(), Path(path), text)

        with mock.patch.object(
            MODULE,
            "_write_new_text_no_clobber",
            side_effect=lambda project_root, path, text: create_conflict_after_preflight(
                path, text
            ),
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(second.read_text(encoding="utf-8"), "manual destination\n")

    def test_new_destination_publish_is_atomic_no_clobber(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destination = self.root / plan["promotions"][0]["deliverable_path"]
        original_new_text = MODULE._write_new_text_no_clobber

        def create_same_destination_then_publish(project_root, path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("manual destination\n", encoding="utf-8")
            return original_new_text(project_root, path, text)

        with mock.patch.object(
            MODULE,
            "_write_new_text_no_clobber",
            side_effect=create_same_destination_then_publish,
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(destination.read_text(encoding="utf-8"), "manual destination\n")

    def test_destination_parent_symlink_swap_cannot_escape_project(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destination = self.root / plan["promotions"][0]["deliverable_path"]
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        original_parent = destination.parent.with_name(destination.parent.name + "-original")
        original_new_text = MODULE._write_new_text_no_clobber

        def swap_parent_then_publish(project_root, path, text):
            path.parent.rename(original_parent)
            path.parent.symlink_to(outside, target_is_directory=True)
            return original_new_text(project_root, path, text)

        try:
            with mock.patch.object(
                MODULE,
                "_write_new_text_no_clobber",
                side_effect=swap_parent_then_publish,
            ):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertFalse((outside / destination.name).exists())
        finally:
            if destination.parent.is_symlink():
                destination.parent.unlink()
            if original_parent.exists():
                original_parent.rename(destination.parent)
            outside.rmdir()

    def test_destination_parent_moved_during_publish_leaves_no_artifact(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        destination = self.root / plan["promotions"][0]["deliverable_path"]
        parent = destination.parent
        moved = self.root.parent / f"{self.root.name}-moved-reports"
        outside = self.root.parent / f"{self.root.name}-publish-outside"
        outside.mkdir()
        original_link = MODULE.os.link
        original_rename = MODULE.os.rename
        swapped = False

        def swap_during_link(source, target, *args, **kwargs):
            nonlocal swapped
            if str(target) == destination.name and not swapped:
                original_rename(str(parent), str(moved))
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_link(source, target, *args, **kwargs)

        try:
            with mock.patch.object(MODULE.os, "link", side_effect=swap_during_link):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertFalse((moved / destination.name).exists())
            self.assertFalse((outside / destination.name).exists())
        finally:
            if parent.is_symlink():
                parent.unlink()
            if moved.exists():
                original_rename(str(moved), str(parent))
            outside.rmdir()

    def test_governance_parent_symlink_swap_cannot_escape_project(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        outside = self.root.parent / f"{self.root.name}-governance-outside"
        outside.mkdir()
        deliverables = self.root / "deliverables"
        original_deliverables = self.root / "deliverables-original"
        original_rewrite = MODULE._rewrite_owned_text
        swapped = False

        def swap_then_rewrite(project_root, relative, text, digest, identity):
            nonlocal swapped
            if Path(relative) == Path("deliverables/MANIFEST.csv") and not swapped:
                deliverables.rename(original_deliverables)
                deliverables.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        try:
            with mock.patch.object(
                MODULE, "_rewrite_owned_text", side_effect=swap_then_rewrite
            ):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertFalse((outside / "MANIFEST.csv").exists())
            self.assertFalse((outside / "README.md").exists())
        finally:
            if deliverables.is_symlink():
                deliverables.unlink()
            if original_deliverables.exists():
                original_deliverables.rename(deliverables)
            outside.rmdir()

    def test_governance_parent_moved_during_exchange_is_rolled_back(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        deliverables = self.root / "deliverables"
        manifest = deliverables / "MANIFEST.csv"
        original_manifest = manifest.read_text(encoding="utf-8")
        moved = self.root.parent / f"{self.root.name}-moved-deliverables"
        outside = self.root.parent / f"{self.root.name}-exchange-outside"
        outside.mkdir()
        original_exchange = MODULE._atomic_exchange
        original_rename = MODULE.os.rename
        swapped = False

        def exchange_then_move(directory_fd, first_name, second_name):
            nonlocal swapped
            original_exchange(directory_fd, first_name, second_name)
            if second_name == "MANIFEST.csv" and not swapped:
                original_rename(str(deliverables), str(moved))
                deliverables.symlink_to(outside, target_is_directory=True)
                swapped = True

        try:
            with mock.patch.object(
                MODULE, "_atomic_exchange", side_effect=exchange_then_move
            ):
                with self.assertRaises(MODULE.GovernanceError):
                    MODULE.apply_plan(self.root, plan=plan)
            self.assertEqual(
                (moved / "MANIFEST.csv").read_text(encoding="utf-8"),
                original_manifest,
            )
            self.assertFalse((outside / "MANIFEST.csv").exists())
        finally:
            if deliverables.is_symlink():
                deliverables.unlink()
            if moved.exists():
                original_rename(str(moved), str(deliverables))
            outside.rmdir()

    def test_governance_atomic_replace_after_exchange_is_preserved(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        manifest = self.root / "deliverables/MANIFEST.csv"
        original_exchange = MODULE._atomic_exchange
        replaced = False

        def exchange_then_replace(directory_fd, first_name, second_name):
            nonlocal replaced
            original_exchange(directory_fd, first_name, second_name)
            if second_name == "MANIFEST.csv" and not replaced:
                concurrent = manifest.with_name("MANIFEST.concurrent")
                concurrent.write_text("valuable atomic replacement\n", encoding="utf-8")
                os.replace(concurrent, manifest)
                replaced = True

        with mock.patch.object(
            MODULE, "_atomic_exchange", side_effect=exchange_then_replace
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(
            manifest.read_text(encoding="utf-8"),
            "valuable atomic replacement\n",
        )

    def test_keyboard_interrupt_after_exchange_rolls_back_without_temp(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        manifest = self.root / "deliverables/MANIFEST.csv"
        original_manifest = manifest.read_text(encoding="utf-8")
        original_exchange = MODULE._atomic_exchange
        interrupted = False

        def exchange_then_interrupt(directory_fd, first_name, second_name):
            nonlocal interrupted
            original_exchange(directory_fd, first_name, second_name)
            if second_name == "MANIFEST.csv" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with mock.patch.object(
            MODULE, "_atomic_exchange", side_effect=exchange_then_interrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(manifest.read_text(encoding="utf-8"), original_manifest)
        self.assertEqual(
            list((self.root / "deliverables").glob(".project-files-governance-*.tmp")),
            [],
        )
        recovered = MODULE.maintain_project(self.root, now=1_800_000_100)
        self.assertEqual(recovered["promoted"], 0)

    def test_governance_file_replacement_is_not_overwritten(self):
        self.initialize()
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        manifest = self.root / "deliverables/MANIFEST.csv"
        original_rewrite = MODULE._rewrite_owned_text
        replaced = False

        def replace_then_rewrite(project_root, relative, text, digest, identity):
            nonlocal replaced
            if Path(relative) == Path("deliverables/MANIFEST.csv") and not replaced:
                manifest.unlink()
                manifest.write_text("manual manifest\n", encoding="utf-8")
                replaced = True
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        with mock.patch.object(
            MODULE, "_rewrite_owned_text", side_effect=replace_then_rewrite
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        self.assertEqual(manifest.read_text(encoding="utf-8"), "manual manifest\n")

    def test_interrupted_governance_rewrite_recovers_manual_rows(self):
        self.initialize()
        manual = self.root / "deliverables/reports/manual.md"
        manual.write_text("# Manual\n", encoding="utf-8")
        manifest = self.root / "deliverables/MANIFEST.csv"
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MODULE.MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow(
                {
                    "id": "R-MANUAL",
                    "category": "report",
                    "title": "Manual",
                    "date": "2026-07-10",
                    "status": "current",
                    "deliverable_path": "deliverables/reports/manual.md",
                    "source_path": "",
                    "notes": "User-maintained",
                }
            )
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/current.md")
        plan = MODULE.scan_project(self.root, now=1_800_000_000)
        original_rewrite = MODULE._rewrite_owned_text
        interrupted = False

        def interrupt_manifest(project_root, relative, text, digest, identity):
            nonlocal interrupted
            if Path(relative) == Path("deliverables/MANIFEST.csv") and not interrupted:
                interrupted = True
                new_bytes = text.encode("utf-8")
                with manifest.open("r+b") as handle:
                    handle.write(new_bytes[: max(1, len(new_bytes) // 2)])
                    handle.flush()
                    os.fsync(handle.fileno())
                raise MODULE.GovernanceError("injected interrupted manifest rewrite")
            return original_rewrite(
                project_root, relative, text, digest, identity
            )

        with mock.patch.object(
            MODULE, "_rewrite_owned_text", side_effect=interrupt_manifest
        ):
            with self.assertRaises(MODULE.GovernanceError):
                MODULE.apply_plan(self.root, plan=plan)

        recovered = MODULE.maintain_project(self.root, now=1_800_000_100)
        rows = self.manifest_rows()

        self.assertEqual(recovered["promoted"], 0)
        self.assertEqual({row["id"] for row in rows}, {"R-MANUAL", plan["promotions"][0]["id"]})

    def test_special_filename_and_nested_deliverables_validate(self):
        self.initialize()
        self.update_policy(deliverables_dir="docs/deliverables")
        MODULE.initialize_project(self.root, mode="semi-auto")
        self.add_rule(["reports/*.md"])
        self.write_stable_file("reports/final #report? (v1).md")

        MODULE.maintain_project(self.root, now=1_800_000_000)
        result = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / "scripts/validate_deliverables.py"),
                "--project-root",
                str(self.root),
                "--deliverables-name",
                "docs/deliverables",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
