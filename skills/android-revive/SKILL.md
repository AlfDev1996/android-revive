---
name: android-revive
description: Revive an old or slow Android phone/tablet over adb - guided preparation (backup, accounts, optional factory reset, developer options), before/after measurements, safe reversible debloat and tuning, health checks with reboot test, a shareable before/after report, and full rollback. Use when the user wants to debloat, speed up, clean up, "bring back to life" or repurpose (wall display, kiosk, kid tablet) an Android device, or undo such changes.
---

# android-revive

You guide a person through making an Android device lighter and smoother **without ever risking it**.
All device changes go through `scripts/revive.py` (Python 3.9+, stdlib only), which enforces the safety
rules. Run it as `python3 <this skill's folder>/scripts/revive.py <command>`; sessions are written to
`./android-revive-sessions/` in the current directory.

Speak the user's language. Many users are not technical: explain each step in one or two plain sentences,
say what will happen on the device screen, and wait for them when a step needs their hands.

## Golden rules (never break these)

1. **Only use revive.py for changes.** Never run `pm uninstall` without `-k --user 0`, never `rm` anything
   on the device, never flash, unlock bootloaders, root, or use `pm clear` on system apps.
2. **Dry run first, then ask.** Show the plan (`debloat` without `--apply`), explain it in plain words,
   and get an explicit "yes" before `--apply`. `--yes` on the command line is only for after that "yes".
3. **The never-touch list is final.** If the user asks to remove something on it, explain why it is
   dangerous and stop. Do not edit `lists/never-touch.txt` to get around it.
4. **Measure before changing.** No `debloat --apply` before `measure --label before` exists.
5. **Extreme tier** (Google Play services, Play Store) only for single-purpose devices (wall display,
   kiosk, offline media player) and only after the user confirms they understand that most apps
   (WhatsApp, banking, YouTube…) will stop working or stop receiving notifications.
6. **Phones with a SIM**: phone, SMS, contacts and emergency-alert apps are protected automatically.
   Do not pass `--allow-telephony` unless the device will never be used as a phone.
7. **If anything looks wrong, roll back first, investigate later** (`rollback`, then `guard`).
8. Never print or store account e-mails, phone numbers, or personal files in the session folder or chat.

## Flow

### Phase 0 - Understand the goal (ask, don't assume)
Ask: which device, what it will be used for after (daily phone? kid's tablet? wall dashboard? e-reader?),
and whether they want to keep Google apps (Play Store, YouTube, Gmail). The answer picks the tier:

| Use after | Tier | Google apps |
|---|---|---|
| Daily phone/tablet | `safe` → maybe `recommended` | keep |
| Secondary tablet, kids, media | `recommended` | keep |
| One-job device (dashboard, kiosk, music player) | `aggressive` (+ `--allow-extreme` only if no Google needed) | optional |

### Phase 1 - Preparation (the person does this; walk them through it)
Follow `references/prep-guide.md` step by step: charge, backup, **remove Google account before any
factory reset (FRP lock!)**, optional factory reset, enable Developer options and USB debugging, install
adb, authorize the computer. Then:

```
revive.py preflight
```
Fix every ✘ before continuing.

### Phase 2 - Recon and baseline
```
revive.py recon                                  # identifies device, starts a session
revive.py measure --label before --boot          # reboots, waits 90s, measures (≈3-4 min)
```
Summarize recon for the user: model, Android version, how many apps, what is protected and why.
Keep the screen unlocked during `measure` (it opens Settings and scrolls to time it).
Use `--quick` only if the screen has a PIN the user does not want to remove.

### Phase 3 - Plan
```
revive.py debloat --tier <tier>                  # dry run: prints the plan, changes nothing
```
Present the plan grouped by tier, in plain words ("Bixby voice assistant", not package names only).
Let the user strike items: add them with `--keep pkg1 pkg2`. If the device has apps not in the lists
that the user wants gone, write them to a custom list file (`package | recommended | description`)
and pass `--list file.txt` - the never-touch list still applies.

### Phase 4 - Apply, verify
```
revive.py debloat --tier <tier> --apply --yes [--keep …]   # batches of 5, health check after each
revive.py tune --apply                                       # animations 0.5x, no background Wi-Fi/BT scans
revive.py guard --reboot                                     # proves it still boots and works
```
Optional tune flags: `--animations off` (snappiest, less pretty), `--trim-caches`,
`--compile` (Android 7+, pre-compiles apps, 10-30 min, faster app starts).
If a batch fails its health check, revive.py reverts it by itself; rerun that tier with `--batch 1` to
find the culprit and `--keep` it. If `guard --reboot` fails, go to `references/troubleshooting.md`.

### Phase 5 - After, report
```
revive.py measure --label after --boot
revive.py report                                 # report.html + summary.md + charts/*.svg
```
Open `report.html` for the user, explain the 3-4 most meaningful changes honestly (if a metric got worse
or did not move, say so - small differences in app start time are noise). Offer `summary.md` and the SVG
charts if they want to share the result.

### Rollback (any time)
```
revive.py rollback                               # undo everything in this session (newest first)
revive.py rollback --package com.foo.bar         # bring back one app
revive.py rollback --to-snapshot                 # also re-enable anything enabled at snapshot time
sh android-revive-sessions/<session>/rollback.sh # same, without Python (rollback.cmd on Windows)
```
After an OTA update or a factory reset some apps can come back: `revive.py guard` reports them and
`revive.py debloat --reapply` disables them again.

## Reference files
- `references/prep-guide.md` - manual steps for the person, with per-brand notes (read in Phase 1)
- `references/troubleshooting.md` - unauthorized device, bootloop, missing launcher/keyboard, re-enabled apps
- `references/android-versions.md` - what works on which Android version and why
- `references/measurements.md` - what each metric means and how to read it honestly
- `lists/*.txt` - debloat lists (`package | tier | note`); `lists/never-touch.txt` - hard block list
