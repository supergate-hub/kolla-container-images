from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PUBLISH_DOC = ROOT / "docs" / "publish.md"
READINESS_DOC = ROOT / "docs" / "build-readiness.md"
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
CONFIG_JSON_FILES = tuple(sorted((ROOT / "config").rglob("*.json")))
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
SOURCE_BOUNDARY_FILES = (
    *sorted(
        path
        for path in (ROOT / "scripts").rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh", ".bash", ".zsh"}
    ),
    *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
    *sorted((ROOT / ".github" / "workflows").glob("*.yaml")),
)
COMPARE_LOCK_SCRIPT = "compare" + "-locks.py"
ENVIRONMENT_ARGUMENT = "--" + "environment"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"
VALIDATE_LOCK_SCRIPT = "validate" + "-lock.py"
ENVIRONMENT_STATE_PATTERN = re.compile(
    r"(?:"
    r"environment[-_](?:lock|tag|pointer|promotion)|"
    r"(?:dev|stg|prod)[-_](?:lock|tag|pointer)|"
    r"(?:lock|tag|pointer)[-_](?:dev|stg|prod)|"
    r"promot(?:e|ion)[-_](?:candidate|state|pointer|target)"
    r")",
    re.IGNORECASE,
)
ENVIRONMENT_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:locks?|tags?|pointers?)[/\\]"
    r"(?:dev|stg|prod)(?:[/\\.]|$)|"
    r"(?<![A-Za-z0-9_-])(?:dev|stg|prod)[/\\]"
    r"(?:locks?|tags?|pointers?)(?:[/\\.]|$)|"
    r"(?<![A-Za-z0-9_-])(?:dev|stg|prod)[-_]"
    r"(?:lock|tag|pointer)(?:[/\\.]|$)|"
    r"(?<![A-Za-z0-9_-])(?:lock|tag|pointer)[-_]"
    r"(?:dev|stg|prod)(?:[/\\.]|$)",
    re.IGNORECASE,
)
SOURCE_BOUNDARY_PATTERNS = (
    re.compile(r"--environment(?:\s|=)", re.IGNORECASE),
    ENVIRONMENT_STATE_PATTERN,
    ENVIRONMENT_PATH_PATTERN,
    re.compile(
        r"\bkolla-ansible\b.{0,80}\b(?:deploy|reconfigure|upgrade|rollback)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bkolla[_-]ansible[_-](?:deploy|reconfigure|upgrade|rollback)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bansible-playbook\b", re.IGNORECASE),
    re.compile(
        r"\b(?:deploy|rollback)[-_](?:site|environment)\b|"
        r"\b(?:site|environment)[-_](?:deploy|rollback)\b",
        re.IGNORECASE,
    ),
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def markdown_input_names(document: str) -> set[str]:
    return set(re.findall(r"(?m)^\| `([a-z_]+)` \|", document))


def markdown_section(document: str, heading: str) -> str:
    marker = f"## {heading}"
    lines = document.splitlines()
    start = lines.index(marker) + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def markdown_rows_with_first_cell(document: str, first_cell: str) -> list[str]:
    rows = []
    for line in document.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if cells and cells[0] == first_cell:
            rows.append(line)
    return rows


def walk_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_strings(child)


class RepositoryBoundaryTest(unittest.TestCase):
    def assert_tokens(self, document: str, *tokens: str) -> None:
        folded = " ".join(document.casefold().split())
        for token in tokens:
            with self.subTest(token=token):
                normalized_token = " ".join(token.casefold().split())
                self.assertIn(normalized_token, folded)

    def render_plan(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--stream",
                "2025.1-rocky-9",
                "--profile",
                "deployment",
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_json_has_no_environment_state(self, payload) -> None:
        forbidden_keys = re.compile(
            r"(?:environment|promotion|rollback|pointer)",
            re.IGNORECASE,
        )
        for key, _value in walk_json(payload):
            with self.subTest(key=key):
                self.assertNotRegex(str(key), forbidden_keys)
                self.assertNotRegex(str(key), ENVIRONMENT_STATE_PATTERN)
        for value in json_strings(payload):
            with self.subTest(value=value):
                self.assertNotRegex(value, ENVIRONMENT_STATE_PATTERN)
                self.assertNotRegex(value, ENVIRONMENT_PATH_PATTERN)
                for pattern in SOURCE_BOUNDARY_PATTERNS:
                    self.assertNotRegex(value, pattern)

    def test_publish_plan_has_no_environment_lock_paths(self) -> None:
        rendered_plan = self.render_plan()

        self.assertNotIn(ENVIRONMENT_LOCK_FIELD, rendered_plan)

    def test_readme_documents_exact_streams_pins_roles_and_counts(self) -> None:
        matrix = json.loads(read_text(MATRIX_PATH))
        readme = read_text(README)
        streams = matrix["streams"]

        self.assertEqual(len(streams), 7)
        self.assertTrue(all(stream["publish_enabled"] is True for stream in streams))
        for stream in streams:
            with self.subTest(stream=stream["id"]):
                rows = [
                    line
                    for line in readme.splitlines()
                    if line.startswith("|") and f"`{stream['id']}`" in line
                ]
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertIn(stream["kolla_version"], row)
                self.assertIn(stream["kolla_ansible_version"], row)
                self.assertIn(stream["base_tag"], row)
                if stream["id"] == "2025.1-rocky-9":
                    self.assert_tokens(row, "standing", "Dev", "Stg", "Prod", "baseline")
                else:
                    self.assert_tokens(
                        row,
                        "build",
                        "manifest",
                        "digest",
                        "native-smoke",
                        "lock",
                        "compatibility",
                    )
                    self.assertNotIn("standing", row.casefold())

        self.assert_tokens(
            readme,
            "All seven streams",
            "publish-capable",
            "dry_run: true",
            "approval",
            "Ubuntu",
            "24.04",
            "noble",
        )
        self.assertRegex(
            readme,
            r"(?m)^\|\s*2025\.1\s*\|\s*63\s*\|\s*64\s*\|\s*$",
        )
        self.assertRegex(
            readme,
            r"(?m)^\|\s*2025\.2\s*\|\s*63\s*\|\s*64\s*\|\s*$",
        )
        self.assertRegex(
            readme,
            r"(?m)^\|\s*2026\.1\s*\|\s*65\s*\|\s*66\s*\|\s*$",
        )
        self.assertNotIn("52-image", readme)

    def test_readme_documents_topology_architecture_and_os_boundaries(self) -> None:
        readme = read_text(README)
        self.assert_tokens(
            readme,
            "bb00",
            "2-3",
            "bb01",
            "bb02",
            "shared Stg",
            "IDC",
            "current Dev and Stg",
            "AMD64",
            "native ARM64",
            "same OpenStack cluster",
            "OpenStack node OS",
            "Kolla container base",
            "Rocky 9 lab VM",
            "Ubuntu physical host",
            "management Kubernetes",
            "workload Kubernetes",
            "separate",
            "outside this repository",
        )
        self.assertRegex(
            readme,
            re.compile(
                r"compatibility.{0,300}(?:do not|does not|without).{0,80}standing cluster",
                re.IGNORECASE | re.DOTALL,
            ),
        )
        for label, pattern in (
            ("Dev", r"(?mi)^-\s+Dev\b[^\n]*\b2-3\b[^\n]*`?bb00`?"),
            ("Stg", r"(?mi)^-\s+[^\n]*\bStg\b[^\n]*`?bb01`?[^\n]*`?bb02`?"),
            ("Prod", r"(?mi)^-\s+[^\n]*\bProd\b[^\n]*\bIDC\b"),
        ):
            with self.subTest(topology=label):
                self.assertRegex(readme, re.compile(pattern))

    def test_readme_documents_backends_observability_and_evidence_ownership(self) -> None:
        readme = read_text(README)
        self.assert_tokens(
            readme,
            "Cinder",
            "Manila",
            "Octavia",
            "Dev",
            "LVM",
            "LIO",
            "Generic",
            "DHSS",
            "NFS",
            "Stg",
            "Prod",
            "external Ceph RBD",
            "CephFS NFS",
            "cephadm",
            "NFS-Ganesha",
            "compatibility smoke",
            "TGT",
            "Prometheus",
            "Grafana",
            "Fluentd",
            "OpenSearch",
            "OpenSearch Dashboards",
            "Octavia Amphora",
            "Manila Generic",
            "Glance",
            "architecture-compatible",
            "scheduling",
            "not Kolla container artifacts",
            "stream × architecture × build unit",
            "stream × architecture",
            "matching-OS",
            "deployment-smoke",
            "openstack-infra-ops",
        )
        backend_contracts = {
            "Dev": ("LVM", "LIO", "Manila Generic", "DHSS", "NFS"),
            "Stg and Prod": (
                "external Ceph RBD",
                "external CephFS NFS",
                "cephadm",
                "NFS-Ganesha",
            ),
            "Compatibility smoke": (
                "LVM",
                "LIO",
                "Rocky",
                "TGT",
                "Ubuntu",
                "Generic",
                "NFS",
            ),
        }
        for tier, tokens in backend_contracts.items():
            with self.subTest(backend=tier):
                rows = markdown_rows_with_first_cell(readme, tier)
                self.assertEqual(len(rows), 1)
                self.assert_tokens(rows[0], *tokens)

    def test_publish_doc_has_current_inputs_approval_and_job_contract(self) -> None:
        document = read_text(PUBLISH_DOC)
        self.assertEqual(
            markdown_input_names(document),
            {"stream", "profile", "image", "dry_run", "approval"},
        )
        self.assert_tokens(
            document,
            "workflow_dispatch",
            "separate workflow run",
            "Actions: write",
            "CI",
            "dry_run: true",
            "frozen publish plan",
            "core/keystone",
            "core/all",
            "deployment/all",
            "ALLOW_GHCR_PUBLISH",
            "ALLOW_GHCR_FULL_CORE_PUBLISH",
            "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
            "ghcr-publish",
            "required reviewers",
            "publish-plan",
            "authorize-publish",
            "build-parent-tier-0",
            "build-parent-tier-1",
            "build-parent-tier-2",
            "build-leaf-stage-0",
            "build-leaf-stage-1",
            "collect-native-evidence",
            "finalize-publish",
            "packages: write",
        )
        self.assertNotIn("workflow_call", document)
        self.assertNotIn("packages: write ceiling", document)
        for phrase in (
            "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
            "2025.1-rocky-9 core/keystone (1 image, amd64/arm64)",
            "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
            "2025.1-rocky-9 core/all (21 images, amd64/arm64)",
            "PUBLISH ghcr.io/supergate-hub/kolla-container-images "
            "2025.1-rocky-9 deployment/all (63 images, amd64/arm64)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, document)
        self.assertNotIn("I approve GHCR", document)
        self.assertNotIn("52 images", document)

    def test_publish_doc_records_exact_artifacts_and_generic_handoff(self) -> None:
        document = read_text(PUBLISH_DOC)
        self.assert_tokens(
            document,
            "publish-plan",
            "native-amd64",
            "native-arm64",
            "publish-<stream>",
            "artifacts/plan/publish-plan.json",
            "artifacts/publish-summary-<stream>.json",
            "artifacts/kolla-ansible-image-lock-<stream>.yml",
            "artifacts/manifests/",
            "multi-architecture manifest",
            "child digests",
            "generic candidate lock",
            "openstack-infra-ops",
            "environment-specific locks",
            "promotion",
            "deployment",
            "rollback",
        )
        self.assertRegex(
            document,
            re.compile(r"only\s+`?deployment/all`?\s+may produce", re.IGNORECASE),
        )

    def test_publish_doc_has_architecture_neutral_consumption_example(self) -> None:
        document = read_text(PUBLISH_DOC)
        for key, value in (
            ("docker_registry", "ghcr.io"),
            ("docker_namespace", "supergate-hub/kolla-container-images"),
            ("kolla_base_distro", "rocky"),
        ):
            with self.subTest(setting=key):
                self.assertRegex(
                    document,
                    rf"(?m)^{re.escape(key)}:\s*[\"']?{re.escape(value)}[\"']?\s*$",
                )
        for setting in (
            'docker_registry_insecure: "no"',
            'openstack_release: "2025.1"',
            'kolla_base_distro_version: "9"',
            'openstack_tag_suffix: ""',
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, document)
        self.assertRegex(
            document,
            re.compile(
                r'(?m)^nova_compute_image_full:\s*"ghcr\.io/supergate-hub/'
                r'kolla-container-images/nova-compute:2025\.1-rocky-9-'
                r'candidate-123456789-1"\s*$'
            ),
        )
        self.assertNotRegex(
            document,
            r"(?m)^[a-z0-9_]+_image_full:.*@sha256:",
        )
        self.assertNotRegex(
            document,
            r"(?im)^\s*[a-z0-9_]+_image_full:.*-(?:amd64|arm64)(?:@sha256|[\"']?\s*$)",
        )
        self.assert_tokens(
            document,
            "extra-vars",
            "multi-architecture manifest",
            "Docker",
            "Podman",
            "homogeneous",
            "mixed-architecture",
        )

    def test_candidate_lock_docs_define_dual_contract_and_external_verification(
        self,
    ) -> None:
        for path in (README, PUBLISH_DOC, READINESS_DOC):
            with self.subTest(path=path.relative_to(ROOT)):
                document = read_text(path)
                self.assert_tokens(
                    document,
                    "_kolla_candidate_lock",
                    "deploy_ref",
                    "manifest_digest",
                    "immutable_ref",
                    "openstack-infra-ops",
                    "before deployment",
                )
                self.assertNotRegex(
                    document,
                    r"(?m)^[a-z0-9_]+_image_full:.*@sha256:",
                )

    def test_operational_docs_define_candidate_proof_artifacts_and_recovery(
        self,
    ) -> None:
        for path in (PUBLISH_DOC, READINESS_DOC):
            with self.subTest(path=path.relative_to(ROOT)):
                document = read_text(path)
                self.assert_tokens(
                    document,
                    "candidate ID",
                    "ancestor chain",
                    "local Linux Docker",
                    "candidate artifact",
                    "stream alias",
                    "openstack-infra-ops",
                    "publish-plan-<candidate-id>",
                    "native-amd64-<candidate-id>",
                    "native-arm64-<candidate-id>",
                    "publish-<stream>-<candidate-id>",
                    "Re-run all jobs",
                    "new candidate ID",
                )

        readiness = read_text(READINESS_DOC)
        self.assert_tokens(
            readiness,
            "ubuntu-24.04",
            "ubuntu-24.04-arm",
            "kolla-build",
            "max-parallel: 4",
            "14 GB",
            "8 GiB",
            "2 GiB",
        )

    def test_publish_doc_lists_eight_manual_prerequisites_and_external_material(
        self,
    ) -> None:
        document = read_text(PUBLISH_DOC)
        heading = "## Manual GitHub and GHCR prerequisites"
        self.assertIn(heading, document)
        manual = markdown_section(document, heading.removeprefix("## "))
        self.assertEqual(
            [int(number) for number in re.findall(r"(?m)^(\d+)\. ", manual)],
            list(range(1, 9)),
        )
        self.assert_tokens(
            manual,
            "Public",
            "ubuntu-24.04",
            "ubuntu-24.04-arm",
            "standard",
            "larger",
            "ghcr-publish",
            "required reviewer",
            "branches",
            "tags",
            "ALLOW_GHCR_PUBLISH",
            "ALLOW_GHCR_FULL_CORE_PUBLISH",
            "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
            "packages: write",
            "Actions: write",
            "dispatch",
            "GitHub App",
            "no package-write permission",
            "--ref main",
            "github.ref_protected",
            "visibility",
            "link",
            "unauthenticated",
            "빈 Docker config",
            "retention",
            "vulnerability scanning",
            "cleanup",
            "approval phrase",
            "core/keystone",
            "8 GiB",
            "2 GiB",
        )
        self.assert_tokens(
            manual,
            "현재 GitHub 설정의 존재를 증명하지 않는다",
            "다시 조회",
            "실제 관측 결과",
        )
        self.assert_tokens(
            document,
            "registry credentials",
            "OpenStack credentials",
            "Ceph keys",
            "private CAs",
            "site-specific configuration",
            "never embedded in images or generated candidate locks",
        )

    def test_build_readiness_documents_native_runner_and_build_contract(self) -> None:
        document = read_text(READINESS_DOC)
        self.assert_tokens(
            document,
            "ubuntu-24.04",
            "ubuntu-24.04-arm",
            "max-parallel: 4",
            "14 GB",
            "8 GiB",
            "2 GiB",
            "Docker",
            "Buildx",
            "network access",
            "matrix-pinned Kolla",
            "--no-cache-dir",
            "dependency tiers 0, 1, and 2",
            "one anchored target",
            "per architecture",
            "--skip-existing",
            "--threads 1",
            "--push-threads 1",
        )
        self.assertNotIn("self-hosted", document)
        self.assertNotIn("150 GiB", document)
        self.assertNotIn("300 GB", document)

    def test_build_readiness_documents_native_and_multiarch_evidence_contract(self) -> None:
        document = read_text(READINESS_DOC)
        self.assert_tokens(
            document,
            "native-amd64",
            "native-arm64",
            "runner machine",
            "immutable reference",
            "immutable digest",
            "linux/amd64",
            "linux/arm64",
            "/bin/true",
            "QEMU",
            "not readiness evidence",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "exactly two",
            "child digests",
            "stream × architecture × leaf",
            "stream × architecture",
            "environment-specific deployment-smoke evidence remains external",
            "deployment/all",
            "unit-evidence",
            "leaf stage 0",
            "leaf stage 1",
            "JSON evidence only",
        )

    def test_publish_plan_has_only_generic_stream_lock_path(self) -> None:
        plan = self.render_plan()
        self.assertEqual(
            plan["kolla_ansible_lock_file"],
            "artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml",
        )
        self.assert_json_has_no_environment_state(plan)

    def test_config_json_does_not_define_environment_state(self) -> None:
        self.assertTrue(CONFIG_JSON_FILES)
        for path in CONFIG_JSON_FILES:
            with self.subTest(path=path.relative_to(ROOT)):
                payload = json.loads(read_text(path))
                self.assert_json_has_no_environment_state(payload)

    def test_source_and_workflows_do_not_promote_or_deploy_environments(self) -> None:
        for path in SOURCE_BOUNDARY_FILES:
            source = read_text(path)
            for pattern in SOURCE_BOUNDARY_PATTERNS:
                with self.subTest(path=path.relative_to(ROOT), pattern=pattern.pattern):
                    self.assertNotRegex(source, pattern)

    def test_boundary_patterns_cover_common_environment_variants(self) -> None:
        forbidden_examples = (
            "environment-lock",
            "artifacts/locks/dev/candidate.yml",
            'path = "locks/dev/candidate.yml"',
            "prod_pointer",
            "promote_candidate(plan)",
            '["kolla-ansible", "deploy"]',
            "kolla_ansible_rollback",
            "deploy_site",
        )
        patterns = (*SOURCE_BOUNDARY_PATTERNS, ENVIRONMENT_PATH_PATTERN)
        for example in forbidden_examples:
            with self.subTest(example=example):
                self.assertTrue(any(pattern.search(example) for pattern in patterns))

    def test_publish_workflow_generates_candidate_lock_without_environment_validation(
        self,
    ) -> None:
        self.assertTrue(PUBLISH_WORKFLOW.exists(), "publish workflow is missing")
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("scripts/generate-lock.py", workflow)
        self.assertNotIn(f"scripts/{VALIDATE_LOCK_SCRIPT}", workflow)
        self.assertNotIn(ENVIRONMENT_ARGUMENT, workflow)

    def test_removed_environment_tools_are_absent(self) -> None:
        self.assertFalse((ROOT / "scripts" / VALIDATE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "scripts" / COMPARE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "locks").exists())


if __name__ == "__main__":
    unittest.main()
