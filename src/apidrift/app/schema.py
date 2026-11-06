"""The database schema, as the list of migrations ``db.migrate`` applies in order. Never edit one that shipped:
append a new one.
"""

MIGRATIONS: list[str] = [
    """
    CREATE TABLE installations (
        id INTEGER PRIMARY KEY,
        account TEXT NOT NULL,
        suspended INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE repos (
        id INTEGER PRIMARY KEY,
        installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE CASCADE,
        full_name TEXT NOT NULL,
        baseline TEXT,
        last_sha TEXT,
        last_result TEXT,
        last_checked_at TEXT,
        issue_number INTEGER,
        issue_state TEXT CHECK (issue_state IN ('open', 'closed_by_us', 'closed_by_user')),
        issue_fingerprint TEXT,
        issue_items TEXT,
        issue_body_sha TEXT,
        issue_version TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX repos_installation ON repos(installation_id);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('check', 'poll')),
        repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
        reason TEXT NOT NULL CHECK (reason IN ('install', 'push', 'release', 'manual')),
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        not_before TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error TEXT
    );
    CREATE UNIQUE INDEX jobs_one_queued ON jobs(kind, repo_id) WHERE status = 'queued';
    CREATE INDEX jobs_claim ON jobs(status, not_before);
    CREATE TABLE deliveries (
        id TEXT PRIMARY KEY,
        event TEXT NOT NULL,
        received_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    ALTER TABLE repos ADD COLUMN gone INTEGER NOT NULL DEFAULT 0;
    """,
    # the one-time "apidrift cannot read <language> here yet" issue, so it is never posted twice
    """
    ALTER TABLE repos ADD COLUMN unread_issue INTEGER;
    """,
    # the fix pull request: its number, branch, the commit apidrift last pushed, what it fixed, and where it stands
    """
    ALTER TABLE repos ADD COLUMN pr_number INTEGER;
    ALTER TABLE repos ADD COLUMN pr_branch TEXT;
    ALTER TABLE repos ADD COLUMN pr_head_sha TEXT;
    ALTER TABLE repos ADD COLUMN pr_fingerprint TEXT;
    ALTER TABLE repos ADD COLUMN pr_items TEXT;
    ALTER TABLE repos ADD COLUMN fix_state TEXT;
    """,
    # the commit apidrift built but has not yet confirmed on the branch (a failed ref update must not read as "diverged")
    """
    ALTER TABLE repos ADD COLUMN pr_pending_sha TEXT;
    """,
    # the index of a repo's Stripe call sites at ``sha`` (inventory.py): a release is assessed from it, without the code
    """
    CREATE TABLE repo_index (repo_id INTEGER PRIMARY KEY REFERENCES repos(id) ON DELETE CASCADE, sha TEXT NOT NULL,
        format INTEGER NOT NULL, scanner TEXT NOT NULL, job_id INTEGER, extras TEXT NOT NULL, written_at TEXT NOT NULL,
        checked TEXT, checked_at TEXT, checked_by TEXT);
    CREATE TABLE index_sites (repo_id INTEGER NOT NULL REFERENCES repo_index(repo_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
        call TEXT NOT NULL, language TEXT NOT NULL, complete INTEGER NOT NULL, fields TEXT NOT NULL, hidden TEXT,
        PRIMARY KEY (repo_id, seq));
    CREATE INDEX index_sites_endpoint ON index_sites(method, path);
    """,
    # one index per repo and provider (format 3), with the values a call sends as hashes (``sent``): the format-2
    # indexes are Stripe's and would not be used again, so they go; each repo's next full check writes the new one
    """
    DROP TABLE index_sites;
    DROP TABLE repo_index;
    CREATE TABLE repo_index (repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE, provider TEXT NOT NULL,
        sha TEXT NOT NULL, format INTEGER NOT NULL, scanner TEXT NOT NULL, job_id INTEGER, extras TEXT NOT NULL,
        written_at TEXT NOT NULL, checked TEXT, checked_at TEXT, checked_by TEXT, PRIMARY KEY (repo_id, provider));
    CREATE TABLE index_sites (repo_id INTEGER NOT NULL, provider TEXT NOT NULL,
        seq INTEGER NOT NULL, file TEXT NOT NULL, line INTEGER NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
        call TEXT NOT NULL, language TEXT NOT NULL, complete INTEGER NOT NULL, fields TEXT NOT NULL, hidden TEXT,
        sent TEXT, PRIMARY KEY (repo_id, provider, seq),
        FOREIGN KEY (repo_id, provider) REFERENCES repo_index(repo_id, provider) ON DELETE CASCADE);
    CREATE INDEX index_sites_endpoint ON index_sites(provider, method, path);
    """,
]
