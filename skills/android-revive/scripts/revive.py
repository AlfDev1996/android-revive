#!/usr/bin/env python3
"""
android-revive - make an old Android device lighter and smoother over adb,
with before/after measurements and a full, write-ahead rollback.

Safety model (read this before changing anything):
  * Only reversible operations are ever used:
      pm disable-user --user 0 <pkg>      -> undo: pm enable <pkg>
      pm uninstall -k --user 0 <pkg>      -> undo: cmd package install-existing <pkg>   (Android 8+ only, opt-in)
      settings put <ns> <key> <value>     -> undo: settings put/delete with the old value
  * Nothing on /system is ever deleted, nothing is flashed, root is never used.
  * Every change is written to actions.jsonl and to rollback.sh / rollback.cmd
    BEFORE it is executed (write-ahead), so an interrupted run can always be undone.
  * A never-touch list plus dynamic protection (current launcher, keyboard,
    WebView, default SMS/dialer, accessibility services, device admins) is
    enforced by this script - not just suggested to the agent.
  * Changes are applied in small batches; after each batch a health check runs
    and the batch is reverted automatically if the device looks unhealthy.

Python 3.9+, standard library only.  Run `revive.py -h` for the commands.
"""

import argparse
import datetime
import fnmatch
import html
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
LISTS_DIR = os.path.join(SKILL_DIR, "lists")
TIERS = ["safe", "recommended", "aggressive", "extreme"]
MIN_SDK = 21  # Android 5.0

KNOWN_LAUNCHERS = [
    "com.android.launcher", "com.android.launcher2", "com.android.launcher3",
    "com.google.android.apps.nexuslauncher", "com.google.android.launcher",
    "com.sec.android.app.launcher", "com.miui.home", "com.mi.android.globallauncher",
    "com.cyanogenmod.trebuchet", "com.huawei.android.launcher", "com.oppo.launcher",
    "net.oneplus.launcher", "com.motorola.launcher3", "com.teslacoilsw.launcher",
]

TELEPHONY_PACKAGES = [
    "com.android.dialer", "com.google.android.dialer", "com.samsung.android.dialer",
    "com.android.incallui", "com.samsung.android.incallui",
    "com.android.messaging", "com.google.android.apps.messaging",
    "com.samsung.android.messaging", "com.android.mms",
    "com.android.contacts", "com.google.android.contacts", "com.samsung.android.app.contacts",
    "com.android.stk", "com.android.cellbroadcastreceiver",
    "com.google.android.cellbroadcastreceiver", "com.android.calllogbackup",
]


# --------------------------------------------------------------------------- output

class Style:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    @staticmethod
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if Style.on else s


def info(msg):
    print(Style.c("36", "• ") + msg)


def ok(msg):
    print(Style.c("32", "✔ ") + msg)


def warn(msg):
    print(Style.c("33", "! ") + msg)


def fail(msg):
    print(Style.c("31", "✘ ") + msg)


def head(msg):
    print("\n" + Style.c("1", msg))


class ReviveError(Exception):
    pass


# --------------------------------------------------------------------------- adb

def adb_cmd():
    custom = os.environ.get("REVIVE_ADB")
    if custom:
        return [sys.executable, custom] if custom.endswith(".py") else [custom]
    path = shutil.which("adb")
    return [path] if path else None


def run_adb(args, timeout=60):
    base = adb_cmd()
    if not base:
        raise ReviveError("adb not found. See references/prep-guide.md (step 4) to install platform-tools.")
    try:
        p = subprocess.run(base + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ReviveError("adb %s timed out after %ss" % (" ".join(args[:3]), timeout))
    return p.returncode, p.stdout.replace("\r", ""), p.stderr.replace("\r", "")


def list_devices():
    _, out, _ = run_adb(["devices"], timeout=20)
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1]))
    return devs


class Device:
    def __init__(self, serial):
        self.serial = serial
        self._sdk = None

    def adb(self, *args, timeout=60):
        return run_adb(["-s", self.serial] + list(args), timeout=timeout)

    def sh(self, cmd, timeout=60):
        rc, out, err = self.adb("shell", cmd, timeout=timeout)
        if rc != 0 and ("device offline" in err or "not found" in err and "device" in err
                        or "no devices" in err or "unauthorized" in err):
            raise ReviveError("device not reachable: " + err.strip())
        return out

    def prop(self, key):
        return self.sh("getprop " + key).strip()

    @property
    def sdk(self):
        if self._sdk is None:
            try:
                self._sdk = int(self.prop("ro.build.version.sdk") or 0)
            except ValueError:
                self._sdk = 0
        return self._sdk


def pick_device(serial=None):
    devs = list_devices()
    if serial:
        for s, state in devs:
            if s == serial:
                if state != "device":
                    raise ReviveError("device %s is '%s'. %s" % (s, state, state_hint(state)))
                return Device(s)
        raise ReviveError("device %s not connected (adb devices shows: %s)" % (serial, devs or "nothing"))
    ready = [s for s, st in devs if st == "device"]
    if not devs:
        raise ReviveError("no device found. Enable Developer options + USB debugging and plug the cable "
                          "(references/prep-guide.md, step 3).")
    if not ready:
        s, st = devs[0]
        raise ReviveError("device %s is '%s'. %s" % (s, st, state_hint(st)))
    if len(ready) > 1:
        raise ReviveError("more than one device connected: %s. Pass --serial." % ", ".join(ready))
    return Device(ready[0])


def state_hint(state):
    return {
        "unauthorized": "Unlock the device and accept the 'Allow USB debugging?' prompt "
                        "(tick 'Always allow from this computer').",
        "offline": "Unplug and replug the cable, or run `adb kill-server`.",
        "recovery": "The device is in recovery: reboot it to Android first.",
        "sideload": "The device is in sideload mode: reboot it to Android first.",
    }.get(state, "Reconnect the device.")


# --------------------------------------------------------------------------- parsers

def parse_pkgs(out):
    res = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            res.add(line.split(":", 1)[1].split("=")[-1].strip())
    return res


def to_kb(num, unit):
    n = float(num.replace(",", ""))
    unit = (unit or "K").upper()
    if unit.startswith("M"):
        return n * 1024
    if unit.startswith("G"):
        return n * 1024 * 1024
    return n


def parse_dumpsys_meminfo(text):
    """Works with Android 5 -> 15 formats ("549,851K" or "549851 kB")."""
    res = {}
    num = r"([\d,]+(?:\.\d+)?)\s*(K|kB|KB|M|MB|G|GB)?"
    m = re.search(r"Used RAM:\s*" + num, text)
    if m:
        res["ram_used_mb"] = round(to_kb(m.group(1), m.group(2)) / 1024)
    m = re.search(r"Free RAM:\s*" + num, text)
    if m:
        res["ram_free_total_mb"] = round(to_kb(m.group(1), m.group(2)) / 1024)
    m = re.search(num + r"\s*free\)", text)
    if m:
        res["ram_free_mb"] = round(to_kb(m.group(1), m.group(2)) / 1024)
    m = re.search(r"Total RAM:\s*" + num, text)
    if m:
        res["ram_total_mb"] = round(to_kb(m.group(1), m.group(2)) / 1024)
    procs = []
    sec = re.search(r"Total PSS by process:\n(.*?)\n\s*\n", text, re.S)
    if sec:
        for line in sec.group(1).splitlines():
            mm = re.match(r"\s*([\d,]+)\s*(K|kB)?:\s*(\S+)\s*\(pid", line)
            if mm:
                procs.append({"name": mm.group(3), "pss_mb": round(to_kb(mm.group(1), mm.group(2)) / 1024, 1)})
    if procs:
        res["processes"] = len(procs)
        res["top_processes"] = procs[:15]
        res["process_names"] = sorted({p["name"] for p in procs})
    return res


