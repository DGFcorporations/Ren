#!/usr/bin/env bash
# Build the Wren Android companion. Device installation is explicit via --install.
set -euo pipefail

INSTALL=false
if [[ "${1-}" == "--install" ]]; then
    INSTALL=true
    shift
fi
if [[ $# -ne 0 ]]; then
    echo "Usage: $0 [--install]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_DIR="$SCRIPT_DIR/android"
cd "$ANDROID_DIR"

echo "=== Wren Android Companion Builder ==="

if [[ -x ./gradlew ]]; then
    GRADLE_CMD=(./gradlew)
elif command -v gradle >/dev/null 2>&1; then
    GRADLE_CMD=(gradle)
else
    echo "ERROR: Android build not run: no executable Gradle wrapper or system Gradle found." >&2
    echo "Add a checked-in Gradle 8.7 wrapper or install Gradle 8.7, then rerun this command." >&2
    exit 127
fi

if [[ -z "${ANDROID_HOME:-}" && -z "${ANDROID_SDK_ROOT:-}" ]]; then
    echo "ERROR: Android build cannot be validated: ANDROID_HOME/ANDROID_SDK_ROOT is not set." >&2
    echo "Install the Android SDK and configure its path before building." >&2
    exit 2
fi

echo "Building debug APK..."
"${GRADLE_CMD[@]}" --no-daemon assembleDebug

APK_PATH="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
if [[ ! -f "$APK_PATH" ]]; then
    echo "ERROR: Gradle reported success but the debug APK was not found at $APK_PATH" >&2
    exit 1
fi

echo "Build successful: $APK_PATH"

if [[ "$INSTALL" == true ]]; then
    if ! command -v adb >/dev/null 2>&1; then
        echo "ERROR: --install was requested, but adb is not installed or not on PATH." >&2
        exit 127
    fi
    DEVICES="$(adb devices | awk 'NR > 1 && $2 == "device" { print $1 }')"
    if [[ -z "$DEVICES" ]]; then
        echo "ERROR: --install was requested, but no authorized Android device is connected." >&2
        exit 1
    fi
    adb install -r "$APK_PATH"
    echo "Installed successfully."
fi
