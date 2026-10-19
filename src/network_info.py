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
#   title: IP addresses of this machine
#   label_wifi: Wi-Fi
#   label_wired: Cable
#   label_usb: USB
#   text_no_link: not connected
#   text_connecting: getting an address
#   text_no_interfaces: no network interface found
#   text_error: could not read the network
#   label_internet: Internet
#   text_no_route: no route
#   text_then: then
#   text_or: or
#   label_vpn: VPN
#   show_vpn: False        (True lists VPN tunnels such as Tailscale)
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
IP_ADDRESS_COMMAND = ("ip", "-j", "address")
IP_ROUTE_COMMAND = ("ip", "-j", "route", "show", "default")
IP_TIMEOUT = 5.0            # seconds before giving up on an `ip` command
ARPHRD_ETHER = "1"          # /sys/class/net/<if>/type of Ethernet and Wi-Fi
ARPHRD_NONE = "65534"       # type of tunnels: Tailscale, WireGuard, OpenVPN tun
USB_PATH = re.compile(r"/usb\d+/")   # path component of a USB bus in sysfs
# Drivers of USB tethering (phone / pocket router as a network card). Any other
# Ethernet driver is a cable, even when the chip hangs off the USB bus (the
# Ethernet port of a Raspberry Pi 3 and a USB-to-RJ45 adapter are both USB).
TETHER_DRIVERS = frozenset({
    "cdc_ether", "rndis_host", "cdc_ncm", "cdc_eem", "ipheth",
    "huawei_cdc_ncm",
})

# Order in which the kinds are listed in the console
KIND_WIFI, KIND_WIRED, KIND_USB, KIND_VPN = "wifi", "wired", "usb", "vpn"
KIND_ORDER = (KIND_WIFI, KIND_WIRED, KIND_USB, KIND_VPN)


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
    Wi-Fi is a wireless interface. USB means tethering, recognised by its
    driver (see TETHER_DRIVERS); every other Ethernet interface is Cable.
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
        if USB_PATH.search(os.path.realpath(base)):
            # On USB, the driver tells tethering (USB) from a wired adapter
            # (Cable). If the driver cannot be read, keep the safe answer: USB.
            driver = base / "device" / "driver"
            if not driver.exists():
                return KIND_USB
            name = os.path.basename(os.path.realpath(driver))
            return KIND_USB if name in TETHER_DRIVERS else KIND_WIRED
        return KIND_WIRED
    except OSError:
        return None


def is_vpn(ifname: str, sysfs: pathlib.Path = SYSFS_NET) -> bool:
    """True for a tunnel interface (no physical device, type 65534).

    Kept apart from classify() on purpose: tunnels are not a way to reach the
    machine from the local network, so they never appear in the Internet line.
    """
    base = sysfs / ifname
    try:
        return (not (base / "device").exists()
                and (base / "type").read_text().strip() == ARPHRD_NONE)
    except OSError:
        return False


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


def parse_default_routes(raw: str) -> Dict[str, int]:
    """Parse the output of `ip -j route show default` into {ifname: metric}.

    The system sends traffic through the default route with the LOWEST metric.
    An interface with several default routes keeps its lowest metric. Routes
    on a link that is down are ignored. A missing metric means 0, which is how
    iproute2 prints it.
    """
    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise NetworkInfoError(f"invalid output from ip: {e}") from e
    routes: Dict[str, int] = {}
    for route in decoded:
        dev = route.get("dev")
        if route.get("dst") != "default" or not dev:
            continue
        if "linkdown" in route.get("flags", []):
            continue
        try:
            metric = int(route.get("metric", 0))
        except (TypeError, ValueError):
            metric = 0
        routes[dev] = min(metric, routes.get(dev, metric))
    return routes


