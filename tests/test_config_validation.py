from __future__ import annotations

import copy
import hashlib
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import (
    Matrix,
    find_stream,
    render_tag,
    resolve_profile,
    stream_ids,
)


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"

EXPECTED_STREAMS = {
    "2025.1-rocky-9.8-20.4.0": ("2025.1", "20.4.0", "20.4.0", "rocky", "9.8", "9.8"),
    "2025.1-rocky-10.2-20.4.0": ("2025.1", "20.4.0", "20.4.0", "rocky", "10.2", "10.2"),
    "2025.1-ubuntu-24.04-20.4.0": (
        "2025.1",
        "20.4.0",
        "20.4.0",
        "ubuntu",
        "24.04",
        "24.04",
    ),
    "2025.1-rocky-10.2-20.5.0": ("2025.1", "20.5.0", "20.5.0", "rocky", "10.2", "10.2"),
    "2025.1-ubuntu-24.04-20.5.0": (
        "2025.1", "20.5.0", "20.5.0", "ubuntu", "24.04", "24.04"
    ),
    "2025.2-rocky-10.2-21.2.0": ("2025.2", "21.2.0", "21.2.0", "rocky", "10.2", "10.2"),
    "2025.2-ubuntu-24.04-21.2.0": (
        "2025.2",
        "21.2.0",
        "21.2.0",
        "ubuntu",
        "24.04",
        "24.04",
    ),
    "2026.1-rocky-10.2-22.1.0": ("2026.1", "22.1.0", "22.1.0", "rocky", "10.2", "10.2"),
    "2026.1-ubuntu-24.04-22.1.0": (
        "2026.1",
        "22.1.0",
        "22.1.0",
        "ubuntu",
        "24.04",
        "24.04",
    ),
}
EXPECTED_RELEASE_METADATA = {
    "repository": "https://opendev.org/openstack/releases",
    "commit": "f5cbd773fd453f59d7002a0c34c5871d71ed8868",
}
EXPECTED_TOOLCHAINS = {
    "20.4.0": {
        "kolla": {
            "repository": "https://opendev.org/openstack/kolla",
            "commit": "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5",
        },
        "kolla_ansible": {
            "repository": "https://opendev.org/openstack/kolla-ansible",
            "commit": "0786e1d6bd9a6da2d8ae15cc16a891bef0d32696",
        },
    },
    "20.5.0": {
        "kolla": {
            "repository": "https://opendev.org/openstack/kolla",
            "commit": "d1c4dd49b92e68509a413c33667bbe87cc3d3a9e",
        },
        "kolla_ansible": {
            "repository": "https://opendev.org/openstack/kolla-ansible",
            "commit": "18f731b2ef55a7dfb43182682458b1c8053c9cc2",
        },
    },
    "21.2.0": {
        "kolla": {
            "repository": "https://opendev.org/openstack/kolla",
            "commit": "422afe0d79511eafa3121547a7d5093096b6e0b6",
        },
        "kolla_ansible": {
            "repository": "https://opendev.org/openstack/kolla-ansible",
            "commit": "34daacfbf2d5987f543787f57535b2bebe7dee19",
        },
    },
    "22.1.0": {
        "kolla": {
            "repository": "https://opendev.org/openstack/kolla",
            "commit": "e40da0d4a7a73212cb16698a12eaeb5799cc55c7",
        },
        "kolla_ansible": {
            "repository": "https://opendev.org/openstack/kolla-ansible",
            "commit": "dcd07540af662e1283ca77ab5d3b92996f4f992d",
        },
    },
}
EXPECTED_RELEASES = {
    "2025.1": {"series": "epoxy", "source_set": "epoxy-20260813-r1"},
    "2025.2": {"series": "flamingo", "source_set": "flamingo-20260820-r1"},
    "2026.1": {"series": "gazpacho", "source_set": "gazpacho-20260820-r1"},
}
EXPECTED_BASES = {
    "rocky-9.8": {
        "distro": "rocky",
        "os_version": "9.8",
        "image": "quay.io/rockylinux/rockylinux",
        "tag": "9.8",
    },
    "rocky-10.2": {
        "distro": "rocky",
        "os_version": "10.2",
        "image": "quay.io/rockylinux/rockylinux",
        "tag": "10.2",
    },
    "ubuntu-24.04": {
        "distro": "ubuntu",
        "os_version": "24.04",
        "image": "docker.io/library/ubuntu",
        "tag": "24.04",
    },
}

