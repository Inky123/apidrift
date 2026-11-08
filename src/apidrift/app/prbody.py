"""The fix pull request's title and body.

Same rules as the issue: everything that comes from the scanned repository (paths, call text) goes through
:func:`issue.permalink` and :func:`issue.code`, and the tokens the child names (fields, paths, versions) are
rendered inside code spans even though :mod:`fixcheck` has already restricted them to Stripe names.

The body is bounded: every list shows at most MAX_PR_LINKS places ("and N more"), at most MAX_PR_GROUPS
migrations are described, and when the body still exceeds :data:`issue.MAX_BODY_CHARS` (GitHub rejects
65,536) it is rendered again with shorter lists and, as a last resort, cut before the footer. A child cannot
make the pull request fail to open by sending long paths or long lists.
"""

from __future__ import annotations

import re

from .. import fixsdk, migrations
from . import fixcheck, prpins
from .issue import FIELD_PATH_RE, MAX_BODY_CHARS, METHOD_RE, VERSION_RE, code, escape, permalink, unread_note

PR_MARKER_RE = re.compile(r"<!-- apidrift:pr=([0-9a-f]{64}) -->")
STRIPE_DOCS = "https://docs.stripe.com/"
MAX_PR_LINKS = 50  # per list; a repository with more than this gets "and N more."
MAX_PR_GROUPS = 40  # migrations described; the rest are counted
LINK_CAPS = (MAX_PR_LINKS, 10, 3, 1)  # the lists shrink until the body fits
TRUNCATED = "The rest of this description was cut to fit GitHub's size limit; the commit itself is complete."
# One fixed sentence per reason the fixer gives for leaving a site alone; nothing from the repo goes in them.
UNFIXABLE_WHY = {
    "nested": "the field sits inside a nested structure apidrift does not rewrite yet",
    "nested_array": "the field sits inside an array of objects, which apidrift does not rewrite yet",
    "unsupported_value": "the value is not one Stripe's changelog documents a rewrite for, so a person has to choose it",
    "would_empty_literal": "removing the field would leave the parameters empty, which apidrift leaves for a person to tidy",
    "not_found": "apidrift could not locate the key in the source text",
    "incomplete_binding": "the request parameters are built in code apidrift could not follow to a literal",
    "shared_literal": "the parameters are also used for something other than this Stripe call (copied, returned, "
    "passed on or read), so changing them here would change that use too",
    "oracle_changed_other_calls": "the rewrite would have changed another Stripe call in the same file",
    "oracle_finding_remains": "the rewrite did not clear the finding at this site",
    "oracle_new_finding": "the rewrite would have introduced a new finding",
    "leftover_tokens": "the old name still appears elsewhere in the file after the rewrite",
    "not_utf8": "the file is not valid UTF-8",
    "syntax": "the edited file would not parse, so apidrift left it alone",
    "too_large": "the file is larger than apidrift rewrites",
    "cap": "the repository has more files to fix than one pull request carries",
    "unsupported_key_form": "the key is written in a form apidrift does not rewrite",
    "sdk_no_rewrite": "stripe-go models this field its own way and apidrift has no verified rewrite for it",
    "in_string": "the call sits inside a heredoc or string, so it is text rather than code",
    "unbounded": "the code around this call can change variables in ways apidrift cannot follow, or the file "
    "cannot be read with certainty",
    "helper_shared": "the parameters come from a helper whose result is also used for something other than this "
    "Stripe call",
    "helper_feeds_other_calls": "the parameters come from a helper that also feeds another Stripe call this "
    "change does not cover",
    "helper_used_elsewhere": "the parameters come from a helper that another file also uses or overrides",
    "helper_version_switch": "the parameters come from a helper that picks the field by Stripe API version; change "
    "the version it switches at rather than the field",
}
NOT_ATTEMPTED_WHY = {"arguments_not_visible": "arguments are not fully visible"}
UNKNOWN_UNFIXABLE = "apidrift could not rewrite this site safely"
UNKNOWN_NOT_ATTEMPTED = "apidrift did not try this site"
POSSIBLY_REMAINING = "possibly still sent through a spread or a variable apidrift cannot see"
# Go is typed: the new field names do not exist in an older stripe-go, so the note says what merging needs.
GO_NOTE = (
    "The Go files use struct fields that exist from stripe-go v87 on (its first release for Stripe 2026-09-30.endive), "
    "so they compile only once `github.com/stripe/stripe-go` is upgraded in the same change. apidrift cannot run the Go "
    "toolchain: run `gofmt` and `go build` on them before merging."
)


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


