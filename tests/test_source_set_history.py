from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-source-set-history.py"


def source_set(source_set_id: str, release: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": source_set_id,
        "release": release,
    }


class SourceSetHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        (self.repository / "config" / "openstack-sources").mkdir(parents=True)
        (self.repository / "config" / "profiles").mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "history-test@example.invalid"],
            cwd=self.repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Source-set history test"],
            cwd=self.repository,
            check=True,
        )

    def write_catalog(
        self,
        *,
        releases: list[str],
        streams: list[str],
        reviewed_streams: list[str],
        deployment_reviewed_streams: list[str] | None = None,
    ) -> None:
        matrix = {
            "releases": {
                release: {
                    "series": f"series-{release}",
                    "source_set": f"series-{release}-20260813-r1",
                }
                for release in releases
            },
            "profiles": ["core", "deployment"],
            "streams": [{"id": stream_id} for stream_id in streams],
        }
        (self.repository / "config" / "build-matrix.json").write_text(
            json.dumps(matrix, indent=2) + "\n",
            encoding="utf-8",
        )
        for profile_name, profile_streams in (
            ("core", reviewed_streams),
            (
                "deployment",
                deployment_reviewed_streams
                if deployment_reviewed_streams is not None
                else reviewed_streams,
            ),
        ):
            (self.repository / "config" / "profiles" / f"{profile_name}.json").write_text(
                json.dumps(
                    {
                        "name": profile_name,
                        "reviewed_streams": profile_streams,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    def write_source_set(
        self,
        source_set_id: str,
        release: str,
        *,
        suffix: str = "\n",
    ) -> Path:
        path = (
            self.repository
            / "config"
            / "openstack-sources"
            / f"{source_set_id}.json"
        )
        path.write_text(
            json.dumps(source_set(source_set_id, release), sort_keys=True) + suffix,
            encoding="utf-8",
        )
        return path

    def commit(self, message: str = "baseline") -> str:
        subprocess.run(["git", "add", "config"], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", message],
            cwd=self.repository,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def run_validator(
        self,
        *,
        baseline: str | None,
        branch: str = "2025-1",
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(VALIDATOR), "--branch", branch]
        if baseline is not None:
            command.append(f"--baseline={baseline}")
        return subprocess.run(
            command,
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=False,
        )

    def create_aggregate_baseline(self) -> tuple[str, list[str]]:
        all_streams = [
            "2025.1-rocky-10.2-20.4.0",
            "2025.1-ubuntu-24.04-20.4.0",
            "2025.2-rocky-10.2-21.1.0",
        ]
        self.write_catalog(
            releases=["2025.1", "2025.2"],
            streams=all_streams,
            reviewed_streams=all_streams,
        )
        self.write_source_set("epoxy-20260812-r1", "2025.1")
        self.write_source_set("epoxy-20260813-r2", "2025.1")
        self.write_source_set("flamingo-20260813-r1", "2025.2")
        return self.commit(), all_streams

    def project_2025_1(self, all_streams: list[str]) -> None:
        owned_streams = [
            stream_id for stream_id in all_streams if stream_id.startswith("2025.1-")
        ]
        self.write_catalog(
            releases=["2025.1"],
            streams=owned_streams,
            reviewed_streams=all_streams,
        )
        (self.repository / "config" / "openstack-sources" / "flamingo-20260813-r1.json").unlink()

    def test_release_projection_preserves_owned_history_and_may_remove_foreign_files(
        self,
    ) -> None:
        baseline, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)

        result = self.run_validator(baseline=baseline)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Source-set history validation passed", result.stdout)

    def test_owned_baseline_source_set_cannot_be_changed(self) -> None:
        baseline, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)
        self.write_source_set("epoxy-20260812-r1", "2025.1", suffix="\n\n")

        result = self.run_validator(baseline=baseline)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must remain byte-identical", result.stderr)
        self.assertIn("epoxy-20260812-r1.json", result.stderr)

    def test_owned_baseline_source_set_cannot_be_deleted(self) -> None:
        baseline, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)
        (self.repository / "config" / "openstack-sources" / "epoxy-20260812-r1.json").unlink()

        result = self.run_validator(baseline=baseline)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be deleted", result.stderr)
        self.assertIn("epoxy-20260812-r1.json", result.stderr)

    def test_new_owned_revision_is_allowed(self) -> None:
        baseline, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)
        self.write_source_set("epoxy-20260814-r3", "2025.1")

        result = self.run_validator(baseline=baseline)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_aggregate_catalog_protects_each_active_release_history(self) -> None:
        baseline, _ = self.create_aggregate_baseline()
        (self.repository / "config" / "openstack-sources" / "flamingo-20260813-r1.json").unlink()

        result = self.run_validator(baseline=baseline, branch="main")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be deleted", result.stderr)
        self.assertIn("flamingo-20260813-r1.json", result.stderr)

    def test_main_cannot_drop_an_entire_release_and_all_of_its_history(self) -> None:
        baseline, all_streams = self.create_aggregate_baseline()
        retained = [
            stream_id for stream_id in all_streams if stream_id.startswith("2025.1-")
        ]
        self.write_catalog(
            releases=["2025.1"],
            streams=retained,
            reviewed_streams=all_streams,
        )
        (
            self.repository
            / "config"
            / "openstack-sources"
            / "flamingo-20260813-r1.json"
        ).unlink()

        result = self.run_validator(baseline=baseline, branch="main")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be deleted", result.stderr)
        self.assertIn("flamingo-20260813-r1.json", result.stderr)

    def test_baseline_must_be_an_exact_commit_sha_not_a_ref_or_path_expression(self) -> None:
        _, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)
        for injected in (
            "HEAD",
            "HEAD:config/build-matrix.json",
            "--help",
            "a" * 40 + ":config/openstack-sources/epoxy.json",
            "../" + "a" * 40,
        ):
            with self.subTest(injected=injected):
                result = self.run_validator(baseline=injected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("baseline must be exactly 40 lowercase hex characters", result.stderr)

    def test_unknown_exact_commit_sha_is_rejected(self) -> None:
        _, all_streams = self.create_aggregate_baseline()
        self.project_2025_1(all_streams)

        result = self.run_validator(baseline="a" * 40)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline commit is not available", result.stderr)

    def test_no_baseline_is_explicitly_skipped_for_local_validation(self) -> None:
        all_streams = ["2025.1-rocky-10.2-20.4.0"]
        self.write_catalog(
            releases=["2025.1"],
            streams=all_streams,
            reviewed_streams=all_streams,
        )

        result = self.run_validator(baseline=None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("immutable Git history check skipped", result.stdout)
        self.assertIn("not supplied", result.stdout)

    def test_zero_push_baseline_is_rejected_instead_of_skipping_history(self) -> None:
        streams = ["2025.1-rocky-10.2-20.4.0"]
        self.write_catalog(
            releases=["2025.1"],
            streams=streams,
            reviewed_streams=streams,
        )
        self.write_source_set("epoxy-20260813-r1", "2025.1")
        self.commit("root commit")

        result = self.run_validator(baseline="0" * 40, branch="2025-1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("zero baseline must be resolved by CI", result.stderr)

    def test_release_matrix_must_equal_its_profile_reviewed_stream_subset(self) -> None:
        reviewed = [
            "2025.1-rocky-10.2-20.4.0",
            "2025.1-ubuntu-24.04-20.4.0",
            "2025.2-rocky-10.2-21.1.0",
        ]
        self.write_catalog(
            releases=["2025.1"],
            streams=[reviewed[0]],
            reviewed_streams=reviewed,
        )

        result = self.run_validator(baseline=None)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release-local stream projection must exactly equal", result.stderr)
        self.assertIn(reviewed[1], result.stderr)

    def test_all_shared_profiles_must_agree_on_reviewed_streams(self) -> None:
        reviewed = [
            "2025.1-rocky-10.2-20.4.0",
            "2025.1-ubuntu-24.04-20.4.0",
        ]
        self.write_catalog(
            releases=["2025.1"],
            streams=reviewed,
            reviewed_streams=reviewed,
            deployment_reviewed_streams=reviewed[:-1],
        )

        result = self.run_validator(baseline=None)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shared profiles must have identical reviewed_streams", result.stderr)

    def test_branch_and_baseline_tree_paths_are_fail_closed(self) -> None:
        all_streams = ["2025.1-rocky-10.2-20.4.0"]
        self.write_catalog(
            releases=["2025.1"],
            streams=all_streams,
            reviewed_streams=all_streams,
        )
        nested = self.repository / "config" / "openstack-sources" / "nested"
        nested.mkdir()
        (nested / "epoxy.json").write_text(
            json.dumps(source_set("epoxy", "2025.1")),
            encoding="utf-8",
        )
        baseline = self.commit()

        branch_result = self.run_validator(baseline=baseline, branch="2025-1;echo-pwned")
        path_result = self.run_validator(baseline=baseline)

        self.assertNotEqual(branch_result.returncode, 0)
        self.assertIn("branch must be main or an exact YYYY-N name", branch_result.stderr)
        self.assertNotEqual(path_result.returncode, 0)
        self.assertIn("unsafe source-set path in baseline", path_result.stderr)


if __name__ == "__main__":
    unittest.main()
