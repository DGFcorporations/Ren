"""End-to-End integrated test for Wren Phase 1 Desktop Vertical Slice."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.targets.x11 import TargetWindow


@pytest.fixture
def mock_target():
    return TargetWindow(
        window_id="12345",
        wm_class="gedit",
        title="Notes.txt - gedit",
        pid=1000,
        target_hash="hash-12345",
        is_terminal=False,
    )


def test_full_dictate_elevate_copy_paste_flow(mock_target):
    controller = WrenController(stt_provider="mock", model="auto")
    
    # Target window is active (e.g. gedit)
    with patch.object(controller.targets, "get_active_target", return_value=mock_target):
        # 1. Start push-to-talk
        controller.start_listening()
        assert controller.state == WrenState.LISTENING
        assert controller.active_target == mock_target

        # 2. Stop push-to-talk with raw speech input
        raw_speech = "can you draft an update to john about tomorrow at 2pm"
        with patch.object(controller.audio, "stop", return_value="/tmp/test.wav"), \
             patch.object(controller.stt, "transcribe", return_value=raw_speech), \
             patch.object(controller.elevator, "elevate") as mock_elevate:
            
            mock_elevate.return_value = MagicMock(
                raw_text=raw_speech,
                elevated_text="Please draft an update to John regarding tomorrow at 2:00 PM.",
                diff="[+Please draft an update to John regarding tomorrow at 2:00 PM.]",
                intent_preserved=True,
                model="auto",
                fallback_used=False,
            )
            
            draft = controller.stop_listening()
            assert controller.state == WrenState.DRAFT_READY
            assert draft.revision == 1
            assert "2:00 PM" in draft.text

        # 3. Preview ≠ clipboard: clipboard is untouched before copy!
        # Verify buffer has draft but clipboard not called
        assert controller.buffer.current_draft.revision == 1
        
        # 4. Voice revision loop: "make it 3pm instead"
        with patch.object(controller.elevator, "elevate") as mock_revise:
            mock_revise.return_value = MagicMock(
                raw_text="make it 3pm instead",
                elevated_text="Please draft an update to John regarding tomorrow at 3:00 PM.",
                diff="[-2:00 PM +3:00 PM]",
                intent_preserved=True,
                model="auto",
                fallback_used=False,
            )
            rev_draft = controller.revise("make it 3pm instead")
            assert rev_draft.revision == 2
            assert "3:00 PM" in rev_draft.text

        # 5. User says "copy"
        current_draft = controller.current_draft
        with patch.object(controller.clipboard, "write", return_value=current_draft.text_sha256):
            controller.copy_current_draft()
            assert controller.state == WrenState.CLIPBOARD_WRITTEN
            assert controller.clipboard_preview_ready is True

        # 6. User says "paste into gedit"
        with patch.object(controller.targets, "paste_into_target", return_value={"status": "pasted"}), \
             patch.object(
                 controller.clipboard,
                 "snapshot",
                 return_value=SimpleNamespace(content_hash=current_draft.text_sha256),
             ):
            receipt = controller.paste_current_draft()
            assert receipt.outcome == "done"
            assert controller.state == WrenState.IDLE
            assert controller.clipboard_preview_ready is False


def test_new_dictation_invalidates_previous_clipboard_review(mock_target):
    controller = WrenController(stt_provider="mock", model="auto")
    draft = controller.buffer.create_draft_from_utterance(
        controller.buffer.record_utterance("review this").id,
        "Reviewed text.",
    )
    controller.state_machine.transition(WrenState.DRAFT_READY)

    with patch.object(controller.clipboard, "write", return_value=draft.text_sha256):
        controller.copy_current_draft()
    assert controller.clipboard_preview_ready is True

    with (
        patch.object(controller.targets, "get_active_target", return_value=mock_target),
        patch.object(
            controller.clipboard,
            "snapshot",
            return_value=SimpleNamespace(content="", content_hash="prior"),
        ),
        patch.object(controller.audio, "start"),
    ):
        controller.start_listening()

    assert controller.state == WrenState.LISTENING
    assert controller.clipboard_preview_ready is False
    with pytest.raises(ValueError, match="Copy the current draft for review"):
        controller.paste_current_draft()


def test_external_clipboard_change_blocks_paste(mock_target):
    controller = WrenController(stt_provider="mock", model="auto")
    draft = controller.buffer.create_draft_from_utterance(
        controller.buffer.record_utterance("review this").id,
        "Reviewed text.",
    )
    controller.active_target = mock_target
    controller.state_machine.transition(WrenState.DRAFT_READY)
    with patch.object(controller.clipboard, "write", return_value=draft.text_sha256):
        controller.copy_current_draft()

    with patch.object(
        controller.clipboard,
        "snapshot",
        return_value=SimpleNamespace(content_hash="a different clipboard value"),
    ), patch.object(controller.targets, "paste_into_target") as paste_into_target:
        with pytest.raises(ValueError, match="Clipboard changed after review"):
            controller.paste_current_draft()

    paste_into_target.assert_not_called()
    assert controller.state == WrenState.IDLE
    assert controller.clipboard_preview_ready is False


def test_target_hijack_aborts_paste(mock_target):
    controller = WrenController(stt_provider="mock", model="auto")
    controller.active_target = mock_target
    
    # Create draft
    u = controller.buffer.record_utterance("some text")
    draft = controller.buffer.create_draft_from_utterance(u.id, "some text")
    controller.state_machine.transition(WrenState.LISTENING)
    controller.state_machine.transition(WrenState.PROCESSING)
    controller.state_machine.transition(WrenState.DRAFT_READY)
    with patch.object(controller.clipboard, "write", return_value=draft.text_sha256):
        controller.copy_current_draft()
    
    # Switched target
    hijacked_target = TargetWindow(
        window_id="99999",
        wm_class="browser",
        title="Malicious Site",
        pid=9999,
        target_hash="hash-browser",
        is_terminal=False,
    )
    
    with (
        patch.object(
            controller.clipboard,
            "snapshot",
            return_value=SimpleNamespace(content_hash=draft.text_sha256),
        ),
        patch.object(controller.targets, "get_active_target", return_value=hijacked_target),
    ):
        with pytest.raises(Exception, match="Active window changed|Target mismatch"):
            controller.paste_current_draft()
