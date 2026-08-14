from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.frozen_sources import (
    _pbr_project_version,
    _verify_requirements_constraints,
    FrozenSourceError,
    checkout_exact_repository,
    load_build_engine_lock,
    materialize_frozen_configs,
    parse_deliverable_pin,
    prepare_project_archive,
    prepare_project_mirror,
    prepare_unit_source_archives,
    prepare_sources,
    validate_plan_source_pins,
    verify_build_engine_install,
    verify_exact_checkout,
    verify_installed_kolla,
    verify_materialized_configs,
    verify_project_mirror,
    verify_project_archive,
    verify_unit_source_archives,
)
from scripts.openstack_source_set import (
    render_frozen_configs,
    validate_source_set_document,
)
from scripts.profile_resolver import find_stream, load_matrix


ROOT = Path(__file__).resolve().parents[1]
BUILD_UNIT_WORKFLOW = ROOT / ".github" / "workflows" / "build-unit.yml"
BUILD_ENGINE_LOCK = ROOT / "config" / "build-engine-requirements.lock"


def source_plan(matrix: dict, stream_id: str) -> dict:
    stream = find_stream(matrix, stream_id)
    frozen = render_frozen_configs(stream["source_set"])
    return {
        "stream": stream["id"],
        "release": stream["release"],
        "release_series": stream["release_series"],
        "release_branch": stream["release_branch"],
        "release_metadata": copy.deepcopy(matrix["release_metadata"]),
        "kolla": copy.deepcopy(stream["toolchain"]["kolla"]),
        "kolla_ansible": copy.deepcopy(stream["toolchain"]["kolla_ansible"]),
        "kolla_version": stream["kolla_version"],
        "kolla_ansible_version": stream["kolla_ansible_version"],
        "openstack_sources": {
            "source_set": copy.deepcopy(stream["source_set"]),
            "canonical_digest": stream["source_set_sha256"],
            "kolla_build_config": {
                "sha256": frozen.config_sha256,
                "content": frozen.config_content,
            },
            "template_override": {
                "sha256": frozen.template_override_sha256,
                "content": frozen.template_override_content,
            },
        },
        "build": {
            "all_units": [
                {
                    "target": "keystone",
                    "ancestor_chain": ["base", "openstack-base", "keystone-base"],
                    "ancestors": [
                        {"image": "base"},
                        {"image": "openstack-base"},
                        {"image": "keystone-base"},
                    ],
                }
            ]
        },
    }


class FrozenSourceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_matrix()
        self.plan = source_plan(self.matrix, self.matrix["streams"][0]["id"])

    def test_plan_must_repeat_every_matrix_source_pin_exactly(self) -> None:
        contract = validate_plan_source_pins(self.matrix, self.plan)

        self.assertEqual(contract["release_metadata"], self.matrix["release_metadata"])
        self.assertEqual(contract["kolla"], self.plan["kolla"])
        self.assertEqual(contract["kolla_ansible"], self.plan["kolla_ansible"])
        self.assertEqual(
            contract["openstack_sources"], self.plan["openstack_sources"]
        )
        self.assertEqual(
            contract["build_images"],
            {"base", "openstack-base", "keystone-base", "keystone"},
        )

        mutations = (
            ("release_metadata", "commit", "a" * 40),
            ("kolla", "repository", "https://example.invalid/kolla"),
            ("kolla", "commit", "b" * 40),
            ("kolla_ansible", "version", "0.0.0"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                altered = copy.deepcopy(self.plan)
                altered[section][key] = value
                with self.assertRaisesRegex(
                    FrozenSourceError, "does not match the branch matrix pin"
                ):
                    validate_plan_source_pins(self.matrix, altered)

    def test_plan_rejects_source_set_and_rendered_config_mutation(self) -> None:
        mutations = []

        moving_ref = copy.deepcopy(self.plan)
        project = next(iter(moving_ref["openstack_sources"]["source_set"]["projects"]))
        moving_ref["openstack_sources"]["source_set"]["projects"][project][
            "build_commit"
        ] = "stable/2025.1"
        mutations.append(("moving source reference", moving_ref))

        canonical_digest = copy.deepcopy(self.plan)
        canonical_digest["openstack_sources"]["canonical_digest"] = (
            "sha256:" + "a" * 64
        )
        mutations.append(("canonical source-set digest", canonical_digest))

        config_content = copy.deepcopy(self.plan)
        config_content["openstack_sources"]["kolla_build_config"]["content"] += (
            "\n[mutated]\n"
        )
        mutations.append(("rendered config content", config_content))

        config_digest = copy.deepcopy(self.plan)
        config_digest["openstack_sources"]["kolla_build_config"]["sha256"] = (
            "sha256:" + "b" * 64
        )
        mutations.append(("rendered config digest", config_digest))

        template_content = copy.deepcopy(self.plan)
        template_content["openstack_sources"]["template_override"]["content"] += (
            "# mutation\n"
        )
        mutations.append(("template override content", template_content))

        for name, altered in mutations:
            with self.subTest(name=name), self.assertRaises(FrozenSourceError):
                validate_plan_source_pins(self.matrix, altered)

    def test_plan_rejects_malformed_or_incomplete_build_closure(self) -> None:
        mismatched = copy.deepcopy(self.plan)
        mismatched["build"]["all_units"][0]["ancestors"][1]["image"] = "nova-base"
        with self.assertRaisesRegex(FrozenSourceError, "ancestor"):
            validate_plan_source_pins(self.matrix, mismatched)

        empty = copy.deepcopy(self.plan)
        empty["build"]["all_units"] = []
        with self.assertRaisesRegex(FrozenSourceError, "build closure"):
            validate_plan_source_pins(self.matrix, empty)

    def test_plan_rejects_missing_extra_and_conflicting_pin_fields(self) -> None:
        missing = copy.deepcopy(self.plan)
        del missing["kolla"]["commit"]
        with self.assertRaisesRegex(FrozenSourceError, "keys must be exactly"):
            validate_plan_source_pins(self.matrix, missing)

        extra = copy.deepcopy(self.plan)
        extra["kolla_ansible"]["ref"] = "stable/2025.1"
        with self.assertRaisesRegex(FrozenSourceError, "keys must be exactly"):
            validate_plan_source_pins(self.matrix, extra)

        conflicting = copy.deepcopy(self.plan)
        conflicting["kolla_version"] = "0.0.0"
        with self.assertRaisesRegex(FrozenSourceError, "conflicts"):
            validate_plan_source_pins(self.matrix, conflicting)


class ReleaseMetadataParserTest(unittest.TestCase):
    def write_metadata(self, directory: Path, text: str) -> Path:
        path = directory / "kolla.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parser_returns_only_the_exact_version_project_hash_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_metadata(
                Path(temp_dir),
                """---
launchpad: kolla
releases:
  - version: 20.3.0
    projects:
      - repo: openstack/kolla
        hash: c3fa85b2e69e13ce07fb54bfb8752754bcc01121
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
branches:
  - name: stable/2025.1
""",
            )

            self.assertEqual(
                parse_deliverable_pin(
                    path,
                    expected_project="openstack/kolla",
                    expected_version="20.4.0",
                ),
                "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5",
            )

    def test_parser_fails_closed_on_ambiguous_or_malformed_metadata(self) -> None:
        cases = {
            "duplicate version": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
""",
            "additional project": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
      - repo: openstack/other
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
            "short hash": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab
""",
            "unsupported key": """---
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
    unknown: true
""",
        }
        for name, document in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                path = self.write_metadata(Path(temp_dir), document)
                with self.assertRaises(FrozenSourceError):
                    parse_deliverable_pin(
                        path,
                        expected_project="openstack/kolla",
                        expected_version="20.4.0",
                    )


class ExactCheckoutTest(unittest.TestCase):
    def git(self, repository: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def test_checkout_fetches_and_detaches_the_exact_full_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            origin = root / "origin"
            origin.mkdir()
            self.git(origin, "init", "--quiet")
            self.git(origin, "config", "user.name", "Frozen Source Test")
            self.git(origin, "config", "user.email", "frozen-source@example.invalid")
            tracked = origin / "tracked.txt"
            tracked.write_text("frozen\n", encoding="utf-8")
            self.git(origin, "add", "tracked.txt")
            self.git(origin, "commit", "--quiet", "-m", "frozen source")
            commit = self.git(origin, "rev-parse", "HEAD")
            checkout = root / "checkout"

            checkout_exact_repository(
                checkout, repository=str(origin), commit=commit
            )

            self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), commit)
            symbolic_ref = subprocess.run(
                ["git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(symbolic_ref.returncode, 1)
            verify_exact_checkout(checkout, repository=str(origin), commit=commit)
            (checkout / "tracked.txt").write_text("mutated\n", encoding="utf-8")
            with self.assertRaisesRegex(FrozenSourceError, "local changes"):
                verify_exact_checkout(checkout, repository=str(origin), commit=commit)

            self.git(checkout, "restore", "tracked.txt")
            (checkout / "untracked.py").write_text(
                "raise RuntimeError('not from the frozen commit')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FrozenSourceError, "local changes"):
                verify_exact_checkout(checkout, repository=str(origin), commit=commit)


class FrozenProjectMirrorTest(unittest.TestCase):
    def git(self, repository: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def test_mirror_fetches_no_remote_tags_and_exposes_only_frozen_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            origin = root / "origin"
            origin.mkdir()
            self.git(origin, "init", "--quiet")
            self.git(origin, "config", "user.name", "Frozen Source Test")
            self.git(
                origin,
                "config",
                "user.email",
                "frozen-source@example.invalid",
            )
            tracked = origin / "tracked.txt"
            tracked.write_text("release\n", encoding="utf-8")
            self.git(origin, "add", "tracked.txt")
            self.git(origin, "commit", "--quiet", "-m", "release")
            release_commit = self.git(origin, "rev-parse", "HEAD")
            self.git(origin, "tag", "1.0.0", release_commit)
            tracked.write_text("snapshot\n", encoding="utf-8")
            self.git(origin, "commit", "--quiet", "-am", "snapshot")
            build_commit = self.git(origin, "rev-parse", "HEAD")

            # This tag represents mutable upstream state that was not part of
            # the source-set and must never enter the build mirror.
            self.git(origin, "tag", "999.0.0", build_commit)
            mirror = root / "mirror.git"
            project = {
                "repository": str(origin),
                "build_commit": build_commit,
                "nearest_release": {
                    "version": "1.0.0",
                    "commit": release_commit,
                },
            }
            prepare_project_mirror(mirror, project)
            verify_project_mirror(mirror, project)

            refs = self.git(
                mirror, "for-each-ref", "--format=%(refname):%(objectname)"
            ).splitlines()
            self.assertEqual(
                refs,
                [
                    f"refs/heads/frozen:{build_commit}",
                    f"refs/tags/1.0.0:{release_commit}",
                ],
            )
            self.assertEqual(self.git(mirror, "remote"), "")
            self.assertFalse((mirror / "FETCH_HEAD").exists())

            self.git(origin, "tag", "1000.0.0", build_commit)
            verify_project_mirror(mirror, project)
            self.assertNotIn("1000.0.0", self.git(mirror, "show-ref"))

    def test_pbr_derivation_ignores_ambient_version_overrides_and_passes_pre_version(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="1.2.4.dev1\n", stderr=""
        )
        with mock.patch.dict(
            "scripts.frozen_sources.os.environ",
            {"PBR_VERSION": "999.0.0", "OSLO_PACKAGE_VERSION": "998.0.0"},
        ), mock.patch(
            "scripts.frozen_sources.subprocess.run", return_value=completed
        ) as run:
            version = _pbr_project_version(
                Path("/source"),
                "demo-service",
                "1.2.4",
                python_executable=Path("/verified/python"),
            )
        self.assertEqual(version, "1.2.4.dev1")
        self.assertNotIn("PBR_VERSION", run.call_args.kwargs["env"])
        self.assertNotIn("OSLO_PACKAGE_VERSION", run.call_args.kwargs["env"])
        self.assertIn("'1.2.4'", run.call_args.args[0][-1])

    def test_export_is_reproducible_git_free_and_preserves_pbr_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            origin = root / "origin"
            origin.mkdir()
            self.git(origin, "init", "--quiet")
            self.git(origin, "config", "user.name", "Frozen Source Test")
            self.git(
                origin,
                "config",
                "user.email",
                "frozen-source@example.invalid",
            )
            (origin / "setup.cfg").write_text(
                "[metadata]\nname = demo-service\n",
                encoding="utf-8",
            )
            executable = origin / "bin" / "service"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            self.git(origin, "add", ".")
            self.git(origin, "commit", "--quiet", "-m", "release")
            release_commit = self.git(origin, "rev-parse", "HEAD")
            self.git(origin, "tag", "1.2.3", release_commit)
            (origin / "README.rst").write_text("snapshot\n", encoding="utf-8")
            self.git(origin, "add", "README.rst")
            self.git(origin, "commit", "--quiet", "-m", "snapshot")
            build_commit = self.git(origin, "rev-parse", "HEAD")
            project = {
                "repository": str(origin),
                "build_commit": build_commit,
                "nearest_release": {
                    "version": "1.2.3",
                    "commit": release_commit,
                },
            }
            archive_root = f"demo-service-archive-{build_commit}"

            mirror_one = root / "mirror-one.git"
            mirror_two = root / "mirror-two.git"
            prepare_project_mirror(mirror_one, project)
            archive_one = root / "one.tar"
            with mock.patch(
                "scripts.frozen_sources._pbr_project_version",
                return_value="1.2.4.dev1",
            ) as derive_version:
                version_one = prepare_project_archive(
                    mirror_one,
                    archive_one,
                    project,
                    archive_root=archive_root,
                    python_executable=Path("/verified/python"),
                )

            # Remote tags added after the source-set snapshot cannot change
            # either PBR's version or the generated source archive bytes.
            self.git(origin, "tag", "999.0.0", build_commit)
            prepare_project_mirror(mirror_two, project)
            archive_two = root / "two.tar"
            with mock.patch(
                "scripts.frozen_sources._pbr_project_version",
                return_value="1.2.4.dev1",
            ) as derive_version_two:
                version_two = prepare_project_archive(
                    mirror_two,
                    archive_two,
                    project,
                    archive_root=archive_root,
                    python_executable=Path("/verified/python"),
                )

            self.assertEqual(version_one, "1.2.4.dev1")
            self.assertEqual(version_two, version_one)
            self.assertEqual(derive_version.call_count, 1)
            self.assertEqual(derive_version_two.call_count, 1)
            self.assertEqual(archive_one.read_bytes(), archive_two.read_bytes())
            with tarfile.open(archive_one, "r:") as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                self.assertFalse(any(".git" in name.split("/") for name in names))
                self.assertEqual(names, sorted(names))
                pkg_info = archive.extractfile(f"{archive_root}/PKG-INFO")
                self.assertIsNotNone(pkg_info)
                self.assertEqual(
                    pkg_info.read().decode("utf-8"),
                    "Metadata-Version: 2.1\n"
                    "Name: demo-service\n"
                    "Version: 1.2.4.dev1\n",
                )
                modes = {member.name: member.mode for member in members}
                self.assertEqual(modes[f"{archive_root}/bin/service"], 0o755)
                self.assertTrue(all(member.mtime == 0 for member in members))

            original_bytes = archive_one.read_bytes()
            for label, old, new in (
                ("tracked source", b"snapshot\n", b"tampered\n"),
                ("PKG-INFO", b"1.2.4.dev1", b"9.9.9.dev9"),
            ):
                with self.subTest(label=label), mock.patch(
                    "scripts.frozen_sources._pbr_project_version",
                    return_value="1.2.4.dev1",
                ):
                    tampered = original_bytes.replace(old, new, 1)
                    self.assertNotEqual(tampered, original_bytes)
                    archive_one.write_bytes(tampered)
                    with self.assertRaisesRegex(FrozenSourceError, "bytes"):
                        verify_project_archive(
                            archive_one,
                            project,
                            archive_root=archive_root,
                            mirror=mirror_one,
                            python_executable=Path("/verified/python"),
                        )
                    archive_one.write_bytes(original_bytes)

    def test_unit_archives_preserve_each_kolla_section_clone_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            origin = root / "origin"
            origin.mkdir()
            self.git(origin, "init", "--quiet")
            self.git(origin, "config", "user.name", "Frozen Source Test")
            self.git(
                origin,
                "config",
                "user.email",
                "frozen-source@example.invalid",
            )
            healthcheck = origin / "11.4" / "healthcheck.sh"
            healthcheck.parent.mkdir()
            healthcheck.write_text("#!/bin/sh\n", encoding="utf-8")
            healthcheck.chmod(0o755)
            self.git(origin, "add", ".")
            self.git(origin, "commit", "--quiet", "-m", "frozen MariaDB source")
            build_commit = self.git(origin, "rev-parse", "HEAD")
            sections = (
                "mariadb-server-additions-healthcheck",
                "mariadb-server-plugin-mariadb-docker",
            )
            project = {
                "repository": str(origin),
                "build_commit": build_commit,
                "nearest_release": None,
                "kolla_sections": list(sections),
            }
            contract = {
                "openstack_sources": {
                    "source_set": {
                        "projects": {"MariaDB/mariadb-docker": project}
                    }
                }
            }
            plan = {
                "build": {
                    "all_units": [
                        {
                            "id": "amd64-leaf-mariadb-server",
                            "target": "mariadb-server",
                            "ancestor_chain": [],
                        }
                    ]
                }
            }
            checkout_root = root / "checkouts"
            archive_dir = root / "artifacts" / "source-archives"

            prepare_unit_source_archives(
                checkout_root,
                contract,
                plan,
                unit_id="amd64-leaf-mariadb-server",
                source_archive_dir=archive_dir,
                python_executable=Path(sys.executable),
            )

            self.assertEqual(
                sorted(path.name for path in archive_dir.iterdir()),
                [f"{section}.tar" for section in sections],
            )
            self.assertEqual(
                [path.name for path in (checkout_root / "project-mirrors").iterdir()],
                ["MariaDB__mariadb-docker.git"],
            )
            for section in sections:
                with self.subTest(section=section), tarfile.open(
                    archive_dir / f"{section}.tar", "r:"
                ) as archive:
                    expected_root = f"{section}-archive-{build_commit}"
                    self.assertEqual(archive.getnames()[0], ".")
                    self.assertIn(
                        f"{expected_root}/11.4/healthcheck.sh",
                        archive.getnames(),
                    )

            def kolla_aggregate(
                source_archive: Path, output: Path, *, items_mtime: int
            ) -> None:
                items_path = output.with_suffix("")
                items_path.mkdir()
                os.utime(items_path, (items_mtime, items_mtime))
                with tarfile.open(source_archive, "r") as source_tar:
                    source_tar.extractall(path=items_path)

                def reset_userinfo(member: tarfile.TarInfo) -> tarfile.TarInfo:
                    member.uid = member.gid = 0
                    member.uname = member.gname = "root"
                    return member

                with tarfile.open(output, "w") as aggregate:
                    aggregate.add(
                        items_path,
                        arcname="plugins",
                        filter=reset_userinfo,
                    )

            plugin_archive = archive_dir / f"{sections[1]}.tar"
            aggregate_one = root / "plugins-one.tar"
            aggregate_two = root / "plugins-two.tar"
            kolla_aggregate(plugin_archive, aggregate_one, items_mtime=100)
            kolla_aggregate(plugin_archive, aggregate_two, items_mtime=200)
            self.assertEqual(aggregate_one.read_bytes(), aggregate_two.read_bytes())
            with tarfile.open(aggregate_one, "r:") as archive:
                plugins_root = archive.getmember("plugins")
                self.assertEqual(plugins_root.mtime, 0)
                self.assertEqual(plugins_root.mode, 0o755)


class KollaSourceClosureIntegrationTest(unittest.TestCase):
    def test_unit_archive_preparation_exports_only_selected_source_projects(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        plan["build"]["all_units"][0].update(
            {
                "id": "amd64-parent-keystone-base",
                "arch": "amd64",
                "kind": "parent",
                "target": "keystone-base",
            }
        )
        contract = validate_plan_source_pins(matrix, plan)
        source_set = contract["openstack_sources"]["source_set"]
        target = "keystone-base"
        expected_projects = {
            project_name
            for project_name, project in source_set["projects"].items()
            if any(
                section == target
                or section.startswith(f"{target}-plugin-")
                or section.startswith(f"{target}-additions-")
                for section in project["kolla_sections"]
            )
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "scripts.frozen_sources.prepare_project_mirror"
        ) as prepare_mirror, mock.patch(
            "scripts.frozen_sources.prepare_project_archive"
        ) as prepare_archive, mock.patch(
            "scripts.frozen_sources.verify_project_mirror"
        ), mock.patch(
            "scripts.frozen_sources.verify_project_archive"
        ):
            root = Path(temp_dir)
            prepare_unit_source_archives(
                root / "checkouts",
                contract,
                plan,
                unit_id="amd64-parent-keystone-base",
                source_archive_dir=root / "artifacts" / "source-archives",
                python_executable=Path("/verified/python"),
            )
            observed = {
                call.args[1]["repository"] for call in prepare_mirror.call_args_list
            }
            self.assertEqual(
                observed,
                {source_set["projects"][name]["repository"] for name in expected_projects},
            )
            expected_sections = {
                section
                for name in expected_projects
                for section in source_set["projects"][name]["kolla_sections"]
                if section == target
                or section.startswith(f"{target}-plugin-")
                or section.startswith(f"{target}-additions-")
            }
            self.assertTrue(expected_sections)
            self.assertEqual(prepare_archive.call_count, len(expected_sections))

    def test_prepare_checks_every_planned_target_and_ancestor_against_checkout(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        contract = validate_plan_source_pins(matrix, plan)
        fake_sources_content = (
            "SOURCES = "
            + repr(
                {
                    section: (
                        {
                            "type": "url",
                            "location": (
                                "$tarballs_base/"
                                + project
                                + "/source-${openstack_branch}.tar.gz"
                            ),
                        }
                        if project.startswith("openstack/")
                        else {
                            "type": "git",
                            "location": source["repository"],
                            "reference": source["track_ref"],
                        }
                    )
                    for project, source in contract["openstack_sources"][
                        "source_set"
                    ]["projects"].items()
                    for section in source["kolla_sections"]
                }
            )
            + "\n"
        )
        source_set = contract["openstack_sources"]["source_set"]
        source_set["kolla_source_inputs"][contract["kolla"]["version"]][
            "kolla"
        ]["sources_sha256"] = hashlib.sha256(
            fake_sources_content.encode("utf-8")
        ).hexdigest()
        contract["openstack_sources"]["canonical_digest"] = (
            validate_source_set_document(source_set).sha256
        )

        def fake_checkout(path: Path, *, repository: str, commit: str) -> None:
            del repository, commit
            path.mkdir()
            if path.name == "kolla":
                source_path = path / "kolla" / "common"
                source_path.mkdir(parents=True)
                source_path.joinpath("sources.py").write_text(
                    fake_sources_content, encoding="utf-8"
                )
                template_fixtures = {
                    "docker/kolla-toolbox/Dockerfile.j2": """
{% block kolla_toolbox_pip_conf %}{% endblock %}
{% block kolla_toolbox_upper_constraints %}
RUN {{ macros.upper_constraints_remove("openstacksdk") }} \\
    && python3 -m venv --system-site-packages {{ venv_path }}
{% endblock %}
""",
                    "docker/ovn/ovn-sb-db-relay/Dockerfile.j2": """
{% block ovn_sb_db_relay_ovn_ctl %}
RUN curl -o /usr/share/ovn/scripts/ovn-ctl https://example.invalid/ovn-ctl
{% endblock %}
""",
                    "docker/mariadb/mariadb-base/Dockerfile.j2": """
{% block mariadb_clustercheck_version %}
ARG mariadb_clustercheck_url=https://example.invalid/clustercheck
{% endblock %}
RUN curl -o /usr/bin/clustercheck ${mariadb_clustercheck_url}
{% block mariadb_base_footer %}{% endblock %}
""",
                }
                for relative_path, content in template_fixtures.items():
                    template = path / relative_path
                    template.parent.mkdir(parents=True, exist_ok=True)
                    template.write_text(content, encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "scripts.frozen_sources.checkout_exact_repository",
            side_effect=fake_checkout,
        ), mock.patch(
            "scripts.frozen_sources.verify_release_metadata"
        ), mock.patch(
            "scripts.frozen_sources.validate_source_set_release_metadata"
        ), mock.patch(
            "scripts.frozen_sources._verify_requirements_constraints"
        ):
            root = Path(temp_dir)
            build_config_dir = root / "artifacts" / "config"
            prepare_sources(
                root / "checkouts",
                contract,
                build_config_dir=build_config_dir,
            )
            self.assertEqual(
                (build_config_dir / "kolla-build.conf").read_text(encoding="utf-8"),
                contract["openstack_sources"]["kolla_build_config"]["content"],
            )
            self.assertEqual(
                (build_config_dir / "template-overrides.j2").read_text(
                    encoding="utf-8"
                ),
                contract["openstack_sources"]["template_override"]["content"],
            )

    def test_prepare_rejects_a_missing_selected_kolla_source_mapping(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        contract = validate_plan_source_pins(matrix, plan)

        def fake_checkout(path: Path, *, repository: str, commit: str) -> None:
            del repository, commit
            path.mkdir()
            if path.name == "kolla":
                source_path = path / "kolla" / "common"
                source_path.mkdir(parents=True)
                source_path.joinpath("sources.py").write_text(
                    "SOURCES = {'openstack-base': {"
                    "'type': 'url', "
                    "'location': '$tarballs_base/openstack/requirements/source.tar.gz'"
                    "}}\n",
                    encoding="utf-8",
                )

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "scripts.frozen_sources.checkout_exact_repository",
            side_effect=fake_checkout,
        ), mock.patch(
            "scripts.frozen_sources.verify_release_metadata"
        ), mock.patch(
            "scripts.frozen_sources.validate_source_set_release_metadata"
        ), mock.patch(
            "scripts.frozen_sources._verify_requirements_constraints"
        ):
            with self.assertRaisesRegex(FrozenSourceError, "Kolla source"):
                root = Path(temp_dir)
                prepare_sources(
                    root / "checkouts",
                    contract,
                    build_config_dir=root / "artifacts" / "config",
                )

    def test_materialized_config_mutation_fails_closed(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        contract = validate_plan_source_pins(matrix, plan)
        with tempfile.TemporaryDirectory() as temp_dir:
            build_config_dir = Path(temp_dir) / "config"
            materialize_frozen_configs(build_config_dir, contract)
            (build_config_dir / "kolla-build.conf").write_text(
                "[mutated]\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(FrozenSourceError, "content"):
                verify_materialized_configs(build_config_dir, contract)

    def test_materialization_refuses_to_replace_existing_wrong_content(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        contract = validate_plan_source_pins(matrix, plan)
        with tempfile.TemporaryDirectory() as temp_dir:
            build_config_dir = Path(temp_dir) / "config"
            build_config_dir.mkdir()
            (build_config_dir / "kolla-build.conf").write_text(
                "[stale]\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(FrozenSourceError, "refusing to replace"):
                materialize_frozen_configs(build_config_dir, contract)

    def test_requirements_checkout_proves_upper_constraints_bytes(self) -> None:
        matrix = load_matrix()
        plan = source_plan(matrix, matrix["streams"][0]["id"])
        contract = validate_plan_source_pins(matrix, plan)
        constraints = b"keystone===26.0.0\n"
        contract["openstack_sources"]["source_set"]["projects"][
            "openstack/requirements"
        ]["upper_constraints_sha256"] = hashlib.sha256(constraints).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir)
            (checkout / "upper-constraints.txt").write_bytes(constraints)
            _verify_requirements_constraints(checkout, contract)

            (checkout / "upper-constraints.txt").write_bytes(b"mutated\n")
            with self.assertRaisesRegex(FrozenSourceError, "digest"):
                _verify_requirements_constraints(checkout, contract)


class InstalledKollaProvenanceTest(unittest.TestCase):
    def test_install_requires_exact_version_local_source_and_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "kolla"
            source.mkdir()
            binary_dir = root / "venv" / "bin"
            binary_dir.mkdir(parents=True)
            python = binary_dir / "python"
            python.write_text("", encoding="utf-8")
            (binary_dir / "kolla-build").write_text("", encoding="utf-8")

            distribution = mock.Mock()
            distribution.version = "20.4.0"
            distribution.read_text.return_value = json.dumps(
                {"url": source.resolve().as_uri(), "dir_info": {}}
            )
            distribution.entry_points = [
                importlib.metadata.EntryPoint(
                    name="kolla-build",
                    value="kolla.cmd.build:main",
                    group="console_scripts",
                )
            ]
            with (
                mock.patch(
                    "scripts.frozen_sources.importlib.metadata.distribution",
                    return_value=distribution,
                ),
                mock.patch("scripts.frozen_sources.sys.executable", str(python)),
            ):
                verify_installed_kolla(source, "20.4.0")

                distribution.read_text.return_value = json.dumps(
                    {"url": (root / "wrong").resolve().as_uri(), "dir_info": {}}
                )
                with self.assertRaisesRegex(FrozenSourceError, "does not match"):
                    verify_installed_kolla(source, "20.4.0")


class BuildEngineLockTest(unittest.TestCase):
    @staticmethod
    def distribution(name: str, version: str) -> mock.Mock:
        distribution = mock.Mock()
        distribution.metadata = {"Name": name}
        distribution.version = version
        return distribution

    def test_repository_lock_is_complete_hashed_and_pins_direct_inputs(self) -> None:
        lock = load_build_engine_lock(BUILD_ENGINE_LOCK)
        provenance = BUILD_ENGINE_LOCK.read_text(encoding="utf-8")

        self.assertRegex(lock["sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertIn("Generated by uv 0.12.3", provenance)
        self.assertIn("Linux amd64/arm64", provenance)
        for commit in (
            "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5",
            "d1c4dd49b92e68509a413c33667bbe87cc3d3a9e",
            "436395ae3523ee925abac3338e63fc5822208744",
            "dcc077f50eafc5849c7de3fdb800353684fe1210",
        ):
            self.assertIn(commit, provenance)
        requirements = lock["requirements"]
        self.assertEqual(requirements["pip"]["version"], "25.3")
        self.assertEqual(requirements["docker"]["version"], "7.1.0")
        self.assertEqual(requirements["setuptools"]["version"], "81.0.0")
        for direct in (
            "gitpython",
            "jinja2",
            "oslo-config",
            "pbr",
        ):
            self.assertIn(direct, requirements)
        self.assertGreaterEqual(len(requirements), 20)
        for name, requirement in requirements.items():
            with self.subTest(name=name):
                self.assertRegex(requirement["version"], r"^[0-9][0-9A-Za-z.]*$")
                self.assertTrue(requirement["hashes"])
                for digest in requirement["hashes"]:
                    self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_lock_rejects_ranges_markers_urls_options_and_missing_hashes(self) -> None:
        invalid_documents = (
            "pip>=25.3 --hash=sha256:" + "a" * 64 + "\n",
            "pip==25.3; python_version >= '3.12' --hash=sha256:" + "a" * 64 + "\n",
            "pip @ https://example.invalid/pip.whl --hash=sha256:" + "a" * 64 + "\n",
            "--index-url https://example.invalid/simple\npip==25.3 --hash=sha256:"
            + "a" * 64
            + "\n",
            "pip==25.3\n",
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "build-engine.lock"
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(FrozenSourceError):
                        load_build_engine_lock(path)

    def test_installed_distribution_set_must_equal_lock_plus_local_kolla(self) -> None:
        lock = {
            "sha256": "sha256:" + "a" * 64,
            "requirements": {
                "docker": {"version": "7.1.0", "hashes": ("sha256:" + "b" * 64,)},
                "pip": {"version": "25.3", "hashes": ("sha256:" + "c" * 64,)},
            },
        }
        exact = [
            self.distribution("docker", "7.1.0"),
            self.distribution("pip", "25.3"),
            self.distribution("kolla", "20.4.0"),
        ]
        with mock.patch(
            "scripts.frozen_sources.importlib.metadata.distributions",
            return_value=exact,
        ):
            self.assertEqual(
                verify_build_engine_install(lock, kolla_version="20.4.0"),
                "sha256:" + "a" * 64,
            )

        for drifted in (
            exact[:-1],
            exact + [self.distribution("wheel", "1.0.0")],
            [
                self.distribution("docker", "7.0.0"),
                self.distribution("pip", "25.3"),
                self.distribution("kolla", "20.4.0"),
            ],
        ):
            with self.subTest(distributions=drifted), mock.patch(
                "scripts.frozen_sources.importlib.metadata.distributions",
                return_value=drifted,
            ):
                with self.assertRaises(FrozenSourceError):
                    verify_build_engine_install(lock, kolla_version="20.4.0")


class FrozenSourceWorkflowTest(unittest.TestCase):
    def test_build_unit_uses_only_the_verified_local_kolla_checkout(self) -> None:
        workflow = BUILD_UNIT_WORKFLOW.read_text(encoding="utf-8")

        prepare = "python3 scripts/frozen_sources.py prepare"
        install = '"$KOLLA_SOURCE_DIR"'
        verify = ".venv/bin/python scripts/frozen_sources.py verify-install"
        self.assertIn(prepare, workflow)
        self.assertIn("--matrix config/build-matrix.json", workflow)
        self.assertIn("--publish-plan artifacts/plan/publish-plan.json", workflow)
        self.assertIn("PBR_VERSION=\"$KOLLA_VERSION\"", workflow)
        self.assertIn("--no-deps", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("--only-binary=:all:", workflow)
        self.assertIn("-r config/build-engine-requirements.lock", workflow)
        self.assertIn(
            "--build-engine-lock config/build-engine-requirements.lock",
            workflow,
        )
        self.assertNotIn('-r "$KOLLA_SOURCE_DIR/requirements.txt"', workflow)
        self.assertNotIn("--upgrade pip", workflow)
        self.assertNotRegex(workflow, r"pip install[^\n]*(?:>=|~=|<)")
        self.assertIn(verify, workflow)
        self.assertNotIn("kolla==$KOLLA_VERSION", workflow)
        self.assertNotRegex(workflow, r"pip install[^\n]*[\"']?kolla==")
        self.assertLess(workflow.index(prepare), workflow.index(install))
        self.assertLess(workflow.index(install), workflow.index(verify))


if __name__ == "__main__":
    unittest.main()