NEUTRON_VARIABLES = [
    "neutron_server_image_full",
    {
        "name": "neutron_rpc_server_image_full",
        "applies_to": {"releases": ["2025.2", "2026.1"]},
    },
    {
        "name": "neutron_periodic_worker_image_full",
        "applies_to": {"releases": ["2025.2", "2026.1"]},
    },
    {
        "name": "neutron_ovn_maintenance_worker_image_full",
        "applies_to": {"releases": ["2025.2", "2026.1"]},
    },
]

DEPLOYMENT_EXPECTED_COUNTS = {
    "2025.1-rocky-9.8-20.4.0": 63,
    "2025.1-rocky-10.2-20.4.0": 63,
    "2025.1-ubuntu-24.04-20.4.0": 64,
    "2025.1-rocky-10.2-20.5.0": 63,
    "2025.1-ubuntu-24.04-20.5.0": 64,
    "2025.2-rocky-10.2-21.2.0": 63,
    "2025.2-ubuntu-24.04-21.2.0": 64,
    "2026.1-rocky-10.2-22.1.0": 65,
    "2026.1-ubuntu-24.04-22.1.0": 66,
}
REQUIRED_CINDER = {
    "cinder-api",
    "cinder-backup",
    "cinder-scheduler",
    "cinder-volume",
}
REQUIRED_MANILA = {
    "manila-api",
    "manila-data",
    "manila-scheduler",
    "manila-share",
}
REQUIRED_OCTAVIA = {
    "octavia-api",
    "octavia-driver-agent",
    "octavia-health-manager",
    "octavia-housekeeping",
    "octavia-worker",
}
REQUIRED_VALKEY = {"valkey-server", "valkey-sentinel"}
REQUIRED_LOGGING = {"fluentd", "opensearch", "opensearch-dashboards"}
REQUIRED_PROMETHEUS = {
    "prometheus-alertmanager",
    "prometheus-blackbox-exporter",
    "prometheus-cadvisor",
    "prometheus-elasticsearch-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-memcached-exporter",
    "prometheus-mysqld-exporter",
    "prometheus-node-exporter",
    "prometheus-openstack-exporter",
    "prometheus-server",
}
NEW_2026_EXPORTERS = {
    "prometheus-openstack-network-exporter",
    "prometheus-valkey-exporter",
}
EXCLUDED_DEPLOYMENT_LEAVES = {"etcd", "multipathd", "redis", "redis-sentinel"}
EXCLUDED_DEPLOYMENT_PREFIXES = (
    "ceph-",
    "designate-",
    "swift-",
    "ironic-",
    "redis-",
)


def load_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def synthetic_source_set(
    release: str,
    toolchains: dict[str, dict[str, dict[str, str]]] | None = None,
) -> dict[str, object]:
    release_config = EXPECTED_RELEASES[release]
    if toolchains is None:
        versions = {
            expected[1]
            for expected in EXPECTED_STREAMS.values()
            if expected[0] == release
        }
        toolchains = {
            version: EXPECTED_TOOLCHAINS[version] for version in versions
        }
    ovn_commit = "3" * 40
    direct_artifacts = {
        "ovn-ctl": {
            "repository": "https://github.com/ovn-org/ovn",
            "commit": ovn_commit,
            "path": "utilities/ovn-ctl",
            "url": (
                "https://raw.githubusercontent.com/ovn-org/ovn/"
                f"{ovn_commit}/utilities/ovn-ctl"
            ),
            "sha256": "4" * 64,
            "kolla_sections": ["ovn-sb-db-relay"],
        }
    }
    if release_config["series"] == "epoxy":
        mariadb_commit = "5" * 40
        direct_artifacts["mariadb-clustercheck"] = {
            "repository": "https://src.fedoraproject.org/rpms/mariadb",
            "commit": mariadb_commit,
            "path": "f/clustercheck.sh",
            "url": (
                "https://src.fedoraproject.org/rpms/mariadb/raw/"
                f"{mariadb_commit}/f/clustercheck.sh"
            ),
            "sha256": "6" * 64,
            "kolla_sections": ["mariadb-base"],
        }
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

    return {
        "schema_version": 3,
        "id": release_config["source_set"],
        "release": release,
        "series": release_config["series"],
        "policy": "stable-head-snapshot",
        "generated_at": "2026-08-13T00:00:00Z",
        "kolla_source_inputs": {
            version: {
                "kolla": {
                    **toolchain["kolla"],
                    "sources_sha256": "7" * 64,
                    "closure_sha256": closure_sha256,
                },
                "kolla_ansible": toolchain["kolla_ansible"],
            }
            for version, toolchain in toolchains.items()
        },
        "projects": projects,
        "direct_artifacts": direct_artifacts,
    }


