#!/usr/bin/env bash
# Tests install.sh and uninstall.sh on a fake machine: no Klipper, no Moonraker,
# no root, and nothing outside a temporary folder is touched. Run from anywhere:
#
#   bash tests/test_install.sh
#
# The main thing checked is that the user's own files (printer.cfg,
# moonraker.conf) are never damaged and come back byte-for-byte after uninstall.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
check() { local d="$1"; shift; if "$@"; then ok "$d"; else bad "$d"; fi; }
check_not() { local d="$1"; shift; if "$@"; then bad "$d"; else ok "$d"; fi; }
section() { printf '\n%s\n' "$1"; }

# ---------------------------------------------------------------- the machine
EXTRA=()          # extra environment variables for the next run
ALLOW_ROOT_VAL=1
PRINT_STATE=standby

new_machine() {
  M="$T/$1"; rm -rf "$M"
  mkdir -p "$M/pd/config" "$M/mr/moonraker/components" "$M/bin" "$M/home"
  cat >"$M/pd/config/printer.cfg" <<'EOF'
[include mainsail.cfg]

[stepper_x]
step_pin: PA0

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [extruder]
#*# pid_kp = 21.5
EOF
  cat >"$M/pd/config/moonraker.conf" <<'EOF'
[server]
host: 0.0.0.0
port: 7125

[authorization]
trusted_clients:
    192.168.0.0/16
EOF
  # a copy of this repository, as if it had been cloned from GitHub
  mkdir "$M/repo"
  (cd "$REPO" && tar --exclude=.git --exclude=__pycache__ -cf - .) | tar -xf - -C "$M/repo"
  git -C "$M/repo" init -q -b main
  git -C "$M/repo" remote add origin https://example.invalid/klipper-ip-status.git
  # stubs: nothing real is restarted, queried or waited for
  printf '#!/bin/sh\nexec "$@"\n' >"$M/bin/sudo"
  printf '#!/bin/sh\necho "$@" >>"%s/systemctl.log"\n' "$M" >"$M/bin/systemctl"
  printf '#!/bin/sh\nexit 0\n' >"$M/bin/ip"
  printf '#!/bin/sh\nexit 0\n' >"$M/bin/sleep"
  cat >"$M/bin/curl" <<'EOF'
#!/bin/sh
case "$*" in
  *print_stats*) echo "{\"result\":{\"status\":{\"print_stats\":{\"state\": \"${PRINT_STATE:-standby}\"}}}}" ;;
  *) echo '{"result":{"state": "ready"}}' ;;
