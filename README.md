# Klipper IP Status

A console macro that prints the IP address of the machine, so you can find it
without a router page, a screen or a network scanner. Built for Klipper +
Moonraker with Mainsail; the output goes to the normal G-code console, so it
also shows in Fluidd and KlipperScreen.

Type `SHOW_IP_ADDRESS` in the console:

```
IP addresses of this machine:
Wi-Fi (wlan0): 192.168.0.177
Cable (eth0): not connected
USB (eth1): 192.168.0.178
```

- One line per **physical** network interface: Wi-Fi, then cable, then USB.
- Works with a phone or router plugged in by USB (tethering): it appears as
  soon as the network is up, and the line is not there when nothing is plugged in.
- Read fresh on every call, so what you see is what the machine has right now.
- Read-only: nothing on the machine is changed by running it.
- All the texts can be changed (see [Configuration](#configuration)).

## Requirements

- Raspberry Pi OS / Debian-based system with systemd and `iproute2` (the `ip`
  command, installed by default)
- Klipper and Moonraker installed in the default layout (`~/printer_data`,
  `~/moonraker`), for example via KIAUH
- `[respond]` in the Klipper config (Mainsail's `mainsail.cfg` already has it)

## Install

```
cd ~ && git clone https://github.com/lrafa97/Klipper-IP-Status.git && bash ./Klipper-IP-Status/install.sh
```

Run it as your normal user, without `sudo`. It asks for `sudo` only to restart
the services.

What it does, and nothing else:

- links the Moonraker component into `~/moonraker/moonraker/components`
- copies the macro to `~/printer_data/config/network/network_info.cfg`
- adds one line, `[include network/network_info.cfg]`, at the **top** of
  `printer.cfg` (the end belongs to Klipper's `SAVE_CONFIG` block)
- adds a marked block to `moonraker.conf` (the `[network_info]` section and the
  update manager entry)
- keeps a copy of your original `printer.cfg` and `moonraker.conf` in
  `config/klipper-ip-status-backup` (the first copy is never overwritten)

It checks everything before changing anything and stops, without touching a
file, if `SHOW_IP_ADDRESS` is already defined in your config, or if a different
`network_info.py` is already in the Moonraker components folder. A
`[network_info]` section you wrote yourself in `moonraker.conf` is kept as it is.

Klipper and Moonraker are restarted on the first install. If a print is in
progress, everything is installed but nothing is restarted, and you are told to
restart Klipper when the printer is idle.

For a different layout:
`PRINTER_DATA=/path MOONRAKER_DIR=/path CONFIG_SUBDIR=folder bash ./Klipper-IP-Status/install.sh`

## Update

From Mainsail: Machine > Update Manager > refresh, then update
*klipper-ip-status*. The Moonraker component is linked to the cloned repository,
so the update replaces its code and only Moonraker is restarted.

The macro is a copy and changes rarely. If an update changes it, run the
installer again (`cd ~/Klipper-IP-Status && bash install.sh`); it tells you when
Klipper needs a restart.

## Configuration

Everything has a default, so there is nothing to configure. To change a text,
add the option under the `[network_info]` section that the installer put in
`moonraker.conf`, then restart Moonraker (`sudo systemctl restart moonraker`).

| Option | Default | Meaning |
| --- | --- | --- |
| `title` | `IP addresses of this machine` | first line of the output |
| `label_wifi` | `Wi-Fi` | name shown for Wi-Fi interfaces |
| `label_wired` | `Cable` | name shown for the built-in Ethernet |
| `label_usb` | `USB` | name shown for USB network adapters and tethering |
| `text_no_link` | `not connected` | interface present but not connected |
| `text_connecting` | `getting an address` | connected, address not received yet |
| `text_no_interfaces` | `no network interface found` | no physical interface at all |
| `text_error` | `could not read the network` | the `ip` command failed |

Quotes, braces and semicolons are removed from these texts, because they would
break the console message.

That section lives inside the block marked by the installer, and running
`install.sh` again rewrites the block. If you changed options, set them again
afterwards.

## How it works

1. `SHOW_IP_ADDRESS` (a Klipper macro) asks Moonraker, through
   `action_call_remote_method`, to run the `network_info` component. A macro
   cannot read the network by itself.
2. The component runs `ip -j address` and decides what each interface is from
   `/sys/class/net`, not from its name: a wireless interface is Wi-Fi, a device
   under a USB bus is USB, any other physical interface is the cable. Loopback,
   VPNs such as Tailscale, bridges and containers have no physical device and
   are not listed.
3. It sends the lines back to the console with `RESPOND`, in one script, so
   they arrive together and in order.

IPv6 and link-local (`169.254.x.x`) addresses are not shown: the second kind
means "no DHCP lease", not a usable address.

## Uninstall

```
bash ~/Klipper-IP-Status/uninstall.sh
```

Removes the link, the macro file, the include line and the `moonraker.conf`
block, and restarts Klipper and Moonraker (it refuses while a print is running,
unless you pass `--force`). Your `printer.cfg` and `moonraker.conf` return to
what they were before the install. The cloned folder and the backups are not
deleted.

## Tests

```
python3 -m unittest discover -s tests -v    # the component, with a fake Moonraker
bash tests/test_install.sh                   # the installer, on a fake machine
```

Neither needs Klipper, Moonraker or any hardware; the installer test works in a
temporary folder and restarts nothing.
