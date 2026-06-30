"""Phase 4 — render the record as JSON (source of truth) or Markdown."""

import json

from .models import ProjectRecord

ROLE_LABELS = {
    "maintainer": "Maintainer",
    "code_owner": "Code owner",
    "steering_council": "Steering council",
    "standards_author": "Standards author",
    "org_owner": "Org owner",
    "org_member": "Org member",
    "contributor": "Contributor",
}


def _record_to_dict(rec: ProjectRecord) -> dict:
    return {
        "project": rec.name_with_owner,
        "url": rec.url,
        "role": rec.role,
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


def render_json(username: str, records: list[ProjectRecord]) -> str:
    payload = {
        "user": username,
        "count": len(records),
        "projects": [_record_to_dict(r) for r in records],
    }
    return json.dumps(payload, indent=2)


def render_markdown(username: str, records: list[ProjectRecord]) -> str:
    lines = [
        f"# Elevated-role record for `{username}`",
        "",
        f"{len(records)} project(s) where this user holds an elevated role "
        "(maintainer / code owner / steering council / standards author).",
        "",
    ]
    if not records:
        lines.append("_No elevated roles found._")
        return "\n".join(lines)

    lines += [
        "| Project | Role | Stars | Confidence | Evidence |",
        "|---|---|---:|---:|---|",
    ]
    for rec in records:
        role = ROLE_LABELS.get(rec.role or "", rec.role or "?")
        best = rec.best_evidence
        ev_link = f"[{best.source}]({best.url})" if best else ""
        imp = f" ·{rec.importance}" if rec.importance else ""
        lines.append(
            f"| [{rec.name_with_owner}]({rec.url}){imp} | {role} | "
            f"{rec.stars} | {rec.confidence:.2f} | {ev_link} |"
        )

    lines += ["", "## Details", ""]
    for rec in records:
        role = ROLE_LABELS.get(rec.role or "", rec.role or "?")
        lines.append(f"### [{rec.name_with_owner}]({rec.url}) — {role}")
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
    return "\n".join(lines)


def render(username: str, records: list[ProjectRecord], fmt: str) -> str:
    if fmt == "json":
        return render_json(username, records)
    return render_markdown(username, records)
