# pi-harness tests — run discipline

Short version: **these suites reset a real MySQL database.** Before running one,
say so in the terminal; if a second runner is active, coordinate instead of
racing.

`npm test` runs everything and needs that database. `npm run test:ci` runs only
the suites that were measured to pass with every external dependency
unreachable — the list, and the justification for each file, is in
`vitest.ci.config.ts`. CI uses the latter; a local "all green" claim must come
from the former.

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

### Why this is coordination, not configuration

`fileParallelism: false` in `vitest.config.ts` keeps the files *inside* one
vitest run serial, but it cannot help when two runners — or vitest and pytest —
share a database name. That is the failure the closeout review reproduced:
running several suites against one `smartcs_phase1_test` produced a Phase 7
`403` that disappeared on a solo re-run of the same file (`4/4 PASS`),
and holding a Python model test and the Pi fault matrix at once produced
`os error 1455 页面文件太小`. Neither was a code regression.
The rule that follows is the one the phase reports already carried:

> heavy suites (integration, crash matrix, anything loading CrossEncoder)
> run **one at a time**, never concurrently.

### Per-suite database suffixes (not yet enabled — needs one-time grants)

The robust fix is one database per suite, so no coordination is needed at all:

```bash
SMARTCS_TEST_DATABASE=smartcs_phase1_test_outbox  npx vitest run tests/phase10-memory-outbox.test.ts
SMARTCS_TEST_DATABASE=smartcs_phase1_test_phase6  npx vitest run tests/phase6-observability.test.ts
```

Both suites already read the name from `SMARTCS_TEST_DATABASE` and reset only
that database, so the scheme needs **no code change** — only privileges.
It is deliberately not enabled by default, because the repository's MySQL user
**cannot `CREATE DATABASE`** (measured in Phase 6b: the account holds grants on
the specific schemas it was created for, not `ALL PRIVILEGES`). A fixture that
created its own database would therefore fail on a clean machine.

To turn it on, a one-time grant by whoever owns the MySQL server (here, the
compose instance on `:3307`):

```bash
# 1. as root, allow the app user to create/drop ONLY its own test schemas
docker exec -i <mysql-container> mysql -uroot -p <<'SQL'
CREATE USER IF NOT EXISTS 'smartcs'@'%' IDENTIFIED BY '<the same MYSQL_PASSWORD>';
GRANT ALL PRIVILEGES ON `smartcs\_phase1\_test%`.* TO 'smartcs'@'%';
FLUSH PRIVILEGES;
SQL

# 2. create the per-suite schemas once (the fixture does NOT create them)
for suffix in outbox phase6 crash; do
  docker exec -i <mysql-container> mysql -uroot -p \
    -e "CREATE DATABASE IF NOT EXISTS smartcs_phase1_test_$suffix CHARACTER SET utf8mb4"
done

# 3. verify with the app user, not root
MYSQL_PWD=<password> mysql -h127.0.0.1 -P3307 -usmartcs \
  -e "SHOW DATABASES LIKE 'smartcs_phase1_test%'"
```

The grant is scoped with a `%` wildcard so the user can still only reach
schemas whose name starts with `smartcs_phase1_test` — it does not widen access
to the development database (`smartcs_checkpoint`). Steps 2–3 are the part that
must be run by a human with server credentials; nothing in the test suite
performs them.

## Credentials

Fixtures never assume the caller's shell is configured. `MYSQL_*` is resolved
the same way the application resolves it — process environment first, then
the repository-root `.env` — and handed to spawned services explicitly
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
