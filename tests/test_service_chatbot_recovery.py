from types import SimpleNamespace
from unittest.mock import MagicMock

from DivineService.service_chatbot import (
    serviceChatbot, SAFE_FALLBACK_REPLY, DEGRADED_FALLBACK_REPLY, INDICATIVE_GENERAL_SUFFIX,
)
from DivineService.llm_gemini import GeminiError
from DivineService.llm_groq import GroqError


def _msg(role, content):
    return SimpleNamespace(role=role, content=content)


def _kb_row(content, similarity):
    return SimpleNamespace(content=content, similarity=similarity, title="Brochure")


def _inv_row(project_name="OPS Divine Greens", area_sqyd=131.43):
    return SimpleNamespace(
        project_name=project_name, city="Karnal", unit_type="plot",
        area_sqyd=area_sqyd, area_sqmt=109.9, width_mtr=9.14, length_mtr=12.03,
        unit_count=12, available_count=5,
    )


class FakeInventory:
    def __init__(self, rows):
        self._rows = rows

    def distinct_plot_sizes(self, project_name=None, unit_type=None):
        return list(self._rows)


def _session():
    return SimpleNamespace(id="s1", lead_id="l1", loan_payload=None)


def _service(gemini, groq, kb_side_effect, inventory_rows=None):
    persistence = MagicMock()
    persistence.list_recent_messages.return_value = [
        _msg("user", "hi"), _msg("assistant", "Hello! How can I help?"),
        _msg("user", "what plot sizes do you have"),
    ]
    persistence.search_kb_chunks.side_effect = kb_side_effect
    persistence.create_message.side_effect = lambda **kw: SimpleNamespace(**kw)
    svc = serviceChatbot(
        persistence=persistence, gemini=gemini, groq=groq, zoho=None,
        loan_persistence=MagicMock(),
        inventory_persistence=FakeInventory(inventory_rows or [_inv_row()]),
    )
    return svc


def test_ungrounded_draft_recovers_with_grounded_answer():
    gemini = MagicMock()
    gemini.generate.side_effect = [
        {"text": None, "function_call": {"name": "search_knowledge_base", "args": {"query": "sizes"}}},
        {"text": "Prices start at 50 lakh.", "function_call": None},
        {"text": "We offer 131 sq. yd plots at OPS Divine Greens; the team can confirm current pricing.",
         "function_call": None},
    ]
    groq = MagicMock()
    # First judge call (normal guardrail) fails it; second (recovery) passes it.
    groq.judge.side_effect = [0.1, 0.9]

    # Normal KB search finds nothing; the broadened recovery search does.
    svc = _service(gemini, groq, kb_side_effect=[[], [_kb_row("OPS Divine Greens has 131 sq. yd plots.", 0.42)]])

    result = svc._run_agent_loop(_session(), "what plot sizes do you have")

    assert result["reply"].startswith("We offer 131 sq. yd plots")
    assert result["guardrail_passed"] is True
    assert SAFE_FALLBACK_REPLY not in result["reply"]


def test_recovery_answer_that_stays_ungrounded_goes_out_tagged_as_general():
    gemini = MagicMock()
    gemini.generate.side_effect = [
        {"text": None, "function_call": {"name": "search_knowledge_base", "args": {"query": "sizes"}}},
        {"text": "Prices start at 50 lakh.", "function_call": None},
        {"text": "Plots in this area are usually between 100 and 200 sq. yd.", "function_call": None},
    ]
    groq = MagicMock()
    groq.judge.side_effect = [0.1, 0.2]  # never grounded

    svc = _service(gemini, groq, kb_side_effect=[[], []], inventory_rows=[])

    result = svc._run_agent_loop(_session(), "what plot sizes do you have")

    assert result["reply"].endswith(INDICATIVE_GENERAL_SUFFIX)
    assert result["guardrail_passed"] is False
    assert SAFE_FALLBACK_REPLY not in result["reply"]
    assert "I don't want to guess" not in result["reply"]


def test_recovery_falls_through_to_degraded_line_only_when_all_llms_are_down():
    gemini = MagicMock()
    gemini.generate.side_effect = [
        {"text": None, "function_call": {"name": "search_knowledge_base", "args": {"query": "sizes"}}},
        {"text": "Prices start at 50 lakh.", "function_call": None},
        GeminiError("gemini down"),
    ]
    groq = MagicMock()
    groq.judge.side_effect = [0.1]
    groq.generate.side_effect = GroqError("groq down")

    svc = _service(gemini, groq, kb_side_effect=[[], []], inventory_rows=[])

    result = svc._run_agent_loop(_session(), "what plot sizes do you have")

    assert result["reply"] == DEGRADED_FALLBACK_REPLY
    assert result["guardrail_passed"] is False


def test_tag_general_guidance_left_alone_when_model_already_hedged():
    svc = _service(MagicMock(), MagicMock(), kb_side_effect=[[]])
    already = "Plots vary in size — our team can share the exact list."
    assert svc._tag_general_guidance(already) == already
