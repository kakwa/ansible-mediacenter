# Hue Plugs Role

Installs a stdlib-only Python CLI (`/usr/local/bin/hue-plugs.py`) for
controlling Philips Hue smart plugs on the box. No daemon is involved --
the tool talks to the Hue bridge on demand through the standard Hue HTTP
API. It also installs a systemd **system-sleep hook**
(`/etc/systemd/system-sleep/hue-plugs`) that powers the plug off on
suspend and powers the device back on on resume.

## Usage

```sh
hue-plugs.py list              # show state of all configured plugs
hue-plugs.py ping              # check the bridge is reachable
hue-plugs.py stop ai           # power off the plug named "ai"
hue-plugs.py start ai          # power on the plug named "ai"
hue-plugs.py restart ai        # off, wait, on (restart the device)
hue-plugs.py stop all          # power off every configured plug
hue-plugs.py autofill          # list every bridge device + room, write the plug list to the config
hue-plugs.py autofill --no-save  # only print, do not touch the config
hue-plugs.py setup             # discover bridge via mDNS, pair, save to config
hue-plugs.py setup --no-save   # only print the credentials, do not touch the config
```

The script reads a JSON config file (default `/etc/hue-plugs.conf`,
overridable with `--config` or the `HUE_PLUGS_CONFIG` environment variable).

Add `--debug` (or set `HUE_PLUGS_DEBUG=1`) to dump every raw HTTP request
and response to stderr. This is useful when pairing or when the bridge
returns an unexpected answer. The client key is masked in the logged URL.

## Suspend / resume behaviour

The system-sleep hook runs on every system sleep transition:

| transition | action |
| --- | --- |
| `pre suspend` / `pre hibernate` | `hue-plugs.py stop <plug>` (network still up) |
| `post resume` / `post thaw` | waits until the bridge answers (≤ ~30 s), then `hue-plugs.py restart <plug>` |

`<plug>` is `hue_suspend_plug` (default `PlugHifi`); the pre-sleep and
wake actions are configurable (`hue_suspend_action`, `hue_wake_action`).
All activity is logged to `/var/log/hue-plugs-sleep.log`.

The hook is **dormant until the config file exists** on the box: it logs a
"no config" line and exits, so it is safe to deploy before pairing.

## Role Variables

Variables (see `defaults/main.yml`):

| Variable | Default | Description |
| --- | --- | --- |
| `hue_plugs_enable` | `true` | Install the script and the sleep hook |
| `hue_bridge_ip` | *(empty)* | Hue bridge IP; required to let Ansible manage the config file |
| `hue_client_key` | *(empty)* | Hue client key (paired username); required to manage the config |
| `hue_plugs` | `[]` | List of `{id, name}` plug entries; required to manage the config |
| `hue_restart_delay` | `15` | Seconds to wait between off and on during `restart` |
| `hue_suspend_plug` | `PlugHifi` | Plug name managed by the sleep hook |
| `hue_suspend_action` | `stop` | Hook action on suspend/hibernate |
| `hue_wake_action` | `restart` | Hook action on resume (`restart` = off, wait, on) |
| `hue_config_file` | `/etc/hue-plugs.conf` | Config file path on the target |
| `hue_script_path` | `/usr/local/bin/hue-plugs.py` | Script install path |
| `hue_sleep_hook` | `/etc/systemd/system-sleep/hue-plugs` | Sleep hook path |
| `hue_packages` | `[python3]` | Packages to install |

## Playbook Usage

First deploy without credentials -- the role installs the script and the
hook, leaves the config file alone, and prints the pairing instructions:

```yaml
- role: hue-plugs
  tags: ["service", "hue-plugs"]
```

After pairing (below), record the printed bridge IP and client key in the
playbook (vault recommended) and fill in the plug list:

```yaml
- role: hue-plugs
  tags: ["service", "hue-plugs"]
  vars:
    hue_bridge_ip: "{{ vault_hue_bridge_ip | default(lookup('file', playbook_dir ~ '/hue_bridge_ip.txt') | trim, true) }}"
    hue_client_key: "{{ vault_hue_client_key | default(lookup('file', playbook_dir ~ '/hue_bridge_key.txt') | trim, true) }}"
    hue_plugs:
      - id: "1"
        name: "PlugHifi"
```

Once `hue_bridge_ip`/`hue_client_key` are set, Ansible manages
`/etc/hue-plugs.conf` from the playbook -- keep `hue_plugs` in sync with
what `autofill` discovers.

## First-time Setup (pairing, manual)

Pairing needs the link button on the bridge, so it is done by hand on the
box (the playbook prints these same instructions):

```sh
hue-plugs.py setup --save       # mDNS discovery + pair (press link button)
hue-plugs.py setup --bridge-ip 192.168.1.10 --save   # skip mDNS
hue-plugs.py autofill --save    # discover the plugs, write them to the config
hue-plugs.py list               # sanity check
```

Afterwards, copy the printed bridge IP and client key into your vault or
one-line files next to the playbook and re-run the playbook.

## Finding Plug IDs (autofill)

`autofill` talks to the bridge, lists every device it finds (with the room
each one lives in) and prints a ready-to-paste `hue_plugs` block for the
playbook:

```
discovered 3 device(s) on the bridge

HUE ID     NAME                 ROOM                 TYPE
----------------------------------------------------------------------
1          PlugHifi             HiFi                 On/Off plug-in unit
6          PlugPcIA             Office               On/Off plug-in unit
9          Hue Bulb             Office               Dimmable light
```

```yaml
  hue_plugs:
    - id: "1"
      name: "PlugHifi"
    - id: "6"
      name: "PlugPcIA"
```

Devices already present in the local config keep their (user-chosen) name;
newly discovered ones fall back to the name the bridge gave them. Add
`--save` to also write the regenerated plug list back to the config file.
