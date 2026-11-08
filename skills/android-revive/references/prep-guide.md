# Preparation guide (manual steps)

These steps need the person's hands. Read them out one at a time and wait for confirmation.

## 1. Charge
Plug the device into a wall charger. It must stay above 30% for the whole session, because the flow
reboots it at least three times. An old battery that dies during a reboot is harmless, just annoying.

## 2. Back up what matters
Photos, videos, documents, WhatsApp chats (WhatsApp > Settings > Chats > Chat backup), notes, 2FA apps
(move authenticator codes to another device **first**). Copy photos to a computer over USB
("File transfer" mode) or with `adb pull /sdcard/DCIM ./backup-DCIM` once adb works.

## 3. Decide: clean the current system, or factory reset first?
- **Keep data** → skip to step 5. android-revive works on a used device too.
- **Factory reset first** (recommended for very old or very cluttered devices: it removes years of
  updates to preinstalled apps, caches and leftovers) → do step 4.

## 4. Factory reset - safely
1. **Remove every Google account BEFORE the reset**: Settings > Accounts (or "Users & accounts") >
   Google > the account > Remove account. Also remove Samsung/Xiaomi accounts
   (Samsung account, Mi account: Settings > Mi Account > Sign out).
   *Why:* Factory Reset Protection (FRP, Android 5.1+) locks a reset device until the **previous**
   account signs in again. Forgotten password = locked device.
2. Settings > System > Reset options > Erase all data (wording varies: "Backup & reset",
   "General management > Reset").
3. If the menu reset fails (it happens on some custom ROMs), use recovery: power off, then hold the
   recovery button combo (often Power + Volume Up; Samsung with Home key: Power + Home + Volume Up) >
   "Wipe data/factory reset". On TWRP: Wipe > Advanced Wipe > Dalvik, Cache, Data - **not** System.
4. During setup after the reset you can skip the Google sign-in if the device will not need Play Store.

## 5. Enable Developer options and USB debugging
1. Settings > About phone (tablet) > tap **Build number** 7 times (enter PIN if asked).
   - Samsung: Settings > About > Software information > Build number
   - Xiaomi: Settings > About phone > MIUI/HyperOS version (tap 7 times)
2. Settings > System > Developer options (Samsung/Xiaomi: at the bottom of Settings, or "Additional settings").
3. Turn on **USB debugging**.
   - Xiaomi: also enable **USB debugging (Security settings)** - needs a Mi account and SIM on some
     models; without it `pm disable-user` fails with a permission error.
   - Some Huawei/Oppo models also need "Allow ADB debugging in charge only mode".
4. Optional, very useful: **Stay awake** (screen never sleeps while charging) - the measurements need
   the screen on. Turn it off at the end if you want.

## 6. Install adb on the computer
- macOS: `brew install android-platform-tools`
- Windows: download "SDK Platform-Tools for Windows" from developer.android.com/tools/releases/platform-tools,
  unzip, open a terminal in that folder. Samsung on Windows may need the Samsung USB driver.
- Linux: `sudo apt install adb` (Debian/Ubuntu) or `sudo dnf install android-tools`.
Python 3.9+ is also needed (`python3 --version`); macOS/Linux usually have it, Windows: python.org.

## 7. Connect and authorize
1. Use a **data** cable (some cheap cables only charge). Unlock the screen.
2. Run `adb devices`. On the device, accept **"Allow USB debugging?"** and tick
   **"Always allow from this computer"**.
3. `adb devices` must show `<serial>  device`. `unauthorized` = the prompt was not accepted
   (unplug/replug, or Developer options > Revoke USB debugging authorizations, then retry).

## 8. Remove the screen lock (optional, for measurements)
The app-start and scroll tests open Settings on the screen. With a PIN/pattern they still run if the
screen is unlocked and "Stay awake" is on; otherwise use `measure --quick`.

Now run `revive.py preflight`.
