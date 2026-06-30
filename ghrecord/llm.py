"""Optional Claude fallback for ambiguous governance prose.

Gated: constructed only when ``anthropic`` is installed and an API key is set,
and called only after the heuristic pass flags a match as ambiguous. Results are
cached so re-runs cost nothing.
"""

import json
import os

from .cache import Cache

DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # cheap tier is plenty for extraction

_SYSTEM = (
    "You decide whether a specific person holds an elevated governance role "
    "(steering council, technical committee, or maintainer/lead) in a software "
    "project, based on an excerpt of its governance document. Answer ONLY with "
    "the requested JSON. Do not count plain contributors as a role."
)


class LLM:
    def __init__(
        self,
        cache: Cache,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        import anthropic  # raises ImportError if the extra isn't installed

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=key)
        self.cache = cache
        self.model = model

    @classmethod
    def maybe(cls, cache: Cache, *, enabled: bool) -> "LLM | None":
        """Return an LLM if possible, else None (never raises)."""
        if not enabled:
            return None
        try:
            return cls(cache)
        except Exception:
            return None

    def classify_governance_role(
        self, *, text: str, names: list[str], logins: list[str]
    ) -> dict | None:
        excerpt = text[:6000]
        ck = Cache.key("llm-gov", self.model, excerpt, names, logins)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached

        person = ", ".join(filter(None, [*names, *(f"@{h}" for h in logins)]))
        prompt = (
            f"Person (any of these identities): {person}\n\n"
            f"Governance excerpt:\n---\n{excerpt}\n---\n\n"
            'Reply with JSON: {"has_role": bool, '
            '"role": "steering_council"|"maintainer"|null, '
            '"confidence": 0..1}.'
        )
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=200,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(
                block.text for block in resp.content if block.type == "text"
            )
            result = self._parse_json(raw)
        except Exception:
            result = None
        self.cache.set(ck, result)
        return result

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        raw = raw.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
