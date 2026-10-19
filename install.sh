#!/usr/bin/env bash
# Installer for klipper-ip-status. Idempotent: safe to run several times.
#   ./install.sh [--no-restart]
# Environment variables: see common.sh
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RESTART=1; [[ "${1:-}" == "--no-restart" ]] && RESTART=0

backup() {   # keeps the FIRST (original) copy of a file, never overwrites it
  local f="$1" base
  base="$(basename "$f")"
  mkdir -p "$BACKUP_DIR"
  [[ -e "$BACKUP_DIR/$base.bak" ]] || cp -p "$f" "$BACKUP_DIR/$base.bak"
}

# ---------------------------------------------------------------- checks
# Everything is checked before the first change: a refusal leaves no trace.
for p in "$PCFG" "$CONF" "$MOONRAKER_DIR/moonraker/components"; do
  [[ -e "$p" ]] || { echo "Not found: $p" >&2
                     echo "(override with PRINTER_DATA=... or MOONRAKER_DIR=...)" >&2; exit 1; }
done

# Another definition of SHOW_IP_ADDRESS would make Klipper refuse to start
CONFLICTS="$(grep -rIliE '^\[gcode_macro[[:space:]]+SHOW_IP_ADDRESS\]' "$CONFIG_DIR" \
             --include='*.cfg' 2>/dev/null | grep -vxF "$MACRO_DST" || true)"
if [[ -n "$CONFLICTS" ]]; then
  echo "ERROR: SHOW_IP_ADDRESS is already defined in your Klipper config:" >&2
  echo "$CONFLICTS" | sed 's/^/  - /' >&2
  echo "Remove or rename it first, then run the installer again. Nothing was changed." >&2
  exit 1
fi

# Never overwrite a component that is not ours
if [[ -e "$COMPONENT_LINK" || -L "$COMPONENT_LINK" ]] \
   && [[ "$(readlink -f "$COMPONENT_LINK")" != "$SRC_REAL/src/network_info.py" ]]; then
  echo "ERROR: $COMPONENT_LINK already exists and is not a link to this package." >&2
  echo "Move it away first, then run the installer again. Nothing was changed." >&2
  exit 1
fi

# A [network_info] section the user wrote by hand is kept, not duplicated
OUTSIDE_BLOCK="$(sed "/$BEGIN/,/$END/d" "$CONF")"
HAVE_SECTION=0
if grep -qE '^\[network_info\][[:space:]]*$' <<<"$OUTSIDE_BLOCK"; then HAVE_SECTION=1; fi

command -v ip >/dev/null || \
  echo "WARNING: 'ip' was not found (package iproute2). The macro will report an error until it is installed." >&2

FIRST=1;         [[ -f "$MACRO_DST" ]] && FIRST=0
MACRO_CHANGED=0; cmp -s "$SRC/src/network_info.cfg" "$MACRO_DST" 2>/dev/null || MACRO_CHANGED=1

# --------------------------------------------------------------- install
echo "==> Moonraker component"
# Linked to the repository: a git update from Mainsail replaces the code, and
# the managed Moonraker restart loads it.
ln -sfn "$SRC_REAL/src/network_info.py" "$COMPONENT_LINK"

