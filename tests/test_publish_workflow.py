from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
VALIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
ENVIRONMENT_ARGUMENT = "--" + "environment"
VALIDATE_LOCK_SCRIPT = "scripts/validate" + "-lock.py"


class PublishWorkflowTest(unittest.TestCase):
    def test_workflows_use_node24_action_majors(self) -> None:
        publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        validate = VALIDATE_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("actions/checkout@v7", publish)
        self.assertIn("actions/checkout@v7", validate)
        self.assertIn("actions/upload-artifact@v7", publish)
        self.assertIn("actions/download-artifact@v8", publish)
        self.assertIn("docker/setup-buildx-action@v4", publish)
        self.assertNotIn("actions/checkout@v4", publish + validate)
        self.assertNotIn("actions/upload-artifact@v4", publish)
        self.assertNotIn("actions/download-artifact@v4", publish)
        self.assertNotIn("docker/setup-buildx-action@v3", publish)

    def test_image_all_is_available_for_full_profile_runs(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("description: Image to publish, or all for a full profile", workflow)
        self.assertIn("          - deployment", workflow)
        self.assertIn("          - all", workflow)
        self.assertIn("if [ '${{ inputs.image }}' != 'all' ]; then", workflow)
        self.assertIn("plan_args+=(--image '${{ inputs.image }}')", workflow)

    def test_real_publish_uses_scope_specific_approval_gate(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: Validate publish approval", workflow)
        self.assertIn("ALLOW_GHCR_PUBLISH: ${{ vars.ALLOW_GHCR_PUBLISH }}", workflow)
        self.assertIn(
            "ALLOW_GHCR_FULL_CORE_PUBLISH: ${{ vars.ALLOW_GHCR_FULL_CORE_PUBLISH }}",
            workflow,
        )
        self.assertIn(
            "ALLOW_GHCR_DEPLOYMENT_PUBLISH: "
            "${{ vars.ALLOW_GHCR_DEPLOYMENT_PUBLISH }}",
            workflow,
        )
        self.assertIn("python3 scripts/validate-publish-approval.py", workflow)

    def test_real_publish_validates_summary_and_generates_candidate_lock(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: Validate publish summary", workflow)
        self.assertIn("scripts/validate-publish-summary.py", workflow)
        self.assertIn("--allow-partial", workflow)
        self.assertIn("if [ '${{ inputs.image }}' != 'all' ]; then", workflow)
        self.assertIn("name: Generate full-profile candidate lock", workflow)
        self.assertIn("scripts/generate-lock.py", workflow)
        self.assertNotIn(VALIDATE_LOCK_SCRIPT, workflow)
        self.assertNotIn(ENVIRONMENT_ARGUMENT, workflow)

    def test_real_publish_uses_parent_and_service_group_matrices(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("name: Render parent and service build matrices", workflow)
        self.assertIn('return "ubuntu-24.04-arm" if arch == "arm64" else "ubuntu-24.04"', workflow)
        self.assertIn("parent_matrix: ${{ steps.build-matrix.outputs.parent_matrix }}", workflow)
        self.assertIn("build_matrix: ${{ steps.build-matrix.outputs.build_matrix }}", workflow)
        self.assertIn("name: Build shared parents ${{ matrix.arch }}", workflow)
        self.assertIn("matrix: ${{ fromJson(needs.publish-plan.outputs.parent_matrix) }}", workflow)
        self.assertIn("name: Build ${{ matrix.group }} ${{ matrix.arch }}", workflow)
        self.assertIn("runs-on: ${{ matrix.runner }}", workflow)
        self.assertIn("matrix: ${{ fromJson(needs.publish-plan.outputs.build_matrix) }}", workflow)
        self.assertIn("max-parallel: 8", workflow)
        self.assertIn('for parent_ref in arch["parent_refs"]:', workflow)
        self.assertIn('["docker", "pull", "--platform", arch["platform"], parent_ref]', workflow)
        self.assertIn('f"parents-{arch_name}"', workflow)
        self.assertIn('f"{group_name}-{arch_name}"', workflow)
        self.assertNotIn("docker/setup-qemu-action", workflow)

    def test_real_publish_finalizes_manifest_after_architecture_builds(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("finalize-publish:", workflow)
        self.assertIn("name: Finalize manifests and candidate lock", workflow)
        self.assertIn("- build-images", workflow)
        self.assertIn("pattern: kolla-leaf-*-${{ inputs.release }}-${{ inputs.distro }}-${{ inputs.distro_version }}", workflow)
        self.assertIn("merge-multiple: true", workflow)
        self.assertIn('logs_dir / f"{image_name}-manifest-create.log"', workflow)
        self.assertIn('manifests_dir / f"{image_name}-publish-summary.json"', workflow)

    def test_publish_flow_serializes_tag_writers_and_bounds_jobs(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("concurrency:", workflow)
        self.assertIn(
            "group: kolla-publish-${{ inputs.release }}-${{ inputs.distro }}-"
            "${{ inputs.distro_version }}",
            workflow,
        )
        self.assertNotIn(
            "${{ inputs.distro_version }}-${{ inputs.profile }}",
            workflow,
        )
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 90", workflow)
        self.assertIn("timeout-minutes: 20", workflow)
        self.assertNotIn("  packages: write\n\nenv:", workflow)

    def test_real_publish_accepts_buildx_descriptor_metadata_digest(self) -> None:
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('manifest_metadata.get("containerimage.digest")', workflow)
        self.assertIn('manifest_metadata.get("containerimage.descriptor")', workflow)
        self.assertIn('manifest_digest = descriptor.get("digest")', workflow)


if __name__ == "__main__":
    unittest.main()
