#!/usr/bin/env python3
"""kiosk-inhibit — inhibitor-aware idle manager for the 3-TTY cage kiosk.

Watches all cage instances (wayland sockets under the configured runtime
directory, which appear lazily as each VT is first visited) through
ext_idle_notify_v1, whose 'idled' event fires only when the cage has
seen no input for the timeout AND holds no active
zwp_idle_inhibit_manager_v1 inhibitor; 'resumed' fires on input or
inhibitor changes.  Media playback (Kodi's transient zwp inhibitor,
Firefox's wake locks) therefore keeps a cage "active" here.

Also registers as the inhibitor endpoints on the shared session bus, so
that apps reach us through whichever classic mechanism they support:

  * org.freedesktop.ScreenSaver            (Kodi, Firefox)
  * org.gnome.SessionManager               (GNOME apps)
  * org.freedesktop.PowerManagement.Inhibit (Firefox fallback)
  * org.freedesktop.portal.Inhibit         (XDG portal, request-object)

Because Firefox's FreeDesktop D-Bus backends report "available" without
probing the name owner, owning org.freedesktop.ScreenSaver routes it to
the classic protocol; Kodi uses the Wayland zwp inhibitor when present.

Behaviour:

  * Screen: the foreground cage's output is disabled (wlr-randr --off)
    while every running cage is idle and no inhibitor cookie is held, and
    re-enabled on the first input.  cage v0.3.1 has no
    wlr-output-power-management-v1, so the output itself is toggled;
    verified safe, and wlroots keeps mode and scale across it.

  * Suspend: a logind INHIBIT_IDLE lock in *block* mode is held while any
    cage is active or any cookie is held.  Block mode is mandatory:
    logind's idle hint (manager_get_idle_hint) only counts block-mode
    inhibitors, delay-mode locks are invisible to it.  Once the lock is
    dropped, logind's IdleAction=suspend (logind-idle.conf.d drop-in)
    does the actual suspend through sleep.target / systemd-sleep, so the
    Hue power-cycle hooks fire.

  * Inhibitor release: D-Bus inhibitors are invisible to cage, so when
    the last cookie is released all cages may already be marked idle.
    A fresh screen_off_after grace period is then started, otherwise the
    screen would blank (and suspend would be allowed) immediately
    instead of after the usual idle timeout.

Configuration (INI file, default /etc/kiosk-inhibit.conf, override with
--config or KIOSK_INHIBIT_CONFIG):
  [idle]     screen_off_after       seconds of cage idleness before screen off
  [wayland]  runtime_dir            user runtime dir holding the wayland sockets
  [dbus]     session_bus_address    shared session bus of the kiosk user
  [timing]   scan_interval          seconds between socket scans
             wlr_randr_timeout      seconds per wlr-randr invocation
"""

import argparse
import configparser
import glob
import logging
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

import dbus
import dbus.bus
import dbus.connection
import dbus.mainloop.glib
import dbus.service
import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

try:
    from gi.repository import GLibUnix
except ImportError:  # PyGObject < 3.44 / GLib without the Unix namespace
    GLibUnix = None

from pywayland.client.display import Display
from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
from pywayland.protocol.wayland import WlSeat

log = logging.getLogger("kiosk-inhibit")

DEFAULT_CONFIG_PATH = "/etc/kiosk-inhibit.conf"


@dataclass(frozen=True)
class Config:
    screen_off_after: int
    runtime_dir: str
    session_bus_address: str
    scan_interval: int
    wlr_timeout: int


