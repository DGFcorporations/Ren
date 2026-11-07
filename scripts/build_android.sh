#!/usr/bin/env bash
# Build and install Wren Android Companion app via ADB
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_DIR="$SCRIPT_DIR/android"

echo "=== Wren Android Companion Builder ==="
cd "$ANDROID_DIR"

if command -v ./gradlew &>/dev/null; then
    GRADLE_CMD="./gradlew"
elif command -v gradle &>/dev/null; then
    GRADLE_CMD="gradle"
else
    echo "Warning: Gradle wrapper not initialized yet."
    echo "To initialize Gradle wrapper, run: gradle wrapper --gradle-version 8.7"
    echo "Project files are ready under $ANDROID_DIR"
    exit 0
fi

echo "Building debug APK..."
$GRADLE_CMD assembleDebug

APK_PATH="$ANDROID_DIR/app/build/outputs/apk/debug/app-debug.apk"
if [ -f "$APK_PATH" ]; then
    echo "Build successful: $APK_PATH"
    
    # Check for connected ADB devices
    ADB_BIN="$(which adb || echo "/home/chandler/.local/bin/adb")"
    if [ -x "$ADB_BIN" ]; then
        DEVICES=$("$ADB_BIN" devices | grep -v "List" | grep "device$" || true)
        if [ -n "$DEVICES" ]; then
            echo "Installing APK to connected device..."
            "$ADB_BIN" install -r "$APK_PATH"
            echo "Installed successfully!"
        else
            echo "No active Android device connected via ADB. Connect device or run an emulator to install."
        fi
    fi
fi
