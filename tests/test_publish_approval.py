from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
VALIDATE_APPROVAL = ROOT / "scripts" / "validate-publish-approval.py"
REGISTRY_PATH = "ghcr.io/supergate-hub/kolla-container-images"
STREAM_IDS = [
    "2025.1-rocky-9",
    "2025.1-rocky-10",
    "2025.1-ubuntu-noble",
    "2025.2-rocky-10",
    "2025.2-ubuntu-noble",
    "2026.1-rocky-10",
    "2026.1-ubuntu-noble",
]
DEPLOYMENT_COUNTS = {
    "2025.1-rocky-9": 63,
    "2025.1-rocky-10": 63,
    "2025.1-ubuntu-noble": 64,
    "2025.2-rocky-10": 63,
    "2025.2-ubuntu-noble": 64,
    "2026.1-rocky-10": 65,
    "2026.1-ubuntu-noble": 66,
}
APPROVAL_VARIABLES = (
    "ALLOW_GHCR_PUBLISH",
    "ALLOW_GHCR_FULL_CORE_PUBLISH",
    "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
)
TEST_CANDIDATE_ID = "123456789-1"


def expected_phrase(stream: str, profile: str, image: str, count: int) -> str:
    noun = "image" if count == 1 else "images"
    return (
        f"PUBLISH {REGISTRY_PATH} {stream} {profile}/{image} "
        f"({count} {noun}, amd64/arm64)"
    )


def generate_plan(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str = TEST_CANDIDATE_ID,
) -> dict:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream",
        stream,
        "--profile",
        profile,
        "--candidate-id",
        candidate_id,
        "--dry-run",
    ]
    if image is not None:
        command.extend(["--image", image])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def write_plan(directory: Path, name: str, plan: dict) -> Path:
    path = directory / f"{name}.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def run_validator(
    plan_path: Path,
    *,
    approval: str | None,
    variables: dict[str, str] | None = None,
    expected_candidate_id: str = TEST_CANDIDATE_ID,
    extra_args: list[str] | None = None,
    publish_plan_option: str = "--publish-plan",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (*APPROVAL_VARIABLES, "APPROVAL"):
        env.pop(name, None)
    if approval is not None:
        env["APPROVAL"] = approval
    if variables:
        env.update(variables)
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_APPROVAL),
            publish_plan_option,
            str(plan_path),
            "--expected-candidate-id",
            expected_candidate_id,
            *(extra_args or []),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


class PublishApprovalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.plan_directory = Path(cls.temp_directory.name)
        cls.plans: dict[tuple[str, str, str], dict] = {}
        for stream in STREAM_IDS:
            cls.plans[(stream, "core", "keystone")] = generate_plan(
                stream=stream,
                profile="core",
                image="keystone",
            )
            cls.plans[(stream, "core", "all")] = generate_plan(
                stream=stream,
                profile="core",
            )
            cls.plans[(stream, "deployment", "all")] = generate_plan(
                stream=stream,
                profile="deployment",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def plan(self, stream: str, profile: str, image: str) -> dict:
        return copy.deepcopy(self.plans[(stream, profile, image)])

    def test_trusted_candidate_id_must_match_frozen_plan(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "candidate-mismatch", plan)
        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
            expected_candidate_id="123456789-2",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate ID", result.stderr)

    def test_local_candidate_id_cannot_authorize_publication(self) -> None:
        plan = generate_plan(candidate_id="local-dry-run")
        path = write_plan(self.plan_directory, "local-candidate", plan)
        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "all", 21),
            variables={"ALLOW_GHCR_FULL_CORE_PUBLISH": "true"},
            expected_candidate_id="local-dry-run",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("workflow candidate ID", result.stderr)

    def test_all_seven_streams_and_three_allowed_scopes_validate(self) -> None:
        case_count = 0
        scopes = (
            ("core", "keystone", "ALLOW_GHCR_PUBLISH"),
            ("core", "all", "ALLOW_GHCR_FULL_CORE_PUBLISH"),
            ("deployment", "all", "ALLOW_GHCR_DEPLOYMENT_PUBLISH"),
        )
        for stream in STREAM_IDS:
            for profile, image, variable in scopes:
                case_count += 1
                count = (
                    1
                    if image == "keystone"
                    else 21
                    if profile == "core"
                    else DEPLOYMENT_COUNTS[stream]
                )
                phrase = expected_phrase(stream, profile, image, count)
                plan = self.plan(stream, profile, image)
                plan_path = write_plan(
                    self.plan_directory,
                    f"positive-{stream}-{profile}-{image}",
                    plan,
                )

                with self.subTest(stream=stream, profile=profile, image=image):
                    self.assertEqual(
                        plan.get("approval"),
                        {
                            "allowed": True,
                            "required_variable": variable,
                            "phrase": phrase,
                        },
                    )
                    self.assertIn(
                        "1 image, amd64/arm64"
                        if count == 1
                        else f"{count} images, amd64/arm64",
                        phrase,
                    )
                    result = run_validator(
                        plan_path,
                        approval=phrase,
                        variables={variable: "true"},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Publish approval validated.", result.stdout)

        self.assertEqual(case_count, 21)

    def test_required_variable_must_be_present(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "missing-variable", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_PUBLISH=true", result.stderr)

    def test_required_variable_must_be_exactly_true(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "false-variable", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "false"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_PUBLISH=true", result.stderr)

    def test_different_scope_variable_does_not_authorize_plan(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "wrong-variable", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_FULL_CORE_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_PUBLISH=true", result.stderr)

    def test_wrong_phrase_is_rejected(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "wrong-phrase", plan)

        result = run_validator(
            path,
            approval="PUBLISH the wrong frozen plan",
            variables={"ALLOW_GHCR_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("exact approval phrase", result.stderr)

    def test_stale_stored_count_is_rejected(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        plan["scope"]["image_count"] = 2
        path = write_plan(self.plan_directory, "stale-count", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("publish plan", result.stderr.lower())

    def test_tampered_namespace_is_rejected(self) -> None:
        for field, value in (
            ("registry", "docker.io"),
            ("owner", "another-owner"),
            ("repository", "another-repository"),
        ):
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            plan[field] = value
            path = write_plan(self.plan_directory, f"tampered-{field}", plan)

            with self.subTest(field=field):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("publish plan", result.stderr.lower())

    def test_tampered_stream_is_rejected(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        plan["stream"] = "2025.1-rocky-10"
        plan["approval"] = {
            "allowed": True,
            "required_variable": "ALLOW_GHCR_PUBLISH",
            "phrase": expected_phrase(
                "2025.1-rocky-10", "core", "keystone", 1
            ),
        }
        path = write_plan(self.plan_directory, "tampered-stream", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-10", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("publish plan", result.stderr.lower())

    def test_tampered_scope_image_is_rejected(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        plan["scope"]["image"] = "glance-api"
        path = write_plan(self.plan_directory, "tampered-image", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("publish plan", result.stderr.lower())

    def test_tampered_selected_images_are_rejected(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "all")
        plan["images"].pop()
        path = write_plan(self.plan_directory, "tampered-selection", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "all", 21),
            variables={"ALLOW_GHCR_FULL_CORE_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("publish plan", result.stderr.lower())

    def test_plan_must_contain_exact_amd64_arm64_platforms(self) -> None:
        mutations = {
            "missing-arm64": lambda plan: plan["build"]["architectures"].pop(),
            "wrong-build-platform": lambda plan: plan["build"]["architectures"][0].__setitem__(
                "platform", "linux/ppc64le"
            ),
            "wrong-image-platform": lambda plan: plan["images"][0][
                "architectures"
            ][0].__setitem__("platform", "linux/ppc64le"),
        }
        for name, mutate in mutations.items():
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            mutate(plan)
            path = write_plan(self.plan_directory, name, plan)

            with self.subTest(mutation=name):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("publish plan", result.stderr.lower())

    def test_tampered_plan_approval_metadata_is_rejected(self) -> None:
        def remove_approval(plan: dict) -> None:
            plan.pop("approval", None)

        mutations = {
            "missing": remove_approval,
            "false": lambda plan: plan["approval"].__setitem__("allowed", False),
            "wrong-variable": lambda plan: plan["approval"].__setitem__(
                "required_variable", "ALLOW_GHCR_DEPLOYMENT_PUBLISH"
            ),
            "wrong-phrase": lambda plan: plan["approval"].__setitem__(
                "phrase", "wrong stored phrase"
            ),
        }
        for name, mutate in mutations.items():
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            self.assertIn("approval", plan)
            mutate(plan)
            path = write_plan(self.plan_directory, f"approval-{name}", plan)

            with self.subTest(mutation=name):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("publish plan", result.stderr.lower())

    def test_partial_core_and_deployment_scopes_are_explicitly_disallowed(self) -> None:
        partial_plans = (
            generate_plan(profile="core", image="glance-api"),
            generate_plan(profile="deployment", image="keystone"),
        )
        for index, plan in enumerate(partial_plans):
            path = write_plan(self.plan_directory, f"partial-{index}", plan)

            with self.subTest(profile=plan["profile"], image=plan["image_filter"]):
                self.assertEqual(
                    plan.get("approval"),
                    {
                        "allowed": False,
                        "required_variable": None,
                        "phrase": None,
                    },
                )
                result = run_validator(
                    path,
                    approval="PUBLISH unsupported partial scope",
                    variables={name: "true" for name in APPROVAL_VARIABLES},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("not approved for real publish", result.stderr)

    def test_validator_accepts_no_independent_scope_arguments(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "legacy-arguments", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
            extra_args=["--profile", "core"],
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --profile core", result.stderr)

    def test_review_rejects_tampered_kolla_base_arguments(self) -> None:
        for option, replacement in (
            ("--base", "ubuntu"),
            ("--base-tag", "24.04"),
        ):
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            command = plan["build"]["all_units"][0]["command"]
            command[command.index(option) + 1] = replacement
            path = write_plan(
                self.plan_directory,
                f"review-tampered-{option.removeprefix('--')}",
                plan,
            )

            with self.subTest(option=option):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("publish plan", result.stderr.lower())

    def test_review_rejects_image_regex_inserted_before_push(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        command = plan["build"]["all_units"][0]["command"]
        command.insert(command.index("--push"), "^glance-api$")
        path = write_plan(self.plan_directory, "review-extra-image-regex", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("publish plan", result.stderr.lower())

    def test_review_rejects_replaced_manifest_commands(self) -> None:
        for command_name in ("manifest_create", "manifest_inspect"):
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            plan["images"][0]["commands"][command_name] = ["replaced-command"]
            path = write_plan(
                self.plan_directory,
                f"review-replaced-{command_name}",
                plan,
            )

            with self.subTest(command=command_name):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("publish plan", result.stderr.lower())

    def test_review_rejects_bool_integer_type_confusion(self) -> None:
        mutations = {
            "dry-run-integer": lambda plan: plan.__setitem__("dry_run", 1),
            "count-boolean": lambda plan: plan["scope"].__setitem__(
                "image_count", True
            ),
            "allowed-integer": lambda plan: plan["approval"].__setitem__(
                "allowed", 1
            ),
        }
        for name, mutate in mutations.items():
            plan = self.plan("2025.1-rocky-9", "core", "keystone")
            mutate(plan)
            path = write_plan(self.plan_directory, f"review-type-{name}", plan)

            with self.subTest(mutation=name):
                result = run_validator(
                    path,
                    approval=expected_phrase(
                        "2025.1-rocky-9", "core", "keystone", 1
                    ),
                    variables={"ALLOW_GHCR_PUBLISH": "true"},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("publish plan", result.stderr.lower())

    def test_review_rejects_stream_disabled_in_repository_matrix(self) -> None:
        module_name = "validate_publish_approval_review_test"
        spec = importlib.util.spec_from_file_location(module_name, VALIDATE_APPROVAL)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            validator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(validator)
        finally:
            sys.path.remove(scripts_path)

        matrix = validator.load_matrix()
        matrix["streams"][0]["publish_enabled"] = False
        original_load_matrix = validator.load_matrix
        validator.load_matrix = lambda: matrix
        try:
            with self.assertRaisesRegex(ValueError, "not enabled for publication"):
                validator.recompute_requirement(
                    self.plan("2025.1-rocky-9", "core", "keystone"),
                    TEST_CANDIDATE_ID,
                )
        finally:
            validator.load_matrix = original_load_matrix

    def test_review_rejects_abbreviated_publish_plan_option(self) -> None:
        plan = self.plan("2025.1-rocky-9", "core", "keystone")
        path = write_plan(self.plan_directory, "review-abbreviated-option", plan)

        result = run_validator(
            path,
            approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
            variables={"ALLOW_GHCR_PUBLISH": "true"},
            publish_plan_option="--publish-p",
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--publish-plan", result.stderr)


if __name__ == "__main__":
    unittest.main()
