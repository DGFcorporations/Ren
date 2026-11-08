"""Wren Floating Orb & Draft Preview Web Server."""

import json
import os
import secrets
import socket
import subprocess

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from wren.core.controller import WrenController
from wren.files.ingest import FileIngestError

DEFAULT_WREN_TOKEN = "wren-pilot-auth-2026-dgf"


def is_loopback_client(client_host: str | None, request_host: str | None) -> bool:
    """Determine whether the request originates from a local loopback interface.

    Both the client peer IP and target hostname must resolve to loopback.
    """
    loopback_peers = {"127.0.0.1", "::1", "localhost", "testclient"}
    loopback_targets = {"127.0.0.1", "::1", "localhost", "testserver"}

    if not client_host or client_host not in loopback_peers:
        return False
    if request_host and request_host not in loopback_targets:
        return False
    return True


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


def get_network_ips() -> dict[str, str]:
    """Detect local LAN IP and Tailscale IP for mobile companion connection."""
    lan_ip = "192.168.4.28"
    tailscale_ip = "100.90.174.74"

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


def create_app(
    controller: WrenController | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    if controller is None:
        controller = WrenController()

    active_auth_token = auth_token or os.environ.get("WREN_AUTH_TOKEN", DEFAULT_WREN_TOKEN)

    app = FastAPI(title="Wren Assistant")

    @app.middleware("http")
    async def enforce_nonloopback_auth(request: Request, call_next):
        client_host = request.client.host if request.client else None
        request_host = request.url.hostname

        if not is_loopback_client(client_host, request_host):
            token = extract_token_from_request(request)
            if not token or not secrets.compare_digest(token, active_auth_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: valid bearer token required for non-loopback access"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        return await call_next(request)

    HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Wren Assistant</title>
<style>
  :root {
    --bg-dark: #090d16;
    --card-bg: rgba(18, 24, 38, 0.92);
    --surface-hover: #1e293b;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --primary: #38bdf8;
    --accent: #818cf8;
    --success: #10b981;
    --danger: #ef4444;
    --warning: #f59e0b;
    --border: rgba(255, 255, 255, 0.08);
    --border-active: rgba(56, 189, 248, 0.4);
    --glow-primary: 0 0 24px rgba(56, 189, 248, 0.35);
    --glow-listening: 0 0 32px rgba(239, 68, 68, 0.6);
    --glow-success: 0 0 24px rgba(16, 185, 129, 0.4);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg-dark);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: 12px;
    -webkit-font-smoothing: antialiased;
  }
  .app-widget {
    width: 100%;
    max-width: 380px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 16px;
    backdrop-filter: blur(20px);
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.05);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .widget-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .brand-group {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .brand-logo {
    font-size: 1.15rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .target-pill {
    font-size: 0.72rem;
    font-weight: 500;
    padding: 3px 8px;
    border-radius: 9999px;
    background: rgba(56, 189, 248, 0.1);
    color: var(--primary);
    border: 1px solid rgba(56, 189, 248, 0.25);
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .mode-bar {
    display: flex;
    background: rgba(15, 23, 42, 0.8);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 3px;
    gap: 2px;
  }
  .mode-btn {
    flex: 1;
    background: transparent;
    color: var(--text-muted);
    border: none;
    border-radius: 9px;
    padding: 6px 4px;
    font-size: 0.75rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }
  .mode-btn:hover {
    color: var(--text-primary);
  }
  .mode-btn.active {
    background: rgba(56, 189, 248, 0.18);
    color: var(--primary);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }
  .orb-hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 16px 0;
    cursor: pointer;
    user-select: none;
  }
  .orb-wrapper {
    position: relative;
    width: 90px;
    height: 90px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .orb-elem {
    width: 72px;
    height: 72px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #38bdf8 0%, #1e3a8a 70%, #0f172a 100%);
    box-shadow: 0 0 20px rgba(56, 189, 248, 0.25);
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    z-index: 2;
  }
  .orb-elem.LISTENING {
    background: radial-gradient(circle at 35% 35%, #f87171 0%, #b91c1c 70%, #450a0a 100%);
    box-shadow: var(--glow-listening);
    transform: scale(1.08);
    animation: orb-breathe 1.6s ease-in-out infinite;
  }
  .orb-elem.PROCESSING {
    background: radial-gradient(circle at 35% 35%, #fbbf24 0%, #b45309 70%, #451a03 100%);
    box-shadow: 0 0 24px rgba(251, 191, 36, 0.5);
    animation: orb-spin 2s linear infinite;
  }
  .orb-elem.DRAFT_READY {
    background: radial-gradient(circle at 35% 35%, #34d399 0%, #047857 70%, #064e3b 100%);
    box-shadow: var(--glow-success);
  }
  .orb-elem.CLIPBOARD_WRITTEN {
    background: radial-gradient(circle at 35% 35%, #818cf8 0%, #4338ca 70%, #1e1b4b 100%);
    box-shadow: 0 0 24px rgba(129, 140, 248, 0.4);
  }
  @keyframes orb-breathe {
    0%, 100% { transform: scale(1.04); }
    50% { transform: scale(1.14); }
  }
  @keyframes orb-spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }
  .waveform {
    display: none;
    align-items: center;
    gap: 3px;
    height: 18px;
    margin-top: 8px;
  }
  .waveform.active {
    display: flex;
  }
  .wave-bar {
    width: 3px;
    background: var(--danger);
    border-radius: 2px;
    animation: wave-motion 1s ease-in-out infinite alternate;
  }
  .wave-bar:nth-child(1) { height: 6px; animation-delay: 0.1s; }
  .wave-bar:nth-child(2) { height: 16px; animation-delay: 0.3s; }
  .wave-bar:nth-child(3) { height: 22px; animation-delay: 0.15s; }
  .wave-bar:nth-child(4) { height: 14px; animation-delay: 0.4s; }
  .wave-bar:nth-child(5) { height: 8px; animation-delay: 0.2s; }
  @keyframes wave-motion {
    0% { height: 4px; }
    100% { height: 22px; }
  }
  .orb-caption {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: 500;
    margin-top: 6px;
  }
  .draft-card {
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .draft-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.75rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .revision-pill {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border);
    padding: 1px 6px;
    border-radius: 9999px;
    font-size: 0.7rem;
    color: var(--text-primary);
  }
  .draft-box {
    background: rgba(9, 13, 22, 0.8);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    font-size: 0.88rem;
    line-height: 1.45;
    min-height: 64px;
    max-height: 200px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-primary);
  }
  .diff-box {
    background: #090d16;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem;
    color: var(--text-muted);
    max-height: 80px;
    overflow-y: auto;
    white-space: pre-wrap;
    display: none;
  }
  .action-row {
    display: flex;
    gap: 6px;
  }
  .btn {
    flex: 1;
    background: var(--surface-hover);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
  }
  .btn:hover:not(:disabled) {
    background: #334155;
    border-color: rgba(255, 255, 255, 0.15);
  }
  .btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .btn-success {
    background: var(--success);
    color: #064e3b;
    border: none;
  }
  .btn-success:hover:not(:disabled) {
    background: #34d399;
  }
  .btn-primary {
    background: var(--primary);
    color: #0c4a6e;
    border: none;
  }
  .btn-primary:hover:not(:disabled) {
    background: #7dd3fc;
  }
  .revise-row {
    display: flex;
    gap: 6px;
    margin-top: 2px;
  }
  .revise-row input {
    flex: 1;
    background: rgba(9, 13, 22, 0.8);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 0.78rem;
    color: var(--text-primary);
  }
  .revise-row input:focus {
    outline: none;
    border-color: var(--primary);
  }
  .collapsible-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.74rem;
    color: var(--text-muted);
    cursor: pointer;
    padding: 4px 2px;
  }
  .collapsible-header:hover { color: var(--text-primary); }
  .collapsible-body {
    display: none;
    flex-direction: column;
    gap: 8px;
    padding-top: 6px;
  }
  .collapsible-body.open { display: flex; }
  .connect-card {
    background: rgba(9, 13, 22, 0.6);
    border: 1px dashed var(--border);
    border-radius: 10px;
    padding: 8px;
    font-size: 0.72rem;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .connect-item {
    display: flex;
    justify-content: space-between;
    color: var(--text-muted);
  }
  .connect-val {
    color: var(--primary);
    font-family: monospace;
    font-weight: 600;
  }
  .chips-container {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .chip {
    background: rgba(56, 189, 248, 0.1);
    border: 1px solid rgba(56, 189, 248, 0.3);
    border-radius: 9999px;
    padding: 2px 6px;
    font-size: 0.68rem;
    color: var(--primary);
    display: flex;
    align-items: center;
    gap: 4px;
  }
</style>
</head>
<body>
<div class="app-widget">
  <div class="widget-header">
    <div class="brand-group">
      <div class="brand-logo">Wren</div>
      <div class="target-pill" id="target-pill">🎯 Searching target...</div>
    </div>
    <div id="status-pill" style="font-size:0.72rem; color:var(--text-muted);">IDLE</div>
  </div>

  <div class="mode-bar">
    <button class="mode-btn active" id="btn-mode-elevate" onclick="switchMode('elevate')">✨ Elevate</button>
    <button class="mode-btn" id="btn-mode-agent" onclick="switchMode('agent')">🤖 Agent</button>
    <button class="mode-btn" id="btn-mode-brainstorm" onclick="switchMode('brainstorm')">
      💡 Brainstorm
    </button>
  </div>

  <div class="orb-hero" onclick="handleOrbClick()">
    <div class="orb-wrapper">
      <div class="orb-elem IDLE" id="orb-elem"></div>
    </div>
    <div class="waveform" id="waveform">
      <div class="wave-bar"></div>
      <div class="wave-bar"></div>
      <div class="wave-bar"></div>
      <div class="wave-bar"></div>
      <div class="wave-bar"></div>
    </div>
    <div class="orb-caption" id="orb-caption">Tap or [Space] to Speak</div>
  </div>

  <div class="draft-card">
    <div class="draft-card-header">
      <span id="draft-header-title">Draft Preview</span>
      <span class="revision-pill" id="rev-pill">v0</span>
    </div>
    <div class="draft-box" id="draft-box">Speak or brainstorm to generate a draft.</div>
    <div class="diff-box" id="diff-box"></div>
    <div class="action-row">
      <button class="btn btn-success" id="btn-paste" onclick="pasteDraft()" disabled>🎯 Paste</button>
      <button class="btn" id="btn-copy" onclick="copyDraft()" disabled>📋 Copy</button>
      <button class="btn" id="btn-revert" onclick="revertDraft()" disabled>🔄 Revert</button>
    </div>
    <div class="revise-row">
      <input type="text" id="revise-input" placeholder="Refine or ask Wren..."
        onkeydown="if(event.key==='Enter') submitRevise()">
      <button class="btn btn-primary" style="flex:0 0 54px;" onclick="submitRevise()">Send</button>
    </div>
  </div>

  <div class="collapsible">
    <div class="collapsible-header" onclick="toggleTray('files-body')">
      <span>📎 Attached Context (<span id="file-count">0</span>)</span>
      <span id="files-caret">▼</span>
    </div>
    <div class="collapsible-body" id="files-body">
      <div class="chips-container" id="chips-container"></div>
      <div style="display:flex; gap:4px;">
        <input type="text" id="file-search-input" placeholder="Search /Dev or Drive..."
          style="flex:1; font-size:0.72rem; padding:4px 8px; background:#090d16; color:#fff;"
          onkeydown="if(event.key==='Enter') doSearchFiles()">
        <button class="btn" style="flex:0 0 44px; padding:4px 6px;" onclick="doSearchFiles()">Find</button>
      </div>
      <div id="file-search-results"
        style="max-height:90px; overflow-y:auto; font-size:0.7rem; color:var(--text-muted);"></div>
    </div>
  </div>

  <div class="collapsible">
    <div class="collapsible-header" onclick="toggleTray('connect-body')">
      <span>📱 Companion Bridge</span>
      <span id="connect-caret">▼</span>
    </div>
    <div class="collapsible-body" id="connect-body">
      <div class="connect-card">
        <div class="connect-item"><span>LAN IP:</span>
          <span class="connect-val" id="lan-ip">192.168.4.28:8765</span></div>
        <div class="connect-item"><span>Tailscale IP:</span>
          <span class="connect-val" id="tailscale-ip">100.90.174.74:8765</span></div>
        <div class="connect-item"><span>WebSocket:</span><span class="connect-val">ws://.../ws/connect</span></div>
      </div>
    </div>
  </div>
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

let currentState = "IDLE";
let currentMode = "elevate";
let isListening = false;

function toggleTray(id) {
  const el = document.getElementById(id);
  el.classList.toggle('open');
}

async function switchMode(mode) {
  try {
    const res = await fetch('/api/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode })
    });
    const data = await res.json();
    updateModeUI(data.mode);
  } catch (err) {
    console.error("Failed to switch mode", err);
  }
}

function updateModeUI(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode-btn').forEach(btn => btn.classList.remove('active'));
  const activeBtn = document.getElementById(`btn-mode-${mode}`);
  if (activeBtn) activeBtn.classList.add('active');

  const headers = {
    'elevate': 'Draft Preview (✨ Elevate)',
    'agent': 'Agent Directive (🤖 5-Gate)',
    'brainstorm': 'Brainstorm Brief (💡 Explore)'
  };
  document.getElementById('draft-header-title').innerText = headers[mode] || 'Draft Preview';
}

async function handleOrbClick() {
  const readyStates = ["IDLE", "DRAFT_READY", "CLIPBOARD_WRITTEN"];
  if (currentState === "LISTENING") {
    await stopListening();
  } else if (readyStates.includes(currentState)) {
    await startListening();
  }
}

async function startListening() {
  try {
    await fetch('/api/listen/start', { method: 'POST' });
    fetchStatus();
  } catch (err) {
    console.error("Error starting listen", err);
  }
}

async function stopListening() {
  try {
    await fetch('/api/listen/stop', { method: 'POST' });
    fetchStatus();
  } catch (err) {
    console.error("Error stopping listen", err);
  }
}

async function pasteDraft() {
  try {
    await fetch('/api/paste', { method: 'POST' });
    fetchStatus();
  } catch (err) {
    console.error("Error pasting", err);
  }
}

async function copyDraft() {
  try {
    await fetch('/api/copy', { method: 'POST' });
    fetchStatus();
  } catch (err) {
    console.error("Error copying", err);
  }
}

async function revertDraft() {
  try {
    await fetch('/api/revert', { method: 'POST' });
    fetchStatus();
  } catch (err) {
    console.error("Error reverting", err);
  }
}

async function submitRevise() {
  const input = document.getElementById('revise-input');
  const val = input.value.trim();
  if (!val) return;
  try {
    await fetch('/api/revise', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instruction: val })
    });
    input.value = '';
    fetchStatus();
  } catch (err) {
    console.error("Error revising", err);
  }
}

async function doSearchFiles() {
  const q = document.getElementById('file-search-input').value.trim();
  const res = await fetch(`/api/files/search?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  const container = document.getElementById('file-search-results');
  container.innerHTML = '';
  data.results.slice(0, 5).forEach(f => {
    const item = document.createElement('div');
    item.style.cursor = 'pointer';
    item.style.padding = '3px 4px';
    item.style.borderRadius = '4px';
    item.style.background = '#1e293b';
    const tag = `<span style="opacity:0.6; font-size:0.65rem;">(${f.source})</span>`;
    item.innerHTML = `<span>${f.name}</span> ` + tag;
    item.onclick = () => attachContextFile(f.path, f.name);
    container.appendChild(item);
  });
}

async function attachContextFile(path, name) {
  await fetch('/api/files/attach', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file_path: path })
  });
  const chips = document.getElementById('chips-container');
  const chip = document.createElement('div');
  chip.className = 'chip';
  chip.innerText = name;
  chips.appendChild(chip);
  const count = chips.querySelectorAll('.chip').length;
  document.getElementById('file-count').innerText = count;
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    currentState = data.state;
    if (data.mode) {
      updateModeUI(data.mode);
    }

    const orb = document.getElementById('orb-elem');
    orb.className = 'orb-elem ' + data.state;
    document.getElementById('status-pill').innerText = data.state;

    const waveform = document.getElementById('waveform');
    const caption = document.getElementById('orb-caption');
    if (data.state === 'LISTENING') {
      waveform.classList.add('active');
      caption.innerText = 'Listening... (Tap to elevate)';
    } else if (data.state === 'PROCESSING') {
      waveform.classList.remove('active');
      caption.innerText = 'Synthesizing...';
    } else if (data.state === 'DRAFT_READY') {
      waveform.classList.remove('active');
      caption.innerText = 'Draft Ready (Tap to dictate new)';
    } else {
      waveform.classList.remove('active');
      caption.innerText = 'Tap or [Space] to Speak';
    }

    if (data.active_target) {
      document.getElementById('target-pill').innerText = '🎯 ' + data.active_target.wm_class;
    } else {
      document.getElementById('target-pill').innerText = '🎯 No Target';
    }

    const draftBox = document.getElementById('draft-box');
    const btnPaste = document.getElementById('btn-paste');
    const btnCopy = document.getElementById('btn-copy');
    const btnRevert = document.getElementById('btn-revert');

    if (data.current_draft) {
      draftBox.innerText = data.current_draft.text;
      document.getElementById('rev-pill').innerText = 'v' + data.current_draft.revision;
      btnPaste.disabled = false;
      btnCopy.disabled = false;
      btnRevert.disabled = false;
    } else {
      draftBox.innerText = 'Speak or brainstorm to generate a draft.';
      btnPaste.disabled = true;
      btnCopy.disabled = true;
      btnRevert.disabled = true;
    }

    if (data.last_diff) {
      const diffBox = document.getElementById('diff-box');
      diffBox.innerText = data.last_diff;
      diffBox.style.display = 'block';
    }
  } catch (err) {
    console.error("Status fetch error", err);
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
  } catch (err) {
    console.error("Connect fetch error", err);
  }
}

// Push to talk shortcut (Spacebar)
window.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
    e.preventDefault();
    handleOrbClick();
  }
});

setInterval(fetchStatus, 1000);
fetchStatus();
fetchConnect();
</script>
</body>
</html>
"""

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_CONTENT

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
            },
        }

    @app.post("/api/mode")
    def set_mode(req: ModeRequest):
        try:
            new_mode = controller.set_mode(req.mode)
            return {"status": "ok", "mode": new_mode}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

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
            "current_draft": draft_dict,
            "last_diff": controller.last_diff,
            "active_target": target_dict,
        }

    @app.post("/api/listen/start")
    def start_listen():
        try:
            controller.start_listening()
            return {"status": "listening"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/listen/stop")
    def stop_listen():
        try:
            draft = controller.stop_listening()
            return {
                "status": "draft_ready",
                "revision": draft.revision,
                "text": draft.text,
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

    @app.post("/api/cancel")
    def cancel():
        controller.cancel()
        return {"status": "cancelled"}

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

    @app.websocket("/ws/connect")
    async def websocket_connect(websocket: WebSocket):
        """Bidirectional WebSocket for mobile companion streaming and real-time synchronization."""
        client_host = websocket.client.host if websocket.client else None
        request_host = websocket.url.hostname

        if not is_loopback_client(client_host, request_host):
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
        # Send initial status handshake
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
                    controller.start_listening()
                    await websocket.send_json({"type": "state_changed", "state": controller.state.value})

                elif action == "stop_listen":
                    draft = controller.stop_listening()
                    await websocket.send_json({
                        "type": "draft_ready",
                        "draft": {
                            "id": draft.id,
                            "text": draft.text,
                            "revision": draft.revision,
                        },
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

        except WebSocketDisconnect:
            pass
        except Exception as e:
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass

    return app
