"""Tests for Wren core draft buffer and state machine."""
import time

import pytest

from wren.core.buffer import (
    ApprovalExpiredError,
    ApprovalInvalidatedError,
    DraftBuffer,
)
from wren.core.state import StateTransitionError, WrenState, WrenStateMachine


def test_utterance_and_initial_draft_creation():
    buffer = DraftBuffer()
    utterance = buffer.record_utterance("Draft an email to John about Friday's deployment")
    assert utterance.raw_text == "Draft an email to John about Friday's deployment"
    assert utterance.audio_retained is False
    
    draft = buffer.create_draft_from_utterance(
        utterance.id,
        "Please review the deployment scheduled for Friday.",
    )
    assert draft.revision == 1
    assert draft.op == "transcribe"
    assert draft.text_sha256 == DraftBuffer.hash_text("Please review the deployment scheduled for Friday.")
    assert buffer.current_draft == draft


def test_revision_loop_and_invalidation():
    buffer = DraftBuffer()
    u1 = buffer.record_utterance("Schedule review on Friday")
    buffer.create_draft_from_utterance(u1.id, "Meeting Friday at 10 AM.")
    
    approval = buffer.create_approval("paste", target_hash="window-123", ttl_seconds=60)
    assert approval.revision == 1
    assert approval.consumed is False
    
    # Adding a revision must invalidate outstanding approval
    u2 = buffer.record_utterance("Make it 11 AM instead")
    d2 = buffer.revise_draft(
        "Meeting Friday at 11 AM.",
        op="revise",
        instruction="Make it 11 AM instead",
        utterance_id=u2.id,
    )
    assert d2.revision == 2
    assert d2.parent_revision == 1
    assert buffer.current_draft.revision == 2
    
    # Prior approval is now invalidated
    with pytest.raises(ApprovalInvalidatedError):
        buffer.consume_approval(approval.id, current_target_hash="window-123")


def test_approval_expiry():
    buffer = DraftBuffer()
    u1 = buffer.record_utterance("Test command")
    buffer.create_draft_from_utterance(u1.id, "Text")
    
    # Short TTL
    approval = buffer.create_approval("paste", target_hash="window-abc", ttl_seconds=0.05)
    time.sleep(0.08)
    
    with pytest.raises(ApprovalExpiredError):
        buffer.consume_approval(approval.id, current_target_hash="window-abc")


def test_approval_wrong_target_rejected():
    buffer = DraftBuffer()
    u1 = buffer.record_utterance("Hello")
    buffer.create_draft_from_utterance(u1.id, "Hello")
    approval = buffer.create_approval("paste", target_hash="window-editor", ttl_seconds=60)
    
    with pytest.raises(ValueError, match="Target mismatch"):
        buffer.consume_approval(approval.id, current_target_hash="window-browser-hijack")


def test_state_machine_valid_and_invalid_transitions():
    sm = WrenStateMachine()
    assert sm.state == WrenState.IDLE
    
    sm.transition(WrenState.LISTENING)
    assert sm.state == WrenState.LISTENING
    
    sm.transition(WrenState.PROCESSING)
    assert sm.state == WrenState.PROCESSING
    
    sm.transition(WrenState.DRAFT_READY)
    assert sm.state == WrenState.DRAFT_READY
    
    # In DRAFT_READY, can revise
    sm.transition(WrenState.PROCESSING)
    sm.transition(WrenState.DRAFT_READY)
    
    # Copy action
    sm.transition(WrenState.COPY_APPROVED)
    sm.transition(WrenState.CLIPBOARD_WRITTEN)
    
    # Paste action
    sm.transition(WrenState.TARGET_VERIFY)
    sm.transition(WrenState.PASTE_PENDING_APPROVAL)
    sm.transition(WrenState.PASTING)
    sm.transition(WrenState.PASTED)
    sm.transition(WrenState.IDLE)
    
    # Invalid transition directly from IDLE to PASTING
    with pytest.raises(StateTransitionError):
        sm.transition(WrenState.PASTING)


def test_cancel_returns_to_idle():
    sm = WrenStateMachine()
    sm.transition(WrenState.LISTENING)
    sm.cancel()
    assert sm.state == WrenState.IDLE
