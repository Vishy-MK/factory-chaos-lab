import asyncio
import ipaddress
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from chaoslab.deploy import control_api, network
from chaoslab.deploy.gateway import Gateway

TOKEN = "t" * 40


def test_only_lan_and_loopback_clients_are_allowed():
    allowed = [ipaddress.ip_network("10.140.134.0/24"), ipaddress.ip_network("127.0.0.0/8")]
    assert network.client_allowed("10.140.134.20", allowed)
    assert network.client_allowed("127.0.0.1", allowed)
    assert network.client_allowed("::ffff:10.140.134.20", allowed)
    assert not network.client_allowed("10.140.135.20", allowed)
    assert not network.client_allowed("8.8.8.8", allowed)
    assert not network.client_allowed("not-an-ip", allowed)


def test_gateway_relays_allowed_clients_and_drops_others(monkeypatch, tmp_path):
    async def scenario():
        async def echo(reader, writer):
            writer.write(b"pong:" + await reader.read(100))
            await writer.drain()
            writer.close()

        target = await asyncio.start_server(echo, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        config = {"lan": {"interface": "lo", "extra_allowed": [], "max_connections_per_route": 4},
                  "routes": {"echo": {"listen_port": 0, "target": f"127.0.0.1:{target_port}"}}}
        monkeypatch.setattr(network, "lan", lambda c: ("lo", ipaddress.ip_interface("127.0.0.1/8"),
                                                        [ipaddress.ip_network("127.0.0.0/8")]))
        gateway = Gateway(config, tmp_path / "gateway.log")
        server = await asyncio.start_server(gateway.handler("echo", f"127.0.0.1:{target_port}"), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ping")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        gateway.allowed = [ipaddress.ip_network("10.0.0.0/8")]           # loopback no longer allowed
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        refused = await asyncio.wait_for(reader.read(100), 5)
        server.close()
        target.close()
        return answer, refused

    answer, refused = asyncio.run(scenario())
    assert answer == b"pong:ping"
    assert refused == b""
    log = [json.loads(line) for line in (tmp_path / "gateway.log").read_text().splitlines()]
    assert any(entry.get("allowed") is False for entry in log)


@pytest.fixture
def control(settings):
    api = control_api.ControlApi(settings, TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), control_api.make_handler(api))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", api
    server.shutdown()


def call(base, path, token=TOKEN, body=None):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_control_api_requires_the_token(control, settings):
    base, _ = control
    assert call(base, "/api/help", token=None)[0] == 200
    for path in ("/api/faults", "/api/experiments", "/api/runs"):
        assert call(base, path, token=None)[0] == 401
        assert call(base, path, token="wrong" * 10)[0] == 401
        assert call(base, path)[0] == 200
    assert call(base, "/api/emergency-stop", token=None, body={})[0] == 401
    events = [json.loads(line)["event"] for line in settings.audit_file.read_text().splitlines()]
    assert "control_api_refused" in events


def test_control_api_still_goes_through_the_safety_policy(control, settings):
    base, _ = control
    status, result = call(base, "/api/run", body={"definition": {
        "name": "remote", "category": "infrastructure", "target": {"plant": "prod", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 5}}}})
    assert status == 202 and result["accepted"] is False and result["stage"] == "policy"
    status, result = call(base, "/api/run", body={"definition": {
        "name": "too-long", "category": "infrastructure", "target": {"plant": "bottling", "component": "api"},
        "fault": {"type": "process_outage", "parameters": {"duration_s": 3600}}}})
    assert result["accepted"] is False
    assert not (settings.state_dir / "submissions").exists()
    events = [json.loads(line) for line in settings.audit_file.read_text().splitlines()]
    assert any(e["event"] == "control_api_request" and e.get("action") == "run" for e in events)


def test_control_api_refuses_a_weak_token_and_a_lan_bind(settings, tmp_path):
    with pytest.raises(ValueError):
        control_api.ControlApi(settings, "short")
    deployment = settings.root / "config" / "deployment.yaml"
    text = deployment.read_text().replace("  host: 127.0.0.1", "  host: 0.0.0.0")
    deployment.write_text(text)
    with pytest.raises(ValueError, match="loopback"):
        control_api.serve(settings)


def test_reader_password_follows_the_deployment_secret(settings, monkeypatch):
    monkeypatch.delenv(settings.lab["reader"]["password_env"], raising=False)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.deployment_env_file.write_text("FSMES_ANALYST_PASSWORD=rotated-secret-value\n")
    from chaoslab import settings as settings_module

    fresh = settings_module.load(settings.root)
    assert fresh.reader_credentials == ("ANALYST", "rotated-secret-value")
