from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OWNER = "supergate-hub"
EXPECTED_NAMESPACE = "supergate-hub/kolla-container-images"
PERSONAL_OWNER = "supergate-" + "jhbyun"
SKIP_PARTS = {".git", ".context", "__pycache__"}


class NamespaceTransitionTest(unittest.TestCase):
    def test_matrix_uses_organization_owner(self) -> None:
        matrix = json.loads(
            (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
        )
        self.assertEqual(matrix["owner"], EXPECTED_OWNER)
        self.assertEqual(
            f'{matrix["owner"]}/{matrix["repository"]}',
            EXPECTED_NAMESPACE,
        )

    def test_personal_owner_is_absent_from_repository_content(self) -> None:
        matches: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if PERSONAL_OWNER in content:
                matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