class ConfigValidationTest(unittest.TestCase):
    def test_v4_matrix_accepts_multiple_toolchains_for_one_release(self) -> None:
        matrix = {
            "schema_version": 4,
            "owner": "supergate-hub",
            "repository": "kolla-container-images",
            "registry": "ghcr.io",
            "profiles": ["core", "deployment"],
            "release_metadata": copy.deepcopy(EXPECTED_RELEASE_METADATA),
            "releases": {
                "2025.1": {
                    "series": "epoxy",
                    "source_set": "epoxy-20260813-r1",
                }
            },
            "toolchains": {
                version: {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": commit,
                    },
                    "kolla_ansible": {
                        "repository": (
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        "commit": ansible_commit,
                    },
                }
                for version, commit, ansible_commit in (
                    ("20.4.0", "a" * 40, "b" * 40),
                    ("20.5.0", "c" * 40, "d" * 40),
                )
            },
            "bases": {
                "rocky-10.2": {
                    "distro": "rocky",
                    "os_version": "10.2",
                    "image": "quay.io/rockylinux/rockylinux",
                    "tag": "10.2",
                }
            },
            "streams": [
                {
                    "id": f"2025.1-rocky-10.2-{version}",
                    "release": "2025.1",
                    "toolchain": version,
                    "base": "rocky-10.2",
                    "publish_enabled": True,
                }
                for version in ("20.4.0", "20.5.0")
            ],
            "architectures": ["amd64", "arm64"],
            "tag_aliases": {},
            "tag_policy": {
                "deploy_tag_template": (
                    "{release}-{distro}-{os_version}-{kolla_ansible_version}"
                )
            },
        }
        errors: list[str] = []
        validator = runpy.run_path(str(ROOT / "scripts" / "validate-config.py"))
        source_set_id = matrix["releases"]["2025.1"]["source_set"]
        (self.synthetic_source_sets_dir / f"{source_set_id}.json").write_text(
            json.dumps(synthetic_source_set("2025.1", matrix["toolchains"])),
            encoding="utf-8",
        )

        validator["validate_matrix"](
            Matrix(matrix, source_sets_dir=self.synthetic_source_sets_dir),
            errors,
            branch_name="2025-1",
        )

        self.assertEqual(errors, [])

    def setUp(self) -> None:
        self.matrix = load_json(MATRIX_PATH)
        source_sets = tempfile.TemporaryDirectory()
        self.addCleanup(source_sets.cleanup)
        self.synthetic_source_sets_dir = Path(source_sets.name)
        for release, release_config in EXPECTED_RELEASES.items():
            source_set_id = release_config["source_set"]
            (self.synthetic_source_sets_dir / f"{source_set_id}.json").write_text(
                json.dumps(synthetic_source_set(release)),
                encoding="utf-8",
            )
        matrix_releases = {
            stream["release"] for stream in self.matrix["streams"]
        }
        self.active_releases = [
            release
            for release in EXPECTED_RELEASES
            if release in matrix_releases
        ]
        self.active_expected_streams = {
            stream_id: expected
            for stream_id, expected in EXPECTED_STREAMS.items()
            if expected[0] in matrix_releases
        }
        self.active_expected_toolchains = {
            version: copy.deepcopy(EXPECTED_TOOLCHAINS[version])
            for version in {
                stream["toolchain"] for stream in self.matrix["streams"]
            }
        }
        self.active_expected_releases = {
            release: copy.deepcopy(EXPECTED_RELEASES[release])
            for release in matrix_releases
        }
        referenced_bases = {stream["base"] for stream in self.matrix["streams"]}
        self.active_expected_bases = {
            base_id: copy.deepcopy(EXPECTED_BASES[base_id])
            for base_id in referenced_bases
        }
        self.validator = runpy.run_path(
            str(ROOT / "scripts" / "validate-config.py")
        )

    def validate_profile(
        self,
        profile_name: str,
        profile: dict[str, object],
        *,
        matrix: dict[str, object] | None = None,
    ) -> list[str]:
        errors: list[str] = []
        self.validator["validate_profile"](
            self.matrix if matrix is None else matrix,
            profile_name,
            profile,
            errors,
        )
        return errors

    def validate_matrix(
        self,
        matrix: dict[str, object],
        *,
        branch_name: str | None = None,
    ) -> list[str]:
        errors: list[str] = []
        self.validator["validate_matrix"](
            matrix,
            errors,
            branch_name=branch_name,
        )
        return errors

    def branch_matrix(self, release: str) -> Matrix:
        matrix = copy.deepcopy(self.matrix)
        matching_streams = [
            copy.deepcopy(stream)
            for stream in self.matrix["streams"]
            if stream["release"] == release
        ]
        if matching_streams:
            release_config = copy.deepcopy(self.matrix["releases"][release])
            toolchains = self.matrix["toolchains"]
            bases = self.matrix["bases"]
        else:
            matching_streams = [
                {
                    "id": stream_id,
                    "release": expected[0],
                    "toolchain": expected[1],
                    "base": f"{expected[3]}-{expected[4]}",
                    "publish_enabled": True,
                }
                for stream_id, expected in EXPECTED_STREAMS.items()
                if expected[0] == release
            ]
            release_config = copy.deepcopy(EXPECTED_RELEASES[release])
            toolchains = EXPECTED_TOOLCHAINS
            bases = EXPECTED_BASES

        matrix["streams"] = matching_streams
        matrix["releases"] = {release: release_config}
        versions = {stream["toolchain"] for stream in matching_streams}
        matrix["toolchains"] = {
            version: copy.deepcopy(toolchains[version])
            for version in versions
        }
        base_ids = {stream["base"] for stream in matching_streams}
        matrix["bases"] = {
            base_id: copy.deepcopy(bases[base_id])
            for base_id in base_ids
        }
        matrix["tag_aliases"] = {
            alias: target
            for alias, target in matrix.get("tag_aliases", {}).items()
            if target in {stream["id"] for stream in matching_streams}
        }
        return Matrix(matrix, source_sets_dir=self.synthetic_source_sets_dir)

    @staticmethod
    def remove_image(profile: dict[str, object], image_name: str) -> None:
        profile["images"] = [
            image for image in profile["images"] if image["name"] != image_name
        ]
        for group in profile["build_groups"]:
            group["images"] = [
                image for image in group["images"] if image != image_name
            ]

    def test_matrix_declares_exact_active_release_toolchains_and_streams(self) -> None:
        self.assertEqual(self.matrix["schema_version"], 4)
        self.assertEqual(self.matrix["owner"], "supergate-hub")
        self.assertEqual(self.matrix["repository"], "kolla-container-images")
        self.assertEqual(self.matrix["registry"], "ghcr.io")
        self.assertEqual(self.matrix["profiles"], ["core", "deployment"])
        self.assertEqual(self.matrix["release_metadata"], EXPECTED_RELEASE_METADATA)
        self.assertEqual(self.matrix["releases"], self.active_expected_releases)
        self.assertEqual(
            self.matrix["toolchains"], self.active_expected_toolchains
        )
        self.assertEqual(self.matrix["bases"], self.active_expected_bases)
        self.assertEqual(self.matrix["architectures"], ["amd64", "arm64"])
        self.assertEqual(
            self.matrix["tag_policy"],
            {
                "deploy_tag_template": (
                    "{release}-{distro}-{os_version}-{kolla_ansible_version}"
                ),
            },
        )
        self.assertEqual(
            stream_ids(self.matrix), list(self.active_expected_streams)
        )

        for stream_id, expected in self.active_expected_streams.items():
            with self.subTest(stream=stream_id):
                raw_stream = next(
                    stream
                    for stream in self.matrix["streams"]
                    if stream["id"] == stream_id
                )
                self.assertEqual(
                    set(raw_stream),
                    {
                        "id",
                        "release",
                        "toolchain",
                        "base",
                        "publish_enabled",
                    },
                )
                stream = find_stream(self.matrix, stream_id)
                self.assertEqual(
                    (
                        stream["release"],
                        stream["kolla_version"],
                        stream["kolla_ansible_version"],
                        stream["distro"],
                        stream["base_tag"],
                        stream["tag_token"],
                    ),
                    expected,
                )
                self.assertEqual(
                    stream["kolla_version"], stream["kolla_ansible_version"]
                )
                toolchain = EXPECTED_TOOLCHAINS[stream["toolchain_version"]]
                release_config = EXPECTED_RELEASES[stream["release"]]
                self.assertEqual(stream["release_series"], release_config["series"])
                self.assertEqual(
                    stream["release_branch"], stream["release"].replace(".", "-")
                )
                self.assertEqual(stream["kolla_commit"], toolchain["kolla"]["commit"])
                self.assertEqual(
                    stream["kolla_ansible_commit"],
                    toolchain["kolla_ansible"]["commit"],
                )
                self.assertIs(stream["publish_enabled"], True)
                self.assertEqual(
                    render_tag(self.matrix, stream),
                    stream_id,
                )

    def test_profiles_review_every_stream_and_resolve_neutron_aliases(self) -> None:
        for profile_name in self.matrix["profiles"]:
            with self.subTest(profile=profile_name):
                profile = load_json(PROFILES_DIR / f"{profile_name}.json")
                self.assertEqual(profile["schema_version"], 3)
                self.assertEqual(
                    set(profile["reviewed_streams"]), set(EXPECTED_STREAMS)
                )
                self.assertEqual(len(profile["reviewed_streams"]), len(EXPECTED_STREAMS))

                neutron = next(
                    image
                    for image in profile["images"]
                    if image["name"] == "neutron-server"
                )
                self.assertEqual(neutron["kolla_ansible_variables"], NEUTRON_VARIABLES)

                for stream_id in self.active_expected_streams:
                    stream = find_stream(self.matrix, stream_id)
                    resolved = resolve_profile(profile, stream)
                    resolved_neutron = next(
                        image
                        for image in resolved["images"]
                        if image["name"] == "neutron-server"
                    )
                    expected_variables = ["neutron_server_image_full"]
                    if stream["release"] in {"2025.2", "2026.1"}:
                        expected_variables.extend(
                            variable["name"] for variable in NEUTRON_VARIABLES[1:]
                        )
                    self.assertEqual(
                        resolved_neutron["kolla_ansible_variables"],
                        expected_variables,
                    )

    def test_deployment_resolves_exact_mixed_backend_policy(self) -> None:
        profile = load_json(PROFILES_DIR / "deployment.json")
        required_common = (
            REQUIRED_CINDER
            | REQUIRED_MANILA
            | REQUIRED_OCTAVIA
            | REQUIRED_VALKEY
            | REQUIRED_LOGGING
            | REQUIRED_PROMETHEUS
            | {"grafana", "iscsid"}
        )

        active_counts = {
            stream_id: DEPLOYMENT_EXPECTED_COUNTS[stream_id]
            for stream_id in self.active_expected_streams
        }
        for stream_id, expected_count in active_counts.items():
            with self.subTest(stream=stream_id):
                stream = find_stream(self.matrix, stream_id)
                resolved = resolve_profile(profile, stream)
                image_names = {image["name"] for image in resolved["images"]}
                relay_group = next(
                    group
                    for group in resolved["build_groups"]
                    if group["images"] == ["ovn-sb-db-relay"]
                )

                self.assertEqual(len(image_names), expected_count)
                self.assertEqual(
                    relay_group["parents"],
                    [
                        "base",
                        "openvswitch-base",
                        "ovn-base",
                        "ovn-sb-db-server",
                    ],
                )
                self.assertNotIn(
                    "ovn-sb-db-server",
                    self.validator["resolved_parent_sequence"](
                        resolved["build_groups"]
                    ),
                )
                self.assertTrue(required_common <= image_names)
                self.assertEqual("tgtd" in image_names, stream["distro"] == "ubuntu")
                self.assertEqual(
                    NEW_2026_EXPORTERS <= image_names,
                    stream["release"] == "2026.1",
                )
                if stream["release"] != "2026.1":
                    self.assertTrue(NEW_2026_EXPORTERS.isdisjoint(image_names))
                self.assertTrue(EXCLUDED_DEPLOYMENT_LEAVES.isdisjoint(image_names))
                self.assertFalse(
                    any(
                        image == "ceph"
                        or image == "designate"
                        or image == "swift"
                        or image == "ironic"
                        or image.startswith(EXCLUDED_DEPLOYMENT_PREFIXES)
                        for image in image_names
                    )
                )

    def test_repository_configuration_validator_passes(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/validate-config.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertEqual(completed.stdout.strip(), "Configuration validation passed.")
        self.assertEqual(completed.stderr, "")

    def test_validator_accepts_each_complete_release_branch_subset(self) -> None:
        for release in self.active_releases:
            branch_name = release.replace(".", "-")
            with self.subTest(release=release, branch=branch_name):
                matrix = self.branch_matrix(release)
                errors = self.validate_matrix(
                    matrix,
                    branch_name=branch_name,
                )
                self.validator["validate_profiles"](matrix, errors)
                self.assertEqual(errors, [])

    def test_release_branch_keeps_owned_revisions_and_rejects_foreign_files(self) -> None:
        release = self.active_releases[0]
        branch_name = release.replace(".", "-")
        source_set_id = self.matrix["releases"][release]["source_set"]
        with tempfile.TemporaryDirectory() as temp_dir:
            source_sets_dir = Path(temp_dir) / "openstack-sources"
            source_sets_dir.mkdir()
            source_path = ROOT / "config" / "openstack-sources" / f"{source_set_id}.json"
            (source_sets_dir / source_path.name).write_bytes(source_path.read_bytes())
            matrix = Matrix(
                self.branch_matrix(release),
                source_sets_dir=source_sets_dir,
            )

            errors: list[str] = []
            self.validator["validate_matrix"](
                matrix,
                errors,
                branch_name=branch_name,
                require_exact_source_files=True,
            )
            self.assertEqual(errors, [])

            historical = json.loads(source_path.read_text(encoding="utf-8"))
            historical["id"] = f"{historical['series']}-20260812-r1"
            (source_sets_dir / f"{historical['id']}.json").write_text(
                json.dumps(historical),
                encoding="utf-8",
            )
            errors = []
            self.validator["validate_matrix"](
                matrix,
                errors,
                branch_name=branch_name,
                require_exact_source_files=True,
            )
            self.assertEqual(errors, [])

            foreign_release = next(
                candidate for candidate in EXPECTED_RELEASES if candidate != release
            )
            foreign_id = EXPECTED_RELEASES[foreign_release]["source_set"]
            foreign_path = self.synthetic_source_sets_dir / f"{foreign_id}.json"
            (source_sets_dir / foreign_path.name).write_bytes(foreign_path.read_bytes())
            errors = []
            self.validator["validate_matrix"](
                matrix,
                errors,
                branch_name=branch_name,
                require_exact_source_files=True,
            )
            self.assertTrue(
                any("is not owned by this matrix" in error for error in errors),
                errors,
            )

    def test_matrix_rejects_a_legacy_active_source_set(self) -> None:
        release = self.active_releases[0]
        source_set_id = self.matrix["releases"][release]["source_set"]
        source_path = (
            ROOT / "config" / "openstack-sources" / f"{source_set_id}.json"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            source_sets_dir = Path(temp_dir) / "openstack-sources"
            source_sets_dir.mkdir()
            document = json.loads(source_path.read_text(encoding="utf-8"))
            document["schema_version"] = 1
            del document["direct_artifacts"]
            del document["kolla_source_inputs"]
            (source_sets_dir / source_path.name).write_text(
                json.dumps(document),
                encoding="utf-8",
            )
            matrix = Matrix(
                self.branch_matrix(release),
                source_sets_dir=source_sets_dir,
            )
            errors: list[str] = []

            self.validator["validate_matrix"](
                matrix,
                errors,
                branch_name=release.replace(".", "-"),
            )

        self.assertTrue(
            any("active source-set schema_version must be 3" in error for error in errors),
            errors,
        )

    def test_main_context_accepts_dynamic_aggregate_catalog(self) -> None:
        self.assertEqual(self.validate_matrix(self.matrix, branch_name="main"), [])

    def test_matrix_rejects_incomplete_or_mixed_release_branch_subsets(self) -> None:
        release = self.active_releases[0]
        branch_name = release.replace(".", "-")
        other_release = next(
            candidate
            for candidate in EXPECTED_RELEASES
            if candidate != release
        )
        if other_release in self.matrix["releases"]:
            other_matrix = self.branch_matrix(other_release)
        else:
            other_stream_id, other_expected = next(
                (stream_id, expected)
                for stream_id, expected in EXPECTED_STREAMS.items()
                if expected[0] == other_release
            )
            _, toolchain_version, _, distro, os_version, _ = other_expected
            base_id = f"{distro}-{os_version}"
            other_matrix = {
                "streams": [
                    {
                        "id": other_stream_id,
                        "release": other_release,
                        "toolchain": toolchain_version,
                        "base": base_id,
                        "publish_enabled": True,
                    }
                ],
                "releases": {
                    other_release: copy.deepcopy(EXPECTED_RELEASES[other_release])
                },
                "toolchains": {
                    toolchain_version: copy.deepcopy(
                        EXPECTED_TOOLCHAINS[toolchain_version]
                    )
                },
                "bases": {base_id: copy.deepcopy(EXPECTED_BASES[base_id])},
            }
        mixed = self.branch_matrix(release)
        mixed["streams"].extend(copy.deepcopy(other_matrix["streams"]))
        mixed["releases"].update(copy.deepcopy(other_matrix["releases"]))
        mixed["toolchains"].update(copy.deepcopy(other_matrix["toolchains"]))
        mixed["bases"].update(copy.deepcopy(other_matrix["bases"]))
        mixed_errors = self.validate_matrix(mixed, branch_name=branch_name)
        self.assertTrue(
            any(
                f"branch '{branch_name}' owns release '{release}'" in error
                for error in mixed_errors
            ),
            mixed_errors,
        )

    def test_matrix_rejects_legacy_stream_level_toolchain_pins(self) -> None:
        matrix = copy.deepcopy(self.matrix)
        matrix["streams"][0]["kolla_version"] = "20.4.0"
        matrix["streams"][0]["kolla_ansible_version"] = "20.4.0"

        errors = self.validate_matrix(matrix)

        self.assertTrue(
            any("streams[0] keys must be exactly" in error for error in errors),
            errors,
        )

    def test_matrix_rejects_missing_unused_and_malformed_toolchains(self) -> None:
        version = next(iter(self.matrix["toolchains"]))
        missing = copy.deepcopy(self.matrix)
        del missing["toolchains"][version]
        missing_errors = self.validate_matrix(missing)
        self.assertTrue(
            any(
                "toolchains must be a non-empty object" in error
                or "toolchain keys must exactly match" in error
                for error in missing_errors
            ),
            missing_errors,
        )

        unused = copy.deepcopy(self.matrix)
        unused["toolchains"]["99.0.0"] = copy.deepcopy(
            unused["toolchains"][version]
        )
        unused_errors = self.validate_matrix(unused)
        self.assertTrue(
            any("toolchain keys must exactly match" in error for error in unused_errors),
            unused_errors,
        )

        malformed = copy.deepcopy(self.matrix)
        malformed["toolchains"][version]["kolla"]["commit"] = "99b84ab"
        malformed["toolchains"][version]["kolla"]["version"] = version
        malformed_errors = self.validate_matrix(malformed)
        self.assertTrue(
            any("lowercase 40-character SHA" in error for error in malformed_errors),
            malformed_errors,
        )
        self.assertTrue(any("keys must be exactly" in error for error in malformed_errors))

    def test_runtime_validator_rejects_coherent_core_leaf_removal(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "core.json"))
        self.remove_image(profile, "neutron-metadata-agent")

        errors = self.validate_profile("core", profile)

        self.assertTrue(
            any("resolved image set must be exactly" in error for error in errors),
            errors,
        )

    def test_core_profile_must_remain_a_deployment_subset(self) -> None:
        core = copy.deepcopy(load_json(PROFILES_DIR / "core.json"))
        deployment = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        self.remove_image(deployment, "neutron-metadata-agent")

        errors: list[str] = []
        self.validator["validate_core_subset"](
            self.matrix,
            {"core": core, "deployment": deployment},
            errors,
        )

        self.assertTrue(
            any("must be a subset of deployment" in error for error in errors),
            errors,
        )

    def test_runtime_validator_rejects_coherent_cinder_leaf_removal(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        self.remove_image(profile, "cinder-api")

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any("resolved image set must be exactly" in error for error in errors),
            errors,
        )

    def test_runtime_validator_rejects_coherent_2026_exporter_removal(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        for image_name in (
            "prometheus-openstack-network-exporter",
            "prometheus-valkey-exporter",
        ):
            self.remove_image(profile, image_name)

        errors = self.validate_profile(
            "deployment",
            profile,
            matrix=self.branch_matrix("2026.1"),
        )

        self.assertTrue(
            any(
                "resolved image set must be exactly" in error
                and "2026.1" in error
                for error in errors
            ),
            errors,
        )

    def test_runtime_validator_rejects_validly_shaped_leaf_replacement(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        image = next(
            image for image in profile["images"] if image["name"] == "cinder-api"
        )
        image["name"] = "designate-api"
        image["kolla_ansible_variables"] = ["designate_api_image_full"]
        group = next(
            group for group in profile["build_groups"] if group["name"] == "cinder"
        )
        group["images"] = [
            "designate-api" if name == "cinder-api" else name
            for name in group["images"]
        ]

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any("resolved image set must be exactly" in error for error in errors),
            errors,
        )

    def test_runtime_validator_rejects_wrong_variable_mapping(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        image = next(
            image for image in profile["images"] if image["name"] == "cinder-api"
        )
        image["kolla_ansible_variables"] = ["replacement_image_full"]

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any("variable mapping must be exactly" in error for error in errors),
            errors,
        )

    def test_runtime_validator_rejects_wrong_conditional_neutron_aliases(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        neutron = next(
            image for image in profile["images"] if image["name"] == "neutron-server"
        )
        for variable in neutron["kolla_ansible_variables"]:
            if isinstance(variable, dict):
                variable["applies_to"] = {"releases": ["2026.1"]}

        errors = self.validate_profile(
            "deployment",
            profile,
            matrix=self.branch_matrix("2025.2"),
        )

        self.assertTrue(
            any(
                "neutron-server variable mapping must be exactly" in error
                and "2025.2" in error
                for error in errors
            ),
            errors,
        )

    def test_runtime_validator_rejects_selector_matching_no_stream(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        group = next(
            group
            for group in profile["build_groups"]
            if "mariadb-server" in group["images"]
        )
        rocky_stream = next(
            stream
            for stream in self.matrix["streams"]
            if self.matrix["bases"][stream["base"]]["distro"] == "rocky"
        )
        group["applies_to"] = {
            "streams": [rocky_stream["id"]],
            "distros": ["ubuntu"],
        }

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any("does not match any supported stream" in error for error in errors),
            errors,
        )

    def test_runtime_validator_rejects_wrong_resolved_parent_set(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        group = next(
            group
            for group in profile["build_groups"]
            if group["name"] == "database-modern"
        )
        group["parent"] = "mariadb-base"
        group["parents"] = ["base", "mariadb-base"]

        errors = self.validate_profile(
            "deployment",
            profile,
            matrix=self.branch_matrix("2025.2"),
        )

        self.assertTrue(
            any(
                "resolved parent set must be exactly" in error
                and "2025.2" in error
                for error in errors
            ),
            errors,
        )

    def test_runtime_validator_rejects_wrong_ovn_relay_parent_chain(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "deployment.json"))
        relay = next(
            group
            for group in profile["build_groups"]
            if group["name"] == "ovn-sb-db-relay"
        )
        relay["parent"] = "ovn-base"
        relay["parents"] = ["base", "openvswitch-base", "ovn-base"]

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any(
                "ovn-sb-db-relay parent chain must be exactly" in error
                for error in errors
            ),
            errors,
        )

    def test_malformed_tag_templates_fail_closed(self) -> None:
        validator = runpy.run_path(str(ROOT / "scripts" / "validate-config.py"))
        validate_matrix = validator["validate_matrix"]

        for template in (
            "{}",
            "{release.missing}-{distro}-{os_version}",
        ):
            with self.subTest(template=template):
                matrix = copy.deepcopy(self.matrix)
                matrix["tag_policy"]["deploy_tag_template"] = template
                errors: list[str] = []

                try:
                    validate_matrix(matrix, errors)
                except (AttributeError, IndexError) as error:
                    self.fail(
                        "validate_matrix must fail closed for malformed tag "
                        f"templates; raised {type(error).__name__}: {error}"
                    )

                self.assertTrue(
                    any("deploy_tag_template fields" in error for error in errors),
                    errors,
                )

        matrix = copy.deepcopy(self.matrix)
        matrix["tag_policy"]["candidate_tag_template"] = (
            "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
        )
        errors: list[str] = []
        validate_matrix(matrix, errors)
        self.assertTrue(
            any("tag_policy keys must be exactly" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
