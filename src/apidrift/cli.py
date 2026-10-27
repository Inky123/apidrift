"""Command-line entry point: ``apidrift snapshot | list | diff | scan | impact``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import snapshot as snap
from .diff import BREAKING, diff_specs
from .impact import assess, render_impact_json, render_impact_text
from .openapi import Spec, load_spec
from .providers import PROVIDERS, get_provider
from .report import filter_changes, render_json, render_text
from .scanner import render_scan_json, render_scan_text, scan_repo
from .sdkmap import SdkIndex


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


def _spec(provider: str | None, ref: str) -> tuple[Path, Spec]:
    path = snap.resolve(provider, ref)
    return path, load_spec(snap.load(path))


def _label(path: Path, spec: Spec) -> str:
    return f"{path.name} (version {spec.version})"


def _require_sdk(index: SdkIndex, provider: str) -> None:
    if index.empty:
        raise SystemExit(f"the stored {provider} spec has no SDK method metadata; run: apidrift snapshot {provider}")


def cmd_scan(args: argparse.Namespace) -> int:
    specs = [_spec(args.provider, args.spec)[1]]
    if args.spec == "latest":
        try:  # calls to endpoints removed in the newest spec still resolve against the previous one
            specs.append(_spec(args.provider, "previous")[1])
        except SystemExit:
            pass
    index = SdkIndex.build(*specs)
    _require_sdk(index, args.provider)
    result = scan_repo(Path(args.repo), index)
    print(render_scan_json(result) if args.json else render_scan_text(result))
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    old_path, old_spec = _spec(args.provider, args.old)
    new_path, new_spec = _spec(args.provider, args.new)
    index = SdkIndex.build(old_spec, new_spec)
    _require_sdk(index, args.provider)
    changes = filter_changes(diff_specs(old_spec, new_spec), min_severity=args.severity)
    result = scan_repo(Path(args.repo), index)
    impacts = assess(result.usages, changes)
    old_label, new_label = _label(old_path, old_spec), _label(new_path, new_spec)
    if args.json:
        print(render_impact_json(result, impacts, old_label, new_label))
    else:
        print(render_impact_text(result, impacts, old_label, new_label, show_all=args.all))
    return 1 if any(i.severity == BREAKING for i in impacts) else 0


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

    p_scan = sub.add_parser("scan", help="list the API calls a checkout makes: endpoint, request fields, location")
    p_scan.add_argument("repo", help="directory (or single file) to scan")
    p_scan.add_argument("--provider", choices=providers, default="stripe")
    p_scan.add_argument("--spec", default="latest", help="snapshot or spec file to resolve SDK names against")
    p_scan.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p_scan.set_defaults(func=cmd_scan)

    p_imp = sub.add_parser("impact", help="which of a checkout's calls the changes between two specs hit (exit 1 if breaking)")
    p_imp.add_argument("repo", help="directory (or single file) to scan")
    p_imp.add_argument("--provider", choices=providers, default="stripe")
    p_imp.add_argument("--old", default="previous", help="file path, snapshot name or prefix, or 'previous' (default)")
    p_imp.add_argument("--new", default="latest", help="file path, snapshot name or prefix, or 'latest' (default)")
    p_imp.add_argument("--severity", choices=["breaking", "warning", "info"], default="warning", help="lowest severity to show")
    p_imp.add_argument("--all", action="store_true", help="also list call sites the upgrade does not hit")
    p_imp.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p_imp.set_defaults(func=cmd_impact)
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
