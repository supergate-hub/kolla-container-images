from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATE_LOCK = ROOT / "scripts" / "generate-lock.py"
PROFILE_PATH = ROOT / "config" / "profiles" / "core.json"


def digest(index: int) -> str:
    return f"sha256:{index:064x}"


def core_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def publish_summary(distro: str = "rocky", distro_version: str = "9") -> dict:
    tag = f"2025.1-{distro}-{distro_version}"
    profile = core_profile()
    return {
        "release": "2025.1",
        "distro": distro,
        "distro_version": distro_version,
        "profile": "core",
        "registry": "ghcr.io",
        "owner": "supergate-jhbyun",
        "repository": "kolla-container-images",
        "images": [
            {
                "image": image["name"],
                "deploy_ref": (
                    "ghcr.io/supergate-jhbyun/kolla-container-images/"
                    f"{image['name']}:{tag}"
                ),
                "manifest_digest": digest(index + 1),
            }
            for index, image in enumerate(profile["images"])
        ],
    }


def generate_lock(summary_path: Path, output_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATE_LOCK),
            "--publish-summary",
            str(summary_path),
            "--profile",
            "core",
            "--release",
            "2025.1",
            "--distro",
            "rocky",
            "--distro-version",
            "9",
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class LockGenerationTest(unittest.TestCase):
    def test_generate_lock_writes_digest_pinned_kolla_ansible_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            summary_path = temp_path / "publish-summary-2025.1-rocky-9.json"
            lock_path = temp_path / "kolla-ansible-image-lock-2025.1-rocky-9.yml"
            summary_path.write_text(
                json.dumps(publish_summary()),
                encoding="utf-8",
            )

            result = generate_lock(summary_path, lock_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            lock = lock_path.read_text(encoding="utf-8")
            self.assertIn(
                'keystone_image_full: "ghcr.io/supergate-jhbyun/'
                "kolla-container-images/keystone:2025.1-rocky-9@"
                "sha256:0000000000000000000000000000000000000000000000000000000000000001"
                '"',
                lock,
            )
            self.assertIn("nova_super_conductor_image_full", lock)
            self.assertIn("nova_conductor_image_full", lock)


if __name__ == "__main__":
    unittest.main()
