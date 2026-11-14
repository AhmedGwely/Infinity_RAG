"""Concrete LLM adapters and provider selection for grounded answers."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.request import Request, urlopen

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, GenerationError
from app.generation.llm_interface import LLMProvider

logger = logging.getLogger(__name__)


class GeminiProvider:
    """Google Gen AI Interactions API adapter for retrieved-context prompts."""

    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini.")
        if not model:
            raise ConfigurationError("GEMINI_MODEL must not be empty.")
        self.model = model
        self._client = client if client is not None else self._create_client(api_key)

    @staticmethod
    def _create_client(api_key: str):
        """Create the official Google Gen AI client only when Gemini is selected."""
        try:
            from google import genai
        except ImportError as error:
            raise ConfigurationError(
                "Gemini requires the google-genai package. Install project requirements first."
            ) from error
        return genai.Client(api_key=api_key)

    def complete(self, prompt: str) -> str:
        """Generate text without persisting the interaction server-side."""
        return self._complete(prompt)

    def complete_with_instructions(
        self,
        prompt: str,
        *,
        system_instruction: str,
    ) -> str:
        """Generate with trusted system policy separate from the input prompt."""
        if not system_instruction.strip():
            raise ValueError("system_instruction must not be empty")
        return self._complete(prompt, system_instruction=system_instruction)

    def _complete(self, prompt: str, *, system_instruction: str | None = None) -> str:
        """Call Interactions without logging request content or credentials."""
        request: dict[str, object] = {
            "model": self.model,
            "input": prompt,
            "store": False,
        }
        if system_instruction is not None:
            request["system_instruction"] = system_instruction
        try:
            interaction = self._client.interactions.create(**request)
        except Exception as error:
            logger.error("Gemini generation request failed (%s).", type(error).__name__)
            raise GenerationError("Gemini generation request failed.") from None

        text = getattr(interaction, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise GenerationError("Gemini returned no answer text.")
        return text.strip()


class OpenAIProvider:
    """OpenAI-compatible adapter using the existing OPENAI_API_KEY setting."""

    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        if not api_key:
            raise ConfigurationError("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")
        self.model = model
        self._client = client if client is not None else self._create_client(api_key)

    @staticmethod
    def _create_client(api_key: str):
        try:
            from openai import OpenAI
        except ImportError as error:
            raise ConfigurationError(
                "OpenAI requires the openai package. Install project requirements first."
            ) from error
        return OpenAI(api_key=api_key)

    def complete(self, prompt: str) -> str:
        """Generate from the exact grounded prompt supplied by AnswerGenerator."""
        return self._chat([{"role": "user", "content": prompt}])

    def complete_with_instructions(self, prompt: str, *, system_instruction: str) -> str:
        """Generate with trusted policy in the system role, separate from untrusted input."""
        if not system_instruction.strip():
            raise ValueError("system_instruction must not be empty")
        return self._chat(
            [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ]
        )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
        except Exception as error:
            logger.error("OpenAI generation request failed (%s).", type(error).__name__)
            raise GenerationError("OpenAI generation request failed.") from None

        text = response.choices[0].message.content if response.choices else None
        if not isinstance(text, str) or not text.strip():
            raise GenerationError("OpenAI returned no answer text.")
        return text.strip()


class OllamaProvider:
    """Local Ollama adapter using its standard non-streaming generation endpoint."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        if not base_url:
            raise ConfigurationError("OLLAMA_BASE_URL must not be empty.")
        if not model:
            raise ConfigurationError("LLM_MODEL must not be empty for Ollama.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        """Send the composed grounded prompt to the configured local Ollama host."""
        return self._generate(prompt, None)

    def complete_with_instructions(self, prompt: str, *, system_instruction: str) -> str:
        """Use Ollama's separate system field for trusted policy text."""
        if not system_instruction.strip():
            raise ValueError("system_instruction must not be empty")
        return self._generate(prompt, system_instruction)

    def _generate(self, prompt: str, system: str | None) -> str:
        payload: dict[str, object] = {"model": self.model, "prompt": prompt, "stream": False}
        if system is not None:
            payload["system"] = system
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            logger.error("Ollama generation request failed (%s).", type(error).__name__)
            raise GenerationError("Ollama generation request failed.") from None

        text = payload.get("response")
        if not isinstance(text, str) or not text.strip():
            raise GenerationError("Ollama returned no answer text.")
        return text.strip()


def create_llm_provider(
    settings: Settings,
    *,
    gemini_client: Any | None = None,
    openai_client: Any | None = None,
) -> LLMProvider:
    """Select a provider while leaving existing Ollama/OpenAI settings supported."""
    provider = settings.llm_provider.strip().lower()
    if provider == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key or "",
            model=settings.gemini_model,
            client=gemini_client,
        )
    if provider == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key or "",
            model=settings.llm_model,
            client=openai_client,
        )
    if provider == "ollama":
        return OllamaProvider(base_url=settings.ollama_base_url, model=settings.llm_model)
    raise ConfigurationError(
        f"Unsupported LLM_PROVIDER {settings.llm_provider!r}; choose gemini, openai, or ollama."
    )
