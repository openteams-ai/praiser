"""Phase 4 — render the record as JSON (source of truth) or Markdown."""

import json

from .models import ProjectRecord

ROLE_LABELS = {
    "maintainer": "Maintainer",
    "code_owner": "Code owner",
    "steering_council": "Steering council",
    "standards_author": "Standards author",
    "author": "Author",
    "core_contributor": "Core contributor",
    "org_owner": "Org owner",
    "org_member": "Org member",
    "contributor": "Contributor",
}


def _role_display(rec: ProjectRecord, role: str) -> str:
    """Label for one role, qualified with the subcomponent(s) when the role is
    held ONLY for a part of the project — e.g. "Author (f2py)". A role also
    evidenced at the whole-project level (any unqualified evidence) stays bare.
    """
    base = ROLE_LABELS.get(role, role)
    evs = [e for e in rec.evidence if e.role == role]
    quals = [e.qualifier for e in evs if e.qualifier]
    if evs and len(quals) == len(evs):  # every evidence for this role is scoped
        uniq = list(dict.fromkeys(quals))          # a person can hold it in several
        shown = ", ".join(uniq[:3])
        if len(uniq) > 3:
            shown += f", +{len(uniq) - 3} more"    # keep the compact view compact
        return f"{base} ({shown})"
    return base


def _roles_label(rec: ProjectRecord) -> str:
    """Human-readable list of the record's distinct elevated roles."""
    return ", ".join(_role_display(rec, r) for r in rec.roles) or "?"


def _record_to_dict(rec: ProjectRecord) -> dict:
    return {
        "project": rec.name_with_owner,
        "url": rec.url,
        "role": rec.role,
        "roles": rec.roles,
        "confidence": round(rec.confidence, 2),
        "stars": rec.stars,
        "forks": rec.forks,
        "importance": rec.importance,
        "score": round(rec.score, 3),
        "evidence": [
            {
                "source": e.source,
                "role": e.role,
                "url": e.url,
                "confidence": round(e.confidence, 2),
                "detail": e.detail,
                "qualifier": e.qualifier,
            }
            for e in sorted(rec.evidence, key=lambda e: (-e.weight, -e.confidence))
        ],
    }


def render_json(
    username: str,
    records: list[ProjectRecord],
    secondary: list[ProjectRecord] | None = None,
) -> str:
    secondary = secondary or []
    payload = {
        "user": username,
        "count": len(records),
        "projects": [_record_to_dict(r) for r in records],
        "secondary_count": len(secondary),
        "secondary": [_record_to_dict(r) for r in secondary],
    }
    return json.dumps(payload, indent=2)


def render_markdown(
    username: str,
    records: list[ProjectRecord],
    secondary: list[ProjectRecord] | None = None,
) -> str:
    secondary = secondary or []
    lines = [
        f"# Elevated-role record for `{username}`",
        "",
        f"{len(records)} project(s) where this user holds an elevated role "
        "(maintainer / code owner / steering council / standards author / "
        "core contributor)"
        + (
            f", plus **{len(secondary)}** less-popular but widely-used and "
            "maintained project(s) with an elevated role."
            if secondary else "."
        ),
        "",
    ]
    if not records and not secondary:
        lines.append("_No elevated roles found._")
        return "\n".join(lines)

    if records:
        lines += [
            "| Project | Role | Stars | Confidence | Evidence |",
            "|---|---|---:|---:|---|",
        ]
        for rec in records:
            best = rec.best_evidence
            ev_link = f"[{best.source}]({best.url})" if best else ""
            imp = f" ·{rec.importance}" if rec.importance else ""
            lines.append(
                f"| [{rec.name_with_owner}]({rec.url}){imp} | {_roles_label(rec)} | "
                f"{rec.stars} | {rec.confidence:.2f} | {ev_link} |"
            )

        lines += ["", "## Details", ""]
        for rec in records:
            lines.append(f"### [{rec.name_with_owner}]({rec.url}) — {_roles_label(rec)}")
            lines.append(
                f"Stars: {rec.stars} · Forks: {rec.forks} · "
                f"Confidence: {rec.confidence:.2f}"
                + (f" · Importance: {rec.importance}" if rec.importance else "")
            )
            lines.append("")
            for e in sorted(rec.evidence, key=lambda e: (-e.weight, -e.confidence)):
                lines.append(
                    f"- **{ROLE_LABELS.get(e.role, e.role)}** "
                    f"({e.confidence:.2f}, via `{e.source}`): {e.detail} — "
                    f"[evidence]({e.url})"
                )
            lines.append("")

    if secondary:
        lines += [
            f"## Less-popular but widely-used & maintained ({len(secondary)})",
            "",
            "Below the popularity threshold, but actively maintained with real "
            "use (forks); the user holds an elevated role.",
            "",
            "| Project | Role | Stars | Forks | Confidence |",
            "|---|---|---:|---:|---:|",
        ]
        for rec in secondary:
            lines.append(
                f"| [{rec.name_with_owner}]({rec.url}) | {_roles_label(rec)} | "
                f"{rec.stars} | {rec.forks} | {rec.confidence:.2f} |"
            )
        lines.append("")
    return "\n".join(lines)


