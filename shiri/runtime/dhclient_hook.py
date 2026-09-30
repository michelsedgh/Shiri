#!/usr/bin/python3
"""Namespace-only DHCP hook. It never changes host DNS, services, or time."""

import ipaddress
import os
import re
import subprocess
import sys


def command(*args, required=True):
    result = subprocess.run(["ip", *args], capture_output=True, timeout=3, check=False)
    if required and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())


def main():
    host_netns = os.environ.get("SHIRI_HOST_NETNS", "")
    if not re.fullmatch(r"net:\[[1-9][0-9]*\]", host_netns):
        raise RuntimeError("Missing or invalid host network namespace identity")
    if os.readlink("/proc/self/ns/net") == host_netns:
        raise RuntimeError("Shiri DHCP hook must run in a separate network namespace")
    interface = os.environ.get("interface", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface):
        raise RuntimeError("Invalid namespace interface")
    reason = os.environ.get("reason", "")
    if reason in {"EXPIRE", "FAIL", "RELEASE", "STOP"}:
        command("-4", "route", "del", "default", "dev", interface, required=False)
        command("-4", "addr", "flush", "dev", interface, required=False)
    elif reason in {"BOUND", "RENEW", "REBIND", "REBOOT", "TIMEOUT"}:
        address = ipaddress.IPv4Address(os.environ["new_ip_address"])
        prefix = ipaddress.IPv4Network("0.0.0.0/" + os.environ["new_subnet_mask"]).prefixlen
        command("link", "set", "dev", interface, "up")
        old = os.environ.get("old_ip_address")
        if old and os.environ.get("old_subnet_mask"):
            old_address = ipaddress.IPv4Address(old)
            old_prefix = ipaddress.IPv4Network("0.0.0.0/" + os.environ["old_subnet_mask"]).prefixlen
            # ip addr replace matches the prefix too. A lease can keep its IP
            # while changing the subnet; retaining the old prefix leaves two
            # addresses/routes on the receiver instead of replacing the lease.
            if (old_address, old_prefix) != (address, prefix):
                command("-4", "addr", "del", f"{old_address}/{old_prefix}", "dev", interface, required=False)
        arguments = ["-4", "addr", "replace", f"{address}/{prefix}"]
        if broadcast := os.environ.get("new_broadcast_address"):
            arguments.extend(["brd", str(ipaddress.IPv4Address(broadcast))])
        command(*arguments, "dev", interface)
        command("-4", "route", "del", "default", "dev", interface, required=False)
        if routers := os.environ.get("new_routers", "").split():
            command(
                "-4",
                "route",
                "replace",
                "default",
                "via",
                str(ipaddress.IPv4Address(routers[0])),
                "dev",
                interface,
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"Shiri namespace DHCP: {exc}", file=sys.stderr)
        sys.exit(1)
