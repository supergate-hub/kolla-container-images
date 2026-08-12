from __future__ import annotations

import copy
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.frozen_sources import (
    FrozenSourceError,
    checkout_exact_repository,
    parse_deliverable_pin,
    validate_plan_source_pins,
    verify_exact_checkout,
    verify_installed_kolla,
)
from scripts.profile_resolver import find_stream, load_matrix


ROOT = Path(__file__).resolve().parents[1]
BUILD_UNIT_WORKFLOW = ROOT / ".github" / "workflows" / "build-unit.yml"


def source_plan(matrix: dict, stream_id: str) -> dict:
    stream = find_stream(matrix, stream_id)
    return {
        "stream": stream["id"],
        "release": stream["release"],
        "release_series": stream["release_series"],
        "release_branch": stream["release_branch"],
        "release_metadata": copy.deepcopy(matrix["release_metadata"]),
        "kolla": copy.deepcopy(stream["toolchain"]["kolla"]),
        "kolla_ansible": copy.deepcopy(stream["toolchain"]["kolla_ansible"]),
        "kolla_version": stream["kolla_version"],
        "kolla_ansible_version": stream["kolla_ansible_version"],
    }


class FrozenSourceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_matrix()
        self.plan = source_plan(self.matrix, self.matrix["streams"][0]["id"])

    def test_plan_must_repeat_every_matrix_source_pin_exactly(self) -> None:
        contract = validate_plan_source_pins(self.matrix, self.plan)

        self.assertEqual(contract["release_metadata"], self.matrix["release_metadata"])
        self.assertEqual(contract["kolla"], self.plan["kolla"])
        self.assertEqual(contract["kolla_ansible"], self.plan["kolla_ansible"])

        mutations = (
            ("release_metadata", "commit", "a" * 40),
            ("kolla", "repository", "https://example.invalid/kolla"),
            ("kolla", "commit", "b" * 40),
            ("kolla_ansible", "version", "0.0.0"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                altered = copy.deepcopy(self.plan)
                altered[section][key] = value
                with self.assertRaisesRegex(
                    FrozenSourceError, "does not match the branch matrix pin"
                ):
                    validate_plan_source_pins(self.matrix, altered)

    def test_plan_rejects_missing_extra_and_conflicting_pin_fields(self) -> None:
        missing = copy.deepcopy(self.plan)
        del missing["kolla"]["commit"]
        with self.assertRaisesRegex(FrozenSourceError, "keys must be exactly"):
            validate_plan_source_pins(self.matrix, missing)

        extra = copy.deepcopy(self.plan)
        extra["kolla_ansible"]["ref"] = "stable/2025.1"
        with self.assertRaisesRegex(FrozenSourceError, "keys must be exactly"):
            validate_plan_source_pins(self.matrix, extra)

        conflicting = copy.deepcopy(self.plan)
        conflicting["kolla_version"] = "0.0.0"
        with self.assertRaisesRegex(FrozenSourceError, "conflicts"):
            validate_plan_source_pins(self.matrix, conflicting)


class ReleaseMetadataParserTest(unittest.TestCase):
    def write_metadata(self, directory: Path, text: str) -> Path:
        path = directory / "kolla.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parser_returns_only_the_exact_version_project_hash_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_metadata(
                Path(temp_dir),
                """---
launchpad: kolla
releases:
  - version: 20.3.0
    projects:
      - repo: openstack/kolla
        hash: c3fa85b2e69e13ce07fb54bfb8752754bcc01121
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
branches:
  - name: stable/2025.1
""",
            )

            self.assertEqual(
                parse_deliverable_pin(
                    path,
                    expected_project="openstack/kolla",
                    expected_version="20.4.0",
                ),
                "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5",
            )

    def test_parser_fails_closed_on_ambiguous_or_malformed_metadata(self) -> None:
        cases = {
            "duplicate version": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
""",
            "additional project": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
      - repo: openstack/other
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
            "short hash": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab
""",
            "unsupported key": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
    unknown: true
""",
        }
        for name, document in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                path = self.write_metadata(Path(temp_dir), document)
                with self.assertRaises(FrozenSourceError):
                    parse_deliverable_pin(
                        path,
                        expected_project="openstack/kolla",
                        expected_version="20.4.0",
                    )


class ExactCheckoutTest(unittest.TestCase):
    def git(self, repository: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def test_checkout_fetches_and_detaches_the_exact_full_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            origin = root / "origin"
            origin.mkdir()
            self.git(origin, "init", "--quiet")
            self.git(origin, "config", "user.name", "Frozen Source Test")
            self.git(origin, "config", "user.email", "frozen-source@example.invalid")
            tracked = origin / "tracked.txt"
            tracked.write_text("frozen\n", encoding="utf-8")
            self.git(origin, "add", "tracked.txt")
            self.git(origin, "commit", "--quiet", "-m", "frozen source")
            commit = self.git(origin, "rev-parse", "HEAD")
            checkout = root / "checkout"

            checkout_exact_repository(
                checkout, repository=str(origin), commit=commit
            )

            self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), commit)
            symbolic_ref = subprocess.run(
                ["git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(symbolic_ref.returncode, 1)
            verify_exact_checkout(checkout, repository=str(origin), commit=commit)
            (checkout / "tracked.txt").write_text("mutated\n", encoding="utf-8")
            with self.assertRaisesRegex(FrozenSourceError, "modified tracked files"):
                verify_exact_checkout(checkout, repository=str(origin), commit=commit)


class InstalledKollaProvenanceTest(unittest.TestCase):
    def test_install_requires_exact_version_local_source_and_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kolla"
            source.mkdir()
            binary_dir = root / "venv" / "bin"
            binary_dir.mkdir(parents=True)
            python = binary_dir / "python"
            python.write_text("", encoding="utf-8")
            (binary_dir / "kolla-build").write_text("", encoding="utf-8")

            distribution = mock.Mock()
            distribution.version = "20.4.0"
            distribution.read_text.return_value = json.dumps(
                {"url": source.resolve().as_uri(), "dir_info": {}}
            )
            distribution.entry_points = [
                importlib.metadata.EntryPoint(
                    name="kolla-build",
                    value="kolla.cmd.build:main",
                    group="console_scripts",
                )
            ]
            with (
                mock.patch(
                    "scripts.frozen_sources.importlib.metadata.distribution",
                    return_value=distribution,
                ),
                mock.patch("scripts.frozen_sources.sys.executable", str(python)),
            ):
                verify_installed_kolla(source, "20.4.0")

                distribution.read_text.return_value = json.dumps(
                    {"url": (root / "wrong").resolve().as_uri(), "dir_info": {}}
                )
                with self.assertRaisesRegex(FrozenSourceError, "does not match"):
                    verify_installed_kolla(source, "20.4.0")


class FrozenSourceWorkflowTest(unittest.TestCase):
    def test_build_unit_uses_only_the_verified_local_kolla_checkout(self) -> None:
        workflow = BUILD_UNIT_WORKFLOW.read_text(encoding="utf-8")

        prepare = "python3 scripts/frozen_sources.py prepare"
        install = '"$KOLLA_SOURCE_DIR"'
        verify = ".venv/bin/python scripts/frozen_sources.py verify-install"
        self.assertIn(prepare, workflow)
        self.assertIn("--matrix config/build-matrix.json", workflow)
        self.assertIn("--publish-plan artifacts/plan/publish-plan.json", workflow)
        self.assertIn("PBR_VERSION=\"$KOLLA_VERSION\"", workflow)
        self.assertIn("--no-deps", workflow)
        self.assertIn('-r "$KOLLA_SOURCE_DIR/requirements.txt"', workflow)
        self.assertIn(verify, workflow)
        self.assertNotIn("kolla==$KOLLA_VERSION", workflow)
        self.assertNotRegex(workflow, r"pip install[^\n]*[\"']?kolla==")
        self.assertLess(workflow.index(prepare), workflow.index(install))
        self.assertLess(workflow.index(install), workflow.index(verify))


if __name__ == "__main__":
    unittest.main()
