#!/usr/bin/env python3
from __future__ import annotations

import json
import http.client
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.modules.review_access.policies import PrincipalContextSigner


CHILD_ENV_ALLOWLIST = {
    "CI",
    "FCR_E2E_API_BASE_URL",
    "FCR_E2E_BASE_URL",
    "FCR_E2E_DISPOSABLE_DATABASE",
    "FCR_E2E_VIDEO_PATH",
    "FCR_E2E_VIDEO_PATH_V2",
    "FCR_E2E_VIDEO_PATH_V3",
    "FCR_PLAYWRIGHT_CHANNEL",
    "FORCE_COLOR",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
    "TERM",
    "TMPDIR",
    "USER",
}


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def loopback_http_url(name: str) -> urllib.parse.ParseResult:
    value = required(name).rstrip("/")
    try:
        parsed = urllib.parse.urlparse(value)
        _ = parsed.port
    except ValueError as error:
        raise RuntimeError(f"{name} must be a valid loopback HTTP origin") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be a loopback HTTP origin without credentials or a path")
    return parsed


def main() -> int:
    if required("FCR_E2E_DISPOSABLE_DATABASE") != "1":
        raise RuntimeError("FCR_E2E_DISPOSABLE_DATABASE=1 is required")
    parsed_api = loopback_http_url("FCR_E2E_API_BASE_URL")
    parsed_frontend = loopback_http_url("FCR_E2E_BASE_URL")
    api_base_url = parsed_api.geturl().rstrip("/")
    signing_secret = required("WRITE_GUARD_SESSION_SECRET")

    run_suffix = secrets.token_hex(4)
    command_id = f"CreateProject_e2e_{run_suffix}"
    project_name = f"真实栈 E2E {run_suffix}"
    payload = {
        "command_id": command_id,
        "command_type": "CreateProject",
        "contract_version": "1.0",
        "payload": {
            "project_code": f"REAL-{run_suffix}",
            "project_name": project_name,
            "description": "项目范围 principal 的真实前端、后端与 PostgreSQL 联调。",
        },
    }
    signer = PrincipalContextSigner(SimpleNamespace(
        write_guard_session_secret=signing_secret,
        write_guard_session_ttl_seconds=int(os.environ.get("WRITE_GUARD_SESSION_TTL_SECONDS", "14400")),
    ))
    create_token = signer.issue(f"fcr-e2e-bootstrap-{run_suffix}", (), "service")
    request = urllib.request.Request(
        f"{api_base_url}/api/v1/final-cut-review/edit/projects",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": command_id,
            "X-Principal-Context": create_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"isolated project bootstrap failed with HTTP {error.code}") from error

    project_ref_id = result.get("data", {}).get("project_ref_id")
    if not isinstance(project_ref_id, str) or not project_ref_id:
        raise RuntimeError("isolated project bootstrap returned no project_ref_id")

    environment = {
        name: value
        for name, value in os.environ.items()
        if name in CHILD_ENV_ALLOWLIST
    }
    environment["FCR_E2E_PROJECT_REF_ID"] = project_ref_id
    environment["FCR_E2E_PROJECT_NAME"] = project_name
    environment["FCR_E2E_PRINCIPAL_CONTEXT"] = signer.issue(
        f"fcr-e2e-project-user-{run_suffix}",
        (project_ref_id,),
        "user",
    )
    proxy_port = os.environ.get("FCR_E2E_BROWSER_PROXY_PORT", "").strip()
    if proxy_port:
        port = int(proxy_port)
        if not 1024 <= port <= 65535:
            raise RuntimeError("FCR_E2E_BROWSER_PROXY_PORT must be an unprivileged TCP port")
        allowed_origin = f"{parsed_frontend.scheme}://{parsed_frontend.netloc}"
        upstream_port = parsed_api.port or 80
        project_token = environment["FCR_E2E_PRINCIPAL_CONTEXT"]
        hop_by_hop = {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "transfer-encoding", "upgrade",
        }

        class ProjectScopedProxyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _allowed(self) -> bool:
                if self.client_address[0] not in {"127.0.0.1", "::1"}:
                    return False
                path = urllib.parse.urlparse(self.path).path
                if path == "/runtimez":
                    return self.command in {"GET", "HEAD"} and self.headers.get("Origin") in {None, allowed_origin}
                if self.headers.get("Origin") != allowed_origin:
                    return False
                path_segments = tuple(segment for segment in path.split("/") if segment)
                if path == "/api/v1/final-cut-review/projects":
                    return self.command in {"GET", "HEAD", "OPTIONS"}
                if path.startswith("/api/v1/final-cut-review/") and project_ref_id in path_segments:
                    return True
                if path == "/api/v1/files/uploads/init":
                    return self.command in {"POST", "OPTIONS"}
                upload_match = re.fullmatch(
                    r"/api/v1/files/uploads/[A-Za-z0-9_-]{1,128}(?:/parts/[1-9][0-9]{0,8}|/(?:complete|abort))?",
                    path,
                )
                if upload_match is None:
                    return False
                if "/parts/" in path:
                    return self.command in {"PUT", "OPTIONS"}
                if path.endswith(("/complete", "/abort")):
                    return self.command in {"POST", "OPTIONS"}
                return self.command in {"GET", "HEAD", "OPTIONS"}

            def _proxy(self) -> None:
                if not self._allowed():
                    self.send_error(403)
                    return
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length) if content_length else None
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in hop_by_hop
                    and key.lower() not in {"authorization", "forwarded", "host", "x-principal-context"}
                    and not key.lower().startswith("x-forwarded-")
                    and not key.lower().startswith("x-write-guard-")
                }
                headers["Host"] = f"{parsed_api.hostname}:{upstream_port}"
                headers["X-Principal-Context"] = project_token
                connection = http.client.HTTPConnection(parsed_api.hostname, upstream_port, timeout=180)
                try:
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    self.send_response(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in hop_by_hop:
                            self.send_header(key, value)
                    self.end_headers()
                    try:
                        while chunk := response.read(64 * 1024):
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        # Browsers routinely cancel stale polling and media-range requests.
                        return
                finally:
                    connection.close()

            do_DELETE = _proxy
            do_GET = _proxy
            do_HEAD = _proxy
            do_OPTIONS = _proxy
            do_PATCH = _proxy
            do_POST = _proxy
            do_PUT = _proxy

            def log_message(self, format: str, *args: object) -> None:
                return

        ThreadingHTTPServer(("127.0.0.1", port), ProjectScopedProxyHandler).serve_forever()
        return 0

    return subprocess.run(
        ["npm", "run", "test:e2e:integration"],
        cwd=ROOT,
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
