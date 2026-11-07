"""Tests for Wren LLM prompt elevation, diff calculation, and intent preservation."""
from unittest.mock import MagicMock, patch

from wren.llm.elevate import PromptElevator


def test_compute_diff():
    elevator = PromptElevator()
    raw = "meeting at 3pm on friday with dr smith"
    elevated = "Meeting at 3:00 PM on Friday with Dr. Smith."
    diff = elevator.compute_diff(raw, elevated)
    assert len(diff) > 0
    assert "Dr. Smith" in diff or "Smith" in diff


def test_intent_preservation_entity_check():
    elevator = PromptElevator()
    raw = "Call Bob at 555-1234 regarding invoice #9872"
    
    # Good elevation: preserves Bob, 555-1234, 9872
    good_elevated = "Please call Bob at 555-1234 regarding invoice #9872."
    assert elevator.verify_intent(raw, good_elevated) is True

    # Bad elevation: dropped the invoice number or changed Bob to Alice
    bad_elevated = "Please call Alice about the latest invoice."
    assert elevator.verify_intent(raw, bad_elevated) is False


def test_elevate_call_local_router():
    elevator = PromptElevator(base_url="http://127.0.0.1:18880/v1")
    raw_text = "hey can you send the report to sarah by tomorrow morning"
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Please send the report to Sarah by tomorrow morning."
                }
            }
        ]
    }
    
    with patch("httpx.Client.post", return_value=mock_response):
        res = elevator.elevate(raw_text)
        assert res.raw_text == raw_text
        assert res.elevated_text == "Please send the report to Sarah by tomorrow morning."
        assert res.intent_preserved is True
        assert res.model == "auto"


def test_elevate_fallback_on_network_error():
    elevator = PromptElevator(base_url="http://127.0.0.1:18880/v1")
    raw_text = "schedule deployment review"
    
    # Simulate connection error
    with patch("httpx.Client.post", side_effect=Exception("Connection refused")):
        res = elevator.elevate(raw_text)
        # Should gracefully return normalized raw text, not crash!
        assert res.raw_text == raw_text
        assert "schedule deployment review" in res.elevated_text.lower()
        assert res.fallback_used is True
