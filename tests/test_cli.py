import ghrecord.config as config
from ghrecord.cli import TOKEN_HELP, _token_hint
from ghrecord.config import resolve_token


def test_resolve_token_explicit_flag():
    assert resolve_token("abc123") == ("abc123", "flag")


def test_resolve_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "envtok")
    assert resolve_token(None) == ("envtok", "env")


def test_resolve_token_none(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(config, "_gh_cli_token", lambda: None)
    assert resolve_token(None) == (None, "none")


def test_token_hint_none_points_to_url():
    hint = _token_hint("none")
    assert "5,000" in hint
    assert "github.com/settings/tokens" in hint


def test_token_hint_gh_explains_and_links():
    hint = _token_hint("gh")
    assert "gh CLI" in hint
    assert "github.com/settings/tokens" in hint


def test_token_hint_explicit_is_silent():
    assert _token_hint("flag") == ""
    assert _token_hint("env") == ""
