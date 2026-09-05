import os
import json
import logging
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CHAT_MODEL = "openai/gpt-oss-120b"
TRANSCRIBE_MODEL = "whisper-large-v3-turbo"


class GroqError(Exception):
    """Raised on any Groq transport/API failure."""


def _recover_tool_call_from_error(exc: Exception):
    """gpt-oss frequently emits `null` for optional tool args it doesn't know; Groq
    then 400s the whole call with code `tool_use_failed` - but hands back the
    model's intended call in `error.failed_generation`. Salvage it: drop the
    null-valued args (every tool handler already defaults a missing arg) and use
    the call, instead of dead-ending the turn. Returns {"name","args"} or None."""
    try:
        body = getattr(exc, "body", None)
        if not isinstance(body, dict):
            return None
        err = body.get("error") or {}
        if err.get("code") != "tool_use_failed" or not err.get("failed_generation"):
            return None
        parsed = json.loads(err["failed_generation"])
        name = parsed.get("name")
        args = parsed.get("arguments")
        if isinstance(args, str):
            args = json.loads(args)
        if not name or not isinstance(args, dict):
            return None
        return {"name": name, "args": {k: v for k, v in args.items() if v is not None}}
    except Exception:
        return None


class llmGroq:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY environment variable must be set")
        self._client = Groq(api_key=api_key)
        # openai/gpt-oss-120b is a reasoning model - even with reasoning_format="hidden" it
        # still spends real wall-clock time thinking before the hidden reasoning is stripped,
        # so 6s was too tight once a turn chains more than one Groq call (a tool round plus a
        # second round for the final answer, or the guardrail judge call on top of that).
        self._timeout = float(os.getenv("GROQ_TIMEOUT_SECONDS", "15"))

    def _to_openai_tools(self, tools: list) -> list:
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools
        ]

    def generate(self, system_instruction: str, messages: list, tools: list = None):
        """
        messages: list of {"role": "user"|"assistant"|"tool", "content": str, "tool_call_id"?: str, "name"?: str}
        Returns {"text": str|None, "function_call": {"name": str, "args": dict}|None}
        """
        # Request building, the API call, and response parsing are one try/except - an
        # unexpected response shape must surface as GroqError like a transport failure would,
        # not crash the request. Callers only ever need to handle GroqError.
        try:
            chat_messages = [{"role": "system", "content": system_instruction}] + messages
            # openai/gpt-oss-120b is a reasoning model - without reasoning_format="hidden", its
            # chain-of-thought can bleed into `content` instead of staying in the separate
            # `.reasoning` field (observed in production: raw internal deliberation text got
            # sent to a visitor). "hidden" guarantees `content` is the final answer only.
            kwargs = {"model": CHAT_MODEL, "messages": chat_messages, "timeout": self._timeout, "reasoning_format": "hidden"}
            if tools:
                kwargs["tools"] = self._to_openai_tools(tools)
                kwargs["tool_choice"] = "auto"
            resp = self._client.chat.completions.create(**kwargs)

            choice_message = resp.choices[0].message
            if choice_message.tool_calls:
                call = choice_message.tool_calls[0]
                try:
                    args = json.loads(call.function.arguments)
                except (TypeError, ValueError):
                    args = {}
                return {"text": None, "function_call": {"name": call.function.name, "args": args}}
            return {"text": choice_message.content or "", "function_call": None}
        except Exception as e:
            recovered = _recover_tool_call_from_error(e)
            if recovered is not None:
                logger.warning("groq_tool_call_recovered name=%s (dropped null args)", recovered["name"])
                return {"text": None, "function_call": recovered}
            logger.warning("groq_generate_failed: %s", e)
            raise GroqError(str(e)) from e

    def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav") -> str:
        try:
            result = self._client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL, file=(filename, audio_bytes),
                response_format="text", timeout=self._timeout,
            )
        except Exception as e:
            logger.warning("groq_transcribe_failed: %s", e)
            raise GroqError(str(e)) from e
        return str(result).strip()

    def generate_json(self, system_instruction: str, user_content: str) -> dict:
        """Generic structured-output call: returns the parsed JSON object the model produces."""
        try:
            resp = self._client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": user_content}],
                response_format={"type": "json_object"},
                reasoning_format="hidden",
                timeout=self._timeout,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("groq_generate_json_failed: %s", e)
            raise GroqError(str(e)) from e

    def judge(self, answer: str, context_chunks: list) -> float:
        """Guardrail: how well is `answer` supported by `context_chunks`? Returns a 0-1 score."""
        context_text = "\n---\n".join(context_chunks) if context_chunks else "(no context provided)"
        try:
            resp = self._client.chat.completions.create(
                model=CHAT_MODEL,
                messages=[
                    {"role": "system", "content": (
                        "You are a strict factual-consistency judge. Given a CONTEXT and an ANSWER, "
                        "return ONLY a JSON object {\"score\": <float 0.0-1.0>} - how well every "
                        "factual claim in ANSWER is directly supported by CONTEXT. 1.0 = fully "
                        "supported, 0.0 = unsupported/contradicted/hallucinated."
                    )},
                    {"role": "user", "content": f"CONTEXT:\n{context_text}\n\nANSWER:\n{answer}"},
                ],
                response_format={"type": "json_object"},
                reasoning_format="hidden",
                timeout=self._timeout,
            )
            data = json.loads(resp.choices[0].message.content)
            return max(0.0, min(1.0, float(data.get("score", 0.0))))
        except Exception as e:
            logger.warning("groq_judge_failed: %s", e)
            raise GroqError(str(e)) from e
