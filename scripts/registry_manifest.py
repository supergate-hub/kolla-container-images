"""Bounded retries for newly published registry manifests."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable


RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 15)
RawManifestRunner = Callable[[list[str]], subprocess.CompletedProcess[bytes]]


def _run_raw_inspect(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, check=True, capture_output=True)


def _error_text(error: subprocess.CalledProcessError) -> str:
    values: list[str] = []
    for value in (error.stdout, error.stderr):
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            values.append(value)
    return "\n".join(values).lower()


def is_transient_registry_error(error: subprocess.CalledProcessError) -> bool:
    """Return whether an inspect failure can be caused by registry propagation."""
    output = _error_text(error)
    if any(marker in output for marker in ("unauthorized", "denied", "invalid reference")):
        return False
    return any(
        marker in output
        for marker in (
            "manifest unknown",
            "not found",
            "too many requests",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "connection reset",
            "temporary failure",
        )
    )


def inspect_raw_manifest(
    reference: str,
    *,
    run: RawManifestRunner = _run_raw_inspect,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch immutable manifest bytes, allowing only bounded transient retries."""
    command = ["docker", "buildx", "imagetools", "inspect", "--raw", reference]
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        try:
            result = run(command)
            break
        except subprocess.CalledProcessError as error:
            if not is_transient_registry_error(error):
                raise
            print(
                f"Registry manifest for {reference} is not visible yet; "
                f"retry {attempt}/{len(RETRY_DELAYS_SECONDS)} in {delay}s.",
                file=sys.stderr,
            )
            sleep(delay)
    else:
        result = run(command)
    if not isinstance(result.stdout, bytes):
        raise RuntimeError(f"raw manifest output for {reference} must be bytes")
    return result.stdout
