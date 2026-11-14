"""Wren Floating Chromatic Plasma Orb, Radial Menu, and REST/WS/MCP Server."""

import hashlib
import json
import logging
import os
import secrets
import socket
import stat
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from wren.core.controller import WrenController
from wren.files.ingest import FileIngestError

# SHA-256 of the old built-in token. The token itself is not stored here.
RETIRED_TOKEN_SHA256 = "1843dd013bce8d1d9f0c9810a03895f67f0e718e016d2cfc81a2d28f8fbe1437"

# Optional workstation plasma orb artwork (procedural plasma renders cleanly if absent).
WORKSTATION_ORB_JPG = Path("/home/chandler/Dev/repos/dgf-ops/workstation/static/ag-plasma-orb.jpg")


def is_loopback_client(client_host: str | None, request_host: str | None) -> bool:
    """Determine whether the request originates from a local loopback interface."""
    loopback_peers = {"127.0.0.1", "::1", "localhost", "testclient"}
    loopback_targets = {"127.0.0.1", "::1", "localhost", "testserver"}

    if not client_host or client_host not in loopback_peers:
        return False
    if request_host and request_host not in loopback_targets:
        return False
    return True


_PROXY_HEADERS = (
    "cf-connecting-ip",
    "cf-ray",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)


def has_proxy_headers(headers) -> bool:
    """True when the request arrived through a forwarding proxy."""
    return any(name in headers for name in _PROXY_HEADERS)


def extract_token_from_request(request: Request) -> str | None:
    """Extract auth token from Authorization header, X-Wren-Token, or query parameter."""
    auth_hdr = request.headers.get("authorization", "")
    if auth_hdr.startswith("Bearer "):
        return auth_hdr[7:].strip()
    if "x-wren-token" in request.headers:
        return request.headers["x-wren-token"].strip()
    token_param = request.query_params.get("token")
    if token_param:
        return token_param.strip()
    return None


class ReviseRequest(BaseModel):
    instruction: str


class ReadFileRequest(BaseModel):
    file_path: str


class AttachFileRequest(BaseModel):
    file_path: str


class ModeRequest(BaseModel):
    mode: str


class ListenStartRequest(BaseModel):
    intent: str = "auto"


class DictateRequest(BaseModel):
    text: str
    source: str = "android"


class ConverseRequest(BaseModel):
    text: str
    speak: bool = False
    src: str = "orb"


class SpeakRequest(BaseModel):
    text: str
    provider: str | None = None


class VoiceConfigRequest(BaseModel):
    provider: str | None = None
    muted: bool | None = None


class HitRegionRequest(BaseModel):
    mode: str = "orb"


class WindowMoveRequest(BaseModel):
    dx: int = 0
    dy: int = 0


class WindowFocusRequest(BaseModel):
    accept: bool = False


class PhoneScreenRequest(BaseModel):
    package: str = ""
    text: str = ""


class BrowserActRequest(BaseModel):
    op: str
    url: str | None = None
    selector: str | None = None
    text: str | None = None
    x: float | None = None
    y: float | None = None
    delta_y: float | None = None
    confirm: bool = False
    approval_id: str | None = None


class WorkspaceWriteRequest(BaseModel):
    path: str
    content: str = ""
    confirm: bool = False
    approval_id: str | None = None


class WorkspaceReadRequest(BaseModel):
    path: str


def token_store_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "wren" / "auth-token"


def _usable_token(candidate: str | None) -> str | None:
    if candidate is None:
        return None
    value = candidate.strip()
    if not value:
        return None
    digest = hashlib.sha256(value.encode()).hexdigest()
    if digest == RETIRED_TOKEN_SHA256:
        return None
    return value


def _write_token(path: Path, token: str) -> None:
    if path.is_symlink():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)


def resolve_auth_token(explicit: str | None = None, *, persist: bool = True) -> tuple[str, bool]:
    """Return the bearer token for non-loopback clients."""
    replaced = False
    if explicit is not None:
        chosen = _usable_token(explicit)
        if chosen:
            return chosen, False
        if explicit.strip():
            replaced = True
    else:
        env_token = os.environ.get("WREN_AUTH_TOKEN")
        chosen = _usable_token(env_token)
        if chosen:
            return chosen, False
        if env_token and env_token.strip():
            replaced = True

    path = token_store_path()
    if path.is_file() and not path.is_symlink():
        stored = _usable_token(path.read_text(encoding="utf-8"))
        if stored:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                path.chmod(0o600)
            return stored, replaced

    generated = secrets.token_urlsafe(32)
    if persist:
        _write_token(path, generated)
    return generated, replaced


def get_network_ips() -> dict[str, str]:
    """Detect local LAN IP and Tailscale IP for mobile companion connection."""
    lan_ip = ""
    tailscale_ip = ""

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        detected_lan = s.getsockname()[0]
        s.close()
        if detected_lan and not detected_lan.startswith("127."):
            lan_ip = detected_lan
    except Exception:
        pass

    try:
        res = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=1)
        if res.returncode == 0 and res.stdout.strip():
            tailscale_ip = res.stdout.strip().split()[0]
    except Exception:
        pass

    return {"lan_ip": lan_ip, "tailscale_ip": tailscale_ip}


