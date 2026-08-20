from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "sync-publish-stream-options.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "publish.yml"
MATRIX_PATH = ROOT / "config" / "build-matrix.json"

spec = importlib.util.spec_from_file_location("publish_stream_options", SCRIPT_PATH)
if spec is None or spec.loader is None:  # pragma: no cover - import guard
    raise RuntimeError(f"cannot load {SCRIPT_PATH}")
stream_options = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stream_options)


class PublishStreamOptionsTest(unittest.TestCase):
    def test_repository_workflow_matches_enabled_matrix_streams(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        expected = stream_options.render_stream_options(
            workflow,
            stream_options.enabled_stream_ids(MATRIX_PATH),
        )
        self.assertEqual(workflow, expected)

    def test_write_then_check_updates_only_the_marked_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            workflow_path = temp_path / "publish.yml"
            matrix_path.write_text(
                json.dumps(
                    {
                        "streams": [
                            {"id": "first", "publish_enabled": True},
                            {"id": "disabled", "publish_enabled": False},
                            {"id": "second", "publish_enabled": True},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            workflow_path.write_text(
                "prefix\n"
                "  # BEGIN GENERATED STREAM OPTIONS\n"
                "  - stale\n"
                "  # END GENERATED STREAM OPTIONS\n"
                "suffix\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SCRIPT_PATH),
                "--matrix",
                str(matrix_path),
                "--workflow",
                str(workflow_path),
            ]
            self.assertNotEqual(subprocess.run(command).returncode, 0)
            self.assertEqual(subprocess.run([*command, "--write"]).returncode, 0)
            self.assertEqual(subprocess.run(command).returncode, 0)
            self.assertEqual(
                workflow_path.read_text(encoding="utf-8"),
                "prefix\n"
                "  # BEGIN GENERATED STREAM OPTIONS\n"
                "  - first\n"
                "  - second\n"
                "  # END GENERATED STREAM OPTIONS\n"
                "suffix\n",
            )

    def test_missing_markers_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            stream_options.render_stream_options("options:\n", ["stream"])


if __name__ == "__main__":
    unittest.main()
