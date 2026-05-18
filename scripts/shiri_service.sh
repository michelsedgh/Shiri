#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${SHIRI_APP_DIR:-/home/ubuntu/Shiri}"
BASE_DIR="${SHIRI_BASE_DIR:-/var/lib/shiri}"
PIDFILE="$BASE_DIR/shiri.pid"
LOGFILE="$BASE_DIR/app.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

usage() {
  echo "Usage: sudo $0 {start|stop|restart|status|cleanup}" >&2
}

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root: sudo $0 $*" >&2
    exit 1
  fi
}

log() {
  echo "[$(date '+%H:%M:%S')] $*"
}

app_pids() {
  ps -eo pid=,comm=,args= | awk -v app="$APP_DIR/app.py" '
    $2 != "python3" { next }
    $0 ~ "python3 " app { print $1 }
    $0 ~ "python3 app.py" { print $1 }
  '
}

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

pidfile_pid() {
  [[ -f "$PIDFILE" ]] && sed -n '1p' "$PIDFILE" || true
}

stop_app() {
  local pids pid deadline
  pids="$(app_pids | sort -u)"
  if [[ -z "$pids" ]]; then
    rm -f "$PIDFILE"
    return
  fi

  log "Stopping Shiri app: ${pids//$'\n'/ }"
  while read -r pid; do
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
  done <<< "$pids"

  deadline=$((SECONDS + 30))
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    pids="$(app_pids | sort -u)"
    [[ -z "$pids" ]] && break
    sleep 1
  done

  pids="$(app_pids | sort -u)"
  if [[ -n "$pids" ]]; then
    log "App did not stop cleanly; forcing: ${pids//$'\n'/ }"
    while read -r pid; do
      [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
    done <<< "$pids"
  fi
  rm -f "$PIDFILE"
}

kill_matching() {
  local pattern="$1"
  local label="$2"
  local pids skip pid ppid
  skip=" $$"
  ppid="$(ps -o ppid= -p "$$" | tr -d ' ' || true)"
  while [[ -n "$ppid" && "$ppid" != "0" ]]; do
    skip="$skip $ppid"
    ppid="$(ps -o ppid= -p "$ppid" | tr -d ' ' || true)"
  done
  pids="$(pgrep -f "$pattern" || true)"
  for pid in $skip; do
    pids="$(printf '%s\n' "$pids" | awk -v skip_pid="$pid" '$1 != skip_pid')"
  done
  [[ -z "$pids" ]] && return
  log "Cleaning $label: ${pids//$'\n'/ }"
  while read -r pid; do
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
  done <<< "$pids"
  sleep 1
  while read -r pid; do
    [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
  done <<< "$pids"
}

cleanup_netns() {
  local ns pids iface
  while read -r ns; do
    [[ -z "$ns" ]] && continue
    log "Cleaning namespace $ns"
    pids="$(ip netns pids "$ns" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      while read -r pid; do
        [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null || true
      done <<< "$pids"
      sleep 1
      while read -r pid; do
        [[ -n "$pid" ]] && kill -KILL "$pid" 2>/dev/null || true
      done <<< "$pids"
    fi
    ip netns delete "$ns" 2>/dev/null || true
  done < <(ip netns list | awk '{print $1}' | grep -E '^shiri_(ot|rx_)' || true)

  for iface in otapi0 otlan0 rx0 rx1 rx2 rx3 rx4 rx5 rx6 rx7 rx8 rx9; do
    ip link delete "$iface" 2>/dev/null || true
  done
}

cleanup_runtime() {
  kill_matching "$APP_DIR/audio_mixer.py" "audio mixers"
  kill_matching "$BASE_DIR/groups/.*/config/mixer_supervisor.sh" "mixer supervisors"
  kill_matching "shairport-sync .* $BASE_DIR/groups/.*/config/shairport-sync.conf" "Shairport receivers"
  kill_matching "owntone .* $BASE_DIR/groups/.*/config/owntone.conf" "OwnTone instances"
  kill_matching "dbus-daemon --config-file $BASE_DIR/(groups|owntone-sender)/.*dbus.*\\.conf" "Shiri D-Bus daemons"
  kill_matching "avahi-daemon: running \\[shiri-" "Shiri Avahi daemons"
  kill_matching "airptpd -f -v" "Shiri airptpd"
  kill_matching "nqptp -v" "Shiri nqptp"
  kill_matching "dhclient .*((/var/lib/dhcp/dhclient-shiri-)|($BASE_DIR/(groups|owntone-sender))|(/run/shiri/dhcp))" "Shiri dhclient processes"
  cleanup_netns
  rm -rf "$BASE_DIR/timing" "$BASE_DIR/timing_sync"
  rm -f "$BASE_DIR/network_leases.json"
}

start_service() {
  mkdir -p "$BASE_DIR"
  local pid existing
  existing="$(pidfile_pid)"
  if is_running "$existing"; then
    log "Shiri already running as pid $existing"
    return
  fi
  if [[ -n "$(app_pids | sort -u)" ]]; then
    sleep 2
  fi
  if [[ -n "$(app_pids | sort -u)" ]]; then
    log "Found app process without current pidfile; refusing to double-start"
    app_pids | sort -u
    exit 1
  fi

  log "Starting Shiri"
  cd "$APP_DIR"
  nohup env PATH="$PATH" PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" "$APP_DIR/app.py" > "$LOGFILE" 2>&1 &
  pid="$!"
  echo "$pid" > "$PIDFILE"
  sleep 1
  if ! is_running "$pid"; then
    log "Shiri failed to start; tail of $LOGFILE:"
    tail -80 "$LOGFILE" || true
    exit 1
  fi
  log "Shiri started as pid $pid"
}

stop_service() {
  stop_app
  cleanup_runtime
  log "Shiri stopped"
}

status_service() {
  local pid
  pid="$(pidfile_pid)"
  if is_running "$pid"; then
    log "App running as pid $pid"
  else
    local pids
    pids="$(app_pids | sort -u)"
    if [[ -n "$pids" ]]; then
      log "App running without pidfile: ${pids//$'\n'/ }"
    else
      log "App not running"
    fi
  fi
  log "Namespaces:"
  ip netns list | grep -E '^shiri_(ot|rx_)' || true
  log "Processes:"
  ps -eo pid,ppid,args | grep -E 'app.py|audio_mixer.py|shairport-sync|owntone|nqptp|airptpd' | grep -v grep || true
}

main() {
  need_root "$@"
  case "${1:-}" in
    start) start_service ;;
    stop) stop_service ;;
    restart) stop_service; start_service ;;
    status) status_service ;;
    cleanup) cleanup_runtime ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
