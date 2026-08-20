from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from scripts.openstack_source_set import (
    KollaSourceInput,
    OpenStackSourceSetError,
    freeze_kolla_sources,
    generate_source_set_document,
    load_source_set,
    render_frozen_configs,
    validate_frozen_source_contract,
    validate_source_set_release_metadata,
    write_new_source_set,
)
from scripts.profile_resolver import Matrix, find_stream


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SET_DIR = ROOT / "config" / "openstack-sources"


def valid_document() -> dict:
    return {
        "schema_version": 1,
        "id": "epoxy-20260813-r1",
        "release": "2025.1",
        "series": "epoxy",
        "policy": "stable-head-snapshot",
        "generated_at": "2026-08-13T00:00:00Z",
        "projects": {
            "openstack/nova": {
                "repository": "https://opendev.org/openstack/nova",
                "track_ref": "stable/2025.1",
                "build_commit": "1" * 40,
                "kolla_sections": ["nova-base"],
                "nearest_release": {
                    "version": "31.2.1",
                    "commit": "2" * 40,
                },
            },
            "openstack/requirements": {
                "repository": "https://opendev.org/openstack/requirements",
                "track_ref": "stable/2025.1",
                "build_commit": "3" * 40,
                "kolla_sections": ["openstack-base"],
                "nearest_release": None,
                "upper_constraints_sha256": "4" * 64,
            },
        },
    }


def valid_v2_document() -> dict:
    document = valid_document()
    document["schema_version"] = 2
    document["direct_artifacts"] = {
        "mariadb-clustercheck": {
            "repository": "https://src.fedoraproject.org/rpms/mariadb",
            "commit": "a8d966d60d33e0ffc35cb5271e1339d4ab63c004",
            "path": "f/clustercheck.sh",
            "url": (
                "https://src.fedoraproject.org/rpms/mariadb/raw/"
                "a8d966d60d33e0ffc35cb5271e1339d4ab63c004/f/clustercheck.sh"
            ),
            "sha256": (
                "4be47a46f99b714bc3681fdf11b09d242dae5e3eb81274b3040a73f9d7800d50"
            ),
            "kolla_sections": ["mariadb-base"],
        },
        "ovn-ctl": {
            "repository": "https://github.com/ovn-org/ovn",
            "commit": "20b9f0b9a771e07f15d2db270464965663d15f56",
            "path": "utilities/ovn-ctl",
            "url": (
                "https://raw.githubusercontent.com/ovn-org/ovn/"
                "20b9f0b9a771e07f15d2db270464965663d15f56/utilities/ovn-ctl"
            ),
            "sha256": (
                "9e4b6e9b8469248bd1a0099eb5b9fc599da14f695b09a3fccfd58a21b4ebf481"
            ),
            "kolla_sections": ["ovn-sb-db-relay"],
        },
    }
    return document


