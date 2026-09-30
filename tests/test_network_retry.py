from __future__ import annotations

from contextlib import redirect_stderr
from email.message import Message
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts import network_retry as retry


class NetworkRetryTest(unittest.TestCase):
    def test_workflow_cli_replays_stdin_and_preserves_exit_and_output(self):
        wrapper = Path(__file__).resolve().parents[1] / "scripts" / "network_retry.py"
        result = subprocess.run([
            sys.executable, str(wrapper), "--timeout", "5", "--stdin", "--", sys.executable,
            "-c", "import sys; assert sys.stdin.buffer.read() == b'private-input'; print('ok')",
        ], input=b"private-input", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"ok\n")
        self.assertEqual(result.stderr, b"")
        result = subprocess.run([
            sys.executable, str(wrapper), "--", sys.executable, "-c",
            "import sys; print('unauthorized', file=sys.stderr); sys.exit(17)",
        ], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stderr, b"unauthorized\n")

    def test_transport_failures_cover_commands_and_python_downloads(self):
        messages = [
            "error: RPC failed; curl 56 GnuTLS recv error (-110): TLS connection was non-properly terminated",
            "unexpected EOF", "read: connection reset by peer", "connection refused",
            "lookup ghcr.io: server misbehaving", "Could not resolve host: opendev.org",
            "TLS handshake timeout", "context deadline exceeded", "broken pipe",
            "unexpected status: 503 Service Unavailable", "HTTP/2 429",
            "The requested URL returned error: 500", "toomanyrequests: rate limit",
            "unexpected status from HEAD request: 504 Gateway Timeout",
        ]
        for message in messages:
            with self.subTest(message=message):
                # subprocess wrappers must work with both text and bytes stderr.
                for value in (message, message.encode()):
                    error = subprocess.CalledProcessError(1, ["network-command"], stderr=value)
                    self.assertTrue(retry.is_transient_network_error(error))
        for error in [TimeoutError(), subprocess.TimeoutExpired(["git"], 60),
                      URLError(ConnectionResetError()), IncompleteRead(b"partial"),
                      ssl.SSLEOFError(), socket.gaierror(socket.EAI_AGAIN, "try again")]:
            with self.subTest(error=error):
                self.assertTrue(retry.is_transient_network_error(error))

    def test_permanent_errors_win_and_digest_digits_are_not_http_statuses(self):
        for message in [
            "denied: requested access; connection reset", "unauthorized: HTTP 503",
            "certificate verify failed: timeout", "fatal: not our ref " + "503" * 13,
            "repository not found", "invalid reference format", "no space left on device",
            "checksum mismatch after unexpected EOF", "manifest unknown",
            "failed to pull image: sha256:" + "500" * 21,
            "bad request for https://registry.test/images/503/layers",
        ]:
            with self.subTest(message=message):
                self.assertFalse(retry.transient_network_text(message))
        for error in [ValueError("timeout"), PermissionError(), FileNotFoundError(),
                      ssl.SSLCertVerificationError(), socket.gaierror(socket.EAI_NONAME, "not found")]:
            self.assertFalse(retry.is_transient_network_error(error))
        self.assertTrue(retry.transient_network_text("manifest unknown", allow_missing_manifest=True))

    def test_bounded_command_retries_preserve_argv_stdin_and_last_error(self):
        command = ["docker", "login", "ghcr.io", "--password-stdin"]
        error = subprocess.CalledProcessError(1, command, stderr="unexpected EOF; secret-value")
        with patch.object(retry.subprocess, "run", side_effect=error) as run, \
             patch.object(retry.time, "sleep") as sleep, redirect_stderr(io.StringIO()) as logs:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                retry.run_network_command(command, label="Login", input="secret-value", timeout=60)
        self.assertIs(raised.exception, error)
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(call == run.call_args for call in run.call_args_list))
        self.assertEqual(run.call_args.kwargs["input"], "secret-value")
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15, 30])
        self.assertNotIn("secret-value", logs.getvalue())
        self.assertIn("unexpected EOF", "\n".join(error.__notes__))
        self.assertNotIn("secret-value", "\n".join(error.__notes__))

    def test_permanent_error_is_not_retried_and_validation_is_not_caught(self):
        for error in [subprocess.CalledProcessError(1, ["docker"], stderr="unauthorized"),
                      ValueError("digest mismatch"), KeyboardInterrupt()]:
            operation = Mock(side_effect=error)
            with patch.object(retry.time, "sleep") as sleep, self.assertRaises(type(error)):
                retry.retry_network(operation, label="Read")
            operation.assert_called_once()
            sleep.assert_not_called()

    def test_http_status_and_retry_after_are_respected_with_a_bound(self):
        for status in (408, 429, 500, 502, 503, 504, 403):
            headers = Message()
            headers["Retry-After"] = "7"
            error = HTTPError("https://example.test", status, "busy", headers, io.BytesIO())
            operation = Mock(side_effect=[error, b"ok"])
            waits = []
            self.assertEqual(retry.retry_network(operation, label="GET", sleep=waits.append), b"ok")
            self.assertEqual(waits, [7])
            self.assertTrue(error.fp.closed)
        for status, retry_after in [(401, None), (403, None), (404, None), (501, None), (429, "120")]:
            headers = Message()
            if retry_after:
                headers["Retry-After"] = retry_after
            error = HTTPError("https://example.test", status, "failed", headers, None)
            operation = Mock(side_effect=error)
            with self.assertRaises(HTTPError):
                retry.retry_network(operation, label="GET", sleep=lambda _: self.fail("unexpected retry"))
            operation.assert_called_once()

    def test_http_retry_after_date(self):
        headers = Message()
        headers["Retry-After"] = "Thu, 01 Jan 1970 00:00:20 GMT"
        error = HTTPError("https://example.test", 503, "busy", headers, None)
        with patch.object(retry.time, "time", return_value=10):
            waits = []
            retry.retry_network(Mock(side_effect=[error, None]), label="GET", sleep=waits.append)
        self.assertEqual(waits, [10])

    def test_real_http_server_recovers_from_503_and_truncated_body(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if len(requests) == 1:
                    self.send_response(503)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "8" if len(requests) == 2 else "2")
                    self.send_header("Docker-Content-Digest", "expected-digest")
                    self.end_headers()
                    self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/immutable",
                              headers={"Authorization": "Bearer local-test-token"})
            with patch.object(retry.time, "sleep") as sleep:
                raw, headers = retry.read_url(request, opener=urlopen, timeout=2, expected_status=200)
            self.assertEqual(raw, b"ok")
            self.assertEqual(headers["Docker-Content-Digest"], "expected-digest")
            self.assertEqual(requests, [("/immutable", "Bearer local-test-token")] * 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_mutations_cannot_use_download_retries(self):
        opener = Mock()
        with self.assertRaises(ValueError):
            retry.read_url(Request("https://example.test", data=b"mutation"), opener=opener)
        opener.assert_not_called()

    def test_digest_addressed_manifest_write_replays_only_the_same_write(self):
        command = ["docker", "buildx", "imagetools", "create", "--tag", "registry/image:revision",
                   "--metadata-file", "manifest.json", "registry/image@sha256:" + "a" * 64]
        error = subprocess.CalledProcessError(1, command, stderr="unexpected EOF")
        done = subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(retry.subprocess, "run", side_effect=[error, done]) as run, \
             patch.object(retry.time, "sleep"):
            self.assertIs(retry.run_network_command(command, label="Manifest"), done)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
