#!/usr/bin/env bash
set -euo pipefail

# Minimal dhclient hook for Shiri network namespaces.
# The stock Ubuntu dhclient-script updates host DNS/system services even when
# dhclient is launched via `ip netns exec`; Shiri only needs an address and a
# default route inside the current namespace.

mask_to_prefix() {
  local mask="$1" bits=0 octet i
  IFS=. read -r -a octets <<< "$mask"
  for octet in "${octets[@]}"; do
    for ((i = 7; i >= 0; i--)); do
      if (( (octet >> i) & 1 )); then
        bits=$((bits + 1))
      fi
    done
  done
  printf '%s\n' "$bits"
}

configure_bound_lease() {
  local prefix router
  [[ -n "${interface:-}" ]] || exit 0
  [[ -n "${new_ip_address:-}" && -n "${new_subnet_mask:-}" ]] || exit 0

  prefix="$(mask_to_prefix "$new_subnet_mask")"
  ip link set dev "$interface" up
  if [[ -n "${new_broadcast_address:-}" ]]; then
    ip addr replace "$new_ip_address/$prefix" brd "$new_broadcast_address" dev "$interface"
  else
    ip addr replace "$new_ip_address/$prefix" dev "$interface"
  fi

  ip route del default dev "$interface" 2>/dev/null || true
  for router in ${new_routers:-}; do
    ip route replace default via "$router" dev "$interface"
    break
  done
}

case "${reason:-}" in
  BOUND|RENEW|REBIND|REBOOT|TIMEOUT)
    configure_bound_lease
    ;;
  EXPIRE|FAIL|RELEASE|STOP)
    [[ -n "${interface:-}" ]] && ip addr flush dev "$interface" 2>/dev/null || true
    ;;
esac

exit 0
