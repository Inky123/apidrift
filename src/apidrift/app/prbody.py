"""The fix pull request's title and body.

Same rules as the issue: everything that comes from the scanned repository (paths, call text) goes through
:func:`issue.permalink` and :func:`issue.code`, and the tokens the child names (fields, paths, versions) are
rendered inside code spans even though :mod:`fixcheck` has already restricted them to Stripe names.
"""

from __future__ import annotations

import re

from .issue import FIELD_RE, METHOD_RE, VERSION_RE, code, escape, permalink, unread_note

PR_MARKER_RE = re.compile(r"<!-- apidrift:pr=([0-9a-f]{64}) -->")
STRIPE_DOCS = "https://docs.stripe.com/"
MAX_PR_LINKS = 50  # per list; a repository with more than this gets "and N more."
# One fixed sentence per reason the fixer gives for leaving a site alone; nothing from the repo goes in them.
UNFIXABLE_WHY = {
    "nested": "the field sits inside a nested object or array, which apidrift does not rewrite yet",
    "not_found": "apidrift could not locate the key in the source text",
    "incomplete_binding": "the request parameters are built in code apidrift could not follow to a literal",
    "oracle_changed_other_calls": "the rewrite would have changed another Stripe call in the same file",
    "oracle_finding_remains": "the rewrite did not clear the finding at this site",
    "oracle_new_finding": "the rewrite would have introduced a new finding",
    "leftover_tokens": "the old name still appears elsewhere in the file after the rewrite",
    "not_utf8": "the file is not valid UTF-8",
    "too_large": "the file is larger than apidrift rewrites",
    "cap": "the repository has more files to fix than one pull request carries",
    "unsupported_key_form": "the key is written in a form apidrift does not rewrite",
}
NOT_ATTEMPTED_WHY = {"arguments_not_visible": "arguments are not fully visible"}


def pr_marker(fingerprint: str) -> str:
    return f"<!-- apidrift:pr={fingerprint} -->"


def pr_marker_in(body: str | None) -> str | None:
    match = PR_MARKER_RE.search(body or "")
    return match.group(1) if match else None


def _where(full_name: str, sha: str, file: str, line: object) -> str:
    try:
        number = int(line)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0
    return permalink(full_name, sha, file, number) or f"{code(file)}:{number}"


def _links(full_name: str, sha: str, places: list[tuple[str, object]]) -> str:
    shown = [_where(full_name, sha, file, line) for file, line in places[:MAX_PR_LINKS]]
    more = len(places) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _calls(payload: dict) -> dict[tuple[str, int, str], str]:
    out: dict[tuple[str, int, str], str] = {}
    for impact in payload.get("impacts") or []:
        usage = impact.get("usage") if isinstance(impact, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("line"), int):
            out[(str(usage.get("file")), usage["line"], str(usage.get("path")))] = str(usage.get("call") or "")
    return out


def safe_version(version: str) -> str:
    """A version string for prose: as is when it looks like one, otherwise inside a code span."""
    return version if VERSION_RE.fullmatch(version) else code(version)


def pr_title(version: str, target: str, sites: int) -> str:
    noun = "call site" if sites == 1 else "call sites"
    field = target if FIELD_RE.fullmatch(target) else "a removed field"
    version = version if VERSION_RE.fullmatch(version) else "the next release"
    return f"Stripe {version}: rename {field} ({sites} {noun})"


