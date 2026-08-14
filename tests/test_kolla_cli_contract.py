from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "validate-kolla-cli-contract.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("kolla_cli_contract", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Kolla CLI contract validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class KollaCliContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_validator()

    def git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def create_fake_kolla(
        self,
        root: Path,
        *,
        behavior: str = "success",
    ) -> tuple[Path, str]:
        repository = root / f"kolla-{behavior}"
        repository.mkdir()
        self.git(repository, "init", "--quiet")
        self.git(repository, "config", "user.name", "Kolla CLI Contract Test")
        self.git(
            repository,
            "config",
            "user.email",
            "kolla-cli-contract@example.invalid",
        )
        package = repository / "kolla"
        common = package / "common"
        common.mkdir(parents=True)
        oslo_config = repository / "oslo_config"
        oslo_config.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (common / "__init__.py").write_text("", encoding="utf-8")
        (oslo_config / "__init__.py").write_text(
            "from . import cfg\n",
            encoding="utf-8",
        )
        (oslo_config / "cfg.py").write_text(
            "class ConfigOpts:\n    pass\n",
            encoding="utf-8",
        )
        pull_expression = "True" if behavior == "pull" else "('--nopull' not in args)"
        regex_expression = (
            "['not-the-plan-regex']" if behavior == "regex" else "[args[-1]]"
        )
        (common / "config.py").write_text(
            f"""from pathlib import Path
import os

from oslo_config import cfg


def parse(conf, args, usage=None, prog=None, default_config_files=None):
    if prog != 'kolla-build':
        raise RuntimeError('unexpected program')
    if os.environ.get('PBR_VERSION') not in ('20.4.0', '20.5.0'):
        raise RuntimeError('missing exact PBR_VERSION')
    config_path = Path(args[args.index('--config-file') + 1])
    if config_path.read_text(encoding='utf-8') != '[DEFAULT]\\n':
        raise RuntimeError('wrong frozen build config')
    override_path = Path(args[args.index('--template-override') + 1])
    if override_path.read_text(encoding='utf-8') != '{{% set marker = true %}}\\n':
        raise RuntimeError('wrong frozen template override')
    conf.pull = {pull_expression}
    conf.regex = {regex_expression}
""",
            encoding="utf-8",
        )
        self.git(repository, "add", "kolla", "oslo_config")
        self.git(repository, "commit", "--quiet", "-m", "fake Kolla package")
        return repository, self.git(repository, "rev-parse", "HEAD")

    def matrix(
        self,
        repository: Path,
        commit: str,
        *,
        second_toolchain: bool = False,
    ) -> dict[str, object]:
        toolchains = {
            "20.4.0": {
                "kolla": {"repository": str(repository), "commit": commit},
            }
        }
        streams = [
            {
                "id": "z-stream",
                "toolchain": "20.4.0",
                "publish_enabled": True,
            },
            {
                "id": "a-stream",
                "toolchain": "20.4.0",
                "publish_enabled": True,
            },
        ]
        if second_toolchain:
            toolchains["20.5.0"] = {
                "kolla": {"repository": str(repository), "commit": commit},
            }
            streams.append(
                {
                    "id": "b-stream",
                    "toolchain": "20.5.0",
                    "publish_enabled": True,
                }
            )
        return {"toolchains": toolchains, "streams": streams}

    def plan(
        self,
        stream_id: str,
        *,
        target_regex: str = "^keystone$",
    ) -> dict[str, object]:
        config_content = "[DEFAULT]\n"
        override_content = "{% set marker = true %}\n"
        command = [
            "kolla-build",
            "--config-file",
            "artifacts/config/kolla-build.conf",
            "--template-override",
            "artifacts/config/template-overrides.j2",
            "--nopull",
            target_regex,
        ]
        return {
            "stream": stream_id,
            "openstack_sources": {
                "kolla_build_config": {
                    "content": config_content,
                    "sha256": (
                        "sha256:"
                        + hashlib.sha256(config_content.encode()).hexdigest()
                    ),
                },
                "template_override": {
                    "content": override_content,
                    "sha256": (
                        "sha256:"
                        + hashlib.sha256(override_content.encode()).hexdigest()
                    ),
                },
            },
            "build": {
                "all_units": [
                    {
                        "id": "amd64-leaf-keystone",
                        "arch": "amd64",
                        "kind": "leaf",
                        "target": "keystone",
                        "ancestor_chain": ["base", "openstack-base", "keystone-base"],
                        "command": command,
                    }
                ]
            },
        }

    def validate(
        self,
        matrix: dict[str, object],
        checkout_root: Path,
        plan_provider,
    ):
        return self.validator.validate_contract(
            matrix,
            plan_provider=plan_provider,
            checkout_root=checkout_root,
            python_executable=Path(sys.executable),
            worker_script=SCRIPT_PATH,
        )

    def test_validates_every_toolchain_using_its_first_representative_stream(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository, commit = self.create_fake_kolla(root)
            matrix = self.matrix(repository, commit, second_toolchain=True)
            planned: list[tuple[str, str]] = []

            def plan_provider(version, stream):
                planned.append((version, stream["id"]))
                return self.plan(stream["id"])

            results = self.validate(matrix, root / "validation", plan_provider)

            self.assertEqual(planned, [("20.4.0", "a-stream"), ("20.5.0", "b-stream")])
            self.assertEqual(
                results,
                [
                    {
                        "toolchain": "20.4.0",
                        "stream": "a-stream",
                        "target_regex": "^keystone$",
                    },
                    {
                        "toolchain": "20.5.0",
                        "stream": "b-stream",
                        "target_regex": "^keystone$",
                    },
                ],
            )
            for version in ("20.4.0", "20.5.0"):
                checkout = root / "validation" / f"kolla-{version}"
                self.assertEqual(
                    self.git(
                        checkout,
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ),
                    "",
                )

    def test_rejects_parser_pull_or_regex_contract_drift(self) -> None:
        for behavior, expected_error in (
            ("pull", "disable upstream base pulls"),
            ("regex", "target was not parsed exactly"),
        ):
            with (
                self.subTest(behavior=behavior),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                repository, commit = self.create_fake_kolla(root, behavior=behavior)
                matrix = self.matrix(repository, commit)

                with self.assertRaisesRegex(
                    self.validator.KollaCliContractError,
                    expected_error,
                ):
                    self.validate(
                        matrix,
                        root / "validation",
                        lambda _version, stream: self.plan(stream["id"]),
                    )

    def test_rejects_plan_target_regex_that_is_not_exactly_anchored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository, commit = self.create_fake_kolla(root)
            matrix = self.matrix(repository, commit)

            with self.assertRaisesRegex(
                self.validator.KollaCliContractError,
                "exact target regex",
            ):
                self.validate(
                    matrix,
                    root / "validation",
                    lambda _version, stream: self.plan(
                        stream["id"], target_regex="keystone"
                    ),
                )

    def test_rejects_tampered_frozen_config_before_parser_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository, commit = self.create_fake_kolla(root)
            matrix = self.matrix(repository, commit)
            plan = self.plan("a-stream")
            plan["openstack_sources"]["kolla_build_config"]["sha256"] = "0" * 64

            with self.assertRaisesRegex(
                self.validator.KollaCliContractError,
                "digest",
            ):
                self.validate(
                    matrix,
                    root / "validation",
                    lambda _version, _stream: plan,
                )

    def test_rejects_a_kolla_commit_that_cannot_be_checked_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository, _commit = self.create_fake_kolla(root)
            matrix = self.matrix(repository, "a" * 40)

            with self.assertRaisesRegex(
                self.validator.KollaCliContractError,
                "checkout",
            ):
                self.validate(
                    matrix,
                    root / "validation",
                    lambda _version, stream: self.plan(stream["id"]),
                )


if __name__ == "__main__":
    unittest.main()
