#!/bin/sh
# Run the full android-revive cycle on real Android emulators.
# Needs the Android SDK (sdkmanager, avdmanager, emulator) and ~3 GB per system image.
#   ANDROID_HOME=~/Library/Android/sdk sh tests/emulator.sh 24 29 34
set -eu
: "${ANDROID_HOME:?set ANDROID_HOME to your Android SDK}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
R="$HERE/skills/android-revive/scripts/revive.py"
ARCH=$(uname -m | sed 's/arm64/arm64-v8a/; s/x86_64/x86_64/')
[ $# -eq 0 ] && set -- 24 29 34
for API in "$@"; do
  IMG="system-images;android-$API;google_apis;$ARCH"
  echo "=== API $API ($IMG)"
  yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" "$IMG" "emulator" "platform-tools" >/dev/null
  echo no | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd -f -n revive$API -k "$IMG" >/dev/null
  "$ANDROID_HOME/emulator/emulator" -avd revive$API -no-window -no-audio -no-snapshot -wipe-data &
  EMU=$!
  adb wait-for-device
  until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
  S="$(mktemp -d)"
  export REVIVE_HOME="$S"
  python3 "$R" preflight
  python3 "$R" recon
  python3 "$R" measure --label before --boot --settle 30 --runs 2
  python3 "$R" debloat --tier aggressive --apply --yes
  python3 "$R" tune --apply
  python3 "$R" guard --reboot
  python3 "$R" measure --label after --boot --settle 30 --runs 2
  python3 "$R" report
  python3 "$R" rollback --yes
  python3 "$R" guard --reboot
  echo "API $API OK - report in $S"
  adb emu kill || kill $EMU
  sleep 5
done