def _links(full_name: str, sha: str, places: list[tuple[str, object]], cap: int = MAX_PR_LINKS) -> str:
    shown = [_where(full_name, sha, file, line) for file, line in places[:cap]]
    more = len(places) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _places(items: object) -> list[tuple[str, object]]:
    if not isinstance(items, list):
        return []
    return [(str(i["file"]), i.get("line")) for i in items if isinstance(i, dict) and i.get("file")]


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


def phrase(action: str, target: str) -> str:
    """A short plain-text phrase for one migration, for titles and commit messages: only a validated field name
    ever goes in; anything else becomes "a removed field"."""
    field = target if FIELD_PATH_RE.fullmatch(target) else "a removed field"
    if action == "delete":
        return f"remove {field}"
    if action == "wrap_type":
        return f"wrap {field.rsplit('.', 1)[-1]}"
    return f"rename {field}"


def summary(keys: set[tuple[str, str]] | list[tuple[str, str]]) -> str:
    """One phrase when every site is the same change (action, target); otherwise "N migrations"."""
    phrases = sorted({phrase(action, target) for action, target in keys})
    return phrases[0] if len(phrases) == 1 else f"{len(phrases)} migrations"


def pr_title(version: str, keys: set[tuple[str, str]] | list[tuple[str, str]], sites: int) -> str:
    noun = "call site" if sites == 1 else "call sites"
    version = version if VERSION_RE.fullmatch(version) else "the next release"
    return f"Stripe {version}: {summary(keys)} ({sites} {noun})"


def _heading(action: str, old: str, new: str, method: str, path: str) -> str:
    where = f"{method} {code(path)}"
    if action == "delete":
        return f"**remove {code(old)} from {where}**"
    if action == "wrap_type":
        return f"**{code(old)} becomes an object with {code('type')} on {where}**"
    return f"**{code(old)} → {code(new)} on {where}**"


_BRACED_RE = re.compile(r"\{[^{}]*\}")


def _note(text: str) -> str:
    """A note from our own table, escaped; a ``{...}`` run (an object literal) goes in a code span instead."""
    out, at = [], 0
    for match in _BRACED_RE.finditer(text):
        out += [escape(text[at:match.start()]), code(match.group(0))]
        at = match.end()
    return "".join(out + [escape(text[at:])])


def _explain(migration: migrations.Migration | None) -> str:
    """The migration's note and changelog link, from the parent's own table (never the child's payload)."""
    if migration is None:
        return ""
    note = _note(migration.note)
    doc = migration.doc
    link = f"[Changelog]({doc})" if doc.startswith(STRIPE_DOCS) and " " not in doc and ")" not in doc else ""
    return " ".join(part for part in (note, link) if part)


def _migration(path: str, target: str, action: str, replacement: str) -> migrations.Migration | None:
    """The parent's table entry for a site, only when the site's action and replacement agree with it."""
    found = migrations.for_site(path, target)
    return found if found is not None and (found.action, found.replacement) == (action, replacement) else None


def _order(key: tuple[str, str, str, str]) -> tuple[str, ...]:
    """One migration's endpoints sit together: by changelog entry, then action, then path."""
    path, old, action, new = key
    found = _migration(path, old, action, new)
    return (found.doc if found is not None else "", action, path, old, new)


