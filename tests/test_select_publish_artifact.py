from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "select-publish-artifact.py"


def load_selector():
    spec = importlib.util.spec_from_file_location("publish_artifact", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load publish artifact selector")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PublishArtifactSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.selector = load_selector()

    def test_selects_only_the_terminal_artifact_for_the_current_attempt(self) -> None:
        document = {
            "artifacts": [
                {"name": "publish-plan-317-1", "expired": False},
                {"name": "publish-2025.1-rocky-10.2-20.5.0-317-1", "expired": False},
                {"name": "publish-2025.1-rocky-10.2-20.5.0-317-2", "expired": False},
            ]
        }
        selected = self.selector.select_terminal_artifact(
            document,
            run_id="317",
            run_attempt="1",
        )
        self.assertEqual(selected, "publish-2025.1-rocky-10.2-20.5.0-317-1")

    def test_plan_run_has_no_terminal_artifact_and_is_a_noop(self) -> None:
        self.assertIsNone(
            self.selector.select_terminal_artifact(
                {"artifacts": [{"name": "publish-plan-317-1", "expired": False}]},
                run_id="317",
                run_attempt="1",
            )
        )

    def test_duplicate_or_expired_terminal_artifacts_fail_closed(self) -> None:
        with self.assertRaises(self.selector.ArtifactSelectionError):
            self.selector.select_terminal_artifact(
                {
                    "artifacts": [
                        {"name": "publish-one-317-1", "expired": False},
                        {"name": "publish-two-317-1", "expired": False},
                    ]
                },
                run_id="317",
                run_attempt="1",
            )
        with self.assertRaises(self.selector.ArtifactSelectionError):
            self.selector.select_terminal_artifact(
                {"artifacts": [{"name": "publish-one-317-1", "expired": True}]},
                run_id="317",
                run_attempt="1",
            )


if __name__ == "__main__":
    unittest.main()
