from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import load_matrix
from scripts.publish_approval import scope_selection


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
VALIDATE_APPROVAL = ROOT / "scripts" / "validate-publish-approval.py"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"
MATRIX = load_matrix()
STREAM_IDS = [stream["id"] for stream in MATRIX["streams"]]
DEFAULT_STREAM = STREAM_IDS[0]
OTHER_STREAM = STREAM_IDS[1]
TEST_CANDIDATE_ID = "123456789-1"

SCOPE_CASES = {
    "keystone": ("core", "keystone"),
    "core": ("core", "all"),
    "deployment": ("deployment", "all"),
}


def generate_plan(
    *,
    stream: str = DEFAULT_STREAM,
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
        "--base-manifest",
        str(BASE_INDEX_FIXTURE),
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
    expected_scope: str,
    expected_candidate_id: str = TEST_CANDIDATE_ID,
    extra_args: list[str] | None = None,
    publish_plan_option: str = "--publish-plan",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_APPROVAL),
            publish_plan_option,
            str(plan_path),
            "--expected-candidate-id",
            expected_candidate_id,
            "--expected-scope",
            expected_scope,
            *(extra_args or []),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class PublishApprovalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.plan_directory = Path(cls.temp_directory.name)
        cls.plans: dict[tuple[str, str], dict] = {}
        for stream in STREAM_IDS:
            for scope, (profile, image) in SCOPE_CASES.items():
                cls.plans[(stream, scope)] = generate_plan(
                    stream=stream,
                    profile=profile,
                    image=None if image == "all" else image,
                )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def plan(self, stream: str, scope: str) -> dict:
        return copy.deepcopy(self.plans[(stream, scope)])

    def test_scope_mapping_contract(self) -> None:
        for scope, (profile, image) in SCOPE_CASES.items():
            with self.subTest(scope=scope):
                self.assertEqual(scope_selection(scope), (profile, image))
        with self.assertRaisesRegex(ValueError, "publish scope"):
            scope_selection("unknown")

    def test_all_active_streams_and_three_scopes_validate(self) -> None:
        case_count = 0
        for stream in STREAM_IDS:
            for scope in SCOPE_CASES:
                case_count += 1
                path = write_plan(
                    self.plan_directory,
                    f"positive-{stream}-{scope}",
                    self.plan(stream, scope),
                )
                with self.subTest(stream=stream, scope=scope):
                    result = run_validator(
                        path,
                        expected_scope=scope,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Frozen publish context validated.", result.stdout)
        self.assertEqual(case_count, len(STREAM_IDS) * len(SCOPE_CASES))

    def test_trusted_candidate_id_must_match_frozen_plan(self) -> None:
        path = write_plan(
            self.plan_directory,
            "candidate-mismatch",
            self.plan(DEFAULT_STREAM, "keystone"),
        )
        result = run_validator(
            path,
            expected_scope="keystone",
            expected_candidate_id="123456789-2",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate ID", result.stderr)

    def test_local_candidate_id_cannot_authorize_publication(self) -> None:
        plan = generate_plan(
            profile="core",
            image="keystone",
            candidate_id="local-dry-run",
        )
        path = write_plan(self.plan_directory, "local-candidate", plan)
        result = run_validator(
            path,
            expected_scope="keystone",
            expected_candidate_id="local-dry-run",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("workflow candidate ID", result.stderr)

    def test_expected_scope_must_match_frozen_plan(self) -> None:
        path = write_plan(
            self.plan_directory,
            "scope-mismatch",
            self.plan(DEFAULT_STREAM, "keystone"),
        )
        result = run_validator(
            path,
            expected_scope="core",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("trusted workflow scope", result.stderr)

    def test_invalid_expected_scope_is_rejected_by_cli(self) -> None:
        path = write_plan(
            self.plan_directory,
            "invalid-scope",
            self.plan(DEFAULT_STREAM, "keystone"),
        )
        result = run_validator(
            path,
            expected_scope="invalid",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_complete_plan_is_canonically_recomputed(self) -> None:
        mutations = {
            "namespace": lambda plan: plan.__setitem__("registry", "docker.io"),
            "stream": lambda plan: plan.__setitem__("stream", OTHER_STREAM),
            "scope": lambda plan: plan["scope"].__setitem__("image", "glance-api"),
            "images": lambda plan: plan["images"].pop(),
            "platform": lambda plan: plan["build"]["architectures"][0].__setitem__(
                "platform", "linux/ppc64le"
            ),
        }
        for name, mutate in mutations.items():
            plan = self.plan(DEFAULT_STREAM, "keystone")
            mutate(plan)
            path = write_plan(self.plan_directory, f"tampered-{name}", plan)
            with self.subTest(mutation=name):
                result = run_validator(
                    path,
                    expected_scope="keystone",
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("publish plan", result.stderr.lower())

    def test_duplicate_plan_keys_are_rejected_before_authorization(self) -> None:
        path = self.plan_directory / "duplicate-plan-key.json"
        raw = json.dumps(self.plan(DEFAULT_STREAM, "keystone"))
        path.write_text(raw[:-1] + ',"stream":"shadow"}', encoding="utf-8")

        result = run_validator(
            path,
            expected_scope="keystone",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate JSON object key", result.stderr)

    def test_partial_scope_is_not_authorized(self) -> None:
        plan = generate_plan(profile="core", image="glance-api")
        path = write_plan(self.plan_directory, "partial", plan)
        result = run_validator(
            path,
            expected_scope="core",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("trusted workflow scope", result.stderr)

    def test_validator_accepts_no_independent_profile_argument(self) -> None:
        path = write_plan(
            self.plan_directory,
            "legacy-argument",
            self.plan(DEFAULT_STREAM, "keystone"),
        )
        result = run_validator(
            path,
            expected_scope="keystone",
            extra_args=["--profile", "core"],
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --profile core", result.stderr)

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
                validator.recompute_publish_context(
                    self.plan(DEFAULT_STREAM, "keystone"),
                    TEST_CANDIDATE_ID,
                    "keystone",
                )
        finally:
            validator.load_matrix = original_load_matrix

    def test_review_rejects_abbreviated_publish_plan_option(self) -> None:
        path = write_plan(
            self.plan_directory,
            "abbreviated-option",
            self.plan(DEFAULT_STREAM, "keystone"),
        )
        result = run_validator(
            path,
            expected_scope="keystone",
            publish_plan_option="--publish-p",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--publish-plan", result.stderr)


if __name__ == "__main__":
    unittest.main()
