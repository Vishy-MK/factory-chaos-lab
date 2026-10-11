"""Authenticated HTTP control API for the lab, for use across the LAN through the gateway.

A thin layer over the same `LabApi` and `Lifecycle` the CLI and MCP server use, so the safety policy, the lease,
the watchdog and the audit log decide and record exactly as they do locally. Every request needs
`Authorization: Bearer <CHAOSLAB_CONTROL_TOKEN>`; the API refuses to start without a strong token and only binds to
loopback (the gateway carries LAN clients to it).

    GET  /api/status                      plant, resources and the active experiment
    GET  /api/faults | /api/targets | /api/experiments | /api/experiments/<name>
    GET  /api/runs?limit=20 | /api/runs/<id> | /api/runs/<id>/recovery | /api/runs/<id>/metrics
    GET  /api/analysis/<id>?section=summary
    POST /api/validate                    {"definition": {...}}
    POST /api/run                         {"name": "..."} or {"definition": {...}}, optional "dry_run": true
    POST /api/emergency-stop
    POST /api/plant/start | /api/plant/stop
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_BODY = 64 * 1024
MIN_TOKEN = 24


class ControlApi:
    def __init__(self, settings, token: str):
        from chaoslab.api import LabApi
        from chaoslab.audit import AuditLog
        from chaoslab.lifecycle import Lifecycle

        if len(token or "") < MIN_TOKEN:
            raise ValueError(f"CHAOSLAB_CONTROL_TOKEN must be at least {MIN_TOKEN} characters")
        self.settings = settings
        self.token = token
        self.api = LabApi(settings.root)
        self.life = Lifecycle(settings)
        self.audit = AuditLog(settings.audit_file)
        self.lock = threading.Lock()

    def authorised(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip().encode(), self.token.encode())

    def get(self, path: str, query: dict) -> tuple[int, object]:
        parts = [p for p in path.split("/") if p][1:]   # drop "api"
        if parts == ["status"]:
            from chaoslab import resources

            return 200, {"plant": self.life.status(), "active": self.api.status(),
                         "resources": resources.gate_report(self.settings.policy)}
        if parts == ["faults"]:
            return 200, {"fault_types": self.api.discover_fault_types()}
        if parts == ["targets"]:
            return 200, self.api.discover_targets()
        if parts == ["experiments"]:
            return 200, {"experiments": self.api.list_experiments()}
        if len(parts) == 2 and parts[0] == "experiments":
            return 200, self.api.get_experiment(parts[1])
        if parts == ["runs"]:
            limit = int((query.get("limit") or ["20"])[0])
            runs = self.api.store.list_runs()[-max(1, min(limit, 200)):]
            return 200, {"runs": [self._run_summary(r) for r in reversed(runs)]}
        if len(parts) == 2 and parts[0] == "runs":
            return 200, self.api.result(parts[1])
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "recovery":
            return 200, self.api.recovery(parts[1])
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "metrics":
            return 200, self.api.metrics(parts[1])
        if len(parts) == 2 and parts[0] == "analysis":
            from chaoslab.analysis import engine
            from chaoslab.analysis.narrative import analysis_section

            section = (query.get("section") or ["summary"])[0]
            return 200, analysis_section(engine.read(self.settings, parts[1]), section)
        return 404, {"error": "not found", "see": "GET /api/help"}

    def _run_summary(self, run_id: str) -> dict:
        try:
            result = self.api.store.read_result(run_id)
        except (OSError, ValueError):
            return {"experiment_id": run_id}
        return {"experiment_id": run_id, "name": (result.get("definition") or {}).get("name"),
                "overall_result": result.get("overall_result"), "finished_at": result.get("finished_at")}

    def post(self, path: str, body: dict, client: str) -> tuple[int, object]:
        parts = [p for p in path.split("/") if p][1:]
        action = "/".join(parts)
        with self.lock:
            self.audit.record(None, "control_api_request", action=action, client=client,
                              body={k: v for k, v in body.items() if k != "definition"} | (
                                  {"definition_name": (body.get("definition") or {}).get("name")}
                                  if body.get("definition") else {}))
            if action == "validate":
                return 200, self.api.validate(body.get("definition") or {})
            if action == "run":
                definition = body.get("definition")
                if definition is None and body.get("name"):
                    definition = self.api.get_experiment(str(body["name"]))["definition"]
                if not isinstance(definition, dict):
                    return 400, {"error": "give a stored experiment name or a definition"}
                if body.get("dry_run"):
                    return 200, self.api.run(definition, dry_run=True)
                return 202, self.api.submit(definition)
            if action == "emergency-stop":
                return 200, self.api.emergency_stop()
            if action == "plant/start":
                return 200, self.life.start(with_exporter=False)
            if action == "plant/stop":
                return 200, self.life.stop()
        return 404, {"error": "not found", "see": "GET /api/help"}


def make_handler(control: ControlApi):
    class Handler(BaseHTTPRequestHandler):
        server_version = "chaoslab-control"

        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload, default=str, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> bool:
            if not control.authorised(self.headers.get("Authorization")):
                control.audit.record(None, "control_api_refused", path=self.path, client=self.client_address[0])
                self._send(401, {"error": "missing or wrong bearer token"})
                return False
            return True

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/api", "/api/help"):
                self._send(200, {"service": "chaoslab control API", "auth": "Authorization: Bearer <token>",
                                 "endpoints": (__doc__ or "").split("\n\n")[2].strip().splitlines()})
                return
            if not self._guard():
                return
            try:
                status, payload = control.get(url.path, parse_qs(url.query))
            except FileNotFoundError as exc:
                status, payload = 404, {"error": str(exc)}
            except Exception as exc:
                status, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
            self._send(status, payload)

        def do_POST(self):
            if not self._guard():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"error": "request body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("the body must be a JSON object")
                status, payload = control.post(urlparse(self.path).path, body, self.client_address[0])
            except FileNotFoundError as exc:
                status, payload = 404, {"error": str(exc)}
            except Exception as exc:
                status, payload = 400, {"error": f"{type(exc).__name__}: {exc}"}
            self._send(status, payload)

        def log_message(self, format, *args):
            return

    return Handler


def serve(settings) -> int:
    import yaml

    config = yaml.safe_load((settings.root / "config" / "deployment.yaml").read_text(encoding="utf-8"))
    host = config["control_api"]["host"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the control API binds to loopback only; the gateway exposes it to the LAN")
    control = ControlApi(settings, os.environ.get("CHAOSLAB_CONTROL_TOKEN", ""))
    server = ThreadingHTTPServer((host, int(config["control_api"]["port"])), make_handler(control))
    server.daemon_threads = True
    control.audit.record(None, "control_api_started", at=time.time())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