def parse_proc_meminfo(text):
    vals = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2))
    res = {}
    if "MemTotal" in vals:
        res["ram_total_mb"] = round(vals["MemTotal"] / 1024)
    if "MemAvailable" in vals:
        res["ram_available_mb"] = round(vals["MemAvailable"] / 1024)
    elif "MemFree" in vals:  # kernels < 3.14
        res["ram_available_mb"] = round((vals["MemFree"] + vals.get("Cached", 0)) / 1024)
    return res


# --------------------------------------------------------------------------- device facts

def package_states(dev):
    """pkg -> {'state': enabled|disabled|uninstalled-user, 'system': bool}"""
    every = parse_pkgs(dev.sh("pm list packages -u"))
    installed = parse_pkgs(dev.sh("pm list packages"))
    disabled = parse_pkgs(dev.sh("pm list packages -d"))
    system = parse_pkgs(dev.sh("pm list packages -s -u"))
    states = {}
    for p in sorted(every | installed):
        if p not in installed:
            st = "uninstalled-user"
        elif p in disabled:
            st = "disabled"
        else:
            st = "enabled"
        states[p] = {"state": st, "system": p in system}
    return states


def home_handlers(dev):
    if dev.sdk < 24:
        return None
    out = dev.sh("cmd package query-activities --brief -a android.intent.action.MAIN "
                 "-c android.intent.category.HOME")
    pk = set()
    for line in out.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.]+)/", line)
        if m:
            pk.add(m.group(1))
    return pk or None


def settings_get(dev, ns, key):
    v = dev.sh("settings get %s %s" % (ns, key)).strip()
    return None if v in ("", "null") else v


def component_pkgs(value):
    if not value:
        return set()
    out = set()
    for part in re.split(r"[:;,]", value):
        part = part.strip()
        if part:
            out.add(part.split("/")[0])
    return out


def dynamic_protection(dev):
    """Packages this particular device must keep, with the reason why."""
    prot = {}

    def add(pkgs, why):
        for p in pkgs:
            if p:
                prot.setdefault(p, why)

    homes = home_handlers(dev)
    if homes:
        add(homes, "handles the HOME screen (launcher)")
    add(component_pkgs(settings_get(dev, "secure", "default_input_method")), "current keyboard")
    add(component_pkgs(settings_get(dev, "secure", "enabled_accessibility_services")),
        "accessibility service in use")
    add(component_pkgs(settings_get(dev, "secure", "sms_default_application")), "default SMS app")
    add(component_pkgs(settings_get(dev, "secure", "dialer_default_application")), "default phone app")
    add([current_webview(dev)], "current WebView provider")
    dp = dev.sh("dumpsys device_policy")
    add(re.findall(r"admin=ComponentInfo\{([\w.]+)/", dp), "active device administrator")
    return prot


def current_webview(dev):
    m = re.search(r"Current WebView package \(name, version\): \(([\w.]+)", dev.sh("dumpsys webviewupdate"))
    if m:
        return m.group(1)
    return settings_get(dev, "global", "webview_provider")  # Android 7: dumpsys prints nothing


def has_telephony(dev):
    return "feature:android.hardware.telephony" in dev.sh("pm list features")


def google_account_count(dev):
    out = dev.sh("dumpsys account")
    return len(re.findall(r"Account \{name=[^,]*, type=com\.google\}", out))


# --------------------------------------------------------------------------- lists

