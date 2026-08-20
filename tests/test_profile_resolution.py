from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import (
    Matrix,
    find_stream,
    find_toolchain,
    load_matrix,
    load_profile,
    render_revision_tag,
    render_tag,
    resolve_profile,
    selector_matches,
    stream_ids,
    validate_candidate_id,
)


def source_set_document(
    source_set_id: str,
    release: str,
    series: str,
    toolchains: dict[str, dict[str, dict[str, str]]],
) -> dict[str, object]:
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
    direct_artifacts = {
        "ovn-ctl": {
            "repository": "https://github.com/ovn-org/ovn",
            "commit": "20b9f0b9a771e07f15d2db270464965663d15f56",
            "path": "utilities/ovn-ctl",
            "url": (
                "https://raw.githubusercontent.com/ovn-org/ovn/"
                "20b9f0b9a771e07f15d2db270464965663d15f56/utilities/ovn-ctl"
            ),
            "sha256": "9" * 64,
            "kolla_sections": ["ovn-sb-db-relay"],
        }
    }
    if series == "epoxy":
        direct_artifacts["mariadb-clustercheck"] = {
            "repository": "https://src.fedoraproject.org/rpms/mariadb",
            "commit": "a8d966d60d33e0ffc35cb5271e1339d4ab63c004",
            "path": "f/clustercheck.sh",
            "url": (
                "https://src.fedoraproject.org/rpms/mariadb/raw/"
                "a8d966d60d33e0ffc35cb5271e1339d4ab63c004/f/clustercheck.sh"
            ),
            "sha256": "4" * 64,
            "kolla_sections": ["mariadb-base"],
        }
    return {
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
                    "sources_sha256": "3" * 64,
                    "closure_sha256": closure_sha256,
                },
                "kolla_ansible": toolchain["kolla_ansible"],
            }
            for version, toolchain in toolchains.items()
        },
        "direct_artifacts": direct_artifacts,
        "projects": projects,
    }


class ProfileResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ubuntu_2026 = {
            "id": "2026.1-ubuntu-24.04-22.1.0",
            "release": "2026.1",
            "toolchain": "22.1.0",
            "base": "ubuntu-24.04",
            "publish_enabled": True,
        }
        self.rocky_2025 = {
            "id": "2025.1-rocky-10.2-20.5.0",
            "release": "2025.1",
            "toolchain": "20.5.0",
            "base": "rocky-10.2",
            "publish_enabled": True,
        }
        self.matrix = {
            "schema_version": 4,
            "releases": {
                "2026.1": {
                    "series": "gazpacho",
                    "source_set": "gazpacho-20260820-r1",
                },
                "2025.1": {
                    "series": "epoxy",
                    "source_set": "epoxy-20260813-r1",
                },
            },
            "toolchains": {
                "22.1.0": {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": "a" * 40,
                    },
                    "kolla_ansible": {
                        "repository": "https://opendev.org/openstack/kolla-ansible",
                        "commit": "b" * 40,
                    },
                },
                "20.5.0": {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": "c" * 40,
                    },
                    "kolla_ansible": {
                        "repository": "https://opendev.org/openstack/kolla-ansible",
                        "commit": "d" * 40,
                    },
                },
            },
            "bases": {
                "ubuntu-24.04": {
                    "distro": "ubuntu",
                    "os_version": "24.04",
                    "image": "docker.io/library/ubuntu",
                    "tag": "24.04",
                },
                "rocky-10.2": {
                    "distro": "rocky",
                    "os_version": "10.2",
                    "image": "quay.io/rockylinux/rockylinux",
                    "tag": "10.2",
                },
            },
            "streams": [self.ubuntu_2026, self.rocky_2025],
            "tag_policy": {
                "deploy_tag_template": (
                    "{release}-{distro}-{os_version}-{kolla_ansible_version}"
                )
            },
        }
        source_sets = tempfile.TemporaryDirectory()
        self.addCleanup(source_sets.cleanup)
        source_sets_dir = Path(source_sets.name)
        self.source_sets_dir = source_sets_dir
        for release, release_config in self.matrix["releases"].items():
            source_set_id = release_config["source_set"]
            document = source_set_document(
                source_set_id,
                release,
                release_config["series"],
                {
                    version: self.matrix["toolchains"][version]
                    for version in {
                        stream["toolchain"]
                        for stream in self.matrix["streams"]
                        if stream["release"] == release
                    }
                },
            )
            (source_sets_dir / f"{source_set_id}.json").write_text(
                json.dumps(document),
                encoding="utf-8",
            )
        self.matrix = Matrix(self.matrix, source_sets_dir=source_sets_dir)
        self.profile = {
            "schema_version": 3,
            "name": "sample",
            "reviewed_streams": stream_ids(self.matrix),
            "images": [
                {
                    "name": "always",
                    "kolla_ansible_variables": [
                        "always_image_full",
                        {
                            "name": "new_alias_image_full",
                            "applies_to": {"releases": ["2026.1"]},
                        },
                        {
                            "name": "rocky_alias_image_full",
                            "applies_to": {"distros": ["rocky"]},
                        },
                    ],
                },
                {
                    "name": "rocky-only",
                    "kolla_ansible_variables": ["rocky_only_image_full"],
                    "applies_to": {"streams": [self.rocky_2025["id"]]},
                },
                {
                    "name": "ubuntu-only",
                    "kolla_ansible_variables": ["ubuntu_only_image_full"],
                    "applies_to": {"distros": ["ubuntu"]},
                },
                {
                    "name": "new-ubuntu-only",
                    "kolla_ansible_variables": ["new_ubuntu_only_image_full"],
                    "applies_to": {
                        "releases": ["2026.1"],
                        "distros": ["ubuntu"],
                    },
                },
            ],
            "build_groups": [
                {
                    "name": "mixed",
                    "parents": ["base", "openstack-base"],
                    "images": ["ubuntu-only", "always", "rocky-only"],
                },
                {
                    "name": "rocky",
                    "parents": ["base", "rocky-base"],
                    "images": ["rocky-only"],
                },
                {
                    "name": "new",
                    "parents": ["base", "prometheus-base"],
                    "images": ["new-ubuntu-only"],
                    "applies_to": {"releases": ["2026.1"]},
                },
                {
                    "name": "legacy",
                    "parents": ["base", "legacy-base"],
                    "images": ["always"],
                    "applies_to": {"releases": ["2025.1"]},
                },
            ],
        }

    def test_active_stream_rejects_legacy_source_set_schema(self) -> None:
        source_set_id = self.matrix["releases"]["2025.1"]["source_set"]
        path = self.source_sets_dir / f"{source_set_id}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        del document["direct_artifacts"]
        del document["kolla_source_inputs"]
        path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "active source-set schema_version"):
            find_stream(self.matrix, self.rocky_2025["id"])

    def test_loaders_read_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            profiles_dir = temp_path / "profiles"
            profiles_dir.mkdir()
            matrix_path.write_text(json.dumps(self.matrix), encoding="utf-8")
            (profiles_dir / "sample.json").write_text(
                json.dumps(self.profile), encoding="utf-8"
            )
            self.assertEqual(load_matrix(matrix_path), self.matrix)
            self.assertEqual(load_profile("sample", profiles_dir), self.profile)

    def test_loaders_reject_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            matrix_path.write_text(
                '{"schema_version": 4, "schema_version": 3}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_matrix(matrix_path)

            profiles_dir = temp_path / "profiles"
            profiles_dir.mkdir()
            (profiles_dir / "sample.json").write_text(
                '{"name": "sample", "name": "other"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_profile("sample", profiles_dir)

    def test_stream_resolution_joins_v4_references_without_mutating_matrix(self) -> None:
        self.assertEqual(stream_ids(self.matrix), [
            "2026.1-ubuntu-24.04-22.1.0",
            "2025.1-rocky-10.2-20.5.0",
        ])
        stream = find_stream(self.matrix, self.rocky_2025["id"])
        self.assertEqual(stream["release_series"], "epoxy")
        self.assertEqual(stream["release_branch"], "2025-1")
        self.assertEqual(stream["source_set_id"], "epoxy-20260813-r1")
        self.assertEqual(stream["source_set"]["id"], "epoxy-20260813-r1")
        self.assertRegex(stream["source_set_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(stream["toolchain_version"], "20.5.0")
        self.assertEqual(stream["kolla_version"], "20.5.0")
        self.assertEqual(stream["kolla_commit"], "c" * 40)
        self.assertEqual(stream["kolla_ansible_version"], "20.5.0")
        self.assertEqual(stream["kolla_ansible_commit"], "d" * 40)
        self.assertEqual(stream["base_id"], "rocky-10.2")
        self.assertEqual(stream["base_image"], "quay.io/rockylinux/rockylinux")
        self.assertEqual(stream["base_tag"], "10.2")
        self.assertEqual(stream["os_version"], "10.2")
        stream["toolchain"]["kolla"]["commit"] = "e" * 40
        self.assertEqual(
            self.matrix["toolchains"]["20.5.0"]["kolla"]["commit"], "c" * 40
        )

    def test_find_toolchain_lists_accepted_versions_on_failure(self) -> None:
        self.assertIs(
            find_toolchain(self.matrix, "20.5.0"),
            self.matrix["toolchains"]["20.5.0"],
        )
        with self.assertRaisesRegex(ValueError, "accepted versions: 20.5.0, 22.1.0"):
            find_toolchain(self.matrix, "23.0.0")

    def test_resolved_stream_rejects_legacy_fields_and_malformed_pins(self) -> None:
        conflicting = copy.deepcopy(self.matrix)
        conflicting["streams"][1]["kolla_version"] = "20.5.0"
        with self.assertRaisesRegex(ValueError, "conflicting fields"):
            find_stream(conflicting, self.rocky_2025["id"])
        malformed = copy.deepcopy(self.matrix)
        malformed["toolchains"]["20.5.0"]["kolla"]["commit"] = "c" * 39
        with self.assertRaisesRegex(ValueError, "lowercase 40-character SHA"):
            find_stream(malformed, self.rocky_2025["id"])

    def test_find_stream_lists_accepted_ids_on_failure(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepted streams:"):
            find_stream(self.matrix, "missing")

        duplicated = copy.deepcopy(self.matrix)
        duplicated["streams"].append(copy.deepcopy(self.rocky_2025))
        with self.assertRaisesRegex(ValueError, "exactly one stream"):
            find_stream(duplicated, self.rocky_2025["id"])

    def test_selector_dimensions_are_anded_and_fail_closed(self) -> None:
        ubuntu = find_stream(self.matrix, self.ubuntu_2026["id"])
        rocky = find_stream(self.matrix, self.rocky_2025["id"])
        self.assertTrue(selector_matches(None, ubuntu))
        selector = {"releases": ["2026.1"], "distros": ["ubuntu"]}
        self.assertTrue(selector_matches(selector, ubuntu))
        self.assertFalse(selector_matches(selector, rocky))
        with self.assertRaisesRegex(ValueError, "unsupported applies_to keys"):
            selector_matches({"architectures": ["arm64"]}, ubuntu)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            selector_matches({}, ubuntu)

    def test_resolve_profile_filters_images_variables_and_groups(self) -> None:
        original = copy.deepcopy(self.profile)
        ubuntu = resolve_profile(
            self.profile, find_stream(self.matrix, self.ubuntu_2026["id"])
        )
        rocky = resolve_profile(
            self.profile, find_stream(self.matrix, self.rocky_2025["id"])
        )
        self.assertEqual(
            [image["name"] for image in ubuntu["images"]],
            ["always", "ubuntu-only", "new-ubuntu-only"],
        )
        self.assertEqual(
            ubuntu["images"][0]["kolla_ansible_variables"],
            ["always_image_full", "new_alias_image_full"],
        )
        self.assertEqual(
            [group["name"] for group in ubuntu["build_groups"]], ["mixed", "new"]
        )
        self.assertEqual(
            [group["name"] for group in rocky["build_groups"]],
            ["mixed", "rocky", "legacy"],
        )
        self.assertEqual(ubuntu["resolved_stream"], self.ubuntu_2026["id"])
        self.assertEqual(self.profile, original)

    def test_profile_review_and_schema_fail_closed(self) -> None:
        stream = find_stream(self.matrix, self.rocky_2025["id"])
        with self.assertRaisesRegex(ValueError, "has not reviewed stream"):
            resolve_profile(self.profile, {**stream, "id": "unreviewed"})
        with self.assertRaisesRegex(ValueError, "schema_version must be 3"):
            resolve_profile({**self.profile, "schema_version": 2}, stream)

    def test_semantic_revision_and_architecture_tags_are_derived(self) -> None:
        stream = find_stream(self.matrix, self.ubuntu_2026["id"])
        self.assertEqual(render_tag(self.matrix, stream), stream["id"])
        self.assertEqual(
            render_tag(self.matrix, stream, "arm64"), f"{stream['id']}-arm64"
        )
        self.assertEqual(
            render_revision_tag(self.matrix, stream, "123-1"),
            f"{stream['id']}-rev-123-1",
        )
        self.assertEqual(
            render_revision_tag(self.matrix, stream, "123-1", "arm64"),
            f"{stream['id']}-rev-123-1-arm64",
        )

    def test_candidate_id_validation_rejects_non_run_shapes(self) -> None:
        self.assertEqual(validate_candidate_id("local-dry-run"), "local-dry-run")
        self.assertEqual(validate_candidate_id("123456789-1"), "123456789-1")
        for value in ("", "0-1", "1-0", "01-1", "1-01", "1", "1-a", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_candidate_id(value)
        with self.assertRaisesRegex(ValueError, "workflow candidate ID"):
            validate_candidate_id("local-dry-run", allow_local=False)


if __name__ == "__main__":
    unittest.main()
