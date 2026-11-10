"""PII detection and masking with Microsoft Presidio (lazy, so tests and mode=off need no model)."""
from typing import Protocol


class PiiScanner(Protocol):
    def scan(self, text: str) -> list[str]: ...
    def mask(self, text: str) -> str: ...


class NullScanner:
    def scan(self, text: str) -> list[str]:
        return []

    def mask(self, text: str) -> str:
        return text


class PresidioScanner:
    ENTITIES = [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE",
        "US_SSN", "IP_ADDRESS", "LOCATION",
    ]

    def __init__(self, language: str = "en", threshold: float = 0.5):
        self.language = language
        self.threshold = threshold
        self._analyzer = None

    def _get(self):
        if self._analyzer is None:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            # Small spaCy model: run `python -m spacy download en_core_web_sm` once.
            nlp = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
                }
            ).create_engine()
            self._analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
        return self._analyzer

    def _results(self, text: str):
        res = self._get().analyze(
            text=text, language=self.language, entities=self.ENTITIES, score_threshold=self.threshold
        )
        return sorted(res, key=lambda r: r.start)

    def scan(self, text: str) -> list[str]:
        return sorted({r.entity_type for r in self._results(text)})

    def mask(self, text: str) -> str:
        out, last = [], 0
        for r in self._results(text):
            if r.start < last:
                continue
            out.append(text[last : r.start])
            out.append(f"<{r.entity_type}>")
            last = r.end
        out.append(text[last:])
        return "".join(out)