def render_pr(
    payload: dict, fixes: list[dict], *, full_name: str, sha: str, version: str, issue_number: int, fingerprint: str,
    tests: list[dict] | None = None,
) -> tuple[str, str]:
    """(title, body) for the pull request that pushes ``fixes`` (the validated subset of ``payload['fixes']``)
    and ``tests`` (the test files :mod:`fixtestcheck` rebuilt; their lines are the parent's own checked ones).

    Repo text (paths, call text) is rendered through :func:`permalink` and :func:`code` only. Each group's
    method, note and changelog link come from the parent's migration table by (path, target); the child's own
    ``migrations`` list is ignored. Value text is never shown: each action is described in a fixed sentence.
    The body never exceeds :data:`issue.MAX_BODY_CHARS` (see the module note).
    """
    for cap in LINK_CAPS:
        title, lines, footer = _render(payload, fixes, full_name, sha, version, issue_number, fingerprint, tests, cap)
        body = "\n".join(lines + footer) + "\n"
        if len(body) <= MAX_BODY_CHARS:
            return title, body
    budget = MAX_BODY_CHARS - sum(len(ln) + 1 for ln in footer) - len(TRUNCATED) - 2
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return title, "\n".join(kept + ["", TRUNCATED] + footer) + "\n"


def _render(
    payload: dict, fixes: list[dict], full_name: str, sha: str, version: str, issue_number: int, fingerprint: str,
    tests: list[dict] | None, cap: int,
) -> tuple[str, list[str], list[str]]:
    """(title, body lines, footer lines) with every list cut to ``cap`` places."""
    calls = _calls(payload)
    groups: dict[tuple[str, str, str, str], list[tuple[str, int]]] = {}
    for fix in fixes:
        for site in fix.get("sites") or []:
            action = str(site.get("action") or "rename")
            key = (str(site.get("path")), str(site.get("target")), action, str(site.get("replacement")))
            groups.setdefault(key, []).append((str(fix["file"]), int(site.get("line") or 0)))
    placed = {(f, ln, k[0]) for k, places in groups.items() for f, ln in places}
    possibly = _possibly_remaining(payload, placed)
    deletes_only = bool(groups) and all(action == "delete" for _, _, action, _ in groups)
    introduced = _introduced(groups, version)
    pinned_older = prpins.older_server(fixcheck.pin_list(payload.get("pins")), introduced)
    lede = prpins.lede({action for _, _, action, _ in groups}, safe_version(introduced), pinned_older)
    lines = [f"Ready for Stripe {safe_version(version)}. {lede}"]
    if not deletes_only and any(str(fix.get("file")).endswith(".go") for fix in fixes):
        lines += ["", GO_NOTE]
    explained_once: set[str] = set()
    ordered = sorted(groups, key=_order)
    shown_groups = min(MAX_PR_GROUPS, cap * 4)
    for key in ordered[:shown_groups]:
        path, old, action, new = key
        migration = _migration(path, old, action, new)
        method = migration.method if migration is not None else "post"
        method = method.upper() if METHOD_RE.fullmatch(method) else "POST"
        lines += ["", _heading(action, old, new, method, path)]
        explained = _explain(migration)
        if explained and explained not in explained_once:  # one note per migration, not per endpoint
            explained_once.add(explained)
            lines.append(explained)
        places = groups[key]
        for file, line in sorted(places)[:cap]:
            call = calls.get((file, line, path))
            lines.append(f"- {_where(full_name, sha, file, line)}" + (f" via {code(call)}" if call else ""))
        if len(places) > cap:
            lines.append(f"- and {len(places) - cap} more.")
    if len(ordered) > shown_groups:
        lines += ["", f"And {len(ordered) - shown_groups} more migrations, at the call sites the diff shows."]
    lines += _pins(payload, {action for _, _, action, _ in groups}, introduced, full_name, sha, cap)
    lines += _sdks(payload, fixes, full_name, sha, cap)
    uncertain = len({place for _, places in possibly for place in places})
    verified = len(placed) - uncertain
    count = f"{verified} call site now sends" if verified == 1 else f"{verified} call sites now send"
    maybe = f" ({uncertain} may still send it through a spread; see below)" if uncertain else ""
    lines += [
        "",
        f"Verified: apidrift re-scanned the changed files; {count} what Stripe {safe_version(version)} expects{maybe}; "
        "every other Stripe call is unchanged.",
    ]
    updated = sorted({(str(t["file"]), int(e["line"])) for t in tests or [] for e in t["edits"]})
    if updated:
        lines += ["", "**Tests updated**", "These tests expected the old request and now expect the new one, changed the "
                  f"same way as the code: {_links(full_name, sha, list(updated), cap)}."]
    lines += _left_for_you(payload, full_name, sha, possibly, set(updated), cap)
    lines += _kept(payload, full_name, sha, cap)
    keys = {(action, old) for _, old, action, _ in groups}
    return pr_title(version, keys, len(placed)), lines, ["", f"Addresses #{issue_number}", pr_marker(fingerprint)]