def load_config(path):
    """Read the INI configuration; missing keys fall back to defaults."""
    parser = configparser.ConfigParser(interpolation=None)
    with open(path, encoding="utf-8") as f:
        parser.read_file(f)

    runtime_dir = parser.get(
        "wayland", "runtime_dir",
        fallback=os.environ.get("XDG_RUNTIME_DIR", "/run/user/1001"))
    config = Config(
        screen_off_after=parser.getint(
            "idle", "screen_off_after", fallback=3600),
        runtime_dir=runtime_dir,
        session_bus_address=parser.get(
            "dbus", "session_bus_address",
            fallback="unix:path=%s/bus" % runtime_dir),
        scan_interval=parser.getint("timing", "scan_interval", fallback=2),
        wlr_timeout=parser.getint(
            "timing", "wlr_randr_timeout", fallback=5),
    )
    if config.screen_off_after <= 0:
        raise ValueError("screen_off_after must be positive")
    if config.scan_interval <= 0:
        raise ValueError("scan_interval must be positive")
    if config.wlr_timeout <= 0:
        raise ValueError("wlr_randr_timeout must be positive")
    if not config.runtime_dir:
        raise ValueError("runtime_dir must not be empty")
    if not config.session_bus_address:
        raise ValueError("session_bus_address must not be empty")
    return config


def parse_args():
    parser = argparse.ArgumentParser(
        description="inhibitor-aware idle manager for the cage kiosk")
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("KIOSK_INHIBIT_CONFIG", DEFAULT_CONFIG_PATH),
        help="INI configuration file (default: %(default)s)")
    return parser.parse_args()


def wlr_randr(config, sock_path, *args):
    """Run wlr-randr against one cage's socket; None on any error."""
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = os.path.basename(sock_path)
    env["XDG_RUNTIME_DIR"] = config.runtime_dir
    try:
        p = subprocess.run(
            ["wlr-randr", *args], env=env,
            capture_output=True, text=True, timeout=config.wlr_timeout,
        )
    except Exception as e:
        log.debug("wlr-randr %r: %r", args, e)
        return None
    if p.returncode != 0:
        log.debug("wlr-randr %r rc=%d: %s",
                  args, p.returncode, p.stderr.strip())
        return None
    return p.stdout


def parse_outputs(text):
    """Yield (name, enabled) for each output block in wlr-randr output.

    wlr-randr >= 0.4 dropped the '(connected)' marker: an output is now
    just a non-indented header line followed by indented properties,
    with 'Enabled: yes|no'.  Older versions append '(connected)' or
    '(disconnected)' to the header; disconnected outputs are skipped.
    enabled is None when the version does not report it.
    """
    name = enabled = None
    for line in text.splitlines():
        if not line:
            continue
        if not line[0].isspace():
            if name is not None:
                yield name, enabled
            name = enabled = None
            if "disconnected" not in line:
                name = line.split()[0]
            continue
        if name is not None and line.strip().startswith("Enabled:"):
            value = line.split(":", 1)[1].strip().lower()
            enabled = value == "yes"
    if name is not None:
        yield name, enabled


def foreground_output(config, cages):
    """Return (sock_path, output_name, enabled) of the cage owning the
    display.

    Sockets are tried in creation order; only the foreground cage has
    an output (wlroots destroys it when the VT is lost), so the first
    one that reports one is the foreground.  enabled is True/False, or
    None if wlr-randr does not say.  Returns (None, None, None) when
    there is no output.
    """
    for path in sorted(cages):
        text = wlr_randr(config, path)
        if text is None:
            continue
        for name, enabled in parse_outputs(text):
            return path, name, enabled
    return None, None, None