echo "==> moonraker.conf"
backup "$CONF"
# Options the user set inside our block survive a re-run of the installer
KEPT="$(sed -n "/$BEGIN/,/$END/p" "$CONF" | awk -v end="$END" '
  $0 == end { on = 0 }
  /^\[network_info\][[:space:]]*$/ { on = 1; next }
  /^\[/ { on = 0 }
  on && NF { print }')"
remove_conf_block
BODY=""
if [[ $HAVE_SECTION -eq 0 ]]; then
  BODY+=$'[network_info]\n'
  [[ -z "$KEPT" ]] || BODY+="$KEPT"$'\n'
  # Written out so it is easy to find and change; False = no VPN line
  grep -qE '^show_vpn[[:space:]]*[:=]' <<<"$KEPT" || BODY+=$'show_vpn: False\n'
fi
# Updates from Mainsail, if this came from a git clone with an origin
if ORIGIN="$(git -C "$SRC" remote get-url origin 2>/dev/null)"; then
  # symbolic-ref prints nothing when detached/unavailable (rev-parse prints "HEAD")
  BRANCH="$(git -C "$SRC" symbolic-ref --short -q HEAD 2>/dev/null || true)"
  [[ -n "$BRANCH" ]] || BRANCH=main
  # The update manager component only loads when the base section exists
  if ! grep -rqE '^\[update_manager\][[:space:]]*$' "$CONFIG_DIR" \
       --include='*.conf' --include='*.cfg' 2>/dev/null; then
    BODY+=$'\n[update_manager]\n'
  fi
  BODY+="$(printf '\n[update_manager klipper-ip-status]\ntype: git_repo\npath: %s\norigin: %s\nprimary_branch: %s\nmanaged_services: moonraker\n' \
           "$SRC_REAL" "$ORIGIN" "$BRANCH")"$'\n'
fi
if [[ -n "$BODY" ]]; then
  { printf '\n%s\n' "$BEGIN"; printf '%s' "$BODY"; printf '%s\n' "$END"; } >> "$CONF"
fi

echo "==> Klipper macro"
install -d "$DEST_DIR"
install -m 644 "$SRC/src/network_info.cfg" "$MACRO_DST"
INCLUDE_ADDED=0
# Looks in every .cfg: the include may already live in one of your own files
if ! grep -rqsF 'network_info.cfg' "$CONFIG_DIR" --include='*.cfg'; then
  backup "$PCFG"
  # At the top: the end of the file belongs to Klipper's SAVE_CONFIG block
  sed -i "1i $INCLUDE_LINE" "$PCFG"
  INCLUDE_ADDED=1
fi
[[ $INCLUDE_ADDED -eq 1 ]] && FIRST=1

# -------------------------------------------------------------- activate
if [[ $RESTART -eq 1 ]] && is_printing; then
  RESTART=0
  echo "A print is in progress: nothing was restarted. Restart Moonraker (and Klipper,"
  echo "if it is the first install or the macro changed) when the printer is idle."
fi

if [[ $RESTART -eq 1 ]]; then
  if [[ $FIRST -eq 1 ]]; then
    sudo systemctl restart moonraker klipper   # first install: the macro needs Klipper
  else
    sudo systemctl restart moonraker           # updates: the component only needs Moonraker
  fi
  echo "==> Waiting for Klipper to report ready (up to 60 s)"
  state=unknown; info=""
  if command -v curl >/dev/null; then
    for _ in $(seq 1 30); do
      sleep 2
      info="$(curl -s --max-time 2 "$MOONRAKER_URL/printer/info" || true)"
      state="$(printf '%s' "$info" | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')"
      [[ "$state" == ready || "$state" == error || "$state" == shutdown ]] && break
      state=unknown
    done
  fi
  case "$state" in
    ready)
      echo "Installed. Type SHOW_IP_ADDRESS in the console to try it." ;;
    error|shutdown)
      echo "WARNING: Klipper reports '$state' after the install." >&2
      echo "$info" | sed -n 's/.*"state_message": *"\([^"]*\)".*/  \1/p' >&2
      echo "If this is caused by this tool, run ./uninstall.sh (backups: config/klipper-ip-status-backup)." >&2 ;;
    *)
      echo "Installed. Could not verify Klipper's state; check Mainsail." ;;
  esac
  if [[ $FIRST -eq 0 && $MACRO_CHANGED -eq 1 ]]; then
    echo "The Klipper macro changed: restart Klipper when the printer is idle."
  fi
else
  echo "Installed/updated without restarting. Restart Moonraker to apply the changes"
  echo "(and Klipper if it is the first install or the macro changed)."
fi
