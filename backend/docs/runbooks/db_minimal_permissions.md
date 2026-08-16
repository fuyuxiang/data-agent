# DB Minimal Permissions (P0-06)

## Why two layers?

The application uses **two independent defenses** to prevent an attacker
who has bypassed the AST guardrails from writing to the warehouse:

| Layer | Mechanism | Configuration |
|---|---|---|
| 1. Connection-level | `default_transaction_read_only=on` is sent to PostgreSQL on every connection | `backend/app/core/db.py` calls `_build_sample_engine(read_only=True)` for `sample_engine` |
| 2. Role-level | `data_agent_reader` role has only `SELECT` privileges | `backend/scripts/create_reader_role.sql` |

If either layer fails (e.g. a deployer misconfigures the connection
options, or the role is misconfigured), the **other** still rejects
writes. Belt-and-braces.

## What the integration tests verify

The tests in `tests/integration/test_db_minimal_permissions.py` prove
that layer 1 is real. With a real Postgres reachable, **all 6 write
operations must fail**:

| Test | What it does |
|---|---|
| `test_select_succeeds` | The primary use case still works |
| `test_insert_raises` | INSERT must fail with a read-only error |
| `test_update_raises` | UPDATE must fail |
| `test_delete_raises` | DELETE must fail |
| `test_create_table_raises` | DDL must fail |
| `test_drop_table_raises` | DDL must fail |
| `test_five_consecutive_connections_all_read_only` | Each new connection inherits the read-only option, not just the first |

The role-level tests in `tests/integration/test_role_grants.py`
verify that the role **exists** with the right privileges. They
**skip gracefully** if the role has not been provisioned yet.

## Deployment checklist

1. **Layer 1 is automatic** — see `app/core/config.py:SAMPLE_DATABASE_URL`
2. **Layer 2 requires admin work**:
   ```bash
   PGPASSWORD=admin_pw psql -h db.example.com -p 5432 -U admin -d sample \
       -f scripts/create_reader_role.sql
   ```
3. **Verify the role exists**:
   ```bash
   psql -h db.example.com -p 5432 -U admin -c "\du data_agent_reader"
   ```
   Expect: `data_agent_reader | Cannot login` (initially) or `Can login`
   (after script run).
4. **Run the integration tests**:
   ```bash
   pytest tests/integration/ -v
   ```
   Expect: 8 passed, 3 skipped (skip means role missing).

## CI integration

The integration tests are skipped when no DB is available — they do
not block CI in environments without Postgres. They run cleanly when
DB is reachable, including the developer machine used to develop them.

## Rollback

If the read-only enforcement somehow regresses (e.g. an attacker slips
a write through), the symptom is a successful write — visible in
`test_insert_raises` failing. The fix is to re-inspect the connection
options in `app/core/db.py` and verify `default_transaction_read_only=on`
appears in the connect_args string.
