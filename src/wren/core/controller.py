"""Wren Controller: Authoritative coordinator of voice, elevation, buffer, clipboard, and target paste."""

from wren.audio.capture import DesktopAudioCapture
from wren.clipboard.factory import get_clipboard_manager
from wren.clipboard.x11 import ClipSnapshot
from wren.core.buffer import Draft, DraftBuffer, Receipt
from wren.core.state import WrenState, WrenStateMachine
from wren.files.ingest import UnifiedFileIngest
from wren.llm.elevate import PromptElevator
from wren.screen.inspector import ScreenContext, ScreenInspector
from wren.stt.engine import BaseSTT, FasterWhisperSTT, MockSTT
from wren.targets.factory import get_target_manager
from wren.targets.x11 import TargetMismatchError, TargetWindow


class WrenController:
    """Master orchestrator for Wren Phase 1 Desktop Vertical Slice."""

    def __init__(
        self,
        stt_provider: str = "faster-whisper",
        model: str = "auto",
        llm_base_url: str = "http://127.0.0.1:18880/v1",
    ):
        self.state_machine = WrenStateMachine()
        self.buffer = DraftBuffer()
        self.audio = DesktopAudioCapture()
        self.elevator = PromptElevator(base_url=llm_base_url, model=model)
        self.clipboard = get_clipboard_manager()
        self.targets = get_target_manager()
        self.screen_inspector = ScreenInspector()
        self.file_ingest = UnifiedFileIngest()
        self.attached_files: list[dict] = []
        
        if stt_provider == "mock":
            self.stt: BaseSTT = MockSTT()
        else:
            self.stt: BaseSTT = FasterWhisperSTT(model_size="base.en")
            
        self.active_target: TargetWindow | None = None
        self._prior_clipboard_snap: ClipSnapshot | None = None
        self._last_diff: str = ""

    @property
    def state(self) -> WrenState:
        return self.state_machine.state

    @property
    def current_draft(self) -> Draft | None:
        return self.buffer.current_draft

    @property
    def last_diff(self) -> str:
        return self._last_diff

    def start_listening(self) -> None:
        """
        Record the currently focused target window BEFORE recording,
        then begin microphone capture.
        """
        try:
            self.active_target = self.targets.get_active_target()
        except Exception:
            self.active_target = None

        self._prior_clipboard_snap = self.clipboard.snapshot()
        self.state_machine.transition(WrenState.LISTENING)
        self.audio.start()

    def stop_listening(self) -> Draft:
        """
        Stop audio recording, run STT transcription, elevate text via LLM,
        and stage in the draft buffer without touching the system clipboard.
        """
        wav_path = self.audio.stop()
        self.state_machine.transition(WrenState.PROCESSING)

        if not wav_path:
            raw_text = "No audio detected"
        else:
            raw_text = self.stt.transcribe(wav_path)

        utterance = self.buffer.record_utterance(raw_text=raw_text)
        prompt_text = raw_text
        if self.attached_files:
            context_blocks = "\n\n".join(
                f"[Attached file: {f['name']}]\n{f['content']}"
                for f in self.attached_files
            )
            prompt_text = f"{context_blocks}\n\n[Instruction / Speech]:\n{raw_text}"

        elev_res = self.elevator.elevate(prompt_text)
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
            self.active_target = self.targets.get_active_target()

        # Target verification check
        current = self.targets.get_active_target()
        if current.window_id != self.active_target.window_id:
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

    def cancel(self) -> None:
        """Cancel and reset state to IDLE."""
        self.audio.cancel()
        self.state_machine.cancel()