def build_lines(
    interfaces: Dict[str, dict],
    labels: Dict[str, str],
    texts: Dict[str, str],
    sysfs: pathlib.Path = SYSFS_NET,
    show_vpn: bool = False,
) -> List[str]:
    """One console line per physical interface: Wi-Fi, then cable, then USB.
    With show_vpn, tunnels (Tailscale, WireGuard...) are listed last."""
    found: Dict[str, List[str]] = {k: [] for k in KIND_ORDER}
    for name in sorted(interfaces):
        kind = classify(name, sysfs)
        if kind is None and show_vpn and is_vpn(name, sysfs):
            kind = KIND_VPN
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


def build_internet_line(
    routes: Dict[str, int],
    labels: Dict[str, str],
    texts: Dict[str, str],
    sysfs: pathlib.Path = SYSFS_NET,
) -> str:
    """The order in which the system prefers the connections, for example
    "Internet: USB (eth1), then Wi-Fi (wlan0)".

    This is the CONFIGURED order (lowest metric first), not a test that the
    connection works: a hotspot without data is still listed first if its
    metric is the lowest. Interfaces with the same metric are joined with
    "or" because the system does not guarantee which of them it picks.
    Only physical interfaces are listed (a VPN is not).
    """
    by_metric: Dict[int, List[str]] = {}
    for name, metric in routes.items():
        kind = classify(name, sysfs)
        if kind is None:
            continue
        by_metric.setdefault(metric, []).append(f"{labels[kind]} ({name})")
    if not by_metric:
        return f"{texts['internet']}: {texts['no_route']}"
    groups = [
        f" {texts['or']} ".join(sorted(by_metric[metric]))
        for metric in sorted(by_metric)
    ]
    return f"{texts['internet']}: " + f", {texts['then']} ".join(groups)


# ---------------------------------------------------------------- component
class NetworkInfo:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.title = _clean(config.get("title", "IP addresses of this machine"))
        self.labels = {
            KIND_WIFI: _clean(config.get("label_wifi", "Wi-Fi")),
            KIND_WIRED: _clean(config.get("label_wired", "Cable")),
            KIND_USB: _clean(config.get("label_usb", "USB")),
            KIND_VPN: _clean(config.get("label_vpn", "VPN")),
        }
        self.show_vpn = config.getboolean("show_vpn", False)
        self.texts = {
            "no_link": _clean(config.get("text_no_link", "not connected")),
            "connecting": _clean(
                config.get("text_connecting", "getting an address")
            ),
            "no_interfaces": _clean(config.get(
                "text_no_interfaces", "no network interface found"
            )),
            "error": _clean(
                config.get("text_error", "could not read the network")
            ),
            "internet": _clean(config.get("label_internet", "Internet")),
            "no_route": _clean(config.get("text_no_route", "no route")),
            "then": _clean(config.get("text_then", "then")),
            "or": _clean(config.get("text_or", "or")),
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

    async def _run_ip(self, command: tuple) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
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
        return out.decode(errors="replace")

    async def _read_interfaces(self) -> Dict[str, dict]:
        return parse_addresses(await self._run_ip(IP_ADDRESS_COMMAND))

    async def _read_routes(self) -> Optional[Dict[str, int]]:
        # The Internet line is an extra: if the routes cannot be read, the line
        # is left out and everything else is still shown. None = unknown, as
        # opposed to {} = the machine has no default route.
        try:
            return parse_default_routes(await self._run_ip(IP_ROUTE_COMMAND))
        except NetworkInfoError as e:
            logging.info(f"network_info: no route information: {e}")
        except Exception:
            logging.exception("network_info: unexpected error reading routes")
        return None

    # ----------------------------------------------------------- handler
    async def _on_show_request(self) -> None:
        try:
            interfaces, routes = await asyncio.gather(
                self._read_interfaces(), self._read_routes()
            )
            lines = build_lines(
                interfaces, self.labels, self.texts, show_vpn=self.show_vpn
            )
            if lines and routes is not None:
                lines.append(
                    build_internet_line(routes, self.labels, self.texts)
                )
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
