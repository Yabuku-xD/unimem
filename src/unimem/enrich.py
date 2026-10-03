"""Write-time enrichment with a small local instruct model.

Enrichment rewrites a stored memory into standalone facts, the questions it
answers, and related keywords. The text is indexed next to the memory so later
keyword recall can match paraphrases and multi-hop questions. The model runs
only inside an explicit enrichment pass, never on the recall path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .policy import contains_secret

DEFAULT_ENRICH_MODEL = "mlx-community/LFM2.5-1.2B-Instruct-4bit"
# Measured on LoCoMo: 16-way batches were 2.5x faster than sequential calls
# with unchanged peak memory (~0.83 GB) on Apple Silicon.
DEFAULT_BATCH_SIZE = 16
MAX_ENRICHMENT_TOKENS = 90
MAX_ENRICHMENT_CHARS = 1500

PROMPT = """Date: {date}
{date_hints}Earlier: {context}
Message: {message}

Rewrite the Message as short standalone facts using full names (no pronouns) and absolute dates. Then add one line "Answers:" with 2 questions it answers, and one line "Keywords:" with related terms and synonyms. Be brief."""


class EnrichUnavailableError(RuntimeError):
    """Raised when the optional local enrichment runtime is unavailable."""


@dataclass(frozen=True)
class EnrichmentInput:
    message: str
    context: tuple[str, ...] = ()
    observed_on: date | None = None


def relative_date_hints(observed_on: date | None) -> str:
    """Resolve common relative dates in code; small models get these wrong."""
    if observed_on is None:
        return ""
    yesterday = observed_on - timedelta(days=1)
    last_week = observed_on - timedelta(days=7)
    last_month = observed_on.replace(day=1) - timedelta(days=1)
    return (
        f"Reference dates: today={observed_on.isoformat()}, "
        f"yesterday={yesterday.isoformat()}, "
        f"last week=week of {last_week.isoformat()}, "
        f"last month={last_month.strftime('%B %Y')}, "
        f"last year={observed_on.year - 1}, next year={observed_on.year + 1}.\n"
    )


def build_prompt(item: EnrichmentInput) -> str:
    return PROMPT.format(
        date=item.observed_on.isoformat() if item.observed_on else "unknown",
        date_hints=relative_date_hints(item.observed_on),
        context=" / ".join(part[:200] for part in item.context[-2:]) or "-",
        message=item.message[:2000],
    )


def clean_enrichment(text: str) -> str:
    """Bound the indexed text and drop output that looks like a secret."""
    cleaned = " ".join(text.replace("*", " ").split())[:MAX_ENRICHMENT_CHARS]
    return "" if contains_secret(cleaned) else cleaned


class LocalEnricher:
    """MLX-backed enricher; loads the model lazily on first use."""

    def __init__(
        self,
        model_name: str = DEFAULT_ENRICH_MODEL,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        try:
            import mlx_lm  # noqa: F401
        except ImportError as error:
            raise EnrichUnavailableError(
                "enrichment requires the 'enrich' optional dependency on Apple Silicon "
                "(uv sync --extra enrich)"
            ) from error
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        self._model = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._model is None:
            from mlx_lm import load

            self._model, self._tokenizer = load(self.model_name)

    def _prompt_tokens(self, item: EnrichmentInput) -> list[int]:
        assert self._tokenizer is not None
        return self._tokenizer.apply_chat_template(
            [{"role": "user", "content": build_prompt(item)}],
            add_generation_prompt=True,
            tokenize=True,
        )

    def enrich_many(self, items: Sequence[EnrichmentInput]) -> list[str]:
        if not items:
            return []
        self._load()
        from mlx_lm import batch_generate

        results: list[str] = []
        for start in range(0, len(items), self.batch_size):
            chunk = items[start : start + self.batch_size]
            response = batch_generate(
                self._model,
                self._tokenizer,
                [self._prompt_tokens(item) for item in chunk],
                max_tokens=MAX_ENRICHMENT_TOKENS,
            )
            results.extend(clean_enrichment(text) for text in response.texts)
        return results
