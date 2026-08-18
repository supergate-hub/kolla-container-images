from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.profile_resolver import find_stream


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
PUBLISH_DOC = ROOT / "docs" / "publish.md"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
READINESS_DOC = ROOT / "docs" / "build-readiness.md"
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
BASE_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"
CONFIG_JSON_FILES = tuple(sorted((ROOT / "config").rglob("*.json")))
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
VALIDATE_LOCK_SCRIPT = "validate" + "-lock.py"
ENVIRONMENT_ARGUMENT = "--" + "environment"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"
ENVIRONMENT_STATE_PATTERN = re.compile(
    r"(?:environment[-_](?:lock|tag|pointer|promotion)|"
    r"(?:dev|stg|prod)[-_](?:lock|tag|pointer)|"
    r"(?:lock|tag|pointer)[-_](?:dev|stg|prod)|"
    r"promot(?:e|ion)[-_](?:candidate|state|pointer|target))",
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
    inputs = document.split("## Inputs", 1)[1].split("The scope mapping", 1)[0]
    return set(re.findall(r"(?m)^\| `([a-z_]+)` \|", inputs))


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
                self.assertIn(" ".join(token.casefold().split()), folded)

    def render_plan(self) -> dict:
        matrix = json.loads(read_text(MATRIX_PATH))
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--stream",
                matrix["streams"][0]["id"],
                "--profile",
                "deployment",
                "--base-manifest",
                str(BASE_FIXTURE),
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
            r"(?:environment|promotion|rollback|pointer)", re.IGNORECASE
        )
        for key, _value in walk_json(payload):
            self.assertNotRegex(str(key), forbidden_keys)
            self.assertNotRegex(str(key), ENVIRONMENT_STATE_PATTERN)
        for value in json_strings(payload):
            self.assertNotRegex(value, ENVIRONMENT_STATE_PATTERN)
            self.assertNotRegex(value, ENVIRONMENT_PATH_PATTERN)
            for pattern in SOURCE_BOUNDARY_PATTERNS:
                self.assertNotRegex(value, pattern)

    def test_readme_documents_schema_v4_aggregate_and_exact_streams(self) -> None:
        matrix = json.loads(read_text(MATRIX_PATH))
        readme = read_text(README)
        self.assertEqual(matrix["schema_version"], 4)
        expected_counts = {"2025.1": 5, "2025.2": 2, "2026.1": 2}
        self.assertEqual(
            len(matrix["streams"]),
            sum(expected_counts[release] for release in matrix["releases"]),
        )
        self.assertTrue(all(stream["publish_enabled"] for stream in matrix["streams"]))
        for raw_stream in matrix["streams"]:
            stream = find_stream(matrix, raw_stream["id"])
            rows = [
                line
                for line in readme.splitlines()
                if line.startswith("|") and f"`{stream['id']}`" in line
            ]
            self.assertEqual(len(rows), 1, stream["id"])
            self.assertIn(stream["kolla_version"], rows[0])
            self.assertIn(stream["kolla_ansible_version"], rows[0])
            self.assertIn(stream["base_tag"], rows[0])
        self.assert_tokens(
            readme,
            "`main` is the aggregate catalog",
            "cannot publish",
            "same workflows",
            "2025-1",
            "2025-2",
            "2026-1",
            "schema v4",
            "Rocky 10.3",
            "do not mutate",
        )

    def test_docs_define_source_set_and_base_freeze_contract(self) -> None:
        for path in (README, PUBLISH_DOC, READINESS_DOC):
            document = read_text(path)
            with self.subTest(path=path.relative_to(ROOT)):
                self.assert_tokens(
                    document,
                    "OpenStack source-set",
                    "build_commit",
                    "canonical digest",
                    "Kolla-Ansible",
                    "configured base tag",
                    "index digest",
                    "linux/amd64",
                    "linux/arm64",
                    "--nopull",
                    "DNF/APT repository snapshots",
                )
        matrix = json.loads(read_text(MATRIX_PATH))
        for base in matrix["bases"].values():
            self.assertEqual(set(base), {"distro", "os_version", "image", "tag"})

    def test_docs_define_semantic_revision_and_lock_v3_contract(self) -> None:
        semantic_ref = (
            "ghcr.io/supergate-hub/kolla-container-images/nova-compute:"
            "2025.1-rocky-10.2-20.5.0"
        )
        revision_ref = f"{semantic_ref}-rev-123456789-1"
        for path in (README, PUBLISH_DOC, READINESS_DOC):
            document = read_text(path)
            with self.subTest(path=path.relative_to(ROOT)):
                self.assert_tokens(
                    document,
                    "{release}-{distro}-{os_version}-{kolla_ansible_version}",
                    "semantic_ref",
                    "revision_ref",
                    semantic_ref,
                    revision_ref,
                    f"{revision_ref}-amd64",
                    f"{revision_ref}-arm64",
                    "schema_version: 3",
                    "manifest_digest",
                    "immutable_ref",
                    "openstack-infra-ops",
                    "before deployment",
                )
                self.assertNotRegex(
                    document,
                    r"(?m)^[a-z0-9_]+_image_full:.*@sha256:",
                )
                self.assertNotRegex(
                    document,
                    r"(?im)^\s*[a-z0-9_]+_image_full:.*-(?:amd64|arm64)"
                    r"(?:@sha256|[\"']?\s*$)",
                )
        self.assert_tokens(
            read_text(README),
            "Existing major/codename GHCR tags are not deleted",
            "no longer updated",
            "not aliases",
        )

    def test_publish_doc_has_exact_three_inputs_and_authorization_contract(self) -> None:
        document = read_text(PUBLISH_DOC)
        self.assertEqual(markdown_input_names(document), {"operation", "stream", "scope"})
        self.assert_tokens(
            document,
            "workflow_dispatch",
            "separate workflow run",
            "operation=plan",
            "operation=publish",
            "keystone -> core / keystone",
            "core -> core / all",
            "deployment -> deployment / all",
            "There is no typed approval phrase",
            "ghcr-publish",
            "required reviewers",
            "self-review allowed",
            "github.ref_protected",
            "packages: write",
        )
        self.assertNotIn("ALLOW_GHCR_", document)

    def test_sensitive_repository_paths_have_the_two_maintainers_as_owners(self) -> None:
        self.assertEqual(
            read_text(CODEOWNERS),
            "\n".join(
                (
                    "/.github/workflows/ @supergate-hsyun @supergate-jhbyun",
                    "/scripts/           @supergate-hsyun @supergate-jhbyun",
                    "/config/            @supergate-hsyun @supergate-jhbyun",
                    "",
                )
            ),
        )

    def test_publish_doc_records_artifacts_order_and_handoff(self) -> None:
        document = read_text(PUBLISH_DOC)
        self.assert_tokens(
            document,
            "publish-plan-<candidate-id>",
            "native-amd64-<candidate-id>",
            "native-arm64-<candidate-id>",
            "publish-<stream>-<candidate-id>",
            "artifacts/plan/publish-plan.json",
            "artifacts/publish-summary-<stream>.json",
            "artifacts/kolla-ansible-image-lock-<stream>.yml",
            "uploaded before semantic aliases move",
            "Only `deployment/all` may produce",
            "Re-run all jobs",
            "new candidate ID",
            "generic candidate lock",
            "environment-specific locks",
            "promotion",
            "deployment",
            "rollback",
        )

    def test_build_readiness_documents_native_evidence_contract(self) -> None:
        document = read_text(READINESS_DOC)
        self.assert_tokens(
            document,
            "ubuntu-24.04",
            "ubuntu-24.04-arm",
            "max-parallel: 4",
            "14 GB",
            "8 GiB",
            "2 GiB",
            "local Linux Docker",
            "detached",
            "source checkout",
            "does not install Kolla from PyPI",
            "dependency tiers 0, 1, and 2",
            "one anchored target",
            "--skip-existing",
            "forbidden",
            "--threads 1",
            "--push-threads 1",
            "/bin/true",
            "QEMU",
            "not readiness evidence",
            "environment-specific deployment-smoke evidence remains external",
        )
        self.assertNotIn("self-hosted", document)

    def test_publish_plan_has_only_generic_stream_lock_path(self) -> None:
        plan = self.render_plan()
        self.assertEqual(
            plan["kolla_ansible_lock_file"],
            f"artifacts/kolla-ansible-image-lock-{plan['stream']}.yml",
        )
        self.assertNotIn(ENVIRONMENT_LOCK_FIELD, plan)
        self.assert_json_has_no_environment_state(plan)

    def test_config_json_does_not_define_environment_state(self) -> None:
        self.assertTrue(CONFIG_JSON_FILES)
        for path in CONFIG_JSON_FILES:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assert_json_has_no_environment_state(json.loads(read_text(path)))

    def test_source_and_workflows_stop_at_generic_handoff(self) -> None:
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
        for example in forbidden_examples:
            self.assertTrue(
                any(
                    pattern.search(example)
                    for pattern in (*SOURCE_BOUNDARY_PATTERNS, ENVIRONMENT_PATH_PATTERN)
                ),
                example,
            )

    def test_workflow_generates_generic_lock_without_environment_validation(self) -> None:
        workflow = read_text(PUBLISH_WORKFLOW)
        self.assertIn("scripts/generate-lock.py", workflow)
        self.assertNotIn(f"scripts/{VALIDATE_LOCK_SCRIPT}", workflow)
        self.assertNotIn(ENVIRONMENT_ARGUMENT, workflow)

    def test_removed_environment_tools_are_absent(self) -> None:
        self.assertFalse((ROOT / "scripts" / VALIDATE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "scripts" / COMPARE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "locks").exists())


if __name__ == "__main__":
    unittest.main()
