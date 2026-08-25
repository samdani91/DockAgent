"""LLMClient protocol + provider implementations (Gemini, OpenAI)."""

import logging
import time
from typing import Protocol, runtime_checkable

log = logging.getLogger("dockagent.llm")


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str:
        """Send a system + user turn; return the assistant reply text."""
        ...


class GeminiClient:
    """Thin wrapper around the Google Gemini API (google-genai SDK).

    Reads GEMINI_API_KEY from the environment.
    """

    def __init__(self, model: str | None = None) -> None:
        from google import genai  # deferred import

        import os
        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self._model = model or os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

    def complete(self, system: str, user: str) -> str:
        from google.genai import types

        log.debug("%s ← prompt %d chars", self._model, len(user))
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=self._model,
                config=types.GenerateContentConfig(system_instruction=system),
                contents=user,
            )
        except Exception as exc:
            log.error("%s failed after %.1fs: %s",
                      self._model, time.monotonic() - started, exc)
            raise
        text = response.text or ""
        log.info("%s replied %s chars in %.1fs",
                 self._model, f"{len(text):,}", time.monotonic() - started)
        return text


class OpenAIClient:
    """Thin wrapper around the OpenAI chat-completions API.

    Reads OPENAI_API_KEY from the environment.
    """

    def __init__(self, model: str = "gpt-4o") -> None:
        import openai  # deferred import

        self._client = openai.OpenAI()
        self._model = model

    def complete(self, system: str, user: str) -> str:
        log.debug("%s ← prompt %d chars", self._model, len(user))
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            log.error("%s failed after %.1fs: %s",
                      self._model, time.monotonic() - started, exc)
            raise
        text = response.choices[0].message.content or ""
        log.info("%s replied %s chars in %.1fs",
                 self._model, f"{len(text):,}", time.monotonic() - started)
        return text