FLOATING_PLASMA_ORB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Wren Assistant</title>
<style>
  :root {
    --obsidian: rgba(5, 10, 22, 0.92);
    --cyan: #38bdf8;
    --neon: #00f5ff;
    --violet: #a855f7;
    --magenta: #ff2bd6;
    --emerald: #10b981;
    --danger: #f43f5e;
    --fg: #e2e8f0;
    --muted: #94a3b8;
    --font: "JetBrains Mono", "Inter", system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%;
    height: 100%;
    background: transparent !important;
    overflow: hidden;
    font-family: var(--font);
    color: var(--fg);
    user-select: none;
    -webkit-user-select: none;
  }

  #stage {
    position: relative;
    width: 340px;
    height: 390px;
    margin: 0 auto;
    background: transparent;
    pointer-events: none;
  }

  /* Floating Preview / Conversation Pill — only visible when active */
  #draft-box {
    position: absolute;
    left: 14px;
    right: 14px;
    bottom: 138px;
    background: var(--obsidian);
    border: 1px solid rgba(56, 189, 248, 0.62);
    border-radius: 14px;
    padding: 10px 12px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.68), 0 0 22px rgba(0, 245, 255, 0.20);
    display: none;
    flex-direction: column;
    gap: 8px;
    pointer-events: auto;
    animation: floatIn 0.18s cubic-bezier(0.16, 1, 0.3, 1);
  }
  #draft-box.visible { display: flex; }

  @keyframes floatIn {
    from { opacity: 0; transform: translateY(10px) scale(0.96); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }

  .pill-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--cyan);
    font-weight: 700;
  }
  .target-chip {
    color: var(--muted);
    font-weight: 500;
    max-width: 170px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-transform: none;
  }
  .icon-close {
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 12px;
    padding: 0 3px;
    line-height: 1;
  }
  .icon-close:hover { color: #fff; }

  #pill-text {
    font-size: 12px;
    line-height: 1.45;
    color: var(--fg);
    max-height: 112px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
    user-select: text;
    -webkit-user-select: text;
  }

  .pill-actions {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }
  .pbtn {
    background: rgba(15, 25, 45, 0.9);
    color: var(--fg);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 8px;
    padding: 4px 9px;
    font: 700 10.5px var(--font);
    letter-spacing: 0.03em;
    cursor: pointer;
    transition: all 0.12s ease;
  }
  .pbtn:hover {
    border-color: var(--neon);
    color: #fff;
    box-shadow: 0 0 10px rgba(0, 245, 255, 0.28);
  }
  .pbtn.primary {
    background: linear-gradient(135deg, #0284c7, #00c8ff);
    color: #020813;
    border-color: var(--neon);
  }
  .pbtn.primary:hover {
    filter: brightness(1.1);
    color: #000;
  }

  /* Companion pairing drawer (mobile bridge info) */
  #pair-drawer {
    position: absolute;
    left: 16px;
    right: 16px;
    top: 8px;
    display: none;
    flex-direction: column;
    gap: 6px;
    background: var(--obsidian);
    border: 1px solid rgba(56, 189, 248, 0.55);
    border-radius: 12px;
    padding: 10px 12px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.65), 0 0 16px rgba(0, 245, 255, 0.18);
    pointer-events: auto;
    animation: floatIn 0.18s cubic-bezier(0.16, 1, 0.3, 1);
  }
  #pair-drawer.visible { display: flex; }
  #connect-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 10.5px;
  }
  .connect-item {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    color: var(--muted);
  }
  .connect-val {
    color: var(--fg);
    font-weight: 700;
    text-align: right;
    word-break: break-all;
  }

  /* Compact 1-line floating prompt input */
  #ask-bar {
    position: absolute;
    left: 16px;
    right: 16px;
    bottom: 138px;
    display: none;
    align-items: center;
    gap: 6px;
    background: var(--obsidian);
    border: 1px solid var(--neon);
    border-radius: 999px;
    padding: 5px 8px 5px 13px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.65), 0 0 18px rgba(0, 245, 255, 0.24);
    pointer-events: auto;
  }
  #ask-bar.visible { display: flex; }
  #ask-input {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: #fff;
    font: 12px var(--font);
  }
  #ask-input::placeholder { color: var(--muted); }

  /* Radial Fan-Out Menu around the Plasma Orb */
  #radial-menu {
    position: absolute;
    left: 170px;
    top: 322px;
    width: 0;
    height: 0;
    pointer-events: none;
  }
  .rnode {
    position: absolute;
    left: -21px;
    top: -21px;
    width: 42px;
    height: 42px;
    border-radius: 50%;
    background: rgba(6, 12, 26, 0.92);
    border: 1px solid rgba(56, 189, 248, 0.55);
    color: var(--fg);
    font: 700 9.5px var(--font);
    letter-spacing: 0.04em;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    opacity: 0;
    transform: translate(0px, 0px) scale(0.35);
    transition: transform 0.22s cubic-bezier(0.16, 1, 0.3, 1),
                opacity 0.16s ease,
                border-color 0.14s ease,
                box-shadow 0.14s ease;
    pointer-events: none;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.65), 0 0 10px rgba(56, 189, 248, 0.18);
  }
  #radial-menu.open .rnode {
    opacity: 1;
    transform: translate(var(--tx), var(--ty)) scale(1);
    pointer-events: auto;
  }
  .rnode:hover {
    border-color: var(--neon);
    color: #fff;
    box-shadow: 0 0 16px rgba(0, 245, 255, 0.52);
  }
  .rnode.active {
    background: rgba(0, 245, 255, 0.20);
    border-color: var(--neon);
    color: var(--neon);
  }
  .rnode.danger {
    border-color: var(--danger);
    color: var(--danger);
  }
  .rnode .ricon { font-size: 12px; line-height: 1; }
  .rnode .rlbl { font-size: 8px; margin-top: 2px; opacity: 0.9; }

  /* Core Chromatic Plasma Orb Anchor */
  #orb-anchor {
    position: absolute;
    left: 95px;
    top: 247px;
    width: 150px;
    height: 150px;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }
  #plasma-canvas {
    width: 150px;
    height: 150px;
    display: block;
    pointer-events: none;
  }
  #orb-hit {
    position: absolute;
    width: 100px;
    height: 100px;
    border-radius: 50%;
    cursor: pointer;
    pointer-events: auto;
    background: transparent;
  }

  #micro-tag {
    position: absolute;
    left: 50%;
    bottom: 4px;
    transform: translateX(-50%);
    background: rgba(5, 10, 22, 0.80);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 999px;
    padding: 1px 8px;
    font: 700 9px var(--font);
    letter-spacing: 0.08em;
    color: var(--cyan);
    white-space: nowrap;
    pointer-events: none;
    transition: all 0.15s ease;
  }
  #micro-tag.listening {
    border-color: var(--neon);
    color: #fff;
    box-shadow: 0 0 12px rgba(0, 245, 255, 0.55);
  }
  #micro-tag.speaking {
    border-color: var(--magenta);
    color: #fff;
    box-shadow: 0 0 12px rgba(255, 43, 214, 0.50);
  }
