"""Bounded retries for newly published registry manifests."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

try:
    from scripts.network_retry import is_transient_network_error, retry_network
except ModuleNotFoundError:
    from network_retry import is_transient_network_error, retry_network

RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 15)
RawManifestRunner = Callable[[list[str]], subprocess.CompletedProcess[bytes]]


def _run_raw_inspect(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=True, capture_output=True, timeout=60)


def is_transient_registry_error(error: subprocess.CalledProcessError) -> bool:
    """Return whether an inspect failure can be caused by registry propagation."""
    return is_transient_network_error(error, allow_missing_manifest=True)


def inspect_raw_manifest(
    reference: str,
    *,
    run: RawManifestRunner = _run_raw_inspect,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch immutable manifest bytes, allowing only bounded transient retries."""
    command = ["docker", "buildx", "imagetools", "inspect", "--raw", reference]
    result = retry_network(lambda: run(command), label="Registry manifest inspection",
                           delays=RETRY_DELAYS_SECONDS, sleep=sleep,
                           allow_missing_manifest=True)
    if not isinstance(result.stdout, bytes):
        raise RuntimeError(f"raw manifest output for {reference} must be bytes")
    return result.stdout
