"""DHCP lease transitions retain only the replacement address/prefix."""

from shiri.runtime import dhclient_hook


def lease_environment(monkeypatch, **changes):
    values = {
        "SHIRI_HOST_NETNS": "net:[1]", "interface": "srtest", "reason": "RENEW",
        "new_ip_address": "192.0.2.10", "new_subnet_mask": "255.255.255.0",
        "old_ip_address": "192.0.2.10", "old_subnet_mask": "255.255.255.0",
    }
    values.update(changes)
    monkeypatch.setattr(dhclient_hook.os, "environ", values)
    monkeypatch.setattr(dhclient_hook.os, "readlink", lambda _path: "net:[2]")
    addresses = {(values["old_ip_address"], 24), ("198.51.100.1", 32)}
    operations = []

    def command(*args, required=True):
        operations.append(args)
        if args[:2] == ("-4", "addr"):
            address, prefix = args[3].split("/")
            if args[2] == "del":
                addresses.discard((address, int(prefix)))
            elif args[2] == "replace":
                addresses.add((address, int(prefix)))

    monkeypatch.setattr(dhclient_hook, "command", command)
    return addresses, operations


def test_same_ip_subnet_change_removes_the_previous_prefix(monkeypatch):
    addresses, operations = lease_environment(monkeypatch, new_subnet_mask="255.255.255.128")
    dhclient_hook.main()
    assert addresses == {("192.0.2.10", 25), ("198.51.100.1", 32)}
    remove = operations.index(("-4", "addr", "del", "192.0.2.10/24", "dev", "srtest"))
    replace = operations.index(("-4", "addr", "replace", "192.0.2.10/25", "dev", "srtest"))
    assert remove < replace


def test_address_and_prefix_change_removes_only_the_old_lease(monkeypatch):
    addresses, _ = lease_environment(monkeypatch, reason="REBIND", new_ip_address="192.0.2.11",
                                      new_subnet_mask="255.255.255.192")
    dhclient_hook.main()
    assert addresses == {("192.0.2.11", 26), ("198.51.100.1", 32)}


def test_unchanged_renewal_does_not_remove_the_live_address(monkeypatch):
    addresses, operations = lease_environment(monkeypatch)
    dhclient_hook.main()
    assert addresses == {("192.0.2.10", 24), ("198.51.100.1", 32)}
    assert not any(args[:3] == ("-4", "addr", "del") for args in operations)
