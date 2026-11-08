"""End-to-end tests against the fake device: python3 -m unittest discover tests"""
import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIVE = os.path.join(ROOT, "skills", "android-revive", "scripts", "revive.py")
FAKE = os.path.join(ROOT, "tests", "fake_adb.py")


class Cycle(unittest.TestCase):
    def setUp(self, **kw):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "device.json")
        self.env = dict(os.environ, REVIVE_ADB=FAKE, FAKE_ADB_STATE=self.state, REVIVE_PAUSE="0",
                        REVIVE_REBOOT_GRACE="0", NO_COLOR="1", REVIVE_HOME=os.path.join(self.tmp, "s"))
        self.init()

    def init(self, *extra):
        subprocess.check_call([sys.executable, FAKE, "--init", self.state] + list(extra))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_revive(self, *args, expect=0):
        p = subprocess.run([sys.executable, REVIVE] + list(args), env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, expect, p.stdout)
        return p.stdout

    def device(self):
        with open(self.state) as f:
            return json.load(f)

    def session(self):
        root = os.path.join(self.tmp, "s")
        return os.path.join(root, sorted(os.listdir(root))[-1])

    def states(self):
        return {p: v["state"] for p, v in self.device()["pkgs"].items()}

    # ------------------------------------------------------------------
    def test_full_cycle_and_rollback(self):
        before = self.states()
        before_settings = self.device()["settings"]
        self.run_revive("preflight")
        self.run_revive("recon")
        self.run_revive("measure", "--label", "before", "--boot", "--settle", "0", "--runs", "1")
        out = self.run_revive("debloat", "--tier", "recommended")
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.states(), before, "dry run must not change anything")
        self.run_revive("debloat", "--tier", "recommended", "--apply", "--yes", "--batch", "2")
        st = self.states()
        for p in ("com.android.egg", "com.facebook.appmanager", "com.android.email", "com.google.android.youtube"):
            self.assertEqual(st[p], "disabled", p)
        # never-touch, protected and higher tiers untouched
        for p in ("com.android.systemui", "com.android.settings", "com.android.launcher3",
                  "com.google.android.webview", "com.android.inputmethod.latin", "com.google.android.gms",
                  "com.android.calculator2", "com.google.android.marvin.talkback", "com.example.userapp"):
            self.assertEqual(st[p], "enabled", p)
        self.run_revive("tune", "--apply")
        self.assertEqual(self.device()["settings"]["global"]["window_animation_scale"], "0.5")
        self.run_revive("guard", "--reboot")
        self.run_revive("measure", "--label", "after", "--boot", "--settle", "0", "--runs", "1")
        self.run_revive("report")
        s = self.session()
        for f in ("report.html", "summary.md", "charts/before-after.svg", "rollback.sh", "rollback.cmd"):
            self.assertTrue(os.path.exists(os.path.join(s, f)), f)
        b = json.load(open(os.path.join(s, "measure-before.json")))
        a = json.load(open(os.path.join(s, "measure-after.json")))
        self.assertLess(a["ram_used_mb"], b["ram_used_mb"])
        self.assertLess(a["packages_enabled"], b["packages_enabled"])
        self.run_revive("rollback", "--yes")
        self.assertEqual(self.states(), before)
        self.assertEqual(self.device()["settings"], before_settings)

    def test_rollback_sh_alone_restores_everything(self):
        before = self.states()
        self.run_revive("recon")
        self.run_revive("debloat", "--tier", "aggressive", "--apply", "--yes")
        self.assertNotEqual(self.states(), before)
        env = dict(self.env, ADB=sys.executable + " " + FAKE)
        script = os.path.join(self.session(), "rollback.sh")
        # rollback.sh quotes "$ADB"; run it through a tiny wrapper
        wrapper = os.path.join(self.tmp, "adb")
        with open(wrapper, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        os.chmod(wrapper, 0o755)
        env["ADB"] = wrapper
        subprocess.check_call(["sh", script], env=env, stdout=subprocess.DEVNULL)
        self.assertEqual(self.states(), before)

    def test_bad_batch_is_reverted_automatically(self):
        self.init("--break", "com.android.egg")  # disabling it "crashes" System UI
        before = self.states()
        self.run_revive("recon")
        out = self.run_revive("debloat", "--apply", "--yes", "--batch", "3", expect=1)
        self.assertIn("reverting", out)
        self.assertEqual(self.states(), before)

    def test_telephony_protection(self):
        self.init("--telephony")
        self.run_revive("recon")
        self.run_revive("debloat", "--tier", "aggressive", "--apply", "--yes")
        st = self.states()
        self.assertEqual(st["com.android.dialer"], "enabled")
        self.assertEqual(st["com.android.messaging"], "enabled")
        self.assertEqual(st["com.android.calculator2"], "disabled")

    def test_extreme_needs_flag_and_never_touch_wins(self):
        self.run_revive("recon")
        self.run_revive("debloat", "--tier", "aggressive", "--apply", "--yes")
        self.assertEqual(self.states()["com.google.android.gms"], "enabled")
        self.run_revive("debloat", "--allow-extreme", "--apply", "--yes")
        self.assertEqual(self.states()["com.google.android.gms"], "disabled")
        out = self.run_revive("debloat", "--only", "com.android.systemui", "com.android.launcher3", "--apply", "--yes")
        self.assertIn("never-touch", out)
        self.assertEqual(self.states()["com.android.systemui"], "enabled")

    def test_reapply_after_system_reenables(self):
        self.run_revive("recon")
        self.run_revive("debloat", "--apply", "--yes")
        st = self.device()
        st["pkgs"]["com.android.egg"]["state"] = "enabled"  # e.g. an OTA re-enabled it
        with open(self.state, "w") as f:
            json.dump(st, f)
        out = self.run_revive("guard")
        self.assertIn("re-enabled by the system", out)
        self.run_revive("debloat", "--reapply")
        self.assertEqual(self.states()["com.android.egg"], "disabled")

    def test_single_package_rollback(self):
        self.run_revive("recon")
        self.run_revive("debloat", "--apply", "--yes")
        self.run_revive("rollback", "--package", "com.android.egg", "--yes")
        st = self.states()
        self.assertEqual(st["com.android.egg"], "enabled")
        self.assertEqual(st["com.facebook.appmanager"], "disabled")

    def test_uninstall_method_roundtrip(self):
        before = self.states()
        self.run_revive("recon")
        self.run_revive("debloat", "--method", "uninstall", "--apply", "--yes")
        self.assertEqual(self.states()["com.android.egg"], "uninstalled-user")
        self.run_revive("rollback", "--yes")
        self.assertEqual(self.states(), before)

    def test_old_android_uninstall_refused(self):
        self.init("--sdk", "25")
        self.run_revive("recon")
        out = self.run_revive("debloat", "--method", "uninstall", "--apply", "--yes", expect=1)
        self.assertIn("Android 8+", out)


if __name__ == "__main__":
    unittest.main()
