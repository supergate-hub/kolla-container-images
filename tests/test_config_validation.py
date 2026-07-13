from __future__ import annotations

import copy
import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.profile_resolver import (
    find_stream,
    render_tag,
    resolve_profile,
    stream_ids,
)


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"

EXPECTED_STREAMS = {
    "2025.1-rocky-9": ("2025.1", "20.4.0", "20.4.0", "rocky", "9", "9"),
    "2025.1-rocky-10": ("2025.1", "20.4.0", "20.4.0", "rocky", "10", "10"),
    "2025.1-ubuntu-noble": (
        "2025.1",
        "20.4.0",
        "20.4.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
    "2025.2-rocky-10": ("2025.2", "21.1.0", "21.1.0", "rocky", "10", "10"),
    "2025.2-ubuntu-noble": (
        "2025.2",
        "21.1.0",
        "21.1.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
    "2026.1-rocky-10": ("2026.1", "22.0.0", "22.0.0", "rocky", "10", "10"),
    "2026.1-ubuntu-noble": (
        "2026.1",
        "22.0.0",
        "22.0.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
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
    "2025.1-rocky-9": 63,
    "2025.1-rocky-10": 63,
    "2025.1-ubuntu-noble": 64,
    "2025.2-rocky-10": 63,
    "2025.2-ubuntu-noble": 64,
    "2026.1-rocky-10": 65,
    "2026.1-ubuntu-noble": 66,
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


class ConfigValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_json(MATRIX_PATH)
        self.validator = runpy.run_path(
            str(ROOT / "scripts" / "validate-config.py")
        )

    def validate_profile(
        self, profile_name: str, profile: dict[str, object]
    ) -> list[str]:
        errors: list[str] = []
        self.validator["validate_profile"](
            self.matrix, profile_name, profile, errors
        )
        return errors

    @staticmethod
    def remove_image(profile: dict[str, object], image_name: str) -> None:
        profile["images"] = [
            image for image in profile["images"] if image["name"] != image_name
        ]
        for group in profile["build_groups"]:
            group["images"] = [
                image for image in group["images"] if image != image_name
            ]

    def test_matrix_declares_exact_seven_stream_policy(self) -> None:
        self.assertEqual(self.matrix["schema_version"], 2)
        self.assertEqual(self.matrix["owner"], "supergate-hub")
        self.assertEqual(self.matrix["repository"], "kolla-container-images")
        self.assertEqual(self.matrix["registry"], "ghcr.io")
        self.assertEqual(self.matrix["profiles"], ["core", "deployment"])
        self.assertEqual(self.matrix["architectures"], ["amd64", "arm64"])
        self.assertEqual(
            self.matrix["tag_policy"],
            {
                "deploy_tag_template": "{release}-{distro}-{tag_token}",
                "candidate_tag_template": (
                    "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
                ),
                "candidate_arch_tag_template": (
                    "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
                ),
            },
        )
        self.assertEqual(stream_ids(self.matrix), list(EXPECTED_STREAMS))

        for stream_id, expected in EXPECTED_STREAMS.items():
            with self.subTest(stream=stream_id):
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
                self.assertIs(stream["publish_enabled"], True)
                self.assertEqual(render_tag(self.matrix, stream), stream_id)

    def test_profiles_review_every_stream_and_resolve_neutron_aliases(self) -> None:
        for profile_name in self.matrix["profiles"]:
            with self.subTest(profile=profile_name):
                profile = load_json(PROFILES_DIR / f"{profile_name}.json")
                self.assertEqual(profile["schema_version"], 3)
                self.assertEqual(
                    set(profile["reviewed_streams"]), set(EXPECTED_STREAMS)
                )
                self.assertEqual(len(profile["reviewed_streams"]), 7)

                neutron = next(
                    image
                    for image in profile["images"]
                    if image["name"] == "neutron-server"
                )
                self.assertEqual(neutron["kolla_ansible_variables"], NEUTRON_VARIABLES)

                for stream_id in EXPECTED_STREAMS:
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

        for stream_id, expected_count in DEPLOYMENT_EXPECTED_COUNTS.items():
            with self.subTest(stream=stream_id):
                stream = find_stream(self.matrix, stream_id)
                resolved = resolve_profile(profile, stream)
                image_names = {image["name"] for image in resolved["images"]}

                self.assertEqual(len(image_names), expected_count)
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

    def test_runtime_validator_rejects_coherent_core_leaf_removal(self) -> None:
        profile = copy.deepcopy(load_json(PROFILES_DIR / "core.json"))
        self.remove_image(profile, "neutron-dhcp-agent")

        errors = self.validate_profile("core", profile)

        self.assertTrue(
            any("resolved image set must be exactly" in error for error in errors),
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

        errors = self.validate_profile("deployment", profile)

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

        errors = self.validate_profile("deployment", profile)

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
        group["applies_to"] = {
            "streams": ["2025.1-rocky-9"],
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

        errors = self.validate_profile("deployment", profile)

        self.assertTrue(
            any(
                "resolved parent set must be exactly" in error
                and "2025.2" in error
                for error in errors
            ),
            errors,
        )

    def test_malformed_tag_templates_fail_closed(self) -> None:
        validator = runpy.run_path(str(ROOT / "scripts" / "validate-config.py"))
        validate_matrix = validator["validate_matrix"]

        for template in (
            "{}",
            "{release.missing}-{distro}-{tag_token}",
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

        cases = (
            ("deploy_tag_template", "{}", "deploy_tag_template fields"),
            (
                "candidate_tag_template",
                "{release}-{distro}-{tag_token}",
                "candidate_tag_template fields",
            ),
            (
                "candidate_arch_tag_template",
                "{release}-{distro}-{tag_token}-candidate-{candidate_id}",
                "candidate_arch_tag_template fields",
            ),
        )
        for field, template, expected_error in cases:
            with self.subTest(field=field):
                matrix = copy.deepcopy(self.matrix)
                matrix["tag_policy"][field] = template
                errors: list[str] = []
                validate_matrix(matrix, errors)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )


if __name__ == "__main__":
    unittest.main()
