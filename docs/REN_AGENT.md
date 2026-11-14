# Ren on the workstation and the phone

Ren is the same assistant as the Wren service in this repo. The phone is a companion. The workstation is the agent. Nothing is pasted, clicked, or written until it is confirmed.

## Workstation

Requirements: Python 3.12, a desktop session if you want the visible browser.

```bash
./run_ren.sh
```

Open `http://127.0.0.1:8765` on this computer. That page is served by the program on this machine. It is not a public site.

Clients that are not on this computer need a bearer token. Ren does not ship a password. On first start it writes one to `~/.config/wren/auth-token` and prints it. Type that value into the phone. Set `WREN_AUTH_TOKEN` before launch if you want to choose the token yourself. Do not commit it, and do not forward port 8765. A token copied from an older checkout is rejected.

Set `WREN_WORKSPACE` to the folder Ren is allowed to edit. If it is unset, Ren uses the directory you started it from.

Live replies use a local OpenAI-compatible model at `http://127.0.0.1:18880/v1` when one is running, with the FreeLLMAPI desktop gateway at `http://127.0.0.1:31415/v1` as fallback. If neither is up, Ren still answers with a local fallback sentence and still previews dictation.

Speech-to-text prefers the workstation bridge (`http://127.0.0.1:8787/v1/audio/transcriptions` — cloud Whisper/Nova AST) and falls back to local Faster-Whisper. Spoken replies prefer the Qwen/QUIN voice (`WREN_TTS_PROVIDER=qwen`, ~4s budget) and fall back to Deepgram Aura through the same bridge, then VoiceStudio `:3900`, then `espeak-ng`. `POST /api/voice/config` and the orb's VOICE node switch providers or mute output. No audio is written to disk — everything stays in memory.

### Floating plasma orb

`--native` launches a frameless, transparent GTK3+WebKit2 floating orb (340×390 stage) instead of a windowed card. The orb:

- renders a chromatic plasma sphere with a radial voice equalizer that glows while you talk and while Ren speaks;
- clicks **through to the window beneath** everywhere outside the orb's active hit area (X11 input shape), so it never covers buttons or text;
- does **not steal focus** — clicking it leaves your current app focused;
- opens a radial fan-out menu on click: TALK (hold-to-talk conversation), DICT (dictate to preview), mode cycle, VOICE cycle/mute, ASK (one-line type-in), PAIR (phone pairing info), QUIT (double-tap to stop);
- drags anywhere to reposition and remembers its spot in `~/.config/wren/orb-position.json`.

### Desktop hotkeys

- **Dictate:** hold `Ctrl+Shift`, speak, release — a preview pill appears; click **Paste** (explicit) to write it into your remembered app. Override with `WREN_HOTKEY`.
- **Talk:** hold `Alt+Space`, speak, release — Ren answers conversationally and speaks the reply. Override with `WREN_TALK_HOTKEY`.
- **Quit:** tap `Ctrl+Alt+Q` (override with `WREN_QUIT_HOTKEY`), or double-tap QUIT in the radial menu. Stops `wren.service` cleanly under systemd.
- Pressing an extra key while a modifier hold is armed (e.g. `Ctrl+Shift+C`) cancels the capture so clipboard shortcuts never collide.
- `Ctrl+Alt` stays reserved for OpenWhispr — the defaults do not collide.

### Safety notes

- **Paste target:** Ren remembers the last app you were working in. Clicking **Paste** inside the orb is safe — it activates that remembered window and pastes there instead of typing into the widget. A background watcher keeps the target fresh while Ren is idle. Paste never sends Enter/Return.
- Consequential actions (paste, browser actions, workspace writes, MCP `ren_paste_commit`) still require explicit confirmation.

## Phone

1. Download the **ren-android** APK from the GitHub Actions artifact or the release on this repo.
2. Install it (Android will ask you to allow installs from the browser or Files app).
3. Open Ren. Type the workstation URL printed at startup (LAN or Tailscale, port `8765`) and the pairing token. The app does not come with an address or a password.
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

### HTTP transport (`/mcp`)

The same nine tools are also served over MCP streamable-HTTP at `http://<host>:8765/mcp` on the running Ren server — this is the transport a remote agent (for example a cloud agent behind a tunnel) uses. Point an MCP client at the URL and pass the pairing token as a bearer credential:

```json
{
  "mcpServers": {
    "ren": {
      "url": "http://<workstation-address>:8765/mcp",
      "headers": {"Authorization": "Bearer <pairing-token>"}
    }
  }
}
```

Loopback clients on the workstation itself do not need the token. Anything reaching Ren through a reverse proxy or tunnel always needs it, even when the proxy runs on the same machine — proxy headers (`x-forwarded-for`, `cf-connecting-ip`, and friends) remove loopback trust on purpose. Tool behavior, confirmation gates, and errors are identical to stdio; `/api/status` reports `mcp_http_mounted` so you can tell whether the endpoint is live.

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
