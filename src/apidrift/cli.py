"""Command-line entry point: ``apidrift snapshot | list | diff``."""

from __future__ import annotations

import argparse
import os
import sys

from . import snapshot as snap
from .diff import BREAKING, diff_specs
from .openapi import load_spec
from .providers import PROVIDERS, get_provider
from .report import filter_changes, render_json, render_text


def cmd_snapshot(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    raw = snap.fetch(args.url or provider.spec_url)
    path = snap.save(provider, raw)
    if path is None:
        print(f"{provider.name}: spec unchanged since the last snapshot ({len(raw):,} bytes)")
    else:
        print(f"{provider.name}: saved {path} ({len(raw):,} bytes)")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    snaps = snap.list_snapshots(args.provider)
    if not snaps:
        print(f"no snapshots for {args.provider}; run: apidrift snapshot {args.provider}")
        return 1
    for path in snaps:
        print(path.name)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old_path = snap.resolve(args.provider, args.old)
    new_path = snap.resolve(args.provider, args.new)
    old_spec = load_spec(snap.load(old_path))
    new_spec = load_spec(snap.load(new_path))
    changes = filter_changes(diff_specs(old_spec, new_spec), min_severity=args.severity, path_prefix=args.path)
    old_label = f"{old_path.name} (version {old_spec.version})"
    new_label = f"{new_path.name} (version {new_spec.version})"
    render = render_json if args.json else render_text
    print(render(changes, old_label, new_label))
    return 1 if any(c.severity == BREAKING for c in changes) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apidrift", description="Detect breaking changes in third-party API specs.")
    sub = parser.add_subparsers(dest="command", required=True)
    providers = sorted(PROVIDERS)

    p_snap = sub.add_parser("snapshot", help="download the current spec and store it if it changed")
    p_snap.add_argument("provider", choices=providers)
    p_snap.add_argument("--url", help="fetch the spec from this URL instead of the provider default")
    p_snap.set_defaults(func=cmd_snapshot)

    p_list = sub.add_parser("list", help="list stored snapshots, oldest first")
    p_list.add_argument("provider", choices=providers)
    p_list.set_defaults(func=cmd_list)

    p_diff = sub.add_parser("diff", help="report changes between two snapshots or spec files (exit 1 if any are breaking)")
    p_diff.add_argument("provider", nargs="?", choices=providers, help="provider whose snapshots --old and --new name")
    p_diff.add_argument("--old", default="previous", help="file path, snapshot name or prefix, or 'previous' (default)")
    p_diff.add_argument("--new", default="latest", help="file path, snapshot name or prefix, or 'latest' (default)")
    p_diff.add_argument("--severity", choices=["breaking", "warning", "info"], default="info", help="lowest severity to show")
    p_diff.add_argument("--path", help="only changes touching endpoints under this prefix, e.g. /v1/charges")
    p_diff.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p_diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        # The reader (e.g. ``head``) closed early; that is not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