def _human_stars(stars: int) -> str:
    if stars >= 1000:
        return f"{stars / 1000:.0f}k"
    return str(stars)


def _approx_count(n: int) -> str:
    """Round an approximate contributor total to 2 significant figures for
    display: 6835 -> "~6800", 2097 -> "~2100", 637 -> "~640", 251 -> "~250"."""
    if n < 100:
        return f"~{n}"                       # small: nothing meaningful to round
    step = 10 ** (len(str(n)) - 2)           # 2 significant figures
    return f"~{round(n / step) * step}"


def _highlight_line(rec: ProjectRecord, link_repos: bool) -> str:
    """One highlight: `REPO (STARS★) — ROLES (#R/N)`. REPO is a markdown link
    when link_repos; the `#R/N` contributor-standing is shown only when known.
    ``N`` is exact when the full contributor list was read, ``N+`` when it was
    truncated and no total was resolved, and ``~N`` (rounded) when the total is a
    snapshot / uncapped estimate."""
    repo = f"[{rec.name_with_owner}]({rec.url})" if link_repos else rec.name_with_owner
    standing = rec.contributor_standing
    rn = ""
    if standing:
        rank, total, capped, approx = standing
        shown = _approx_count(total) if approx else f"{total}{'+' if capped else ''}"
        rn = f" (#{rank}/{shown})"
    return f"- {repo} ({_human_stars(rec.stars)}★) — {_roles_label(rec)}{rn}"


def render_highlights(
    username: str,
    records: list[ProjectRecord],
    n: int,
    secondary: list[ProjectRecord] | None = None,
    link_repos: bool = False,
) -> str:
    """A compact top-N summary plus reach stats.

    The top-N are the most important roles. The footer summarises the rest: the
    remaining elevated-role projects, the smaller-but-widely-used projects where
    the user also holds a notable role, and the overall community reach (distinct
    organisations) — a proxy for breadth / potential to seed ideas widely.
    """
    secondary = secondary or []
    if not records and not secondary:
        return f"{username}: no elevated roles found."

    top = records[:max(1, n)] if records else []
    header = f"{username} — top {len(top)} highlights:" if top else None
    items = [_highlight_line(rec, link_repos) for rec in top]

    # Footer stats.
    footer: list[str] = []
    bits = []
    extra = len(records) - len(top)
    if extra > 0:
        bits.append(f"{extra} more elevated-role project(s)")
    if secondary:
        bits.append(
            f"{len(secondary)} smaller but widely-used project(s) "
            "with a notable role")
    if bits:
        footer.append("…plus " + "; ".join(bits) + ".")
    owners = {r.name_with_owner.split("/", 1)[0]
              for r in (*records, *secondary)}
    communities = {o for o in owners if o.lower() != username.lower()}
    total = len(records) + len(secondary)
    footer.append(
        f"Reach: {total} project(s) across {len(communities)} "
        "communities (distinct orgs).")

    if link_repos:
        # Markdown (web): separate the header, the bullet list, and each footer
        # line with BLANK lines — else st.markdown lazily merges the footer onto
        # the last list item (the "Reach: … on the same line as REPO" bug).
        blocks = ([header] if header else []) + \
                 (["\n".join(items)] if items else []) + footer
        return "\n\n".join(blocks)
    # Plain text (CLI): single newlines, rendered verbatim.
    return "\n".join(([header] if header else []) + items + footer)


def render(
    username: str,
    records: list[ProjectRecord],
    fmt: str,
    secondary: list[ProjectRecord] | None = None,
) -> str:
    if fmt == "json":
        return render_json(username, records, secondary)
    return render_markdown(username, records, secondary)
