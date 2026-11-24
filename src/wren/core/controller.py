"""Wren Controller: Authoritative coordinator of voice, elevation, buffer, clipboard, and target paste."""

import os
import re
import subprocess
import threading
import time

from wren.audio.capture import DesktopAudioCapture
from wren.bridge.workspace import WorkspaceWriter
from wren.browser.session import BrowserController
from wren.clipboard.factory import get_clipboard_manager
from wren.clipboard.x11 import ClipboardError, ClipSnapshot
from wren.core.buffer import Draft, DraftBuffer, Receipt
from wren.core.state import WrenState, WrenStateMachine
from wren.files.ingest import UnifiedFileIngest
from wren.llm.elevate import PromptElevator
from wren.memory.store import MemoryStore
from wren.screen.inspector import ScreenContext, ScreenInspector
from wren.stt.correct import apply_corrections, load_corrections
from wren.stt.engine import BaseSTT, CloudHybridSTT, FasterWhisperSTT, MockSTT
from wren.targets.factory import get_target_manager
from wren.targets.x11 import TargetMismatchError, TargetWindow
from wren.tts.engine import NeuralTTS, TTSAudio


class WrenController:
    """Master orchestrator for Wren Phase 1 Desktop Vertical Slice."""

    def __init__(
        self,
        stt_provider: str = "hybrid",
        model: str = "auto",
        llm_base_url: str = "http://127.0.0.1:18880/v1",
        tts_provider: str | None = None,
    ):
        self.state_machine = WrenStateMachine()
        self.buffer = DraftBuffer()
        self.audio = DesktopAudioCapture()
        self.elevator = PromptElevator(base_url=llm_base_url, model=model)
        self.tts = NeuralTTS(
            default_provider=tts_provider or os.environ.get("WREN_TTS_PROVIDER", "qwen")
        )
        self.clipboard = get_clipboard_manager()
        self.targets = get_target_manager()
        self.screen_inspector = ScreenInspector()
        self.file_ingest = UnifiedFileIngest()
        self.attached_files: list[dict] = []
        self.mode: str = "elevate"
        self.phone_screen: dict = {"package": "", "text": "", "at": 0.0}
        self.live_snapshotter = None
        self.live_engine = None
        self._agentwork = None
        self.conversation: list[dict] = []
        self.conversation_ttl: float = 900.0  # turns older than 15 min are excluded from prompts
        self.last_reply: str = ""
        self.last_transcript: str = ""
        self.is_speaking: bool = False
        self.speaking_level: float = 0.0
        self.browser = BrowserController()
        self.workspace = WorkspaceWriter()
        # Remote-agent session: while an MCP client is driving, local mutating
        # surfaces (hotkeys, local REST actions) yield until the lease expires.
        self._remote_session_until: float = 0.0
        self.remote_session_ttl: float = float(os.environ.get("WREN_REMOTE_SESSION_TTL", "180"))
        # Persistent memory: facts (glossary/contacts/prefs), agent registry,
        # and the Ren<->agent handoff queue. One SQLite file, no daemon.
        self.memory = MemoryStore()

        if stt_provider == "mock":
            self.stt: BaseSTT = MockSTT()
        elif stt_provider == "faster-whisper":
            self.stt = FasterWhisperSTT(model_size="base.en")
        else:
            self.stt = CloudHybridSTT()

        self.active_target: TargetWindow | None = None
        self._prior_clipboard_snap: ClipSnapshot | None = None
        self._last_diff: str = ""
        self._listen_intent: str = "auto"
        self._pre_listen_state: WrenState = WrenState.IDLE
        self._target_watch_stop = threading.Event()
        threading.Thread(
            target=self._watch_targets, daemon=True, name="wren-target-watch"
        ).start()

    def _paste_target(self) -> TargetWindow:
        """Active window for paste purposes; falls back on platforms without memory."""
        getter = getattr(self.targets, "get_paste_target", None) or self.targets.get_active_target
        return getter()

    def _watch_targets(self) -> None:
        """Keep the remembered external window fresh while Ren is idle, so the
        orb can always tell which app the user was last working in."""
        peek = getattr(self.targets, "peek_paste_target", None)
        while not self._target_watch_stop.wait(2.5):
            try:
                if self.state == WrenState.IDLE and peek is not None:
                    target = peek()
                    if target is not None:
                        self.active_target = target
            except Exception:
                pass

    @property
    def agentwork(self):
        """Lazy AgentWorkspace coordinator (agent work-through layer)."""
        if self._agentwork is None:
            from wren.agentwork import AgentWorkspace

            self._agentwork = AgentWorkspace(self)
        return self._agentwork

    @property
    def state(self) -> WrenState:
        return self.state_machine.state

    @property
    def current_draft(self) -> Draft | None:
        return self.buffer.current_draft

    @property
    def last_diff(self) -> str:
        return self._last_diff

    @property
    def voice_level(self) -> float:
        """Unified 0.0..1.0 energy level driving the orb's radial glow equalizer."""
        return max(self.audio.level, self.speaking_level)

    @property
    def remote_session_active(self) -> bool:
        """Whether a remote MCP agent currently holds the yield lease."""
        return time.time() < self._remote_session_until

    def touch_remote_session(self) -> None:
        """Extend the remote-agent lease; called per MCP request so a quiet
        agent releases local control automatically."""
        self._remote_session_until = time.time() + self.remote_session_ttl

    def release_remote_session(self) -> None:
        """User kill-switch: end the remote-agent lease immediately."""
        self._remote_session_until = 0.0

    def status_dict(self, mcp_http_mounted: bool) -> dict:
        """The /api/status payload, shared by the REST route and the in-process
        MCP dispatch so both report exactly the same shape."""
        target_dict = None
        if self.active_target:
            target_dict = {
                "window_id": self.active_target.window_id,
                "wm_class": self.active_target.wm_class,
                "title": self.active_target.title,
                "is_terminal": self.active_target.is_terminal,
            }

        draft_dict = None
        if self.current_draft:
            draft_dict = {
                "id": self.current_draft.id,
                "revision": self.current_draft.revision,
                "text": self.current_draft.text,
                "op": self.current_draft.op,
            }

        return {
            "state": self.state.value,
            "mode": self.mode,
            "is_recording": self.audio.is_recording,
            "is_speaking": self.is_speaking,
            "voice_level": round(self.voice_level, 3),
            "tts_provider": self.tts.provider,
            "tts_muted": self.tts.muted,
            "current_draft": draft_dict,
            "last_diff": self.last_diff,
            "last_transcript": self.last_transcript,
            "active_target": target_dict,
            "last_reply": self.last_reply,
            "phone_screen": {
                "package": self.phone_screen.get("package", ""),
                "chars": len(self.phone_screen.get("text") or ""),
                "age_s": round(time.time() - float(self.phone_screen.get("at", 0)), 1)
                    if self.phone_screen.get("at") else None,
            },
            "conversation_turns": len(self.conversation),
            "memory_facts": self.memory.fact_count(),
            "agents_online": len(self.memory.agents(fresh_s=600)),
            "handoffs_open": self.memory.open_handoff_count(),
            "remote_session": {
                "active": self.remote_session_active,
                "ttl_s": round(max(0.0, self._remote_session_until - time.time()), 1),
            },
            "mcp_http_mounted": mcp_http_mounted,
        }

    def set_mode(self, mode: str) -> str:
        """Set operation mode: 'elevate' | 'agent' | 'brainstorm' | 'talk'."""
        valid_modes = {"elevate", "agent", "brainstorm", "talk"}
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode '{mode}'. Valid modes: {valid_modes}")
        self.mode = mode
        return self.mode

    def speak_async(self, text: str, provider: str | None = None) -> None:
        """Synthesize and play speech asynchronously through system output while animating orb level."""
        if self.tts.muted or not (text or "").strip():
            return

        def _run() -> None:
            audio: TTSAudio | None = self.tts.synthesize(text, provider=provider)
            if audio is None:
                return
            self.is_speaking = True
            try:
                def _on_lvl(val: float) -> None:
                    self.speaking_level = val

                self.tts.play_audio_bytes(audio, on_level=_on_lvl)
            finally:
                self.is_speaking = False
                self.speaking_level = 0.0

        threading.Thread(target=_run, daemon=True, name="wren-tts-play").start()

    def start_listening(self, intent: str = "auto") -> None:
        """
        Record the currently focused target window BEFORE recording,
        halt any active TTS playback (barge-in), then begin microphone capture.
        """
        if self.remote_session_active:
            # A remote MCP agent is driving — local mic/hotkey input yields
            # until its lease expires (or the user hits /api/session/release).
            return
        self.tts.stop()
        self.is_speaking = False
        self.speaking_level = 0.0
        if self.state == WrenState.ERROR:
            self.state_machine.cancel()
        self._pre_listen_state = self.state
        self._listen_intent = intent
        try:
            self.active_target = self._paste_target()
        except Exception:
            self.active_target = None

        self._prior_clipboard_snap = self.clipboard.snapshot()
        self.state_machine.transition(WrenState.LISTENING)
        self.audio.start()

    def _restore_after_empty_capture(self) -> None:
        if self.current_draft and self._pre_listen_state == WrenState.DRAFT_READY:
            self.state_machine.transition(WrenState.DRAFT_READY)
        else:
            self.state_machine.transition(WrenState.IDLE)

    _STT_NOISE_ARTIFACTS = {
        "thank you", "thanks", "thank you for watching", "thanks for watching",
        "you", "bye", "bye bye", "okay", "ok", "so", "the end", "subscribe",
        "see you", "thank you so much", "all right", "alright", "um", "uh",
        "music", "applause", "laughter",
    }

    def _is_stt_artifact(self, text: str, wav_data: bytes) -> bool:
        """
        Grounding check: is this transcript plausibly real speech by the user?
        Whisper hallucinates stock phrases on silence/noise — a short,
        low-energy or near-silent capture that yields one of those phrases
        is dropped here, before it can become a phantom conversation turn.
        """
        normalized = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if not normalized:
            return True
        peak = float(getattr(self.audio, "peak_level", 1.0) or 0.0)
        duration_s = max(0, len(wav_data) - 44) / 32000.0  # 16kHz s16le mono
        # Known noise-hallucination phrase on a weak capture -> artifact.
        # Real phrases at real energy are kept even when short.
        if normalized in self._STT_NOISE_ARTIFACTS and (peak < 0.04 or duration_s < 0.8):
            return True
        # Essentially silent mic + trivially short transcript -> drop.
        if peak < 0.02 and len(normalized) <= 2:
            return True
        return False

    def stop_listening(self, intent: str | None = None) -> Draft | None:
        """
        Stop audio recording, run AST/STT transcription, and either:
          - 'talk': converse back via FreeLLMAPI + neural TTS (two-way voice conversation)
          - 'dictate': elevate text into the preview draft buffer without touching clipboard
        """
        eff_intent = intent or self._listen_intent
        if eff_intent == "auto":
            eff_intent = "talk" if self.mode == "talk" else "dictate"

        wav_data = self.audio.stop()
        self.state_machine.transition(WrenState.PROCESSING)

        if not wav_data:
            self._restore_after_empty_capture()
            return None

        # Memory glossary rides on top of the built-in corrections: a fact the
        # user stored as a spelling rule ('Dearing is spelled D-E-E-R-I-N-G')
        # corrects every future transcription deterministically.
        corr_table = load_corrections()
        try:
            corr_table.update(self.memory.glossary())
        except Exception:
            pass
        raw_text = apply_corrections((self.stt.transcribe(wav_data) or "").strip(), corr_table)
        if not raw_text:
            self._restore_after_empty_capture()
            return None

        # Grounding gate: a transcript is only real if the user actually spoke.
        # Short / low-energy captures that yield known Whisper noise artifacts
        # are dropped here — before they can become phantom conversation turns.
        if self._is_stt_artifact(raw_text, wav_data):
            self._restore_after_empty_capture()
            return None

        self.last_transcript = raw_text

        if eff_intent == "talk":
            reply = self.converse(raw_text, source="voice")
            self.state_machine.transition(WrenState.IDLE)
            self.speak_async(reply)
            return None

        utterance = self.buffer.record_utterance(raw_text=raw_text)
        prompt_text = raw_text
        if self.attached_files:
            context_blocks = "\n\n".join(
                f"[Attached file: {f['name']}]\n{f['content']}"
                for f in self.attached_files
            )
            prompt_text = f"{context_blocks}\n\n[Instruction / Speech]:\n{raw_text}"

        elev_res = self.elevator.elevate(prompt_text, mode=self.mode)
        self._last_diff = elev_res.diff

        draft = self.buffer.create_draft_from_utterance(
            utterance_id=utterance.id,
            text=elev_res.elevated_text,
            op="elevate",
            model=elev_res.model,
        )
        draft.intent_flag = not elev_res.intent_preserved

        self.state_machine.transition(WrenState.DRAFT_READY)
        return draft

    def revise(self, instruction: str) -> Draft:
        """
        Revise the current draft by instruction, preserving prior intent
        and invalidating any outstanding approvals.
        """
        if not self.current_draft:
            raise ValueError("No active draft to revise")

        self.state_machine.transition(WrenState.PROCESSING)
        utterance = self.buffer.record_utterance(raw_text=instruction)

        elev_res = self.elevator.elevate(
            raw_text=instruction,
            instruction=instruction,
            current_draft_text=self.current_draft.text,
            mode=self.mode,
        )
        self._last_diff = elev_res.diff

        rev_draft = self.buffer.revise_draft(
            new_text=elev_res.elevated_text,
            op="revise",
            instruction=instruction,
            utterance_id=utterance.id,
            model=elev_res.model,
        )
        rev_draft.intent_flag = not elev_res.intent_preserved

        self.state_machine.transition(WrenState.DRAFT_READY)
        return rev_draft

    def revert_to_raw(self) -> Draft | None:
        """Revert draft directly to the raw text of the initial utterance."""
        draft = self.buffer.revert_to_raw()
        if draft:
            self._last_diff = self.elevator.compute_diff(
                self.buffer.utterances[draft.utterance_ids[0]].raw_text,
                draft.text,
            )
        return draft

    def copy_current_draft(self) -> str:
        """
        Write the previewed draft to the system clipboard upon explicit command.
        Verifies readback hash.
        """
        if not self.current_draft:
            raise ValueError("No active draft to copy")

        self.state_machine.transition(WrenState.COPY_APPROVED)
        written_hash = self.clipboard.write(self.current_draft.text)
        self.state_machine.transition(WrenState.CLIPBOARD_WRITTEN)
        return written_hash

    def paste_current_draft(self) -> Receipt:
        """
        Verify target window matches what was captured when listening started,
        write to clipboard if not already written, execute paste shortcut,
        record verified receipt, and safely return to IDLE.
        """
        if not self.current_draft:
            raise ValueError("No active draft to paste")
        if not self.active_target:
            self.active_target = self._paste_target()

        # Target verification check — Ren's own widget holding focus is not a
        # mismatch; paste_into_target activates the approved window instead.
        current = self.targets.get_active_target()
        if not getattr(current, "is_self", False) and current.window_id != self.active_target.window_id:
            raise TargetMismatchError(
                f"Active window changed from {self.active_target.wm_class} "
                f"to {current.wm_class}. Aborting paste."
            )

        # Create approval bound to exact draft and target
        approval = self.buffer.create_approval(
            action="paste",
            target_hash=self.active_target.target_hash,
            ttl_seconds=120.0,
        )

        # Write to clipboard if not yet written
        if self.state != WrenState.CLIPBOARD_WRITTEN:
            self.copy_current_draft()

        # Consume approval
        self.buffer.consume_approval(approval.id, current_target_hash=self.active_target.target_hash)

        # Transition to PASTING
        if self.state == WrenState.CLIPBOARD_WRITTEN:
            self.state_machine.transition(WrenState.TARGET_VERIFY)
        self.state_machine.transition(WrenState.PASTE_PENDING_APPROVAL)
        self.state_machine.transition(WrenState.PASTING)

        # Execute injection
        paste_result = self.targets.paste_into_target(self.active_target)

        # Record receipt
        shortcut_used = paste_result.get("shortcut", "ctrl+v") if isinstance(paste_result, dict) else "ctrl+v"
        evidence_str = (
            f"Pasted into {self.active_target.wm_class} "
            f"(win:{self.active_target.window_id}) via {shortcut_used}"
        )
        receipt = self.buffer.record_receipt(
            action_id=approval.id,
            outcome="done",
            evidence=evidence_str,
        )

        self.state_machine.transition(WrenState.PASTED)
        self.state_machine.transition(WrenState.IDLE)
        return receipt

    def attach_file(self, file_path: str) -> dict:
        """Attach a file's content to the prompt context."""
        content = self.file_ingest.read_file(file_path)
        from pathlib import Path
        name = Path(file_path).name
        attached = {"path": file_path, "name": name, "content": content}
        self.attached_files.append(attached)
        return attached

    def clear_attached_files(self) -> None:
        """Clear all attached context files."""
        self.attached_files.clear()

    def get_screen_context(self, capture_screenshot: bool = False) -> ScreenContext:
        """Inspect active screen/window interface context."""
        return self.screen_inspector.get_active_screen_context(capture_screenshot=capture_screenshot)

    def _enter_processing(self) -> None:
        if self.state == WrenState.PROCESSING:
            return
        if self.state == WrenState.LISTENING:
            self.audio.cancel()
            self.state_machine.transition(WrenState.PROCESSING)
            return
        if self.state not in (WrenState.IDLE, WrenState.DRAFT_READY):
            self.state_machine.cancel()
        self.state_machine.transition(WrenState.PROCESSING)

    def _draft_payload(self, draft: Draft, raw: str) -> dict:
        return {
            "type": "paste_preview",
            "requires_confirm": True,
            "raw": raw,
            "draft": {"id": draft.id, "text": draft.text, "revision": draft.revision},
        }

    def note_phone_screen(self, package: str, text: str) -> dict:
        self.phone_screen = {
            "package": (package or "")[:200],
            "text": (text or "")[:20000],
            "at": time.time(),
        }
        return {
            "type": "screen_stored",
            "chars": len(self.phone_screen["text"]),
            "package": self.phone_screen["package"],
        }

    def accept_dictation(self, text: str, source: str = "android") -> Draft:
        """Turn a finished dictation into a paste preview. Does not paste."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Empty dictation")
        self._enter_processing()
        provider = "android-speech" if source == "android" else "text"
        utterance = self.buffer.record_utterance(raw_text=cleaned, source=source, stt_provider=provider)
        # Dictation paste preview is the speaker's words, cleaned. Chat lives in converse().
        elev_res = self.elevator.elevate(cleaned, mode="elevate")
        self._last_diff = elev_res.diff
        draft = self.buffer.create_draft_from_utterance(
            utterance_id=utterance.id,
            text=elev_res.elevated_text,
            op="dictate",
            model=elev_res.model,
        )
        draft.intent_flag = not elev_res.intent_preserved
        self.state_machine.transition(WrenState.DRAFT_READY)
        return draft

    _REVISE_REPLY_RE = re.compile(
        r"\b(say (it|that)|make (it|that)|rephrase|repeat|shorter|longer|"
        r"more formal|less formal|simpler|friendlier|punchier|expand|"
        r"in my own words|your (last )?(answer|reply|response))\b",
        re.I,
    )

    _CLIP_EDIT_RE = re.compile(
        r"\b(clipboard|that text|this text|what i copied|the text)\b"
        r".*\b(fix|edit|rewrite|correct|change|clean|shorten|summar|translate|improve|expand|formal|spell)\b"
        r"|\b(fix|edit|rewrite|correct|change|clean|shorten|summar|translate|improve|expand|formal|spell)\b"
        r".*\b(clipboard|that text|this text|what i copied|the text)\b",
        re.I,
    )

    _REMEMBER_RE = re.compile(r"^remember\s+(?:that\s+)?(.+)$", re.I)
    _SPELLING_RE = re.compile(r"^(\w[\w\-]*)\s+(?:is\s+)?spel(?:l|led|t)\s+(.+)$", re.I)
    _HANDOFF_RE = re.compile(r"\b(?:ask|tell|have|get)\s+(\w+)\s+to\s+(.+)$", re.I)
    _HANDOFF_SKIP = {"me", "you", "him", "her", "them", "it", "us", "ren"}

    def remember(self, clause: str) -> str:
        """Store a durable fact. 'X is spelled Y' also becomes a permanent
        STT correction so dictation gets the spelling right from now on."""
        clause = (clause or "").strip()
        sm = self._SPELLING_RE.match(clause)
        if sm:
            misspelling = sm.group(1).lower()
            correct = re.sub(r"[\s\-_.]+", "", sm.group(2))
            correct = correct.title() if correct.isupper() else correct
            self.memory.remember(f"spell:{misspelling}", correct, tags="correction")
            return f"Got it — {misspelling} is spelled {correct}. I'll get it right from now on."
        km = re.match(r"^(.{2,60}?)\s+is\s+(.+)$", clause, re.I)
        if km:
            key, value = km.group(1).strip().lower(), km.group(2).strip()
        else:
            key, value = clause[:60].lower(), clause
        self.memory.remember(key, value)
        return f"Remembered: {key} — {value}."

    def delegate(self, agent: str, task: str) -> str:
        """Queue a task for a remote agent; reports honestly if he's offline."""
        self.memory.create_handoff(task, created_by="ren")
        online = {a["name"] for a in self.memory.agents(fresh_s=600)}
        if agent in online:
            return f"On it — {agent} picks it up on his next check-in."
        return f"Queued for {agent} — he's not connected right now, but it'll be waiting."

    def _note_turn(self, user_text: str, reply: str, source: str) -> str:
        now = time.time()
        self.conversation.append({"role": "user", "content": user_text, "at": now, "src": source})
        self.conversation.append({"role": "assistant", "content": reply, "at": time.time()})
        self.conversation = self.conversation[-20:]
        self.last_reply = reply
        return reply

    def edit_clipboard(self, instruction: str) -> dict:
        """OpenWhispr-style: transform the user's clipboard in place via LLM."""
        cleaned = (instruction or "").strip()
        snap = self.clipboard.snapshot()
        if not snap.content.strip():
            raise ClipboardError("Clipboard is empty — nothing to change")
        res = self.elevator.elevate(
            cleaned,
            instruction=cleaned,
            current_draft_text=snap.content,
            mode="revise",
        )
        new_text = res.elevated_text.strip()
        if not new_text:
            raise ClipboardError("LLM returned empty edit")
        self.clipboard.write(new_text)
        return {
            "type": "clipboard_updated",
            "chars": len(new_text),
            "preview": new_text[:240],
            "requires_confirm": True,
        }

    def converse(self, text: str, source: str = "orb") -> str:
        """Live reply for the orb. Does not paste and does not rewrite the clipboard."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Empty message")
        # Reply-revision intent: 'say it shorter', 'make it more formal', etc.
        if self.last_reply and len(cleaned.split()) <= 10 and self._REVISE_REPLY_RE.search(cleaned):
            rev = self.elevator.elevate(
                cleaned, instruction=cleaned,
                current_draft_text=self.last_reply, mode="revise",
            )
            reply = rev.elevated_text.strip() or self.last_reply
            now = time.time()
            self.conversation.append({"role": "user", "content": cleaned, "at": now, "src": source})
            self.conversation.append({"role": "assistant", "content": reply, "at": time.time()})
            self.conversation = self.conversation[-20:]
            self.last_reply = reply
            return reply

        # Clipboard-edit intent: 'fix the spelling in my clipboard' etc.
        if self._CLIP_EDIT_RE.search(cleaned):
            try:
                result = self.edit_clipboard(cleaned)
                reply = (
                    f"Done — your clipboard is updated ({result['chars']} chars). "
                    f"Paste it wherever you want."
                )
            except ClipboardError as e:
                reply = f"I couldn't edit the clipboard: {e}"
            return self._note_turn(cleaned, reply, source)

        # Memory intent: 'remember that dana's email is ...'
        m = self._REMEMBER_RE.match(cleaned)
        if m:
            return self._note_turn(cleaned, self.remember(m.group(1)), source)

        # Delegation intent: 'ask bart to check the tunnel' — queues a handoff.
        m = self._HANDOFF_RE.search(cleaned)
        if m and m.group(1).lower() not in self._HANDOFF_SKIP:
            return self._note_turn(cleaned, self.delegate(m.group(1).lower(), m.group(2).strip()), source)

        now = time.time()
        notes: list[str] = []
        if self.active_target:
            notes.append(
                f"Desktop focus: {self.active_target.wm_class} — {self.active_target.title[:160]}"
            )
        # Phone screen is context, not gospel: only if fresh (<10 min old).
        phone = self.phone_screen
        if phone.get("text") and now - float(phone.get("at", 0)) < 600:
            notes.append(
                f"Phone screen ({phone.get('package', '?')}): {str(phone['text'])[:1500]}"
            )
        # Real screen grounding: X11 primary selection = text the user is
        # literally pointing at right now. Instant, zero LLM cost.
        try:
            sel = subprocess.run(
                ["xclip", "-selection", "primary", "-o"],
                capture_output=True, text=True, timeout=1.0, check=False,
            )
            sel_text = (sel.stdout or "").strip()[:800] if sel.returncode == 0 else ""
            if sel_text:
                notes.append(f"Highlighted text on screen:\n{sel_text}")
        except Exception:
            pass
        # Persistent memory (talk path only — brainstorm/elevate deliberately
        # do not inject, so generation stays unanchored). Top hits for this
        # utterance + who is connected right now.
        try:
            hits = self.memory.recall(cleaned, limit=3)
            if hits:
                notes.append(
                    "MEMORY (facts Ren has stored):\n"
                    + "\n".join(f"- {h['key']}: {h['value']}" for h in hits)
                )
            online = self.memory.agents(fresh_s=600)
            if online:
                notes.append(
                    "Connected agents: "
                    + ", ".join(
                        f"{a['name']} ({', '.join(a['capabilities']) or 'general'}, "
                        f"{int(a['age_s'])}s ago)"
                        for a in online
                    )
                )
        except Exception:
            pass
        screen_note = "\n".join(notes)
        # History TTL: drop turns older than 15 min so stale sessions can't
        # masquerade as the current conversation.
        live_history = [
            t for t in self.conversation
            if now - float(t.get("at", now)) < self.conversation_ttl
        ]
        result = self.elevator.converse(cleaned, screen_note=screen_note, history=live_history)
        reply = result.elevated_text.strip()
        self.conversation.append({"role": "user", "content": cleaned, "at": now, "src": source})
        self.conversation.append({"role": "assistant", "content": reply, "at": time.time()})
        self.conversation = self.conversation[-20:]
        self.last_reply = reply
        return reply

    def paste_preview(self) -> dict:
        draft = self.current_draft
        if not draft:
            return {"type": "paste_preview", "requires_confirm": True, "draft": None}
        return {
            "type": "paste_preview",
            "requires_confirm": True,
            "draft": {"id": draft.id, "text": draft.text, "revision": draft.revision},
        }

    def cancel(self) -> None:
        """Cancel and reset state to IDLE."""
        self.tts.stop()
        self.is_speaking = False
        self.speaking_level = 0.0
        self.audio.cancel()
        self.state_machine.cancel()
