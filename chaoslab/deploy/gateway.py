"""TCP gateway: LAN address:port -> 127.0.0.1 service, for clients from the LAN subnet only.

A plain byte relay, so Grafana's websockets and the MES web app work unchanged; every service keeps its own login.
Each connection is logged with the client address (the services themselves only see 127.0.0.1).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from chaoslab.deploy import network


class Gateway:
    def __init__(self, config: dict, log_path: Path):
        self.config = config
        self.log_path = log_path
        self.interface, self.address, self.allowed = network.lan(config)
        self.limit = int(config["lan"].get("max_connections_per_route", 64))
        self.active: dict[str, int] = {}

    def log(self, **entry) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}) + "\n")

    async def _pipe(self, reader, writer, counter: list[int]) -> None:
        try:
            while data := await reader.read(65536):
                counter[0] += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def handler(self, name: str, target: str):
        host, port = target.rsplit(":", 1)

        async def handle(client_reader, client_writer):
            peer = client_writer.get_extra_info("peername") or ("?", 0)
            client = peer[0]
            if not network.client_allowed(client, self.allowed):
                self.log(route=name, client=client, allowed=False, reason="outside the allowed networks")
                client_writer.close()
                return
            if self.active.get(name, 0) >= self.limit:
                self.log(route=name, client=client, allowed=False, reason="connection limit")
                client_writer.close()
                return
            self.active[name] = self.active.get(name, 0) + 1
            started, up, down = time.monotonic(), [0], [0]
            try:
                server_reader, server_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 5)
            except (OSError, asyncio.TimeoutError) as exc:
                self.log(route=name, client=client, allowed=True, error=f"target unreachable: {exc}")
                client_writer.close()
                self.active[name] -= 1
                return
            try:
                await asyncio.gather(self._pipe(client_reader, server_writer, up),
                                     self._pipe(server_reader, client_writer, down))
            finally:
                self.active[name] -= 1
                if client != "127.0.0.1":
                    self.log(route=name, client=client, allowed=True, bytes_in=up[0], bytes_out=down[0],
                             seconds=round(time.monotonic() - started, 2))

        return handle

    async def serve(self) -> None:
        servers = []
        for name, route in self.config["routes"].items():
            server = await asyncio.start_server(self.handler(name, route["target"]), str(self.address.ip),
                                                int(route["listen_port"]))
            servers.append(server)
            self.log(event="listening", route=name, address=f"{self.address.ip}:{route['listen_port']}",
                     target=route["target"], allowed=[str(n) for n in self.allowed])
        await asyncio.gather(*(s.serve_forever() for s in servers))


def run(settings) -> int:
    import yaml

    config = yaml.safe_load((settings.root / "config" / "deployment.yaml").read_text(encoding="utf-8"))
    gateway = Gateway(config, settings.log_dir / "gateway.log")
    try:
        asyncio.run(gateway.serve())
    except KeyboardInterrupt:
        pass
    return 0
