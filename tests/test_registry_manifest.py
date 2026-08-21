from __future__ import annotations

import subprocess
import unittest

from scripts.registry_manifest import inspect_raw_manifest


REF = "ghcr.io/supergate-hub/kolla-container-images/keystone@sha256:" + "a" * 64
RAW_MANIFEST = b'{"schemaVersion":2,"manifests":[]}'


class RegistryManifestTest(unittest.TestCase):
    def test_retries_a_new_manifest_until_the_registry_exposes_it(self) -> None:
        attempts: list[list[str]] = []
        waits: list[float] = []

        def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
            attempts.append(command)
            if len(attempts) <= 2:
                raise subprocess.CalledProcessError(
                    1,
                    command,
                    stderr=b"manifest unknown: manifest is not known to the registry",
                )
            return subprocess.CompletedProcess(command, 0, stdout=RAW_MANIFEST)

        actual = inspect_raw_manifest(REF, run=run, sleep=waits.append)

        self.assertEqual(actual, RAW_MANIFEST)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(waits, [1, 2])
        self.assertEqual(
            attempts[0],
            ["docker", "buildx", "imagetools", "inspect", "--raw", REF],
        )

    def test_does_not_retry_a_permanent_registry_error(self) -> None:
        attempts: list[list[str]] = []
        waits: list[float] = []

        def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
            attempts.append(command)
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr=b"denied: requested access to the resource is denied",
            )

        with self.assertRaises(subprocess.CalledProcessError):
            inspect_raw_manifest(REF, run=run, sleep=waits.append)

        self.assertEqual(len(attempts), 1)
        self.assertEqual(waits, [])

    def test_reports_the_final_transient_error_after_bounded_retries(self) -> None:
        attempts: list[list[str]] = []
        waits: list[float] = []

        def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
            attempts.append(command)
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr=b"manifest unknown",
            )

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            inspect_raw_manifest(REF, run=run, sleep=waits.append)

        self.assertEqual(raised.exception.stderr, b"manifest unknown")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(waits, [1, 2, 4, 8, 15])
