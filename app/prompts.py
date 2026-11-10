"""Versioned prompts.

Each version is a file `prompts/<name>/v<N>.yaml`; `config.yaml` (`prompts:`) picks the active one.
A prompt's ID, e.g. `answer@v2#1a2b3c4d`, is recorded in traces, the audit log, eval runs and chunk
payloads, so every answer, caption and eval score can be traced back to the exact prompt text.
"""
import hashlib
import string
from dataclasses import dataclass
from pathlib import Path

import yaml

PROMPT_NAMES = ("answer", "citation_retry", "rerank", "caption")


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    template: str
    variables: tuple[str, ...] = ()
    changelog: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()

    @property
    def id(self) -> str:
        """Version plus content hash: an edit without a version bump still changes the ID."""
        return f"{self.name}@v{self.version}#{self.sha256[:8]}"

    def render(self, **values) -> str:
        given, wanted = set(values), set(self.variables)
        if given != wanted:
            raise PromptError(
                f"{self.id}: expected variables {sorted(wanted)}, got {sorted(given)}"
            )
        return self.template.format(**values) if wanted else self.template


def _placeholders(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}


def load_prompt(prompts_dir: str | Path, name: str, version: int) -> Prompt:
    path = Path(prompts_dir) / name / f"v{version}.yaml"
    if not path.is_file():
        raise PromptError(f"Prompt file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("name") != name or raw.get("version") != version:
        raise PromptError(f"{path}: name/version inside the file must be {name!r}/{version}")
    template = raw.get("template")
    if not isinstance(template, str) or not template.strip():
        raise PromptError(f"{path}: 'template' is missing or empty")
    variables = tuple(raw.get("variables") or ())
    try:
        found = _placeholders(template)
    except ValueError as e:  # e.g. a single '{' in the text
        raise PromptError(f"{path}: bad template ({e}); write literal braces as '{{{{' and '}}}}'") from e
    if found != set(variables):
        raise PromptError(
            f"{path}: template placeholders {sorted(found)} do not match variables {sorted(variables)}"
        )
    return Prompt(name, version, template, variables, str(raw.get("changelog") or ""))


class PromptSet:
    """The active version of every prompt, loaded once and validated together."""

    def __init__(self, prompts: dict[str, Prompt]):
        missing = set(PROMPT_NAMES) - set(prompts)
        if missing:
            raise PromptError(f"Missing prompts: {sorted(missing)}")
        self._prompts = prompts

    def __getitem__(self, name: str) -> Prompt:
        return self._prompts[name]

    @property
    def ids(self) -> dict[str, str]:
        return {n: p.id for n, p in sorted(self._prompts.items())}


def load_prompts(cfg) -> PromptSet:
    p = cfg.prompts
    return PromptSet({n: load_prompt(p.dir, n, getattr(p, n)) for n in PROMPT_NAMES})
