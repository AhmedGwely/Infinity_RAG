"""Provider-independent LLM completion contract."""

from typing import Protocol, runtime_checkable


class LLMProvider(Protocol):
    """Interface implemented by cloud and local language model adapters."""

    def complete(self, prompt: str) -> str:
        """Generate text from a prompt using the configured provider."""
        ...


@runtime_checkable
class SystemInstructionProvider(Protocol):
    """Optional provider capability for separating policy from user-supplied input."""

    def complete_with_instructions(
        self,
        prompt: str,
        *,
        system_instruction: str,
    ) -> str:
        """Generate with separate trusted instructions and untrusted input."""
        ...
