# Shared by install.sh and uninstall.sh. Sourced, never run directly.
#
# Optional environment variables:
#   PRINTER_DATA    Klipper data folder            (default ~/printer_data)
#   MOONRAKER_DIR   Moonraker source folder        (default ~/moonraker)
#   CONFIG_SUBDIR   folder inside the Klipper config where the macro file goes
#                   (default "network")
#   MOONRAKER_URL   Moonraker address              (default http://127.0.0.1:7125)

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_REAL="$(readlink -f "$SRC")"

# Runs as the normal user: root would leave root-owned files in the Klipper
# config. sudo is only used to restart the services. ALLOW_ROOT=1 is for tests.
if [[ $EUID -eq 0 && "${ALLOW_ROOT:-}" != 1 ]]; then
  echo "Run this as your normal user, without sudo (it asks for sudo only to restart the services)." >&2
  exit 1
fi

PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"
MOONRAKER_DIR="${MOONRAKER_DIR:-$HOME/moonraker}"
CONFIG_SUBDIR="${CONFIG_SUBDIR:-network}"
MOONRAKER_URL="${MOONRAKER_URL:-http://127.0.0.1:7125}"

# A simple relative folder name: it ends up inside an [include ...] line
if ! [[ "$CONFIG_SUBDIR" =~ ^[A-Za-z0-9_][A-Za-z0-9_/-]*$ && "$CONFIG_SUBDIR" != */ ]]; then
  echo "CONFIG_SUBDIR must be a simple relative folder name (letters, digits, _ - /)." >&2
  exit 1
fi

CONFIG_DIR="$PRINTER_DATA/config"
DEST_DIR="$CONFIG_DIR/$CONFIG_SUBDIR"
MACRO_DST="$DEST_DIR/network_info.cfg"
COMPONENT_LINK="$MOONRAKER_DIR/moonraker/components/network_info.py"
CONF="$CONFIG_DIR/moonraker.conf"
PCFG="$CONFIG_DIR/printer.cfg"
BACKUP_DIR="$CONFIG_DIR/klipper-ip-status-backup"
INCLUDE_LINE="[include $CONFIG_SUBDIR/network_info.cfg]"
BEGIN="# >>> klipper-ip-status >>>"
END="# <<< klipper-ip-status <<<"

# True while a print is running/paused (restarting Klipper would abort it)
is_printing() {
  command -v curl >/dev/null || return 1
  local st
  st="$(curl -s --max-time 3 "$MOONRAKER_URL/printer/objects/query?print_stats=state" \
        | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p' || true)"
  [[ "$st" == printing || "$st" == paused ]]
}

# Removes our block from moonraker.conf, and the blank lines left at the end
remove_conf_block() {
  sed -i "/$BEGIN/,/$END/d" "$CONF"
  sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$CONF"
}
