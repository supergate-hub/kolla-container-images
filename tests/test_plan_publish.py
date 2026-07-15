from __future__ import annotations

import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
PARENT_FIXTURE = ROOT / "tests" / "fixtures" / "kolla-parent-dependencies.json"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"
STREAM_IDS = [
    "2025.1-rocky-9",
    "2025.1-rocky-10",
    "2025.1-ubuntu-noble",
    "2025.2-rocky-10",
    "2025.2-ubuntu-noble",
    "2026.1-rocky-10",
    "2026.1-ubuntu-noble",
]
STREAM_EXPECTATIONS = {
    "2025.1-rocky-9": ("2025.1", "rocky", "9", "20.4.0", 63, 16),
    "2025.1-rocky-10": ("2025.1", "rocky", "10", "20.4.0", 63, 16),
    "2025.1-ubuntu-noble": ("2025.1", "ubuntu", "24.04", "20.4.0", 64, 16),
    "2025.2-rocky-10": ("2025.2", "rocky", "10", "21.1.0", 63, 15),
    "2025.2-ubuntu-noble": ("2025.2", "ubuntu", "24.04", "21.1.0", 64, 15),
    "2026.1-rocky-10": ("2026.1", "rocky", "10", "22.0.0", 65, 15),
    "2026.1-ubuntu-noble": ("2026.1", "ubuntu", "24.04", "22.0.0", 66, 15),
}
ARCHITECTURES = {
    "amd64": {
        "kolla_base_arch": "x86_64",
        "platform": "linux/amd64",
        "runner": "ubuntu-24.04",
        "runner_machine": "x86_64",
        "runner_labels": ["ubuntu-24.04"],
    },
    "arm64": {
        "kolla_base_arch": "aarch64",
        "platform": "linux/arm64",
        "runner": "ubuntu-24.04-arm",
        "runner_machine": "aarch64",
        "runner_labels": ["ubuntu-24.04-arm"],
    },
}
TEST_CANDIDATE_ID = "123456789-1"


def expected_candidate_tag(stream: str, arch: str | None = None) -> str:
    tag = f"{stream}-candidate-{TEST_CANDIDATE_ID}"
    return f"{tag}-{arch}" if arch else tag


def expected_ref(image: str, stream: str, arch: str | None = None) -> str:
    return (
        "ghcr.io/supergate-hub/kolla-container-images/"
        f"{image}:{expected_candidate_tag(stream, arch)}"
    )


def plan_command(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str | None = TEST_CANDIDATE_ID,
    dry_run: bool = True,
) -> list[str]:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream",
        stream,
        "--profile",
        profile,
    ]
    if image is not None:
        command.extend(["--image", image])
    if candidate_id is not None:
        command.extend(["--candidate-id", candidate_id])
    if dry_run:
        command.append("--dry-run")
    return command


