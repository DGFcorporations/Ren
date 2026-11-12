# Ren on the workstation and the phone

Ren is the same assistant as the Wren service in this repo. The phone is a companion. The workstation is the agent. Nothing is pasted, clicked, or written until it is confirmed.

## Workstation

Requirements: Python 3.12, a desktop session if you want the visible browser.

```bash
./run_ren.sh
```

Open `http://127.0.0.1:8765` on this computer. Off-machine clients need the bearer token. The default matches the phone app. Override both sides with `WREN_AUTH_TOKEN`.

Set `WREN_WORKSPACE` to the folder Ren is allowed to edit. If it is unset, Ren uses the directory you started it from.

Live replies use a local OpenAI-compatible model at `http://127.0.0.1:18880/v1` when one is running. If it is not, Ren still answers with a local fallback sentence and still previews dictation.

## Phone

1. Download the **ren-android** APK from the GitHub Actions artifact or the release on this repo.
2. Install it (Android will ask you to allow installs from the browser or Files app).
3. Open Ren. Set the workstation URL to your LAN or Tailscale address, port `8765`, and the same token the workstation printed.
4. Grant overlay, accessibility, microphone, and notifications.
5. Tap **Launch Ren orb**.

Tap the orb to dictate. You see the words as you speak. When you stop, a card shows the text that would be pasted. **Paste here** writes it into the focused field. **Ask Ren** sends it to the workstation and shows the reply on the card. Nothing is pasted until you press Paste here. Dismiss throws the preview away.

The orb also sends the accessibility screen text to the workstation so Ren and other agents can read what is on the phone.

## Other agents (MCP)

With Ren already running:

```bash
ren-mcp
```

Point Cursor, Claude, or another MCP client at that stdio command:

```json
{
  "mcpServers": {
    "ren": {
      "command": "ren-mcp",
      "env": {"REN_URL": "http://127.0.0.1:8765"}
    }
  }
}
```

Tools:

| Tool | What it does |
|---|---|
| `ren_status` | Mode, draft, last reply |
| `ren_converse` | Talk. Does not paste |
| `ren_dictate` | Stage a clipboard preview |
| `ren_paste_preview` | Read the preview |
| `ren_paste_commit` | Paste on the workstation. Requires `confirm: true` |
| `ren_screen_read` | Phone screen bridge and active desktop window |
| `ren_browser` | Owned Chromium. `navigate` and `snapshot` run immediately. `click`, `type`, `move`, and `scroll` require `confirm: true` |
| `ren_files` | List and read `WREN_WORKSPACE`. Writes require `confirm: true` |
| `ren_set_mode` | `elevate`, `agent`, or `brainstorm` |

Browser control opens Ren's own Chromium. It does not attach to your signed-in daily browser. Mouse actions are page coordinates or a CSS selector inside that window. `file:` and `javascript:` URLs are refused.

File tools cannot leave `WREN_WORKSPACE` and refuse `.env`, `.git`, and key files. There is no shell tool.

## Android build

The debug APK is built by `.github/workflows/android-apk.yml` on every push that touches `android/`. You can also run it by hand from the Actions tab. A connected phone can install `app-debug.apk` with `adb install -r`.

From a machine with the Android SDK:

```bash
cd android
chmod +x gradlew
./gradlew assembleDebug
```

The Gradle wrapper is included. The first build downloads Gradle 8.9 and the Android SDK pieces.

## What is still local-only

Microphone audio is recognized on the phone (Android speech recognizer) and the finished sentence is sent as text. The workstation can still use its own microphone from the desktop orb. Faster-Whisper is optional there and is not downloaded by the phone.

Ren will not drive arbitrary apps with gestures. Screen reading and paste use the accessibility service you turn on, and paste only hits the focused field after the card confirmation.
