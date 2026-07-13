from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "profiles" / "deployment.json"
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
GENERATE_LOCK = ROOT / "scripts" / "generate-lock.py"
VALIDATE_SUMMARY = ROOT / "scripts" / "validate-publish-summary.py"

EXPECTED_LEAVES = {
    "cron",
    "fluentd",
    "glance-api",
    "grafana",
    "haproxy",
    "heat-api",
    "heat-api-cfn",
    "heat-engine",
    "horizon",
    "keepalived",
    "keystone",
    "keystone-fernet",
    "keystone-ssh",
    "kolla-toolbox",
    "mariadb-server",
    "memcached",
    "neutron-metadata-agent",
    "neutron-server",
    "nova-api",
    "nova-compute",
    "nova-conductor",
    "nova-libvirt",
    "nova-novncproxy",
    "nova-scheduler",
    "nova-ssh",
    "octavia-api",
    "octavia-driver-agent",
    "octavia-health-manager",
    "octavia-housekeeping",
    "octavia-worker",
    "opensearch",
    "opensearch-dashboards",
    "openvswitch-db-server",
    "openvswitch-vswitchd",
    "ovn-controller",
    "ovn-nb-db-server",
    "ovn-northd",
    "ovn-sb-db-relay",
    "ovn-sb-db-server",
    "placement-api",
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
    "proxysql",
    "rabbitmq",
}

EXPECTED_VARIABLES = {
    "cron": ["cron_image_full"],
    "fluentd": ["fluentd_image_full"],
    "glance-api": ["glance_api_image_full"],
    "grafana": ["grafana_image_full"],
    "haproxy": ["haproxy_image_full"],
    "heat-api": ["heat_api_image_full"],
    "heat-api-cfn": ["heat_api_cfn_image_full"],
    "heat-engine": ["heat_engine_image_full"],
    "horizon": ["horizon_image_full"],
    "keepalived": ["keepalived_image_full"],
    "keystone": ["keystone_image_full"],
    "keystone-fernet": ["keystone_fernet_image_full"],
    "keystone-ssh": ["keystone_ssh_image_full"],
    "kolla-toolbox": ["kolla_toolbox_image_full"],
    "mariadb-server": ["mariadb_image_full"],
    "memcached": ["memcached_image_full"],
    "neutron-metadata-agent": [
        "neutron_metadata_agent_image_full",
        "neutron_ovn_metadata_agent_image_full",
    ],
    "neutron-server": ["neutron_server_image_full"],
    "nova-api": ["nova_api_image_full"],
    "nova-compute": ["nova_compute_image_full"],
    "nova-conductor": [
        "nova_conductor_image_full",
        "nova_super_conductor_image_full",
    ],
    "nova-libvirt": ["nova_libvirt_image_full"],
    "nova-novncproxy": ["nova_novncproxy_image_full"],
    "nova-scheduler": ["nova_scheduler_image_full"],
    "nova-ssh": ["nova_ssh_image_full"],
    "octavia-api": ["octavia_api_image_full"],
    "octavia-driver-agent": ["octavia_driver_agent_image_full"],
    "octavia-health-manager": ["octavia_health_manager_image_full"],
    "octavia-housekeeping": ["octavia_housekeeping_image_full"],
    "octavia-worker": ["octavia_worker_image_full"],
    "opensearch": ["opensearch_image_full"],
    "opensearch-dashboards": ["opensearch_dashboards_image_full"],
    "openvswitch-db-server": ["openvswitch_db_image_full"],
    "openvswitch-vswitchd": ["openvswitch_vswitchd_image_full"],
    "ovn-controller": ["ovn_controller_image_full"],
    "ovn-nb-db-server": ["ovn_nb_db_image_full"],
    "ovn-northd": ["ovn_northd_image_full"],
    "ovn-sb-db-relay": ["ovn_sb_db_relay_image_full"],
    "ovn-sb-db-server": ["ovn_sb_db_image_full"],
    "placement-api": ["placement_api_image_full"],
    "prometheus-alertmanager": ["prometheus_alertmanager_image_full"],
    "prometheus-blackbox-exporter": ["prometheus_blackbox_exporter_image_full"],
    "prometheus-cadvisor": ["prometheus_cadvisor_image_full"],
    "prometheus-elasticsearch-exporter": [
        "prometheus_elasticsearch_exporter_image_full"
    ],
    "prometheus-libvirt-exporter": ["prometheus_libvirt_exporter_image_full"],
    "prometheus-memcached-exporter": [
        "prometheus_memcached_exporter_image_full"
    ],
    "prometheus-mysqld-exporter": ["prometheus_mysqld_exporter_image_full"],
    "prometheus-node-exporter": ["prometheus_node_exporter_image_full"],
    "prometheus-openstack-exporter": [
        "prometheus_openstack_exporter_image_full"
    ],
    "prometheus-server": ["prometheus_server_image_full"],
    "proxysql": ["proxysql_image_full"],
    "rabbitmq": ["rabbitmq_image_full"],
}

EXPECTED_PARENTS = {
    "base",
    "glance-base",
    "heat-base",
    "keystone-base",
    "mariadb-base",
    "neutron-base",
    "nova-base",
    "octavia-base",
    "openstack-base",
    "openvswitch-base",
    "ovn-base",
    "placement-base",
    "prometheus-base",
}


def load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def run_plan() -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(PLAN_PUBLISH),
            "--profile",
            "deployment",
            "--release",
            "2025.1",
            "--distro",
            "rocky",
            "--distro-version",
            "9",
            "--dry-run",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def digest(index: int) -> str:
    return f"sha256:{index:064x}"


