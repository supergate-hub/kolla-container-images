from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.base_resolution import resolve_base
from scripts.openstack_source_set import render_frozen_configs
from scripts.profile_resolver import Matrix, find_stream, load_matrix
from scripts.release_policy import (
    branch_for_ref,
    release_branch_for,
    release_for_branch,
    validate_matrix_branch,
    validate_plan_matrix,
    validate_publish_context,
    validate_publish_source,
)


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-release-context.py"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"


class ReleasePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        source_sets = tempfile.TemporaryDirectory()
        self.addCleanup(source_sets.cleanup)
        self.source_sets_dir = Path(source_sets.name)

    def write_synthetic_source_set(
        self,
        source_set_id: str,
        release: str,
        series: str,
        toolchains: dict[str, dict[str, dict[str, str]]],
    ) -> None:
        projects = {
            "openstack/requirements": {
                "repository": "https://opendev.org/openstack/requirements",
                "track_ref": f"stable/{release}",
                "build_commit": "1" * 40,
                "kolla_sections": ["openstack-base"],
                "nearest_release": None,
                "upper_constraints_sha256": "2" * 64,
            }
        }
        closure_sha256 = hashlib.sha256(
            json.dumps(
                {
                    name: {
                        "repository": project["repository"],
                        "track_ref": project["track_ref"],
                        "kolla_sections": project["kolla_sections"],
                    }
                    for name, project in projects.items()
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        document = {
            "schema_version": 3,
            "id": source_set_id,
            "release": release,
            "series": series,
            "policy": "stable-head-snapshot",
            "generated_at": "2026-08-13T00:00:00Z",
            "kolla_source_inputs": {
                version: {
                    "kolla": {
                        **toolchain["kolla"],
                        "sources_sha256": "8" * 64,
                        "closure_sha256": closure_sha256,
                    },
                    "kolla_ansible": toolchain["kolla_ansible"],
                }
                for version, toolchain in toolchains.items()
            },
            "direct_artifacts": {
                "ovn-ctl": {
                    "repository": "https://github.com/ovn-org/ovn",
                    "commit": "20b9f0b9a771e07f15d2db270464965663d15f56",
                    "path": "utilities/ovn-ctl",
                    "url": (
                        "https://raw.githubusercontent.com/ovn-org/ovn/"
                        "20b9f0b9a771e07f15d2db270464965663d15f56/"
                        "utilities/ovn-ctl"
                    ),
                    "sha256": "9" * 64,
                    "kolla_sections": ["ovn-sb-db-relay"],
                }
            },
            "projects": projects,
        }
        (self.source_sets_dir / f"{source_set_id}.json").write_text(
            json.dumps(document),
            encoding="utf-8",
        )

    def branch_matrix(self, release: str) -> dict:
        matrix = load_matrix()
        branch_matrix = copy.deepcopy(matrix)
        matching_streams = [
            stream for stream in matrix["streams"] if stream["release"] == release
        ]
        if matching_streams and release in matrix["releases"]:
            branch_matrix["streams"] = copy.deepcopy(matching_streams)
            branch_matrix["releases"] = {
                release: copy.deepcopy(matrix["releases"][release])
            }
            toolchain_versions = {stream["toolchain"] for stream in matching_streams}
            branch_matrix["toolchains"] = {
                version: copy.deepcopy(matrix["toolchains"][version])
                for version in toolchain_versions
            }
            base_ids = {stream["base"] for stream in matching_streams}
            branch_matrix["bases"] = {
                base_id: copy.deepcopy(matrix["bases"][base_id])
                for base_id in base_ids
            }
            return Matrix(
                branch_matrix,
                source_sets_dir=matrix.source_sets_dir,
            )

        # Synthetic releases retain the v4 reference shape.
        stream = copy.deepcopy(matrix["streams"][0])
        stream["release"] = release
        version = stream["toolchain"]
        base_id = stream["base"]
        base = matrix["bases"][base_id]
        stream["id"] = (
            f"{release}-{base['distro']}-{base['os_version']}-{version}"
        )
        branch_matrix["streams"] = [stream]
        branch_matrix["releases"] = {
            release: {
                "series": f"synthetic-{release.replace('.', '-')}",
                "source_set": f"synthetic-{release.replace('.', '-')}-r1",
            }
        }
        branch_matrix["toolchains"] = {
            version: copy.deepcopy(matrix["toolchains"][version])
        }
        branch_matrix["bases"] = {base_id: copy.deepcopy(base)}
        source_set_id = branch_matrix["releases"][release]["source_set"]
        self.write_synthetic_source_set(
            source_set_id,
            release,
            branch_matrix["releases"][release]["series"],
            branch_matrix["toolchains"],
        )
        return Matrix(
            branch_matrix,
            source_sets_dir=self.source_sets_dir,
        )

    def branch_plan(self, matrix: dict, release: str) -> dict:
        stream = find_stream(matrix, matrix["streams"][0]["id"])
        frozen_sources = render_frozen_configs(stream["source_set"])
        return {
            "stream": stream["id"],
            "release": release,
            "release_series": stream["release_series"],
            "release_branch": release_branch_for(release),
            "release_metadata": copy.deepcopy(matrix["release_metadata"]),
            "kolla": {
                "repository": stream["kolla_repository"],
                "version": stream["kolla_version"],
                "commit": stream["kolla_commit"],
            },
            "kolla_ansible": {
                "repository": stream["kolla_ansible_repository"],
                "version": stream["kolla_ansible_version"],
                "commit": stream["kolla_ansible_commit"],
            },
            "base": resolve_base(
                {
                    "id": stream["base_id"],
                    "distro": stream["distro"],
                    "os_version": stream["os_version"],
                    "image": stream["base_image"],
                    "tag": stream["base_tag"],
                },
                BASE_INDEX_FIXTURE.read_bytes(),
            ),
            "openstack_sources": {
                "source_set": copy.deepcopy(stream["source_set"]),
                "canonical_digest": stream["source_set_sha256"],
                "kolla_build_config": {
                    "sha256": frozen_sources.config_sha256,
                    "content": frozen_sources.config_content,
                },
                "template_override": {
                    "sha256": frozen_sources.template_override_sha256,
                    "content": frozen_sources.template_override_content,
                },
            },
        }

    def test_release_and_branch_names_have_one_canonical_mapping(self) -> None:
        cases = {
            "2025.1": "2025-1",
            "2025.2": "2025-2",
            "2026.1": "2026-1",
        }
        for release, branch in cases.items():
            with self.subTest(release=release, branch=branch):
                self.assertEqual(release_branch_for(release), branch)
                self.assertEqual(release_for_branch(branch), release)

    def test_release_and_branch_aliases_are_rejected(self) -> None:
        for release in ("", "2025", "2025-1", "2025.0", "2025.01", True):
            with self.subTest(release=release):
                with self.assertRaises(ValueError):
                    release_branch_for(release)
        for branch in (
            "",
            "2025.1",
            "release/2025-1",
            "2025-0",
            "2025-01",
            True,
        ):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    release_for_branch(branch)

        self.assertEqual(branch_for_ref("refs/heads/2025-1"), "2025-1")
        for git_ref in (
            "main",
            "refs/heads/main",
            "refs/tags/2025-1",
            "refs/heads/release/2025-1",
            True,
        ):
            with self.subTest(git_ref=git_ref):
                with self.assertRaises(ValueError):
                    branch_for_ref(git_ref)

    def test_branch_local_matrix_accepts_only_its_release(self) -> None:
        branch_matrix = self.branch_matrix("2025.1")

        self.assertEqual(validate_matrix_branch(branch_matrix, "2025-1"), [])

        foreign_stream = self.branch_matrix("2025.2")["streams"][0]
        branch_matrix["streams"].append(copy.deepcopy(foreign_stream))
        errors = validate_matrix_branch(branch_matrix, "2025-1")
        self.assertTrue(
            any(
                foreign_stream["id"] in error and "2025.2" in error
                for error in errors
            ),
            errors,
        )

    def test_branch_local_matrix_requires_exact_referenced_toolchains(self) -> None:
        branch_matrix = self.branch_matrix("2025.2")
        branch_matrix["toolchains"] = {}

        errors = validate_matrix_branch(branch_matrix, "2025-2")

        self.assertTrue(any("toolchains must exactly match" in error for error in errors))

    def test_branch_matrix_rejects_release_ownership_mismatch(self) -> None:
        matrix = self.branch_matrix("2025.1")
        matrix["releases"]["2025.2"] = copy.deepcopy(
            self.branch_matrix("2025.2")["releases"]["2025.2"]
        )

        errors = validate_matrix_branch(matrix, "2025-1")

        self.assertTrue(any("must contain exactly the '2025.1' release" in error for error in errors))

    def test_frozen_plan_is_bound_to_branch_stream_and_source_pins(self) -> None:
        matrix = self.branch_matrix("2025.1")
        plan = self.branch_plan(matrix, "2025.1")
        self.assertEqual(validate_plan_matrix(matrix, plan, "2025-1"), [])
        self.assertEqual(
            validate_publish_context(matrix, plan, "refs/heads/2025-1"),
            [],
        )

        mutations = {
            "release": lambda value: value.__setitem__("release", "2025.2"),
            "release branch": lambda value: value.__setitem__(
                "release_branch", "2025-2"
            ),
            "release series": lambda value: value.__setitem__(
                "release_series", "wrong"
            ),
            "stream": lambda value: value.__setitem__("stream", "missing"),
            "metadata": lambda value: value["release_metadata"].__setitem__(
                "commit", "0" * 40
            ),
            "Kolla": lambda value: value["kolla"].__setitem__(
                "commit", "0" * 40
            ),
            "Kolla-Ansible": lambda value: value["kolla_ansible"].__setitem__(
                "commit", "0" * 40
            ),
            "base identity": lambda value: value["base"].__setitem__(
                "requested_ref", "quay.io/rockylinux/rockylinux:wrong"
            ),
            "OpenStack source": lambda value: value["openstack_sources"].__setitem__(
                "canonical_digest", "sha256:" + "0" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(plan)
                mutate(malformed)
                self.assertTrue(validate_plan_matrix(matrix, malformed, "2025-1"))

    def test_live_publish_context_requires_protected_exact_release_ref(self) -> None:
        matrix = self.branch_matrix("2025.1")
        plan = self.branch_plan(matrix, "2025.1")
        self.assertEqual(
            validate_publish_context(
                matrix,
                plan,
                "refs/heads/2025-1",
                require_protected=True,
                ref_protected=True,
            ),
            [],
        )
        for git_ref, protected in (
            ("refs/heads/main", True),
            ("refs/heads/2025-2", True),
            ("refs/tags/2025-1", True),
            ("refs/heads/2025-1", False),
        ):
            with self.subTest(git_ref=git_ref, protected=protected):
                self.assertTrue(
                    validate_publish_context(
                        matrix,
                        plan,
                        git_ref,
                        require_protected=True,
                        ref_protected=protected,
                    )
                )

    def test_cli_fails_closed_for_matrix_plan_and_protection(self) -> None:
        repository_matrix = load_matrix()
        release = repository_matrix["streams"][0]["release"]
        branch = release_branch_for(release)
        matrix = self.branch_matrix(release)
        plan = self.branch_plan(matrix, release)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            plan_path = temp_path / "plan.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            matrix_result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "matrix",
                    "--matrix",
                    str(matrix_path),
                    "--branch",
                    branch,
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(matrix_result.returncode, 0, matrix_result.stderr)

            base_command = [
                sys.executable,
                str(VALIDATOR),
                "publish",
                "--matrix",
                str(matrix_path),
                "--publish-plan",
                str(plan_path),
                "--git-ref",
                f"refs/heads/{branch}",
            ]
            self.assertEqual(subprocess.run(base_command).returncode, 0)
            missing_protection = subprocess.run(
                [*base_command, "--require-protected"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(missing_protection.returncode, 0)
            protected = subprocess.run(
                [
                    *base_command,
                    "--require-protected",
                    "--ref-protected",
                    "true",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(protected.returncode, 0, protected.stderr)

    def test_publish_source_requires_exact_protected_release_branch(self) -> None:
        plan = {"release": "2025.1"}
        self.assertEqual(
            validate_publish_source(plan, "refs/heads/2025-1", True),
            [],
        )

        for ref, protected in (
            ("refs/heads/main", True),
            ("refs/heads/2025-2", True),
            ("refs/heads/feature/test", True),
            ("refs/tags/2025-1", True),
            ("refs/heads/2025-1", False),
        ):
            with self.subTest(ref=ref, protected=protected):
                self.assertTrue(validate_publish_source(plan, ref, protected))


if __name__ == "__main__":
    unittest.main()