def render_pr(
    payload: dict, fixes: list[dict], *, full_name: str, sha: str, version: str, issue_number: int, fingerprint: str
) -> tuple[str, str]:
    """(title, body) for the pull request that pushes ``fixes`` (the validated subset of ``payload['fixes']``).

    Repo text (paths, call text) is rendered through :func:`permalink` and :func:`code` only; the migration
    note and doc link come from our own table but are escaped and checked all the same.
    """
    calls = _calls(payload)
    migrations = {
        (str(m.get("path")), str(m.get("target"))): m for m in payload.get("migrations") or [] if isinstance(m, dict)
    }
    groups: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
    for fix in fixes:
        for site in fix.get("sites") or []:
            key = (str(site.get("path")), str(site.get("target")), str(site.get("replacement")))
            groups.setdefault(key, []).append((str(fix["file"]), int(site.get("line") or 0)))
    sites = len({(f, ln, k[0]) for k, places in groups.items() for f, ln in places})
    target = next((k[1] for k in groups), "payment_method_types")
    lines = [
        f"Ready for Stripe {safe_version(version)}. Merge this together with your API-version and SDK upgrade; on your "
        "current pinned version Stripe rejects the new parameter.",
    ]
    for (path, old, new), places in sorted(groups.items()):
        migration = migrations.get((path, old), {})
        method = str(migration.get("method") or "post")
        method = method.upper() if METHOD_RE.fullmatch(method) else "POST"
        lines += ["", f"**{code(old)} → {code(new)} on {method} {code(path)}**"]
        note = escape(str(migration.get("note") or ""))
        doc = str(migration.get("doc") or "")
        link = f"[Changelog]({doc})" if doc.startswith(STRIPE_DOCS) and " " not in doc and ")" not in doc else ""
        if note or link:
            lines.append(" ".join(part for part in (note, link) if part))
        for file, line in sorted(places)[:MAX_PR_LINKS]:
            call = calls.get((file, line, path))
            lines.append(f"- {_where(full_name, sha, file, line)}" + (f" via {code(call)}" if call else ""))
        if len(places) > MAX_PR_LINKS:
            lines.append(f"- and {len(places) - MAX_PR_LINKS} more.")
    lines += ["", "**Merge together with your upgrade**"]
    pins = [p for p in payload.get("pins") or [] if isinstance(p, dict) and p.get("file")]
    if pins:
        lines.append("These files pin an API version that predates the rename; bump them and the Stripe SDK in the same change:")
        for pin in pins[:MAX_PR_LINKS]:
            lines.append(f"- {_where(full_name, sha, str(pin['file']), pin.get('line'))} pins {code(str(pin.get('version') or ''))}")
    else:
        lines.append("No API-version pin was found in the scanned files.")
    lines += [
        "",
        f"Verified: apidrift re-scanned the changed files; {sites} call sites no longer send {code(target)}; "
        "every other Stripe call is unchanged.",
    ]
    lines += _left_for_you(payload, full_name, sha)
    lines += ["", f"Addresses #{issue_number}", pr_marker(fingerprint)]
    return pr_title(version, target, sites), "\n".join(lines) + "\n"


def _left_for_you(payload: dict, full_name: str, sha: str) -> list[str]:
    by_why: dict[str, list[tuple[str, object]]] = {}
    for item in payload.get("unfixable") or []:
        if isinstance(item, dict) and item.get("file"):
            by_why.setdefault(str(item.get("why") or ""), []).append((str(item["file"]), item.get("line")))
    not_attempted: dict[str, list[tuple[str, object]]] = {}
    for item in payload.get("not_attempted") or []:
        if isinstance(item, dict) and item.get("file"):
            not_attempted.setdefault(str(item.get("why") or ""), []).append((str(item["file"]), item.get("line")))
    leftovers = [(str(i["file"]), i.get("line")) for i in payload.get("leftovers") or [] if isinstance(i, dict) and i.get("file")]
    unread = unread_note(payload)
    if not (by_why or not_attempted or leftovers or unread):
        return []
    lines = ["", "**Left for you**"]
    for why, places in sorted(by_why.items()):
        sentence = UNFIXABLE_WHY.get(why) or f"apidrift could not rewrite this site safely ({escape(why or 'no reason given')})"
        lines.append(f"- Not changed, {sentence}: {_links(full_name, sha, places)}")
    for why, places in sorted(not_attempted.items()):
        reason = NOT_ATTEMPTED_WHY.get(why) or f"apidrift did not try this site ({escape(why or 'no reason given')})"
        lines.append(f"- Not attempted ({reason}): {_links(full_name, sha, places)}")
    if leftovers:
        lines.append(f"- Still mentions the old name at {_links(full_name, sha, leftovers)}; check whether those are this field.")
    if unread:
        lines.append(f"- {unread}")
    return lines
