# Measurements - what they mean and how to read them honestly

| Metric | Source | Meaning |
|---|---|---|
| RAM used | `dumpsys meminfo` "Used RAM" | memory held by running processes + kernel. The main "lighter" signal. |
| RAM truly free | `dumpsys meminfo` "… free)" | memory nobody uses. Android fills RAM with cache on purpose, so a low number is normal; a big jump means fewer things are running. |
| RAM available to apps | `/proc/meminfo` MemAvailable | what a new app can get without killing others. The best "headroom" signal. |
| Processes in memory | `dumpsys meminfo` process list | fewer = fewer background services waking up the CPU. |
| App processes running | `ps` | app (not native) processes alive right now. |
| Enabled packages | `pm list packages` | how much software can run at all. |
| Settings cold start | `am start -W -S` median of 3 | time to open the Settings app from scratch - a proxy for "the device feels snappy". |
| Janky frames | `dumpsys gfxinfo` during 8 scripted scrolls | % of frames that missed the deadline - smoothness. |
| Boot time | `adb reboot` → `sys.boot_completed=1` | seconds until Android reports it has booted. |

## Fair comparisons
- Use `measure --boot` for **both** before and after: same conditions (fresh boot + 90 s settle).
- Keep the device on the charger, screen on, same Wi-Fi state, no apps open.
- App-start and jank numbers vary ±10-15% run to run on old hardware; treat smaller changes as noise
  and say so in the report.
- The first boot after debloating can be slower (app re-optimization). Measure "after" on the second boot
  if boot time looks odd.

## Emulators
Emulators are fine for testing that android-revive works, not for performance numbers: with no GPU
window the scroll test reports 90%+ janky frames, and CPU load right after boot swings wildly.
