"""Plain-Python guardrails driven by config/guardrails.yaml. Fail closed on any error."""
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.settings import get_settings


class Policy(BaseModel):
    max_question_chars: int = 2000
    daily_request_limit: int = 200
    not_found_message: str = "I could not find this in the documents."
    refusal_message: str = "I can't give a reliable, sourced answer to that."
    injection_patterns: list[str] = []


class GuardrailBlocked(Exception):
    def __init__(self, message: str, code: str = "blocked"):
        super().__init__(message)
        self.message, self.code = message, code


@lru_cache
def get_policy() -> Policy:
    path = Path(get_settings().config_path).with_name("guardrails.yaml")
    return Policy(**yaml.safe_load(path.read_text(encoding="utf-8")))


class Guard:
    def __init__(self, policy: Policy):
        self.policy = policy
        self._patterns = [re.compile(p, re.IGNORECASE) for p in policy.injection_patterns]

    def check_input(self, question: str) -> str:
        try:
            q = (question or "").strip()
            if not q:
                raise GuardrailBlocked("Question is empty", "empty")
            if len(q) > self.policy.max_question_chars:
                raise GuardrailBlocked(
                    f"Question is longer than {self.policy.max_question_chars} characters", "too_long"
                )
            if any(p.search(q) for p in self._patterns):
                raise GuardrailBlocked(
                    "The question looks like an attempt to change the assistant's instructions",
                    "injection",
                )
            return q
        except GuardrailBlocked:
            raise
        except Exception as e:  # fail closed
            raise GuardrailBlocked("Input check failed", "guard_error") from e

    def sanitize_context(self, text: str) -> str:
        """Retrieved text is data. Neutralise lines that look like instructions to the model."""
        out = []
        for line in text.splitlines():
            if any(p.search(line) for p in self._patterns):
                out.append("[removed: suspicious instruction in document]")
            else:
                out.append(line)
        return "\n".join(out)

    _cite = re.compile(r"\[S(\d+)\]")

    def check_output(self, answer: str, n_sources: int) -> tuple[bool, set[int]]:
        """Every citation must map to a retrieved source and a factual answer needs at least one."""
        try:
            if self.is_not_found(answer):
                return True, set()
            cited = {int(x) for x in self._cite.findall(answer)}
            if not cited or any(c < 1 or c > n_sources for c in cited):
                return False, cited
            return True, cited
        except Exception:
            return False, set()

    def is_not_found(self, answer: str) -> bool:
        norm = lambda s: re.sub(r"[\s.]+$", "", s.strip().lower())  # noqa: E731
        return norm(answer) == norm(self.policy.not_found_message)
