"""CLI commands of the GitHub App: ``serve``, ``worker``, ``check``, ``status`` and ``app-setup``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
from pathlib import Path

from .config import DEFAULT_HOST, REQUIRED, from_env, load_dotenv

DEFAULT_MAX_MEM_MB = 1024


def add_parsers(sub: argparse._SubParsersAction) -> None:
    p_serve = sub.add_parser("serve", help="run the web process: pages and the GitHub webhook")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    p_worker = sub.add_parser("worker", help="poll Stripe for releases and run queued checks")
    p_worker.add_argument("--once", action="store_true", help="one poll and one drain of the queue, then exit")
    p_worker.set_defaults(func=cmd_worker)

    p_check = sub.add_parser("check", help="check a repository's Stripe calls against the stored snapshots")
    mode = p_check.add_mutually_exclusive_group(required=True)
    mode.add_argument("--job", type=int, metavar="N", help="run queued job N in this process")
    mode.add_argument("--repo", metavar="OWNER/NAME", help="queue a check for an installed repository")
    mode.add_argument("--all", action="store_true", help="queue a check for every installed repository")
    mode.add_argument("--stale", action="store_true", help="queue a check for repositories not checked since the latest snapshot")
    mode.add_argument("--local", metavar="DIR", help="scan a local checkout; needs no GitHub configuration")
    p_check.add_argument("--now", action="store_true", help="with --repo: run the check right away")
    p_check.add_argument("--old", default="previous", help="baseline snapshot for --local (default: previous)")
    p_check.add_argument("--new", default="latest", help="target snapshot for --local (default: latest)")
    p_check.add_argument("--json", action="store_true", help="with --local: emit the check payload as JSON")
    p_check.add_argument(
        "--max-mem-mb", type=int, default=DEFAULT_MAX_MEM_MB, help="with --local: RLIMIT_AS cap on Linux; 0 for none"
    )
    p_check.set_defaults(func=cmd_check)

    p_status = sub.add_parser("status", help="installations, repos, issues, recent jobs and the last Stripe poll")
    p_status.add_argument("--hours", type=int, default=24, help="how far back to list jobs (default 24)")
    p_status.set_defaults(func=cmd_status)

    p_setup = sub.add_parser("app-setup", help="print the GitHub App registration checklist and validate .env")
    p_setup.set_defaults(func=cmd_app_setup)


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class HealthzFilter(logging.Filter):
    """Drop uvicorn access-log lines for the load balancer's ``/healthz`` probes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def cmd_serve(args: argparse.Namespace) -> int:
    settings = from_env()
    _logging()
    import uvicorn

    from .web import create_app

    logging.getLogger("uvicorn.access").addFilter(HealthzFilter())
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1, log_level="info")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    settings = from_env()
    _logging()
    from .worker import loop

    loop(settings, once=args.once)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if args.local:
        return _check_local(args)
    settings = from_env()
    _logging()
    from . import db
    from .github import GitHubClient
    from .worker import run_one

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    if args.all:
        queued = sum(1 for repo in db.active_repos(conn) if db.enqueue(conn, "check", repo["id"], "manual"))
        print(f"queued {queued} checks")
        return 0
    if args.stale:
        from .worker import sweep

        print(f"queued {sweep(settings, conn)} checks for stale repositories")
        return 0
    if args.job is not None:
        job = db.claim_job(conn, job_id=args.job)
        if job is None:
            raise SystemExit(f"job {args.job} is not queued")
        print(run_one(job, conn, GitHubClient(settings), settings))
        return 0
    repo = db.find_repo(conn, args.repo)
    if repo is None:
        raise SystemExit(f"{args.repo} is not an installed repository")
    job_id = db.enqueue(conn, "check", repo["id"], "manual") or db.queued_for(conn, repo["id"])
    if not args.now:
        print(f"queued check {job_id} for {repo['full_name']}")
        return 0
    job = db.claim_job(conn, job_id=job_id)
    if job is None:
        raise SystemExit(f"job {job_id} was claimed by the worker first")
    print(run_one(job, conn, GitHubClient(settings), settings))
    return 0