</style>
</head>
<body>
<div id="stage">
  <!-- Floating Preview / Reply Pill -->
  <div id="draft-box">
    <div class="pill-head">
      <span id="pill-kind">DRAFT</span>
      <span class="target-chip" id="pill-target"></span>
      <button class="icon-close" title="Dismiss" onclick="dismissPill()">✕</button>
    </div>
    <div id="pill-text"></div>
    <div class="pill-actions" id="pill-actions-draft">
      <button class="pbtn primary" id="btn-paste" onclick="pasteDraft()">Paste</button>
      <button class="pbtn" id="btn-copy" onclick="copyDraft()">Copy</button>
      <button class="pbtn" onclick="askFromDraft()">Ask Ren</button>
      <button class="pbtn" onclick="revertDraft()">Raw</button>
    </div>
    <div class="pill-actions" id="pill-actions-reply" style="display:none;">
      <button class="pbtn primary" onclick="triggerListen('talk')">Reply</button>
      <button class="pbtn" id="btn-copy-reply" onclick="copyReply()">Copy</button>
      <button class="pbtn" onclick="ReplaySpeech()">Replay</button>
    </div>
  </div>

  <!-- Compact 1-line Ask Ren Input -->
  <div id="ask-bar">
    <input id="ask-input" type="text" placeholder="Ask Ren anything… (Enter)" />
    <button class="pbtn primary" onclick="submitAsk()">Send</button>
    <button class="icon-close" onclick="closeAsk()">✕</button>
  </div>

  <!-- 7-Node Radial Fan-Out Menu -->
  <div id="radial-menu">
    <button class="rnode" style="--tx:-100px; --ty:-18px;"
      title="Talk with Ren (Hold Alt+Space)" onclick="triggerListen('talk')">
      <span class="ricon">◉</span><span class="rlbl">TALK</span>
    </button>
    <button class="rnode" style="--tx:-82px; --ty:-61px;"
      title="Dictate to preview (Hold Ctrl+Shift)" onclick="triggerListen('dictate')">
      <span class="ricon">✦</span><span class="rlbl">DICT</span>
    </button>
    <button class="rnode" id="node-mode" style="--tx:-46px; --ty:-91px;"
      title="Cycle Dictation Mode" onclick="cycleMode()">
      <span class="ricon" id="lbl-mode-icon">⚡</span><span class="rlbl" id="lbl-mode">ELV</span>
    </button>
    <button class="rnode" style="--tx:0px; --ty:-102px;"
      title="Type a question to Ren" onclick="toggleAsk()">
      <span class="ricon">💬</span><span class="rlbl">ASK</span>
    </button>
    <button class="rnode" id="node-voice" style="--tx:46px; --ty:-91px;"
      title="Cycle Voice: Qwen / Deepgram / Studio / Mute" onclick="cycleVoice()">
      <span class="ricon">♫</span><span class="rlbl" id="lbl-voice">QWN</span>
    </button>
    <button class="rnode" id="node-pair" style="--tx:82px; --ty:-61px;"
      title="Pair phone / companion device" onclick="togglePair()">
      <span class="ricon">⇄</span><span class="rlbl">PAIR</span>
    </button>
    <button class="rnode" id="node-quit" style="--tx:100px; --ty:-18px;"
      title="Quit Ren (Ctrl+Alt+Q)" onclick="shutdownRen()">
      <span class="ricon" id="lbl-quit-icon">⏻</span><span class="rlbl" id="lbl-quit">QUIT</span>
    </button>
  </div>

  <!-- Companion Pairing Drawer -->
  <div id="pair-drawer">
    <div class="pill-head">
      <span>📱 COMPANION BRIDGE</span>
      <button class="icon-close" title="Close" onclick="togglePair()">✕</button>
    </div>
    <div id="connect-body">
      <div class="connect-item"><span>LAN IP:</span>
        <span class="connect-val" id="lan-ip">not detected</span></div>
      <div class="connect-item"><span>Tailscale IP:</span>
        <span class="connect-val" id="tailscale-ip">not detected</span></div>
      <div class="connect-item"><span>WebSocket:</span><span class="connect-val">ws://…/ws/connect</span></div>
      <div class="connect-item"><span>Token:</span>
        <span class="connect-val">~/.config/wren/auth-token</span></div>
    </div>
  </div>

  <!-- Workstation Chromatic Plasma Orb + 48-Bar Radial Voice Equalizer -->
  <div id="orb-anchor">
    <canvas id="plasma-canvas" width="300" height="300"></canvas>
    <div id="orb-hit"
      title="Click for menu · Drag to move · Alt+Space Talk · Ctrl+Shift Dictate"></div>
  </div>
  <div id="micro-tag">REN</div>
</div>

<script>
const urlParams = new URLSearchParams(window.location.search);
const authToken = urlParams.get('token') || '';
if (authToken) {
  const originalFetch = window.fetch;
  window.fetch = function(url, options = {}) {
    const opts = { ...options };
    opts.headers = new Headers(opts.headers || {});
    if (!opts.headers.has('Authorization')) {
      opts.headers.set('Authorization', `Bearer ${authToken}`);
    }
    return originalFetch(url, opts);
  };
}

// Reconcile WebKit DPI/text-scaling zoom with the native X11 input-shape rects,
// which are defined in device pixels. Scale #stage so stage px == device px.
(function fitStageToWindow() {
  const stage = document.getElementById('stage');
  const apply = () => {
    stage.style.position = 'absolute';
    stage.style.left = '0px';
    stage.style.top = '0px';
    stage.style.margin = '0';
    stage.style.transformOrigin = '0 0';
    const zx = window.innerWidth / 340;
    const zy = window.innerHeight / 390;
    stage.style.zoom = String(Math.min(zx, zy));
  };
  apply();
  window.addEventListener('resize', apply);
})();

const canvas = document.getElementById('plasma-canvas');
const ctx = canvas.getContext('2d');
const orbImg = new Image();
orbImg.src = '/api/orb-texture';

let appState = 'IDLE';
let appMode = 'elevate';
let ttsProvider = 'qwen';
let ttsMuted = false;
let isSpeaking = false;
let targetLevel = 0.0;
let smoothLevel = 0.0;

let menuOpen = false;
let askOpen = false;
let pairOpen = false;
let pillMode = null; // null | 'draft' | 'reply'
let currentDraftId = null;
let dismissedDraftId = null;
let lastSeenReply = '';
let firstPoll = true;
let replyHideTimer = null;
let quitArmed = false;
let quitTimer = null;
let lastRegion = '';

function syncHitRegion() {
  let next = 'orb';
  if (pillMode !== null || askOpen || pairOpen) next = 'expanded';
  else if (menuOpen) next = 'radial';
  if (next === lastRegion) return;
  lastRegion = next;
  fetch('/api/ui/hitregion', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: next })
  }).catch(() => {});
}

function setMenuOpen(v) {
  menuOpen = v;
  document.getElementById('radial-menu').classList.toggle('open', menuOpen);
  syncHitRegion();
}

