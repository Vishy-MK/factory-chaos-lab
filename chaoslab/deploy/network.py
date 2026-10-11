"""Which LAN address to serve on and which clients may connect."""

from __future__ import annotations

import ipaddress
import subprocess


def default_interface() -> str | None:
    out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    return None


def interface_address(interface: str) -> ipaddress.IPv4Interface | None:
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", interface], capture_output=True, text=True,
                         check=False)
    for line in out.stdout.splitlines():
        parts = line.split()
        if "inet" in parts:
            return ipaddress.ip_interface(parts[parts.index("inet") + 1])
    return None


def lan(config: dict) -> tuple[str, ipaddress.IPv4Interface, list[ipaddress.IPv4Network]]:
    """(interface, address with prefix, allowed client networks)."""
    interface = config["lan"].get("interface") or "auto"
    if interface == "auto":
        interface = default_interface()
    if not interface:
        raise RuntimeError("no default route: cannot tell which LAN to serve")
    address = interface_address(interface)
    if address is None:
        raise RuntimeError(f"interface {interface} has no IPv4 address")
    if not address.ip.is_private:
        raise RuntimeError(f"{address.ip} is not a private address; refusing to serve the lab on it")
    allowed = [address.network, ipaddress.ip_network("127.0.0.0/8")]
    allowed += [ipaddress.ip_network(n) for n in config["lan"].get("extra_allowed") or []]
    return interface, address, allowed


def client_allowed(host: str, allowed: list) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in allowed if network.version == ip.version)