class CageClient:
    """pywayland client watching one cage's idle state."""

    def __init__(self, kiosk, path):
        self.kiosk = kiosk
        self.path = path
        self.active = True  # until an 'idled' event says otherwise
        self.dead = False
        self.display = None
        self.io_watch = None
        self._seat = None
        self._notifier = None
        self._notification = None

    def start(self):
        try:
            display = Display(os.path.basename(self.path))
            display.connect()
        except Exception as e:
            log.warning("cage %s: connect failed: %r", self.path, e)
            self.dead = True
            return
        self.display = display
        registry = display.get_registry()
        registry.dispatcher["global"] = self._on_global

        # Pump until the initial registry burst completes (sync roundtrip)
        # so the seat/notifier globals are bound before the IO watch takes
        # over.
        done = []

        def _sync_done(_proxy, _data):
            done.append(True)

        sync = display.sync()
        sync.dispatcher["done"] = _sync_done
        deadline = time.monotonic() + 5.0
        while not done and time.monotonic() < deadline:
            try:
                display.read()
                display.dispatch(block=False)
                display.flush()
            except Exception as e:
                log.warning("cage %s: pump failed: %r", self.path, e)
                self.kill()
                return
            time.sleep(0.01)
        if not done:
            log.warning("cage %s: registry sync timed out", self.path)
            self.kill()
            return
        self.io_watch = GLib.io_add_watch(
            display.get_fd(), GLib.IO_IN, self._on_readable)
        log.info("cage %s: watching (idle after %ds)",
                 self.path, self.kiosk.config.screen_off_after)

    def _on_global(self, registry, name, iface, version):
        if iface == "wl_seat" and self._seat is None:
            self._seat = registry.bind(name, WlSeat, version)
        elif iface == "ext_idle_notifier_v1" and self._notifier is None:
            self._notifier = registry.bind(name, ExtIdleNotifierV1, version)
        self._maybe_notify()

    def _maybe_notify(self):
        if (self._seat is not None and self._notifier is not None
                and self._notification is None and not self.dead):
            notif = self._notifier.get_idle_notification(
                self.kiosk.config.screen_off_after * 1000, self._seat)
            notif.dispatcher["idled"] = self._on_idled
            notif.dispatcher["resumed"] = self._on_resumed
            self._notification = notif

    def _on_readable(self, _source, _cond):
        d = self.display
        if d is None:
            return False
        try:
            d.read()
            d.dispatch(block=False)
            d.flush()
        except Exception as e:
            log.warning("cage %s: read/dispatch failed: %r", self.path, e)
            self.kill()
            return False
        return True

    def _on_idled(self, _proxy):
        if not self.active:
            return
        self.active = False
        log.info("cage %s: idle", self.path)
        self.kiosk.state_changed()

    def _on_resumed(self, _proxy):
        if self.active:
            return
        self.active = True
        log.info("cage %s: resumed", self.path)
        self.kiosk.state_changed()

    def kill(self):
        """Stop watching. The display object is parked in the kiosk's
        graveyard: pywayland teardown can segfault, so nothing is released
        until process exit (where a segfault is harmless)."""
        if self.dead:
            return
        self.dead = True
        if self.io_watch is not None:
            GLib.source_remove(self.io_watch)
            self.io_watch = None
        if self.display is not None:
            self.kiosk.graveyard.append(self.display)
            self.display = None
            self._seat = None
            self._notifier = None
            self._notification = None


class ScreenSaverService(dbus.service.Object):
    """org.freedesktop.ScreenSaver (classic).

    Note the non-standard capitalisation of the release method: both
    Kodi and Firefox call it 'UnInhibit'.
    """

    def __init__(self, bus_name, kiosk):
        super().__init__(bus_name, "/ScreenSaver")
        self.kiosk = kiosk

    @dbus.service.method("org.freedesktop.ScreenSaver",
                         in_signature="ss", out_signature="u",
                         sender_keyword="sender")
    def Inhibit(self, who, why, sender):
        log.info("ScreenSaver.Inhibit from %s: %s / %s", sender, who, why)
        return self.kiosk.add_cookie(sender)

    @dbus.service.method("org.freedesktop.ScreenSaver", in_signature="u")
    def UnInhibit(self, cookie):
        self.kiosk.drop_cookie(cookie)

    @dbus.service.method("org.freedesktop.ScreenSaver", out_signature="b")
    def GetActive(self):
        # "active" would mean the saver is running; ours is the screen-off.
        return self.kiosk.screen_off

    @dbus.service.method("org.freedesktop.ScreenSaver")
    def SimulateUserActivity(self):
        pass


