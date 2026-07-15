from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
BUILD_UNIT_WORKFLOW = ROOT / ".github" / "workflows" / "build-unit.yml"
VALIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
README = ROOT / "README.md"
BUILD_READINESS = ROOT / "docs" / "build-readiness.md"
PUBLISH_DOC = ROOT / "docs" / "publish.md"
DESIGN_SPEC = (
    ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-07-13-kolla-multi-stream-ghcr-design.md"
)
IMPLEMENTATION_PLAN = (
    ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-13-kolla-multi-stream-ghcr.md"
)
EXPECTED_ACTIONS = {
    "actions/checkout": ("9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0", "v7"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7"),
    "actions/download-artifact": ("3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", "v8"),
    "actions/setup-python": ("ece7cb06caefa5fff74198d8649806c4678c61a1", "v6"),
    "docker/setup-buildx-action": ("bb05f3f5519dd87d3ba754cc423b652a5edd6d2c", "v4"),
}
ACTION_RE = re.compile(
    r"(?m)^\s*uses:\s+([^@\s]+)@([0-9a-f]{40})\s+#\s+(v[0-9]+)\s*$"
)
FAKE_DOCKER = textwrap.dedent(
    r"""
    #!/usr/bin/env python3
    import json
    import os
    import pathlib
    import sys

    args = sys.argv[1:]
    log_path = pathlib.Path(os.environ["FAKE_DOCKER_LOG"])
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\n")

    fail_contains = os.environ.get("FAKE_DOCKER_FAIL_CONTAINS")
    if fail_contains and any(fail_contains in argument for argument in args):
        print("forced docker failure", file=sys.stderr)
        raise SystemExit(23)

    state_path = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    if len(args) == 5 and args[:4] == [
        "buildx", "imagetools", "inspect", "--raw"
    ]:
        reference = args[4]
        mismatch_ref = os.environ.get("FAKE_DOCKER_MISMATCH_REF")
        if reference == mismatch_ref:
            raw = f"mismatch:{reference}".encode()
        else:
            raw = f"raw:{state.get(reference, reference)}".encode()
        sys.stdout.buffer.write(raw)
    elif len(args) == 6 and args[:4] == [
        "buildx", "imagetools", "create", "--tag"
    ]:
        state[args[4]] = args[5]
        state_path.write_text(json.dumps(state), encoding="utf-8")
    else:
        print(f"unexpected fake docker command: {args!r}", file=sys.stderr)
        raise SystemExit(64)
    """
).lstrip()


def expected_action_use(repository: str) -> str:
    sha, release = EXPECTED_ACTIONS[repository]
    return f"uses: {repository}@{sha} # {release}"


def yaml_block(document: str, header: str) -> str:
    """Return the indentation-delimited YAML block beginning at header."""
    lines = document.splitlines()
    start = lines.index(header)
    indentation = len(header) - len(header.lstrip())
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and len(line) - len(line.lstrip()) <= indentation:
            end = index
            break
    return "\n".join(lines[start:end])


def python_heredoc(document: str, step_header: str) -> str:
    """Extract the executable Python heredoc from one workflow step."""
    step = yaml_block(document, step_header)
    lines = step.splitlines()
    opener = "          python3 - <<'PY'"
    terminator = "          PY"
    start = lines.index(opener) + 1
    end = lines.index(terminator, start)
    body = []
    for line in lines[start:end]:
        if line and not line.startswith("          "):
            raise AssertionError(f"unexpected heredoc indentation: {line!r}")
        body.append(line[10:] if line else "")
    return "\n".join(body) + "\n"


def alias_fixture() -> tuple[dict, dict]:
    candidate_id = "123456789-1"
    stream = "2025.1-rocky-9"
    images = []
    summary_images = []
    for index, image in enumerate(("keystone", "nova-api"), start=1):
        repository = (
            "ghcr.io/supergate-hub/kolla-container-images/" + image
        )
        deploy_ref = f"{repository}:{stream}-candidate-{candidate_id}"
        images.append(
            {
                "image": image,
                "stream_ref": f"{repository}:{stream}",
                "deploy_ref": deploy_ref,
            }
        )
        summary_images.append(
            {
                "image": image,
                "deploy_ref": deploy_ref,
                "manifest_digest": f"sha256:{index:064x}",
            }
        )
    return (
        {
            "candidate_id": candidate_id,
            "publish_summary_file": "artifacts/publish-summary.json",
            "images": images,
        },
        {
            "candidate_id": candidate_id,
            "images": summary_images,
        },
    )


class PublishWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.build_unit = BUILD_UNIT_WORKFLOW.read_text(encoding="utf-8")
        cls.validate = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.build_readiness = BUILD_READINESS.read_text(encoding="utf-8")
        cls.publish_doc = PUBLISH_DOC.read_text(encoding="utf-8")
        cls.design_spec = DESIGN_SPEC.read_text(encoding="utf-8")
        cls.implementation_plan = IMPLEMENTATION_PLAN.read_text(encoding="utf-8")
        cls.alias_script = python_heredoc(
            cls.publish,
            "      - name: Update convenience stream aliases",
        )
        cls.matrix_script = python_heredoc(
            cls.publish,
            "      - name: Publish dynamic build matrices",
        )

    def publish_job(self, name: str) -> str:
        return yaml_block(self.publish, f"  {name}:")

    def run_alias_script(
        self,
        plan: dict,
        summary: dict,
        *,
        mismatch_ref: str | None = None,
        fail_contains: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan_path = temp_path / "artifacts" / "plan" / "publish-plan.json"
            summary_path = temp_path / plan["publish_summary_file"]
            plan_path.parent.mkdir(parents=True)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
            fake_docker.chmod(0o755)
            command_log = temp_path / "docker-commands.jsonl"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
                    "FAKE_DOCKER_LOG": str(command_log),
                    "FAKE_DOCKER_STATE": str(temp_path / "docker-state.json"),
                }
            )
            if mismatch_ref is not None:
                environment["FAKE_DOCKER_MISMATCH_REF"] = mismatch_ref
            if fail_contains is not None:
                environment["FAKE_DOCKER_FAIL_CONTAINS"] = fail_contains

            result = subprocess.run(
                [sys.executable, "-c", self.alias_script],
                cwd=temp_path,
                env=environment,
                text=True,
                capture_output=True,
            )
            commands = (
                [
                    json.loads(line)
                    for line in command_log.read_text(encoding="utf-8").splitlines()
                ]
                if command_log.exists()
                else []
            )
            return result, commands

    def run_matrix_script(
        self,
        plan: dict,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str], bytes]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan_path = temp_path / "artifacts" / "plan" / "publish-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            output_path = temp_path / "github-output.txt"
            environment = os.environ.copy()
            environment["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                [sys.executable, "-c", self.matrix_script],
                cwd=temp_path,
                env=environment,
                text=True,
                capture_output=True,
            )
            output_bytes = output_path.read_bytes() if output_path.exists() else b""
            outputs = dict(
                line.split("=", 1)
                for line in output_bytes.decode("utf-8").splitlines()
            )
            return result, outputs, output_bytes

    def test_workflows_use_only_reviewed_action_commits(self) -> None:
        combined = self.publish + "\n" + self.build_unit + "\n" + self.validate
        raw_uses = re.findall(r"(?m)^\s*uses:\s+.+$", combined)
        local_calls = [
            line for line in raw_uses
            if "./.github/workflows/build-unit.yml" in line
        ]
        external_uses = [
            line for line in raw_uses
            if "./.github/workflows/build-unit.yml" not in line
        ]
        matches = ACTION_RE.findall(combined)
        self.assertEqual(len(local_calls), 5)
        self.assertEqual(len(matches), len(external_uses))
        for repository, sha, release in matches:
            with self.subTest(repository=repository):
                self.assertIn(repository, EXPECTED_ACTIONS)
                self.assertEqual((sha, release), EXPECTED_ACTIONS[repository])
        self.assertEqual(
            self.publish.count("uses: ./.github/workflows/build-unit.yml"),
            5,
        )
        self.assertNotIn("./.github/workflows/build-unit.yml", self.validate)
        self.assertNotIn("./.github/workflows/build-unit.yml", self.build_unit)

    def test_every_checkout_disables_persisted_credentials(self) -> None:
        checkout_header = (
            "uses: actions/checkout@"
            "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7"
        )
        for document, count in (
            (self.publish, 4),
            (self.build_unit, 1),
            (self.validate, 1),
        ):
            with self.subTest(count=count):
                self.assertEqual(document.count(checkout_header), count)
                blocks = re.findall(
                    rf"(?ms)^\s*- name: Check out repository\n"
                    rf"\s+{re.escape(checkout_header)}\n"
                    rf"\s+with:\n\s+persist-credentials: false(?:\n|$)",
                    document,
                )
                self.assertEqual(len(blocks), count)

    def test_dispatch_is_the_only_trigger_and_has_exact_frozen_inputs(self) -> None:
        trigger_block = yaml_block(self.publish, "on:")
        trigger_entries = [
            line.strip()
            for line in trigger_block.splitlines()[1:]
            if line.strip()
            and not line.lstrip().startswith("#")
            and len(line) - len(line.lstrip()) == 2
        ]
        self.assertEqual(trigger_entries, ["workflow_dispatch:"])
        dispatch = yaml_block(self.publish, "  workflow_dispatch:")
        expected_inputs = {"stream", "profile", "image", "dry_run", "approval"}
        inputs = set(re.findall(r"^      ([a-z_]+):$", dispatch, re.MULTILINE))
        self.assertEqual(inputs, expected_inputs)
        self.assertIn("type: string", yaml_block(dispatch, "      stream:"))
        dry_run = yaml_block(dispatch, "      dry_run:")
        self.assertIn("type: boolean", dry_run)
        self.assertIn("default: true", dry_run)
        self.assertNotIn("workflow_call:", self.publish)
        for legacy in ("release", "distro", "distro_version", "candidate_id"):
            self.assertNotIn(f"      {legacy}:", dispatch)

    def test_publish_flow_serializes_same_stream_writers(self) -> None:
        self.assertIn("concurrency:", self.publish)
        self.assertIn("group: kolla-publish-${{ inputs.stream }}", self.publish)
        self.assertIn("cancel-in-progress: false", self.publish)
        self.assertRegex(
            self.publish,
            r"(?m)^permissions:\n  contents: read$",
        )
        self.assertNotIn("environment_", self.publish)

    def test_publish_jobs_are_the_minimal_staged_dag_in_order(self) -> None:
        jobs = re.findall(
            r"(?m)^  ([a-z][a-z0-9-]+):$",
            yaml_block(self.publish, "jobs:"),
        )
        self.assertEqual(
            jobs,
            [
                "publish-plan",
                "authorize-publish",
                "build-parent-tier-0",
                "build-parent-tier-1",
                "build-parent-tier-2",
                "build-leaf-stage-0",
                "build-leaf-stage-1",
                "collect-native-evidence",
                "finalize-publish",
            ],
        )

    def test_plan_job_is_read_only_and_publishes_dynamic_matrices(self) -> None:
        job = self.publish_job("publish-plan")
        self.assertNotIn("packages: write", job)
        self.assertNotIn("docker login", job)
        self.assertNotIn("GITHUB_TOKEN", job)
        self.assertLess(
            job.index("python3 scripts/validate-config.py"),
            job.index("python3 scripts/plan-publish.py"),
        )
        for output in (
            "parent_tier_0_matrix",
            "parent_tier_1_matrix",
            "parent_tier_2_matrix",
            "leaf_stage_0_matrix",
            "leaf_stage_1_matrix",
            "leaf_stage_1_count",
        ):
            self.assertIn(
                f"{output}: ${{{{ steps.publish-matrices.outputs.{output} }}}}",
                job,
            )
        self.assertIn('plan["build"]["parent_tiers"]', job)
        self.assertIn('plan["build"]["leaf_stages"]', job)
        self.assertIn("[entry[\"stage\"] for entry in leaf_stages] != [0, 1]", job)
        self.assertIn("leaf_stage_1_count=", job)
        self.assertIn("separators=(',', ':')", job)
        self.assertIn("path: artifacts/plan/publish-plan.json", job)
        upload = yaml_block(job, "      - name: Upload publish plan")
        self.assertIn(expected_action_use("actions/upload-artifact"), upload)
        self.assertIn("if-no-files-found: error", upload)
        self.assertIn("retention-days: 7", upload)

    def test_matrix_output_keeps_an_empty_second_leaf_stage_safe(self) -> None:
        plan = {
            "build": {
                "parent_tiers": [
                    {"tier": tier, "matrix": {"include": [{"id": f"p{tier}"}]}}
                    for tier in range(3)
                ],
                "leaf_stages": [
                    {"stage": 0, "matrix": {"include": [{"id": "leaf-0"}]}},
                    {"stage": 1, "matrix": {"include": []}},
                ],
            }
        }

        result, outputs, output_bytes = self.run_matrix_script(plan)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs["leaf_stage_1_count"], "0")
        self.assertEqual(json.loads(outputs["leaf_stage_1_matrix"]), {"include": []})
        self.assertLess(len(output_bytes.decode("utf-8").encode("utf-16-le")), 1024**2)

    def test_plan_job_rejects_cross_repository_dispatch_before_checkout(self) -> None:
        job = self.publish_job("publish-plan")
        guard = "Require repository-owned invocation"
        self.assertIn(guard, job)
        self.assertIn("CALLER_REPOSITORY: ${{ github.repository }}", job)
        self.assertIn(
            'if [ "$CALLER_REPOSITORY" != "supergate-hub/kolla-container-images" ]; then',
            job,
        )
        self.assertLess(
            job.index(guard),
            job.index(expected_action_use("actions/checkout")),
        )

    def test_workflow_candidate_id_comes_only_from_run_context(self) -> None:
        candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
        self.assertIn(f"CANDIDATE_ID: {candidate}", self.publish)
        self.assertEqual(self.publish.count(f"candidate_id: {candidate}"), 5)
        dispatch = yaml_block(self.publish, "  workflow_dispatch:")
        self.assertNotIn("candidate_id:", dispatch)
        self.assertNotIn("workflow_call:", self.publish)

    def test_artifacts_are_unique_short_lived_and_build_artifacts_are_small(self) -> None:
        candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
        for name in (
            f"publish-plan-{candidate}",
            f"native-amd64-{candidate}",
            f"native-arm64-{candidate}",
            f"publish-${{{{ inputs.stream }}}}-{candidate}",
        ):
            self.assertIn(f"name: {name}", self.publish)
        self.assertIn(
            "name: unit-evidence-${{ fromJSON(inputs.unit).id }}-"
            "${{ inputs.candidate_id }}",
            self.build_unit,
        )
        self.assertIn(
            "name: unit-diagnostics-${{ fromJSON(inputs.unit).id }}-"
            "${{ inputs.candidate_id }}",
            self.build_unit,
        )
        self.assertEqual(self.publish.count("retention-days: 7"), 4)
        self.assertEqual(self.build_unit.count("retention-days: 7"), 1)
        self.assertEqual(self.publish.count("retention-days: 1"), 0)
        self.assertEqual(self.build_unit.count("retention-days: 1"), 1)
        for forbidden in ("docker save", "image.tar", "cache-to:", "cache-from:"):
            self.assertNotIn(forbidden, self.publish + self.build_unit)
        self.assertNotIn("overwrite:", self.publish + self.build_unit)

    def test_all_live_publish_stages_use_the_non_dry_run_gate(self) -> None:
        live_jobs = (
            "authorize-publish",
            "build-parent-tier-0",
            "build-parent-tier-1",
            "build-parent-tier-2",
            "build-leaf-stage-0",
            "build-leaf-stage-1",
            "collect-native-evidence",
            "finalize-publish",
        )
        for name in live_jobs:
            with self.subTest(job=name):
                self.assertIn("!inputs.dry_run", self.publish_job(name))

    def test_authorization_is_bound_before_all_package_writes(self) -> None:
        authorize = self.publish_job("authorize-publish")
        self.assertIn("needs: publish-plan", authorize)
        self.assertIn("environment: ghcr-publish", authorize)
        self.assertNotIn("packages: write", authorize)
        approval_validator = "python3 scripts/validate-publish-approval.py"
        candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
        self.assertIn(approval_validator, authorize)
        self.assertIn(candidate_binding, authorize)

        for name in (
            "build-parent-tier-0",
            "build-parent-tier-1",
            "build-parent-tier-2",
            "build-leaf-stage-0",
            "build-leaf-stage-1",
        ):
            with self.subTest(job=name):
                job = self.publish_job(name)
                self.assertIn("authorize-publish", job)
                self.assertIn("packages: write", job)
                self.assertIn("uses: ./.github/workflows/build-unit.yml", job)

        self.assertIn(approval_validator, self.build_unit)
        self.assertIn(candidate_binding, self.build_unit)
        self.assertLess(
            self.build_unit.index(approval_validator),
            self.build_unit.index("docker login ghcr.io"),
        )
        finalize = self.publish_job("finalize-publish")
        self.assertIn(approval_validator, finalize)
        self.assertLess(
            finalize.index(approval_validator),
            finalize.index("docker login ghcr.io"),
        )

    def test_every_mutating_layer_fails_closed_to_protected_main(self) -> None:
        for name in ("authorize-publish", "finalize-publish"):
            with self.subTest(job=name):
                job = self.publish_job(name)
                guard = job.index("Require protected main ref")
                checkout = job.index("Check out repository")
                self.assertLess(guard, checkout)
                self.assertIn("PUBLISH_REF: ${{ github.ref }}", job)
                self.assertIn("REF_PROTECTED: ${{ github.ref_protected }}", job)
                self.assertIn('"$PUBLISH_REF" != "refs/heads/main"', job)
                self.assertIn('"$REF_PROTECTED" != "true"', job)

        guard = self.build_unit.index("Require repository-owned invocation")
        checkout = self.build_unit.index("Check out repository")
        self.assertLess(guard, checkout)
        self.assertIn("CALLER_REF: ${{ github.ref }}", self.build_unit)
        self.assertIn(
            "CALLER_REF_PROTECTED: ${{ github.ref_protected }}",
            self.build_unit,
        )
        self.assertIn('"$CALLER_REF" != "refs/heads/main"', self.build_unit)
        self.assertIn('"$CALLER_REF_PROTECTED" != "true"', self.build_unit)

    def test_dynamic_stages_follow_parent_then_leaf_dependency_order(self) -> None:
        expected = {
            "build-parent-tier-0": (
                "parent_tier_0_matrix",
                "needs: [publish-plan, authorize-publish]",
            ),
            "build-parent-tier-1": (
                "parent_tier_1_matrix",
                "needs: [publish-plan, authorize-publish, build-parent-tier-0]",
            ),
            "build-parent-tier-2": (
                "parent_tier_2_matrix",
                "needs: [publish-plan, authorize-publish, build-parent-tier-1]",
            ),
            "build-leaf-stage-0": (
                "leaf_stage_0_matrix",
                "needs: [publish-plan, authorize-publish, build-parent-tier-2]",
            ),
            "build-leaf-stage-1": (
                "leaf_stage_1_matrix",
                "needs: [publish-plan, authorize-publish, build-leaf-stage-0]",
            ),
        }
        for name, (matrix_output, dependency) in expected.items():
            with self.subTest(job=name):
                job = self.publish_job(name)
                self.assertIn("fail-fast: false", job)
                self.assertIn("max-parallel: 4", job)
                self.assertIn(
                    f"matrix: ${{{{ fromJSON(needs.publish-plan.outputs.{matrix_output}) }}}}",
                    job,
                )
                self.assertIn(dependency, job)

        stage_1 = self.publish_job("build-leaf-stage-1")
        self.assertIn("leaf_stage_1_count != '0'", stage_1)
        native = self.publish_job("collect-native-evidence")
        self.assertIn("needs: [publish-plan, build-leaf-stage-0, build-leaf-stage-1]", native)
        self.assertIn("needs.build-leaf-stage-0.result == 'success'", native)
        self.assertIn("needs.build-leaf-stage-1.result == 'success'", native)
        self.assertIn("needs.build-leaf-stage-1.result == 'skipped'", native)
        self.assertIn("needs.publish-plan.outputs.leaf_stage_1_count == '0'", native)
        self.assertIn("!cancelled()", native)
        self.assertIn(
            "needs: collect-native-evidence",
            self.publish_job("finalize-publish"),
        )
        self.assertNotIn("self-hosted", self.publish + self.build_unit)
        self.assertNotIn("qemu", (self.publish + self.build_unit).lower())
        self.assertIn(
            "runs-on: ${{ fromJSON(inputs.unit).runner }}",
            self.build_unit,
        )

    def test_build_stages_download_only_the_evidence_available_to_them(self) -> None:
        candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
        parent_pattern = f"unit-evidence-*-parent-*-{candidate}"
        all_units_pattern = f"unit-evidence-*-{candidate}"

        self.assertNotIn(
            "input_evidence_artifact_pattern:",
            self.publish_job("build-parent-tier-0"),
        )
        for name in (
            "build-parent-tier-1",
            "build-parent-tier-2",
            "build-leaf-stage-0",
        ):
            with self.subTest(job=name):
                self.assertIn(
                    f"input_evidence_artifact_pattern: {parent_pattern}",
                    self.publish_job(name),
                )
        stage_1 = self.publish_job("build-leaf-stage-1")
        self.assertIn(
            f"input_evidence_artifact_pattern: {all_units_pattern}",
            stage_1,
        )
        self.assertNotIn("collect-parent-evidence", self.publish)
        self.assertNotIn("parent-index", self.publish)

    def test_build_unit_is_repository_owned_native_and_uses_local_docker(self) -> None:
        guard = self.build_unit.index("Require repository-owned invocation")
        checkout = self.build_unit.index("Check out repository")
        login = self.build_unit.index("docker login ghcr.io")
        self.assertLess(guard, checkout)
        self.assertIn(
            'if [ "$CALLER_REPOSITORY" != "supergate-hub/kolla-container-images" ]; then',
            self.build_unit,
        )
        for token in (
            "platform.machine()",
            "EXPECTED_RUNNER_MACHINE",
            "docker context inspect",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "unix:///",
            "{{.OSType}}",
            "{{.Architecture}}",
        ):
            self.assertIn(token, self.build_unit)
        self.assertLess(self.build_unit.index("platform.machine()"), login)
        self.assertLess(
            self.build_unit.index("Docker endpoint must be a local Unix socket"),
            login,
        )

    def test_build_unit_uses_preinstalled_buildx_without_install_cache(self) -> None:
        buildx = self.build_unit.index("docker buildx version")
        disk = self.build_unit.index("docker system df")
        login = self.build_unit.index("docker login ghcr.io")
        self.assertLess(buildx, disk)
        self.assertLess(disk, login)
        self.assertNotIn("docker system prune", self.build_unit)
        self.assertNotIn("docker/setup-buildx-action", self.build_unit)
        self.assertNotIn("cache: pip", self.build_unit)
        self.assertIn("pip install --no-cache-dir", self.build_unit)
        self.assertIn('"kolla==$KOLLA_VERSION" "docker==7.1.0"', self.build_unit)
        self.assertIn(".venv/bin/kolla-build --version", self.build_unit)

    def test_build_unit_and_collectors_exchange_only_evidence(self) -> None:
        for token in (
            "scripts/run-build-unit.py",
            "--unit-id",
            "--input-evidence-dir artifacts/input-evidence",
            '--output "artifacts/unit-evidence/$UNIT_ID.json"',
        ):
            self.assertIn(token, self.build_unit)
        success = yaml_block(self.build_unit, "      - name: Upload unit evidence")
        self.assertIn(".json", success)
        self.assertNotIn("kolla-summary", success)
        self.assertNotIn("kolla-logs", success)
        failure = yaml_block(self.build_unit, "      - name: Upload failure diagnostics")
        self.assertIn("if: ${{ failure() }}", failure)
        self.assertIn(".txt", failure)
        self.assertIn("retention-days: 1", failure)

        native = self.publish_job("collect-native-evidence")
        self.assertIn("pattern: unit-evidence-*-${{ github.run_id }}-${{ github.run_attempt }}", native)
        self.assertIn("merge-multiple: true", native)
        self.assertNotIn("--mode", native)
        self.assertNotIn("--parent-evidence", native)
        self.assertIn("artifacts/arch/native-amd64.json", native)
        self.assertIn("artifacts/arch/native-arm64.json", native)
        self.assertNotIn("parent-index", self.publish + self.build_unit)
        self.assertNotIn("parent-evidence", self.publish + self.build_unit)

    def test_package_write_is_limited_to_unit_callers_and_finalizer(self) -> None:
        package_callers = (
            "build-parent-tier-0",
            "build-parent-tier-1",
            "build-parent-tier-2",
            "build-leaf-stage-0",
            "build-leaf-stage-1",
        )
        self.assertEqual(self.publish.count("packages: write"), 6)
        for name in package_callers + ("finalize-publish",):
            self.assertIn("packages: write", self.publish_job(name))
        for name in (
            "publish-plan",
            "authorize-publish",
            "collect-native-evidence",
        ):
            self.assertNotIn("packages: write", self.publish_job(name))
        self.assertEqual(self.build_unit.count("packages: write"), 1)

    def test_package_jobs_use_fresh_ephemeral_docker_config_and_cleanup(self) -> None:
        for document in (self.build_unit, self.publish_job("finalize-publish")):
            with self.subTest(document=document[:50]):
                prepare = document.index("Prepare ephemeral Docker client state")
                login = document.index("docker login ghcr.io")
                cleanup = document.index("Remove ephemeral Docker client state")
                self.assertLess(prepare, login)
                self.assertLess(login, cleanup)
                self.assertIn("RUNNER_TEMP", document)
                self.assertIn("GITHUB_RUN_ID", document)
                self.assertIn("GITHUB_RUN_ATTEMPT", document)
                self.assertIn("if: ${{ always() }}", document)
                self.assertIn('rm -f -- "$DOCKER_CONFIG/config.json"', document)

    def test_finalize_downloads_exact_evidence_and_revalidates_before_login(self) -> None:
        job = self.publish_job("finalize-publish")
        self.assertIn("needs: collect-native-evidence", job)
        self.assertIn(expected_action_use("actions/checkout"), job)
        candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
        for artifact in ("publish-plan", "native-amd64", "native-arm64"):
            self.assertIn(f"name: {artifact}-{candidate}", job)
        self.assertNotIn("pattern:", job)
        self.assertNotIn("merge-multiple:", job)
        approval_validator = "python3 scripts/validate-publish-approval.py"
        candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
        self.assertIn(approval_validator, job)
        self.assertIn(candidate_binding, job)
        self.assertLess(
            job.index(approval_validator),
            job.index("docker login ghcr.io"),
        )

    def test_finalize_uses_recorded_children_and_verifies_exact_multiarch_manifest(self) -> None:
        job = self.publish_job("finalize-publish")
        self.assertIn('child_ref = f"{repository}@{record[\'digest\']}"', job)
        self.assertRegex(
            job,
            r'"imagetools",\s+"create",\s+"--tag",\s+deploy_ref',
        )
        self.assertIn('"imagetools", "inspect", "--raw", deploy_ref', job)
        self.assertIn('len(index["manifests"]) != 2', job)
        self.assertIn('{"linux/amd64", "linux/arm64"}', job)
        self.assertIn("recorded_child_digests", job)
        self.assertIn('if "annotations" in descriptor:', job)
        self.assertIn('manifest_metadata.get("containerimage.digest")', job)
        self.assertIn('manifest_metadata.get("containerimage.descriptor")', job)
        self.assertIn("DIGEST_RE.fullmatch(manifest_digest)", job)
        self.assertIn('summary_path = pathlib.Path(plan["publish_summary_file"])', job)
        self.assertIn("scripts/validate-publish-summary.py", job)
        self.assertNotIn('image["commands"]["manifest_create"]', job)
        self.assertIn('expected_parent_names', job)
        self.assertIn('expected_image_names', job)
        self.assertIn('evidence["stream"] != plan["stream"]', job)
        self.assertIn('evidence["kolla_version"] != plan["kolla_version"]', job)
        self.assertIn('record["smoke"].get("passed") is not True', job)
        self.assertNotIn('publish_summary["images"].append(parent', job)

    def test_candidate_artifact_is_uploaded_before_stream_alias_updates(self) -> None:
        job = self.publish_job("finalize-publish")
        manifests = job.index("Create and verify candidate multi-architecture manifests")
        validate = job.index("Validate summary and generate eligible candidate lock")
        upload = job.index("Upload publish artifacts")
        aliases = job.index("Update convenience stream aliases")
        self.assertLess(manifests, validate)
        self.assertLess(validate, upload)
        self.assertLess(upload, aliases)

    def test_stream_aliases_are_created_from_candidate_immutable_digests(self) -> None:
        step = yaml_block(
            self.publish_job("finalize-publish"),
            "      - name: Update convenience stream aliases",
        )
        self.assertIn('stream_ref = planned_image["stream_ref"]', step)
        self.assertIn('candidate_ref = summary_image["deploy_ref"]', step)
        self.assertIn('immutable_ref = f"{repository}@{manifest_digest}"', step)
        self.assertRegex(
            step,
            r'"imagetools",\s+"create",\s+"--tag",\s+stream_ref,\s+immutable_ref',
        )
        self.assertIn("if stream_raw.stdout != immutable_raw.stdout:", step)

    def test_alias_step_executes_two_images_sequentially_from_immutable_refs(self) -> None:
        plan, summary = alias_fixture()

        result, commands = self.run_alias_script(plan, summary)

        expected_commands = []
        for planned_image, summary_image in zip(
            plan["images"],
            summary["images"],
            strict=True,
        ):
            repository = planned_image["deploy_ref"].rpartition(":")[0]
            immutable_ref = f"{repository}@{summary_image['manifest_digest']}"
            expected_commands.extend(
                [
                    ["buildx", "imagetools", "inspect", "--raw", immutable_ref],
                    [
                        "buildx",
                        "imagetools",
                        "create",
                        "--tag",
                        planned_image["stream_ref"],
                        immutable_ref,
                    ],
                    [
                        "buildx",
                        "imagetools",
                        "inspect",
                        "--raw",
                        planned_image["stream_ref"],
                    ],
                ]
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(commands, expected_commands)

    def test_alias_step_rejects_preflight_tampering_without_docker(self) -> None:
        cases = []

        plan, summary = alias_fixture()
        summary["candidate_id"] = "123456789-2"
        cases.append(
            (
                "candidate ID mismatch",
                "publish summary candidate ID does not match frozen plan",
                plan,
                summary,
            )
        )

        plan, summary = alias_fixture()
        summary["images"][1]["image"] = summary["images"][0]["image"]
        cases.append(
            (
                "duplicate summary names",
                "publish summary contains duplicate image names",
                plan,
                summary,
            )
        )

        plan, summary = alias_fixture()
        summary["images"][0]["image"] = "glance-api"
        cases.append(
            (
                "membership mismatch",
                "publish summary images do not match frozen plan",
                plan,
                summary,
            )
        )

        plan, summary = alias_fixture()
        summary["images"][0]["deploy_ref"] += "-wrong"
        cases.append(
            (
                "candidate ref mismatch",
                "candidate ref mismatch: keystone",
                plan,
                summary,
            )
        )

        plan, summary = alias_fixture()
        wrong_repository_ref = plan["images"][0]["deploy_ref"].replace(
            "ghcr.io/supergate-hub/kolla-container-images/keystone",
            "ghcr.io/supergate-hub/other/keystone",
        )
        plan["images"][0]["deploy_ref"] = wrong_repository_ref
        summary["images"][0]["deploy_ref"] = wrong_repository_ref
        cases.append(
            (
                "repository mismatch",
                "candidate/stream ref mismatch: keystone",
                plan,
                summary,
            )
        )

        plan, summary = alias_fixture()
        summary["images"][0]["manifest_digest"] = "sha256:bad"
        cases.append(
            (
                "malformed digest",
                "candidate digest is invalid: keystone",
                plan,
                summary,
            )
        )

        for name, expected_error, plan, summary in cases:
            with self.subTest(case=name):
                result, commands = self.run_alias_script(plan, summary)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                self.assertEqual(commands, [])

    def test_alias_step_raw_mismatch_stops_before_later_image_write(self) -> None:
        plan, summary = alias_fixture()
        first_plan = plan["images"][0]
        first_summary = summary["images"][0]
        repository = first_plan["deploy_ref"].rpartition(":")[0]
        immutable_ref = f"{repository}@{first_summary['manifest_digest']}"

        result, commands = self.run_alias_script(
            plan,
            summary,
            mismatch_ref=first_plan["stream_ref"],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "stream alias bytes do not match candidate: keystone\n",
        )
        self.assertEqual(
            commands,
            [
                ["buildx", "imagetools", "inspect", "--raw", immutable_ref],
                [
                    "buildx",
                    "imagetools",
                    "create",
                    "--tag",
                    first_plan["stream_ref"],
                    immutable_ref,
                ],
                [
                    "buildx",
                    "imagetools",
                    "inspect",
                    "--raw",
                    first_plan["stream_ref"],
                ],
            ],
        )
        self.assertFalse(
            any(plan["images"][1]["stream_ref"] in command for command in commands)
        )

    def test_alias_step_docker_failure_propagates_and_stops_later_writes(self) -> None:
        plan, summary = alias_fixture()
        first_plan = plan["images"][0]
        first_summary = summary["images"][0]
        repository = first_plan["deploy_ref"].rpartition(":")[0]
        immutable_ref = f"{repository}@{first_summary['manifest_digest']}"

        result, commands = self.run_alias_script(
            plan,
            summary,
            fail_contains=first_plan["stream_ref"],
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("returned non-zero exit status 23", result.stderr)
        self.assertEqual(
            commands,
            [
                ["buildx", "imagetools", "inspect", "--raw", immutable_ref],
                [
                    "buildx",
                    "imagetools",
                    "create",
                    "--tag",
                    first_plan["stream_ref"],
                    immutable_ref,
                ],
            ],
        )
        self.assertFalse(
            any(plan["images"][1]["stream_ref"] in command for command in commands)
        )

    def test_finalize_binds_summary_digest_to_exact_immutable_manifest_bytes(self) -> None:
        job = self.publish_job("finalize-publish")

        self.assertIn("import hashlib", job)
        self.assertIn(
            'manifest_descriptor = manifest_metadata.get("containerimage.descriptor")',
            job,
        )
        self.assertIn('manifest_descriptor.get("digest")', job)
        self.assertIn('manifest_descriptor.get("mediaType")', job)
        self.assertIn('manifest_descriptor.get("size")', job)
        self.assertIn('immutable_manifest_ref = f"{repository}@{manifest_digest}"', job)
        self.assertRegex(
            job,
            r'"imagetools",\s+"inspect",\s+"--raw",\s+immutable_manifest_ref',
        )
        self.assertIn(
            'raw_digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"',
            job,
        )
        self.assertIn("if raw_digest != manifest_digest:", job)
        self.assertIn("if manifest_size != len(raw_bytes):", job)
        self.assertIn(
            '["docker", "buildx", "imagetools", "inspect", "--raw", deploy_ref]',
            job,
        )
        self.assertIn("if tagged_raw_result.stdout != raw_bytes:", job)

        metadata = job.index(
            'manifest_descriptor = manifest_metadata.get("containerimage.descriptor")'
        )
        immutable = job.index('immutable_manifest_ref = f"{repository}@{manifest_digest}"')
        tag_match = job.index("if tagged_raw_result.stdout != raw_bytes:")
        self.assertLess(metadata, immutable)
        self.assertLess(immutable, tag_match)

    def test_finalize_accepts_descriptor_only_metadata_and_checks_optional_digest(self) -> None:
        job = self.publish_job("finalize-publish")

        self.assertIn(
            'metadata_digest = manifest_metadata.get("containerimage.digest")',
            job,
        )
        self.assertIn(
            "if metadata_digest is not None and metadata_digest != manifest_digest:",
            job,
        )
        self.assertNotIn(
            'if manifest_metadata.get("containerimage.digest") != manifest_digest:',
            job,
        )

    def test_finalize_accepts_only_standard_multiarch_media_types(self) -> None:
        job = self.publish_job("finalize-publish")
        manifest_step = yaml_block(
            job,
            "      - name: Create and verify candidate multi-architecture manifests",
        )
        media_types_match = re.search(
            r"MULTIARCH_MEDIA_TYPES = \{(?P<body>.*?)\n\s+\}",
            manifest_step,
            re.DOTALL,
        )
        self.assertIsNotNone(media_types_match)
        assert media_types_match is not None
        media_types = re.findall(r'"([^"]+)"', media_types_match.group("body"))

        self.assertEqual(
            media_types,
            [
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
            ],
        )
        self.assertIn(
            'if manifest_media_type not in MULTIARCH_MEDIA_TYPES:',
            job,
        )
        self.assertIn(
            'if index.get("mediaType") != manifest_media_type:',
            job,
        )
        self.assertIn("-multiarch-manifest.json", job)
        self.assertNotIn("OCI_INDEX_MEDIA_TYPE =", job)

        for document in (self.publish_doc, self.build_readiness):
            with self.subTest(document=document[:40]):
                for media_type in media_types:
                    self.assertIn(media_type, document)

    def test_docs_record_hosted_shards_diagnostics_and_arm_policy(self) -> None:
        for document in (self.publish_doc, self.build_readiness):
            with self.subTest(document=document[:40]):
                self.assertIn("ubuntu-24.04", document)
                self.assertIn("ubuntu-24.04-arm", document)
                self.assertIn("max-parallel: 4", document)
                self.assertIn("Re-run all jobs", document)
                self.assertNotIn("self-hosted", document)
        self.assertIn("unit-diagnostics", self.publish_doc)
        self.assertIn("failed unit", self.build_readiness.lower())

        normalized_readme = " ".join(self.readme.split())
        self.assertIn(
            "The pipeline policy requires every stream to be built and image-smoked "
            "on native ARM64 CI",
            normalized_readme,
        )
        self.assertNotIn(
            "Every stream is also built and\nsmoked on native ARM64 CI",
            self.readme,
        )

    def test_design_docs_pin_the_required_docker_sdk(self) -> None:
        for document in (
            self.build_readiness,
            self.design_spec,
            self.implementation_plan,
        ):
            with self.subTest(document=document[:40]):
                self.assertIn("docker==7.1.0", document)

    def test_candidate_lock_is_only_generated_from_deployment_all_plan(self) -> None:
        job = self.publish_job("finalize-publish")
        self.assertIn('plan["scope"] == {', job)
        self.assertIn('"profile": "deployment"', job)
        self.assertIn('"image": "all"', job)
        self.assertIn('"image_count": len(plan["images"])', job)
        self.assertIn("scripts/generate-lock.py", job)
        self.assertIn('plan["kolla_ansible_lock_file"]', job)
        self.assertIn("else:\n              if plan[\"kolla_ansible_lock_file\"] is not None:", job)
        self.assertIn(
            "name: publish-${{ inputs.stream }}-"
            "${{ github.run_id }}-${{ github.run_attempt }}",
            job,
        )
        self.assertIn("artifacts/publish-summary-${{ inputs.stream }}.json", job)
        self.assertIn(
            "artifacts/kolla-ansible-image-lock-${{ inputs.stream }}.yml",
            job,
        )
        self.assertNotIn("--environment", self.publish)
        self.assertNotRegex(self.publish.lower(), r"\b(?:dev|stg|prod)\b")

    def test_validation_ci_parses_all_json_and_exercises_required_plans(self) -> None:
        self.assertIn("find . -type f -name '*.json' -print0", self.validate)
        self.assertIn("python3 -m json.tool", self.validate)
        self.assertIn("set -euo pipefail", self.validate)
        self.assertIn("python3 scripts/validate-config.py", self.validate)
        self.assertIn("--stream 2025.1-rocky-9 --profile core --dry-run", self.validate)
        self.assertIn("--stream 2025.1-rocky-9 --profile deployment --dry-run", self.validate)
        self.assertIn(
            "--stream 2025.1-rocky-9 --profile core --image keystone --dry-run",
            self.validate,
        )
        self.assertIn("python3 -m unittest discover -s tests -v", self.validate)


if __name__ == "__main__":
    unittest.main()
