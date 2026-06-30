"""Command-line entry point: ``gh-record <username> [...]``."""

import argparse
import sys

from . import __version__
from .config import Config, resolve_token
from .github_client import RateLimitError
from .pipeline import _humanize, run
from .render import render

# Shown whenever a token would help. Public-data discovery needs no scopes; add
# `repo` + `read:org` to reach private repos and resolve org/team membership.
TOKEN_HELP = (
    "Get a token at https://github.com/settings/tokens (classic: no scopes "
    "needed for public data; add 'repo' and 'read:org' for private/org access; "
    "fine-grained: read-only 'Contents' + 'Members'), then run "
    "`export GITHUB_TOKEN=<token>` or pass --token. Or just `gh auth login`."
)


def _token_hint(token_source: str) -> str:
    """A leading-newline hint about tokens, tailored to where ours came from."""
    if token_source == "none":
        return (
            "\nA token raises the limit from ~60 to 5,000 requests/hour. "
            + TOKEN_HELP
        )
    if token_source == "gh":
        return (
            "\nYou're authenticated via the gh CLI (already 5,000 requests/hour), "
            "so a different token won't raise the limit — just wait and re-run. "
            "To use an explicit token instead, set GITHUB_TOKEN: " + TOKEN_HELP
        )
    # flag / env: the user already supplied a token; 5,000/hr is the ceiling.
    return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gh-record",
        description="Record the popular projects a GitHub user maintains, "
                    "steers, or authors standards for (contributors excluded).",
    )
    p.add_argument("username", help="GitHub login to investigate")
    p.add_argument("--min-stars", type=int, default=50,
                   help="popularity threshold (default: 50); high-signal roles "
                        "and registry overrides survive regardless")
    p.add_argument("--format", choices=["md", "json"], default="md",
                   dest="fmt", help="output format (default: md)")
    p.add_argument("--token", default=None,
                   help="GitHub token (or set GITHUB_TOKEN / GH_TOKEN)")
    p.add_argument("--cache-dir", default=None,
                   help="cache directory (default: ~/.cache/ghrecord)")
    p.add_argument("--registry", default=None, dest="registry_path",
                   help="extra known-projects JSON file, merged over the seed")
    p.add_argument("--save-registry", action="store_true",
                   help="write observed popularity back to --registry")
    p.add_argument("--no-llm", action="store_true",
                   help="disable the Claude fallback for ambiguous prose")
    p.add_argument("--include-private", action="store_true",
                   help="also scan private repos (default: skip them)")
    p.add_argument("-o", "--output", default=None,
                   help="write output to a file instead of stdout")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="detailed per-repo logging to stderr")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress the live progress display")
    p.add_argument("--version", action="version",
                   version=f"gh-record {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    token, token_source = resolve_token(args.token)
    if not token:
        print(
            "warning: no GitHub token found; discovery and rate limits will be "
            "severely restricted (~60 requests/hour).\n" + TOKEN_HELP,
            file=sys.stderr,
        )

    config = Config(
        username=args.username,
        token=token,
        min_stars=args.min_stars,
        fmt=args.fmt,
        cache_dir=args.cache_dir,
        use_llm=not args.no_llm,
        registry_path=args.registry_path,
        save_registry=args.save_registry,
        verbose=args.verbose,
        quiet=args.quiet,
        include_private=args.include_private,
    )

    try:
        result = run(config)
    except KeyboardInterrupt:
        # Cancelled by the user (Ctrl-C): exit quietly, no stack trace. The
        # cache keeps whatever already succeeded, so a re-run resumes.
        print("\ncancelled (partial work is cached; re-run to continue).",
              file=sys.stderr)
        return 130
    except RateLimitError as exc:
        print(
            "error: GitHub rate limit reached before discovery could run; "
            f"wait {_humanize(exc.reset_in)} for it to reset."
            + _token_hint(token_source),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.partial_reset_in is not None:
        print(
            "warning: GitHub rate limit reached during the run — results are "
            "PARTIAL (some repos were not fully scanned). Wait "
            f"{_humanize(result.partial_reset_in)} for the limit to reset, then "
            "re-run to finish; the cache preserves what already succeeded."
            + _token_hint(token_source),
            file=sys.stderr,
        )

    output = render(
        config.username, result.records, config.fmt, result.secondary
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
