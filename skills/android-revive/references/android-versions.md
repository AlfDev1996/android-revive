# What works on which Android version

| Feature | Min Android (API) | Notes |
|---|---|---|
| `pm disable-user --user 0` | 5.0 (21) | Default method. Undo: `pm enable`. Survives reboots and OTA (most of the time). |
| `pm uninstall -k --user 0` | 5.0, restorable from 8.0 (26) | revive.py allows it only on 8.0+ where `cmd package install-existing` brings the app back. Also hides the app from Settings. |
| `cmd package query-activities` (launcher detection) | 7.0 (24) | Below 7.0 all known launchers are protected by name. |
| `dumpsys gfxinfo` janky frames | 6.0 (23) | Scroll test skipped below. |
| `cmd package compile` | 7.0 (24) | `tune --compile`. Undo: `cmd package compile --reset -a`. |
| `/proc/meminfo` MemAvailable | kernel 3.14 | Older kernels: MemFree + Cached approximation. |
| `ps -A` | 8.0 (26) | Before 8.0 plain `ps` lists everything. |
| Factory Reset Protection | 5.1 (22) | Remove accounts before resetting. |

Android 4.x is not supported: `pm disable-user` from adb is unreliable there and FRP-era safety
assumptions do not hold.

## Brand notes
- **Samsung**: Knox packages are never-touch (warranty bit, Secure Folder, payments). One UI re-enables
  some apps after major updates → `debloat --reapply`.
- **Xiaomi/MIUI/HyperOS**: needs "USB debugging (Security settings)". `com.miui.securitycenter`,
  `com.lbe.security.miui`, `com.xiaomi.finddevice` and theme manager cause bootloops → never-touch.
- **LineageOS / custom ROMs**: setup wizards and updaters of abandoned builds are safe to disable.
- **Huawei (EMUI)**: `com.huawei.systemmanager` is never-touch.
