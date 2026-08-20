from __future__ import annotations

import base64
import hashlib
import json
import runpy
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.profile_resolver import find_stream


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
PARENT_FIXTURE = ROOT / "tests" / "fixtures" / "kolla-parent-dependencies.json"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"
MATRIX = json.loads(
    (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
)
STREAM_IDS = [stream["id"] for stream in MATRIX["streams"]]
DEFAULT_STREAM_ID = STREAM_IDS[0]
CATALOG_STREAM_EXPECTATIONS = {
    "2025.1-rocky-9.8-20.4.0": ("2025.1", "rocky", "9.8", "20.4.0", 63, 16),
    "2025.1-rocky-10.2-20.4.0": ("2025.1", "rocky", "10.2", "20.4.0", 63, 16),
    "2025.1-ubuntu-24.04-20.4.0": ("2025.1", "ubuntu", "24.04", "20.4.0", 64, 16),
    "2025.1-rocky-10.2-20.5.0": ("2025.1", "rocky", "10.2", "20.5.0", 63, 16),
    "2025.1-ubuntu-24.04-20.5.0": ("2025.1", "ubuntu", "24.04", "20.5.0", 64, 16),
    "2025.2-rocky-10.2-21.1.0": ("2025.2", "rocky", "10.2", "21.1.0", 63, 15),
    "2025.2-ubuntu-24.04-21.1.0": ("2025.2", "ubuntu", "24.04", "21.1.0", 64, 15),
    "2026.1-rocky-10.2-22.0.0": ("2026.1", "rocky", "10.2", "22.0.0", 65, 15),
    "2026.1-ubuntu-24.04-22.0.0": ("2026.1", "ubuntu", "24.04", "22.0.0", 66, 15),
}
STREAM_EXPECTATIONS = {
    stream_id: CATALOG_STREAM_EXPECTATIONS[stream_id]
    for stream_id in STREAM_IDS
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


def expected_deploy_tag(stream: str, arch: str | None = None) -> str:
    resolved = find_stream(MATRIX, stream)
    tag = MATRIX["tag_policy"]["deploy_tag_template"].format(
        release=resolved["release"],
        distro=resolved["distro"],
        os_version=resolved.get("os_version", resolved.get("tag_token")),
        tag_token=resolved.get("tag_token", resolved.get("os_version")),
        kolla_ansible_version=resolved["kolla_ansible_version"],
    )
    return f"{tag}-{arch}" if arch else tag


def expected_ref(
    image: str,
    stream: str,
    arch: str | None = None,
    candidate_id: str | None = None,
) -> str:
    tag = expected_deploy_tag(stream)
    if candidate_id is not None:
        tag = f"{tag}-rev-{candidate_id}"
    if arch is not None:
        tag = f"{tag}-{arch}"
    return (
        "ghcr.io/supergate-hub/kolla-container-images/"
        f"{image}:{tag}"
    )


def expected_revision_tag(stream: str, arch: str | None = None) -> str:
    tag = f"{expected_deploy_tag(stream)}-rev-{TEST_CANDIDATE_ID}"
    return f"{tag}-{arch}" if arch else tag


def expected_revision_ref(image: str, stream: str, arch: str | None = None) -> str:
    return expected_ref(
        image,
        stream,
        arch,
        candidate_id=TEST_CANDIDATE_ID,
    )


def plan_command(
    *,
    stream: str = DEFAULT_STREAM_ID,
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
        "--base-manifest",
        str(BASE_INDEX_FIXTURE),
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
    stream: str = DEFAULT_STREAM_ID,
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
    def test_plan_freezes_one_base_index_and_both_native_descriptors(self) -> None:
        plan = run_plan(image="keystone")
        resolved = find_stream(MATRIX, DEFAULT_STREAM_ID)

        self.assertEqual(
            plan["base"],
            {
                "id": resolved["base_id"],
                "requested_ref": f"{resolved['base_image']}:{resolved['base_tag']}",
                "index_digest": (
                    "sha256:"
                    + hashlib.sha256(BASE_INDEX_FIXTURE.read_bytes()).hexdigest()
                ),
                "index_manifest_b64": base64.b64encode(
                    BASE_INDEX_FIXTURE.read_bytes()
                ).decode("ascii"),
                "platforms": {
                    "amd64": {
                        "platform": "linux/amd64",
                        "digest": "sha256:" + "1" * 64,
                    },
                    "arm64": {
                        "platform": "linux/arm64",
                        "digest": "sha256:" + "2" * 64,
                    },
                },
            },
        )

    def test_candidate_id_selects_an_immutable_revision_beneath_one_semantic_ref(
        self,
    ) -> None:
        local = run_plan(image="keystone", candidate_id=None)
        live = run_plan(image="keystone", candidate_id=TEST_CANDIDATE_ID)
        semantic_ref = expected_ref("keystone", DEFAULT_STREAM_ID)
        local_revision_ref = expected_ref(
            "keystone", DEFAULT_STREAM_ID, candidate_id="local-dry-run"
        )
        live_revision_ref = expected_ref(
            "keystone", DEFAULT_STREAM_ID, candidate_id=TEST_CANDIDATE_ID
        )

        self.assertEqual(local["candidate_id"], "local-dry-run")
        self.assertEqual(local["images"][0]["semantic_ref"], semantic_ref)
        self.assertEqual(local["images"][0]["revision_ref"], local_revision_ref)
        self.assertEqual(live["candidate_id"], TEST_CANDIDATE_ID)
        image = live["images"][0]
        self.assertEqual(image["semantic_ref"], semantic_ref)
        self.assertEqual(image["revision_ref"], live_revision_ref)
        self.assertNotEqual(local_revision_ref, live_revision_ref)
        self.assertEqual(
            [entry["revision_arch_ref"] for entry in image["architectures"]],
            [
                expected_ref(
                    "keystone",
                    DEFAULT_STREAM_ID,
                    "amd64",
                    candidate_id=TEST_CANDIDATE_ID,
                ),
                expected_ref(
                    "keystone",
                    DEFAULT_STREAM_ID,
                    "arm64",
                    candidate_id=TEST_CANDIDATE_ID,
                ),
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

    def test_all_streams_use_semantic_and_revision_tags(self) -> None:
        for stream_id in STREAM_IDS:
            with self.subTest(stream=stream_id):
                plan = run_plan(stream=stream_id, image="keystone")
                image = plan["images"][0]
                semantic_tag = expected_deploy_tag(stream_id)
                revision = expected_revision_tag(stream_id)
                self.assertEqual(plan["candidate_id"], TEST_CANDIDATE_ID)
                self.assertEqual(image["semantic_tag"], semantic_tag)
                self.assertEqual(image["semantic_ref"], expected_ref("keystone", stream_id))
                self.assertEqual(image["revision_tag"], revision)
                self.assertEqual(image["revision_ref"], expected_revision_ref("keystone", stream_id))
                for architecture in plan["build"]["architectures"]:
                    arch = architecture["arch"]
                    arch_tag = expected_revision_tag(stream_id, arch)
                    self.assertEqual(architecture["revision_arch_tag"], arch_tag)
                    self.assertTrue(
                        all(
                            entry["revision_arch_ref"].endswith(f":{arch_tag}")
                            for entry in architecture["parents"]
                        )
                    )
                    self.assertTrue(
                        all(
                            entry["revision_arch_ref"].endswith(f":{arch_tag}")
                            for entry in architecture["images"]
                        )
                    )
                for unit in plan["build"]["all_units"]:
                    arch_tag = expected_revision_tag(stream_id, unit["arch"])
                    self.assertEqual(option_value(unit["command"], "--tag"), arch_tag)
                    self.assertTrue(unit["arch_ref"].endswith(f":{arch_tag}"))

    def test_default_alias_is_included_for_the_selected_default_stream(self) -> None:
        plan = run_plan(stream="2025.1-rocky-10.2-20.5.0", image="keystone")
        image = plan["images"][0]

        self.assertEqual(image["alias_tags"], ["2025.1-rocky-10.2"])
        self.assertEqual(
            image["alias_refs"],
            [
                "ghcr.io/supergate-hub/kolla-container-images/keystone:"
                "2025.1-rocky-10.2"
            ],
        )

    def test_parent_sets_match_checked_in_kolla_dependency_fixture(self) -> None:
        fixture = json.loads(PARENT_FIXTURE.read_text(encoding="utf-8"))
        matrix_pins = {
            stream["id"]: find_stream(MATRIX, stream["id"])["kolla_version"]
            for stream in MATRIX["streams"]
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
        active_fixture_streams = [
            stream for stream in fixture["streams"] if stream["id"] in STREAM_IDS
        ]
        self.assertEqual(
            [stream["id"] for stream in active_fixture_streams], STREAM_IDS
        )

        scope_inputs = {
            "core/keystone": {"profile": "core", "image": "keystone"},
            "core/all": {"profile": "core", "image": None},
            "deployment/all": {"profile": "deployment", "image": None},
        }
        for expected in active_fixture_streams:
            stream_id = expected["id"]
            with self.subTest(stream=stream_id, check="kolla-pin"):
                self.assertEqual(
                    expected["kolla_version"], matrix_pins[stream_id]
                )
            for scope, inputs in scope_inputs.items():
                with self.subTest(stream=stream_id, scope=scope):
                    plan = run_plan(stream=stream_id, **inputs)
                    self.assertEqual(
                        plan["kolla"]["version"], expected["kolla_version"]
                    )
                    for architecture in plan["build"]["architectures"]:
                        self.assertEqual(
                            [parent["image"] for parent in architecture["parents"]],
                            expected["scopes"][scope],
                        )

    def test_core_profile_images_and_resolved_variables_are_included(self) -> None:
        stream_id = (
            "2025.2-rocky-10"
            if "2025.2-rocky-10" in STREAM_IDS
            else DEFAULT_STREAM_ID
        )
        plan = run_plan(stream=stream_id)
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
                "neutron-metadata-agent",
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
        expected_neutron_variables = ["neutron_server_image_full"]
        if plan["release"] in {"2025.2", "2026.1"}:
            expected_neutron_variables.extend(
                [
                    "neutron_rpc_server_image_full",
                    "neutron_periodic_worker_image_full",
                    "neutron_ovn_maintenance_worker_image_full",
                ]
            )
        self.assertEqual(
            variables_by_image["neutron-server"], expected_neutron_variables
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
                self.assertEqual(plan["kolla"]["version"], kolla_version)
                self.assertEqual(plan["kolla_ansible"]["version"], kolla_version)
                resolved_stream = find_stream(MATRIX, stream_id)
                self.assertEqual(
                    plan["release_metadata"], MATRIX["release_metadata"]
                )
                self.assertEqual(plan["release_series"], resolved_stream["release_series"])
                self.assertEqual(plan["release_branch"], resolved_stream["release_branch"])
                self.assertEqual(
                    plan["kolla"],
                    {
                        "repository": resolved_stream["kolla_repository"],
                        "version": resolved_stream["kolla_version"],
                        "commit": resolved_stream["kolla_commit"],
                    },
                )
                self.assertEqual(
                    plan["kolla_ansible"],
                    {
                        "repository": resolved_stream["kolla_ansible_repository"],
                        "version": resolved_stream["kolla_ansible_version"],
                        "commit": resolved_stream["kolla_ansible_commit"],
                    },
                )
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
                        image["semantic_tag"], expected_deploy_tag(stream_id)
                    )
                    self.assertEqual(
                        image["semantic_ref"], expected_ref(image_name, stream_id)
                    )
                    self.assertEqual(
                        image["revision_ref"],
                        expected_revision_ref(image_name, stream_id),
                    )
                    self.assertEqual(
                        image["manifest_metadata_file"],
                        f"artifacts/manifests/{image_name}-"
                        f"{expected_revision_tag(stream_id)}.json",
                    )
                    self.assertEqual(
                        [
                            (
                                architecture["revision_arch_tag"],
                                architecture["revision_arch_ref"],
                                architecture["platform"],
                            )
                            for architecture in image["architectures"]
                        ],
                        [
                            (
                                expected_revision_tag(stream_id, arch),
                                expected_revision_ref(image_name, stream_id, arch),
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
                        expected_revision_tag(stream_id, unit["arch"]),
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
                    self.assertEqual(command.count("--nopull"), 1)
                    self.assertNotIn("--no-pull", command)
                    self.assertEqual(command.count("--skip-existing"), 1)
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
                                f":{expected_revision_tag(stream_id, unit['arch'])}"
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
                        "revision_arch_ref": (
                            expected_revision_ref(parent, DEFAULT_STREAM_ID, arch)
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
                        "revision_arch_ref": (
                            expected_revision_ref("keystone", DEFAULT_STREAM_ID, arch)
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
        semantic_tag = expected_deploy_tag(DEFAULT_STREAM_ID)
        semantic_ref = expected_ref("keystone", DEFAULT_STREAM_ID)
        revision = expected_revision_tag(DEFAULT_STREAM_ID)
        revision_ref = expected_revision_ref("keystone", DEFAULT_STREAM_ID)

        self.assertEqual(plan["registry"], "ghcr.io")
        self.assertEqual(plan["owner"], "supergate-hub")
        self.assertEqual(plan["repository"], "kolla-container-images")
        self.assertEqual(image["semantic_tag"], semantic_tag)
        self.assertEqual(image["semantic_ref"], semantic_ref)
        self.assertEqual(image["revision_tag"], revision)
        self.assertEqual(image["revision_ref"], revision_ref)
        self.assertEqual(
            [architecture["revision_arch_ref"] for architecture in image["architectures"]],
            [
                expected_revision_ref("keystone", DEFAULT_STREAM_ID, "amd64"),
                expected_revision_ref("keystone", DEFAULT_STREAM_ID, "arm64"),
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
                revision_ref,
                "--metadata-file",
                f"artifacts/manifests/keystone-{revision}.json",
                expected_revision_ref("keystone", DEFAULT_STREAM_ID, "amd64"),
                expected_revision_ref("keystone", DEFAULT_STREAM_ID, "arm64"),
            ],
        )
        self.assertEqual(
            image["commands"]["manifest_inspect"],
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                revision_ref,
            ],
        )

    def test_ubuntu_base_and_semantic_tag_use_exact_os_version(self) -> None:
        ubuntu_stream_id = next(
            stream["id"]
            for stream in MATRIX["streams"]
            if find_stream(MATRIX, stream["id"])["distro"] == "ubuntu"
        )
        resolved = find_stream(MATRIX, ubuntu_stream_id)
        plan = run_plan(stream=ubuntu_stream_id, image="keystone")
        command = plan["build"]["all_units"][0]["command"]
        image = plan["images"][0]

        self.assertEqual(plan["distro_version"], resolved["base_tag"])
        self.assertEqual(option_value(command, "--base-tag"), resolved["base_tag"])
        self.assertEqual(
            option_value(command, "--tag"),
            expected_revision_tag(ubuntu_stream_id, "amd64"),
        )
        self.assertEqual(image["semantic_tag"], expected_deploy_tag(ubuntu_stream_id))

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

    def test_typed_approval_metadata_is_absent_from_frozen_plans(self) -> None:
        for plan in (
            run_plan(image="keystone"),
            run_plan(profile="core"),
            run_plan(profile="deployment"),
        ):
            self.assertNotIn("approval", plan)

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
            f"artifacts/publish-summary-{DEFAULT_STREAM_ID}.json",
        )
        self.assertEqual(
            deployment["kolla_ansible_lock_file"],
            f"artifacts/kolla-ansible-image-lock-{DEFAULT_STREAM_ID}.yml",
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
        release = find_stream(MATRIX, DEFAULT_STREAM_ID)["release"]
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--stream",
                DEFAULT_STREAM_ID,
                "--profile",
                "core",
                "--release",
                release,
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"unrecognized arguments: --release {release}", result.stderr)

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
