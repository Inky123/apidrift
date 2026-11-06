"""The operator's view of the stored indexes: ``apidrift status`` counts and ``apidrift index <repo>``."""

from __future__ import annotations

import json
import sqlite3

from . import db


def summary(conn: sqlite3.Connection) -> dict:
    """Counts for ``apidrift status``: plain types only."""
    head = conn.execute(
        "SELECT count(DISTINCT repo_id) AS repos, coalesce(sum(checked_by = 'index'), 0) AS by_index FROM repo_index"
    ).fetchone()
    body = conn.execute(
        "SELECT coalesce(sum(language != 'doc'), 0) AS call_sites, coalesce(sum(language = 'doc'), 0) AS doc_sites,"
        " count(DISTINCT CASE WHEN language != 'doc' THEN method || ' ' || path END) AS endpoints FROM index_sites"
    ).fetchone()
    return {**dict(head), **dict(body)}


def render(full_name: str, header: db.Row, rows: list[db.Row]) -> str:
    """One repo's index as text, grouped by endpoint. Every value was validated as printable before storage."""
    counted = [r for r in rows if r["language"] != "doc"]
    groups: dict[tuple[str, str], list[db.Row]] = {}
    for row in counted:
        groups.setdefault((row["path"], row["method"]), []).append(row)
    lines = [
        f"{full_name} at {header['sha'][:12]}: {len(counted)} {header['provider'].capitalize()} call sites"
        f" on {len(groups)} endpoints",
        f"indexed {header['written_at']} by job {header['job_id']}; last checked against {header['checked']}"
        f" ({'from the index' if header['checked_by'] == 'index' else 'by a full check'}, {header['checked_at']})",
    ]
    for (path, method), group in sorted(groups.items()):
        lines += ["", f"{method.upper()} {path}  ({len(group)})"]
        for row in group:
            fields = json.loads(row["fields"])
            shown = ", ".join(fields[:8]) + (f" (+{len(fields) - 8} more)" if len(fields) > 8 else "")
            if not row["complete"]:
                shown += (" " if shown else "") + "(+ fields not visible)"
            lines.append(f"  {row['file']}:{row['line']}  {row['call']}  [{row['language']}]" + (f"  fields: {shown}" if shown else ""))
    docs = [r for r in rows if r["language"] == "doc"]
    if docs:
        lines += ["", f"documentation, fixtures and tests (not counted): {len(docs)}"]
        lines += [f"  {r['file']}:{r['line']}  {r['method'].upper()} {r['path']}  {r['call']}" for r in docs]
    return "\n".join(lines)