def load_never():
    pats = []
    with open(os.path.join(LISTS_DIR, "never-touch.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                pats.append(line)
    return pats


def never_match(pkg, pats):
    for p in pats:
        if fnmatch.fnmatchcase(pkg, p):
            return p
    return None


def load_lists(extra=None):
    """-> {pkg: {'tier','note','list'}}; later files override earlier ones."""
    files = sorted(f for f in os.listdir(LISTS_DIR) if f.endswith(".txt") and f != "never-touch.txt")
    paths = [os.path.join(LISTS_DIR, f) for f in files] + list(extra or [])
    entries = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|")]
                pkg = parts[0]
                tier = parts[1] if len(parts) > 1 and parts[1] else "recommended"
                if tier not in TIERS:
                    raise ReviveError("%s:%d unknown tier '%s' (use %s)" % (path, n, tier, "/".join(TIERS)))
                entries[pkg] = {"tier": tier, "note": parts[2] if len(parts) > 2 else "",
                                "list": os.path.basename(path)}
    return entries


# --------------------------------------------------------------------------- sessions

def sessions_root(args):
    return os.path.abspath(getattr(args, "root", None) or os.environ.get("REVIVE_HOME") or "android-revive-sessions")


def new_session(args, dev):
    model = re.sub(r"[^A-Za-z0-9_-]+", "-", dev.prop("ro.product.model") or "device")
    path = os.path.join(sessions_root(args), "%s-%s" % (model, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(path)
    write_json(os.path.join(path, "session.json"),
               {"serial": dev.serial, "model": model, "created": now_iso(), "tool": VERSION})
    return path


def find_session(args, dev=None, create=False):
    if getattr(args, "session", None):
        if not os.path.isdir(args.session):
            raise ReviveError("session folder not found: " + args.session)
        return os.path.abspath(args.session)
    root = sessions_root(args)
    cands = []
    if os.path.isdir(root):
        for d in os.listdir(root):
            meta = os.path.join(root, d, "session.json")
            if os.path.isfile(meta):
                m = read_json(meta)
                if dev is None or m.get("serial") == dev.serial:
                    cands.append((m.get("created", ""), os.path.join(root, d)))
    if cands:
        return sorted(cands)[-1][1]
    if create and dev:
        return new_session(args, dev)
    raise ReviveError("no session found in %s - run `recon` first." % root)


def now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- action log + rollback

class ActionLog:
    """Write-ahead journal. An entry is written (and the rollback scripts are
    regenerated) BEFORE the change is executed, so an interruption at any point
    still leaves a correct undo. Undoing a change that never happened is a no-op."""

    def __init__(self, session, serial):
        self.session = session
        self.serial = serial
        self.path = os.path.join(session, "actions.jsonl")

    def entries(self):
        res = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        res.append(json.loads(line))
        return res

    def active(self):
        ents = self.entries()
        undone = {e["ref"] for e in ents if e["type"] == "undo"}
        return [e for e in ents if e["type"] != "undo" and e["id"] not in undone]

    def append(self, entry):
        ents = self.entries()
        entry = dict(entry, id=len(ents) + 1, ts=now_iso())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.write_scripts()
        return entry

    def write_scripts(self):
        acts = list(reversed(self.active()))
        cmds = [inverse_cmd(a) for a in acts]
        sh = ["#!/bin/sh",
              "# android-revive rollback - generated %s" % now_iso(),
              "# Undoes every change of this session, newest first. Safe to run more than once.",
              "# Usage: sh rollback.sh            (SERIAL=xxxx sh rollback.sh to pick a device)",
              'ADB="${ADB:-adb}"',
              'SERIAL="${SERIAL:-%s}"' % self.serial, ""]
        sh += ['"$ADB" -s "$SERIAL" shell %s' % shlex.quote(c) for c in cmds]
        sh += ["", 'echo "Rollback done (%d changes). Reboot the device to be sure: adb reboot"' % len(cmds)]
        with open(os.path.join(self.session, "rollback.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(sh) + "\n")
        os.chmod(os.path.join(self.session, "rollback.sh"), 0o755)
        cmd = ["@echo off", "rem android-revive rollback - generated %s" % now_iso(),
               "if \"%%SERIAL%%\"==\"\" set SERIAL=%s" % self.serial]
        cmd += ['adb -s %%SERIAL%% shell "%s"' % c.replace('"', "'") for c in cmds]
        cmd += ["echo Rollback done (%d changes). Reboot the device: adb reboot" % len(cmds)]
        with open(os.path.join(self.session, "rollback.cmd"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(cmd) + "\n")


def forward_cmd(a):
    if a["type"] == "package":
        if a["method"] == "uninstall":
            return "pm uninstall -k --user 0 %s" % a["pkg"]
        return "pm disable-user --user 0 %s" % a["pkg"]
    if a["type"] == "setting":
        return "settings put %s %s %s" % (a["ns"], a["key"], shlex.quote(str(a["new"])))
    if a["type"] == "command":
        return a["cmd"]
    raise ValueError(a)


def inverse_cmd(a):
    if a["type"] == "package":
        if a["method"] == "uninstall":
            return "cmd package install-existing %s" % a["pkg"]
        return "pm enable %s" % a["pkg"]
    if a["type"] == "setting":
        if a["old"] is None:
            return "settings delete %s %s" % (a["ns"], a["key"])
        return "settings put %s %s %s" % (a["ns"], a["key"], shlex.quote(str(a["old"])))
    if a["type"] == "command":
        return a.get("undo") or "true"
    raise ValueError(a)


def undo_entries(dev, log, entries):
    bad = []
    for a in sorted(entries, key=lambda e: -e["id"]):
        out = dev.sh(inverse_cmd(a))
        if a["type"] == "package":
            st = package_states_for(dev, [a["pkg"]]).get(a["pkg"])
            if st != "enabled":
                if a["method"] == "uninstall" and dev.sdk >= 24:  # older firmware: pm install-existing
                    dev.sh("pm install-existing %s" % a["pkg"])
                    st = package_states_for(dev, [a["pkg"]]).get(a["pkg"])
                if st != "enabled":
                    bad.append((a, out.strip()))
                    continue
        log.append({"type": "undo", "ref": a["id"]})
    log.write_scripts()
    return bad


def package_states_for(dev, pkgs):
    every = package_states(dev)
    return {p: every.get(p, {}).get("state", "missing") for p in pkgs}


# --------------------------------------------------------------------------- health check

def health_checks(dev, expect=None):
    """-> list of (name, ok, detail). expect = recon dict for context."""
    res = []

    def chk(name, cond, detail=""):
        res.append((name, bool(cond), detail))

    try:
        alive = "revive-ok" in dev.sh("echo revive-ok", timeout=20)
    except ReviveError as e:
        return [("adb shell responds", False, str(e))]
    chk("adb shell responds", alive)
    chk("Android finished booting", dev.prop("sys.boot_completed") == "1")
    chk("package manager responds", "package:" in dev.sh("pm path android"))
    ps = dev.sh("ps -A" if dev.sdk >= 26 else "ps")
    chk("System UI is running", "com.android.systemui" in ps)
    states = package_states(dev)

    def enabled(p):
        return states.get(p, {}).get("state") == "enabled"

    chk("Settings app enabled", enabled("com.android.settings"))
    homes = home_handlers(dev)
    if homes is not None:
        real = [h for h in homes if h != "com.android.settings" and enabled(h)]
        chk("a launcher (HOME app) is available", real, ", ".join(sorted(real)) or "none")
    else:
        known = [p for p in KNOWN_LAUNCHERS if enabled(p)]
        had = expect and [p for p in KNOWN_LAUNCHERS
                          if expect.get("packages", {}).get(p, {}).get("state") == "enabled"]
        chk("a known launcher is enabled", known or not had, ", ".join(known) or "could not verify")
    ime = component_pkgs(settings_get(dev, "secure", "default_input_method"))
    if ime:
        chk("keyboard enabled", all(enabled(p) for p in ime), ", ".join(ime))
    if expect and expect.get("webview"):
        chk("WebView provider enabled", enabled(expect["webview"]), expect["webview"])
    return res


def print_health(res):
    good = True
    for name, passed, detail in res:
        (ok if passed else fail)(name + (" (%s)" % detail if detail else ""))
        good = good and passed
    return good


def wake(dev):
    dev.sh("input keyevent 224")  # WAKEUP
    dev.sh("input keyevent 82")   # MENU: dismisses a swipe lock screen


def reboot_and_wait(dev, timeout=300):
    t0 = time.time()
    dev.adb("reboot", timeout=30)
    time.sleep(float(os.environ.get("REVIVE_REBOOT_GRACE", "8")))
    while time.time() - t0 < timeout:
        try:
            rc, _, _ = dev.adb("wait-for-device", timeout=max(5, int(timeout - (time.time() - t0))))
            if dev.prop("sys.boot_completed") == "1":
                return round(time.time() - t0, 1)
        except ReviveError:
            pass
        time.sleep(1)
    raise ReviveError("device did not finish booting within %ss" % timeout)


# --------------------------------------------------------------------------- commands

def cmd_preflight(args):
    head("Preflight checks")
    good = True
    base = adb_cmd()
    if not base:
        fail("adb is not installed. macOS: `brew install android-platform-tools` · "
             "Linux: `sudo apt install adb` · Windows: download platform-tools from developer.android.com")
        return 1
    _, out, _ = run_adb(["version"])
    ok("adb found: " + (out.splitlines() or ["?"])[0])
    try:
        dev = pick_device(args.serial)
    except ReviveError as e:
        fail(str(e))
        return 1
    ok("device connected and authorized: " + dev.serial)
    model = "%s %s" % (dev.prop("ro.product.manufacturer"), dev.prop("ro.product.model"))
    rel = dev.prop("ro.build.version.release")
    info("%s · Android %s (API %s)" % (model.strip(), rel, dev.sdk))
    if dev.sdk < MIN_SDK:
        fail("Android below 5.0 is not supported (pm disable-user is unreliable there).")
        good = False
    bat = dev.sh("dumpsys battery")
    lvl = re.search(r"level:\s*(\d+)", bat)
    charging = re.search(r"(AC|USB|Wireless) powered:\s*true", bat)
    if lvl:
        level = int(lvl.group(1))
        if level < 30 and not charging:
            fail("battery at %d%% and not charging - charge to 30%%+ first (a reboot test is part of the flow)." % level)
            good = False
        else:
            ok("battery %d%%%s" % (level, ", charging" if charging else ""))
    g = google_account_count(dev)
    if g:
        warn("%d Google account(s) on the device. If you plan a FACTORY RESET, remove them first "
             "(Settings > Accounts) or Factory Reset Protection will lock the device after the reset." % g)
    else:
        ok("no Google account on the device (no Factory Reset Protection risk)")
    if dev.sh("which su").strip():
        info("root detected - not needed: android-revive never uses root.")
    if dev.sdk < 24:
        warn("Android < 7: launcher detection is limited; the tool will protect all known launchers.")
    head("Result: " + ("READY" if good else "NOT READY"))
    return 0 if good else 1


def cmd_recon(args):
    dev = pick_device(args.serial)
    session = find_session(args) if args.session else new_session(args, dev)
    head("Recon · " + session)
    states = package_states(dev)
    props = {k: dev.prop(k) for k in [
        "ro.product.manufacturer", "ro.product.brand", "ro.product.model", "ro.product.device",
        "ro.build.version.release", "ro.build.version.sdk", "ro.build.display.id",
        "ro.build.version.security_patch", "ro.product.cpu.abi", "ro.build.type"]}
    recon = {
        "taken_at": now_iso(), "serial": dev.serial, "sdk": dev.sdk, "props": props,
        "telephony": has_telephony(dev), "root": bool(dev.sh("which su").strip()),
        "google_accounts": google_account_count(dev),
        "home_handlers": sorted(home_handlers(dev) or []),
        "webview": current_webview(dev),
        "protected": dynamic_protection(dev),
        "packages": states,
    }
    write_json(os.path.join(session, "recon.json"), recon)
    en = sum(1 for v in states.values() if v["state"] == "enabled")
    sysn = sum(1 for v in states.values() if v["system"])
    ok("%s %s · Android %s (API %s)" % (props["ro.product.manufacturer"], props["ro.product.model"],
                                         props["ro.build.version.release"], dev.sdk))
    info("%d packages (%d system, %d user) · %d enabled" % (len(states), sysn, len(states) - sysn, en))
    info("telephony: %s · root: %s" % ("yes" if recon["telephony"] else "no", "yes" if recon["root"] else "no"))
    info("protected on this device: " + (", ".join("%s (%s)" % kv for kv in sorted(recon["protected"].items())) or "-"))
    ok("saved recon.json")
    return 0


def cmd_measure(args):
    dev = pick_device(args.serial)
    session = find_session(args, dev, create=True)
    head("Measure '%s' · %s" % (args.label, os.path.basename(session)))
    m = {"label": args.label, "taken_at": now_iso(), "conditions": {}}
    if args.boot:
        info("Rebooting to measure boot time and start from a clean state…")
        m["boot_s"] = reboot_and_wait(dev)
        ok("boot completed in %.1fs" % m["boot_s"])
    if args.settle:
        info("Waiting %ss for the system to settle (background services start after boot)…" % args.settle)
        time.sleep(args.settle)
    m["conditions"] = {"after_reboot": bool(args.boot), "settle_s": args.settle,
                       "uptime_s": float((dev.sh("cat /proc/uptime").split() or ["0"])[0])}
    for attempt in range(3):  # under heavy load right after boot the dump can come back incomplete
        mem = parse_dumpsys_meminfo(dev.sh("dumpsys meminfo", timeout=180))
        if "ram_used_mb" in mem and "processes" in mem:
            break
        warn("dumpsys meminfo came back incomplete, retrying in 15s…")
        time.sleep(15)
    m.update(mem)
    m.update({k: v for k, v in parse_proc_meminfo(dev.sh("cat /proc/meminfo")).items()
              if k not in m or k == "ram_available_mb"})
    ps = dev.sh("ps -A" if dev.sdk >= 26 else "ps").splitlines()[1:]
    m["apps_running"] = sum(1 for l in ps if re.match(r"u\d+_a\d+", l))
    states = package_states(dev)
    m["packages_total"] = len(states)
    m["packages_enabled"] = sum(1 for v in states.values() if v["state"] == "enabled")
    df = [l.split() for l in dev.sh("df /data").splitlines()[1:] if l.strip()]
    if df and len(df[-1]) >= 3:
        m["data_used_mb"] = parse_size_mb(df[-1][2])
    la = dev.sh("cat /proc/loadavg").split()
    if la:
        m["load_1m"] = float(la[0])
    if not args.quick:
        wake(dev)
        times = []
        for _ in range(args.runs):
            out = open_settings(dev, wait=True)
            t = re.search(r"TotalTime:\s*(\d+)", out) or re.search(r"WaitTime:\s*(\d+)", out)
            if t:
                times.append(int(t.group(1)))
            time.sleep(float(os.environ.get("REVIVE_PAUSE", "2")))
        if times:
            m["settings_cold_start_ms"] = int(statistics.median(times))
            m["settings_cold_start_runs"] = times
        j = jank_test(dev)
        if j:
            m.update(j)
        dev.sh("input keyevent 3")
    path = os.path.join(session, "measure-%s.json" % args.label)
    write_json(path, m)
    for key in METRIC_ORDER:
        if m.get(key) is not None:
            d = METRICS[key]
            info("%-28s %s" % (d["label"], fmt(m[key], d["unit"])))
    ok("saved " + os.path.basename(path))
    return 0


def parse_size_mb(s):
    m = re.match(r"([\d.]+)([KMGT]?)", s.upper())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "":  # df on Android 7+ prints 1K blocks
        return round(n / 1024)
    return round(n * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit])


def open_settings(dev, wait):
    """Cold-start Settings. The explicit component works everywhere; the action
    is a fallback (on some ROMs `am start -a android.settings.SETTINGS` fails)."""
    flags = "-W -S" if wait else "-S"
    out = dev.sh("am start %s -n com.android.settings/.Settings" % flags, timeout=60)
    if "Error" in out:
        out = dev.sh("am start %s -a android.settings.SETTINGS" % flags, timeout=60)
    return out


def jank_test(dev):
    if dev.sdk < 23:
        return None
    if "Error" in open_settings(dev, wait=False):
        return None
    time.sleep(float(os.environ.get("REVIVE_PAUSE", "2.5")))
    size = re.findall(r"(\d+)x(\d+)", dev.sh("wm size"))
    w, h = (int(size[-1][0]), int(size[-1][1])) if size else (720, 1280)
    dev.sh("dumpsys gfxinfo com.android.settings reset")
    for _ in range(4):
        dev.sh("input swipe %d %d %d %d 250" % (w // 2, int(h * .75), w // 2, int(h * .25)))
        dev.sh("input swipe %d %d %d %d 250" % (w // 2, int(h * .25), w // 2, int(h * .75)))
    out = dev.sh("dumpsys gfxinfo com.android.settings")
    res = {}
    t = re.search(r"Total frames rendered:\s*(\d+)", out)
    j = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", out)
    p = re.search(r"90th percentile:\s*(\d+)ms", out)
    if t and int(t.group(1)) > 20 and j:
        res["frames_rendered"] = int(t.group(1))
        res["jank_pct"] = float(j.group(2))
        if p:
            res["frame_p90_ms"] = int(p.group(1))
    return res


def cmd_snapshot(args, dev=None, session=None, quiet=False):
    dev = dev or pick_device(args.serial)
    session = session or find_session(args, dev, create=True)
    path = os.path.join(session, "snapshot.json")
    if os.path.exists(path) and not getattr(args, "force", False):
        if not quiet:
            warn("snapshot.json already exists (it is the reference for rollback --to-snapshot); "
                 "keeping it. Use --force to overwrite.")
        return 0
    snap = {"taken_at": now_iso(), "serial": dev.serial, "packages": package_states(dev), "settings": {}}
    for ns in ("global", "secure", "system"):
        kv = {}
        for line in dev.sh("settings list %s" % ns).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k] = v
        snap["settings"][ns] = kv
    write_json(path, snap)
    ActionLog(session, dev.serial).write_scripts()
    if not quiet:
        ok("snapshot saved: %d packages, %d settings" % (
            len(snap["packages"]), sum(len(v) for v in snap["settings"].values())))
    return 0


def build_plan(dev, args, entries):
    states = package_states(dev)
    never = load_never()
    prot = dynamic_protection(dev)
    tel = has_telephony(dev)
    max_tier = TIERS.index(args.tier)
    keep = set(args.keep or [])
    plan, skipped = [], []
    targets = dict(entries)
    for p in (args.only or []):
        targets.setdefault(p, {"tier": "recommended", "note": "requested with --only", "list": "cli"})
    for pkg, e in sorted(targets.items(), key=lambda kv: (TIERS.index(kv[1]["tier"]), kv[0])):
        if args.only and pkg not in args.only:
            continue
        st = states.get(pkg, {}).get("state")
        if st is None:
            continue  # not on this device
        reason = None
        if never_match(pkg, never):
            reason = "never-touch (%s)" % never_match(pkg, never)
        elif pkg in prot:
            reason = "protected: " + prot[pkg]
        elif tel and pkg in TELEPHONY_PACKAGES and not args.allow_telephony:
            reason = "phone/SMS app on a device with a SIM slot (--allow-telephony to override)"
        elif pkg in keep:
            reason = "kept with --keep"
        elif e["tier"] == "extreme" and not args.allow_extreme:
            reason = "extreme tier (--allow-extreme)"
        elif TIERS.index(e["tier"]) > max_tier and not args.only and e["tier"] != "extreme":
            continue
        elif st != "enabled":
            continue  # already disabled/removed
        if reason:
            if st == "enabled" and (TIERS.index(e["tier"]) <= max_tier or args.only or e["tier"] == "extreme"):
                skipped.append((pkg, e, reason))
            continue
        plan.append((pkg, e))
    return plan, skipped


def cmd_debloat(args):
    dev = pick_device(args.serial)
    session = find_session(args, dev, create=True)
    if args.method == "uninstall" and dev.sdk < 26:
        raise ReviveError("--method uninstall needs Android 8+ (to be restorable). Use the default 'disable'.")
    log = ActionLog(session, dev.serial)
    if args.reapply:
        return reapply(dev, log)
    entries = load_lists(args.list)
    plan, skipped = build_plan(dev, args, entries)
    head("Debloat plan · tier ≤ %s · method %s · %s" % (args.tier, args.method,
                                                        "APPLY" if args.apply else "DRY RUN"))
    cur = None
    for pkg, e in plan:
        if e["tier"] != cur:
            cur = e["tier"]
            print("  [%s]" % cur)
        print("    %-52s %s" % (pkg, e["note"]))
    if skipped:
        print("  [protected - will NOT be touched]")
        for pkg, e, why in skipped:
            print("    %-52s %s" % (pkg, why))
    info("%d packages to %s, %d protected" % (len(plan), args.method, len(skipped)))
    write_json(os.path.join(session, "plan.json"),
               {"tier": args.tier, "method": args.method,
                "plan": [{"pkg": p, **e} for p, e in plan],
                "skipped": [{"pkg": p, "reason": r, **e} for p, e, r in skipped]})
    if not args.apply:
        info("Dry run only. Re-run with --apply to execute (rollback is generated first).")
        return 0
    if not plan:
        ok("nothing to do")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise ReviveError("refusing to apply without confirmation: pass --yes")
        if input("Type YES to apply %d changes: " % len(plan)).strip() != "YES":
            warn("aborted")
            return 1
    cmd_snapshot(args, dev, session, quiet=True)
    recon = read_json(os.path.join(session, "recon.json"))
    pre = health_checks(dev, recon)
    if not all(r[1] for r in pre):
        print_health(pre)
        raise ReviveError("the device is not healthy BEFORE any change - stopping.")
    done, failed = 0, []
    batches = [plan[i:i + args.batch] for i in range(0, len(plan), args.batch)]
    for bi, batch in enumerate(batches, 1):
        head("Batch %d/%d" % (bi, len(batches)))
        applied = []
        for pkg, e in batch:
            a = log.append({"type": "package", "pkg": pkg, "method": args.method, "tier": e["tier"],
                            "note": e["note"], "from": "enabled",
                            "to": "uninstalled-user" if args.method == "uninstall" else "disabled"})
            out = dev.sh(forward_cmd(a)).strip()
            applied.append(a)
            print("    %-52s %s" % (pkg, out.splitlines()[-1] if out else ""))
        states = package_states_for(dev, [a["pkg"] for a in applied])
        for a in applied:
            if states[a["pkg"]] == "enabled":
                failed.append(a["pkg"])
                log.append({"type": "undo", "ref": a["id"]})  # nothing changed, drop it
        time.sleep(float(os.environ.get("REVIVE_PAUSE", "2")))
        res = health_checks(dev, recon)
        if not all(r[1] for r in res):
            print_health(res)
            fail("health check failed after this batch - reverting it now.")
            bad = undo_entries(dev, log, [a for a in applied if a["pkg"] not in failed])
            if bad:
                fail("could not revert: %s - run rollback.sh" % ", ".join(a["pkg"] for a, _ in bad))
            print_health(health_checks(dev, recon))
            raise ReviveError("stopped. Find which package of this batch is needed with --batch 1, "
                              "or add it to --keep.")
        done += len(applied) - len([a for a in applied if a["pkg"] in failed])
        ok("batch healthy")
    log.write_scripts()
    if failed:
        warn("the system refused %d package(s) (normal for some OEM apps): %s" % (len(failed), ", ".join(failed)))
    ok("%d packages changed. Undo any time: revive.py rollback  (or sh %s)" %
       (done, os.path.join(session, "rollback.sh")))
    info("Next: revive.py guard --reboot   (proves the device still boots)")
    return 0


def reapply(dev, log):
    head("Re-applying changes the system reverted on its own")
    acts = [a for a in log.active() if a["type"] == "package"]
    states = package_states_for(dev, [a["pkg"] for a in acts])
    n = 0
    for a in acts:
        if states[a["pkg"]] == "enabled":
            dev.sh(forward_cmd(a))
            info("re-applied " + a["pkg"])
            n += 1
    ok("%d re-applied" % n)
    return 0


def cmd_tune(args):
    dev = pick_device(args.serial)
    session = find_session(args, dev, create=True)
    log = ActionLog(session, dev.serial)
    changes = []
    if args.animations != "keep":
        val = {"fast": "0.5", "off": "0"}[args.animations]
        for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            changes.append(("global", key, val, "animations %s" % args.animations))
    if args.scans_off:
        changes.append(("global", "wifi_scan_always_enabled", "0", "no Wi-Fi scanning while Wi-Fi is off"))
        changes.append(("global", "ble_scan_always_enabled", "0", "no Bluetooth scanning while BT is off"))
    head("Tune · " + ("APPLY" if args.apply else "DRY RUN"))
    todo = []
    for ns, key, val, why in changes:
        old = settings_get(dev, ns, key)
        mark = "=" if old == val else "→"
        print("    %-7s %-32s %s %s %s   %s" % (ns, key, old, mark, val, why))
        if old != val:
            todo.append((ns, key, old, val, why))
    if args.trim_caches:
        print("    trim app caches (pm trim-caches) - frees cache files, nothing to undo")
    if args.compile:
        if dev.sdk < 24:
            warn("--compile needs Android 7+, skipped")
            args.compile = False
        else:
            print("    pre-compile apps (cmd package compile -m speed-profile -a) - can take 10-30 min")
    if not args.apply:
        info("Dry run only. Re-run with --apply.")
        return 0
    cmd_snapshot(args, dev, session, quiet=True)
    for ns, key, old, val, why in todo:
        a = log.append({"type": "setting", "ns": ns, "key": key, "old": old, "new": val, "note": why})
        dev.sh(forward_cmd(a))
    if args.trim_caches:
        dev.sh("pm trim-caches 999G", timeout=300)
    if args.compile:
        info("compiling… (the screen may stutter meanwhile)")
        log.append({"type": "command", "cmd": "cmd package compile -m speed-profile -a",
                    "undo": "cmd package compile --reset -a", "note": "pre-compile apps"})
        dev.sh("cmd package compile -m speed-profile -a", timeout=3600)
    ok("%d settings changed" % len(todo))
    return 0


def cmd_guard(args):
    dev = pick_device(args.serial)
    try:
        session = find_session(args, dev)
        recon = read_json(os.path.join(session, "recon.json"))
    except ReviveError:
        session, recon = None, None
    if args.reboot:
        head("Reboot test")
        try:
            t = reboot_and_wait(dev, timeout=args.timeout)
            ok("booted in %.1fs" % t)
        except ReviveError as e:
            fail(str(e))
            print(open(os.path.join(SKILL_DIR, "references", "troubleshooting.md"), encoding="utf-8").read()
                  .split("## Device does not finish booting")[1].split("\n## ")[0])
            return 2
    head("Health check")
    good = print_health(health_checks(dev, recon))
    if session:
        log = ActionLog(session, dev.serial)
        acts = [a for a in log.active() if a["type"] == "package"]
        if acts:
            st = package_states_for(dev, [a["pkg"] for a in acts])
            drift = [a["pkg"] for a in acts if st[a["pkg"]] == "enabled"]
            if drift:
                warn("re-enabled by the system since: %s  →  revive.py debloat --reapply" % ", ".join(drift))
            else:
                ok("all %d changes still in place" % len(acts))
    head("Result: " + ("HEALTHY" if good else "PROBLEM - run `revive.py rollback`"))
    return 0 if good else 1


def cmd_rollback(args):
    dev = pick_device(args.serial)
    session = find_session(args, dev)
    log = ActionLog(session, dev.serial)
    acts = log.active()
    if args.package:
        acts = [a for a in acts if a.get("pkg") in args.package]
        if not acts:
            raise ReviveError("no active change for: " + ", ".join(args.package))
    head("Rollback · %d change(s) · %s" % (len(acts), os.path.basename(session)))
    for a in sorted(acts, key=lambda e: -e["id"]):
        print("    " + inverse_cmd(a))
    if not args.yes and sys.stdin.isatty():
        if input("Type YES to roll back: ").strip() != "YES":
            warn("aborted")
            return 1
    bad = undo_entries(dev, log, acts)
    if args.to_snapshot:
        snap = read_json(os.path.join(session, "snapshot.json"))
        if not snap:
            raise ReviveError("no snapshot.json in this session")
        now = package_states(dev)
        for p, v in snap["packages"].items():
            if v["state"] == "enabled" and now.get(p, {}).get("state") == "disabled":
                dev.sh("pm enable " + p)
                info("re-enabled %s (enabled in snapshot)" % p)
            elif v["state"] == "enabled" and now.get(p, {}).get("state") == "uninstalled-user":
                dev.sh("cmd package install-existing " + p)
                info("restored %s (present in snapshot)" % p)
        for ns, kv in snap["settings"].items():
            for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale",
                        "wifi_scan_always_enabled", "ble_scan_always_enabled"):
                if ns == "global" and key in kv and settings_get(dev, ns, key) != kv[key]:
                    dev.sh("settings put global %s %s" % (key, shlex.quote(kv[key])))
    if bad:
        for a, out in bad:
            fail("could not restore %s: %s" % (a.get("pkg"), out))
        return 1
    ok("rollback complete. Reboot to be sure: adb reboot")
    return 0


def cmd_status(args):
    dev = pick_device(args.serial)
    session = find_session(args, dev)
    log = ActionLog(session, dev.serial)
    acts = log.active()
    head("Session " + session)
    for f in ("recon.json", "measure-before.json", "snapshot.json", "measure-after.json", "report.html"):
        (ok if os.path.exists(os.path.join(session, f)) else info)(f + ("" if os.path.exists(os.path.join(session, f)) else "  (missing)"))
    info("%d active changes (%d packages, %d settings)" % (
        len(acts), sum(a["type"] == "package" for a in acts), sum(a["type"] == "setting" for a in acts)))
    return 0


# --------------------------------------------------------------------------- report

METRICS = {
    "ram_used_mb": {"label": "RAM used", "unit": "MB", "better": "lower"},
    "ram_free_mb": {"label": "RAM truly free", "unit": "MB", "better": "higher"},
    "ram_available_mb": {"label": "RAM available to apps", "unit": "MB", "better": "higher"},
    "processes": {"label": "Processes in memory", "unit": "", "better": "lower"},
    "apps_running": {"label": "App processes running", "unit": "", "better": "lower"},
    "packages_enabled": {"label": "Enabled packages", "unit": "", "better": "lower"},
    "settings_cold_start_ms": {"label": "Settings cold start", "unit": "ms", "better": "lower"},
    "jank_pct": {"label": "Janky frames (scroll test)", "unit": "%", "better": "lower"},
    "frame_p90_ms": {"label": "Frame time p90", "unit": "ms", "better": "lower"},
    "boot_s": {"label": "Boot time", "unit": "s", "better": "lower"},
    "load_1m": {"label": "CPU load (1 min)", "unit": "", "better": "lower"},
}
METRIC_ORDER = ["ram_used_mb", "ram_free_mb", "ram_available_mb", "processes", "apps_running",
                "packages_enabled", "settings_cold_start_ms", "jank_pct", "frame_p90_ms", "boot_s", "load_1m"]


def fmt(v, unit):
    if v is None:
        return "–"
    s = ("%.1f" % v) if isinstance(v, float) and v != int(v) else "{:,}".format(int(round(v)))
    return s + ((" " + unit) if unit and unit != "%" else unit)


def delta(b, a, better):
    if b in (None, 0) or a is None:
        return None, None
    pct = (a - b) / float(b) * 100
    improved = (a < b) if better == "lower" else (a > b)
    return pct, improved


SVG_STYLE = """<style>
  .t{font:600 15px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;fill:#0b0b0b}
  .l{font:500 13px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;fill:#0b0b0b}
  .v{font:500 12px ui-monospace,SFMono-Regular,Menlo,monospace;fill:#52514e}
  .d{font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;fill:#0b0b0b}
  .m{font:400 12px system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;fill:#52514e}
  .bg{fill:#fcfcfb;stroke:#e4e3de}
  .b0{fill:#86b6ef}.b1{fill:#2a78d6}.k{fill:#c3c2b7}.grid{stroke:#e4e3de;stroke-width:1}
  @media (prefers-color-scheme: dark){
    .t,.l,.d{fill:#f0efec}.v,.m{fill:#c3c2b7}
    .bg{fill:#1a1a19;stroke:#383835}
    .b0{fill:#184f95}.b1{fill:#3987e5}.k{fill:#52514e}.grid{stroke:#383835}
  }
</style>"""


PAD = 24


def svg_open(width, h, title):
    W, H = width + 2 * PAD, h + 2 * PAD
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
            'aria-label="%s">%s<rect class="bg" x="0.5" y="0.5" width="%d" height="%d" rx="14"/>'
            '<g transform="translate(%d %d)">' % (W, H, W, H, html.escape(title), SVG_STYLE, W - 1, H - 1, PAD, PAD))


def pct_label(pct, imp):
    mark = "✓" if imp else "✗"
    if pct > 200:
        return "%s ×%.1f" % (mark, 1 + pct / 100.0)
    return "%s %+.0f%%" % (mark, pct)


def svg_before_after(rows, title, width=760):
    """Small multiples: each metric gets its own row and its own scale (starting at 0)."""
    lab_w, val_w, d_w = 210, 90, 90
    bar_w = width - lab_w - val_w - d_w - 20
    rh, bh = 58, 15
    top = 72
    h = top + rh * len(rows) + 16
    o = [svg_open(width, h, title),
         '<text class="t" x="0" y="20">%s</text>' % html.escape(title),
         '<rect class="b0" x="0" y="38" width="12" height="12" rx="3"/><text class="m" x="18" y="49">Before</text>',
         '<rect class="b1" x="80" y="38" width="12" height="12" rx="3"/><text class="m" x="98" y="49">After</text>',
         '<text class="m" x="%d" y="49" text-anchor="end">change</text>' % (width - 4)]
    for i, (label, b, a, unit, better) in enumerate(rows):
        y = top + i * rh
        mx = max(b or 0, a or 0) or 1
        o.append('<line class="grid" x1="0" x2="%d" y1="%d" y2="%d"/>' % (width, y - 8, y - 8))
        o.append('<text class="l" x="0" y="%d">%s</text>' % (y + bh + 6, html.escape(label)))
        o.append('<text class="m" x="0" y="%d">%s is better</text>' % (y + bh + 22, better))
        for j, (v, cls) in enumerate(((b, "b0"), (a, "b1"))):
            yy = y + j * (bh + 4)
            w = max(2, bar_w * (v or 0) / mx)
            o.append('<rect class="%s" x="%d" y="%d" width="%.1f" height="%d" rx="4"><title>%s %s: %s</title></rect>'
                     % (cls, lab_w, yy, w, bh, html.escape(label), ("before", "after")[j], fmt(v, unit)))
            o.append('<text class="v" x="%.1f" y="%d">%s</text>' % (lab_w + w + 6, yy + bh - 3, fmt(v, unit)))
        pct, imp = delta(b, a, better)
        if pct is not None:
            o.append('<text class="d" x="%d" y="%d" text-anchor="end">%s</text>'
                     % (width - 4, y + bh + 6, pct_label(pct, imp)))
    o.append("</g></svg>")
    return "\n".join(o)


def proc_owner(name, pkgs):
    for p in pkgs:
        if name == p or name.startswith(p + ":") or name.startswith(p + "."):
            return p
    return None


def svg_top_processes(procs, removed, title, width=760):
    procs = procs[:12]
    lab_w, rh = 290, 26
    top = 70
    h = top + rh * len(procs) + 10
    mx = max(p["pss_mb"] for p in procs) or 1
    bar_w = width - lab_w - 80
    o = [svg_open(width, h, title),
         '<text class="t" x="0" y="20">%s</text>' % html.escape(title),
         '<rect class="b1" x="0" y="38" width="12" height="12" rx="3"/><text class="m" x="18" y="49">Disabled by android-revive</text>',
         '<rect class="k" x="200" y="38" width="12" height="12" rx="3"/><text class="m" x="218" y="49">Kept</text>']
    for i, p in enumerate(procs):
        y = top + i * rh
        gone = proc_owner(p["name"], removed) is not None
        w = max(2, bar_w * p["pss_mb"] / mx)
        o.append('<text class="l" x="0" y="%d">%s</text>' % (y + 13, html.escape(p["name"][:40])))
        o.append('<rect class="%s" x="%d" y="%d" width="%.1f" height="16" rx="4"><title>%s: %s MB</title></rect>'
                 % ("b1" if gone else "k", lab_w, y + 1, w, html.escape(p["name"]), p["pss_mb"]))
        o.append('<text class="v" x="%.1f" y="%d">%s MB</text>' % (lab_w + w + 6, y + 13, int(round(p["pss_mb"]))))
    o.append("</g></svg>")
    return "\n".join(o)


def report_rows(before, after):
    rows = []
    for k in METRIC_ORDER:
        b, a = before.get(k), after.get(k)
        if b is not None and a is not None:
            d = METRICS[k]
            rows.append((d["label"], b, a, d["unit"], d["better"]))
    return rows


def removed_packages(session):
    acts = []
    p = os.path.join(session, "actions.jsonl")
    if os.path.exists(p):
        log = ActionLog(session, "")
        acts = [a for a in log.active() if a["type"] == "package"]
    return acts


def cmd_report(args):
    session = find_session(args)
    before = read_json(os.path.join(session, "measure-before.json"))
    after = read_json(os.path.join(session, "measure-after.json"))
    if not before or not after:
        raise ReviveError("need measure-before.json and measure-after.json in " + session)
    recon = read_json(os.path.join(session, "recon.json"), {})
    acts = removed_packages(session)
    settings = [a for a in ActionLog(session, "").active() if a["type"] == "setting"] \
        if os.path.exists(os.path.join(session, "actions.jsonl")) else []
    removed = {a["pkg"] for a in acts}
    rows = report_rows(before, after)
    charts = os.path.join(session, "charts")
    os.makedirs(charts, exist_ok=True)
    svg1 = svg_before_after(rows, "Before vs after")
    with open(os.path.join(charts, "before-after.svg"), "w", encoding="utf-8") as f:
        f.write(svg1)
    svg2 = ""
    if before.get("top_processes"):
        svg2 = svg_top_processes(before["top_processes"], removed,
                                 "Biggest RAM users before (PSS)")
        with open(os.path.join(charts, "top-ram-before.svg"), "w", encoding="utf-8") as f:
            f.write(svg2)
    props = recon.get("props", {})
    device = ("%s %s" % (props.get("ro.product.manufacturer", ""), props.get("ro.product.model", ""))).strip() \
        or read_json(os.path.join(session, "session.json"), {}).get("model", "Android device")
    android = props.get("ro.build.version.release", "?")
    # markdown summary (also handy for a post)
    md = ["# %s · android-revive report" % device, "",
          "Android %s · %s · %d packages disabled, %d settings tuned" % (
              android, after.get("taken_at", "")[:10], len(acts), len(settings)), "",
          "| Metric | Before | After | Change |", "|---|---:|---:|---:|"]
    for label, b, a, unit, better in rows:
        pct, imp = delta(b, a, better)
        md.append("| %s | %s | %s | %s |" % (label, fmt(b, unit), fmt(a, unit),
                                             "–" if pct is None else pct_label(pct, imp)))
    md += ["", "Measured with android-revive %s. Undo everything: `sh rollback.sh`." % VERSION]
    with open(os.path.join(session, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    # html
    tiles = []
    for label, b, a, unit, better in rows[:4]:
        pct, imp = delta(b, a, better)
        tiles.append('<div class="tile"><div class="tl">%s</div><div class="tv">%s</div>'
                     '<div class="ts">from %s · <b>%s</b></div></div>' % (
                         html.escape(label), fmt(a, unit), fmt(b, unit),
                         "" if pct is None else pct_label(pct, imp)))
    pkg_rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
        html.escape(a["pkg"]), a.get("tier", ""), html.escape(a.get("note", ""))) for a in acts)
    set_rows = "".join("<tr><td><code>%s %s</code></td><td>%s → %s</td><td>%s</td></tr>" % (
        a["ns"], a["key"], a["old"], a["new"], html.escape(a.get("note", ""))) for a in settings)
    cond = []
    for lab, m in (("before", before), ("after", after)):
        c = m.get("conditions", {})
        cond.append("%s: %s, %s" % (lab, m.get("taken_at", "?"),
                                     "fresh reboot + %ss settle" % c.get("settle_s") if c.get("after_reboot")
                                     else "uptime %ss" % int(c.get("uptime_s", 0))))
    page = REPORT_HTML.format(
        title=html.escape(device), sub="Android %s · %d packages disabled · %d settings tuned" % (
            android, len(acts), len(settings)),
        tiles="".join(tiles), chart1=svg1,
        chart2='<h2>Where the memory was going</h2><div class="chart">%s</div>' % svg2 if svg2 else "",
        pkgs=pkg_rows or '<tr><td colspan="3">none</td></tr>',
        sets=set_rows or '<tr><td colspan="3">none</td></tr>',
        cond=html.escape(" · ".join(cond)), version=VERSION)
    with open(os.path.join(session, "report.html"), "w", encoding="utf-8") as f:
        f.write(page)
    head("Report")
    print("\n".join(md[4:4 + len(rows) + 2]))
    ok("report.html, summary.md and charts/*.svg written to " + session)
    return 0


REPORT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · android-revive</title>
<style>
:root{{--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--line:#e4e3de;--accent:#2a78d6;color-scheme:light}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121211;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#383835;--accent:#3987e5;color-scheme:dark}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:32px 16px 64px}}h1{{font-size:28px;margin:0}}h2{{font-size:18px;margin:40px 0 12px}}
.sub{{color:var(--ink2);margin:4px 0 24px}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.tl{{color:var(--ink2);font-size:13px}}.tv{{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}}.ts{{color:var(--ink2);font-size:13px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;overflow-x:auto}}svg{{max-width:100%;height:auto}}.chart{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
code{{font:13px ui-monospace,SFMono-Regular,Menlo,monospace}}.note{{color:var(--ink2);font-size:13px}}
</style></head><body><main>
<h1>{title}</h1><p class="sub">{sub}</p>
<div class="tiles">{tiles}</div>
<h2>All measurements</h2><div class="chart">{chart1}</div>
{chart2}
<h2>Packages disabled</h2><div class="card"><table><tr><th>Package</th><th>Tier</th><th>What it is</th></tr>{pkgs}</table></div>
<h2>Settings changed</h2><div class="card"><table><tr><th>Setting</th><th>Value</th><th>Why</th></tr>{sets}</table></div>
<h2>Undo</h2><div class="card"><p>Everything above is reversible. From this folder: <code>sh rollback.sh</code> (macOS/Linux) or
<code>rollback.cmd</code> (Windows), or <code>revive.py rollback</code>. Single app: <code>revive.py rollback --package NAME</code>.</p></div>
<p class="note">Measurement conditions — {cond}. Generated by android-revive {version}.</p>
</main></body></html>
"""


# --------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="revive.py", description="Make an old Android device lighter, safely.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd")

    def p(name, fn, helptext):
        sp = sub.add_parser(name, help=helptext, description=helptext)
        sp.add_argument("--serial", help="device serial (when several are connected)")
        sp.add_argument("--session", help="session folder (default: latest for this device)")
        sp.add_argument("--root", help="where sessions are stored (default ./android-revive-sessions)")
        sp.set_defaults(fn=fn)
        return sp

    p("preflight", cmd_preflight, "check adb, connection, battery, accounts")
    p("recon", cmd_recon, "identify the device and start a new session")
    sp = p("measure", cmd_measure, "measure RAM, processes, app start, jank, boot")
    sp.add_argument("--label", default="before", choices=["before", "after"])
    sp.add_argument("--boot", action="store_true", help="reboot first and measure boot time (recommended)")
    sp.add_argument("--settle", type=int, default=90, help="seconds to wait before measuring (default 90)")
    sp.add_argument("--runs", type=int, default=3, help="cold-start repetitions (median is kept)")
    sp.add_argument("--quick", action="store_true", help="skip app-start and scroll tests")
    sp = p("snapshot", cmd_snapshot, "save package and settings state (rollback reference)")
    sp.add_argument("--force", action="store_true")
    sp = p("debloat", cmd_debloat, "disable unneeded packages (dry run unless --apply)")
    sp.add_argument("--tier", default="safe", choices=TIERS[:3])
    sp.add_argument("--allow-extreme", action="store_true", help="also allow 'extreme' entries (e.g. Google Play services)")
    sp.add_argument("--allow-telephony", action="store_true")
    sp.add_argument("--method", default="disable", choices=["disable", "uninstall"])
    sp.add_argument("--list", action="append", help="extra list file (pkg | tier | note)")
    sp.add_argument("--only", nargs="+", help="only these packages")
    sp.add_argument("--keep", nargs="+", help="never touch these packages")
    sp.add_argument("--batch", type=int, default=5)
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    sp.add_argument("--reapply", action="store_true", help="re-disable packages the system re-enabled")
    sp = p("tune", cmd_tune, "animations, background scans, caches, compilation")
    sp.add_argument("--animations", default="fast", choices=["fast", "off", "keep"])
    sp.add_argument("--scans-off", action="store_true", default=True)
    sp.add_argument("--keep-scans", dest="scans_off", action="store_false")
    sp.add_argument("--trim-caches", action="store_true")
    sp.add_argument("--compile", action="store_true")
    sp.add_argument("--apply", action="store_true")
    sp = p("guard", cmd_guard, "health check (optionally after a reboot)")
    sp.add_argument("--reboot", action="store_true")
    sp.add_argument("--timeout", type=int, default=300)
    sp = p("rollback", cmd_rollback, "undo changes of the session")
    sp.add_argument("--package", nargs="+", help="undo only these packages")
    sp.add_argument("--to-snapshot", action="store_true", help="also re-enable anything enabled in the snapshot")
    sp.add_argument("--yes", action="store_true")
    p("status", cmd_status, "show the session state")
    p("report", cmd_report, "build report.html, summary.md and charts from the measurements")

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    try:
        return args.fn(args)
    except ReviveError as e:
        fail(str(e))
        return 1
    except KeyboardInterrupt:
        warn("interrupted - every change made so far is already in rollback.sh")
        return 130


if __name__ == "__main__":
    sys.exit(main())
