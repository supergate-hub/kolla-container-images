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

    def test_workflows_use_only_reviewed_action_commits(self) -> None:
        combined = self.publish + "\n" + self.validate
        raw_uses = re.findall(r"(?m)^\s*uses:\s+.+$", combined)
        matches = ACTION_RE.findall(combined)
        self.assertEqual(len(raw_uses), 17)
        self.assertEqual(len(matches), len(raw_uses))
        for repository, sha, release in matches:
            with self.subTest(repository=repository):
                self.assertIn(repository, EXPECTED_ACTIONS)
                self.assertEqual((sha, release), EXPECTED_ACTIONS[repository])

    def test_every_checkout_disables_persisted_credentials(self) -> None:
        checkout_header = (
            "uses: actions/checkout@"
            "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7"
        )
        self.assertEqual(self.publish.count(checkout_header), 4)
        self.assertEqual(self.validate.count(checkout_header), 1)
        for document, count in ((self.publish, 4), (self.validate, 1)):
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

    def test_plan_job_is_read_only_and_uploads_one_exact_plan(self) -> None:
        job = self.publish_job("publish-plan")
        self.assertNotIn("packages: write", job)
        self.assertNotIn("docker login", job)
        self.assertNotIn("GITHUB_TOKEN", job)
        self.assertLess(
            job.index("python3 scripts/validate-config.py"),
            job.index("python3 scripts/plan-publish.py"),
        )
        self.assertIn("mkdir -p artifacts/plan", job)
        self.assertIn(
            'python3 scripts/plan-publish.py "${plan_args[@]}" '
            "> artifacts/plan/publish-plan.json",
            job,
        )
        self.assertIn("STREAM: ${{ inputs.stream }}", job)
        self.assertIn("PROFILE: ${{ inputs.profile }}", job)
        self.assertIn("IMAGE: ${{ inputs.image }}", job)
        self.assertIn("plan_args=(", job)
        for argument in (
            '--stream "$STREAM"',
            '--profile "$PROFILE"',
            '--candidate-id "$CANDIDATE_ID"',
            "--dry-run",
        ):
            self.assertIn(argument, job)
        self.assertIn('if [ "$IMAGE" != "all" ]; then', job)
        self.assertIn('plan_args+=(--image "$IMAGE")', job)
        self.assertIn("name: publish-plan", job)
        self.assertIn("path: artifacts/plan/publish-plan.json", job)
        self.assertNotIn("artifacts/logs", job)
        self.assertNotIn("artifacts/manifests", job)
        upload = yaml_block(job, "      - name: Upload publish plan")
        self.assertIn(expected_action_use("actions/upload-artifact"), upload)
        self.assertNotIn("if:", upload)
        self.assertEqual(job.count(expected_action_use("actions/upload-artifact")), 1)

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
        expected = "CANDIDATE_ID: ${{ github.run_id }}-${{ github.run_attempt }}"
        self.assertEqual(self.publish.count(expected), 4)
        self.assertEqual(self.publish.count('--candidate-id "$CANDIDATE_ID"'), 1)
        self.assertEqual(
            self.publish.count('--expected-candidate-id "$CANDIDATE_ID"'),
            3,
        )
        dispatch = yaml_block(self.publish, "  workflow_dispatch:")
        self.assertNotIn("candidate_id:", dispatch)
        self.assertNotIn("workflow_call:", self.publish)

    def test_every_artifact_name_is_candidate_qualified(self) -> None:
        candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
        self.assertEqual(self.publish.count(f"name: publish-plan-{candidate}"), 4)
        self.assertEqual(
            self.publish.count(f"name: native-${{{{ matrix.arch }}}}-{candidate}"),
            1,
        )
        self.assertEqual(
            self.publish.count(
                f"name: native-diagnostics-${{{{ matrix.arch }}}}-{candidate}"
            ),
            1,
        )
        self.assertEqual(self.publish.count(f"name: native-amd64-{candidate}"), 1)
        self.assertEqual(self.publish.count(f"name: native-arm64-{candidate}"), 1)
        self.assertEqual(
            self.publish.count(
                f"name: publish-${{{{ inputs.stream }}}}-{candidate}"
            ),
            1,
        )
        for line in re.findall(
            r"(?m)^\s+name: (?:publish-plan|native-|publish-).+$",
            self.publish,
        ):
            self.assertIn(candidate, line)
        self.assertNotIn("overwrite:", self.publish)

    def test_mutating_jobs_use_exact_non_dry_run_gate(self) -> None:
        for name in ("authorize-publish", "build-native", "finalize-publish"):
            with self.subTest(job=name):
                job = self.publish_job(name)
                self.assertIn("if: ${{ !inputs.dry_run }}", job)

        self.assertEqual(self.publish.count("if: ${{ !inputs.dry_run }}"), 3)

    def test_authorization_is_bound_to_the_exact_plan(self) -> None:
        job = self.publish_job("authorize-publish")
        self.assertIn("needs: publish-plan", job)
        self.assertIn("environment: ghcr-publish", job)
        self.assertNotIn("packages: write", job)
        self.assertIn(expected_action_use("actions/checkout"), job)
        self.assertIn("name: publish-plan", job)
        self.assertIn("path: artifacts/plan", job)
        approval_validator = "python3 scripts/validate-publish-approval.py"
        candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
        self.assertIn(approval_validator, job)
        self.assertIn(candidate_binding, job)
        self.assertIn("ALLOW_GHCR_PUBLISH: ${{ vars.ALLOW_GHCR_PUBLISH }}", job)
        self.assertIn(
            "ALLOW_GHCR_FULL_CORE_PUBLISH: "
            "${{ vars.ALLOW_GHCR_FULL_CORE_PUBLISH }}",
            job,
        )
        self.assertIn(
            "ALLOW_GHCR_DEPLOYMENT_PUBLISH: "
            "${{ vars.ALLOW_GHCR_DEPLOYMENT_PUBLISH }}",
            job,
        )
        self.assertIn("APPROVAL: ${{ inputs.approval }}", job)
        self.assertEqual(job.count("vars."), 3)
        self.assertNotIn("secrets.", job)
        self.assertEqual(self.publish.count(approval_validator), 3)
        self.assertEqual(self.publish.count(candidate_binding), 3)

        for name in ("authorize-publish", "build-native", "finalize-publish"):
            with self.subTest(job=name):
                live_job = self.publish_job(name)
                self.assertIn(
                    "ALLOW_GHCR_PUBLISH: ${{ vars.ALLOW_GHCR_PUBLISH }}",
                    live_job,
                )
                self.assertIn(
                    "ALLOW_GHCR_FULL_CORE_PUBLISH: "
                    "${{ vars.ALLOW_GHCR_FULL_CORE_PUBLISH }}",
                    live_job,
                )
                self.assertIn(
                    "ALLOW_GHCR_DEPLOYMENT_PUBLISH: "
                    "${{ vars.ALLOW_GHCR_DEPLOYMENT_PUBLISH }}",
                    live_job,
                )
                self.assertIn("APPROVAL: ${{ inputs.approval }}", live_job)
                self.assertIn(approval_validator, live_job)
                self.assertIn(candidate_binding, live_job)

    def test_one_two_entry_native_matrix_uses_required_runner_labels(self) -> None:
        job = self.publish_job("build-native")
        self.assertIn("needs: authorize-publish", job)
        self.assertIn("max-parallel: 2", job)
        self.assertIn(
            'runs-on: [self-hosted, linux, "${{ matrix.runner_arch }}", '
            '"kolla-${{ \'build\' }}"]',
            job,
        )
        self.assertIn("- arch: amd64\n            runner_arch: x64\n            runner_machine: x86_64", job)
        self.assertIn("- arch: arm64\n            runner_arch: ARM64\n            runner_machine: aarch64", job)
        self.assertEqual(job.count("- arch:"), 2)
        self.assertNotIn("build-parents:", self.publish)
        self.assertNotIn("build-images:", self.publish)
        self.assertNotIn("setup-qemu", self.publish)
        self.assertNotIn("qemu", self.publish.lower())

    def test_native_build_revalidates_capacity_and_plan_pin_before_login(self) -> None:
        job = self.publish_job("build-native")
        self.assertIn("permissions:\n      contents: read\n      packages: write", job)
        self.assertIn(expected_action_use("actions/checkout"), job)
        self.assertIn("name: publish-plan", job)
        self.assertIn("path: artifacts/plan", job)
        approval_validator = "python3 scripts/validate-publish-approval.py"
        candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
        self.assertIn(approval_validator, job)
        self.assertIn(candidate_binding, job)
        self.assertIn("platform.machine()", job)
        self.assertIn('docker info --format \'{{.DockerRootDir}}\'', job)
        self.assertIn("df -Pk", job)
        self.assertIn("150 * 1024 * 1024", job)
        self.assertIn("300 GB", job)
        self.assertIn(expected_action_use("actions/setup-python"), job)
        self.assertIn("cache: pip", job)
        self.assertIn("cache-dependency-path: config/build-matrix.json", job)
        self.assertIn("python3 -m venv .venv", job)
        self.assertIn('KOLLA_VERSION="$(', job)
        self.assertIn('"kolla==$KOLLA_VERSION"', job)
        self.assertIn('stream["kolla_version"]', job)
        self.assertNotIn('KOLLA_VERSION: "20.4.0"', self.publish)
        self.assertNotRegex(self.publish, r"kolla==(?:20|21|22)\.")
        self.assertLess(job.index(approval_validator), job.index("docker login ghcr.io"))
        login = job.index("docker login ghcr.io")
        self.assertLess(job.index("platform.machine()"), login)
        self.assertLess(job.index("df -Pk"), login)
        self.assertLess(job.index('stream["kolla_version"]'), login)

    def test_native_build_preflights_pinned_docker_sdk_and_kolla_before_login(self) -> None:
        job = self.publish_job("build-native")
        install = yaml_block(
            job,
            "      - name: Install the frozen Kolla and Docker SDK versions in a virtual environment",
        )
        preflight = yaml_block(job, "      - name: Preflight isolated build environment")

        self.assertIn('"kolla==$KOLLA_VERSION" "docker==7.1.0"', install)
        self.assertIn("import docker", preflight)
        self.assertIn('docker.__version__ != "7.1.0"', preflight)
        self.assertIn(".venv/bin/kolla-build --version", preflight)
        self.assertLess(job.index(install), job.index(preflight))
        self.assertLess(job.index(preflight), job.index("docker login ghcr.io"))

    def test_native_docker_is_local_linux_and_native_before_login(self) -> None:
        job = self.publish_job("build-native")
        buildx = job.index("Set up Docker Buildx")
        daemon = job.index("Validate native local Docker daemon")
        storage = job.index("Check Docker storage capacity")
        login = job.index("docker login ghcr.io")
        self.assertLess(buildx, daemon)
        self.assertLess(daemon, storage)
        self.assertLess(storage, login)
        for token in (
            "{{.OSType}}",
            "{{.Architecture}}",
            "EXPECTED_DOCKER_ARCH",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "docker context inspect",
            "unix:///",
            'printf \'DOCKER_HOST=%s\\n\' "$endpoint" >> "$GITHUB_ENV"',
            'printf \'DOCKER_CONTEXT=\\n\' >> "$GITHUB_ENV"',
        ):
            self.assertIn(token, job)

    def test_current_kolla_summary_is_required_before_remote_inspection(self) -> None:
        job = self.publish_job("build-native")
        unlink = job.index("summary_path.unlink(missing_ok=True)")
        build = job.index("subprocess.run(command, check=True)")
        validate = job.index("scripts/validate-kolla-build-summary.py")
        remote = job.index("def inspect_remote_descriptor")
        self.assertLess(unlink, build)
        self.assertLess(build, validate)
        self.assertLess(validate, remote)
        self.assertIn('"--publish-plan", str(PLAN_PATH)', job)
        self.assertIn('"--arch", arch_name', job)

    def test_kolla_version_command_substitution_is_not_duplicated(self) -> None:
        job = self.publish_job("build-native")
        self.assertEqual(job.count('KOLLA_VERSION="$('), 1)

    def test_native_build_executes_structured_argv_and_records_digest_evidence(self) -> None:
        job = self.publish_job("build-native")
        self.assertIn('command = architecture["commands"]["kolla_build_push"]', job)
        self.assertIn("subprocess.run(command, check=True", job)
        self.assertNotIn("shell=True", job)
        self.assertRegex(
            job,
            r'"imagetools",\s+"inspect",\s+arch_ref,\s+"--format",\s+'
            r'"\{\{json \.Manifest\}\}"',
        )
        self.assertIn("if not isinstance(descriptor, dict)", job)
        self.assertIn('if "manifests" in descriptor:', job)
        self.assertIn('len(descriptor["manifests"]) != 1', job)
        self.assertIn('child_descriptor = descriptor["manifests"][0]', job)
        self.assertIn('DIGEST_RE.fullmatch(descriptor_digest)', job)
        self.assertIn("expected_repository", job)
        self.assertIn("expected_arch_ref", job)
        self.assertIn("descriptor_platform", job)
        self.assertIn('immutable_ref = f"{repository}@{descriptor_digest}"', job)
        self.assertIn('["docker", "pull", "--platform", platform, immutable_ref]', job)
        self.assertIn('"{{.Os}}/{{.Architecture}}"', job)
        self.assertRegex(
            job,
            r'"--entrypoint",\s+"/bin/true",\s+immutable_ref',
        )
        self.assertIn("expected_parent_names", job)
        self.assertIn("expected_image_names", job)
        self.assertIn('"schema_version": 1', job)
        self.assertIn('"runner_machine": runner_machine', job)
        self.assertIn('"parents": parent_evidence', job)
        self.assertIn('"images": image_evidence', job)
        self.assertIn("set(parent_evidence_by_name) != set(expected_parent_names)", job)
        self.assertIn("set(image_evidence_by_name) != set(expected_image_names)", job)
        self.assertIn("artifacts/arch/native-${{ matrix.arch }}.json", job)
        self.assertIn("name: native-${{ matrix.arch }}", job)

    def test_native_diagnostics_are_uploaded_without_changing_evidence_layout(self) -> None:
        job = self.publish_job("build-native")
        evidence = yaml_block(job, "      - name: Upload native evidence")
        diagnostics = yaml_block(job, "      - name: Upload native diagnostics")

        self.assertIn("name: native-${{ matrix.arch }}", evidence)
        self.assertIn("path: artifacts/arch/native-${{ matrix.arch }}.json", evidence)
        self.assertNotIn("artifacts/kolla-summary", evidence)
        self.assertNotIn("artifacts/kolla-logs", evidence)
        self.assertIn("if: ${{ always() }}", diagnostics)
        self.assertIn("name: native-diagnostics-${{ matrix.arch }}", diagnostics)
        self.assertIn("artifacts/kolla-summary/", diagnostics)
        self.assertIn("artifacts/kolla-logs/", diagnostics)
        self.assertIn("if-no-files-found: warn", diagnostics)

    def test_only_native_build_and_finalize_can_write_packages(self) -> None:
        self.assertEqual(self.publish.count("packages: write"), 2)
        self.assertIn("packages: write", self.publish_job("build-native"))
        self.assertIn("packages: write", self.publish_job("finalize-publish"))

    def test_package_jobs_use_fresh_ephemeral_docker_config_and_always_cleanup(self) -> None:
        for name, suffix in (
            ("build-native", "native"),
            ("finalize-publish", "finalize"),
        ):
            with self.subTest(job=name):
                job = self.publish_job(name)
                prepare = job.index("Prepare ephemeral Docker client state")
                buildx = job.index("Set up Docker Buildx")
                login = job.index("docker login ghcr.io")
                cleanup = job.index("Remove ephemeral Docker client state")
                self.assertLess(prepare, buildx)
                self.assertLess(buildx, login)
                self.assertLess(login, cleanup)
                step_items = [
                    line.strip()
                    for line in job.splitlines()
                    if len(line) - len(line.lstrip()) == 6
                    and (line.strip() == "-" or line.strip().startswith("- "))
                ]
                self.assertEqual(
                    step_items[-1],
                    "- name: Remove ephemeral Docker client state",
                )
                cleanup_block = yaml_block(
                    job,
                    "      - name: Remove ephemeral Docker client state",
                )
                self.assertIn(
                    'rm -f -- "$DOCKER_CONFIG/config.json"',
                    cleanup_block,
                )
                self.assertNotIn("rm -rf", cleanup_block)
                preceding_step = (
                    "Upload native diagnostics"
                    if name == "build-native"
                    else "Update convenience stream aliases"
                )
                self.assertLess(job.index(preceding_step), cleanup)
                for token in (
                    "RUNNER_TEMP",
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "DOCKER_CONFIG",
                    "install -d -m 0700",
                    "if: ${{ always() }}",
                    'rm -f -- "$DOCKER_CONFIG/config.json"',
                ):
                    self.assertIn(token, job)
                self.assertIn(suffix, job)
        self.assertEqual(
            self.publish.count("Prepare ephemeral Docker client state"),
            2,
        )
        self.assertEqual(
            self.publish.count("Remove ephemeral Docker client state"),
            2,
        )

    def test_finalize_downloads_exact_evidence_and_revalidates_before_login(self) -> None:
        job = self.publish_job("finalize-publish")
        self.assertIn("needs: build-native", job)
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

    def test_docs_record_diagnostics_artifacts_runner_minimum_and_arm_policy(self) -> None:
        for document in (self.publish_doc, self.build_readiness):
            with self.subTest(document=document[:40]):
                self.assertIn("native-diagnostics-amd64", document)
                self.assertIn("native-diagnostics-arm64", document)
                self.assertIn("2.327.1", document)

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
