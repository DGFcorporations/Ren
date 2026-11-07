"""Wren Draft Buffer, Utterance records, and Approval tracking."""
import hashlib
import time
import uuid
from dataclasses import dataclass, field


class ApprovalExpiredError(ValueError):
    """Raised when an approval has expired."""
    pass


class ApprovalInvalidatedError(ValueError):
    """Raised when an approval has been invalidated by a newer revision."""
    pass


@dataclass
class Utterance:
    id: str
    started_at: float
    ended_at: float
    source: str  # desktop | android
    stt_provider: str
    raw_text: str
    audio_retained: bool = False


@dataclass
class Draft:
    id: str
    utterance_ids: list[str]
    revision: int
    text: str
    text_sha256: str
    parent_revision: int | None
    op: str  # transcribe | elevate | revise | revert
    instruction: str
    intent_flag: bool = False
    model: str = "local"
    created_at: float = field(default_factory=time.time)


@dataclass
class Approval:
    id: str
    action: str  # copy | paste | click | navigate
    draft_id: str
    revision: int
    text_sha256: str
    target_hash: str
    expires_at: float
    approved_by: str = "voice"  # voice | click
    consumed: bool = False
    invalidated: bool = False


@dataclass
class Receipt:
    action_id: str
    outcome: str  # done | failed | uncertain
    evidence: str
    at: float = field(default_factory=time.time)


class DraftBuffer:
    """Manages draft versions, diffs, revisions, and approval lifecycles."""

    def __init__(self):
        self.utterances: dict[str, Utterance] = {}
        self.drafts: list[Draft] = []
        self.approvals: dict[str, Approval] = {}
        self.receipts: list[Receipt] = []

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def current_draft(self) -> Draft | None:
        return self.drafts[-1] if self.drafts else None

    @property
    def current_revision(self) -> int:
        return len(self.drafts)

    def record_utterance(
        self,
        raw_text: str,
        source: str = "desktop",
        stt_provider: str = "faster-whisper",
        audio_retained: bool = False,
    ) -> Utterance:
        u_id = uuid.uuid4().hex
        now = time.time()
        u = Utterance(
            id=u_id,
            started_at=now,
            ended_at=now,
            source=source,
            stt_provider=stt_provider,
            raw_text=raw_text,
            audio_retained=audio_retained,
        )
        self.utterances[u_id] = u
        return u

    def create_draft_from_utterance(
        self,
        utterance_id: str,
        text: str,
        op: str = "transcribe",
        model: str = "local",
    ) -> Draft:
        revision = 1
        d_id = uuid.uuid4().hex
        draft = Draft(
            id=d_id,
            utterance_ids=[utterance_id],
            revision=revision,
            text=text,
            text_sha256=self.hash_text(text),
            parent_revision=None,
            op=op,
            instruction="",
            model=model,
        )
        self.drafts.append(draft)
        self._invalidate_outstanding_approvals(new_revision=revision)
        return draft

    def revise_draft(
        self,
        new_text: str,
        op: str = "revise",
        instruction: str = "",
        utterance_id: str | None = None,
        model: str = "local",
    ) -> Draft:
        parent_rev = self.current_revision
        revision = parent_rev + 1
        d_id = uuid.uuid4().hex
        u_ids = [utterance_id] if utterance_id else []
        if self.current_draft:
            u_ids = self.current_draft.utterance_ids + u_ids

        draft = Draft(
            id=d_id,
            utterance_ids=u_ids,
            revision=revision,
            text=new_text,
            text_sha256=self.hash_text(new_text),
            parent_revision=parent_rev if parent_rev > 0 else None,
            op=op,
            instruction=instruction,
            model=model,
        )
        self.drafts.append(draft)
        self._invalidate_outstanding_approvals(new_revision=revision)
        return draft

    def revert_to_raw(self) -> Draft | None:
        """Revert the draft directly to the raw text of the first utterance."""
        if not self.drafts or not self.current_draft.utterance_ids:
            return None
        first_uid = self.current_draft.utterance_ids[0]
        first_utterance = self.utterances.get(first_uid)
        if not first_utterance:
            return None
        return self.revise_draft(
            first_utterance.raw_text,
            op="revert",
            instruction="Revert to raw transcription",
        )

    def create_approval(
        self,
        action: str,
        target_hash: str,
        ttl_seconds: float = 120.0,
        approved_by: str = "voice",
    ) -> Approval:
        if not self.current_draft:
            raise ValueError("No active draft to approve")
        app_id = uuid.uuid4().hex
        approval = Approval(
            id=app_id,
            action=action,
            draft_id=self.current_draft.id,
            revision=self.current_draft.revision,
            text_sha256=self.current_draft.text_sha256,
            target_hash=target_hash,
            expires_at=time.time() + ttl_seconds,
            approved_by=approved_by,
            consumed=False,
        )
        self.approvals[app_id] = approval
        return approval

    def consume_approval(self, approval_id: str, current_target_hash: str) -> Approval:
        approval = self.approvals.get(approval_id)
        if not approval:
            raise ValueError(f"Approval {approval_id} not found")
        if approval.invalidated or (self.current_draft and approval.revision != self.current_draft.revision):
            msg = (
                f"Approval for revision {approval.revision} invalidated "
                f"by current revision {self.current_revision}"
            )
            raise ApprovalInvalidatedError(msg)
        if approval.consumed:
            raise ValueError(f"Approval {approval_id} already consumed")
        if time.time() > approval.expires_at:
            raise ApprovalExpiredError(f"Approval {approval_id} has expired")
        if approval.target_hash != current_target_hash:
            raise ValueError(
                f"Target mismatch: approved for {approval.target_hash}, current is {current_target_hash}"
            )
        approval.consumed = True
        return approval

    def record_receipt(self, action_id: str, outcome: str, evidence: str) -> Receipt:
        r = Receipt(action_id=action_id, outcome=outcome, evidence=evidence)
        self.receipts.append(r)
        return r

    def _invalidate_outstanding_approvals(self, new_revision: int):
        # Outstanding approvals tied to older revisions are rendered obsolete
        for app in self.approvals.values():
            if app.revision < new_revision:
                app.invalidated = True
