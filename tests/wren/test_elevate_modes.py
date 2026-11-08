"""Tests for Wren Prompt Elevator Multi-Mode Engine (Elevate, Agent Directive, Brainstorm)."""
from unittest.mock import MagicMock, patch

from wren.llm.elevate import ElevationResult, PromptElevator


def test_elevate_default_mode():
    elevator = PromptElevator()
    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Polished text."}}]
        }
        mock_post.return_value = mock_resp

        res = elevator.elevate("rough text", mode="elevate")
        assert isinstance(res, ElevationResult)
        assert res.elevated_text == "Polished text."


def test_elevate_agent_mode():
    elevator = PromptElevator()
    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {"content": "# Agent Directive: Blog Optimization\n\n## 1. Goal & Intent"}
            }]
        }
        mock_post.return_value = mock_resp

        res = elevator.elevate("optimize blog build", mode="agent")
        assert isinstance(res, ElevationResult)
        assert "# Agent Directive" in res.elevated_text
        assert "Goal & Intent" in res.elevated_text


def test_elevate_brainstorm_mode():
    elevator = PromptElevator()
    with patch("httpx.Client.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "**Validated:** Great idea.\n**2 Key Trade-offs:**"}}]
        }
        mock_post.return_value = mock_resp

        res = elevator.elevate("new med spa calculator", mode="brainstorm")
        assert isinstance(res, ElevationResult)
        assert "Validated" in res.elevated_text
        assert "Trade-offs" in res.elevated_text
