# Troubleshooting

## `adb devices` shows nothing
Data cable? USB debugging on? Try another port, `adb kill-server && adb devices`, on Windows install
the OEM USB driver. On the device choose USB mode "File transfer"/"MTP" if asked.

## `unauthorized`
Unlock the device, accept the RSA prompt, tick "Always allow". No prompt? Developer options >
Revoke USB debugging authorizations, unplug, replug.

## `pm disable-user` fails with "Shell cannot change component state" or SecurityException
- Xiaomi: enable "USB debugging (Security settings)".
- Some OEMs block disabling specific apps. revive.py reports them as "refused" and continues.
- Try `--method uninstall` (Android 8+): removes the app for the user only, restorable.

## Health check failed during debloat
revive.py already reverted that batch. Find the culprit:
`revive.py debloat --tier <tier> --apply --yes --batch 1` - the failing package is printed; keep it
with `--keep <pkg>` and please open an issue so it can be moved out of that tier.

## Device does not finish booting
Stay calm: disabled apps are still on the device, nothing was deleted.
1. Wait 5 minutes - the first boot after changes can be slow (it re-optimizes apps).
2. adb usually works during a stuck boot. Run `revive.py rollback --yes`
   (or `sh rollback.sh` from the session folder), then `adb reboot`.
3. If adb does not see the device: hold Power (+ Volume Down on many models) 10-20 s to force a
   restart, then try step 2 as soon as `adb devices` shows it.
4. Boot into **Safe mode** (on most devices: hold Volume Down while the logo appears). Safe mode
   starts only system apps; adb often works there - run the rollback.
5. Last resort: factory reset from recovery (see prep-guide step 4). It re-enables every app; your
   data is lost unless you backed it up, which is why the guide starts with a backup.

## No home screen / black screen after unlocking
The launcher protection should make this impossible, but if it happens:
`adb shell pm enable <launcher package>` (see `recon.json` → `home_handlers`), or
`revive.py rollback --to-snapshot`.

## Keyboard missing
`adb shell pm enable com.android.inputmethod.latin` (or your keyboard), then
`adb shell ime reset`.

## Apps came back after an update / reset
`revive.py guard` lists them; `revive.py debloat --reapply` disables them again. Play Store is known to
re-enable itself when Play services are enabled.

## Some app crashes now
Something it depends on was disabled. `revive.py rollback --package <pkg>` for the most likely
candidates (Google Play services, WebView provider, Google app), or roll back the whole session.
