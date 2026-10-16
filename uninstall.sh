#!/usr/bin/env bash
# Removes everything install.sh added. The backups in
# config/klipper-ip-status-backup and the cloned folder are NOT deleted.
#   ./uninstall.sh [--no-restart] [--force]
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

RESTART=1; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    --force)      FORCE=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $RESTART -eq 1 && $FORCE -eq 0 ]] && is_printing; then
  echo "A print is in progress. Uninstalling restarts Klipper and would abort it." >&2
  echo "Run again when the printer is idle (or use --force)." >&2
  exit 1
fi

# Only a link that points to this package is removed
if [[ -L "$COMPONENT_LINK" && "$(readlink -f "$COMPONENT_LINK")" == "$SRC_REAL/src/network_info.py" ]]; then
  rm -f "$COMPONENT_LINK"
elif [[ -e "$COMPONENT_LINK" || -L "$COMPONENT_LINK" ]]; then
  echo "Left in place (not a link to this package): $COMPONENT_LINK" >&2
fi

rm -f "$MACRO_DST"
rmdir "$DEST_DIR" 2>/dev/null || true   # only removed if empty

# The include line is only removed from printer.cfg, where the installer puts it
sed -i "\|^\\[include $CONFIG_SUBDIR/network_info.cfg\\]\$|d" "$PCFG"
if [[ -f "$CONF" ]]; then remove_conf_block; fi

if [[ $RESTART -eq 1 ]]; then
  sudo systemctl restart moonraker klipper
  echo "Removed. The cloned folder and config/klipper-ip-status-backup were not deleted."
else
  echo "Removed without restarting. Restart Moonraker and Klipper to apply the changes."
fi