def _introduced(groups: dict[tuple[str, str, str, str], list[tuple[str, int]]], version: str) -> str:
    """The earliest release (from the parent's table) that changed a non-delete field here; else ``version``."""
    found = [m.introduced for (path, old, action, new) in groups if action != "delete"
             and (m := _migration(path, old, action, new)) is not None]
    return min(found) if found else version


def _pins(payload: dict, actions: set[str], introduced: str, full_name: str, sha: str, cap: int = MAX_PR_LINKS) -> list[str]:
    """Pins checked by :func:`fixcheck.pin_list`, compared with ``introduced`` (:mod:`prpins`, which caps its
    own lists at MAX_PR_LINKS; a tighter ``cap`` cuts the pins it sees)."""
    where = lambda file, line: _where(full_name, sha, file, line)  # noqa: E731
    pins = fixcheck.pin_list(payload.get("pins"))
    pins = pins if cap >= MAX_PR_LINKS else pins[:cap]
    return prpins.section(pins, actions, introduced, safe_version(introduced), where)


TYPED_NODE = (".ts", ".tsx", ".mts", ".cts")


def _sdks(payload: dict, fixes: list[dict], full_name: str, sha: str, cap: int = MAX_PR_LINKS) -> list[str]:
    """One sentence per locked SDK version (validated by :func:`fixcheck.sdk_locks`), measured against the
    parent's own table of first releases for the API version the fixed sites need. Nothing for deletions."""
    locks = fixcheck.sdk_locks(payload.get("sdks"), fixes)[:cap]
    needed = {
        m.introduced for fix in fixes for s in fix.get("sites") or []
        if s.get("action") != "delete" and (m := migrations.for_site(str(s.get("path")), str(s.get("target")))) is not None
    }
    if not locks or not needed:
        return []
    api = max(needed)
    typed = any(str(fix.get("file")).lower().endswith(TYPED_NODE) for fix in fixes)
    lines = ["", "**Stripe SDK version**"]
    for lock in locks:
        sdk, version = lock["sdk"], lock["version"]
        where = _where(full_name, sha, lock["file"], lock["line"])
        first = fixsdk.FIRST_RELEASE.get(api, {}).get(sdk)
        older = fixsdk.older(sdk, version, api)
        if older and first:
            note = f"[{sdk} {first}]({fixsdk.CHANGELOGS[sdk]}#{first.replace('.', '-')})"
            line = (f"- {where} locks {code(sdk)} {code(version)}, which is older than {note}, its first release for "
                    f"Stripe {safe_version(api)}. Upgrade it in this change, or make sure every request it sends uses "
                    f"API version {safe_version(api)} or later.")
            if sdk == "stripe-go":
                major = first.split(".")[0]
                line += (f" stripe-go v{major} is imported as {code(f'{fixsdk.GO_MODULE}/v{major}')}, so go.mod and "
                         "the import paths change with it; apidrift does not change import paths.")
            if sdk == "stripe-node" and typed:
                line += " Its TypeScript types may not have the new field names or shapes, so the type check can fail until then."
        elif older is None:
            line = (f"- {where} locks {code(sdk)} {code(version)}; it must be a release built for Stripe API version "
                    f"{safe_version(api)} or later" + (f" ({sdk} {first} or later)." if first else "."))
        else:
            line = f"- {where} locks {code(sdk)} {code(version)}, a release built for Stripe {safe_version(api)} or later."
        lines.append(line)
    return lines