class SessionManagerService(dbus.service.Object):
    """org.gnome.SessionManager (classic GNOME inhibit)."""

    def __init__(self, bus_name, kiosk):
        super().__init__(bus_name, "/org/gnome/SessionManager")
        self.kiosk = kiosk

    @dbus.service.method("org.gnome.SessionManager",
                         in_signature="susu", out_signature="u",
                         sender_keyword="sender")
    def Inhibit(self, client_pid, xid, reason, flags, sender):
        log.info("SessionManager.Inhibit from %s: pid=%s xid=%s "
                 "reason=%s flags=%u", sender, client_pid, xid, reason, flags)
        return self.kiosk.add_cookie(sender)

    @dbus.service.method("org.gnome.SessionManager", in_signature="u")
    def Uninhibit(self, cookie):
        self.kiosk.drop_cookie(cookie)


class PowerManagementService(dbus.service.Object):
    """org.freedesktop.PowerManagement.Inhibit (classic power inhibit)."""

    def __init__(self, bus_name, kiosk):
        super().__init__(bus_name, "/org/freedesktop/PowerManagement/Inhibit")
        self.kiosk = kiosk

    @dbus.service.method("org.freedesktop.PowerManagement.Inhibit",
                         in_signature="s", out_signature="u",
                         sender_keyword="sender")
    def Inhibit(self, reason, sender):
        log.info("PowerManagement.Inhibit from %s: %s", sender, reason)
        return self.kiosk.add_cookie(sender)

    @dbus.service.method("org.freedesktop.PowerManagement.Inhibit",
                         in_signature="u")
    def UnInhibit(self, cookie):
        self.kiosk.drop_cookie(cookie)


class PortalRequest(dbus.service.Object):
    """org.freedesktop.portal.Request — Close() releases the cookie."""

    def __init__(self, bus_name, path, kiosk, cookie):
        super().__init__(bus_name, path)
        self.kiosk = kiosk
        self.cookie = cookie

    @dbus.service.signal("org.freedesktop.portal.Request", signature="u")
    def Finished(self, type):
        pass

    @dbus.service.method("org.freedesktop.portal.Request")
    def Close(self):
        self.kiosk.drop_cookie(self.cookie)
        # In dbus-python, a @dbus.service.signal method IS the emitter:
        # calling it runs the body and then broadcasts the signal.
        self.Finished(0)


class PortalService(dbus.service.Object):
    """org.freedesktop.portal.Inhibit (request-object variant:
    Inhibit(s u a{sv}) -> (o), released via Request.Close)."""

    def __init__(self, bus_name, kiosk):
        super().__init__(bus_name, "/org/freedesktop/portal/desktop")
        self.kiosk = kiosk
        self._bus_name = bus_name
        self._seq = 0

    @dbus.service.method("org.freedesktop.portal.Inhibit",
                         out_signature="as")
    def GetInterfaces(self):
        return ["inhibit"]

    @dbus.service.method("org.freedesktop.portal.Inhibit",
                         in_signature="sua{sv}", out_signature="o",
                         sender_keyword="sender")
    def Inhibit(self, app_id, flags, options, sender):
        log.info("portal.Inhibit from %s: app=%s flags=%u options=%r",
                 sender, app_id, flags, options)
        self._seq += 1
        path = "/org/freedesktop/portal/request/%d" % self._seq
        cookie = self.kiosk.add_cookie(sender)
        self.kiosk.requests[path] = PortalRequest(
            self._bus_name, path, self.kiosk, cookie)
        return dbus.ObjectPath(path)


