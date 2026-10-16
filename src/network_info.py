# Moonraker component: network_info
#
# Called by the SHOW_IP_ADDRESS macro (see src/network_info.cfg). Reads the IP
# address of every physical network interface (Wi-Fi, cable, USB) and prints
# one line per interface in the Mainsail / KlipperScreen console.
#
# The interface list is read fresh on every call (`ip -j address`), so a
# hotspot plugged in a few seconds ago is already included. Nothing is cached
# and nothing is changed on the machine: this component only reads.
#
# moonraker.conf (every option is optional, these are the defaults):
#   [network_info]
#   title: Endereços IP da máquina
#   label_wifi: Wi-Fi
#   label_wired: Cabo
#   label_usb: USB
#   text_no_link: sem ligação
#   text_connecting: a obter endereço
#   text_no_interfaces: nenhuma placa de rede encontrada
#   text_error: não foi possível ler a rede
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from confighelper import ConfigHelper

SYSFS_NET = pathlib.Path("/sys/class/net")
IP_COMMAND = ("ip", "-j", "address")
IP_TIMEOUT = 5.0            # seconds before giving up on the `ip` command
ARPHRD_ETHER = "1"          # /sys/class/net/<if>/type of Ethernet and Wi-Fi
USB_PATH = re.compile(r"/usb\d+/")   # path component of a USB bus in sysfs

# Order in which the kinds are listed in the console
KIND_WIFI, KIND_WIRED, KIND_USB = "wifi", "wired", "usb"
KIND_ORDER = (KIND_WIFI, KIND_WIRED, KIND_USB)


class NetworkInfoError(Exception):
    """Error with a message that is safe to show to the user."""


def _clean(text: str) -> str:
    # RESPOND MSG="..." does not tolerate quotes, braces, line breaks or ";"
    # (Klipper treats ";" as the start of a comment and cuts the line there)
    for ch in '"{};\n\r\\':
        text = text.replace(ch, " ")
    return text.strip()


# --------------------------------------------------------------- pure logic
def classify(ifname: str, sysfs: pathlib.Path = SYSFS_NET) -> Optional[str]:
    """Return "wifi", "wired" or "usb" for a physical interface, else None.

    Decided from sysfs and not from the interface name, so it keeps working
    when the system names interfaces differently (eth1, enx..., usb0, end0).
    Virtual interfaces (loopback, VPN, bridges, containers, CAN) return None.
    """
    base = sysfs / ifname
    try:
        # No "device" link = virtual interface (lo, tailscale0, docker0...)
        if not (base / "device").exists():
            return None
        if (base / "type").read_text().strip() != ARPHRD_ETHER:
            return None
        if (base / "wireless").exists():
            return KIND_WIFI
        # A USB network adapter lives under a .../usbN/... device path
        if USB_PATH.search(os.path.realpath(base)):
            return KIND_USB
        return KIND_WIRED
    except OSError:
        return None


def parse_addresses(raw: str) -> Dict[str, dict]:
    """Parse the output of `ip -j address`.

    Returns {ifname: {"up": bool, "ips": [ipv4, ...]}}. IPv6 is ignored and
    so are link-local IPv4 addresses (169.254.x.x): those mean "no DHCP lease",
    not a usable address.
    """
    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise NetworkInfoError(f"invalid output from ip: {e}") from e
    result: Dict[str, dict] = {}
    for iface in decoded:
        name = iface.get("ifname")
        if not name:
            continue
        ips = [
            a["local"]
            for a in iface.get("addr_info", [])
            if a.get("family") == "inet"
            and a.get("local")
            and a.get("scope") != "link"
        ]
        result[name] = {
            "up": iface.get("operstate", "").upper() in ("UP", "UNKNOWN")
                  and "NO-CARRIER" not in iface.get("flags", []),
            "ips": ips,
        }
    return result


def build_lines(
    interfaces: Dict[str, dict],
    labels: Dict[str, str],
    texts: Dict[str, str],
    sysfs: pathlib.Path = SYSFS_NET,
) -> List[str]:
    """One console line per physical interface: Wi-Fi, then cable, then USB."""
    found: Dict[str, List[str]] = {k: [] for k in KIND_ORDER}
    for name in sorted(interfaces):
        kind = classify(name, sysfs)
        if kind is None:
            continue
        info = interfaces[name]
        if info["ips"]:
            state = ", ".join(info["ips"])
        elif info["up"]:
            state = texts["connecting"]
        else:
            state = texts["no_link"]
        found[kind].append(f"{labels[kind]} ({name}): {state}")
    return [line for kind in KIND_ORDER for line in found[kind]]


# ---------------------------------------------------------------- component
class NetworkInfo:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.title = _clean(config.get("title", "Endereços IP da máquina"))
        self.labels = {
            KIND_WIFI: _clean(config.get("label_wifi", "Wi-Fi")),
            KIND_WIRED: _clean(config.get("label_wired", "Cabo")),
            KIND_USB: _clean(config.get("label_usb", "USB")),
        }
        self.texts = {
            "no_link": _clean(config.get("text_no_link", "sem ligação")),
            "connecting": _clean(
                config.get("text_connecting", "a obter endereço")
            ),
            "no_interfaces": _clean(config.get(
                "text_no_interfaces", "nenhuma placa de rede encontrada"
            )),
            "error": _clean(
                config.get("text_error", "não foi possível ler a rede")
            ),
        }
        self._log_version()
        self.server.register_remote_method(
            "network_info_show", self._on_show_request
        )

    def _log_version(self) -> None:
        # Lets you prove which code Moonraker is really running
        here = pathlib.Path(__file__).resolve()
        try:
            version = (here.parent.parent / "VERSION").read_text().strip()
        except OSError:
            version = "unknown"
        logging.info(f"network_info: version {version} loaded from {here}")

    async def _respond(self, lines: List[str], error: bool = False) -> None:
        kapis = self.server.lookup_component("klippy_apis")
        kind = " TYPE=error" if error else ""
        # One script, one RESPOND per line: the lines arrive together and in order
        script = "\n".join(
            f'RESPOND{kind} MSG="{_clean(line)}"' for line in lines if line
        )
        try:
            await kapis.run_gcode(script)
        except self.server.error as e:
            logging.info(f"network_info: could not notify Klipper: {e}")

    async def _read_interfaces(self) -> Dict[str, dict]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *IP_COMMAND,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise NetworkInfoError(f"could not run ip: {e.strerror}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(), IP_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise NetworkInfoError("ip did not answer in time")
        if proc.returncode != 0:
            raise NetworkInfoError(
                f"ip failed: {err.decode(errors='replace').strip()}"
            )
        return parse_addresses(out.decode(errors="replace"))

    # ----------------------------------------------------------- handler
    async def _on_show_request(self) -> None:
        try:
            interfaces = await self._read_interfaces()
            lines = build_lines(interfaces, self.labels, self.texts)
        except NetworkInfoError as e:
            logging.info(f"network_info: {e}")
            await self._respond([f"{self.title}: {self.texts['error']}"], True)
            return
        except Exception:
            logging.exception("network_info: unexpected error")
            await self._respond([f"{self.title}: {self.texts['error']}"], True)
            return
        if not lines:
            lines = [self.texts["no_interfaces"]]
        await self._respond([f"{self.title}:"] + lines)


def load_component(config: ConfigHelper) -> NetworkInfo:
    return NetworkInfo(config)
