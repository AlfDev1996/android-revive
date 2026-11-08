#!/usr/bin/env python3
"""A tiny fake `adb` that simulates one Android device, for tests and CI.

State lives in the JSON file named by $FAKE_ADB_STATE.  Set it up with
`fake_adb.py --init <path> [--sdk N] [--telephony] [--break pkg]` - a package
passed with --break makes System UI "crash" while it is disabled, which is how
the tests prove that a bad batch is detected and reverted automatically."""
import json, os, re, shlex, sys

STATE = os.environ.get("FAKE_ADB_STATE", "")
SERIAL = "FAKE0001"

BASE_PKGS = {
    # pkg: system?
    "android": True, "com.android.systemui": True, "com.android.settings": True,
    "com.android.phone": True, "com.android.providers.media": True,
    "com.android.launcher3": True, "com.android.inputmethod.latin": True,
    "com.google.android.webview": True, "com.android.shell": True,
    "com.android.egg": True, "com.android.dreams.basic": True, "com.android.email": True,
    "com.android.printspooler": True, "com.android.calculator2": True,
    "com.android.dialer": True, "com.android.messaging": True,
    "com.google.android.gms": True, "com.google.android.gsf": True, "com.android.vending": True,
    "com.google.android.youtube": True, "com.google.android.apps.magazines": True,
    "com.google.android.marvin.talkback": True,
    "com.facebook.appmanager": True, "com.facebook.services": True,
    "com.example.userapp": False,
}


def init(path, sdk, telephony, brk, already_disabled):
    st = {
        "sdk": sdk, "telephony": telephony, "break": brk,
        "pkgs": {p: {"system": s, "state": "enabled"} for p, s in BASE_PKGS.items()},
        "settings": {"global": {"window_animation_scale": "1.0", "transition_animation_scale": "1.0",
                                "animator_duration_scale": "1.0", "wifi_scan_always_enabled": "1"},
                     "secure": {"default_input_method": "com.android.inputmethod.latin/.LatinIME",
                                "enabled_accessibility_services": "com.google.android.marvin.talkback/.TalkBackService"},
                     "system": {"screen_brightness": "120"}},
        "boots": 0,
    }
    for p in already_disabled:
        st["pkgs"][p]["state"] = "disabled"
    save(path, st)


def load():
    with open(STATE) as f:
        return json.load(f)


def save(path, st):
    with open(path, "w") as f:
        json.dump(st, f, indent=1)


def enabled(st, p):
    return st["pkgs"].get(p, {}).get("state") == "enabled"


def ui_alive(st):
    return enabled(st, "com.android.systemui") and not (st["break"] and not enabled(st, st["break"]))


