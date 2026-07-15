from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.profile_resolver import find_stream, resolve_profile


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILE_PATH = ROOT / "config" / "profiles" / "deployment.json"

EXPECTED_COUNTS = {
    "2025.1-rocky-9": 63,
    "2025.1-rocky-10": 63,
    "2025.1-ubuntu-noble": 64,
    "2025.2-rocky-10": 63,
    "2025.2-ubuntu-noble": 64,
    "2026.1-rocky-10": 65,
    "2026.1-ubuntu-noble": 66,
}

BASE_LEAVES = {
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

CINDER_LEAVES = {
    "cinder-api",
    "cinder-backup",
    "cinder-scheduler",
    "cinder-volume",
}
MANILA_LEAVES = {
    "manila-api",
    "manila-data",
    "manila-scheduler",
    "manila-share",
}
OCTAVIA_LEAVES = {
    "octavia-api",
    "octavia-driver-agent",
    "octavia-health-manager",
    "octavia-housekeeping",
    "octavia-worker",
}
VALKEY_LEAVES = {"valkey-server", "valkey-sentinel"}
LOGGING_LEAVES = {"fluentd", "opensearch", "opensearch-dashboards"}
BASE_PROMETHEUS_IMAGES = [
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
]
COMMON_ADDITIONS = CINDER_LEAVES | MANILA_LEAVES | VALKEY_LEAVES | {"iscsid"}
NEW_2026_EXPORTERS = {
    "prometheus-openstack-network-exporter",
    "prometheus-valkey-exporter",
}
NEUTRON_ALIASES = [
    "neutron_rpc_server_image_full",
    "neutron_periodic_worker_image_full",
    "neutron_ovn_maintenance_worker_image_full",
]
EXCLUDED_LEAVES = {"etcd", "multipathd", "redis", "redis-sentinel"}
EXCLUDED_FAMILY_PREFIXES = ("ceph-", "designate-", "swift-", "ironic-")
EXPECTED_OVN_IMAGE_ORDER = [
    "ovn-controller",
    "ovn-nb-db-server",
    "ovn-northd",
    "ovn-sb-db-relay",
    "ovn-sb-db-server",
]

EXPECTED_NEW_GROUPS = {
    "coordination": {
        "name": "coordination",
        "parent": "valkey-base",
        "parents": ["base", "valkey-base"],
        "images": ["valkey-server", "valkey-sentinel"],
    },
    "storage-runtime": {
        "name": "storage-runtime",
        "parent": "base",
        "parents": ["base"],
        "images": ["iscsid", "tgtd"],
    },
    "cinder": {
        "name": "cinder",
        "parent": "cinder-base",
        "parents": ["base", "openstack-base", "cinder-base"],
        "images": [
            "cinder-api",
            "cinder-backup",
            "cinder-scheduler",
            "cinder-volume",
        ],
    },
    "manila": {
        "name": "manila",
        "parent": "manila-base",
        "parents": ["base", "openstack-base", "manila-base"],
        "images": [
            "manila-api",
            "manila-data",
            "manila-scheduler",
            "manila-share",
        ],
    },
}

EXPECTED_NEW_IMAGE_MAPPINGS = {
    "cinder-api": {
        "name": "cinder-api",
        "kolla_ansible_variables": ["cinder_api_image_full"],
    },
    "cinder-backup": {
        "name": "cinder-backup",
        "kolla_ansible_variables": ["cinder_backup_image_full"],
    },
    "cinder-scheduler": {
        "name": "cinder-scheduler",
        "kolla_ansible_variables": ["cinder_scheduler_image_full"],
    },
    "cinder-volume": {
        "name": "cinder-volume",
        "kolla_ansible_variables": ["cinder_volume_image_full"],
    },
    "iscsid": {
        "name": "iscsid",
        "kolla_ansible_variables": ["iscsid_image_full"],
    },
    "manila-api": {
        "name": "manila-api",
        "kolla_ansible_variables": ["manila_api_image_full"],
    },
    "manila-data": {
        "name": "manila-data",
        "kolla_ansible_variables": ["manila_data_image_full"],
    },
    "manila-scheduler": {
        "name": "manila-scheduler",
        "kolla_ansible_variables": ["manila_scheduler_image_full"],
    },
    "manila-share": {
        "name": "manila-share",
        "kolla_ansible_variables": ["manila_share_image_full"],
    },
    "valkey-server": {
        "name": "valkey-server",
        "kolla_ansible_variables": ["valkey_image_full"],
    },
    "valkey-sentinel": {
        "name": "valkey-sentinel",
        "kolla_ansible_variables": ["valkey_sentinel_image_full"],
    },
    "tgtd": {
        "name": "tgtd",
        "kolla_ansible_variables": ["tgtd_image_full"],
        "applies_to": {"distros": ["ubuntu"]},
    },
    "prometheus-openstack-network-exporter": {
        "name": "prometheus-openstack-network-exporter",
        "kolla_ansible_variables": [
            "prometheus_openstack_network_exporter_image_full"
        ],
        "applies_to": {"releases": ["2026.1"]},
    },
    "prometheus-valkey-exporter": {
        "name": "prometheus-valkey-exporter",
        "kolla_ansible_variables": ["prometheus_valkey_exporter_image_full"],
        "applies_to": {"releases": ["2026.1"]},
    },
}


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class DeploymentProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_json(MATRIX_PATH)
        self.profile = load_json(PROFILE_PATH)

    def test_resolved_closure_is_exact_for_every_stream(self) -> None:
        required_common = (
            CINDER_LEAVES
            | MANILA_LEAVES
            | OCTAVIA_LEAVES
            | VALKEY_LEAVES
            | LOGGING_LEAVES
            | set(BASE_PROMETHEUS_IMAGES)
            | {"grafana", "iscsid"}
        )

        for stream_id, expected_count in EXPECTED_COUNTS.items():
            with self.subTest(stream=stream_id):
                stream = find_stream(self.matrix, stream_id)
                resolved = resolve_profile(self.profile, stream)
                images = resolved["images"]
                image_names = {image["name"] for image in images}

                expected_names = BASE_LEAVES | COMMON_ADDITIONS
                if stream["distro"] == "ubuntu":
                    expected_names |= {"tgtd"}
                if stream["release"] == "2026.1":
                    expected_names |= NEW_2026_EXPORTERS

                self.assertEqual(len(image_names), expected_count)
                self.assertEqual(image_names, expected_names)
                self.assertEqual(len(images), len(image_names))
                self.assertEqual(
                    [
                        image["name"]
                        for image in images
                        if image["name"] in EXPECTED_OVN_IMAGE_ORDER
                    ],
                    EXPECTED_OVN_IMAGE_ORDER,
                )
                self.assertTrue(required_common <= image_names)

                self.assertEqual("tgtd" in image_names, stream["distro"] == "ubuntu")
                self.assertEqual(
                    NEW_2026_EXPORTERS <= image_names,
                    stream["release"] == "2026.1",
                )
                if stream["release"] != "2026.1":
                    self.assertTrue(NEW_2026_EXPORTERS.isdisjoint(image_names))

                self.assertTrue(EXCLUDED_LEAVES.isdisjoint(image_names))
                self.assertFalse(
                    any(
                        image == "ceph"
                        or image == "designate"
                        or image == "swift"
                        or image == "ironic"
                        or image.startswith(EXCLUDED_FAMILY_PREFIXES)
                        for image in image_names
                    )
                )
                self.assertFalse(
                    any(
                        image == "redis" or image.startswith("redis-")
                        for image in image_names
                    )
                )

                neutron = next(
                    image for image in images if image["name"] == "neutron-server"
                )
                expected_neutron_variables = ["neutron_server_image_full"]
                if stream["release"] in {"2025.2", "2026.1"}:
                    expected_neutron_variables.extend(NEUTRON_ALIASES)
                self.assertEqual(
                    neutron["kolla_ansible_variables"], expected_neutron_variables
                )

                grouped = [
                    image
                    for group in resolved["build_groups"]
                    for image in group["images"]
                ]
                self.assertEqual(len(grouped), len(set(grouped)))
                self.assertEqual(set(grouped), image_names)

                variables = [
                    variable
                    for image in images
                    for variable in image["kolla_ansible_variables"]
                ]
                self.assertTrue(
                    all("applies_to" not in image for image in resolved["images"])
                )
                self.assertEqual(len(variables), len(set(variables)))

    def test_new_build_groups_and_monitoring_membership_are_exact(self) -> None:
        groups = {group["name"]: group for group in self.profile["build_groups"]}
        actual_new_groups = {
            name: groups[name] for name in EXPECTED_NEW_GROUPS if name in groups
        }

        self.assertEqual(actual_new_groups, EXPECTED_NEW_GROUPS)
        self.assertEqual(
            groups["monitoring"]["images"],
            BASE_PROMETHEUS_IMAGES
            + [
                "prometheus-openstack-network-exporter",
                "prometheus-valkey-exporter",
            ],
        )

    def test_new_image_mappings_and_selectors_are_exact(self) -> None:
        images = {image["name"]: image for image in self.profile["images"]}
        actual_new_mappings = {
            name: images[name]
            for name in EXPECTED_NEW_IMAGE_MAPPINGS
            if name in images
        }

        self.assertEqual(actual_new_mappings, EXPECTED_NEW_IMAGE_MAPPINGS)

    def test_ovn_relay_has_its_exact_selected_leaf_dependency(self) -> None:
        expected_ovn = {
            "name": "ovn",
            "parent": "ovn-base",
            "parents": ["base", "openvswitch-base", "ovn-base"],
            "images": [
                "ovn-controller",
                "ovn-nb-db-server",
                "ovn-northd",
                "ovn-sb-db-server",
            ],
        }
        expected_relay = {
            "name": "ovn-sb-db-relay",
            "parent": "ovn-sb-db-server",
            "parents": [
                "base",
                "openvswitch-base",
                "ovn-base",
                "ovn-sb-db-server",
            ],
            "images": ["ovn-sb-db-relay"],
        }
        groups = {group["name"]: group for group in self.profile["build_groups"]}

        self.assertEqual(groups["ovn"], expected_ovn)
        self.assertEqual(groups["ovn-sb-db-relay"], expected_relay)
        for stream_id in EXPECTED_COUNTS:
            with self.subTest(stream=stream_id):
                stream = find_stream(self.matrix, stream_id)
                resolved = resolve_profile(self.profile, stream)
                resolved_groups = {
                    group["name"]: group for group in resolved["build_groups"]
                }
                self.assertEqual(resolved_groups["ovn"], expected_ovn)
                self.assertEqual(resolved_groups["ovn-sb-db-relay"], expected_relay)

    def test_database_parent_chain_is_pinned_to_each_kolla_release(self) -> None:
        groups = {group["name"]: group for group in self.profile["build_groups"]}
        self.assertEqual(
            groups["database-2025-1"],
            {
                "name": "database-2025-1",
                "parent": "mariadb-base",
                "parents": ["base", "mariadb-base"],
                "images": ["mariadb-server"],
                "applies_to": {"releases": ["2025.1"]},
            },
        )
        self.assertEqual(
            groups["database-modern"],
            {
                "name": "database-modern",
                "parent": "base",
                "parents": ["base"],
                "images": ["mariadb-server"],
                "applies_to": {"releases": ["2025.2", "2026.1"]},
            },
        )
        self.assertNotIn("database", groups)

        for stream_id in EXPECTED_COUNTS:
            with self.subTest(stream=stream_id):
                stream = find_stream(self.matrix, stream_id)
                resolved = resolve_profile(self.profile, stream)
                database = next(
                    group
                    for group in resolved["build_groups"]
                    if "mariadb-server" in group["images"]
                )
                expected_parents = (
                    ["base", "mariadb-base"]
                    if stream["release"] == "2025.1"
                    else ["base"]
                )
                self.assertEqual(database["parents"], expected_parents)
                self.assertEqual(database["parent"], expected_parents[-1])
                self.assertNotIn("applies_to", database)


if __name__ == "__main__":
    unittest.main()
