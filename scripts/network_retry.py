"""Bounded retries at explicitly idempotent network boundaries.

Do not wrap a whole build, validation, or arbitrary shell script in this policy.
Callers must replay the same read or write the same content to the same target.
"""
from __future__ import annotations

import argparse
import errno
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
import re
import socket
import ssl
import subprocess
import sys
import time
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError

RETRY_DELAYS_SECONDS = (5, 15, 30)
MAX_RETRY_AFTER_SECONDS = 60
T = TypeVar("T")


def transient_network_text(message: str, *, allow_missing_manifest: bool = False) -> bool:
    message = message.lower()
    # Permanent failures take precedence even if the output also says timeout.
    if any(marker in message for marker in (
        "unauthorized", "forbidden", "denied", "authentication failed",
        "invalid reference", "not our ref", "couldn't find remote ref",
        "repository not found", "certificate", "no space left",
        "checksum", "digest mismatch", "unknown flag", "unknown option",
        "unrecognized arguments", "invalid argument", "non-fast-forward", "fetch first",
        "could not read username", "could not read password", "authentication required",
    )):
        return False
    if allow_missing_manifest and any(marker in message for marker in (
        "manifest unknown", "not found",
    )):
        return True
    # Match HTTP status contexts, never bare digits in a SHA, URL, or byte count.
    if re.search(
        r"(?:http(?:/[12](?:\.\d)?)?\s+|status(?:\s+code)?[:=]?\s+|error:\s*)"
        r"(?:408|429|500|502|503|504)\b", message
    ):
        return True
    if re.search(r"\b(?:408 request timeout|429 too many requests|500 internal server error|"
                 r"502 bad gateway|503 service unavailable|504 gateway timeout)\b", message):
        return True
    return any(marker in message for marker in (
        "too many requests", "toomanyrequests", "connection reset",
        "connection refused", "connection aborted", "connection closed",
        "connection timed out", "operation timed out", "i/o timeout",
        "timeout", "timed out", "context deadline exceeded",
        "temporary failure", "could not resolve host", "could not resolve proxy",
        "server misbehaving", "network is unreachable", "no route to host",
        "gnutls recv error", "tls connection was non-properly terminated",
        "unexpected eof", "early eof", "remote end closed connection",
        "recv failure", "send failure", "broken pipe", "empty reply from server",
        "http/2 stream", "http2: stream closed", "stream error: stream id",
    )) or bool(re.search(r"(?:^|[ :])eof\s*$", message))


def is_transient_network_error(error: Exception, *, allow_missing_manifest: bool = False) -> bool:
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504} or (
            error.code == 403 and bool(error.headers)
            and bool(error.headers.get("Retry-After"))
        )
    if isinstance(error, URLError):
        if isinstance(error.reason, Exception):
            return is_transient_network_error(error.reason)
        return transient_network_text(str(error.reason))
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired, ConnectionError,
                          IncompleteRead, RemoteDisconnected, ssl.SSLEOFError)):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, OSError) and error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return True
    if isinstance(error, subprocess.CalledProcessError):
        message = "\n".join(
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            for value in (error.stdout, error.stderr) if isinstance(value, (str, bytes))
        )
        return transient_network_text(message, allow_missing_manifest=allow_missing_manifest)
    return False


def _retry_after(error: Exception) -> float:
    if not isinstance(error, HTTPError) or not error.headers:
        return 0
    value = error.headers.get("Retry-After")
    if not value:
        return 0
    try:
        return max(0, float(int(value)))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


def retry_network(operation: Callable[[], T], *, label: str,
                  delays=RETRY_DELAYS_SECONDS, sleep=None,
                  allow_missing_manifest: bool = False) -> T:
    sleep = sleep or time.sleep
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except Exception as error:
            delay = _retry_after(error)
            if isinstance(error, HTTPError):
                error.close()
            if (attempt == len(delays) or delay > MAX_RETRY_AFTER_SECONDS
                    or not is_transient_network_error(error, allow_missing_manifest=allow_missing_manifest)):
                raise
            delay = max(delays[attempt], delay)
            # Avoid echoing credential-bearing commands, URLs, or server responses.
            print(f"{label}: transient network failure; retry {attempt + 1}/{len(delays)} "
                  f"in {delay:g}s.", file=sys.stderr)
            sleep(delay)
    raise AssertionError("unreachable retry state")


def run_network_command(command: list[str], *, label: str, timeout: int = 300,
                        text: bool = True, input=None):
    """Replay a caller-approved idempotent command with captured error output."""
    try:
        return retry_network(
            lambda: subprocess.run(command, check=True, capture_output=True,
                                   text=text, input=input, timeout=timeout),
            label=label,
        )
    except subprocess.CalledProcessError as error:
        # Capturing is needed to classify failures; retain the final diagnostic
        # in tracebacks instead of reducing it to just an exit status.
        detail = error.stderr or error.stdout
        if detail:
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            secret = input.decode("utf-8", errors="replace") if isinstance(input, bytes) else input
            if secret:
                detail = detail.replace(secret, "[redacted]")
            error.add_note(detail)
        raise


def read_url(request, *, opener, timeout: int = 30, expected_status: int | None = None):
    """Retry the entire GET, including an interrupted body, and close each response."""
    if request.get_method() != "GET":
        raise ValueError("network download retries require GET")

    def read():
        with opener(request, timeout=timeout) as response:
            if expected_status is not None and response.status != expected_status:
                raise HTTPError(request.full_url, response.status, "unexpected status",
                                response.headers, None)
            return response.read(), response.headers

    return retry_network(read, label="HTTP download")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--stdin", action="store_true", help="Replay stdin without logging it")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("a command and a positive timeout are required")
    payload = sys.stdin.buffer.read() if args.stdin else None
    try:
        result = run_network_command(command, label="Network command", timeout=args.timeout,
                                     text=False, input=payload)
    except subprocess.CalledProcessError as error:
        sys.stdout.buffer.write(error.stdout or b"")
        sys.stderr.buffer.write(error.stderr or b"")
        return error.returncode
    except subprocess.TimeoutExpired:
        print("Network command timed out after bounded retries.", file=sys.stderr)
        return 124
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