def shell(st, cmd):
    a = shlex.split(cmd)
    if not a:
        return ""
    c = a[0]
    if c == "echo":
        return " ".join(a[1:]) + "\n"
    if c == "getprop":
        props = {"ro.build.version.sdk": str(st["sdk"]), "ro.build.version.release": "10",
                 "ro.product.manufacturer": "Fake", "ro.product.model": "Phone X", "ro.product.brand": "fake",
                 "sys.boot_completed": "1"}
        return props.get(a[1], "") + "\n"
    if c == "pm":
        if a[1] == "list" and a[2] == "packages":
            f = a[3:]
            out = []
            for p, v in sorted(st["pkgs"].items()):
                if v["state"] == "uninstalled-user" and "-u" not in f:
                    continue
                if "-d" in f and v["state"] != "disabled":
                    continue
                if "-e" in f and v["state"] != "enabled":
                    continue
                if "-s" in f and not v["system"]:
                    continue
                if "-3" in f and v["system"]:
                    continue
                out.append("package:" + p)
            return "\n".join(out) + "\n"
        if a[1] == "list" and a[2] == "features":
            return "feature:android.hardware.wifi\n" + ("feature:android.hardware.telephony\n" if st["telephony"] else "")
        if a[1] == "disable-user":
            p = a[-1]
            if p not in st["pkgs"]:
                return "Error: unknown package\n"
            st["pkgs"][p]["state"] = "disabled"
            return "Package %s new state: disabled-user\n" % p
        if a[1] == "enable":
            p = a[-1]
            if st["pkgs"].get(p, {}).get("state") == "disabled":
                st["pkgs"][p]["state"] = "enabled"
            return "Package %s new state: enabled\n" % p
        if a[1] == "uninstall":
            st["pkgs"][a[-1]]["state"] = "uninstalled-user"
            return "Success\n"
        if a[1] == "install-existing":
            st["pkgs"][a[-1]]["state"] = "enabled"
            return "Package %s installed for user: 0\n" % a[-1]
        if a[1] == "path":
            return "package:/system/framework/framework-res.apk\n"
        if a[1] == "trim-caches":
            return ""
    if c == "cmd" and a[1] == "package":
        if a[2] == "query-activities":
            homes = [p for p in ("com.android.launcher3", "com.android.settings") if p in st["pkgs"]]
            return "".join("%s/.Home\n" % p for p in homes if enabled(st, p))
        if a[2] == "install-existing":
            st["pkgs"][a[-1]]["state"] = "enabled"
            return "Package %s installed for user: 0\n" % a[-1]
        if a[2] == "compile":
            return "Success\n"
    if c == "settings":
        op, ns = a[1], a[2]
        d = st["settings"].setdefault(ns, {})
        if op == "get":
            return d.get(a[3], "null") + "\n"
        if op == "put":
            d[a[3]] = a[4]
            return ""
        if op == "delete":
            d.pop(a[3], None)
            return "Deleted 1 rows\n"
        if op == "list":
            return "".join("%s=%s\n" % kv for kv in sorted(d.items()))
    if c == "dumpsys":
        what = a[1] if len(a) > 1 else ""
        if what == "battery":
            return "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  level: 81\n"
        if what == "account":
            return "Accounts: 1\n    Account {name=someone@example.com, type=com.google}\n"
        if what == "webviewupdate":
            return "Current WebView package (name, version): (com.google.android.webview, 99.0)\n"
        if what == "device_policy":
            return ""
        if what == "meminfo":
            procs = [("system", 90000), ("com.android.systemui", 60000)]
            for p in sorted(st["pkgs"]):
                if enabled(st, p) and p not in ("android", "com.android.systemui", "com.android.shell"):
                    procs.append((p, 20000 + 1000 * (len(p) % 9)))
            procs.sort(key=lambda x: -x[1])
            used = sum(x[1] for x in procs)
            lines = ["Applications Memory Usage (in Kilobytes):", "", "Total PSS by process:"]
            lines += ["    {:,}K: {} (pid {})".format(k, n, 100 + i) for i, (n, k) in enumerate(procs)]
            lines += ["", "Total RAM: 2,000,000K (status normal)",
                      " Free RAM: {:,}K (  100,000K cached pss + 200,000K cached kernel + {:,}K free)".format(
                          2000000 - used, 1700000 - used),
                      " Used RAM: {:,}K (  {:,}K used pss + 80,000K kernel)".format(used + 80000, used), ""]
            return "\n".join(lines) + "\n"
        if what == "gfxinfo":
            n = sum(1 for v in st["pkgs"].values() if v["state"] == "enabled")
            return "Total frames rendered: 240\nJanky frames: %d (%.2f%%)\n90th percentile: %dms\n" % (
                n, n / 2.4, 10 + n // 3)
    if c == "cat":
        if a[1] == "/proc/meminfo":
            n = sum(1 for v in st["pkgs"].values() if v["state"] == "enabled")
            return "MemTotal: 2000000 kB\nMemFree: %d kB\nMemAvailable: %d kB\nCached: 300000 kB\n" % (
                1200000 - n * 20000, 1500000 - n * 20000)
        if a[1] == "/proc/uptime":
            return "1234.5 4000.1\n"
        if a[1] == "/proc/loadavg":
            return "2.10 2.00 1.90 1/400 1234\n"
    if c == "ps":
        rows = ["USER PID NAME"]
        if ui_alive(st):
            rows.append("u0_a20 900 com.android.systemui")
        rows += ["u0_a%d %d %s" % (30 + i, 1000 + i, p) for i, p in enumerate(sorted(st["pkgs"])) if enabled(st, p)
                 and not st["pkgs"][p]["system"]]
        return "\n".join(rows) + "\n"
    if c == "df":
        return "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/block/dm-0 25000000 9000000 16000000 36% /data\n"
    if c == "am":
        n = sum(1 for v in st["pkgs"].values() if v["state"] == "enabled")
        return "Status: ok\nActivity: com.android.settings/.Settings\nTotalTime: %d\nWaitTime: %d\nComplete\n" % (
            300 + n * 10, 310 + n * 10)
    if c in ("input", "wm"):
        return "Physical size: 1080x1920\n" if c == "wm" else ""
    if c == "which":
        return ""
    if c == "true":
        return ""
    return "/system/bin/sh: %s: not found\n" % c


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--init":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--init")
        ap.add_argument("--sdk", type=int, default=29)
        ap.add_argument("--telephony", action="store_true")
        ap.add_argument("--break", dest="brk")
        ap.add_argument("--disabled", nargs="*", default=[])
        o = ap.parse_args(argv)
        init(o.init, o.sdk, o.telephony, o.brk, o.disabled)
        return 0
    if argv[:1] == ["version"]:
        print("Android Debug Bridge version 1.0.41 (fake)")
        return 0
    if argv[:1] == ["devices"]:
        print("List of devices attached\n%s\tdevice\n" % SERIAL)
        return 0
    if argv[:2] == ["-s", SERIAL]:
        argv = argv[2:]
    elif argv[:1] == ["-s"]:
        sys.stderr.write("error: device '%s' not found\n" % argv[1])
        return 1
    st = load()
    if argv[0] == "shell":
        out = shell(st, " ".join(argv[1:]))
        save(STATE, st)
        sys.stdout.write(out)
        return 0
    if argv[0] == "reboot":
        st["boots"] += 1
        save(STATE, st)
        return 0
    if argv[0] == "wait-for-device":
        return 0
    sys.stderr.write("fake adb: unsupported %s\n" % argv)
    return 1


if __name__ == "__main__":
    sys.exit(main())