// Drag-to-move vs click-to-bloom on the Orb
(function wireOrbPointer() {
  const hit = document.getElementById('orb-hit');
  let down = false;
  let dragged = false;
  let lastX = 0, lastY = 0;

  hit.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    down = true;
    dragged = false;
    lastX = e.screenX;
    lastY = e.screenY;
  });

  window.addEventListener('mousemove', (e) => {
    if (!down) return;
    const dx = e.screenX - lastX;
    const dy = e.screenY - lastY;
    if (Math.abs(dx) + Math.abs(dy) >= 4) {
      dragged = true;
      lastX = e.screenX;
      lastY = e.screenY;
      fetch('/api/ui/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dx, dy })
      }).catch(() => {});
    }
  });

  window.addEventListener('mouseup', async () => {
    if (!down) return;
    down = false;
    if (dragged) return;
    if (appState === 'LISTENING') {
      await fetch('/api/listen/stop', { method: 'POST' });
      pollStatus();
      return;
    }
    if (isSpeaking) {
      await fetch('/api/cancel', { method: 'POST' });
      pollStatus();
      return;
    }
    setMenuOpen(!menuOpen);
  });
})();

async function triggerListen(intent) {
  setMenuOpen(false);
  if (appState === 'LISTENING') {
    await fetch('/api/listen/stop', { method: 'POST' });
  } else {
    await fetch('/api/listen/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intent })
    });
  }
  pollStatus();
}

const MODES = ['elevate', 'agent', 'brainstorm'];
const MODE_SHORT = { elevate: 'ELV', agent: 'AGT', brainstorm: 'BRN', talk: 'TLK' };
async function cycleMode() {
  const idx = (MODES.indexOf(appMode) + 1) % MODES.length;
  const next = MODES[idx];
  const r = await fetch('/api/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: next })
  });
  if (r.ok) {
    appMode = next;
    document.getElementById('lbl-mode').innerText = MODE_SHORT[next] || 'ELV';
  }
}

const VOICES = [
  { provider: 'qwen', muted: false, label: 'QWN' },
  { provider: 'deepgram', muted: false, label: 'DGM' },
  { provider: 'voicestudio', muted: false, label: 'VST' },
  { provider: 'qwen', muted: true, label: 'OFF' },
];
let voiceIdx = 0;
async function cycleVoice() {
  voiceIdx = (voiceIdx + 1) % VOICES.length;
  const v = VOICES[voiceIdx];
  const r = await fetch('/api/voice/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider: v.provider, muted: v.muted })
  });
  if (r.ok) {
    ttsProvider = v.provider;
    ttsMuted = v.muted;
    document.getElementById('lbl-voice').innerText = v.label;
    document.getElementById('node-voice').classList.toggle('active', !v.muted);
  }
}

async function fetchConnect() {
  try {
    const res = await fetch('/api/connect');
    const data = await res.json();
    if (data.lan_ip) {
      document.getElementById('lan-ip').innerText = `${data.lan_ip}:${data.port}`;
    }
    if (data.tailscale_ip) {
      document.getElementById('tailscale-ip').innerText = `${data.tailscale_ip}:${data.port}`;
    }
  } catch (_) {}
}

function togglePair() {
  pairOpen = !pairOpen;
  document.getElementById('pair-drawer').classList.toggle('visible', pairOpen);
  if (pairOpen) { setMenuOpen(false); fetchConnect(); }
  syncHitRegion();
}

function toggleAsk() {
  if (askOpen) { closeAsk(); return; }
  setMenuOpen(false);
  askOpen = true;
  document.getElementById('ask-bar').classList.add('visible');
  syncHitRegion();
  fetch('/api/ui/focus', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accept: true })
  }).then(() => {
    setTimeout(() => document.getElementById('ask-input').focus(), 60);
  }).catch(() => {});
}

function closeAsk() {
  askOpen = false;
  document.getElementById('ask-bar').classList.remove('visible');
  fetch('/api/ui/focus', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accept: false })
  }).catch(() => {});
  syncHitRegion();
}

async function submitAsk() {
  const inp = document.getElementById('ask-input');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  closeAsk();
  const r = await fetch('/api/converse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, speak: true })
  });
  if (r.ok) {
    const d = await r.json();
    lastSeenReply = d.text || '';
    showReplyPill(lastSeenReply);
  }
}

document.getElementById('ask-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); submitAsk(); }
  if (e.key === 'Escape') { e.preventDefault(); closeAsk(); }
});

function showDraftPill(draft, target) {
  clearTimeout(replyHideTimer);
  pillMode = 'draft';
  document.getElementById('pill-kind').innerText =
    (appMode || 'elevate').toUpperCase() + ' · R' + draft.revision;
  document.getElementById('pill-target').innerText = target ? ('→ ' + target.wm_class) : '';
  document.getElementById('pill-text').innerText = draft.text;
  document.getElementById('pill-actions-draft').style.display = 'flex';
  document.getElementById('pill-actions-reply').style.display = 'none';
  document.getElementById('draft-box').classList.add('visible');
  syncHitRegion();
}

function showReplyPill(replyText) {
  if (!replyText) return;
  clearTimeout(replyHideTimer);
  pillMode = 'reply';
  document.getElementById('pill-kind').innerText = 'REN · ' + (ttsProvider || 'VOICE').toUpperCase();
  document.getElementById('pill-target').innerText = '';
  document.getElementById('pill-text').innerText = replyText;
  document.getElementById('pill-actions-draft').style.display = 'none';
  document.getElementById('pill-actions-reply').style.display = 'flex';
  document.getElementById('draft-box').classList.add('visible');
  syncHitRegion();
  replyHideTimer = setTimeout(() => {
    if (pillMode === 'reply' && !isSpeaking) hidePill();
  }, 16000);
}

function hidePill() {
  pillMode = null;
  document.getElementById('draft-box').classList.remove('visible');
  syncHitRegion();
}

async function dismissPill() {
  if (pillMode === 'draft' && currentDraftId) {
    dismissedDraftId = currentDraftId;
    await fetch('/api/dismiss', { method: 'POST' }).catch(() => {});
  }
  hidePill();
}

async function pasteDraft() {
  const btn = document.getElementById('btn-paste');
  btn.innerText = '…';
  const r = await fetch('/api/paste', { method: 'POST' });
  if (r.ok) {
    btn.innerText = 'Pasted ✓';
    dismissedDraftId = currentDraftId;
    setTimeout(() => { btn.innerText = 'Paste'; hidePill(); }, 550);
  } else {
    const err = await r.json().catch(() => ({}));
    btn.innerText = 'Retry';
    document.getElementById('pill-target').innerText = err.detail ? err.detail.slice(0, 34) : 'focus app';
    setTimeout(() => { btn.innerText = 'Paste'; }, 1600);
  }
}