def deployment_summary(profile: dict) -> dict:
    tag = "2025.1-rocky-9"
    return {
        "release": "2025.1",
        "distro": "rocky",
        "distro_version": "9",
        "profile": "deployment",
        "registry": "ghcr.io",
        "owner": "supergate-jhbyun",
        "repository": "kolla-container-images",
        "images": [
            {
                "image": image["name"],
                "kolla_ansible_variables": image["kolla_ansible_variables"],
                "deploy_ref": (
                    "ghcr.io/supergate-jhbyun/kolla-container-images/"
                    f"{image['name']}:{tag}"
                ),
                "deploy_tag": tag,
                "manifest_digest": digest(index),
                "architectures": [
                    {
                        "arch": arch,
                        "platform": f"linux/{arch}",
                        "arch_ref": (
                            "ghcr.io/supergate-jhbyun/kolla-container-images/"
                            f"{image['name']}:{tag}-{arch}"
                        ),
                    }
                    for arch in ("amd64", "arm64")
                ],
            }
            for index, image in enumerate(profile["images"], 1)
        ],
    }


class DeploymentProfileTest(unittest.TestCase):
    def test_observed_candidates_equal_fresh_deploy_closure(self) -> None:
        profile_images = {image["name"] for image in load_profile()["images"]}

        self.assertEqual(len(profile_images), 52)
        self.assertEqual(profile_images, EXPECTED_LEAVES)
        self.assertEqual(EXPECTED_LEAVES - profile_images, set())
        self.assertEqual(profile_images - EXPECTED_LEAVES, set())

    def test_image_variable_mapping_is_exact_and_aliases_are_present(self) -> None:
        profile = load_profile()
        actual = {
            image["name"]: image["kolla_ansible_variables"]
            for image in profile["images"]
        }
        variables = [variable for values in actual.values() for variable in values]

        self.assertEqual(actual, EXPECTED_VARIABLES)
        self.assertEqual(len(variables), 54)
        self.assertEqual(len(variables), len(set(variables)))

    def test_build_groups_cover_every_leaf_exactly_once(self) -> None:
        profile = load_profile()
        grouped = [image for group in profile["build_groups"] for image in group["images"]]

        self.assertEqual(len(profile["build_groups"]), 17)
        self.assertEqual(len(grouped), 52)
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(grouped), EXPECTED_LEAVES)

    def test_planner_has_exact_parent_dependency_closure(self) -> None:
        plan = run_plan()

        self.assertEqual(set(plan["build"]["parents"]["images"]), EXPECTED_PARENTS)
        self.assertEqual(len(plan["build"]["parents"]["images"]), 13)
        ovn = next(group for group in plan["build"]["groups"] if group["name"] == "ovn")
        self.assertEqual(
            ovn["parents"],
            ["base", "openvswitch-base", "ovn-base"],
        )
        self.assertEqual(
            [ref.rsplit("/", 1)[-1].split(":", 1)[0] for ref in ovn["architectures"][0]["parent_refs"]],
            ovn["parents"],
        )

    def test_planner_renders_all_native_architecture_and_deploy_refs(self) -> None:
        plan = run_plan()
        arch_refs = [
            architecture["arch_ref"]
            for image in plan["images"]
            for architecture in image["architectures"]
        ]
        deploy_refs = [image["deploy_ref"] for image in plan["images"]]

        self.assertEqual(len(plan["build"]["parents"]["architectures"]), 2)
        self.assertEqual(len(plan["build"]["groups"]), 17)
        self.assertEqual(
            sum(len(group["architectures"]) for group in plan["build"]["groups"]),
            34,
        )
        self.assertEqual(len(arch_refs), 104)
        self.assertEqual(len(set(arch_refs)), 104)
        self.assertEqual(len(deploy_refs), 52)
        self.assertEqual(len(set(deploy_refs)), 52)
        self.assertEqual(
            {architecture["platform"] for image in plan["images"] for architecture in image["architectures"]},
            {"linux/amd64", "linux/arm64"},
        )

    def test_full_summary_generates_digest_pinned_candidate_lock(self) -> None:
        profile = load_profile()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            summary_path = temp_path / "publish-summary.json"
            lock_path = temp_path / "deployment-lock.yml"
            summary = deployment_summary(profile)
            summary_path.write_text(
                json.dumps(summary),
                encoding="utf-8",
            )

            summary_result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATE_SUMMARY),
                    "--publish-summary",
                    str(summary_path),
                    "--profile",
                    "deployment",
                    "--release",
                    "2025.1",
                    "--distro",
                    "rocky",
                    "--distro-version",
                    "9",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(summary_result.returncode, 0, summary_result.stderr)

            generate_result = subprocess.run(
                [
                    sys.executable,
                    str(GENERATE_LOCK),
                    "--publish-summary",
                    str(summary_path),
                    "--profile",
                    "deployment",
                    "--release",
                    "2025.1",
                    "--distro",
                    "rocky",
                    "--distro-version",
                    "9",
                    "--output",
                    str(lock_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(generate_result.returncode, 0, generate_result.stderr)
            lock = lock_path.read_text(encoding="utf-8")
            for image in summary["images"]:
                expected_ref = f'{image["deploy_ref"]}@{image["manifest_digest"]}'
                for variable in image["kolla_ansible_variables"]:
                    self.assertIn(f'{variable}: "{expected_ref}"', lock)


if __name__ == "__main__":
    unittest.main()
