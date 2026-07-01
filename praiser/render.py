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


def _roles_label(rec: ProjectRecord) -> str:
    """Human-readable list of the record's distinct elevated roles."""
    return ", ".join(ROLE_LABELS.get(r, r) for r in rec.roles) or "?"


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


def render_highlights(
    username: str,
    records: list[ProjectRecord],
    n: int,
    secondary: list[ProjectRecord] | None = None,
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

    lines: list[str] = []
    top = records[:max(1, n)] if records else []
    if top:
        lines.append(f"{username} — top {len(top)} highlights:")
        for rec in top:
            lines.append(
                f"- {rec.name_with_owner} — {_roles_label(rec)} "
                f"({_human_stars(rec.stars)}★)"
            )

    # Footer stats.
    bits = []
    extra = len(records) - len(top)
    if extra > 0:
        bits.append(f"{extra} more elevated-role project(s)")
    if secondary:
        bits.append(
            f"{len(secondary)} smaller but widely-used project(s) "
            "with a notable role")
    if bits:
        lines.append("…plus " + "; ".join(bits) + ".")

    owners = {r.name_with_owner.split("/", 1)[0]
              for r in (*records, *secondary)}
    communities = {o for o in owners if o.lower() != username.lower()}
    total = len(records) + len(secondary)
    lines.append(
        f"Reach: {total} project(s) across {len(communities)} "
        "communities (distinct orgs).")
    return "\n".join(lines)


def render(
    username: str,
    records: list[ProjectRecord],
    fmt: str,
    secondary: list[ProjectRecord] | None = None,
) -> str:
    if fmt == "json":
        return render_json(username, records, secondary)
    return render_markdown(username, records, secondary)
