#!/usr/bin/env bash
# Minimal helper: create a network namespace with its own macvlan
# on the selected interface, then drop you into a shell inside it.
# NO dbus/avahi/nqptp automation here on purpose.
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

if ! command -v ip >/dev/null 2>&1; then
  echo "Missing 'ip' command (iproute2). Install it first." >&2
  exit 1
fi

mapfile -t INTERFACES < <(ip -o link show | awk -F': ' '($2!="lo") {print $2"|"$3}')
if [[ ${#INTERFACES[@]} -eq 0 ]]; then
  echo "No candidate interfaces found." >&2
  exit 1
fi

echo "Available network interfaces:" >&2
for i in "${!INTERFACES[@]}"; do
  name=${INTERFACES[$i]%|*}
  info=${INTERFACES[$i]#*|}
  printf "  [%d] %s  (%s)\n" "$i" "$name" "$info"
done

read -rp "Select parent interface index: " choice
if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 0 || choice >= ${#INTERFACES[@]} )); then
  echo "Invalid selection" >&2
  exit 1
fi
PARENT_IF=${INTERFACES[$choice]%|*}

suffix=$(date +%s)
NS_NAME="ap2ns_${suffix}"
MACVLAN_IF="ap2mv_${suffix}"

echo "[info] Creating netns '$NS_NAME' on parent '$PARENT_IF'..."
if ! ip netns add "$NS_NAME" 2>/tmp/netns_ap2_err.log; then
  echo "[error] ip netns add failed. Details:" >&2
  cat /tmp/netns_ap2_err.log >&2 || true
  exit 1
fi
trap 'ip netns delete "$NS_NAME" 2>/dev/null || true' EXIT

if ! ip link add "$MACVLAN_IF" link "$PARENT_IF" type macvlan 2>/tmp/netns_ap2_err.log; then
  echo "[error] ip link add ... type macvlan failed. This usually means the kernel or this NIC does not support macvlan in this environment (e.g., some VM setups)." >&2
  cat /tmp/netns_ap2_err.log >&2 || true
  echo "[hint] Try running 'sudo ip link add testmv link $PARENT_IF type macvlan' manually to confirm." >&2
  exit 1
fi

if ! ip link set "$MACVLAN_IF" netns "$NS_NAME" 2>/tmp/netns_ap2_err.log; then
  echo "[error] ip link set ... netns failed." >&2
  cat /tmp/netns_ap2_err.log >&2 || true
  exit 1
fi

echo "[info] Namespace created. To delete later manually: sudo ip netns delete $NS_NAME"
echo
cat <<EOF
Inside the namespace shell, run these steps manually (one by one):

  # 1. Bring up interfaces
  ip link set lo up
  ip link set $MACVLAN_IF up
  ip addr show dev $MACVLAN_IF

  # 2. Get an IP from your router (DHCP)
  dhclient -v $MACVLAN_IF

  # 3. (Optional but recommended) Isolate /run and start dbus+avahi
  # WARNING: This shell shares the host mount namespace, so DO NOT mount over /run here
  #          unless you are comfortable with Linux mounts; instead, for now we can
  #          skip running a private avahi and just focus on verifying network + shairport.

  # 4. Start nqptp and shairport-sync
  nqptp &
  shairport-sync -a "Test-$suffix" --statistics

We are intentionally keeping this minimal so you can see each step and its errors.
EOF

echo
echo "[info] Dropping into shell inside namespace '$NS_NAME' now."
ip netns exec "$NS_NAME" bash