Possibly = list[tuple[str, list[tuple[str, int, str]]]]


def _possibly_remaining(payload: dict, placed: set[tuple[str, int, str]]) -> Possibly:
    """(field, places) for delete sites that may still send the old key through a spread: only sites this pull
    request changes, with the field named from the parent's table (never the child's text)."""
    by_field: dict[str, list[tuple[str, int, str]]] = {}
    items = payload.get("possibly_remaining")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        file, line, path = str(item.get("file")), item.get("line"), str(item.get("path"))
        found = migrations.for_site(path, str(item.get("target")))
        if found is None or isinstance(line, bool) or not isinstance(line, int) or (file, line, path) not in placed:
            continue
        if (file, line, path) not in by_field.setdefault(found.target, []):
            by_field[found.target].append((file, line, path))
    return sorted(by_field.items())


def _left_for_you(
    payload: dict, full_name: str, sha: str, possibly: Possibly | None = None, updated: set | frozenset = frozenset(),
    cap: int = MAX_PR_LINKS,
) -> list[str]:
    """Fixed sentences only: a reason the child gives that is not in our tables becomes one constant sentence,
    never the child's text, so there is at most one line per known reason plus one, each capped in length."""
    by_why: dict[str, list[tuple[str, object]]] = {}
    for item in payload.get("unfixable") or []:
        if isinstance(item, dict) and item.get("file"):
            sentence = UNFIXABLE_WHY.get(str(item.get("why") or ""), UNKNOWN_UNFIXABLE)
            by_why.setdefault(sentence, []).append((str(item["file"]), item.get("line")))
    not_attempted: dict[str, list[tuple[str, object]]] = {}
    for item in payload.get("not_attempted") or []:
        if isinstance(item, dict) and item.get("file"):
            reason = NOT_ATTEMPTED_WHY.get(str(item.get("why") or ""), UNKNOWN_NOT_ATTEMPTED)
            not_attempted.setdefault(reason, []).append((str(item["file"]), item.get("line")))
    leftovers = _places(payload.get("leftovers"))
    tests = [(f, line) for f, line in _places(payload.get("tests")) if (f, line) not in updated]
    cassettes = [(f, line) for f, line in _places(payload.get("cassettes")) if f.lower().endswith((".yml", ".yaml"))]
    unread = unread_note(payload)
    if not (by_why or not_attempted or possibly or leftovers or tests or cassettes or unread):
        return []
    lines = ["", "**Left for you**"]
    links = lambda places: _links(full_name, sha, places, cap)  # noqa: E731
    for sentence, places in sorted(by_why.items()):
        lines.append(f"- Not changed, {sentence}: {links(places)}")
    for reason, places in sorted(not_attempted.items()):
        lines.append(f"- Not attempted ({reason}): {links(places)}")
    for field, spots in possibly or []:
        places: list[tuple[str, object]] = [(file, line) for file, line, _ in spots]
        lines.append(f"- {code(field)} {POSSIBLY_REMAINING}: {links(places)}")
    if tests:
        label = "Other tests that name the old field" if updated else "Tests that name the old field"
        lines.append(f"- {label}: {links(tests)}. A test that expects the request "
                     "this pull request changes fails until it is updated here; one that reads the field from a "
                     "Stripe response can stay.")
    if cassettes:
        lines.append(f"- Recorded responses to re-record: {links(cassettes)}. Their recorded requests send "
                     "the old field; a cassette matched on the request body stops replaying until it is re-recorded.")
    if leftovers:
        lines.append(f"- Still mentions the old name at {links(leftovers)}; check whether those are this field.")
    if unread:
        lines.append(f"- {unread}")
    return lines


def _kept(payload: dict, full_name: str, sha: str, cap: int = MAX_PR_LINKS) -> list[str]:
    """Old-key reads the fix left alone on purpose (``fixreads``): a webhook or response payload is tested there."""
    kept = _places(payload.get("kept"))
    return ["", f"Kept on purpose (reads of a webhook or response payload, unchanged): {_links(full_name, sha, kept, cap)}"] if kept else []
