from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import toolchain_image
from scripts.toolchain_image import ToolchainError
try:
    from test_publish_workflow import ACTION_RE, EXPECTED_ACTIONS, yaml_block
except ModuleNotFoundError:
    from tests.test_publish_workflow import ACTION_RE, EXPECTED_ACTIONS, yaml_block


ROOT = Path(__file__).resolve().parents[1]
BASE_FIXTURE = ROOT / "tests" / "fixtures" / "docker-base-index.json"
WORKFLOW = ROOT / ".github" / "workflows" / "toolchain.yml"
CHECK_WORKFLOW = ROOT / ".github" / "workflows" / "toolchain-check.yml"
DOCKERFILE = ROOT / "toolchain" / "Dockerfile"
AMD64 = "sha256:" + "1" * 64
ARM64 = "sha256:" + "2" * 64


def copy_tree() -> Path:
    """Copy the parts load_toolchain reads into a scratch repository."""
    scratch = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "config", scratch / "config")
    shutil.copytree(ROOT / "toolchain", scratch / "toolchain")
    return scratch


def edit_manifest(root: Path, version: str, change) -> None:
    path = root / "toolchain" / version / "toolchain.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def probe_for(toolchain: dict) -> dict:
    """A probe of an image that matches the pins exactly."""
    collections = {item["name"]: {"version": item["version"]} for item in toolchain["collections"]}
    kolla = toolchain["kolla_collection"]
    collections[kolla["name"]] = {"version": kolla["galaxy_version"]}
    client = toolchain["requirements"]["python-openstackclient"]["version"]
    return {
        "uid": 42400,
        "distributions": {entry["name"]: entry["version"] for entry in toolchain["requirements"].values()},
        "kolla_ansible_pbr": {"git_version": toolchain["kolla_ansible"]["commit"][:7], "is_release": True},
        "kolla_ansible_version": {"returncode": 0, "stdout": f"kolla-ansible {toolchain['toolchain']}\n", "stderr": ""},
        "openstack_version": {"returncode": 0, "stdout": f"openstack {client}\n", "stderr": ""},
        "collections": {
            "returncode": 0,
            "stdout": json.dumps({"/usr/share/ansible/collections/ansible_collections": collections}),
            "stderr": "",
        },
    }


class ToolchainPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.versions = toolchain_image.toolchain_versions()

    def test_every_toolchain_pin_set_is_valid_and_matches_the_matrix(self) -> None:
        self.assertEqual(self.versions, ["22.2.0"])
        matrix = json.loads((ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8"))
        for version in self.versions:
            with self.subTest(version=version):
                toolchain = toolchain_image.load_toolchain(version)
                self.assertEqual(
                    toolchain["kolla_ansible"]["commit"],
                    matrix["toolchains"][version]["kolla_ansible"]["commit"],
                )
                self.assertEqual(toolchain["requirements"]["kolla-ansible"]["version"], version)
                ansible_core = toolchain["requirements"]["ansible-core"]["version"]
                self.assertTrue(toolchain_image.satisfies(ansible_core, ">=2.19.1,<2.21"))
                self.assertNotIn("pip", toolchain["requirements"])

    def test_requirements_lock_requires_exact_hashed_pins(self) -> None:
        parsed = toolchain_image.parse_requirements_lock(
            "# header\nfoo==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\nBar_Baz==2.0 --hash=sha256:" + "b" * 64 + "\n"
        )
        self.assertEqual(set(parsed), {"foo", "bar-baz"})
        for text in (
            "foo==1.0\n",
            "foo>=1.0 --hash=sha256:" + "a" * 64 + "\n",
            "foo==1.0 --hash=md5:abc\n",
            "foo==1.0 --hash=sha256:" + "a" * 64 + "\nfoo==1.1 --hash=sha256:" + "b" * 64 + "\n",
            "-e git+https://example.invalid/foo\n",
            "foo==1.0 \\\n",
            "",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ToolchainError):
                    toolchain_image.parse_requirements_lock(text)

    def test_collection_constraints(self) -> None:
        self.assertTrue(toolchain_image.satisfies("8.7.1", "<9"))
        self.assertFalse(toolchain_image.satisfies("9.0.0", "<9"))
        self.assertTrue(toolchain_image.satisfies("1.1.5", ">=1.0.0"))
        self.assertTrue(toolchain_image.satisfies("2.20.10", ">=2.19,!=2.19.0,<2.21"))
        self.assertFalse(toolchain_image.satisfies("2.19.0", ">=2.19,!=2.19.0,<2.21"))
        with self.assertRaises(ToolchainError):
            toolchain_image.satisfies("1.0.0", "~=1.0")

    def test_tampered_pins_fail_closed(self) -> None:
        version = self.versions[0]
        cases = {
            "commit": lambda m: m["kolla_ansible"].update(commit="0" * 40),
            "release": lambda m: m.update(release="2025.1"),
            "rocky base": lambda m: m.update(base="rocky-10.2"),
            "constraints digest": lambda m: m["python_requirements"].update(upper_constraints_sha256="0" * 64),
            "collection outside constraint": lambda m: m["collections"][0].update(version="99.0.0"),
            "collection url": lambda m: m["collections"][0].update(url="https://example.invalid/x.tar.gz"),
            "collection hash": lambda m: m["collections"][0].update(sha256="A" * 64),
            "unsorted collections": lambda m: m["collections"].reverse(),
            "kolla collection commit": lambda m: m["kolla_collection"].update(commit="main"),
            "unknown key": lambda m: m.update(extra=True),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                root = copy_tree()
                try:
                    edit_manifest(root, version, change)
                    with self.assertRaises(ToolchainError):
                        toolchain_image.load_toolchain(version, root=root)
                finally:
                    shutil.rmtree(root)

    def test_lock_must_pin_the_toolchain_release(self) -> None:
        version = self.versions[0]
        root = copy_tree()
        try:
            lock = root / "toolchain" / version / "requirements.lock"
            lock.write_text(lock.read_text().replace(f"kolla-ansible=={version}", "kolla-ansible==1.0.0"))
            with self.assertRaises(ToolchainError):
                toolchain_image.load_toolchain(version, root=root)
        finally:
            shutil.rmtree(root)


class ToolchainPlanAndLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.version = toolchain_image.toolchain_versions()[0]
        self.plan = toolchain_image.render_plan(
            self.version, "123-1", base_manifest=BASE_FIXTURE.read_bytes()
        )

    def test_plan_freezes_native_bases_and_revision_tags(self) -> None:
        plan = self.plan
        self.assertEqual(plan["revision_tag"], f"{self.version}-ubuntu-24.04-rev-123-1")
        self.assertEqual(
            plan["repository"], "ghcr.io/supergate-hub/kolla-container-images/kolla-ansible"
        )
        self.assertEqual(set(plan["architectures"]), {"amd64", "arm64"})
        self.assertEqual(plan["architectures"]["arm64"]["runner"], "ubuntu-24.04-arm")
        for arch, entry in plan["architectures"].items():
            self.assertEqual(
                entry["base_ref"],
                "docker.io/library/ubuntu@" + plan["base"]["platforms"][arch]["digest"],
            )
            self.assertEqual(entry["revision_arch_ref"], f"{plan['revision_ref']}-{arch}")
        with self.assertRaises(ToolchainError):
            toolchain_image.render_plan(self.version, "latest", base_manifest=BASE_FIXTURE.read_bytes())

    def evidence(self) -> dict:
        records = {}
        for arch, digest in (("amd64", AMD64), ("arm64", ARM64)):
            records[arch] = toolchain_image.render_evidence(
                self.plan, arch, {"containerimage.digest": digest}
            )
        return records

    def raw_index(self, children: dict[str, str]) -> bytes:
        return json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": digest,
                        "size": 1000,
                        "platform": {"os": "linux", "architecture": arch},
                    }
                    for arch, digest in children.items()
                ],
            }
        ).encode()

    def test_lock_binds_the_published_index_to_native_evidence(self) -> None:
        raw = self.raw_index({"amd64": AMD64, "arm64": ARM64})
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        lock = toolchain_image.render_lock(self.plan, self.evidence(), raw, digest, "a" * 40)
        self.assertEqual(lock["kind"], "kolla-ansible-toolchain-lock")
        self.assertEqual(lock["image"]["immutable_ref"], f"{self.plan['repository']}@{digest}")
        self.assertEqual(lock["image"]["architectures"]["arm64"]["digest"], ARM64)
        self.assertEqual(lock["kolla_ansible"]["version"], self.version)
        self.assertIn("kolla-ansible", lock["python_requirements"]["packages"])
        self.assertEqual(lock["base"], self.plan["base"])

    def test_lock_rejects_mismatched_manifests_and_evidence(self) -> None:
        good = self.raw_index({"amd64": AMD64, "arm64": ARM64})
        good_digest = "sha256:" + hashlib.sha256(good).hexdigest()
        swapped = self.raw_index({"amd64": ARM64, "arm64": AMD64})
        single = self.raw_index({"amd64": AMD64})
        foreign = copy.deepcopy(self.evidence())
        foreign["arm64"]["candidate_id"] = "999-1"
        cases = [
            (swapped, "sha256:" + hashlib.sha256(swapped).hexdigest(), self.evidence(), "a" * 40),
            (single, "sha256:" + hashlib.sha256(single).hexdigest(), self.evidence(), "a" * 40),
            (good, "sha256:" + "0" * 64, self.evidence(), "a" * 40),
            (good, good_digest, foreign, "a" * 40),
            (good, good_digest, self.evidence(), "main"),
        ]
        for raw, digest, evidence, commit in cases:
            with self.assertRaises(ToolchainError):
                toolchain_image.render_lock(self.plan, evidence, raw, digest, commit)

    def test_smoke_accepts_only_an_exact_image(self) -> None:
        toolchain = toolchain_image.load_toolchain(self.version)
        self.assertEqual(toolchain_image.check_smoke(toolchain, probe_for(toolchain)), [])

        def collections_without_kolla(probe):
            listing = json.loads(probe["collections"]["stdout"])
            for entries in listing.values():
                entries.pop("openstack.kolla")
            probe["collections"]["stdout"] = json.dumps(listing)

        mutations = {
            "root": lambda p: p.update(uid=0),
            "extra package": lambda p: p["distributions"].update(pip="24.0"),
            "wrong package": lambda p: p["distributions"].update({"ansible-core": "2.19.0"}),
            "foreign commit": lambda p: p.update(kolla_ansible_pbr={"git_version": "deadbee"}),
            "cli version": lambda p: p["kolla_ansible_version"].update(stdout="kolla-ansible 1.0.0"),
            "missing collection": collections_without_kolla,
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                probe = probe_for(toolchain)
                mutate(probe)
                self.assertNotEqual(toolchain_image.check_smoke(toolchain, probe), [])


class ToolchainWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.check = CHECK_WORKFLOW.read_text(encoding="utf-8")

    def test_toolchain_choices_match_pinned_directories(self) -> None:
        block = yaml_block(self.workflow, "      toolchain:")
        options = re.findall(r"(?m)^          - ([0-9.]+)$", block)
        self.assertEqual(options, toolchain_image.toolchain_versions())

    def test_actions_are_pinned_to_reviewed_commits(self) -> None:
        for document in (self.workflow, self.check):
            uses = re.findall(r"(?m)^\s*uses:.*$", document)
            self.assertTrue(uses)
            for line in uses:
                match = ACTION_RE.match(line)
                self.assertIsNotNone(match, line)
                repository, sha, release = match.groups()
                self.assertEqual(EXPECTED_ACTIONS[repository], (sha, release))

    def test_publication_is_approved_main_only_and_narrowly_privileged(self) -> None:
        self.assertIn("permissions:\n  contents: read\n\njobs:", self.workflow)
        self.assertEqual(self.workflow.count("environment: ghcr-publish"), 1)
        self.assertIn("environment: ghcr-publish", yaml_block(self.workflow, "  authorize-toolchain:"))
        self.assertEqual(self.workflow.count("packages: write"), 2)
        for job in ("  build-native:", "  finalize-toolchain:"):
            block = yaml_block(self.workflow, job)
            self.assertIn("packages: write", block)
            self.assertIn("needs: [toolchain-plan", block)
            self.assertIn('[ "$CALLER_REF_PROTECTED" != "true" ]', block)
        self.assertIn('if [ "$CALLER_REF" != "refs/heads/main" ]; then', self.workflow)
        self.assertIn("needs: [toolchain-plan, authorize-toolchain]", self.workflow)
        self.assertEqual(self.workflow.count("Refusing to overwrite existing"), 2)

    def test_lock_artifact_outlives_build_evidence(self) -> None:
        handoff = yaml_block(self.workflow, "      - name: Upload toolchain lock")
        self.assertIn("retention-days: 90", handoff)
        self.assertEqual(self.workflow.count("retention-days: 90"), 1)
        self.assertEqual(self.workflow.count("retention-days: 7"), 2)

    def test_smoke_runs_before_evidence_is_recorded(self) -> None:
        step = yaml_block(self.workflow, "      - name: Smoke-check the pushed image")
        self.assertLess(step.index("toolchain_image.py smoke"), step.index("toolchain_image.py evidence"))

    def test_pull_request_check_builds_without_publishing(self) -> None:
        self.assertIn("permissions:\n  contents: read\n\njobs:", self.check)
        for forbidden in ("packages: write", "--push", "docker login", "secrets.", "environment:"):
            self.assertNotIn(forbidden, self.check)
        self.assertIn("toolchain_image.py smoke", self.check)
        self.assertIn("pull_request:", self.check)

    def test_dockerfile_installs_only_hashed_pins_and_runs_unprivileged(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        froms = re.findall(r"(?m)^FROM (\S+)", dockerfile)
        self.assertEqual(froms, ["${BASE_IMAGE}", "runtime", "runtime"])
        self.assertIn("--no-deps --only-binary=:all: --require-hashes -r", dockerfile)
        self.assertIn("pip uninstall --yes", dockerfile)
        self.assertIn("collection install --offline --no-deps", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER [1-9][0-9]*:[1-9][0-9]*$")
        for forbidden in ("latest", "curl ", "ADD ", "--trusted-host"):
            self.assertNotIn(forbidden, dockerfile)


if __name__ == "__main__":
    unittest.main()