def valid_v3_document() -> dict:
    document = valid_v2_document()
    document["schema_version"] = 3
    closure = {
        project_name: {
            "repository": project["repository"],
            "track_ref": project["track_ref"],
            "kolla_sections": project["kolla_sections"],
        }
        for project_name, project in document["projects"].items()
    }
    closure_sha256 = hashlib.sha256(
        json.dumps(closure, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document["kolla_source_inputs"] = {
        "20.5.0": {
            "kolla": {
                "repository": "https://opendev.org/openstack/kolla",
                "commit": "5" * 40,
                "sources_sha256": "6" * 64,
                "closure_sha256": closure_sha256,
            },
            "kolla_ansible": {
                "repository": "https://opendev.org/openstack/kolla-ansible",
                "commit": "7" * 40,
            },
        }
    }
    return document


class SourceSetLoadingTest(unittest.TestCase):
    def test_v3_records_exact_kolla_inputs_used_to_derive_the_closure(self) -> None:
        document = valid_v3_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / f"{document['id']}.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            source_set = load_source_set(path)

        self.assertEqual(
            source_set.document["kolla_source_inputs"]["20.5.0"],
            {
                "kolla": {
                    "repository": "https://opendev.org/openstack/kolla",
                    "commit": "5" * 40,
                    "sources_sha256": "6" * 64,
                    "closure_sha256": document["kolla_source_inputs"][
                        "20.5.0"
                    ]["kolla"]["closure_sha256"],
                },
                "kolla_ansible": {
                    "repository": "https://opendev.org/openstack/kolla-ansible",
                    "commit": "7" * 40,
                },
            },
        )

    def test_v3_rejects_incomplete_or_unproven_toolchain_inputs(self) -> None:
        mutations = {
            "missing Kolla-Ansible pin": lambda value: value.pop(
                "kolla_ansible"
            ),
            "untrusted Kolla repository": lambda value: value["kolla"].__setitem__(
                "repository", "https://example.invalid/openstack/kolla"
            ),
            "malformed sources digest": lambda value: value["kolla"].__setitem__(
                "sources_sha256", "moving"
            ),
            "unproven closure digest": lambda value: value["kolla"].__setitem__(
                "closure_sha256", "f" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = json.loads(json.dumps(valid_v3_document()))
                mutate(document["kolla_source_inputs"]["20.5.0"])
                with self.assertRaises(OpenStackSourceSetError):
                    render_frozen_configs(document)

    def test_loads_strict_schema_and_returns_canonical_digest(self) -> None:
        document = valid_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / f"{document['id']}.json"
            path.write_text(json.dumps(document, indent=2), encoding="utf-8")

            source_set = load_source_set(
                path,
                expected_id="epoxy-20260813-r1",
                expected_release="2025.1",
                expected_series="epoxy",
            )

        canonical = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(source_set.document, document)
        self.assertEqual(source_set.canonical_json, canonical.decode("utf-8"))
        self.assertEqual(
            source_set.sha256, f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        )

    def test_rejects_moving_build_reference_and_malformed_contracts(self) -> None:
        cases = {
            "moving build ref": ("openstack/nova", "build_commit", "stable/2025.1"),
            "wrong track ref": ("openstack/nova", "track_ref", "master"),
            "short nearest release": (
                "openstack/nova",
                "nearest_release",
                {"version": "31.2.1", "commit": "abc123"},
            ),
            "missing constraints hash": (
                "openstack/requirements",
                "upper_constraints_sha256",
                None,
            ),
        }
        for name, (project, field, value) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                document = valid_document()
                if name == "missing constraints hash":
                    del document["projects"][project][field]
                else:
                    document["projects"][project][field] = value
                path = Path(temp_dir) / f"{document['id']}.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(OpenStackSourceSetError):
                    load_source_set(
                        path,
                        expected_id="epoxy-20260813-r1",
                        expected_release="2025.1",
                        expected_series="epoxy",
                    )

    def test_rejects_non_string_constraints_digest(self) -> None:
        document = valid_document()
        document["projects"]["openstack/requirements"][
            "upper_constraints_sha256"
        ] = int("4" * 64)

        with self.assertRaisesRegex(OpenStackSourceSetError, "SHA-256"):
            render_frozen_configs(document)

    def test_requirements_and_external_projects_have_no_nearest_release(self) -> None:
        document = valid_document()
        document["projects"]["openstack/requirements"]["nearest_release"] = {
            "version": "bogus",
            "commit": "0" * 40,
        }

        with self.assertRaisesRegex(OpenStackSourceSetError, "nearest_release"):
            render_frozen_configs(document)

    def test_repository_source_sets_are_complete_and_placeholder_free(self) -> None:
        catalog = {
            "epoxy-20260813-r1": ("2025.1", "epoxy", 36, 37),
            "flamingo-20260820-r1": ("2025.2", "flamingo", 36, 37),
            "gazpacho-20260820-r1": ("2026.1", "gazpacho", 35, 37),
        }
        matrix = json.loads(
            (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
        )
        expected = {
            release["source_set"]: catalog[release["source_set"]]
            for release in matrix["releases"].values()
        }
        present = {path.stem for path in SOURCE_SET_DIR.glob("*.json")}
        self.assertTrue(set(expected) <= present)
        owned_releases = set(matrix["releases"])
        for source_set_id in present:
            with self.subTest(source_set_id=source_set_id):
                source_set = load_source_set(
                    SOURCE_SET_DIR / f"{source_set_id}.json",
                    expected_id=source_set_id,
                )
                rendered = render_frozen_configs(source_set.document)
                self.assertIn(source_set.document["release"], owned_releases)
                self.assertEqual(
                    source_set.document["series"],
                    matrix["releases"][source_set.document["release"]]["series"],
                )
                expected_versions = {
                    stream["toolchain"]
                    for stream in matrix["streams"]
                    if stream["release"] == source_set.document["release"]
                }
                self.assertEqual(
                    set(source_set.document["kolla_source_inputs"]),
                    expected_versions,
                )
                for version in expected_versions:
                    recorded = source_set.document["kolla_source_inputs"][version]
                    configured = matrix["toolchains"][version]
                    self.assertEqual(
                        {
                            "repository": recorded["kolla"]["repository"],
                            "commit": recorded["kolla"]["commit"],
                        },
                        configured["kolla"],
                    )
                    self.assertEqual(
                        recorded["kolla_ansible"],
                        configured["kolla_ansible"],
                    )
                if source_set_id in expected:
                    release, series, projects, sections = expected[source_set_id]
                    self.assertEqual(source_set.document["release"], release)
                    self.assertEqual(source_set.document["series"], series)
                    self.assertEqual(len(source_set.document["projects"]), projects)
                    self.assertEqual(len(rendered.source_sections), sections)
                self.assertNotRegex(
                    source_set.canonical_json,
                    r"<(?:40-sha|sha256|official version|UTC timestamp)>",
                )
                self.assertNotIn("stable/", rendered.config_content)
                self.assertNotIn("master", rendered.config_content)
                self.assertNotIn("https://", rendered.config_content)


class SourceSetToolchainBindingTest(unittest.TestCase):
    def test_stream_rejects_a_toolchain_pin_not_recorded_by_the_source_set(self) -> None:
        document = valid_v3_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_sets_dir = Path(temp_dir)
            (source_sets_dir / f"{document['id']}.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            matrix = Matrix(
                {
                    "releases": {
                        "2025.1": {
                            "series": "epoxy",
                            "source_set": document["id"],
                        }
                    },
                    "toolchains": {
                        "20.5.0": {
                            "kolla": {
                                "repository": "https://opendev.org/openstack/kolla",
                                "commit": "8" * 40,
                            },
                            "kolla_ansible": {
                                "repository": (
                                    "https://opendev.org/openstack/kolla-ansible"
                                ),
                                "commit": "7" * 40,
                            },
                        }
                    },
                    "bases": {
                        "rocky-10.2": {
                            "distro": "rocky",
                            "os_version": "10.2",
                            "image": "quay.io/rockylinux/rockylinux",
                            "tag": "10.2",
                        }
                    },
                    "streams": [
                        {
                            "id": "2025.1-rocky-10.2-20.5.0",
                            "release": "2025.1",
                            "toolchain": "20.5.0",
                            "base": "rocky-10.2",
                            "publish_enabled": True,
                        }
                    ],
                },
                source_sets_dir=source_sets_dir,
            )

            with self.assertRaisesRegex(ValueError, "source-set.*toolchain pin"):
                find_stream(matrix, "2025.1-rocky-10.2-20.5.0")

            matrix["toolchains"]["20.5.0"]["kolla"]["commit"] = "5" * 40
            self.assertEqual(
                find_stream(matrix, "2025.1-rocky-10.2-20.5.0")[
                    "source_set_id"
                ],
                document["id"],
            )

            matrix["toolchains"]["20.6.0"] = matrix["toolchains"].pop("20.5.0")
            matrix["streams"][0]["toolchain"] = "20.6.0"
            with self.assertRaisesRegex(
                ValueError, "does not record toolchain pin.*new source-set revision"
            ):
                find_stream(matrix, "2025.1-rocky-10.2-20.5.0")


class KollaSourceFreezingTest(unittest.TestCase):
    def test_v3_freeze_requires_the_selected_toolchain_sources_digest(self) -> None:
        document = valid_v3_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sources.py"
            source_path.write_text(
                "SOURCES = "
                + repr(
                    {
                        "openstack-base": {
                            "type": "url",
                            "location": (
                                "$tarballs_base/openstack/requirements/"
                                "requirements.tar.gz"
                            ),
                        },
                        "nova-base": {
                            "type": "url",
                            "location": (
                                "$tarballs_base/openstack/nova/nova.tar.gz"
                            ),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                OpenStackSourceSetError, "selected Kolla sources digest"
            ):
                freeze_kolla_sources(
                    document,
                    source_path,
                    images={"openstack-base"},
                    toolchain_version="20.5.0",
                )

            document["kolla_source_inputs"]["20.5.0"]["kolla"][
                "sources_sha256"
            ] = hashlib.sha256(source_path.read_bytes()).hexdigest()
            frozen = freeze_kolla_sources(
                document,
                source_path,
                images={"openstack-base"},
                toolchain_version="20.5.0",
            )

        self.assertEqual(frozen.source_sections, ("nova-base", "openstack-base"))

    def test_schema_v2_renders_checksum_verified_direct_build_inputs(self) -> None:
        document = valid_v2_document()

        rendered = render_frozen_configs(document)

        template = rendered.template_override_content
        requirements = document["projects"]["openstack/requirements"]
        for expected in (
            "{% block kolla_toolbox_upper_constraints %}",
            requirements["upper_constraints_sha256"],
            "{% block ovn_sb_db_relay_ovn_ctl %}",
            document["direct_artifacts"]["ovn-ctl"]["url"],
            document["direct_artifacts"]["ovn-ctl"]["sha256"],
            "{% block mariadb_clustercheck_version %}",
            "{% block mariadb_base_footer %}",
            document["direct_artifacts"]["mariadb-clustercheck"]["url"],
            document["direct_artifacts"]["mariadb-clustercheck"]["sha256"],
        ):
            self.assertIn(expected, template)
        self.assertGreaterEqual(template.count("sha256sum -c -"), 3)

    def test_schema_v2_requires_the_pinned_kolla_template_override_seams(self) -> None:
        document = valid_v2_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "kolla" / "common" / "sources.py"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(
                "SOURCES = "
                + repr(
                    {
                        "openstack-base": {
                            "type": "url",
                            "location": "$tarballs_base/openstack/requirements/source.tar.gz",
                        },
                        "nova-base": {
                            "type": "url",
                            "location": "$tarballs_base/openstack/nova/source.tar.gz",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(OpenStackSourceSetError, "template"):
                freeze_kolla_sources(
                    document,
                    source_path,
                    images={
                        "mariadb-base",
                        "nova-base",
                        "openstack-base",
                        "ovn-sb-db-relay",
                    },
                )

    def test_ast_parsing_renders_deterministic_local_archives_and_constraints(self) -> None:
        document = valid_document()
        del document["projects"]["openstack/nova"]
        document["projects"]["openstack/neutron"] = {
            "repository": "https://opendev.org/openstack/neutron",
            "track_ref": "stable/2025.1",
            "build_commit": "5" * 40,
            "kolla_sections": [
                "neutron-base",
                "neutron-base-plugin-neutron",
            ],
            "nearest_release": None,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sources.py"
            source_path.write_text(
                """SOURCES = {
    'openstack-base': {
        'type': 'url',
        'location': ('$tarballs_base/openstack/requirements/'
                     'requirements-${openstack_branch}.tar.gz')},
    'neutron-base': {
        'type': 'url',
        'location': '$tarballs_base/openstack/neutron/neutron.tar.gz'},
    'neutron-base-plugin-neutron': {
        'type': 'url',
        'location': '$tarballs_base/openstack/neutron/neutron.tar.gz'},
}
""",
                encoding="utf-8",
            )

            frozen = freeze_kolla_sources(
                document,
                source_path,
                images={"openstack-base", "neutron-base"},
            )

        expected_config = """[neutron-base]
type = local
location = $locals_base/artifacts/source-archives/neutron-base.tar

[neutron-base-plugin-neutron]
type = local
location = $locals_base/artifacts/source-archives/neutron-base-plugin-neutron.tar

[openstack-base]
type = local
location = $locals_base/artifacts/source-archives/openstack-base.tar
"""
        self.assertEqual(frozen.config_content, expected_config)
        self.assertEqual(
            frozen.config_sha256,
            f"sha256:{hashlib.sha256(expected_config.encode()).hexdigest()}",
        )

        rendered_without_checkout = render_frozen_configs(document)
        self.assertEqual(rendered_without_checkout, frozen)
        self.assertEqual(
            frozen.template_override_content,
            """{% extends parent_template %}

{% block kolla_toolbox_pip_conf %}
ENV UPPER_CONSTRAINTS_FILE=https://releases.openstack.org/constraints/upper/3333333333333333333333333333333333333333
{% endblock %}
""",
        )
        self.assertEqual(
            frozen.source_sections,
            ("neutron-base", "neutron-base-plugin-neutron", "openstack-base"),
        )

    def test_kolla_toolbox_source_keeps_constraints_local(self) -> None:
        document = valid_document()
        del document["projects"]["openstack/nova"]
        document["projects"]["openstack/requirements"]["kolla_sections"] = [
            "kolla-toolbox",
            "openstack-base",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sources.py"
            source_path.write_text(
                """SOURCES = {
    'openstack-base': {
        'type': 'url',
        'location': '$tarballs_base/openstack/requirements/requirements.tar.gz'},
    'kolla-toolbox': {
        'type': 'url',
        'location': '$tarballs_base/openstack/requirements/requirements.tar.gz'},
}
""",
                encoding="utf-8",
            )

            frozen = freeze_kolla_sources(
                document,
                source_path,
                images={"openstack-base", "kolla-toolbox"},
            )

        self.assertIn("[kolla-toolbox]", frozen.config_content)
        self.assertEqual(frozen.template_override_content, "")
        self.assertEqual(
            frozen.template_override_sha256,
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_schema_v2_kolla_toolbox_source_hashes_local_constraints(self) -> None:
        document = valid_v2_document()
        del document["direct_artifacts"]["mariadb-clustercheck"]
        document["series"] = "gazpacho"
        document["release"] = "2026.1"
        for project in document["projects"].values():
            project["track_ref"] = "stable/2026.1"
        document["projects"]["openstack/requirements"]["kolla_sections"] = [
            "kolla-toolbox",
            "openstack-base",
        ]

        frozen = render_frozen_configs(document)

        template = frozen.template_override_content
        self.assertIn("ADD kolla-toolbox-archive /kolla-toolbox-source", template)
        self.assertIn("ln -s kolla-toolbox-source/* /requirements", template)
        self.assertIn(
            document["projects"]["openstack/requirements"][
                "upper_constraints_sha256"
            ],
            template,
        )
        self.assertNotIn("releases.openstack.org/constraints", template)
        self.assertNotIn('curl --fail --show-error --location', template.split(
            "{% block ovn_sb_db_relay_ovn_ctl %}", 1
        )[0])

    def test_parser_never_executes_kolla_source_module(self) -> None:
        document = valid_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sources.py"
            source_path.write_text(
                "SOURCES = __import__('os').environ.clear()\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(OpenStackSourceSetError, "literal"):
                freeze_kolla_sources(
                    document,
                    source_path,
                    images={"openstack-base"},
                )

    def test_external_archives_must_be_versioned_and_checksum_pinned(self) -> None:
        document = valid_document()
        del document["projects"]["openstack/nova"]
        valid_archive = {
            "type": "url",
            "version": "1.2.3",
            "location": "https://example.invalid/tool-${version}.tar.gz",
            "sha256": {"amd64": "6" * 64, "arm64": "7" * 64},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sources.py"
            source_path.write_text(
                "SOURCES = "
                + repr(
                    {
                        "openstack-base": {
                            "type": "url",
                            "location": "$tarballs_base/openstack/requirements/requirements.tar.gz",
                        },
                        "prometheus-server": valid_archive,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            frozen = freeze_kolla_sources(
                document,
                source_path,
                images={"openstack-base", "prometheus-server"},
            )
            self.assertEqual(
                frozen.source_sections,
                ("openstack-base",),
            )

            moving_archive = dict(valid_archive)
            del moving_archive["sha256"]
            source_path.write_text(
                "SOURCES = "
                + repr(
                    {
                        "openstack-base": {
                            "type": "url",
                            "location": "$tarballs_base/openstack/requirements/requirements.tar.gz",
                        },
                        "prometheus-server": moving_archive,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenStackSourceSetError, "checksum"):
                freeze_kolla_sources(
                    document,
                    source_path,
                    images={"openstack-base", "prometheus-server"},
                )


class FrozenSourceContractTest(unittest.TestCase):
    def test_contract_must_exactly_match_canonical_source_and_rendered_configs(self) -> None:
        document = valid_document()
        rendered = render_frozen_configs(document)
        contract = {
            "source_set": document,
            "canonical_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "kolla_build_config": {
                "sha256": rendered.config_sha256,
                "content": rendered.config_content,
            },
            "template_override": {
                "sha256": rendered.template_override_sha256,
                "content": rendered.template_override_content,
            },
        }

        self.assertEqual(validate_frozen_source_contract(contract), contract)

        for section, key in (
            ("", "canonical_digest"),
            ("kolla_build_config", "sha256"),
            ("kolla_build_config", "content"),
            ("template_override", "sha256"),
        ):
            with self.subTest(section=section, key=key):
                altered = json.loads(json.dumps(contract))
                target = altered if not section else altered[section]
                target[key] = "sha256:" + "f" * 64 if key.endswith("digest") or key == "sha256" else "tampered\n"
                with self.assertRaises(OpenStackSourceSetError):
                    validate_frozen_source_contract(altered)


class ReleaseMetadataProofTest(unittest.TestCase):
    def test_nearest_release_must_match_pinned_releases_checkout(self) -> None:
        document = valid_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir)
            deliverables = checkout / "deliverables" / "epoxy"
            deliverables.mkdir(parents=True)
            (deliverables / "nova.yaml").write_text(
                """---
releases:
  - version: 31.0.0
    projects:
      - repo: openstack/nova
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  - version: 31.2.1
    projects:
      - repo: openstack/nova
        hash: 2222222222222222222222222222222222222222
""",
                encoding="utf-8",
            )

            validate_source_set_release_metadata(document, checkout)

            document["projects"]["openstack/nova"]["nearest_release"][
                "commit"
            ] = "a" * 40
            with self.assertRaisesRegex(OpenStackSourceSetError, "nearest_release"):
                validate_source_set_release_metadata(document, checkout)


class SourceSetGenerationTest(unittest.TestCase):
    def test_generation_snapshots_profile_closure_and_proves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources_path = root / "sources.py"
            sources_path.write_text(
                """SOURCES = {
    'openstack-base': {
        'type': 'url',
        'location': '$tarballs_base/openstack/requirements/requirements.tar.gz'},
    'nova-base': {
        'type': 'url',
        'location': '$tarballs_base/openstack/nova/nova.tar.gz'},
}
""",
                encoding="utf-8",
            )
            profile_path = root / "deployment.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "build_groups": [
                            {
                                "parent": "nova-base",
                                "parents": ["openstack-base", "nova-base"],
                                "images": ["nova-api"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            releases = root / "releases" / "deliverables" / "epoxy"
            releases.mkdir(parents=True)
            (releases / "nova.yaml").write_text(
                """---
releases:
  - version: 31.0.0
    projects:
      - repo: openstack/nova
        hash: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
                encoding="utf-8",
            )
            commits = {
                (
                    "https://opendev.org/openstack/nova",
                    "stable/2025.1",
                ): "1" * 40,
                (
                    "https://opendev.org/openstack/requirements",
                    "stable/2025.1",
                ): "2" * 40,
                ("https://github.com/ovn-org/ovn", "main"): "5" * 40,
                (
                    "https://src.fedoraproject.org/rpms/mariadb",
                    "10.9",
                ): "6" * 40,
            }

            def resolve(repository: str, track_ref: str) -> str:
                return commits[(repository, track_ref)]

            artifact_bytes = {
                (
                    "https://raw.githubusercontent.com/ovn-org/ovn/"
                    + "5" * 40
                    + "/utilities/ovn-ctl"
                ): b"frozen ovn-ctl\n",
                (
                    "https://src.fedoraproject.org/rpms/mariadb/raw/"
                    + "6" * 40
                    + "/f/clustercheck.sh"
                ): b"frozen clustercheck\n",
            }
            expected_sources_sha256 = hashlib.sha256(
                sources_path.read_bytes()
            ).hexdigest()
            expected_closure_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "openstack/nova": {
                            "repository": "https://opendev.org/openstack/nova",
                            "track_ref": "stable/2025.1",
                            "kolla_sections": ["nova-base"],
                        },
                        "openstack/requirements": {
                            "repository": (
                                "https://opendev.org/openstack/requirements"
                            ),
                            "track_ref": "stable/2025.1",
                            "kolla_sections": ["openstack-base"],
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()

            document = generate_source_set_document(
                source_set_id="epoxy-20260813-r1",
                release="2025.1",
                series="epoxy",
                generated_at="2026-08-13T04:21:17Z",
                profile_path=profile_path,
                kolla_source_inputs=[
                    KollaSourceInput(
                        version="20.4.0",
                        kolla_repository="https://opendev.org/openstack/kolla",
                        kolla_commit="9" * 40,
                        kolla_ansible_repository=(
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        kolla_ansible_commit="a" * 40,
                        sources_path=sources_path,
                    ),
                    KollaSourceInput(
                        version="20.5.0",
                        kolla_repository="https://opendev.org/openstack/kolla",
                        kolla_commit="7" * 40,
                        kolla_ansible_repository=(
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        kolla_ansible_commit="8" * 40,
                        sources_path=sources_path,
                    )
                ],
                releases_checkout=root / "releases",
                resolve_git_ref=resolve,
                read_constraints=lambda commit: b"nova===31.0.0\n",
                read_artifact=lambda url: artifact_bytes[url],
            )
            validate_source_set_release_metadata(document, root / "releases")

            incompatible_sources_path = root / "incompatible-sources.py"
            incompatible_sources_path.write_text(
                sources_path.read_text(encoding="utf-8").replace(
                    "openstack/nova/nova.tar.gz",
                    "openstack/cinder/cinder.tar.gz",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                OpenStackSourceSetError,
                "do not share one deployment source contract",
            ):
                generate_source_set_document(
                    source_set_id="epoxy-20260813-r2",
                    release="2025.1",
                    series="epoxy",
                    generated_at="2026-08-13T04:21:17Z",
                    profile_path=profile_path,
                    kolla_source_inputs=[
                        KollaSourceInput(
                            version="20.4.0",
                            kolla_repository="https://opendev.org/openstack/kolla",
                            kolla_commit="9" * 40,
                            kolla_ansible_repository=(
                                "https://opendev.org/openstack/kolla-ansible"
                            ),
                            kolla_ansible_commit="a" * 40,
                            sources_path=sources_path,
                        ),
                        KollaSourceInput(
                            version="20.5.0",
                            kolla_repository="https://opendev.org/openstack/kolla",
                            kolla_commit="7" * 40,
                            kolla_ansible_repository=(
                                "https://opendev.org/openstack/kolla-ansible"
                            ),
                            kolla_ansible_commit="8" * 40,
                            sources_path=incompatible_sources_path,
                        ),
                    ],
                    releases_checkout=root / "releases",
                    resolve_git_ref=resolve,
                    read_constraints=lambda commit: b"nova===31.0.0\n",
                    read_artifact=lambda url: artifact_bytes[url],
                )

        self.assertEqual(document["schema_version"], 3)
        self.assertEqual(
            document["kolla_source_inputs"],
            {
                "20.4.0": {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": "9" * 40,
                        "sources_sha256": expected_sources_sha256,
                        "closure_sha256": expected_closure_sha256,
                    },
                    "kolla_ansible": {
                        "repository": (
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        "commit": "a" * 40,
                    },
                },
                "20.5.0": {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": "7" * 40,
                        "sources_sha256": expected_sources_sha256,
                        "closure_sha256": expected_closure_sha256,
                    },
                    "kolla_ansible": {
                        "repository": (
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        "commit": "8" * 40,
                    },
                }
            },
        )
        self.assertEqual(
            set(document["direct_artifacts"]),
            {"mariadb-clustercheck", "ovn-ctl"},
        )
        self.assertEqual(
            document["direct_artifacts"]["ovn-ctl"]["commit"],
            "5" * 40,
        )
        self.assertEqual(
            document["direct_artifacts"]["ovn-ctl"]["sha256"],
            hashlib.sha256(b"frozen ovn-ctl\n").hexdigest(),
        )
        self.assertEqual(
            document["direct_artifacts"]["mariadb-clustercheck"]["commit"],
            "6" * 40,
        )
        self.assertEqual(
            document["direct_artifacts"]["mariadb-clustercheck"]["sha256"],
            hashlib.sha256(b"frozen clustercheck\n").hexdigest(),
        )
        self.assertEqual(list(document["projects"]), [
            "openstack/nova",
            "openstack/requirements",
        ])
        self.assertEqual(
            document["projects"]["openstack/nova"]["kolla_sections"],
            ["nova-base"],
        )
        self.assertEqual(
            document["projects"]["openstack/requirements"][
                "upper_constraints_sha256"
            ],
            hashlib.sha256(b"nova===31.0.0\n").hexdigest(),
        )

    def test_generator_writes_new_revision_once_and_never_overwrites(self) -> None:
        document = valid_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "epoxy-20260813-r1.json"
            write_new_source_set(output, document)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")), document
            )
            with self.assertRaisesRegex(OpenStackSourceSetError, "already exists"):
                write_new_source_set(output, document)

    def test_generator_write_failure_does_not_poison_the_immutable_path(self) -> None:
        document = valid_document()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "epoxy-20260813-r1.json"

            def fail_after_partial_write(value: object, file_obj: object, **_: object) -> None:
                del value
                file_obj.write("{")
                raise OSError("simulated disk full")

            with mock.patch(
                "scripts.openstack_source_set.json.dump",
                side_effect=fail_after_partial_write,
            ), self.assertRaisesRegex(OpenStackSourceSetError, "cannot write"):
                write_new_source_set(output, document)

            self.assertFalse(output.exists())
            write_new_source_set(output, document)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                document,
            )


@unittest.skipUnless(
    os.environ.get("KOLLA_SOURCE_SMOKE") == "1",
    "set KOLLA_SOURCE_SMOKE=1 to verify the exact pinned Kolla checkouts",
)
class ExactKollaClosureSmokeTest(unittest.TestCase):
    def test_all_four_toolchains_expose_the_frozen_override_seams(self) -> None:
        matrix = json.loads(
            (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
        )
        expected_commits = {
            "20.4.0": "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5",
            "20.5.0": "d1c4dd49b92e68509a413c33667bbe87cc3d3a9e",
            "21.2.0": "422afe0d79511eafa3121547a7d5093096b6e0b6",
            "22.1.0": "e40da0d4a7a73212cb16698a12eaeb5799cc55c7",
        }
        self.assertEqual(
            {
                version: toolchain["kolla"]["commit"]
                for version, toolchain in matrix["toolchains"].items()
            },
            expected_commits,
        )
        releases_by_toolchain = {
            version: {
                stream["release"]
                for stream in matrix["streams"]
                if stream["toolchain"] == version
            }
            for version in expected_commits
        }
        profile = json.loads(
            (ROOT / "config" / "profiles" / "deployment.json").read_text(
                encoding="utf-8"
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            verified_urls: set[str] = set()
            for version, commit in expected_commits.items():
                with self.subTest(toolchain=version, commit=commit):
                    self.assertEqual(len(releases_by_toolchain[version]), 1)
                    release = next(iter(releases_by_toolchain[version]))
                    source_set_id = matrix["releases"][release]["source_set"]
                    source_set = load_source_set(
                        SOURCE_SET_DIR / f"{source_set_id}.json",
                        expected_id=source_set_id,
                        expected_release=release,
                        expected_series=matrix["releases"][release]["series"],
                    ).document
                    self.assertEqual(source_set["schema_version"], 3)
                    recorded_toolchain = source_set["kolla_source_inputs"][version]
                    self.assertEqual(
                        recorded_toolchain["kolla"]["commit"], commit
                    )
                    self.assertEqual(
                        recorded_toolchain["kolla_ansible"],
                        matrix["toolchains"][version]["kolla_ansible"],
                    )
                    requirements = source_set["projects"]["openstack/requirements"]
                    pinned_downloads = [
                        (
                            "https://releases.openstack.org/constraints/upper/"
                            + requirements["build_commit"],
                            requirements["upper_constraints_sha256"],
                        ),
                        *(
                            (artifact["url"], artifact["sha256"])
                            for artifact in source_set["direct_artifacts"].values()
                        ),
                    ]
                    for url, expected_sha256 in pinned_downloads:
                        if url in verified_urls:
                            continue
                        request = Request(
                            url, headers={"User-Agent": "kolla-source-smoke/1"}
                        )
                        with urlopen(request, timeout=30) as response:
                            actual_sha256 = hashlib.sha256(response.read()).hexdigest()
                        self.assertEqual(actual_sha256, expected_sha256)
                        verified_urls.add(url)
                    checkout = root / version
                    relative_paths = [
                        "kolla/common/sources.py",
                        "docker/kolla-toolbox/Dockerfile.j2",
                        "docker/ovn/ovn-sb-db-relay/Dockerfile.j2",
                    ]
                    if "mariadb-clustercheck" in source_set["direct_artifacts"]:
                        relative_paths.append(
                            "docker/mariadb/mariadb-base/Dockerfile.j2"
                        )
                    for relative_path in relative_paths:
                        target = checkout / relative_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        url = (
                            "https://opendev.org/openstack/kolla/raw/commit/"
                            f"{commit}/{relative_path}"
                        )
                        request = Request(
                            url, headers={"User-Agent": "kolla-source-smoke/1"}
                        )
                        with urlopen(request, timeout=30) as response:
                            target.write_bytes(response.read())
                        if relative_path == "kolla/common/sources.py":
                            self.assertEqual(
                                hashlib.sha256(target.read_bytes()).hexdigest(),
                                recorded_toolchain["kolla"]["sources_sha256"],
                            )

                    images: set[str] = set()
                    for group in profile["build_groups"]:
                        applies_to = group.get("applies_to", {})
                        releases = applies_to.get("releases", [release])
                        if release not in releases:
                            continue
                        images.add(group["parent"])
                        images.update(group["parents"])
                        images.update(group["images"])
                    frozen = freeze_kolla_sources(
                        source_set,
                        checkout / "kolla" / "common" / "sources.py",
                        images=images,
                        toolchain_version=version,
                    )
                    self.assertIn(
                        "{% block ovn_sb_db_relay_ovn_ctl %}",
                        frozen.template_override_content,
                    )
                    self.assertIn(
                        "UPPER_CONSTRAINTS_SHA256",
                        frozen.template_override_content,
                    )
                    if source_set["series"] == "epoxy":
                        self.assertIn(
                            "{% block mariadb_clustercheck_version %}",
                            frozen.template_override_content,
                        )
                    else:
                        self.assertNotIn(
                            "{% block mariadb_clustercheck_version %}",
                            frozen.template_override_content,
                        )


if __name__ == "__main__":
    unittest.main()