def _check_local(args: argparse.Namespace) -> int:
    from ..impact import render_impact_text
    from .checker import limit_memory, run_local

    limit_memory(args.max_mem_mb)  # first action of the sandboxed child; a no-op off Linux
    # No load_dotenv here: the sandboxed child only needs APIDRIFT_DATA, which the parent passes in its environment,
    # and it must not read the App's secrets from a .env in the cwd.
    check = run_local(Path(args.local), args.old, args.new)
    if args.json:
        print(json.dumps(check.payload(), indent=2))
    else:
        print(render_impact_text(check.result, check.impacts, check.old_label, check.new_label))
    return 1 if check.breaking else 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print what an operator asks first: is anything installed, checked, failing, and is Stripe being polled."""
    settings = from_env()
    from . import db

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    try:
        print(render_status(db.status_summary(conn, hours=args.hours), hours=args.hours))
    finally:
        conn.close()
    return 0


def render_status(summary: dict, hours: int = 24) -> str:
    lines = [f"installations: {summary['installations']} active, {summary['suspended_installations']} suspended"]
    repos = summary["repos"] or {}
    lines.append("repos: " + (", ".join(f"{n} {state}" for state, n in sorted(repos.items())) or "none"))
    lines.append(f"open issues: {summary['open_issues']}")
    jobs = summary["jobs"]
    lines.append(f"jobs (last {hours} h): {jobs['queued']} queued, {jobs['running']} running, {jobs['done']} done, {jobs['failed']} failed")
    for err in summary["job_errors"]:
        lines.append(f"  job {err['id']} repo {err['repo_id']} {err['status']}: {err['error']}")
    lines.append(f"last poll: {summary.get('last_poll_at') or 'never'} ({summary.get('last_poll_status') or 'no status'})")
    lines.append(f"latest snapshot: {summary.get('latest_snapshot') or 'none'}")
    return "\n".join(lines)


def cmd_app_setup(args: argparse.Namespace) -> int:
    load_dotenv()
    host = os.environ.get("APIDRIFT_HOST", DEFAULT_HOST)
    base = f"https://{host}"
    print("Register the GitHub App at https://github.com/settings/apps/new with these values:")
    print("  GitHub App name:      apidrift (any free name; its slug becomes APIDRIFT_GITHUB_APP_SLUG)")
    print(f"  Homepage URL:         {base}/")
    print(f"  Setup URL:            {base}/installed   (tick 'Redirect on update')")
    print(f"  Webhook URL:          {base}/github/webhook   (webhook active)")
    print(f"  Webhook secret:       {secrets.token_hex(32)}   (generated now; if .env already has one, keep that)")
    print("  Repository permissions:")
    print("    Contents: Read-only")
    print("    Issues: Read and write")
    print("    Metadata: Read-only (granted automatically)")
    print("  Subscribe to events:  Push only (installation events reach every App automatically)")
    print("  Where can it be installed: Any account")
    print("Then: generate a private key, download the .pem, note the App ID and the slug, and write .env")
    print("next to compose.yaml with these lines (compose mounts the key at /secrets/app.pem):")
    print("  APIDRIFT_GITHUB_APP_ID=<App ID from the settings page>")
    print("  APIDRIFT_GITHUB_APP_SLUG=<slug: github.com/apps/<slug>>")
    print("  APIDRIFT_GITHUB_WEBHOOK_SECRET=<the webhook secret above>")
    print("  APIDRIFT_GITHUB_PRIVATE_KEY_PATH=/secrets/app.pem")
    print(f"  APIDRIFT_HOST={host}")
    print("  APIDRIFT_DATA=/data")
    print("  APIDRIFT_POLL_MINUTES=30")
    print()
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    for name in REQUIRED:
        print(f"  {name}: {'set' if name not in missing else 'MISSING'}")
    ok = not missing
    key_path = Path(os.environ.get("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", ""))
    if key_path.is_file():
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            load_pem_private_key(key_path.read_bytes(), password=None)
            print(f"  private key: OK ({key_path})")
        except (ValueError, TypeError) as exc:
            print(f"  private key: unreadable ({exc})")
            ok = False
    else:
        print("  private key: file not found" + (f" ({key_path})" if str(key_path) else ""))
        ok = False
    if ok:
        from_env()
        print("configuration OK")
    return 0 if ok else 1
