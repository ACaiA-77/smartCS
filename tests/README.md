# pi-harness tests — run discipline

Short version: **these suites reset a real MySQL database.** Before running one,
say so in the terminal; if a second runner is active, coordinate instead of
racing.

## The shared database

Both suites talk to a real MySQL instance and both reset it with
`DROP TABLE … / CREATE TABLE … / migrations`:

| suite | where the name comes from | default |
|---|---|---|
| `pi-harness` (vitest) | `tests/helpers/phase1.ts` → `SMARTCS_TEST_DATABASE` | `smartcs_phase1_test` |
| `python-impl` (pytest) | `tests/internal_api_helpers.py` → `SMARTCS_TEST_DATABASE` | `smartcs_phase1_test` |

The same variable drives both sides, so **two runners must not share a database
name** unless they have agreed on a window. Two suites running concurrently
against one database produce failures that look like real bugs and are not:
`Failed to open the referenced table 'platform_user'` (the other runner dropped
the parent table between our `CREATE TABLE` statements), `Duplicate column name
…` (two runners applying migration 002 at once), `Duplicate entry '<username>'`
(stale rows from the other runner's seed).

Window discipline:

1. Before starting a suite that touches the database, state in the terminal
   which database you are taking and for roughly how long.
2. Do not start a second suite against the same database until the first is
   finished.
3. Use your own database when you can (below) — that removes the coordination
   cost entirely.

## Running against your own database

```bash
SMARTCS_TEST_DATABASE=smartcs_<runner>_verify npx vitest run        # ts fixtures
SMARTCS_TEST_DATABASE=smartcs_<runner>_verify python -m pytest -q   # python fixtures
```

The database must already exist and the MySQL user needs full privileges on it
(the repository's MySQL user only has grants for the databases it was created
for, so a new name has to be created by whoever owns the server).

## Credentials

Fixtures never assume the caller's shell is configured. `MYSQL_*` is resolved
the same way the application resolves it — process environment first, then
`python-impl/.env` — and handed to spawned services explicitly
(`childMysqlEnv()` in `tests/helpers/phase1.ts`). A missing password fails in
the fixture with a message naming the variable, instead of starting a runtime
that cannot reach its platform database.

With live writes enabled (`SMARTCS_WRITE_MODE=live`) the fixture also probes the
platform schema at startup (`platform_user`, `conversation_session`,
`agent_run_receipt`, `pending_action`) and refuses to start if it is missing —
a wrong database must be loud, not silently degrade the two-phase flow.

## Seeds are idempotent

`seedAccount` / `seedSession` remove their own residue before inserting, because
`platform_user.username`, `platform_user.business_user_id` and
`conversation_session.session_id` are unique keys: an aborted earlier run would
otherwise make the next run fail with a duplicate-entry error unrelated to the
behaviour under test.
