"""Tests for src/network_info.py using a fake Moonraker and a fake sysfs.
Run from the repository root:

    python3 -m unittest discover -s tests -v

No Moonraker, Klipper or hardware needed.
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import network_info  # noqa: E402

LABELS = {"wifi": "Wi-Fi", "wired": "Cable", "usb": "USB", "vpn": "VPN"}
TEXTS = {"no_link": "not connected", "connecting": "getting an address",
         "internet": "Internet", "no_route": "no route", "then": "then",
         "or": "or"}

# Shape of `ip -j route show default` with a hotspot (eth1) and Wi-Fi (wlan0)
ROUTE_OUTPUT = json.dumps([
    {"dst": "default", "gateway": "192.168.0.1", "dev": "eth1",
     "metric": 100, "flags": []},
    {"dst": "default", "gateway": "192.168.0.1", "dev": "wlan0",
     "metric": 600, "flags": []},
])

# Shape of `ip -j address` on the M-BIND (trimmed to the fields we read)
IP_OUTPUT = json.dumps([
    {"ifname": "lo", "operstate": "UNKNOWN", "flags": ["LOOPBACK", "UP"],
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
    {"ifname": "eth0", "operstate": "DOWN",
     "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "addr_info": []},
    {"ifname": "wlan0", "operstate": "UP",
     "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
     "addr_info": [
         {"family": "inet", "local": "192.168.0.177", "scope": "global"},
         {"family": "inet6", "local": "fe80::1", "scope": "link"}]},
    {"ifname": "tailscale0", "operstate": "UNKNOWN",
     "flags": ["POINTOPOINT", "UP", "LOWER_UP"],
     "addr_info": [{"family": "inet", "local": "100.64.0.9", "scope": "global"}]},
    {"ifname": "eth1", "operstate": "UNKNOWN",
     "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
     "addr_info": [{"family": "inet", "local": "192.168.0.178",
                    "scope": "global"}]},
])


def make_iface(sysfs, name, devpath, type_="1", wireless=False, driver=None):
    """Build /sys/class/net/<name> -> /sys/devices/<devpath>/net/<name>."""
    real = sysfs / "devices" / devpath / "net" / name
    real.mkdir(parents=True)
    (real / "type").write_text(type_ + "\n")
    if wireless:
        (real / "wireless").mkdir()
    (real / "device").symlink_to(real.parent.parent)
    if driver:
        drv = sysfs / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (real.parent.parent / "driver").symlink_to(drv)
    net = sysfs / "class" / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / name).symlink_to(real)


def make_virtual(sysfs, name, type_):
    d = sysfs / "class" / "net" / name   # real directory, no "device" link
    d.mkdir(parents=True)
    (d / "type").write_text(type_ + "\n")


def build_sysfs(root):
    make_virtual(root, "lo", "772")
    make_virtual(root, "tailscale0", "65534")
    make_virtual(root, "docker0", "1")   # Ethernet-like but virtual
    make_iface(root, "wlan0", "platform/soc/mmc0/mmc0:0001", wireless=True)
    make_iface(root, "eth0", "platform/axi/pcie/ethernet")
    make_iface(root, "eth1", "platform/axi/xhci/usb1/1-1/1-1.1/1-1.1.1:1.0",
               driver="cdc_ether")
    return root / "class" / "net"


class ServerError(Exception):
    pass


class FakeServer:
    error = ServerError

    def __init__(self, components=None):
        self.components = components or {}
        self.methods = {}

    def register_remote_method(self, name, cb):
        self.methods[name] = cb

    def lookup_component(self, name):
        if name not in self.components:
            raise ServerError(name)
        return self.components[name]


class FakeConfig:
    def __init__(self, server, **opts):
        self.server, self.opts = server, opts

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        return self.opts.get(key, default)

    def getboolean(self, key, default=None):
        return self.opts.get(key, default)


class FakeKlippyApis:
    def __init__(self, fail=False):
        self.gcodes, self.fail = [], fail

    async def run_gcode(self, script):
        if self.fail:
            raise ServerError("klippy not ready")
        self.gcodes.append(script)


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, delay=0.0):
        self.out, self.err, self.returncode, self.delay = out, err, code, delay
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(self.delay)
        return self.out, self.err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_component(**opts):
    apis = FakeKlippyApis()
    server = FakeServer({"klippy_apis": apis})
    comp = network_info.NetworkInfo(FakeConfig(server, **opts))
    # Routes unknown by default; tests that need them patch _read_routes again
    comp._read_routes = mock.AsyncMock(return_value=None)
    return comp, server, apis


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = build_sysfs(pathlib.Path(self.tmp.name))

    def test_kinds(self):
        c = lambda n: network_info.classify(n, self.net)  # noqa: E731
        self.assertEqual(c("wlan0"), "wifi")
        self.assertEqual(c("eth0"), "wired")
        self.assertEqual(c("eth1"), "usb")

    def test_usb_ethernet_chip_is_cable(self):
        # Raspberry Pi 3: the Ethernet port is a smsc95xx chip on the USB bus
        root = pathlib.Path(self.tmp.name)
        make_iface(root, "eth5", "platform/soc/3f980000.usb/usb1/1-1/1-1.1/1-1.1:1.0",
                   driver="smsc95xx")
        self.assertEqual(network_info.classify("eth5", self.net), "wired")

    def test_every_tether_driver_is_usb(self):
        root = pathlib.Path(self.tmp.name)
        for i, drv in enumerate(sorted(network_info.TETHER_DRIVERS)):
            name = f"enx{i}"
            make_iface(root, name, f"platform/soc/usb1/1-{i}/1-{i}:1.0",
                       driver=drv)
            self.assertEqual(network_info.classify(name, self.net), "usb", drv)

    def test_usb_with_unreadable_driver_stays_usb(self):
        root = pathlib.Path(self.tmp.name)
        make_iface(root, "eth6", "platform/soc/usb1/1-9/1-9:1.0")
        self.assertEqual(network_info.classify("eth6", self.net), "usb")

    def test_virtual_interfaces_are_ignored(self):
        for name in ("lo", "tailscale0", "docker0", "does-not-exist"):
            self.assertIsNone(network_info.classify(name, self.net), name)

    def test_name_does_not_decide(self):
        # A USB adapter that the system calls enx<MAC> is still USB
        make_iface(pathlib.Path(self.tmp.name), "enx344b50000000",
                   "platform/axi/xhci/usb3/3-1/3-1:1.0")
        self.assertEqual(network_info.classify("enx344b50000000", self.net),
                         "usb")


class ParseRoutesTests(unittest.TestCase):
    def test_lowest_metric_per_interface(self):
        self.assertEqual(network_info.parse_default_routes(ROUTE_OUTPUT),
                         {"eth1": 100, "wlan0": 600})

    def test_interface_keeps_its_lowest_metric(self):
        raw = json.dumps([{"dst": "default", "dev": "eth0", "metric": 700},
                          {"dst": "default", "dev": "eth0", "metric": 100}])
        self.assertEqual(network_info.parse_default_routes(raw), {"eth0": 100})

    def test_linkdown_route_is_ignored(self):
        raw = json.dumps([{"dst": "default", "dev": "eth0", "metric": 100,
                           "flags": ["linkdown"]}])
        self.assertEqual(network_info.parse_default_routes(raw), {})

    def test_missing_metric_is_zero(self):
        raw = json.dumps([{"dst": "default", "dev": "eth0"}])
        self.assertEqual(network_info.parse_default_routes(raw), {"eth0": 0})

    def test_empty_means_no_default_route(self):
        self.assertEqual(network_info.parse_default_routes("[]"), {})

    def test_garbage_raises(self):
        with self.assertRaises(network_info.NetworkInfoError):
            network_info.parse_default_routes("not json")


class VpnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = build_sysfs(pathlib.Path(self.tmp.name))

    def lines(self, show_vpn):
        ifaces = network_info.parse_addresses(IP_OUTPUT)
        return network_info.build_lines(ifaces, LABELS, TEXTS, self.net,
                                        show_vpn=show_vpn)

    def test_is_vpn_only_for_tunnels(self):
        v = lambda n: network_info.is_vpn(n, self.net)  # noqa: E731
        self.assertTrue(v("tailscale0"))
        for name in ("lo", "docker0", "wlan0", "eth0", "eth1", "nope"):
            self.assertFalse(v(name), name)

    def test_hidden_by_default(self):
        self.assertFalse(any("VPN" in x for x in self.lines(False)))

    def test_listed_last_when_enabled(self):
        out = self.lines(True)
        self.assertEqual(out[-1], "VPN (tailscale0): 100.64.0.9")
        self.assertEqual(len(out), 4)

    def test_ip_lines_are_unchanged_by_the_option(self):
        self.assertEqual(self.lines(True)[:3], self.lines(False))

    def test_vpn_never_in_internet_line(self):
        line = network_info.build_internet_line(
            {"tailscale0": 0, "eth1": 100}, LABELS, TEXTS, self.net)
        self.assertEqual(line, "Internet: USB (eth1)")

    def test_option_is_read_from_config(self):
        self.assertFalse(make_component()[0].show_vpn)
        self.assertTrue(make_component(show_vpn=True)[0].show_vpn)


class InternetLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = build_sysfs(pathlib.Path(self.tmp.name))

    def line(self, routes):
        return network_info.build_internet_line(routes, LABELS, TEXTS, self.net)

    def test_lowest_metric_first(self):
        self.assertEqual(self.line({"wlan0": 600, "eth1": 100}),
                         "Internet: USB (eth1), then Wi-Fi (wlan0)")

    def test_same_metric_is_or(self):
        self.assertEqual(self.line({"wlan0": 100, "eth1": 100}),
                         "Internet: USB (eth1) or Wi-Fi (wlan0)")

    def test_three_groups(self):
        self.assertEqual(
            self.line({"wlan0": 600, "eth0": 100, "eth1": 300}),
            "Internet: Cable (eth0), then USB (eth1), then Wi-Fi (wlan0)")

    def test_no_route(self):
        self.assertEqual(self.line({}), "Internet: no route")

    def test_only_unknown_interfaces_is_no_route(self):
        self.assertEqual(self.line({"ppp9": 10}), "Internet: no route")


class ParseTests(unittest.TestCase):
    def test_addresses_and_state(self):
        r = network_info.parse_addresses(IP_OUTPUT)
        self.assertEqual(r["wlan0"], {"up": True, "ips": ["192.168.0.177"]})
        self.assertEqual(r["eth1"], {"up": True, "ips": ["192.168.0.178"]})
        self.assertEqual(r["eth0"], {"up": False, "ips": []})

    def test_link_local_ipv4_is_not_an_address(self):
        raw = json.dumps([{"ifname": "eth1", "operstate": "UP", "flags": ["UP"],
                           "addr_info": [{"family": "inet",
                                          "local": "169.254.3.4",
                                          "scope": "link"}]}])
        self.assertEqual(network_info.parse_addresses(raw)["eth1"]["ips"], [])

    def test_several_addresses_on_one_interface(self):
        raw = json.dumps([{"ifname": "eth0", "operstate": "UP", "flags": ["UP"],
                           "addr_info": [
                               {"family": "inet", "local": "10.0.0.2"},
                               {"family": "inet", "local": "10.0.0.3"}]}])
        self.assertEqual(network_info.parse_addresses(raw)["eth0"]["ips"],
                         ["10.0.0.2", "10.0.0.3"])

    def test_garbage_raises(self):
        with self.assertRaises(network_info.NetworkInfoError):
            network_info.parse_addresses("not json")


class BuildLinesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = build_sysfs(pathlib.Path(self.tmp.name))

    def lines(self, raw=IP_OUTPUT):
        ifaces = network_info.parse_addresses(raw)
        return network_info.build_lines(ifaces, LABELS, TEXTS, self.net)

    def test_real_machine_example(self):
        self.assertEqual(self.lines(), [
            "Wi-Fi (wlan0): 192.168.0.177",
            "Cable (eth0): not connected",
            "USB (eth1): 192.168.0.178",
        ])

    def test_usb_not_listed_when_not_plugged(self):
        data = [i for i in json.loads(IP_OUTPUT) if i["ifname"] != "eth1"]
        out = self.lines(json.dumps(data))
        self.assertEqual(len(out), 2)
        self.assertFalse(any("USB" in line for line in out))

    def test_up_without_address_is_connecting(self):
        data = json.loads(IP_OUTPUT)
        for i in data:
            if i["ifname"] == "eth1":
                i["addr_info"] = []
        self.assertIn("USB (eth1): getting an address",
                      self.lines(json.dumps(data)))


class ComponentTests(unittest.TestCase):
    def test_registers_remote_method(self):
        _, server, _ = make_component()
        self.assertIn("network_info_show", server.methods)

    def test_show_sends_one_script_with_title_and_lines(self):
        comp, _, apis = make_component()
        ifaces = {"wlan0": {"up": True, "ips": ["192.168.0.177"]}}
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value=ifaces)), \
             mock.patch.object(network_info, "classify", return_value="wifi"):
            run(comp._on_show_request())
        self.assertEqual(len(apis.gcodes), 1)
        self.assertEqual(apis.gcodes[0].split("\n"), [
            'RESPOND MSG="IP addresses of this machine:"',
            'RESPOND MSG="Wi-Fi (wlan0): 192.168.0.177"',
        ])

    def test_internet_line_is_appended(self):
        comp, _, apis = make_component()
        ifaces = {"wlan0": {"up": True, "ips": ["192.168.0.177"]}}
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value=ifaces)), \
             mock.patch.object(comp, "_read_routes",
                               mock.AsyncMock(return_value={"wlan0": 600})), \
             mock.patch.object(network_info, "classify", return_value="wifi"):
            run(comp._on_show_request())
        self.assertEqual(apis.gcodes[0].split("\n")[-1],
                         'RESPOND MSG="Internet: Wi-Fi (wlan0)"')

    def test_unknown_routes_omit_only_the_internet_line(self):
        comp, _, apis = make_component()
        ifaces = {"wlan0": {"up": True, "ips": ["192.168.0.177"]}}
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value=ifaces)), \
             mock.patch.object(network_info, "classify", return_value="wifi"):
            run(comp._on_show_request())
        self.assertNotIn("Internet", apis.gcodes[0])
        self.assertIn("Wi-Fi (wlan0): 192.168.0.177", apis.gcodes[0])

    def test_no_internet_line_without_interface_lines(self):
        comp, _, apis = make_component()
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value={})), \
             mock.patch.object(comp, "_read_routes",
                               mock.AsyncMock(return_value={})):
            run(comp._on_show_request())
        self.assertNotIn("Internet", apis.gcodes[0])

    def test_no_interfaces(self):
        comp, _, apis = make_component()
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value={})):
            run(comp._on_show_request())
        self.assertIn("no network interface found", apis.gcodes[0])

    def test_read_error_is_reported_as_error(self):
        comp, _, apis = make_component()
        boom = mock.AsyncMock(side_effect=network_info.NetworkInfoError("x"))
        with mock.patch.object(comp, "_read_interfaces", boom):
            run(comp._on_show_request())
        self.assertEqual(apis.gcodes, [
            'RESPOND TYPE=error MSG="IP addresses of this machine: '
            'could not read the network"'])

    def test_unexpected_error_does_not_escape(self):
        comp, _, apis = make_component()
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(side_effect=RuntimeError("x"))):
            run(comp._on_show_request())
        self.assertIn("TYPE=error", apis.gcodes[0])

    def test_klippy_down_does_not_raise(self):
        comp, server, _ = make_component()
        server.components["klippy_apis"] = FakeKlippyApis(fail=True)
        with mock.patch.object(comp, "_read_interfaces",
                               mock.AsyncMock(return_value={})):
            run(comp._on_show_request())   # must not raise

    def test_custom_labels_are_cleaned(self):
        comp, _, _ = make_component(label_wifi='Wi-Fi "casa"; {x}')
        self.assertNotIn('"', comp.labels["wifi"])
        self.assertNotIn(";", comp.labels["wifi"])
        self.assertNotIn("{", comp.labels["wifi"])


class ReadInterfacesTests(unittest.TestCase):
    def read(self, proc):
        comp, _, _ = make_component()
        with mock.patch("asyncio.create_subprocess_exec",
                        mock.AsyncMock(return_value=proc)):
            return run(comp._read_interfaces())

    def test_ok(self):
        r = self.read(FakeProc(out=IP_OUTPUT.encode()))
        self.assertIn("eth1", r)

    def test_nonzero_exit(self):
        with self.assertRaises(network_info.NetworkInfoError):
            self.read(FakeProc(err=b"boom", code=1))

    def test_timeout_kills_the_process(self):
        proc = FakeProc(delay=1.0)
        with mock.patch.object(network_info, "IP_TIMEOUT", 0.05):
            with self.assertRaises(network_info.NetworkInfoError):
                self.read(proc)
        self.assertTrue(proc.killed)

    def test_ip_missing(self):
        comp, _, _ = make_component()
        with mock.patch("asyncio.create_subprocess_exec",
                        mock.AsyncMock(side_effect=FileNotFoundError(2, "no"))):
            with self.assertRaises(network_info.NetworkInfoError):
                run(comp._read_interfaces())


class ReadRoutesTests(unittest.TestCase):
    def read(self, proc=None, exc=None):
        comp, _, _ = make_component()
        comp._read_routes = network_info.NetworkInfo._read_routes.__get__(comp)
        patch = (mock.AsyncMock(side_effect=exc) if exc
                 else mock.AsyncMock(return_value=proc))
        with mock.patch("asyncio.create_subprocess_exec", patch):
            return run(comp._read_routes())

    def test_ok(self):
        self.assertEqual(self.read(FakeProc(out=ROUTE_OUTPUT.encode())),
                         {"eth1": 100, "wlan0": 600})

    def test_failure_means_unknown_not_an_error(self):
        self.assertIsNone(self.read(FakeProc(err=b"boom", code=1)))

    def test_garbage_means_unknown(self):
        self.assertIsNone(self.read(FakeProc(out=b"nope")))

    def test_ip_missing_means_unknown(self):
        self.assertIsNone(self.read(exc=FileNotFoundError(2, "no")))


if __name__ == "__main__":
    unittest.main()
