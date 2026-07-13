from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"


def plan_command(*extra_args: str) -> list[str]:
    return [
        sys.executable,
        str(PLAN_PUBLISH),
        "--profile",
        "core",
        "--release",
        "2025.1",
        "--distro",
        "rocky",
        "--distro-version",
        "9",
        *extra_args,
        "--dry-run",
    ]


def run_plan(*extra_args: str) -> dict:
    result = subprocess.run(
        plan_command(*extra_args),
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


class PlanPublishTest(unittest.TestCase):
    def test_core_profile_images_are_included(self) -> None:
        plan = run_plan()
        image_names = {image["image"] for image in plan["images"]}

        self.assertEqual(
            image_names,
            {
                "keystone",
                "keystone-fernet",
                "keystone-ssh",
                "glance-api",
                "placement-api",
                "nova-api",
                "nova-scheduler",
                "nova-conductor",
                "nova-compute",
                "nova-libvirt",
                "nova-ssh",
                "nova-novncproxy",
                "neutron-server",
                "neutron-dhcp-agent",
                "neutron-l3-agent",
                "neutron-metadata-agent",
                "neutron-openvswitch-agent",
                "heat-api",
                "heat-api-cfn",
                "heat-engine",
                "horizon",
            },
        )

    def test_core_profile_includes_kolla_ansible_variables(self) -> None:
        plan = run_plan()
        variables_by_image = {
            image["image"]: image["kolla_ansible_variables"] for image in plan["images"]
        }

        self.assertEqual(variables_by_image["keystone"], ["keystone_image_full"])
        self.assertEqual(variables_by_image["glance-api"], ["glance_api_image_full"])
        self.assertEqual(variables_by_image["nova-compute"], ["nova_compute_image_full"])
        self.assertEqual(
            variables_by_image["nova-conductor"],
            ["nova_super_conductor_image_full", "nova_conductor_image_full"],
        )

    def test_image_filter_keeps_only_requested_image(self) -> None:
        plan = run_plan("--image", "keystone")

        self.assertEqual(plan["image_filter"], "keystone")
        self.assertEqual([image["image"] for image in plan["images"]], ["keystone"])

    def test_unknown_image_filter_fails(self) -> None:
        result = subprocess.run(
            plan_command("--image", "missing-image"),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("image does not exist in profile core: missing-image", result.stderr)

    def test_filter_uses_actual_kolla_image_name(self) -> None:
        plan = run_plan("--image", "glance-api")

        self.assertEqual(plan["image_filter"], "glance-api")
        self.assertEqual([image["image"] for image in plan["images"]], ["glance-api"])
        self.assertEqual(
            plan["images"][0]["kolla_ansible_variables"],
            ["glance_api_image_full"],
        )
        self.assertEqual(
            plan["build"]["parents"]["images"],
            ["base", "openstack-base", "glance-base"],
        )
        self.assertEqual(
            [group["name"] for group in plan["build"]["groups"]],
            ["glance"],
        )

    def test_full_core_build_groups_cover_each_image_once(self) -> None:
        plan = run_plan()
        groups = plan["build"]["groups"]

        self.assertEqual(
            [group["name"] for group in groups],
            [
                "identity",
                "glance",
                "placement",
                "compute",
                "network",
                "orchestration",
                "dashboard",
            ],
        )
        self.assertEqual(
            plan["build"]["parents"]["images"],
            [
                "base",
                "openstack-base",
                "keystone-base",
                "glance-base",
                "placement-base",
                "nova-base",
                "neutron-base",
                "heat-base",
            ],
        )
        grouped_images = [image for group in groups for image in group["images"]]
        self.assertEqual(len(grouped_images), 21)
        self.assertEqual(
            set(grouped_images),
            {image["image"] for image in plan["images"]},
        )
        self.assertEqual(
            sum(len(group["architectures"]) for group in groups),
            14,
        )

    def test_deploy_tags_do_not_include_arch(self) -> None:
        plan = run_plan()

        for image in plan["images"]:
            self.assertEqual(image["deploy_tag"], "2025.1-rocky-9")
            self.assertTrue(image["deploy_ref"].endswith(":2025.1-rocky-9"))
            self.assertFalse(image["deploy_tag"].endswith("-amd64"))
            self.assertFalse(image["deploy_tag"].endswith("-arm64"))

    def test_arch_tags_include_arch(self) -> None:
        plan = run_plan()

        for image in plan["images"]:
            arch_tags = {arch["arch"]: arch["arch_tag"] for arch in image["architectures"]}
            self.assertEqual(arch_tags["amd64"], "2025.1-rocky-9-amd64")
            self.assertEqual(arch_tags["arm64"], "2025.1-rocky-9-arm64")

    def test_expected_ghcr_refs_are_rendered(self) -> None:
        plan = run_plan()
        first_image = plan["images"][0]

        self.assertEqual(plan["registry"], "ghcr.io")
        self.assertEqual(plan["owner"], "supergate-jhbyun")
        self.assertEqual(
            first_image["expected_ghcr_ref"],
            "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9",
        )
        self.assertEqual(
            first_image["architectures"][0]["expected_ghcr_ref"],
            "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64",
        )

    def test_publish_and_candidate_lock_artifact_paths_are_rendered(self) -> None:
        plan = run_plan()

        self.assertEqual(
            plan["publish_summary_file"],
            "artifacts/publish-summary-2025.1-rocky-9.json",
        )
        self.assertEqual(
            plan["kolla_ansible_lock_file"],
            "artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml",
        )
        self.assertNotIn(ENVIRONMENT_LOCK_FIELD, plan)

    def test_parent_build_command_builds_shared_parents_once(self) -> None:
        plan = run_plan()
        parent_arch = plan["build"]["parents"]["architectures"][0]
        command = parent_arch["commands"]["kolla_build_push"]

        self.assertEqual(command[0], "kolla-build")
        self.assertIn("--base", command)
        self.assertIn("rocky", command)
        self.assertIn("--base-tag", command)
        self.assertIn("9", command)
        self.assertIn("--base-arch", command)
        self.assertIn("x86_64", command)
        self.assertIn("--platform", command)
        self.assertIn("linux/amd64", command)
        self.assertIn("--registry", command)
        self.assertIn("ghcr.io", command)
        self.assertIn("--namespace", command)
        self.assertIn("supergate-jhbyun/kolla-container-images", command)
        self.assertIn("--tag", command)
        self.assertIn("2025.1-rocky-9-amd64", command)
        self.assertIn("--threads", command)
        self.assertIn("4", command)
        self.assertIn("--push-threads", command)
        self.assertIn("1", command)
        self.assertIn("--summary-json-file", command)
        self.assertIn("--push", command)
        self.assertNotIn("--skip-existing", command)
        self.assertEqual(
            command[-8:],
            [
                "^base$",
                "^openstack-base$",
                "^keystone-base$",
                "^glance-base$",
                "^placement-base$",
                "^nova-base$",
                "^neutron-base$",
                "^heat-base$",
            ],
        )

    def test_group_build_command_skips_only_prepulled_images(self) -> None:
        plan = run_plan()
        compute = next(group for group in plan["build"]["groups"] if group["name"] == "compute")
        amd64 = next(arch for arch in compute["architectures"] if arch["arch"] == "amd64")
        command = amd64["commands"]["kolla_build_push"]

        self.assertEqual(
            amd64["parent_refs"],
            [
                "ghcr.io/supergate-jhbyun/kolla-container-images/base:2025.1-rocky-9-amd64",
                "ghcr.io/supergate-jhbyun/kolla-container-images/openstack-base:2025.1-rocky-9-amd64",
                "ghcr.io/supergate-jhbyun/kolla-container-images/nova-base:2025.1-rocky-9-amd64",
            ],
        )
        self.assertIn("--skip-existing", command)
        self.assertNotIn("--skip-parents", command)
        self.assertEqual(command[-7:], [f"^{image}$" for image in compute["images"]])

    def test_keystone_only_build_plan_keeps_one_parent_and_one_group(self) -> None:
        plan = run_plan("--image", "keystone")

        self.assertEqual(
            plan["build"]["parents"]["images"],
            ["base", "openstack-base", "keystone-base"],
        )
        self.assertEqual(len(plan["build"]["parents"]["architectures"]), 2)
        self.assertEqual(
            [group["name"] for group in plan["build"]["groups"]],
            ["identity"],
        )
        self.assertEqual(plan["build"]["groups"][0]["images"], ["keystone"])

    def test_manifest_commands_use_per_arch_refs(self) -> None:
        plan = run_plan()
        first_image = plan["images"][0]

        self.assertEqual(
            first_image["commands"]["manifest_create"],
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9",
                "--metadata-file",
                "artifacts/manifests/keystone-2025.1-rocky-9.json",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-arm64",
            ],
        )
        self.assertEqual(
            first_image["manifest_metadata_file"],
            "artifacts/manifests/keystone-2025.1-rocky-9.json",
        )
        self.assertEqual(
            first_image["commands"]["manifest_inspect"],
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9",
            ],
        )

    def test_keystone_only_plan_keeps_arch_refs_and_manifest_command(self) -> None:
        plan = run_plan("--image", "keystone")
        image = plan["images"][0]

        self.assertEqual(
            [arch["arch_ref"] for arch in image["architectures"]],
            [
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-arm64",
            ],
        )
        self.assertEqual(
            image["commands"]["manifest_create"],
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9",
                "--metadata-file",
                "artifacts/manifests/keystone-2025.1-rocky-9.json",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64",
                "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-arm64",
            ],
        )

    def test_refuses_without_dry_run(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--profile",
                "core",
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

        self.assertEqual(result.returncode, 2)
        self.assertIn("--dry-run", result.stderr)


if __name__ == "__main__":
    unittest.main()
