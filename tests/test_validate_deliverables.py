import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_deliverables.py"
SPEC = importlib.util.spec_from_file_location("validate_deliverables", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ValidateDeliverablesRegressionTests(unittest.TestCase):
    def test_runtime_log_in_deliverables_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            deliverables = root / "deliverables"
            for name in ("reports", "figures", "tables", "archive"):
                (deliverables / name).mkdir(parents=True, exist_ok=True)
            (deliverables / "MANIFEST.csv").write_text(
                "id,category,title,date,status,deliverable_path,source_path,notes\n",
                encoding="utf-8",
            )
            (deliverables / "reports/job.log").write_text("runtime\n", encoding="utf-8")

            errors = MODULE.validate_deliverables(root)

            self.assertTrue(any("Forbidden artifact" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
