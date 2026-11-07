"""Wren Floating Orb & Draft Preview Web Server."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from wren.core.controller import WrenController


class ReviseRequest(BaseModel):
    instruction: str


def create_app(controller: WrenController | None = None) -> FastAPI:
    if controller is None:
        controller = WrenController()

    app = FastAPI(title="Wren Assistant")

    HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wren Assistant</title>
<style>
  :root {
    --bg-color: #0f172a;
    --card-bg: #1e293b;
    --text-color: #f8fafc;
    --text-muted: #94a3b8;
    --primary: #38bdf8;
    --accent: #818cf8;
    --success: #34d399;
    --danger: #f87171;
    --warning: #fbbf24;
    --border: #334155;
  }
  body {
    margin: 0;
    padding: 24px;
    background-color: var(--bg-color);
    color: var(--text-color);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    justify-content: center;
  }
  .container {
    width: 100%;
    max-width: 680px;
    background: var(--card-bg);
    border-radius: 16px;
    border: 1px solid var(--border);
    padding: 24px;
    box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    margin-bottom: 20px;
  }
  .orb-status {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .orb {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--text-muted);
    transition: all 0.3s ease;
  }
  .orb.IDLE { background: var(--text-muted); }
  .orb.LISTENING {
    background: var(--danger);
    box-shadow: 0 0 15px var(--danger);
    animation: pulse 1.5s infinite;
  }
  .orb.PROCESSING { background: var(--warning); }
  .orb.DRAFT_READY { background: var(--success); box-shadow: 0 0 10px var(--success); }
  .orb.CLIPBOARD_WRITTEN { background: var(--primary); }
  .orb.ERROR { background: var(--danger); }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(1.15); opacity: 0.8; }
  }
  .state-label {
    font-weight: 600;
    font-size: 1.1rem;
    letter-spacing: 0.5px;
  }
  .target-badge {
    background: #0f172a;
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 0.85rem;
    color: var(--primary);
  }
  .section {
    margin-bottom: 20px;
  }
  .section-title {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-muted);
    margin-bottom: 8px;
  }
  .draft-box {
    background: #0f172a;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    font-size: 1.05rem;
    line-height: 1.5;
    min-height: 70px;
    white-space: pre-wrap;
  }
  .diff-box {
    background: #111827;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    font-family: monospace;
    font-size: 0.9rem;
    color: var(--text-muted);
    max-height: 120px;
    overflow-y: auto;
    white-space: pre-wrap;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 20px;
  }
  button {
    background: var(--border);
    color: var(--text-color);
    border: none;
    padding: 10px 18px;
    border-radius: 8px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.2s;
  }
  button:hover { background: #475569; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.btn-primary { background: var(--primary); color: #0f172a; font-weight: 600; }
  button.btn-primary:hover { background: #7dd3fc; }
  button.btn-success { background: var(--success); color: #0f172a; font-weight: 600; }
  button.btn-success:hover { background: #6ee7b7; }
  button.btn-danger { background: var(--danger); color: #0f172a; }
  button.btn-danger:hover { background: #fca5a5; }
  .revision-input-group {
    display: flex;
    gap: 8px;
    margin-top: 14px;
  }
  input[type="text"] {
    flex: 1;
    background: #0f172a;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text-color);
    padding: 10px 14px;
    font-size: 0.95rem;
  }
  input[type="text"]:focus {
    outline: 2px solid var(--primary);
    border-color: transparent;
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="orb-status">
      <div id="orb" class="orb IDLE"></div>
      <div id="state-label" class="state-label">IDLE</div>
    </div>
    <div id="target-badge" class="target-badge">Target: None</div>
  </div>

  <div class="section">
    <div class="section-title">Elevated Draft Preview (Revision <span id="rev-num">0</span>)</div>
    <div id="draft-box" class="draft-box">Waiting for voice input...</div>
  </div>

  <div class="section">
    <div class="section-title">Change Diff vs Raw Utterance</div>
    <div id="diff-box" class="diff-box">No changes</div>
  </div>

  <div class="controls">
    <button id="btn-talk" class="btn-primary" onmousedown="startTalk()" onmouseup="stopTalk()">
      Hold to Speak (PTT)
    </button>
    <button id="btn-copy" onclick="copyDraft()">Copy to Clipboard</button>
    <button id="btn-paste" class="btn-success" onclick="pasteDraft()">Paste into Target</button>
    <button id="btn-revert" onclick="revertDraft()">Revert to Raw</button>
    <button id="btn-cancel" class="btn-danger" onclick="cancelOp()">Cancel</button>
  </div>

  <div class="revision-input-group">
    <input type="text" id="revise-input" placeholder="Revise draft by instruction...">
    <button onclick="submitRevision()">Revise</button>
  </div>
</div>

<script>
let pollInterval = null;

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    
    document.getElementById('state-label').innerText = data.state;
    document.getElementById('orb').className = 'orb ' + data.state;
    
    if (data.active_target) {
      const snip = data.active_target.title.slice(0, 15);
      const badgeText = `Target: ${data.active_target.wm_class} (${snip})`;
      document.getElementById('target-badge').innerText = badgeText;
    } else {
      document.getElementById('target-badge').innerText = 'Target: Active Desktop Window';
    }

    if (data.current_draft) {
      document.getElementById('draft-box').innerText = data.current_draft.text;
      document.getElementById('rev-num').innerText = data.current_draft.revision;
    }

    if (data.last_diff) {
      document.getElementById('diff-box').innerText = data.last_diff;
    }
  } catch (err) {
    console.error('Status poll error:', err);
  }
}

async function startTalk() {
  document.getElementById('btn-talk').innerText = 'Listening... Release to elevate';
  await fetch('/api/listen/start', { method: 'POST' });
  fetchStatus();
}

async function stopTalk() {
  document.getElementById('btn-talk').innerText = 'Hold to Speak (PTT)';
  await fetch('/api/listen/stop', { method: 'POST' });
  fetchStatus();
}

async function copyDraft() {
  await fetch('/api/copy', { method: 'POST' });
  fetchStatus();
}

async function pasteDraft() {
  const res = await fetch('/api/paste', { method: 'POST' });
  const data = await res.json();
  if (!res.ok) {
    alert('Paste failed: ' + (data.detail || 'Target mismatch or error'));
  }
  fetchStatus();
}

async function revertDraft() {
  await fetch('/api/revert', { method: 'POST' });
  fetchStatus();
}

async function cancelOp() {
  await fetch('/api/cancel', { method: 'POST' });
  fetchStatus();
}

async function submitRevision() {
  const input = document.getElementById('revise-input');
  const val = input.value.trim();
  if (!val) return;
  await fetch('/api/revise', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction: val })
  });
  input.value = '';
  fetchStatus();
}

setInterval(fetchStatus, 1000);
fetchStatus();
</script>
</body>
</html>
"""

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_CONTENT

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

    return app