async function copyDraft() {
  const btn = document.getElementById('btn-copy');
  const r = await fetch('/api/copy', { method: 'POST' });
  if (r.ok) {
    btn.innerText = 'Copied ✓';
    setTimeout(() => { btn.innerText = 'Copy'; }, 1100);
  }
}

async function copyReply() {
  const txt = document.getElementById('pill-text').innerText;
  try { await navigator.clipboard.writeText(txt); } catch (_) {}
  const btn = document.getElementById('btn-copy-reply');
  btn.innerText = 'Copied ✓';
  setTimeout(() => { btn.innerText = 'Copy'; }, 1100);
}

async function ReplaySpeech() {
  const txt = document.getElementById('pill-text').innerText;
  if (!txt) return;
  await fetch('/api/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: txt })
  });
}

async function revertDraft() {
  await fetch('/api/revert', { method: 'POST' });
  pollStatus();
}

async function askFromDraft() {
  const txt = document.getElementById('pill-text').innerText.trim();
  if (!txt) return;
  dismissedDraftId = currentDraftId;
  const r = await fetch('/api/converse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text: `Discuss this draft I just dictated:\n>>>\n${txt}\n<<<`,
      src: 'draft', speak: true
    })
  });
  if (r.ok) {
    const d = await r.json();
    lastSeenReply = d.text || '';
    showReplyPill(lastSeenReply);
  }
}

async function shutdownRen() {
  const node = document.getElementById('node-quit');
  const lbl = document.getElementById('lbl-quit');
  if (!quitArmed) {
    quitArmed = true;
    node.classList.add('danger');
    lbl.innerText = 'SURE?';
    quitTimer = setTimeout(() => {
      quitArmed = false;
      node.classList.remove('danger');
      lbl.innerText = 'QUIT';
    }, 3000);
    return;
  }
  clearTimeout(quitTimer);
  await fetch('/api/shutdown', { method: 'POST' });
  document.getElementById('micro-tag').innerText = 'OFF';
}

// Workstation Chromatic Plasma Orb + 48-bar Radial Voice Equalizer
function renderOrb(nowMs) {
  const W = canvas.width, H = canvas.height;
  const cx = W * 0.5, cy = H * 0.5;
  const t = nowMs * 0.001;

  smoothLevel += (targetLevel - smoothLevel) * 0.28;
  const active = appState === 'LISTENING' || appState === 'PROCESSING' || isSpeaking || smoothLevel > 0.04;
  const lvl = Math.min(1.0, smoothLevel + (appState === 'PROCESSING' ? 0.25 : 0.0));

  ctx.clearRect(0, 0, W, H);

  const baseHue = isSpeaking ? (290 + Math.sin(t * 2.2) * 35)
    : (appState === 'LISTENING' ? ((t * 58) % 360) : (198 + Math.sin(t * 1.1) * 24));
  const coreR = 54 * (1.0 + Math.sin(t * 2.8) * 0.03 + lvl * 0.16);

  // 1. Soft outer plasma corona (fades strictly to alpha 0 inside canvas)
  const bloom = ctx.createRadialGradient(cx, cy, coreR * 0.35, cx, cy, 138);
  bloom.addColorStop(0, `hsla(${baseHue}, 100%, 66%, ${active ? 0.42 + lvl * 0.32 : 0.22})`);
  bloom.addColorStop(0.5, `hsla(${(baseHue + 95) % 360}, 100%, 58%, ${active ? 0.20 + lvl * 0.22 : 0.08})`);
  bloom.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = bloom;
  ctx.beginPath();
  ctx.arc(cx, cy, 140, 0, Math.PI * 2);
  ctx.fill();

  // 2. Concentric shockwave rings when listening or speaking
  const ringCount = active ? 3 : 1;
  for (let rIdx = 0; rIdx < ringCount; rIdx++) {
    const ringR = coreR + 8 + rIdx * 16
      + (active ? ((t * 36 + rIdx * 14) % 22) + lvl * 12 : Math.sin(t * 2) * 2);
    ctx.beginPath();
    ctx.arc(cx, cy, Math.min(138, ringR), 0, Math.PI * 2);
    const ringAlpha = active ? (0.45 - rIdx * 0.11 + lvl * 0.2) : 0.22;
    ctx.strokeStyle = `hsla(${(baseHue + rIdx * 80) % 360}, 100%, 70%, ${ringAlpha})`;
    ctx.lineWidth = active ? (1.6 + lvl * 1.6) : 1.1;
    ctx.stroke();
  }

  // 3. Spherical clipped plasma core (Workstation ag-plasma-orb + procedural ribbons)
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
  ctx.clip();

  const coreBg = ctx.createRadialGradient(cx - coreR * 0.25, cy - coreR * 0.25, 4, cx, cy, coreR);
  coreBg.addColorStop(0, `hsla(${baseHue}, 95%, 62%, 0.95)`);
  coreBg.addColorStop(0.55, `hsla(${(baseHue + 65) % 360}, 90%, 28%, 0.95)`);
  coreBg.addColorStop(1, 'rgba(4, 8, 22, 0.96)');
  ctx.fillStyle = coreBg;
  ctx.fillRect(cx - coreR, cy - coreR, coreR * 2, coreR * 2);

  if (orbImg.complete && orbImg.naturalWidth > 0) {
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * (active ? 0.35 : 0.12));
    ctx.globalCompositeOperation = 'screen';
    const hue = Math.round((baseHue - 195 + 360) % 360);
    const sat = 135 + Math.round(lvl * 85);
    const bri = 105 + Math.round(lvl * 35);
    ctx.filter = `hue-rotate(${hue}deg) saturate(${sat}%) brightness(${bri}%)`;
    const drawS = coreR * 2.55;
    ctx.drawImage(orbImg, -drawS * 0.5, -drawS * 0.5, drawS, drawS);
    ctx.restore();
  }

  // Procedural plasma energy filaments inside the sphere
  ctx.globalCompositeOperation = 'lighter';
  for (let f = 0; f < 4; f++) {
    ctx.beginPath();
    const fHue = (baseHue + f * 58) % 360;
    for (let x = -coreR; x <= coreR; x += 3) {
      const y = Math.sin(x * 0.065 + t * (2.6 + f * 0.7) + f) * (coreR * (0.28 + lvl * 0.32));
      const rot = t * 0.4 + f * 0.85;
      const rx = cx + x * Math.cos(rot) - y * Math.sin(rot);
      const ry = cy + x * Math.sin(rot) + y * Math.cos(rot);
      if (x === -coreR) ctx.moveTo(rx, ry);
      else ctx.lineTo(rx, ry);
    }
    ctx.strokeStyle = `hsla(${fHue}, 100%, 72%, ${0.42 + lvl * 0.38})`;
    ctx.lineWidth = 2.6 + lvl * 2.2;
    ctx.stroke();
  }

  // Specular 3D glass rim & crown highlight
  ctx.globalCompositeOperation = 'source-over';
  const gloss = ctx.createRadialGradient(cx - coreR * 0.32, cy - coreR * 0.36, 2, cx, cy, coreR);
  gloss.addColorStop(0, 'rgba(255, 255, 255, 0.55)');
  gloss.addColorStop(0.28, 'rgba(255, 255, 255, 0.08)');
  gloss.addColorStop(0.85, 'rgba(0, 0, 0, 0)');
  gloss.addColorStop(1, `hsla(${baseHue}, 100%, 76%, 0.55)`);
  ctx.fillStyle = gloss;
  ctx.fillRect(cx - coreR, cy - coreR, coreR * 2, coreR * 2);
  ctx.restore();

  // 4. 48-bar radial voice equalizer around the Plasma Orb
  const bars = 48;
  const rBase = coreR + 6;
  for (let i = 0; i < bars; i++) {
    const ang = (i / bars) * Math.PI * 2 + t * (active ? 0.55 : 0.18);
    const wave = 0.25 + 0.75 * Math.abs(Math.sin(i * 0.55 + t * (active ? 7.2 : 2.0)));
    const amp = active ? wave * (8 + lvl * 34) : wave * 3.0;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(ang) * rBase, cy + Math.sin(ang) * rBase);
    ctx.lineTo(cx + Math.cos(ang) * (rBase + amp), cy + Math.sin(ang) * (rBase + amp));
    ctx.strokeStyle = `hsla(${(baseHue + i * 5) % 360}, 100%, 72%, ${active ? 0.88 : 0.36})`;
    ctx.lineWidth = active ? 2.1 : 1.3;
    ctx.stroke();
  }

  // 5. Spinning orbital arcs while synthesizing/processing
  if (appState === 'PROCESSING') {
    for (let k = 0; k < 2; k++) {
      ctx.beginPath();
      const a0 = t * (4.2 - k * 7.5) + k * Math.PI;
      ctx.arc(cx, cy, coreR + 16 + k * 7, a0, a0 + 1.35);
      ctx.strokeStyle = k === 0 ? '#00f5ff' : '#ffd166';
      ctx.lineWidth = 2.6;
      ctx.stroke();
    }
  }

  requestAnimationFrame(renderOrb);
}
requestAnimationFrame(renderOrb);

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    const d = await r.json();

    appState = d.state || 'IDLE';
    appMode = d.mode || 'elevate';
    isSpeaking = Boolean(d.is_speaking);
    targetLevel = Number(d.voice_level || 0);
    ttsProvider = d.tts_provider || 'qwen';
    ttsMuted = Boolean(d.tts_muted);

    document.getElementById('lbl-mode').innerText = MODE_SHORT[appMode] || 'ELV';
    document.getElementById('lbl-voice').innerText = ttsMuted ? 'OFF'
      : (ttsProvider === 'deepgram' ? 'DGM' : (ttsProvider === 'voicestudio' ? 'VST' : 'QWN'));

    const tag = document.getElementById('micro-tag');
    tag.className = '';
    if (appState === 'LISTENING') {
      tag.innerText = 'LISTENING';
      tag.classList.add('listening');
    } else if (appState === 'PROCESSING') {
      tag.innerText = 'THINKING';
      tag.classList.add('listening');
    } else if (isSpeaking) {
      tag.innerText = 'SPEAKING';
      tag.classList.add('speaking');
    } else if (d.current_draft && d.current_draft.id !== dismissedDraftId) {
      tag.innerText = 'READY';
    } else {
      tag.innerText = 'REN';
    }

    if (d.current_draft && d.current_draft.id !== dismissedDraftId) {
      currentDraftId = d.current_draft.id;
      showDraftPill(d.current_draft, d.active_target);
    } else if (pillMode === 'draft' && !d.current_draft) {
      hidePill();
    }

    if (d.last_reply && d.last_reply !== lastSeenReply) {
      const wasFirst = firstPoll;
      lastSeenReply = d.last_reply;
      if (!wasFirst) showReplyPill(d.last_reply);
    }
    firstPoll = false;
  } catch (_) {
  } finally {
    const fast = appState === 'LISTENING' || appState === 'PROCESSING' || isSpeaking;
    setTimeout(pollStatus, fast ? 130 : 600);
  }
}

