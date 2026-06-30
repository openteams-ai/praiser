"""Command-line entry point: ``gh-record <username> [...]``."""

import argparse
import sys

from . import __version__
from .config import Config, resolve_token
from .github_client import RateLimitError
from .pipeline import _humanize, run
from .render import render


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
    p.add_argument("-o", "--output", default=None,
                   help="write output to a file instead of stdout")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log progress to stderr")
    p.add_argument("--version", action="version",
                   version=f"gh-record {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    token = resolve_token(args.token)
    if not token:
        print(
            "warning: no GitHub token (set --token or GITHUB_TOKEN); "
            "discovery and rate limits will be severely restricted.",
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
    )

    try:
        records = run(config)
    except RateLimitError as exc:
        hint = "" if token else " Provide a token with --token or GITHUB_TOKEN."
        print(
            f"error: GitHub rate limit reached before discovery could run; "
            f"wait {_humanize(exc.reset_in)} for it to reset.{hint}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output = render(config.username, records, config.fmt)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