esac
EOF
  chmod +x "$M"/bin/*
  save_originals
  EXTRA=(); ALLOW_ROOT_VAL=1; PRINT_STATE=standby
}

save_originals() {
  cp "$M/pd/config/printer.cfg" "$M/printer.orig"
  cp "$M/pd/config/moonraker.conf" "$M/moonraker.orig"
}

run() {   # run <script> [args]: output goes to $M/out.log, returns its exit code
  local script="$1"; shift
  ( cd "$M" && env PATH="$M/bin:$PATH" HOME="$M/home" PRINTER_DATA="$M/pd" \
      MOONRAKER_DIR="$M/mr" ALLOW_ROOT="$ALLOW_ROOT_VAL" PRINT_STATE="$PRINT_STATE" \
      ${EXTRA[@]+"${EXTRA[@]}"} bash "$M/repo/$script" "$@" ) >"$M/out.log" 2>&1
}

snap() {   # a fingerprint of every file and link of the fake machine
  ( cd "$M" && find pd mr \( -type f -o -type l \) | sort | while read -r f; do
      printf '%s ' "$f"
      if [ -L "$f" ]; then readlink "$f"; else cksum <"$f"; fi
    done )
}

LINK() { echo "$M/mr/moonraker/components/network_info.py"; }
CONF() { echo "$M/pd/config/moonraker.conf"; }
PCFG() { echo "$M/pd/config/printer.cfg"; }
restarts() { cat "$M/systemctl.log" 2>/dev/null || true; }
same() { diff -q "$1" "$2" >/dev/null; }
points_to_repo() { [[ "$(readlink -f "$1")" == "$(readlink -f "$M/repo/src/network_info.py")" ]]; }
has() { grep -qF -- "$1" "$2"; }
count() { grep -cE -- "$1" "$2" || true; }

# =========================================================================
section "First install"
new_machine first
run install.sh; rc=$?
check "exits 0" test "$rc" -eq 0
check "component is a link to the repository" points_to_repo "$(LINK)"
check "macro file installed in network/" same "$M/repo/src/network_info.cfg" "$M/pd/config/network/network_info.cfg"
check "printer.cfg starts with the include" test "$(head -n1 "$(PCFG)")" = "[include network/network_info.cfg]"
check "rest of printer.cfg untouched (SAVE_CONFIG block included)" diff -q <(tail -n +2 "$(PCFG)") "$M/printer.orig"
check "moonraker.conf has [network_info]" has "[network_info]" "$(CONF)"
check "moonraker.conf has the update manager entry" has "[update_manager klipper-ip-status]" "$(CONF)"
check "update manager origin is the clone's origin" has "origin: https://example.invalid/klipper-ip-status.git" "$(CONF)"
check "update manager branch is main" has "primary_branch: main" "$(CONF)"
check "original moonraker.conf lines kept" bash -c "grep -qF 'trusted_clients:' '$(CONF)' && grep -qF '192.168.0.0/16' '$(CONF)'"
check "backup of printer.cfg is the original" same "$M/pd/config/klipper-ip-status-backup/printer.cfg.bak" "$M/printer.orig"
check "backup of moonraker.conf is the original" same "$M/pd/config/klipper-ip-status-backup/moonraker.conf.bak" "$M/moonraker.orig"
check "first install restarts moonraker and klipper" test "$(restarts)" = "restart moonraker klipper"
SNAP1="$(snap)"

section "Second install (idempotent)"
run install.sh; rc=$?
check "exits 0" test "$rc" -eq 0
check "nothing changed, byte for byte" test "$(snap)" = "$SNAP1"
check "only moonraker restarted this time" test "$(restarts)" = $'restart moonraker klipper\nrestart moonraker'

section "Macro changed in a new version"
echo "# old version" >>"$M/pd/config/network/network_info.cfg"
run install.sh; rc=$?
check "exits 0" test "$rc" -eq 0
check "macro file restored from the repository" same "$M/repo/src/network_info.cfg" "$M/pd/config/network/network_info.cfg"
check "tells the user to restart Klipper" has "The Klipper macro changed" "$M/out.log"

section "Uninstall"
run uninstall.sh; rc=$?
check "exits 0" test "$rc" -eq 0
check "printer.cfg back to the original" same "$(PCFG)" "$M/printer.orig"
check "moonraker.conf back to the original" same "$(CONF)" "$M/moonraker.orig"
check_not "component link removed" test -e "$(LINK)" -o -L "$(LINK)"
check_not "macro file removed" test -e "$M/pd/config/network/network_info.cfg"
check_not "empty network/ folder removed" test -d "$M/pd/config/network"
check "backups are kept" test -f "$M/pd/config/klipper-ip-status-backup/printer.cfg.bak"
run install.sh; rc=$?
check "installs again after an uninstall" test "$rc" -eq 0 -a "$(head -n1 "$(PCFG)")" = "[include network/network_info.cfg]"

# =========================================================================
section "Refusals leave no trace"
new_machine conflict
printf '[gcode_macro SHOW_IP_ADDRESS]\ngcode:\n  M117 mine\n' >"$M/pd/config/other.cfg"
BEFORE="$(snap)"
run install.sh; rc=$?
check "macro with the same name: refuses" test "$rc" -ne 0
check "  ...and says which file" has "other.cfg" "$M/out.log"
check "  ...and changes nothing" test "$(snap)" = "$BEFORE"

new_machine foreign
echo "someone else's component" >"$(LINK)"
BEFORE="$(snap)"
run install.sh; rc=$?
check "foreign component at the link path: refuses" test "$rc" -ne 0
check "  ...and changes nothing" test "$(snap)" = "$BEFORE"

new_machine badsubdir
EXTRA=(CONFIG_SUBDIR=../x)
BEFORE="$(snap)"
run install.sh; rc=$?
check "unsafe CONFIG_SUBDIR: refuses" test "$rc" -ne 0
check "  ...and changes nothing" test "$(snap)" = "$BEFORE"

new_machine missing
rm "$M/pd/config/moonraker.conf"
run install.sh; rc=$?
check "missing moonraker.conf: refuses" test "$rc" -ne 0

if [[ $EUID -eq 0 ]]; then
  new_machine root
  ALLOW_ROOT_VAL=""
  run install.sh; rc=$?
  check "running as root: refuses" test "$rc" -ne 0
  check "  ...and says why" has "normal user" "$M/out.log"
fi

# =========================================================================
section "The user's own configuration is respected"
new_machine handsection
printf '\n[network_info]\nlabel_usb: Hotspot\n' >>"$M/pd/config/moonraker.conf"
save_originals
run install.sh; rc=$?
check "exits 0" test "$rc" -eq 0
check "[network_info] written by hand is not duplicated" test "$(count '^\[network_info\]' "$(CONF)")" -eq 1
check "their option is still there" has "label_usb: Hotspot" "$(CONF)"
run uninstall.sh
check "uninstall gives their moonraker.conf back" same "$(CONF)" "$M/moonraker.orig"

new_machine otherinclude
printf '[include network/network_info.cfg]\n' >"$M/pd/config/extra.cfg"
run install.sh; rc=$?
check "include already in another file: exits 0" test "$rc" -eq 0
check "  ...printer.cfg is not touched" same "$(PCFG)" "$M/printer.orig"

new_machine basesection
printf '\n[update_manager]\nrefresh_interval: 168\n' >>"$M/pd/config/moonraker.conf"
save_originals
run install.sh
check "existing [update_manager] is not repeated" test "$(count '^\[update_manager\]' "$(CONF)")" -eq 1
run uninstall.sh
check "uninstall gives moonraker.conf back" same "$(CONF)" "$M/moonraker.orig"

new_machine nogit
rm -rf "$M/repo/.git"; git -C "$M/repo" init -q -b main
run install.sh; rc=$?
check "folder without a git origin: exits 0" test "$rc" -eq 0
check "  ...no update manager entry" bash -c "! grep -qF 'update_manager' '$(CONF)'"

new_machine subdir
EXTRA=(CONFIG_SUBDIR=system/net)
run install.sh
check "custom CONFIG_SUBDIR: include line" test "$(head -n1 "$(PCFG)")" = "[include system/net/network_info.cfg]"
check "  ...macro in that folder" test -f "$M/pd/config/system/net/network_info.cfg"
run uninstall.sh
check "  ...uninstall gives printer.cfg back" same "$(PCFG)" "$M/printer.orig"

# =========================================================================
section "Print in progress / no restart"
new_machine printing
PRINT_STATE=printing
run install.sh; rc=$?
check "install during a print: exits 0" test "$rc" -eq 0
check "  ...restarts nothing" test -z "$(restarts)"
check "  ...tells the user" has "A print is in progress" "$M/out.log"
BEFORE="$(snap)"
run uninstall.sh; rc=$?
check "uninstall during a print: refuses" test "$rc" -ne 0
check "  ...and changes nothing" test "$(snap)" = "$BEFORE"
run uninstall.sh --force; rc=$?
check "uninstall --force during a print: goes ahead" test "$rc" -eq 0 -a ! -e "$M/pd/config/network/network_info.cfg"

new_machine norestart
run install.sh --no-restart; rc=$?
check "--no-restart: exits 0 and restarts nothing" test "$rc" -eq 0 -a -z "$(restarts)"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
