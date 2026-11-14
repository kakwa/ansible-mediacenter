# ansible-kodi

Ansible setup for the Kodi media box (192.168.1.6).

## Layout

- `inventory/hosts.yml` — single host `kodi`
- `group_vars/kodi_boxes.yml` — box-specific tunables (user, media roots, browser URLs, timeouts)
- `roles/kiosk/vars/main.yml` — the kiosk TTY map: which TTY runs what, per-TTY
  `prestop`, and optional compositor-level `scale` (wlr-randr)
- `site.yml` — role ordering

## Roles

| role        | purpose |
|-------------|---------|
| base        | hostname `kodi`, kakwa in `render` group, base packages |
| kodi        | kodi + inputstreams, `sources.xml` for NFS media dirs, settings |
| kiosk       | one `cage` instance per TTY (F1 kodi / F2 browser / F3 spotify), Alt+F<n> switching; optional per-TTY `scale` applied via `cage-run.sh` + `wlr-randr` |
| sound       | PipeWire sound server, USB Sound Blaster as default output |
| avahi       | announce `kodi.local` |
| plymouth    | boot splash + grub cleanup |
| powersaving | idle screen-off + system suspend, tlp |
| hue-plugs   | Hue smart plug CLI (`hue-plugs.py`) + stop-on-suspend / restart-on-resume hook and on-at-boot / off-at-shutdown unit |
| spotifyd    | spotifyd system service routed to PipeWire |
| nfs         | `/home/nfs` mount from the NAS |

## Usage

```sh
ansible-playbook -i inventory/hosts.yml site.yml
```

## Notes

- The box runs Debian (sid). Kodi's Debian package is re-versioned
  (`2:21.3+dfsg-*`); the old `5:21.3-dmo0` package must be purged first.
- kakwa's home on the box is `/var/lib/xbmc` (see /etc/passwd), not `/home/kakwa`.
- Kodi config lives in `/var/lib/xbmc/.kodi`.
- Suspend timeouts live in group_vars; the powersaving role renders them.