def run_plan(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str | None = TEST_CANDIDATE_ID,
) -> dict:
    result = subprocess.run(
        plan_command(
            stream=stream,
            profile=profile,
            image=image,
            candidate_id=candidate_id,
        ),
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def option_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def parent_units(plan: dict) -> list[dict]:
    return [
        unit
        for tier in plan["build"]["parent_tiers"]
        for unit in tier["matrix"]["include"]
    ]


def leaf_units(plan: dict) -> list[dict]:
    return [
        unit
        for stage in plan["build"]["leaf_stages"]
        for unit in stage["matrix"]["include"]
    ]


def build_matrices(plan: dict) -> list[tuple[str, dict]]:
    return [
        *[
            (f"parent_tier_{entry['tier']}_matrix", entry["matrix"])
            for entry in plan["build"]["parent_tiers"]
        ],
        *[
            (f"leaf_stage_{entry['stage']}_matrix", entry["matrix"])
            for entry in plan["build"]["leaf_stages"]
        ],
    ]


def planner_symbols() -> dict:
    scripts_dir = str(PLAN_PUBLISH.parent)
    inserted = scripts_dir not in sys.path
    if inserted:
        sys.path.insert(0, scripts_dir)
    try:
        return runpy.run_path(str(PLAN_PUBLISH))
    finally:
        if inserted:
            sys.path.remove(scripts_dir)


class PlanPublishTest(unittest.TestCase):
    def test_local_default_and_explicit_workflow_candidate_refs(self) -> None:
        local = run_plan(image="keystone", candidate_id=None)
        live = run_plan(image="keystone", candidate_id=TEST_CANDIDATE_ID)

        self.assertEqual(local["candidate_id"], "local-dry-run")
        self.assertEqual(
            local["images"][0]["deploy_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone:"
            "2025.1-rocky-9-candidate-local-dry-run",
        )
        self.assertEqual(live["candidate_id"], TEST_CANDIDATE_ID)
        image = live["images"][0]
        self.assertEqual(
            image["deploy_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone:"
            "2025.1-rocky-9-candidate-123456789-1",
        )
        self.assertEqual(
            image["stream_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9",
        )
        self.assertEqual(
            [entry["arch_ref"] for entry in image["architectures"]],
            [
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-amd64",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-arm64",
            ],
        )

    def test_invalid_candidate_id_is_rejected(self) -> None:
        result = subprocess.run(
            plan_command(candidate_id="01-1"),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("candidate ID", result.stderr)

    def test_all_streams_use_candidate_build_and_deploy_tags(self) -> None:
        for stream_id in STREAM_IDS:
            with self.subTest(stream=stream_id):
                plan = run_plan(stream=stream_id, image="keystone")
                image = plan["images"][0]
                candidate_tag = f"{stream_id}-candidate-{TEST_CANDIDATE_ID}"
                self.assertEqual(plan["candidate_id"], TEST_CANDIDATE_ID)
                self.assertEqual(image["deploy_tag"], candidate_tag)
                self.assertTrue(image["deploy_ref"].endswith(f":{candidate_tag}"))
                self.assertTrue(image["stream_ref"].endswith(f":{stream_id}"))
                for architecture in plan["build"]["architectures"]:
                    arch = architecture["arch"]
                    arch_tag = f"{candidate_tag}-{arch}"
                    self.assertEqual(architecture["arch_tag"], arch_tag)
                    self.assertTrue(
                        all(
                            entry["arch_ref"].endswith(f":{arch_tag}")
                            for entry in architecture["parents"]
                        )
                    )
                    self.assertTrue(
                        all(
                            entry["arch_ref"].endswith(f":{arch_tag}")
                            for entry in architecture["images"]
                        )
                    )
                for unit in plan["build"]["all_units"]:
                    arch_tag = f"{candidate_tag}-{unit['arch']}"
                    self.assertEqual(option_value(unit["command"], "--tag"), arch_tag)
                    self.assertTrue(unit["arch_ref"].endswith(f":{arch_tag}"))

    def test_parent_sets_match_checked_in_kolla_dependency_fixture(self) -> None:
        fixture = json.loads(PARENT_FIXTURE.read_text(encoding="utf-8"))
        matrix = json.loads(
            (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
        )
        matrix_pins = {
            stream["id"]: stream["kolla_version"] for stream in matrix["streams"]
        }

        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(
            fixture["source"]["command"], "kolla-build --list-dependencies"
        )
        self.assertEqual(
            fixture["source"]["command_template"],
            "kolla-build --base <distro> --base-tag <base_tag> "
            "--base-arch x86_64 --platform linux/amd64 "
            "--openstack-release <release> --list-dependencies "
            "<anchored_leaf_regexes>",
        )
        self.assertIn("^<image>$", fixture["source"]["leaf_arguments"])
        self.assertIn("Kolla dependency graph", fixture["source"]["normalization"])
        self.assertIn("first occurrence", fixture["source"]["normalization"])
        self.assertEqual(
            [stream["id"] for stream in fixture["streams"]], STREAM_IDS
        )

        scope_inputs = {
            "core/keystone": {"profile": "core", "image": "keystone"},
            "core/all": {"profile": "core", "image": None},
            "deployment/all": {"profile": "deployment", "image": None},
        }
        for expected in fixture["streams"]:
            stream_id = expected["id"]
            with self.subTest(stream=stream_id, check="kolla-pin"):
                self.assertEqual(
                    expected["kolla_version"], matrix_pins[stream_id]
                )
            for scope, inputs in scope_inputs.items():
                with self.subTest(stream=stream_id, scope=scope):
                    plan = run_plan(stream=stream_id, **inputs)
                    self.assertEqual(
                        plan["kolla_version"], expected["kolla_version"]
                    )
                    for architecture in plan["build"]["architectures"]:
                        self.assertEqual(
                            [parent["image"] for parent in architecture["parents"]],
                            expected["scopes"][scope],
                        )

    def test_core_profile_images_and_resolved_variables_are_included(self) -> None:
        plan = run_plan(stream="2025.2-rocky-10")
        image_names = {image["image"] for image in plan["images"]}
        variables_by_image = {
            image["image"]: image["kolla_ansible_variables"]
            for image in plan["images"]
        }

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
        self.assertEqual(variables_by_image["keystone"], ["keystone_image_full"])
        self.assertEqual(
            variables_by_image["nova-conductor"],
            ["nova_super_conductor_image_full", "nova_conductor_image_full"],
        )
        self.assertEqual(
            variables_by_image["neutron-server"],
            [
                "neutron_server_image_full",
                "neutron_rpc_server_image_full",
                "neutron_periodic_worker_image_full",
                "neutron_ovn_maintenance_worker_image_full",
            ],
        )

    def test_all_streams_emit_exact_pins_native_units_and_deployment_counts(self) -> None:
        for stream_id, expected in STREAM_EXPECTATIONS.items():
            release, distro, base_tag, kolla_version, image_count, parent_count = expected
            with self.subTest(stream=stream_id):
                plan = run_plan(stream=stream_id, profile="deployment")

                self.assertEqual(plan["stream"], stream_id)
                self.assertEqual(plan["release"], release)
                self.assertEqual(plan["distro"], distro)
                self.assertEqual(plan["distro_version"], base_tag)
                self.assertEqual(plan["kolla_version"], kolla_version)
                self.assertEqual(plan["kolla_ansible_version"], kolla_version)
                self.assertEqual(
                    plan["scope"],
                    {
                        "profile": "deployment",
                        "image": "all",
                        "image_count": image_count,
                    },
                )
                self.assertEqual(len(plan["images"]), image_count)
                self.assertEqual(
                    plan["publish_summary_file"],
                    f"artifacts/publish-summary-{stream_id}.json",
                )
                self.assertEqual(
                    plan["kolla_ansible_lock_file"],
                    f"artifacts/kolla-ansible-image-lock-{stream_id}.yml",
                )
                self.assertEqual(
                    set(plan["build"]),
                    {"architectures", "parent_tiers", "leaf_stages", "all_units"},
                )
                self.assertEqual(
                    [entry["arch"] for entry in plan["build"]["architectures"]],
                    ["amd64", "arm64"],
                )

                leaf_names = [image["image"] for image in plan["images"]]
                parents = parent_units(plan)
                leaves = leaf_units(plan)
                all_units = plan["build"]["all_units"]
                self.assertEqual(
                    [tier["tier"] for tier in plan["build"]["parent_tiers"]],
                    [0, 1, 2],
                )
                self.assertEqual(
                    [stage["stage"] for stage in plan["build"]["leaf_stages"]],
                    [0, 1],
                )
                self.assertEqual(len(parents), parent_count * 2)
                self.assertEqual(len(leaves), image_count * 2)
                self.assertEqual(len(all_units), (parent_count + image_count) * 2)
                self.assertEqual(all_units, parents + leaves)
                self.assertEqual(
                    [
                        len(stage["matrix"]["include"])
                        for stage in plan["build"]["leaf_stages"]
                    ],
                    [(image_count - 1) * 2, 2],
                )
                self.assertEqual(
                    len({unit["id"] for unit in all_units}), len(all_units)
                )
                self.assertEqual(
                    len({unit["summary_file"] for unit in all_units}), len(all_units)
                )
                self.assertEqual(
                    len({unit["logs_dir"] for unit in all_units}), len(all_units)
                )
                self.assertNotIn(
                    "ovn-sb-db-server",
                    {unit["target"] for unit in parents},
                )
                stage_one_units = plan["build"]["leaf_stages"][1]["matrix"][
                    "include"
                ]
                self.assertEqual(
                    [unit["target"] for unit in stage_one_units],
                    ["ovn-sb-db-relay", "ovn-sb-db-relay"],
                )
                self.assertTrue(all(unit["tier"] == 4 for unit in stage_one_units))
                self.assertTrue(
                    all(
                        unit["ancestor_chain"]
                        == [
                            "base",
                            "openvswitch-base",
                            "ovn-base",
                            "ovn-sb-db-server",
                        ]
                        for unit in stage_one_units
                    )
                )
                for image in plan["images"]:
                    image_name = image["image"]
                    self.assertEqual(
                        image["deploy_tag"], expected_candidate_tag(stream_id)
                    )
                    self.assertEqual(
                        image["deploy_ref"],
                        "ghcr.io/supergate-hub/kolla-container-images/"
                        f"{image_name}:{expected_candidate_tag(stream_id)}",
                    )
                    self.assertEqual(
                        image["stream_ref"],
                        "ghcr.io/supergate-hub/kolla-container-images/"
                        f"{image_name}:{stream_id}",
                    )
                    self.assertEqual(
                        image["expected_ghcr_ref"],
                        expected_ref(image_name, stream_id),
                    )
                    self.assertEqual(
                        image["manifest_metadata_file"],
                        f"artifacts/manifests/{image_name}-"
                        f"{expected_candidate_tag(stream_id)}.json",
                    )
                    self.assertEqual(
                        [
                            (
                                architecture["arch_tag"],
                                architecture["arch_ref"],
                                architecture["platform"],
                            )
                            for architecture in image["architectures"]
                        ],
                        [
                            (
                                expected_candidate_tag(stream_id, arch),
                                "ghcr.io/supergate-hub/kolla-container-images/"
                                f"{image_name}:"
                                f"{expected_candidate_tag(stream_id, arch)}",
                                ARCHITECTURES[arch]["platform"],
                            )
                            for arch in ("amd64", "arm64")
                        ],
                    )
                for architecture in plan["build"]["architectures"]:
                    arch = architecture["arch"]
                    arch_expectation = ARCHITECTURES[arch]

                    self.assertEqual(
                        architecture["kolla_base_arch"],
                        arch_expectation["kolla_base_arch"],
                    )
                    self.assertEqual(
                        architecture["platform"], arch_expectation["platform"]
                    )
                    self.assertEqual(
                        architecture["runner_labels"],
                        arch_expectation["runner_labels"],
                    )
                    self.assertNotIn("commands", architecture)
                    self.assertTrue(
                        {
                            parent["image"]
                            for parent in architecture["parents"]
                        }.isdisjoint(leaf_names)
                    )
                    self.assertEqual(
                        [image["image"] for image in architecture["images"]],
                        leaf_names,
                    )
                    for image in architecture["images"]:
                        self.assertEqual(
                            image["smoke"],
                            {
                                "ref_source": "recorded_child_digest",
                                "platform": arch_expectation["platform"],
                                "inspect_platform": True,
                                "entrypoint": "/bin/true",
                            },
                        )

                for unit in all_units:
                    arch_expectation = ARCHITECTURES[unit["arch"]]
                    command = unit["command"]
                    self.assertEqual(
                        set(unit),
                        {
                            "id",
                            "kind",
                            "tier",
                            "arch",
                            "runner",
                            "runner_machine",
                            "kolla_base_arch",
                            "platform",
                            "target",
                            "ancestor_chain",
                            "ancestors",
                            "arch_ref",
                            "summary_file",
                            "logs_dir",
                            "command",
                        },
                    )
                    self.assertEqual(unit["runner"], arch_expectation["runner"])
                    self.assertEqual(
                        unit["runner_machine"], arch_expectation["runner_machine"]
                    )
                    self.assertEqual(
                        unit["kolla_base_arch"], arch_expectation["kolla_base_arch"]
                    )
                    self.assertEqual(unit["platform"], arch_expectation["platform"])
                    self.assertEqual(command[0], "kolla-build")
                    self.assertEqual(option_value(command, "--engine"), "docker")
                    self.assertEqual(option_value(command, "--base"), distro)
                    self.assertEqual(option_value(command, "--base-tag"), base_tag)
                    self.assertEqual(
                        option_value(command, "--base-arch"),
                        arch_expectation["kolla_base_arch"],
                    )
                    self.assertEqual(
                        option_value(command, "--platform"),
                        arch_expectation["platform"],
                    )
                    self.assertEqual(option_value(command, "--openstack-release"), release)
                    self.assertEqual(option_value(command, "--registry"), "ghcr.io")
                    self.assertEqual(
                        option_value(command, "--namespace"),
                        "supergate-hub/kolla-container-images",
                    )
                    self.assertEqual(
                        option_value(command, "--tag"),
                        expected_candidate_tag(stream_id, unit["arch"]),
                    )
                    self.assertEqual(option_value(command, "--threads"), "1")
                    self.assertEqual(option_value(command, "--push-threads"), "1")
                    self.assertEqual(
                        option_value(command, "--summary-json-file"),
                        unit["summary_file"],
                    )
                    self.assertEqual(
                        option_value(command, "--logs-dir"),
                        unit["logs_dir"],
                    )
                    self.assertIn("--push", command)
                    self.assertEqual(command.count("--push"), 1)
                    self.assertIn("--skip-existing", command)
                    self.assertNotIn("--skip-parents", command)
                    self.assertEqual(
                        command[-1], f"^{unit['target']}$"
                    )
                    self.assertEqual(
                        unit["id"], f"{unit['arch']}-{unit['kind']}-{unit['target']}"
                    )
                    self.assertEqual(
                        [ancestor["image"] for ancestor in unit["ancestors"]],
                        unit["ancestor_chain"],
                    )
                    self.assertTrue(
                        all(
                            ancestor["arch_ref"].endswith(
                                f":{expected_candidate_tag(stream_id, unit['arch'])}"
                            )
                            for ancestor in unit["ancestors"]
                        )
                    )

    def test_all_stream_build_matrices_fit_github_limits(self) -> None:
        for stream_id in STREAM_IDS:
            with self.subTest(stream=stream_id):
                plan = run_plan(stream=stream_id, profile="deployment")
                matrices = build_matrices(plan)
                self.assertEqual(
                    [name for name, _ in matrices],
                    [
                        "parent_tier_0_matrix",
                        "parent_tier_1_matrix",
                        "parent_tier_2_matrix",
                        "leaf_stage_0_matrix",
                        "leaf_stage_1_matrix",
                    ],
                )
                self.assertTrue(
                    all(len(matrix["include"]) <= 256 for _, matrix in matrices)
                )
                github_output = "".join(
                    f"{name}={json.dumps(matrix, separators=(',', ':'))}\n"
                    for name, matrix in matrices
                )
                self.assertLessEqual(
                    len(github_output.encode("utf-16-le")),
                    1024 * 1024,
                )

                if stream_id == "2025.1-rocky-9":
                    self.assertEqual(
                        [len(matrix["include"]) for _, matrix in matrices],
                        [2, 10, 20, 124, 2],
                    )

    def test_leaf_stage_planning_fails_closed_on_cycles_and_depth(self) -> None:
        stage_map = planner_symbols()["selected_leaf_stage_map"]

        self.assertEqual(
            stage_map(
                {
                    "independent": ["base"],
                    "dependency": ["base"],
                    "dependent": ["base", "dependency"],
                }
            ),
            {"independent": 0, "dependency": 0, "dependent": 1},
        )
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            stage_map({"first": ["second"], "second": ["first"]})
        with self.assertRaisesRegex(ValueError, "depth exceeds supported stages"):
            stage_map(
                {
                    "first": ["base"],
                    "second": ["first"],
                    "third": ["second"],
                }
            )

    def test_native_architectures_record_parent_and_leaf_evidence(self) -> None:
        plan = run_plan(image="keystone")

        for architecture in plan["build"]["architectures"]:
            arch = architecture["arch"]
            platform = ARCHITECTURES[arch]["platform"]
            self.assertEqual(
                architecture["parents"],
                [
                    {
                        "image": parent,
                        "arch_ref": (
                            expected_ref(parent, "2025.1-rocky-9", arch)
                        ),
                    }
                    for parent in ("base", "openstack-base", "keystone-base")
                ],
            )
            self.assertEqual(
                architecture["images"],
                [
                    {
                        "image": "keystone",
                        "arch_ref": (
                            expected_ref("keystone", "2025.1-rocky-9", arch)
                        ),
                        "smoke": {
                            "ref_source": "recorded_child_digest",
                            "platform": platform,
                            "inspect_platform": True,
                            "entrypoint": "/bin/true",
                        },
                    }
                ],
            )
            self.assertNotIn("commands", architecture)

        self.assertEqual(
            [
                (tier["tier"], [unit["id"] for unit in tier["matrix"]["include"]])
                for tier in plan["build"]["parent_tiers"]
            ],
            [
                (0, ["amd64-parent-base", "arm64-parent-base"]),
                (
                    1,
                    ["amd64-parent-openstack-base", "arm64-parent-openstack-base"],
                ),
                (
                    2,
                    ["amd64-parent-keystone-base", "arm64-parent-keystone-base"],
                ),
            ],
        )
        self.assertEqual(
            [unit["id"] for unit in leaf_units(plan)],
            ["amd64-leaf-keystone", "arm64-leaf-keystone"],
        )
        self.assertEqual(
            [
                len(stage["matrix"]["include"])
                for stage in plan["build"]["leaf_stages"]
            ],
            [2, 0],
        )
        for unit in leaf_units(plan):
            self.assertEqual(
                unit["ancestor_chain"],
                ["base", "openstack-base", "keystone-base"],
            )
            self.assertEqual(unit["tier"], 3)
            self.assertEqual(unit["command"][-1], "^keystone$")

    def test_organization_arch_and_neutral_refs_are_exact(self) -> None:
        plan = run_plan(image="keystone")
        image = plan["images"][0]

        self.assertEqual(plan["registry"], "ghcr.io")
        self.assertEqual(plan["owner"], "supergate-hub")
        self.assertEqual(plan["repository"], "kolla-container-images")
        self.assertEqual(
            image["deploy_tag"],
            "2025.1-rocky-9-candidate-123456789-1",
        )
        self.assertEqual(
            image["deploy_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone:"
            "2025.1-rocky-9-candidate-123456789-1",
        )
        self.assertEqual(
            image["stream_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9",
        )
        self.assertEqual(
            [architecture["arch_ref"] for architecture in image["architectures"]],
            [
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-amd64",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-arm64",
            ],
        )
        self.assertEqual(
            [architecture["platform"] for architecture in image["architectures"]],
            ["linux/amd64", "linux/arm64"],
        )
        self.assertEqual(
            image["commands"]["manifest_create"],
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1",
                "--metadata-file",
                "artifacts/manifests/keystone-2025.1-rocky-9-"
                "candidate-123456789-1.json",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-amd64",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1-arm64",
            ],
        )
        self.assertEqual(
            image["commands"]["manifest_inspect"],
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-9-candidate-123456789-1",
            ],
        )

    def test_ubuntu_base_tag_and_noble_publish_tags_stay_distinct(self) -> None:
        plan = run_plan(stream="2025.1-ubuntu-noble", image="keystone")
        command = plan["build"]["all_units"][0]["command"]
        image = plan["images"][0]

        self.assertEqual(plan["distro_version"], "24.04")
        self.assertEqual(option_value(command, "--base-tag"), "24.04")
        self.assertEqual(
            option_value(command, "--tag"),
            "2025.1-ubuntu-noble-candidate-123456789-1-amd64",
        )
        self.assertEqual(
            image["deploy_tag"],
            "2025.1-ubuntu-noble-candidate-123456789-1",
        )
        self.assertNotIn("24.04", image["deploy_ref"])

    def test_image_filter_limits_scope_build_and_manifest_to_one_leaf(self) -> None:
        plan = run_plan(image="glance-api")

        self.assertEqual(plan["image_filter"], "glance-api")
        self.assertEqual(
            plan["scope"],
            {"profile": "core", "image": "glance-api", "image_count": 1},
        )
        self.assertEqual([image["image"] for image in plan["images"]], ["glance-api"])
        for architecture in plan["build"]["architectures"]:
            self.assertEqual(
                [image["image"] for image in architecture["images"]], ["glance-api"]
            )
        self.assertEqual(
            [unit["target"] for unit in leaf_units(plan)],
            ["glance-api", "glance-api"],
        )
        self.assertTrue(
            all(unit["command"][-1] == "^glance-api$" for unit in leaf_units(plan))
        )

    def test_relay_filter_adds_only_its_build_leaf_dependency(self) -> None:
        plan = run_plan(profile="deployment", image="ovn-sb-db-relay")

        self.assertEqual(
            plan["scope"],
            {"profile": "deployment", "image": "ovn-sb-db-relay", "image_count": 1},
        )
        self.assertEqual([image["image"] for image in plan["images"]], ["ovn-sb-db-relay"])
        for architecture in plan["build"]["architectures"]:
            self.assertEqual(
                [image["image"] for image in architecture["images"]],
                ["ovn-sb-db-relay"],
            )
            self.assertEqual(
                [parent["image"] for parent in architecture["parents"]],
                ["base", "openvswitch-base", "ovn-base"],
            )

        self.assertEqual(
            [
                [unit["id"] for unit in tier["matrix"]["include"]]
                for tier in plan["build"]["parent_tiers"]
            ],
            [
                ["amd64-parent-base", "arm64-parent-base"],
                [
                    "amd64-parent-openvswitch-base",
                    "arm64-parent-openvswitch-base",
                ],
                ["amd64-parent-ovn-base", "arm64-parent-ovn-base"],
            ],
        )
        self.assertEqual(
            [
                [unit["id"] for unit in stage["matrix"]["include"]]
                for stage in plan["build"]["leaf_stages"]
            ],
            [
                [
                    "amd64-leaf-ovn-sb-db-server",
                    "arm64-leaf-ovn-sb-db-server",
                ],
                [
                    "amd64-leaf-ovn-sb-db-relay",
                    "arm64-leaf-ovn-sb-db-relay",
                ],
            ],
        )
        self.assertEqual(
            [unit["id"] for unit in plan["build"]["all_units"]],
            [
                "amd64-parent-base",
                "arm64-parent-base",
                "amd64-parent-openvswitch-base",
                "arm64-parent-openvswitch-base",
                "amd64-parent-ovn-base",
                "arm64-parent-ovn-base",
                "amd64-leaf-ovn-sb-db-server",
                "arm64-leaf-ovn-sb-db-server",
                "amd64-leaf-ovn-sb-db-relay",
                "arm64-leaf-ovn-sb-db-relay",
            ],
        )
        self.assertEqual(len(plan["build"]["all_units"]), 10)
        self.assertNotIn(
            "ovn-sb-db-server",
            {unit["target"] for unit in parent_units(plan)},
        )
        manifest_command = plan["images"][0]["commands"]["manifest_create"]
        self.assertTrue(
            all(
                "/ovn-sb-db-relay:" in item
                for item in manifest_command
                if item.startswith("ghcr.io/")
            )
        )

    def test_core_nova_libvirt_uses_only_the_base_parent_chain(self) -> None:
        plan = run_plan(image="nova-libvirt")

        self.assertEqual(
            [len(tier["matrix"]["include"]) for tier in plan["build"]["parent_tiers"]],
            [2, 0, 0],
        )
        self.assertEqual(
            [unit["ancestor_chain"] for unit in leaf_units(plan)],
            [["base"], ["base"]],
        )
        self.assertEqual(
            [parent["image"] for parent in plan["build"]["architectures"][0]["parents"]],
            ["base"],
        )

    def test_approval_metadata_is_bound_to_the_frozen_scope(self) -> None:
        cases = (
            (
                run_plan(image="keystone"),
                {
                    "allowed": True,
                    "required_variable": "ALLOW_GHCR_PUBLISH",
                    "phrase": (
                        "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
                        "2025.1-rocky-9 core/keystone (1 image, amd64/arm64)"
                    ),
                },
            ),
            (
                run_plan(profile="core"),
                {
                    "allowed": True,
                    "required_variable": "ALLOW_GHCR_FULL_CORE_PUBLISH",
                    "phrase": (
                        "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
                        "2025.1-rocky-9 core/all (21 images, amd64/arm64)"
                    ),
                },
            ),
            (
                run_plan(profile="deployment"),
                {
                    "allowed": True,
                    "required_variable": "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
                    "phrase": (
                        "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
                        "2025.1-rocky-9 deployment/all (63 images, amd64/arm64)"
                    ),
                },
            ),
            (
                run_plan(image="glance-api"),
                {"allowed": False, "required_variable": None, "phrase": None},
            ),
            (
                run_plan(profile="deployment", image="keystone"),
                {"allowed": False, "required_variable": None, "phrase": None},
            ),
        )

        for plan, expected in cases:
            with self.subTest(
                profile=plan["profile"], image=plan["scope"]["image"]
            ):
                self.assertEqual(plan.get("approval"), expected)

    def test_unknown_image_filter_fails(self) -> None:
        result = subprocess.run(
            plan_command(image="missing-image"),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("image does not exist in profile core: missing-image", result.stderr)

    def test_invalid_stream_lists_all_accepted_ids(self) -> None:
        result = subprocess.run(
            plan_command(stream="missing-stream"),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "unsupported stream: missing-stream; accepted streams: "
            + ", ".join(STREAM_IDS),
            result.stderr,
        )

    def test_only_deployment_all_has_candidate_lock_path(self) -> None:
        core = run_plan(profile="core")
        core_partial = run_plan(profile="core", image="keystone")
        deployment_partial = run_plan(profile="deployment", image="keystone")
        deployment = run_plan(profile="deployment")

        for plan in (core, core_partial, deployment_partial):
            self.assertIsNone(plan["kolla_ansible_lock_file"])
            self.assertNotIn(ENVIRONMENT_LOCK_FIELD, plan)
        self.assertEqual(
            deployment["publish_summary_file"],
            "artifacts/publish-summary-2025.1-rocky-9.json",
        )
        self.assertEqual(
            deployment["kolla_ansible_lock_file"],
            "artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml",
        )
        self.assertNotIn(ENVIRONMENT_LOCK_FIELD, deployment)

    def test_parent_refs_are_evidence_only(self) -> None:
        plan = run_plan(image="keystone")
        deployable_images = {image["image"] for image in plan["images"]}

        self.assertTrue(
            {"base", "openstack-base", "keystone-base"}.isdisjoint(deployable_images)
        )
        for image in plan["images"]:
            manifest_command = image["commands"]["manifest_create"]
            for parent in ("base", "openstack-base", "keystone-base"):
                self.assertFalse(any(f"/{parent}:" in item for item in manifest_command))

    def test_legacy_release_and_distro_arguments_are_rejected(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--stream",
                "2025.1-rocky-9",
                "--profile",
                "core",
                "--release",
                "2025.1",
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --release 2025.1", result.stderr)

    def test_refuses_without_dry_run(self) -> None:
        result = subprocess.run(
            plan_command(dry_run=False),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--dry-run", result.stderr)


if __name__ == "__main__":
    unittest.main()
