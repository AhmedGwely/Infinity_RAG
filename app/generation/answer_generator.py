"""Answer generation with explicit evidence insufficiency handling."""

from app.core.schemas import SearchHit
from app.generation.llm_interface import LLMProvider, SystemInstructionProvider
from app.generation.prompt_templates import (
    SYSTEM_INSTRUCTIONS,
    append_validated_citations,
    build_answer_prompt,
)


NOT_FOUND_ANSWER = "I could not find this information in the indexed documents."


class AnswerGenerator:
    """Generate cited answers from retrieved evidence using a replaceable LLM."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def answer(self, question: str, hits: list[SearchHit]) -> str:
        """Return a grounded answer or a not-found response when evidence is empty."""
        if not hits:
            return NOT_FOUND_ANSWER
        prompt = build_answer_prompt(question, hits)
        if isinstance(self._provider, SystemInstructionProvider):
            answer = self._provider.complete_with_instructions(
                prompt,
                system_instruction=SYSTEM_INSTRUCTIONS,
            )
        else:
            answer = self._provider.complete(f"{SYSTEM_INSTRUCTIONS}\n\n{prompt}")
        if answer.strip() == NOT_FOUND_ANSWER:
            return answer.strip()
        return append_validated_citations(answer, hits)