syncHitRegion();
pollStatus();
</script>
</body>
</html>
"""


def create_app(
    controller: WrenController | None = None,
    auth_token: str | None = None,
    window_hooks: Any = None,
) -> FastAPI:
    if controller is None:
        controller = WrenController()

    active_auth_token, _replaced = resolve_auth_token(auth_token, persist=False)

    mcp_transport = None
    try:
        from wren.mcp.http import create_mcp_transport

        mcp_transport = create_mcp_transport()
    except Exception as exc:
        logging.getLogger(__name__).warning("Ren MCP HTTP transport unavailable: %s", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_transport is not None:
            async with mcp_transport.manager.run():
                yield
        else:
            yield

    app = FastAPI(title="Wren Assistant", lifespan=lifespan)

    @app.middleware("http")
    async def enforce_nonloopback_auth(request: Request, call_next):
        client_host = request.client.host if request.client else None
        request_host = request.url.hostname

        if not is_loopback_client(client_host, request_host) or has_proxy_headers(request.headers):
            token = extract_token_from_request(request)
            if not token or not secrets.compare_digest(token, active_auth_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: valid bearer token required for non-loopback access"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FLOATING_PLASMA_ORB_HTML

    @app.get("/api/orb-texture")
    def get_orb_texture():
        if WORKSTATION_ORB_JPG.is_file():
            return Response(
                content=WORKSTATION_ORB_JPG.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return Response(status_code=204)

    @app.get("/api/connect")
    def get_connect_info():
        ips = get_network_ips()
        return {
            "status": "ready",
            "state": controller.state.value,
            "mode": controller.mode,
            "lan_ip": ips["lan_ip"],
            "tailscale_ip": ips["tailscale_ip"],
            "port": 8765,
            "auth_required": True,
            "endpoints": {
                "ws_connect": "/ws/connect",
                "status": "/api/status",
                "mode": "/api/mode",
                "listen_start": "/api/listen/start",
                "listen_stop": "/api/listen/stop",
                "screen_active": "/api/screen/active",
                "files_search": "/api/files/search",
                "dictate": "/api/dictate",
                "converse": "/api/converse",
                "paste_preview": "/api/paste/preview",
                "screen_phone": "/api/screen/phone",
                "browser": "/api/browser/act",
                "workspace": "/api/workspace/list",
                "mcp": "stdio ren-mcp",
            },
        }

    @app.post("/api/mode")
    def set_mode(req: ModeRequest):
        try:
            new_mode = controller.set_mode(req.mode)
            return {"status": "ok", "mode": new_mode}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/voice/config")
    def get_voice_config():
        return {
            "provider": controller.tts.provider,
            "muted": controller.tts.muted,
            "last_provider_used": controller.tts.last_provider_used,
        }

    @app.post("/api/voice/config")
    def set_voice_config(req: VoiceConfigRequest):
        try:
            if req.provider is not None:
                controller.tts.set_provider(req.provider)
            if req.muted is not None:
                controller.tts.muted = bool(req.muted)
                if controller.tts.muted:
                    controller.tts.stop()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "status": "ok",
            "provider": controller.tts.provider,
            "muted": controller.tts.muted,
        }

    @app.post("/api/ui/hitregion")
    def set_ui_hitregion(req: HitRegionRequest):
        if window_hooks is not None and hasattr(window_hooks, "set_hit_region"):
            window_hooks.set_hit_region(req.mode)
        return {"status": "ok", "mode": req.mode}

    @app.post("/api/ui/move")
    def move_ui_window(req: WindowMoveRequest):
        if window_hooks is not None and hasattr(window_hooks, "move_by"):
            window_hooks.move_by(req.dx, req.dy)
        return {"status": "ok"}

    @app.post("/api/ui/focus")
    def focus_ui_window(req: WindowFocusRequest):
        if window_hooks is not None and hasattr(window_hooks, "set_focusable"):
            window_hooks.set_focusable(req.accept)
        return {"status": "ok", "accept": req.accept}

    @app.get("/api/status")
    def get_status():
        target_dict = None
        if controller.active_target:
            target_dict = {
                "window_id": controller.active_target.window_id,
                "wm_class": controller.active_target.wm_class,
                "title": controller.active_target.title,
                "is_terminal": controller.active_target.is_terminal,
            }

        draft_dict = None
        if controller.current_draft:
            draft_dict = {
                "id": controller.current_draft.id,
                "revision": controller.current_draft.revision,
                "text": controller.current_draft.text,
                "op": controller.current_draft.op,
            }

        return {
            "state": controller.state.value,
            "mode": controller.mode,
            "is_recording": controller.audio.is_recording,
            "is_speaking": controller.is_speaking,
            "voice_level": round(controller.voice_level, 3),
            "tts_provider": controller.tts.provider,
            "tts_muted": controller.tts.muted,
            "current_draft": draft_dict,
            "last_diff": controller.last_diff,
            "last_transcript": controller.last_transcript,
            "active_target": target_dict,
            "last_reply": controller.last_reply,
            "phone_screen": {
                "package": controller.phone_screen.get("package", ""),
                "chars": len(controller.phone_screen.get("text") or ""),
                "age_s": round(time.time() - float(controller.phone_screen.get("at", 0)), 1)
                    if controller.phone_screen.get("at") else None,
            },
            "conversation_turns": len(controller.conversation),
            "mcp_http_mounted": mcp_transport is not None,
        }

    @app.post("/api/listen/start")
    def start_listen(req: ListenStartRequest | None = None):
        intent = req.intent if req is not None else "auto"
        try:
            controller.start_listening(intent=intent)
            return {"status": "listening", "intent": intent}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/listen/stop")
    def stop_listen():
        try:
            draft = controller.stop_listening()
            if draft is not None:
                return {
                    "status": "draft_ready",
                    "revision": draft.revision,
                    "text": draft.text,
                }
            return {
                "status": "idle",
                "reply": controller.last_reply,
                "text": controller.last_reply,
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/revise")
    def revise_draft(req: ReviseRequest):
        try:
            draft = controller.revise(req.instruction)
            return {
                "status": "revised",
                "revision": draft.revision,
                "text": draft.text,
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/revert")
    def revert_draft():
        try:
            draft = controller.revert_to_raw()
            return {
                "status": "reverted",
                "revision": draft.revision if draft else 0,
                "text": draft.text if draft else "",
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/copy")
    def copy_draft():
        try:
            whash = controller.copy_current_draft()
            return {"status": "copied", "hash": whash}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/paste")
    def paste_draft():
        try:
            receipt = controller.paste_current_draft()
            return {"status": "pasted", "outcome": receipt.outcome, "evidence": receipt.evidence}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/dismiss")
    def dismiss_draft():
        controller.buffer.clear()
        controller.cancel()
        return {"status": "dismissed"}

    @app.post("/api/cancel")
    def cancel():
        controller.cancel()
        return {"status": "cancelled"}

    @app.post("/api/speak")
    def speak(req: SpeakRequest):
        """Speak text aloud via Qwen primary -> Deepgram Aura fallback."""
        text = req.text.strip()[:2000]
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        controller.speak_async(text, provider=req.provider)
        return {"status": "speaking", "provider": req.provider or controller.tts.provider}

    @app.post("/api/shutdown")
    def shutdown():
        """Stop the Ren service cleanly."""
        def _stop():
            time.sleep(0.6)
            try:
                subprocess.run(
                    ["systemctl", "--user", "stop", "wren.service"],
                    timeout=10,
                    capture_output=True,
                )
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_stop, daemon=True).start()
        return {"status": "stopping"}

    @app.get("/api/files/roots")
    def get_file_roots():
        return {"roots": controller.file_ingest.get_available_roots()}

    @app.get("/api/files/search")
    def search_files(q: str = ""):
        results = controller.file_ingest.search(q)
        return {"results": results}

    @app.post("/api/files/read")
    def read_file(req: ReadFileRequest):
        try:
            content = controller.file_ingest.read_file(req.file_path)
            return {"path": req.file_path, "content": content}
        except FileIngestError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.post("/api/files/attach")
    def attach_file(req: AttachFileRequest):
        try:
            attached = controller.attach_file(req.file_path)
            return {"ok": True, "attached_name": attached["name"], "path": attached["path"]}
        except FileIngestError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    @app.get("/api/screen/active")
    def get_screen_active():
        ctx = controller.get_screen_context(capture_screenshot=False)
        return {
            "app_name": ctx.app_name,
            "window_title": ctx.window_title,
            "window_id": ctx.window_id,
            "is_browser": ctx.is_browser,
            "is_editor": ctx.is_editor,
            "is_terminal": ctx.is_terminal,
            "timestamp": ctx.timestamp,
        }

    @app.get("/api/screen/capture")
    def get_screen_capture():
        ctx = controller.get_screen_context(capture_screenshot=True)
        return {
            "app_name": ctx.app_name,
            "window_title": ctx.window_title,
            "screenshot_b64": ctx.screenshot_b64,
            "timestamp": ctx.timestamp,
        }

    @app.get("/api/screen/phone")
    def get_phone_screen():
        return controller.phone_screen

    @app.post("/api/screen/phone")
    def post_phone_screen(req: PhoneScreenRequest):
        return controller.note_phone_screen(req.package, req.text)

    @app.post("/api/dictate")
    def dictate(req: DictateRequest):
        try:
            draft = controller.accept_dictation(req.text, source=req.source)
            return {
                "type": "paste_preview",
                "requires_confirm": True,
                "raw": req.text,
                "status": "draft_ready",
                "revision": draft.revision,
                "text": draft.text,
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/converse")
    def converse(req: ConverseRequest):
        try:
            reply = controller.converse(req.text, source=req.src)
            if req.speak:
                controller.speak_async(reply)
            return {"type": "assistant_message", "text": reply, "requires_confirm": True}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/conversation/clear")
    def clear_conversation():
        controller.conversation.clear()
        controller.last_reply = ""
        return {"type": "conversation_cleared"}

    @app.get("/api/paste/preview")
    def paste_preview():
        return controller.paste_preview()

    @app.post("/api/browser/act")
    def browser_act(req: BrowserActRequest):
        try:
            return controller.browser.act(
                req.op,
                confirm=req.confirm,
                approval_id=req.approval_id,
                url=req.url,
                selector=req.selector,
                text=req.text,
                x=req.x,
                y=req.y,
                delta_y=req.delta_y,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/workspace/list")
    def workspace_list(path: str = "."):
        try:
            return controller.workspace.list_dir(path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/workspace/read")
    def workspace_read(req: WorkspaceReadRequest):
        try:
            return controller.workspace.read(req.path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/workspace/write")
    def workspace_write(req: WorkspaceWriteRequest):
        try:
            if req.approval_id:
                return controller.workspace.commit_write(req.approval_id)
            return controller.workspace.write(req.path, req.content, confirm=req.confirm)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.websocket("/ws/connect")
    async def websocket_connect(websocket: WebSocket):
        """Bidirectional WebSocket for mobile companion streaming and synchronization."""
        client_host = websocket.client.host if websocket.client else None
        request_host = websocket.url.hostname

        if not is_loopback_client(client_host, request_host) or has_proxy_headers(websocket.headers):
            token = websocket.query_params.get("token") or websocket.headers.get("x-wren-token")
            auth_hdr = websocket.headers.get("authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()

            if not token or not secrets.compare_digest(token, active_auth_token):
                await websocket.close(
                    code=1008,
                    reason="Unauthorized: valid token required for non-loopback access",
                )
                return

        await websocket.accept()
        await websocket.send_json({
            "type": "handshake",
            "state": controller.state.value,
            "mode": controller.mode,
            "active_draft": {
                "text": controller.current_draft.text,
                "revision": controller.current_draft.revision,
            } if controller.current_draft else None,
        })

        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                action = msg.get("action")

                if action == "ping":
                    await websocket.send_json({"type": "pong"})

                elif action == "set_mode":
                    mode = msg.get("mode", "elevate")
                    controller.set_mode(mode)
                    await websocket.send_json({"type": "mode_updated", "mode": controller.mode})

                elif action == "start_listen":
                    controller.start_listening(intent=msg.get("intent", "auto"))
                    await websocket.send_json({"type": "state_changed", "state": controller.state.value})

                elif action == "stop_listen":
                    draft = controller.stop_listening()
                    if draft is not None:
                        await websocket.send_json({
                            "type": "draft_ready",
                            "draft": {
                                "id": draft.id,
                                "text": draft.text,
                                "revision": draft.revision,
                            },
                        })
                        await websocket.send_json({
                            "type": "paste_preview",
                            "requires_confirm": True,
                            "raw": draft.text,
                            "draft": {"id": draft.id, "text": draft.text, "revision": draft.revision},
                        })
                    else:
                        await websocket.send_json({
                            "type": "assistant_message",
                            "text": controller.last_reply,
                            "requires_confirm": True,
                        })

                elif action == "elevate_text":
                    raw = msg.get("text", "")
                    mode = msg.get("mode", controller.mode)
                    elev_res = controller.elevator.elevate(raw, mode=mode)
                    await websocket.send_json({
                        "type": "elevated",
                        "raw_text": raw,
                        "elevated_text": elev_res.elevated_text,
                        "diff": elev_res.diff,
                        "mode": mode,
                    })

                elif action == "copy":
                    controller.copy_current_draft()
                    await websocket.send_json({"type": "copied", "state": controller.state.value})

                elif action == "paste":
                    receipt = controller.paste_current_draft()
                    await websocket.send_json({
                        "type": "pasted",
                        "outcome": receipt.outcome,
                        "evidence": receipt.evidence,
                    })

                elif action == "dictate":
                    if msg.get("screen"):
                        controller.note_phone_screen(msg.get("package", ""), msg.get("screen", ""))
                    draft = controller.accept_dictation(
                        msg.get("text", ""), source=msg.get("source", "android")
                    )
                    await websocket.send_json({
                        "type": "paste_preview",
                        "requires_confirm": True,
                        "raw": msg.get("text", ""),
                        "draft": {"id": draft.id, "text": draft.text, "revision": draft.revision},
                    })

                elif action == "screen_context":
                    stored = controller.note_phone_screen(msg.get("package", ""), msg.get("text", ""))
                    await websocket.send_json(stored)

                elif action == "converse":
                    if msg.get("screen"):
                        controller.note_phone_screen(msg.get("package", ""), msg.get("screen", ""))
                    reply = controller.converse(msg.get("text", ""))
                    if msg.get("speak"):
                        controller.speak_async(reply)
                    await websocket.send_json({
                        "type": "assistant_message",
                        "text": reply,
                        "requires_confirm": True,
                    })

                elif action == "request_screen":
                    await websocket.send_json({"type": "screen_request"})

                else:
                    await websocket.send_json({"type": "error", "message": f"Unknown action {action}"})

        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass

    if mcp_transport is not None:
        app.add_route("/mcp", mcp_transport, methods=["GET", "POST", "DELETE"])
        app.add_route("/mcp/", mcp_transport, methods=["GET", "POST", "DELETE"])

    return app
