"""Wren state machine governing audio, preview, and approval flow."""
from enum import StrEnum


class StateTransitionError(ValueError):
    """Raised when an invalid state transition is attempted."""
    pass


class WrenState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    DRAFT_READY = "DRAFT_READY"
    COPY_APPROVED = "COPY_APPROVED"
    CLIPBOARD_WRITTEN = "CLIPBOARD_WRITTEN"
    TARGET_VERIFY = "TARGET_VERIFY"
    PASTE_PENDING_APPROVAL = "PASTE_PENDING_APPROVAL"
    PASTING = "PASTING"
    PASTED = "PASTED"
    MUTED = "MUTED"
    ERROR = "ERROR"


VALID_TRANSITIONS: dict[WrenState, set[WrenState]] = {
    WrenState.IDLE: {
        WrenState.LISTENING,
        WrenState.DRAFT_READY,
        WrenState.PROCESSING,
        WrenState.COPY_APPROVED,
        WrenState.MUTED,
        WrenState.ERROR,
    },
    WrenState.LISTENING: {WrenState.PROCESSING, WrenState.IDLE, WrenState.MUTED, WrenState.ERROR},
    WrenState.PROCESSING: {WrenState.DRAFT_READY, WrenState.IDLE, WrenState.ERROR},
    WrenState.DRAFT_READY: {
        WrenState.LISTENING,         # Start a fresh dictation; invalidate old clipboard review
        WrenState.PROCESSING,       # Voice/text revision loop
        WrenState.COPY_APPROVED,    # User "copy" command
        WrenState.TARGET_VERIFY,    # Direct paste workflow
        WrenState.IDLE,
        WrenState.ERROR,
    },
    WrenState.COPY_APPROVED: {WrenState.CLIPBOARD_WRITTEN, WrenState.IDLE, WrenState.ERROR},
    WrenState.CLIPBOARD_WRITTEN: {
        WrenState.LISTENING,         # Start a fresh dictation; invalidate old clipboard review
        WrenState.PROCESSING,       # Revise the draft; invalidate old clipboard review
        WrenState.DRAFT_READY,      # Revert the draft; require a fresh copy/review
        WrenState.TARGET_VERIFY,
        WrenState.IDLE,
        WrenState.ERROR,
    },
    WrenState.TARGET_VERIFY: {WrenState.PASTE_PENDING_APPROVAL, WrenState.IDLE, WrenState.ERROR},
    WrenState.PASTE_PENDING_APPROVAL: {WrenState.PASTING, WrenState.IDLE, WrenState.ERROR},
    WrenState.PASTING: {WrenState.PASTED, WrenState.ERROR},
    WrenState.PASTED: {WrenState.IDLE, WrenState.ERROR},
    WrenState.MUTED: {WrenState.IDLE, WrenState.LISTENING, WrenState.ERROR},
    WrenState.ERROR: {WrenState.IDLE},
}


class WrenStateMachine:
    """Manages the operational lifecycle of Wren."""

    def __init__(self, initial_state: WrenState = WrenState.IDLE):
        self._state = initial_state

    @property
    def state(self) -> WrenState:
        return self._state

    def transition(self, next_state: WrenState) -> WrenState:
        allowed = VALID_TRANSITIONS.get(self._state, set())
        if next_state not in allowed:
            allowed_names = [s.value for s in allowed]
            raise StateTransitionError(
                f"Cannot transition from {self._state.value} to {next_state.value}. Allowed: {allowed_names}"
            )
        self._state = next_state
        return self._state

    def cancel(self) -> WrenState:
        """Cancel any active operation and safely return to IDLE."""
        self._state = WrenState.IDLE
        return self._state

    def error(self, message: str) -> WrenState:
        """Move to ERROR state on unexpected failures."""
        self._state = WrenState.ERROR
        return self._state
