"""Optional Claude fallback for ambiguous governance prose.

Gated: constructed only when ``anthropic`` is installed and an API key is set,
and called only after the heuristic pass flags a match as ambiguous. Results are
cached so re-runs cost nothing.
"""

import json
import os

from .cache import Cache

DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # cheap tier is plenty for extraction


# Subscription path: Claude Code can mint an OAuth token (`claude setup-token`)
# that the anthropic SDK accepts as a bearer auth_token instead of an API key.
_AUTH_TOKEN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")


def _env_credentials() -> tuple[str | None, str | None]:
    """Return (api_key, auth_token) from the environment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = next(
        (os.environ[v] for v in _AUTH_TOKEN_VARS if os.environ.get(v)), None
    )
    return api_key, auth_token


def availability() -> str | None:
    """Return None if the LLM is usable, else a short reason why not."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "the 'anthropic' package is not installed (pip install 'praiser[llm]')"
    api_key, auth_token = _env_credentials()
    if not (api_key or auth_token):
        return ("no Anthropic credentials (set ANTHROPIC_API_KEY, or "
                "CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)")
    return None

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
        auth_token: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        import anthropic  # raises ImportError if the extra isn't installed

        env_key, env_token = _env_credentials()
        api_key = api_key or env_key
        auth_token = auth_token or env_token
        if api_key:  # API key (pay-as-you-go) takes precedence
            self._client = anthropic.Anthropic(api_key=api_key)
        elif auth_token:  # Claude subscription via OAuth bearer token
            self._client = anthropic.Anthropic(auth_token=auth_token)
        else:
            raise RuntimeError("no Anthropic credentials")
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

    def discover_role_sources(
        self, name_with_owner: str, project_name: str | None = None
    ) -> list[dict]:
        """Use Claude + web search to find authoritative role pages for a project.

        Returns a list of {"url", "role", "label"}. Cached per project, and
        degrades to [] if web search is unavailable or nothing is found.
        """
        ck = Cache.key("llm-rolesrc", self.model, name_with_owner)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached

        desc = name_with_owner + (f" ({project_name})" if project_name else "")
        prompt = (
            f"Find the official web page(s) that list the people holding "
            f"governance roles for the open-source project {desc}: maintainers / "
            f"core team, and any steering council or technical committee. Prefer "
            f"the project's OWN website (team / governance / people / about "
            f"pages); avoid GitHub, blogs, and third-party sites. "
            'Reply with ONLY a JSON array of objects '
            '{"url": str, "role": "maintainer"|"steering_council", "label": str}. '
            "Use steering_council for steering/technical committees, maintainer "
            "otherwise. Return [] if you cannot find an authoritative page."
        )
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": 5}],
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            result = self._parse_role_sources(raw)
        except Exception:
            result = []
        self.cache.set(ck, result)
        return result

    def discover_founders(
        self, name_with_owner: str, project_name: str | None = None
    ) -> list[dict]:
        """Use Claude + web search to name a project's founder(s)/creator(s).

        Returns ``[{"name", "handle", "url"}]`` — the original author(s), their
        GitHub username if known, and a citation. Cached; degrades to [].
        """
        ck = Cache.key("llm-founders", self.model, name_with_owner)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached

        desc = name_with_owner + (f" ({project_name})" if project_name else "")
        prompt = (
            f"Who originally created or founded the open-source project {desc}? "
            "Name ONLY the original author(s) / creator(s) / co-founder(s) — NOT "
            "later maintainers, leads, or contributors. For each, give their "
            "GitHub username if you know it, and a citation URL (the project's "
            "history/about page, Wikipedia, or the founding paper). "
            'Reply with ONLY a JSON array of {"name": str, "handle": str|null, '
            '"url": str}. Return [] if you are not sure.'
        )
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": 5}],
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            result = self._parse_founders(raw)
        except Exception:
            result = []
        self.cache.set(ck, result)
        return result

    @staticmethod
    def _parse_founders(raw: str) -> list[dict]:
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            items = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            handle = it.get("handle")
            handle = handle.lstrip("@") if isinstance(handle, str) and handle else None
            url = it.get("url") if isinstance(it.get("url"), str) else None
            name = it.get("name") if isinstance(it.get("name"), str) else None
            if handle or name:
                out.append({"name": name, "handle": handle, "url": url})
        return out

    @staticmethod
    def _parse_role_sources(raw: str) -> list[dict]:
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            items = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []
        out: list[dict] = []
        for it in items if isinstance(items, list) else []:
            url = (it or {}).get("url", "")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            role = str(it.get("role", "maintainer")).lower()
            role = "steering_council" if "steer" in role or "council" in role else "maintainer"
            out.append({"url": url, "role": role, "label": it.get("label") or "discovered role page"})
        return out

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
