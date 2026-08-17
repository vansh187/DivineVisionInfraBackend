import os
import logging
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GENERATE_MODEL = "gemini-3.6-flash"
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 768


class GeminiError(Exception):
    """Raised on any Gemini transport/API failure - callers fall back to Groq on this."""


def _to_schema(json_schema: dict) -> types.Schema:
    properties = {
        name: types.Schema(type=prop.get("type", "STRING").upper(), description=prop.get("description"))
        for name, prop in (json_schema.get("properties") or {}).items()
    }
    return types.Schema(type="OBJECT", properties=properties, required=json_schema.get("required") or [])


class llmGemini:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable must be set")
        # The Gemini SDK rejects a deadline shorter than 10s outright, so the floor is enforced here.
        timeout_ms = int(max(10.0, float(os.getenv("GEMINI_TIMEOUT_SECONDS", "10"))) * 1000)
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))

    def generate(self, system_instruction: str, messages: list, tools: list = None):
        """
        messages: list of {"role": "user"|"model", "text": str} OR tool-result turns
                  {"role": "user", "function_response": {"name": str, "response": dict}}
        tools: list of {"name", "description", "parameters": <json-schema dict>}
        Returns {"text": str|None, "function_call": {"name": str, "args": dict}|None}
        """
        # The whole method - request building, the API call, and response parsing - is one
        # try/except: a malformed response shape (e.g. `.text` raising on a mixed-part
        # candidate, a known google-genai quirk) must trigger the same Gemini->Groq fallback
        # as a transport failure, not crash the request. Callers only ever need to handle
        # GeminiError, never a raw SDK/parsing exception.
        try:
            contents = []
            for m in messages:
                if "function_response" in m:
                    contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                        name=m["function_response"]["name"], response=m["function_response"]["response"]
                    )]))
                elif m.get("function_call"):
                    # Gemini requires the exact thought_signature from its own prior response to
                    # be replayed back on this function_call part, or the next call is rejected.
                    contents.append(types.Content(role="model", parts=[types.Part(
                        function_call=types.FunctionCall(name=m["function_call"]["name"], args=m["function_call"]["args"]),
                        thought_signature=m["function_call"].get("thought_signature"),
                    )]))
                else:
                    contents.append(types.Content(role=m["role"], parts=[types.Part(text=m["text"])]))

            config_kwargs = {"system_instruction": system_instruction}
            if tools:
                declarations = [
                    types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=_to_schema(t["parameters"]))
                    for t in tools
                ]
                config_kwargs["tools"] = [types.Tool(function_declarations=declarations)]

            response = self._client.models.generate_content(
                model=GENERATE_MODEL, contents=contents, config=types.GenerateContentConfig(**config_kwargs),
            )

            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                return {"text": (response.text or ""), "function_call": None}

            for part in candidate.content.parts:
                if part.function_call:
                    return {"text": None, "function_call": {
                        "name": part.function_call.name, "args": dict(part.function_call.args or {}),
                        "thought_signature": part.thought_signature,
                    }}
            return {"text": response.text or "", "function_call": None}
        except Exception as e:
            logger.warning("gemini_generate_failed: %s", e)
            raise GeminiError(str(e)) from e

    def embed(self, text: str) -> list:
        try:
            result = self._client.models.embed_content(
                model=EMBED_MODEL, contents=text,
                config=types.EmbedContentConfig(output_dimensionality=EMBED_DIMENSIONS),
            )
            return list(result.embeddings[0].values)
        except Exception as e:
            logger.warning("gemini_embed_failed: %s", e)
            raise GeminiError(str(e)) from e