class Kiosk:
    """Holds all state; everything runs in the GLib main loop thread."""

    def __init__(self, config):
        self.config = config
        self.cages = {}      # sock_path -> CageClient
        self.cookies = {}    # cookie(int) -> sender unique bus name
        self.requests = {}   # portal request path -> PortalRequest
        self.graveyard = []  # dead Displays, kept until process exit
        self.screen_off = False
        self._cookie_seq = 0
        self._session_bus = None
        self._logind_bus = None
        self._logind_mgr = None
        self._lock_fd = None
        self._grace_until = 0.0
        self._no_output_warned = False

    # -- inhibitor cookies ------------------------------------------------

    def add_cookie(self, sender):
        self._cookie_seq += 1
        cookie = self._cookie_seq
        self.cookies[cookie] = sender
        try:
            # Reap the cookie if the caller vanishes (crash/kill).
            self._session_bus.watch_name_owner(
                sender, self._owner_changed(cookie))
        except Exception as e:
            log.warning("watch_name_owner(%s) failed: %r", sender, e)
        log.info("inhibit cookie %d held by %s", cookie, sender)
        self.state_changed()
        return cookie

    def _owner_changed(self, cookie):
        def cb(owner):
            if not owner:
                log.info("%s left the bus; reaping its cookies",
                         self.cookies.get(cookie, "?"))
                self.drop_cookie(cookie)
        return cb

    def drop_cookie(self, cookie):
        sender = self.cookies.pop(cookie, None)
        if sender is None:
            return
        log.info("inhibit cookie %d released (%s)", cookie, sender)
        # D-Bus inhibitors are invisible to the compositor.  If the cages
        # already went idle while the cookie was held, their idle state is
        # stale, so releasing the last cookie would otherwise blank the
        # screen (and allow suspend) immediately instead of after the
        # usual timeout.  Restart the countdown.
        if not self.cookies:
            self._grace_until = (
                time.monotonic() + self.config.screen_off_after)
            log.info("idle countdown restarted (%ds)",
                     self.config.screen_off_after)
        self.state_changed()

    # -- logind INHIBIT_IDLE lock -----------------------------------------

    def _get_logind_manager(self):
        if self._logind_mgr is None:
            self._logind_bus = dbus.SystemBus()
            obj = self._logind_bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1")
            self._logind_mgr = dbus.Interface(
                obj, "org.freedesktop.login1.Manager")
        return self._logind_mgr

    def _lock_wanted(self):
        if time.monotonic() < self._grace_until:
            return True
        return bool(self.cookies) or any(
            c.active for c in self.cages.values())

    def _sync_lock(self):
        if self._lock_wanted():
            if self._lock_fd is None:
                try:
                    fd = self._get_logind_manager().Inhibit(
                        "idle", "kiosk-inhibit",
                        "kiosk cages active or inhibited", "block")
                    self._lock_fd = fd.take()
                    log.info("logind INHIBIT_IDLE lock acquired (fd %d)",
                             self._lock_fd)
                except Exception as e:
                    log.error("logind Inhibit(idle) failed: %r", e)
        elif self._lock_fd is not None:
            os.close(self._lock_fd)
            log.info("logind INHIBIT_IDLE lock released")
            self._lock_fd = None

    # -- cage sockets ------------------------------------------------------

    def scan_cages(self):
        """Wayland sockets appear lazily (first VT visit) and persist
        afterwards; watch for both appearance and disappearance."""
        seen = set()
        for path in sorted(glob.glob(os.path.join(
                self.config.runtime_dir, "wayland-*"))):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISSOCK(st.st_mode):
                continue  # wayland-N.lock
            seen.add(path)
            if path not in self.cages:
                log.info("cage socket appeared: %s", path)
                client = CageClient(self, path)
                self.cages[path] = client
                client.start()
        for path in list(self.cages):
            if path not in seen:
                log.info("cage socket gone: %s", path)
                self.cages[path].kill()
                del self.cages[path]

    # -- screen ------------------------------------------------------------

    def _sync_screen(self):
        want_off = bool(self.cages) and not self._lock_wanted()
        path, name, enabled = foreground_output(self.config, self.cages)
        if not name:
            if want_off != self.screen_off and not self._no_output_warned:
                log.warning("want screen %s but no foreground output",
                            "off" if want_off else "on")
                self._no_output_warned = True
            return
        self._no_output_warned = False

        # Reconcile against the compositor instead of trusting our own
        # flag: wlroots destroys/recreates the output on VT switches, so
        # the screen can come back on while we still think it is off.
        screen_off_now = self.screen_off if enabled is None else not enabled
        if want_off == screen_off_now:
            self.screen_off = screen_off_now
            return
        if want_off:
            if wlr_randr(
                    self.config, path, "--output", name, "--off") is None:
                log.warning("wlr-randr --off failed: %s (%s)",
                            name, os.path.basename(path))
                return
            self.screen_off = True
            log.info("screen off: %s (%s)", name, os.path.basename(path))
        else:
            if wlr_randr(
                    self.config, path, "--output", name, "--on") is None:
                log.warning("wlr-randr --on failed: %s (%s)",
                            name, os.path.basename(path))
                return
            self.screen_off = False
            log.info("screen on: %s (%s)", name, os.path.basename(path))

    # -- state machine -----------------------------------------------------

    def state_changed(self):
        self._sync_lock()
        self._sync_screen()

    def tick(self):
        """Periodic: pick up socket changes and retry screen transitions
        (e.g. when the foreground was momentarily unavailable)."""
        self.scan_cages()
        self._sync_lock()
        self._sync_screen()
        return True


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout)
    GLib.set_prgname("kiosk-inhibit")

    try:
        config = load_config(args.config)
    except (OSError, ValueError, configparser.Error) as e:
        log.error("cannot load config %s: %s", args.config, e)
        return 1

    # Make the configured paths authoritative for pywayland, dbus and the
    # wlr-randr children instead of relying on the unit environment.
    os.environ["XDG_RUNTIME_DIR"] = config.runtime_dir
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = config.session_bus_address

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    kiosk = Kiosk(config)
    # BusConnection (not the bare Connection): dbus.service.BusName
    # needs the _bus_names registry that only a BusConnection carries.
    kiosk._session_bus = dbus.bus.BusConnection(config.session_bus_address)

    # Own the inhibitor names. Fail hard if one is already taken: the
    # design routes apps to us by name ownership, and silently doing
    # nothing would leave cookies unmanaged and suspend unblocked.
    bus_names = []
    for name in ("org.freedesktop.ScreenSaver",
                 "org.gnome.SessionManager",
                 "org.freedesktop.PowerManagement",
                 "org.freedesktop.portal.Desktop"):
        try:
            bus_names.append(dbus.service.BusName(
                name, kiosk._session_bus, do_not_queue=True))
        except Exception as e:
            log.error("cannot own %s: %r", name, e)
            return 1

    ScreenSaverService(bus_names[0], kiosk)
    SessionManagerService(bus_names[1], kiosk)
    PowerManagementService(bus_names[2], kiosk)
    PortalService(bus_names[3], kiosk)
    log.info("inhibitor names owned")

    kiosk.scan_cages()
    kiosk._sync_lock()
    kiosk._sync_screen()

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(config.scan_interval, kiosk.tick)

    # GLib signal callbacks get no arguments (unlike IO watches).
    def _sigterm(*_args):
        log.info("SIGTERM received, shutting down")
        loop.quit()
        return False

    if GLibUnix is not None:
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _sigterm)
    else:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _sigterm)

    log.info("kiosk-inhibit started (screen_off_after=%ds)",
             config.screen_off_after)
    try:
        loop.run()
    finally:
        # Drop the logind lock, then tear down the parked wayland
        # displays explicitly instead of letting the interpreter GC
        # them at exit (pywayland teardown can segfault, and a SEGV
        # would mark the unit "failed" after a clean stop).
        kiosk._sync_lock()
        for d in kiosk.graveyard:
            try:
                d.disconnect()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
