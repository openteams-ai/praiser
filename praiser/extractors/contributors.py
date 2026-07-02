"""Core-contributor extractor (commit-volume signal).

Being a substantial committer to a *popular* project is an elevated role even
when the person isn't named in any governance/role file — this is how we catch
historical maintainers and authors of major components (e.g. f2py in NumPy)
whose involvement lives in the commit history, not a CODEOWNERS line.

Gated on popularity (stars >= floor) so it both matches the "popular software"
goal and bounds the extra API calls. Uses the cached contributors list, so it
also corroborates other extractors for free.
"""

from ..models import CORE_CONTRIBUTOR, Evidence
from . import register
from .base import Extractor, ExtractContext


# Also scan repos that aren't star-popular but are widely forked/used.
WIDELY_USED_FORKS = 25
# Only spend a search-API call on the merged-PR rescue for notable repos.
PR_RESCUE_MIN_STARS = 1000
# A high rank only means "core" with a real amount of work behind it. On a repo
# with few contributors, 1-2 commits can rank top-10 — that's a drive-by, not a
# core contributor. So the rank shortcut requires at least this many commits/PRs.
MIN_RANKED_CONTRIBUTIONS = 10


def classify(count: int, rank: int) -> float | None:
    """Confidence for a contributor, or None if too minor to count as elevated.

    ``count`` is commits (or merged PRs, via the rescue). "Core" means either a
    substantial body of work (volume), or a genuine top-of-project position
    backed by real work. A merely double-digit rank with barely double-digit
    commits (e.g. 11 commits at #17) is a *regular* contributor, not core — that
    middle zone is deliberately excluded to avoid over-crediting."""
    if count >= 100:
        return 0.8                          # heavy volume
    if count >= 25:
        return 0.6                          # solid volume
    # Rank shortcut: only a genuine top-10 position, and only with real work
    # behind it (a couple of commits ranking high on a small-team repo isn't
    # core). No looser "top-30" tier — that over-credits regular contributors.
    if count >= MIN_RANKED_CONTRIBUTIONS and rank <= 10:
        return 0.8
    return None


class ContributorsExtractor(Extractor):
    name = "contributors"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Worthwhile for star-popular repos and for widely-forked (used) ones,
        # so we also catch core roles on less-popular-but-widely-used projects.
        # A user-vouched (manual) repo is always checked, whatever its size.
        return (
            candidate.name_with_owner in ctx.manual_repos
            or candidate.stars >= max(1, ctx.popularity_floor)
            or candidate.forks >= WIDELY_USED_FORKS
        )

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        # Commit history is copy-vulnerable: a repo that vendored an upstream's
        # history makes the user look like a heavy committer everywhere. Trust
        # the contributor signal on the user's own/org repos or the canonical
        # (popular/widely-forked) project — OR when the user is the #1
        # contributor to a widely-forked, non-fork repo (a vendored copy rarely
        # has the upstream author as its top committer) — OR when the repo was
        # discovered via a genuine live-contribution signal (contributed/history):
        # GitHub attributes a vendored copy's commits to the ORIGINAL authors, not
        # the copier, so those sources are copy-resistant and rescue genuine leads
        # of modest projects (e.g. a 110-star lib) without relaxing thresholds.
        trusted = ctx.trust_role_file(candidate)
        genuine_contrib = bool(candidate.sources & {"contributed", "history"})
        rank_rescue = (not trusted and not genuine_contrib and not candidate.is_fork
                       and candidate.forks >= WIDELY_USED_FORKS)
        if not trusted and not genuine_contrib and not rank_rescue:
            return []
        contribs = ctx.contributors(candidate)
        if not contribs:
            return []
        count = max((contribs.get(h, 0) for h in ctx.identity.logins), default=0)
        if count <= 0:
            return []
        rank = 1 + sum(1 for v in contribs.values() if v > count)
        if rank_rescue and rank != 1:
            return []  # widely-forked but not the top contributor -> not trusted
        manual = candidate.name_with_owner in ctx.manual_repos
        confidence = classify(count, rank)
        detail = f"{count} commits (~#{rank} contributor)"

        if confidence is None:
            # Commit count can understate real impact: squash/ghstack land one
            # commit per PR, and unlinked commit emails aren't attributed. Fall
            # back to merged-PR count (workflow-agnostic) for notable repos.
            if candidate.stars >= PR_RESCUE_MIN_STARS or manual:
                prs = max((ctx.forge.merged_pr_count(
                    candidate.owner, candidate.repo, h)
                    for h in ctx.identity.logins), default=0)
                pr_conf = classify(prs, rank)
                if pr_conf is not None:
                    confidence = pr_conf
                    detail = f"{prs} merged PRs ({count} commits, ~#{rank})"
        if confidence is None:
            # Still below the bar: a plain contributor, excluded — unless the
            # user explicitly vouched for this repo via --add-repo.
            if not manual:
                return []
            confidence = 0.4
        # Resolve the true contributor total only now that we know this repo
        # earns a role — a registry snapshot, else the fetched length (exact
        # unless it hit our page cap, in which case one request gets the real,
        # uncapped total; see ExtractContext.contributor_total).
        n_contributors, capped, approx = ctx.contributor_total(
            candidate, len(contribs))
        return [Evidence(
            source=self.name, role=CORE_CONTRIBUTOR,
            url=f"{candidate.url}/graphs/contributors",
            confidence=confidence, detail=detail,
            rank=rank, n_contributors=n_contributors,
            contributors_capped=capped, contributors_approx=approx,
        )]


register(ContributorsExtractor())
