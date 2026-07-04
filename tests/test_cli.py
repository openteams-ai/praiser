import praiser.cli as cli
import praiser.config as config
from praiser.cli import _token_hint
from praiser.config import resolve_token


def _stub_run_one_record(monkeypatch):
    from praiser.models import CODE_OWNER, Evidence, ProjectRecord
    from praiser.pipeline import RunResult
    rec = ProjectRecord("o/r", "https://github.com/o/r", stars=500,
                        evidence=[Evidence("x", CODE_OWNER, "u", 0.9, "")])
    monkeypatch.setattr(cli, "resolve_token", lambda explicit: ("tok", "flag"))
    monkeypatch.setattr(cli, "run", lambda config: RunResult(records=[rec]))


def test_default_output_is_highlights(monkeypatch, capsys):
    _stub_run_one_record(monkeypatch)
    assert cli.main(["someuser"]) == 0
    out = capsys.readouterr().out
    assert "highlights" in out
    assert "# Elevated-role record" not in out


def test_format_md_gives_full_report(monkeypatch, capsys):
    _stub_run_one_record(monkeypatch)
    assert cli.main(["someuser", "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert "# Elevated-role record" in out


def test_main_keyboardinterrupt_exits_cleanly(monkeypatch, capsys):
    def boom(_config):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run", boom)
    monkeypatch.setattr(cli, "resolve_token", lambda explicit: ("tok", "flag"))

    rc = cli.main(["someuser"])

    assert rc == 130  # conventional SIGINT exit code
    err = capsys.readouterr().err
    assert "cancelled" in err
    assert "Traceback" not in err


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
