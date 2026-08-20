from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
BUILD_UNIT_WORKFLOW = ROOT / ".github" / "workflows" / "build-unit.yml"
VALIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
SYNC_STREAM_OPTIONS_WORKFLOW = (
    ROOT / ".github" / "workflows" / "sync-publish-stream-options.yml"
)
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
    "actions/create-github-app-token": (
        "bcd2ba49218906704ab6c1aa796996da409d3eb1",
        "v3",
    ),
    "docker/setup-buildx-action": ("bb05f3f5519dd87d3ba754cc423b652a5edd6d2c", "v4"),
}
ACTION_RE = re.compile(
    r"(?m)^\s*uses:\s+([^@\s]+)@([0-9a-f]{40})\s+#\s+(v[0-9]+)\s*$"
)
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


class PublishWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.build_unit = BUILD_UNIT_WORKFLOW.read_text(encoding="utf-8")
        cls.validate = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
        cls.sync_stream_options = SYNC_STREAM_OPTIONS_WORKFLOW.read_text(
            encoding="utf-8"
        )
        cls.readme = README.read_text(encoding="utf-8")
        cls.build_readiness = BUILD_READINESS.read_text(encoding="utf-8")
        cls.publish_doc = PUBLISH_DOC.read_text(encoding="utf-8")
        cls.design_spec = DESIGN_SPEC.read_text(encoding="utf-8")
        cls.implementation_plan = IMPLEMENTATION_PLAN.read_text(encoding="utf-8")
        cls.matrix_script = python_heredoc(
            cls.publish,
            "      - name: Publish dynamic build matrices",
        )

    def publish_job(self, name: str) -> str:
        return yaml_block(self.publish, f"  {name}:")

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
        combined = "\n".join(
            (
                self.publish,
                self.build_unit,
                self.validate,
                self.sync_stream_options,
            )
        )
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
        expected_inputs = {"operation", "stream", "scope"}
        inputs = set(re.findall(r"^      ([a-z_]+):$", dispatch, re.MULTILINE))
        self.assertEqual(inputs, expected_inputs)
        stream = yaml_block(dispatch, "      stream:")
        self.assertIn("type: choice", stream)
        self.assertIn("# BEGIN GENERATED STREAM OPTIONS", stream)
        self.assertIn("# END GENERATED STREAM OPTIONS", stream)
        self.assertIn("- 2025.1-rocky-10.2-20.5.0", stream)
        operation = yaml_block(dispatch, "      operation:")
        self.assertIn("type: choice", operation)
        self.assertIn("options:", operation)
        self.assertIn("- plan", operation)
        self.assertIn("- publish", operation)
        self.assertIn("default: plan", operation)
        scope = yaml_block(dispatch, "      scope:")
        self.assertIn("type: choice", scope)
        for choice in ("keystone", "core", "deployment"):
            self.assertIn(f"- {choice}", scope)
        self.assertIn("default: keystone", scope)
        self.assertNotIn("workflow_call:", self.publish)
        for legacy in (
            "release",
            "distro",
            "distro_version",
            "candidate_id",
            "profile",
            "image",
            "dry_run",
            "approval",
        ):
            self.assertNotIn(f"      {legacy}:", dispatch)

        self.assertIn(
            "run-name: Kolla ${{ inputs.operation }} · ${{ github.ref_name }} · "
            "${{ inputs.stream }} · ${{ inputs.scope }}",
            self.publish,
        )

    def test_publish_flow_queues_every_same_stream_writer(self) -> None:
        concurrency = yaml_block(self.publish, "concurrency:")
        self.assertIn("inputs.operation == 'publish'", concurrency)
        self.assertIn(
            "format('kolla-publish-{0}-{1}', github.ref_name, inputs.stream)",
            concurrency,
        )
        self.assertIn("format('kolla-plan-{0}', github.run_id)", concurrency)
        self.assertIn("queue: max", concurrency)
        self.assertNotIn("cancel-in-progress:", concurrency)
        self.assertRegex(
            self.publish,
            r"(?m)^permissions:\n  contents: read$",
        )
        self.assertNotIn("environment_", self.publish)

    def test_plan_flow_cancels_only_older_same_stream_plans(self) -> None:
        plan_job = self.publish_job("publish-plan")
        concurrency = yaml_block(plan_job, "    concurrency:")
        self.assertIn("inputs.operation == 'plan'", concurrency)
        self.assertIn(
            "format('kolla-plan-{0}-{1}', github.ref_name, inputs.stream)",
            concurrency,
        )
        self.assertIn("format('kolla-publish-plan-{0}', github.run_id)", concurrency)
        self.assertIn(
            "cancel-in-progress: ${{ inputs.operation == 'plan' }}",
            concurrency,
        )
        self.assertNotIn("queue: max", concurrency)

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
                "publish-result",
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

    def test_plan_validates_against_exact_pinned_release_metadata(self) -> None:
        job = self.publish_job("publish-plan")
        checkout = yaml_block(
            job,
            "      - name: Check out pinned OpenStack release metadata",
        )
        self.assertIn(
            "RELEASES_REPOSITORY: https://opendev.org/openstack/releases",
            checkout,
        )
        self.assertIn('matrix["release_metadata"]["commit"]', checkout)
        self.assertIn('git init --quiet "$CHECKOUT_PATH"', checkout)
        self.assertIn(
            'git -C "$CHECKOUT_PATH" remote add origin "$RELEASES_REPOSITORY"',
            checkout,
        )
        self.assertIn(
            'git -C "$CHECKOUT_PATH" fetch --no-tags --depth=1 origin '
            '"$RELEASES_COMMIT"',
            checkout,
        )
        self.assertIn(
            'git -C "$CHECKOUT_PATH" checkout --quiet --detach FETCH_HEAD',
            checkout,
        )

        validation = yaml_block(job, "      - name: Validate repository configuration")
        self.assertIn(
            '--release-metadata-checkout "$RELEASE_METADATA_CHECKOUT"',
            validation,
        )
        self.assertLess(
            job.index("Check out pinned OpenStack release metadata"),
            job.index("Validate repository configuration"),
        )
        self.assertLess(
            job.index("Validate repository configuration"),
            job.index("      - name: Render frozen publish plan"),
        )

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
        self.assertIn(f"CANDIDATE_ID: {candidate}", self.build_unit)
        self.assertNotIn("candidate_id:", self.build_unit)
        self.assertNotIn("candidate_id:", self.publish)
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
            "${{ github.run_id }}-${{ github.run_attempt }}",
            self.build_unit,
        )
        self.assertIn(
            "name: unit-diagnostics-${{ fromJSON(inputs.unit).id }}-"
            "${{ github.run_id }}-${{ github.run_attempt }}",
            self.build_unit,
        )
        self.assertEqual(self.publish.count("retention-days: 7"), 4)
        self.assertEqual(self.build_unit.count("retention-days: 7"), 1)
        self.assertEqual(self.publish.count("retention-days: 1"), 0)
        self.assertEqual(self.build_unit.count("retention-days: 1"), 1)
        for forbidden in ("docker save", "image.tar", "cache-to:", "cache-from:"):
            self.assertNotIn(forbidden, self.publish + self.build_unit)
        self.assertNotIn("overwrite:", self.publish + self.build_unit)

    def test_all_live_publish_stages_use_the_publish_operation_gate(self) -> None:
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
                self.assertIn(
                    "inputs.operation == 'publish'",
                    self.publish_job(name),
                )
                self.assertNotIn("inputs.dry_run", self.publish_job(name))

    def test_environment_gate_is_bound_before_all_package_writes(self) -> None:
        authorize = self.publish_job("authorize-publish")
        self.assertIn("needs: publish-plan", authorize)
        self.assertIn("environment: ghcr-publish", authorize)
        self.assertNotIn("packages: write", authorize)
        approval_validator = "python3 scripts/validate-publish-approval.py"
        candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
        self.assertIn(approval_validator, authorize)
        self.assertIn(candidate_binding, authorize)
        self.assertIn('--expected-scope "$PUBLISH_SCOPE"', authorize)
        self.assertNotIn("APPROVAL:", authorize)
        self.assertNotIn("ALLOW_GHCR_", self.publish + self.build_unit)

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
        self.assertIn('--expected-scope "$PUBLISH_SCOPE"', self.build_unit)
        self.assertNotIn("approval:", self.build_unit)
        self.assertLess(
            self.build_unit.index(approval_validator),
            self.build_unit.index("docker login ghcr.io"),
        )
        finalize = self.publish_job("finalize-publish")
        self.assertIn(approval_validator, finalize)
        self.assertIn('--expected-scope "$PUBLISH_SCOPE"', finalize)
        self.assertNotIn("APPROVAL:", finalize)
        self.assertLess(
            finalize.index(approval_validator),
            finalize.index("docker login ghcr.io"),
        )

    def test_scope_input_is_mapped_only_inside_the_plan_job(self) -> None:
        job = self.publish_job("publish-plan")
        self.assertIn("SCOPE: ${{ inputs.scope }}", job)
        self.assertIn('case "$SCOPE" in', job)
        self.assertIn("keystone)", job)
        self.assertIn('profile="core"', job)
        self.assertIn('image="keystone"', job)
        self.assertIn("core)", job)
        self.assertIn('image="all"', job)
        self.assertIn("deployment)", job)
        self.assertIn('profile="deployment"', job)
        self.assertIn('--profile "$profile"', job)
        self.assertIn('plan_args+=(--image "$image")', job)
        self.assertNotIn("PROFILE: ${{ inputs.profile }}", job)
        self.assertNotIn("IMAGE: ${{ inputs.image }}", job)

    def test_plan_and_result_summaries_are_always_available(self) -> None:
        plan = self.publish_job("publish-plan")
        self.assertIn("Write plan summary", plan)
        self.assertIn("GITHUB_STEP_SUMMARY", plan)
        self.assertIn("operation", plan.lower())
        self.assertIn("stream", plan.lower())
        self.assertIn("scope", plan.lower())
        self.assertIn("Base index digest", plan)
        self.assertIn('for architecture in ("amd64", "arm64")', plan)
        self.assertIn('f"- Base linux/{architecture}:', plan)
        self.assertIn("OpenStack source set", plan)
        self.assertIn("Source-set digest", plan)
        self.assertEqual(plan.count("Source-set digest"), 1)
        self.assertIn("Semantic tag", plan)
        self.assertIn("Revision tag", plan)

        result = self.publish_job("publish-result")
        self.assertIn("if: ${{ always() }}", result)
        self.assertIn("GITHUB_STEP_SUMMARY", result)
        self.assertNotIn("packages: write", result)

    def test_publish_plan_is_bound_to_protected_main(self) -> None:
        job = self.publish_job("publish-plan")
        self.assertNotIn("Validate release branch matrix", job)
        self.assertIn("Bind frozen plan to protected main", job)
        ref_gate = yaml_block(job, "      - name: Bind frozen plan to protected main")
        self.assertIn("if: ${{ inputs.operation == 'publish' }}", ref_gate)
        self.assertIn("REF_PROTECTED: ${{ github.ref_protected }}", ref_gate)
        self.assertIn("--require-protected", ref_gate)
        self.assertIn('--ref-protected "$REF_PROTECTED"', ref_gate)
        self.assertIn("PUBLISH_REF: ${{ github.ref }}", job)
        self.assertIn("scripts/validate-release-context.py", job)
        self.assertNotIn("validate-release-context.py publish", job)
        self.assertIn('--git-ref "$PUBLISH_REF"', job)
        self.assertLess(
            job.index("Bind frozen plan to protected main"),
            job.index("Upload publish plan"),
        )

    def test_plan_operation_has_no_main_ref_gate_or_registry_mutation(self) -> None:
        job = self.publish_job("publish-plan")
        self.assertNotIn("packages: write", job)
        self.assertNotIn("docker login", job)
        self.assertNotIn("GITHUB_TOKEN", job)
        step = yaml_block(job, "      - name: Bind frozen plan to protected main")
        self.assertIn("if: ${{ inputs.operation == 'publish' }}", step)

    def test_disabled_publish_stream_is_rejected_before_environment_approval(self) -> None:
        plan = self.publish_job("publish-plan")
        step = yaml_block(plan, "      - name: Reject disabled publish stream")
        self.assertIn("if: ${{ inputs.operation == 'publish' }}", step)
        self.assertIn("find_stream", step)
        self.assertIn('stream.get("publish_enabled") is not True', step)
        self.assertLess(
            plan.index("Reject disabled publish stream"),
            plan.index("Upload publish plan"),
        )

    def test_every_mutating_layer_fails_closed_to_protected_main(self) -> None:
        for name in ("authorize-publish", "finalize-publish"):
            with self.subTest(job=name):
                job = self.publish_job(name)
                guard = job.index("protected main publication context")
                self.assertIn("PUBLISH_REF: ${{ github.ref }}", job)
                self.assertIn("REF_PROTECTED: ${{ github.ref_protected }}", job)
                self.assertIn("scripts/validate-release-context.py", job)
                self.assertNotIn("validate-release-context.py publish", job)
                self.assertIn('--git-ref "$PUBLISH_REF"', job)
                self.assertIn("--require-protected", job)
                self.assertIn('--ref-protected "$REF_PROTECTED"', job)
                self.assertIn("protected main", job)
                next_sensitive_step = (
                    "docker login ghcr.io"
                    if name == "finalize-publish"
                    else "python3 scripts/validate-publish-approval.py"
                )
                self.assertLess(guard, job.index(next_sensitive_step))

        guard = self.build_unit.index("Require repository-owned invocation")
        checkout = self.build_unit.index("Check out repository")
        self.assertLess(guard, checkout)
        self.assertIn("CALLER_REF: ${{ github.ref }}", self.build_unit)
        self.assertIn(
            "CALLER_REF_PROTECTED: ${{ github.ref_protected }}",
            self.build_unit,
        )
        self.assertIn("scripts/validate-release-context.py", self.build_unit)
        self.assertNotIn("validate-release-context.py publish", self.build_unit)
        self.assertIn('--git-ref "$CALLER_REF"', self.build_unit)
        self.assertIn("--require-protected", self.build_unit)
        self.assertIn('--ref-protected "$CALLER_REF_PROTECTED"', self.build_unit)
        self.assertIn('"$CALLER_REF" != "refs/heads/main"', self.build_unit)

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
        finalize = self.publish_job("finalize-publish")
        self.assertIn("needs: collect-native-evidence", finalize)
        self.assertIn("needs.collect-native-evidence.result == 'success'", finalize)
        self.assertIn("!cancelled()", finalize)
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
        self.assertIn("scripts/frozen_sources.py prepare", self.build_unit)
        self.assertEqual(
            self.build_unit.count("--build-config-dir artifacts/config"),
            3,
        )
        self.assertIn(
            "scripts/frozen_sources.py prepare-unit-sources", self.build_unit
        )
        self.assertEqual(
            self.build_unit.count(
                "--source-archive-dir artifacts/source-archives"
            ),
            2,
        )
        self.assertIn('PBR_VERSION="$KOLLA_VERSION"', self.build_unit)
        self.assertIn('"$KOLLA_SOURCE_DIR"', self.build_unit)
        self.assertNotIn('"kolla==$KOLLA_VERSION"', self.build_unit)
        self.assertIn(".venv/bin/kolla-build --version", self.build_unit)
        self.assertIn('export PATH="$PWD/.venv/bin:$PATH"', self.build_unit)
        self.assertIn(
            'test "$(command -v kolla-build)" = "$PWD/.venv/bin/kolla-build"',
            self.build_unit,
        )

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
            "publish-result",
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
            r'"imagetools",\s+"create",\s+"--tag",\s+revision_ref',
        )
        self.assertIn('"imagetools", "inspect", "--raw", revision_ref', job)
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
        self.assertIn('evidence["schema_version"] != 3', job)
        self.assertIn('evidence["kolla"] != plan["kolla"]', job)
        self.assertIn('evidence["base"] != plan["base"]', job)
        self.assertIn(
            'evidence["openstack_sources"] != plan["openstack_sources"]',
            job,
        )
        for key in (
            "release_series",
            "release_branch",
            "release_metadata",
            "kolla",
            "kolla_ansible",
            "base",
        ):
            self.assertIn(f'"{key}": plan["{key}"]', job)
        self.assertIn('record["smoke"].get("passed") is not True', job)
        self.assertNotIn('publish_summary["images"].append(parent', job)

    def test_revision_artifact_precedes_semantic_alias_mutation(self) -> None:
        job = self.publish_job("finalize-publish")
        manifests = job.index("Create and verify final multi-architecture manifests")
        validate = job.index("Validate summary and generate eligible candidate lock")
        upload = job.index("Upload publish artifacts")
        aliases = job.index("Update and verify semantic aliases")
        self.assertLess(manifests, validate)
        self.assertLess(validate, upload)
        self.assertLess(upload, aliases)
        self.assertIn('image["revision_ref"]', job)
        self.assertIn('image["semantic_ref"]', job)
        self.assertIn('image["revision_tag"]', job)
        self.assertIn('image["semantic_tag"]', job)
        self.assertIn('architecture["revision_arch_ref"]', job)
        alias_step = yaml_block(job, "      - name: Update and verify semantic aliases")
        self.assertIn('immutable_ref = f"{repository}@{manifest_digest}"', alias_step)
        self.assertRegex(
            alias_step,
            r'"imagetools",\s+"create",\s+"--tag",\s+semantic_ref,\s+immutable_ref',
        )
        self.assertIn('for alias_ref in alias_refs', alias_step)
        self.assertRegex(
            alias_step,
            r'"imagetools",\s+"create",\s+"--tag",\s+alias_ref,\s+immutable_ref',
        )
        self.assertIn('"imagetools", "inspect", "--raw", semantic_ref', alias_step)
        self.assertIn("if semantic_raw.stdout != revision_raw.stdout:", alias_step)

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
            '["docker", "buildx", "imagetools", "inspect", "--raw", revision_ref]',
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
            "      - name: Create and verify final multi-architecture manifests",
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

    def test_current_build_docs_and_workflow_hash_lock_the_docker_sdk(self) -> None:
        self.assertIn("config/build-engine-requirements.lock", self.build_readiness)
        self.assertIn("--require-hashes", self.build_readiness)
        self.assertIn("config/build-engine-requirements.lock", self.build_unit)
        self.assertIn("--require-hashes", self.build_unit)
        self.assertNotIn('"docker==7.1.0"', self.build_unit)

        # These historical documents are retained for context and are explicitly
        # superseded by the current schema-v4 plan.
        for document in (self.design_spec, self.implementation_plan):
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
        checkout = yaml_block(
            self.validate,
            "      - name: Check out repository",
        )
        self.assertIn("fetch-depth: 0", checkout)
        context_step = yaml_block(self.validate, "      - name: Resolve main history baseline")
        self.assertIn("EVENT_NAME: ${{ github.event_name }}", context_step)
        self.assertIn("BASE_REF: ${{ github.base_ref }}", context_step)
        self.assertIn(
            "BASE_SHA: ${{ github.event.pull_request.base.sha }}",
            context_step,
        )
        self.assertIn(
            "STACK_BASE_REF: ${{ github.event.pull_request.stack.base.ref }}",
            context_step,
        )
        self.assertIn(
            "STACK_BASE_SHA: ${{ github.event.pull_request.stack.base.sha }}",
            context_step,
        )
        self.assertIn("BEFORE_SHA: ${{ github.event.before }}", context_step)
        self.assertIn("REF_NAME: ${{ github.ref_name }}", context_step)
        self.assertIn("REF_TYPE: ${{ github.ref_type }}", context_step)
        self.assertIn('if [ "$EVENT_NAME" = "pull_request" ]', context_step)
        self.assertIn('effective_base_ref="${STACK_BASE_REF:-$BASE_REF}"', context_step)
        self.assertIn('effective_base_sha="${STACK_BASE_SHA:-$BASE_SHA}"', context_step)
        self.assertIn('if [ -z "$effective_base_sha" ] || [ "$effective_base_sha" = "0000000000000000000000000000000000000000" ]', context_step)
        self.assertIn("Pull request effective base SHA is required", context_step)
        self.assertIn('if [ "$effective_base_ref" != "main" ]', context_step)
        self.assertIn("Pull requests must target main.", context_step)
        self.assertIn('baseline_sha="$effective_base_sha"', context_step)
        self.assertIn('elif [ "$EVENT_NAME" = "push" ] && [ "$REF_TYPE" = "branch" ]', context_step)
        self.assertIn('if [ "$REF_NAME" = "main" ]', context_step)
        self.assertIn('baseline_sha="$BEFORE_SHA"', context_step)
        self.assertIn('if [ "$baseline_sha" = "0000000000000000000000000000000000000000" ]', context_step)
        self.assertIn("A recreated main branch has no trusted history baseline", context_step)
        self.assertIn("baseline=%s", context_step)
        history_gate = yaml_block(
            self.validate,
            "      - name: Validate append-only source-set history",
        )
        self.assertIn("scripts/validate-source-set-history.py", history_gate)
        self.assertIn(
            'steps.history-baseline.outputs.baseline',
            history_gate,
        )
        self.assertIn('--baseline="$BASELINE_SHA"', history_gate)
        self.assertNotIn("--branch", history_gate)
        metadata_checkout = yaml_block(
            self.validate,
            "      - name: Check out pinned OpenStack release metadata",
        )
        self.assertIn(
            "RELEASES_REPOSITORY: https://opendev.org/openstack/releases",
            metadata_checkout,
        )
        self.assertIn('matrix["release_metadata"]["commit"]', metadata_checkout)
        self.assertIn(
            'git -C "$CHECKOUT_PATH" fetch --no-tags --depth=1 origin '
            '"$RELEASES_COMMIT"',
            metadata_checkout,
        )
        repository_validation = yaml_block(
            self.validate,
            "      - name: Validate repository configuration",
        )
        self.assertIn(
            '--release-metadata-checkout "$RELEASE_METADATA_CHECKOUT"',
            repository_validation,
        )
        self.assertIn(
            'python3 scripts/validate-config.py "${validation_args[@]}"',
            self.validate,
        )
        dropdown = yaml_block(
            self.validate,
            "      - name: Verify publish stream dropdown",
        )
        self.assertIn("scripts/sync-publish-stream-options.py --check", dropdown)
        cli_contract = yaml_block(
            self.validate,
            "      - name: Validate every pinned Kolla CLI contract",
        )
        self.assertIn("scripts/validate-kolla-cli-contract.py", cli_contract)
        self.assertIn("config/build-engine-requirements.lock", cli_contract)
        self.assertIn("--require-hashes --only-binary=:all:", cli_contract)
        self.assertIn("--base-manifest tests/fixtures/oci-base-index.json", cli_contract)
        self.assertIn("--checkout-root \"$CONTRACT_CHECKOUT_ROOT\"", cli_contract)
        self.assertLess(
            self.validate.index("Validate repository configuration"),
            self.validate.index("Validate every pinned Kolla CLI contract"),
        )
        self.assertLess(
            self.validate.index("Validate every pinned Kolla CLI contract"),
            self.validate.index("Validate every active stream dry-run plan"),
        )
        self.assertIn("Validate every active stream dry-run plan", self.validate)
        self.assertIn('for stream in matrix["streams"]', self.validate)
        self.assertIn('if stream["publish_enabled"] is True', self.validate)
        self.assertIn('for profile in matrix["profiles"]', self.validate)
        self.assertIn('"--image",\n                      "keystone"', self.validate)
        self.assertNotIn("--stream 2025.1-rocky-9", self.validate)
        self.assertIn("python3 -m unittest discover -s tests -v", self.validate)

    def test_matrix_prs_receive_a_trusted_dropdown_stack_pr(self) -> None:
        workflow = self.sync_stream_options
        trigger = yaml_block(workflow, "on:")
        self.assertIn("pull_request_target:", trigger)
        self.assertIn("- main", trigger)
        self.assertIn("- config/build-matrix.json", trigger)
        for event_type in ("opened", "reopened", "synchronize"):
            self.assertIn(f"- {event_type}", trigger)
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: read$")
        self.assertIn(
            "sync-publish-stream-options-${{ github.event.pull_request.number }}",
            workflow,
        )
        self.assertIn("cancel-in-progress: true", workflow)
        job = yaml_block(workflow, "  synchronize:")
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            job,
        )
        self.assertIn("automation/sync-publish-stream-options/", job)
        trusted_checkout = yaml_block(job, "      - name: Check out trusted main tools")
        self.assertIn(expected_action_use("actions/checkout"), trusted_checkout)
        self.assertIn(
            "ref: ${{ github.event.pull_request.stack.base.sha || "
            "github.event.pull_request.base.sha }}",
            trusted_checkout,
        )
        self.assertIn("path: trusted", trusted_checkout)
        self.assertIn("persist-credentials: false", trusted_checkout)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.sha }}", job)
        token = yaml_block(job, "      - name: Create least-privilege catalog bot token")
        self.assertIn(expected_action_use("actions/create-github-app-token"), token)
        self.assertIn("PUBLISH_DROPDOWN_APP_CLIENT_ID", token)
        self.assertIn("PUBLISH_DROPDOWN_APP_PRIVATE_KEY", token)
        self.assertIn("permission-contents: write", token)
        self.assertIn("permission-pull-requests: write", token)
        create = yaml_block(
            job,
            "      - name: Create or refresh dropdown synchronization stack PR",
        )
        self.assertIn(
            'python3 "$TRUSTED_REPOSITORY/scripts/sync-publish-stack-pr.py"',
            create,
        )
        for argument in (
            '--repository "$GITHUB_REPOSITORY"',
            '--head-sha "$HEAD_SHA"',
            '--source-branch "$SOURCE_BRANCH"',
            '--pull-request-number "$PULL_REQUEST_NUMBER"',
            '--repository-dir "$TRUSTED_REPOSITORY"',
        ):
            self.assertIn(argument, create)
        self.assertNotIn("gh api", create)
        self.assertNotIn("gh pr", create)
        self.assertNotIn("git -C", create)


if __name__ == "__main__":
    unittest.main()
