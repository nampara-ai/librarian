# Database Migrations

Librarian applies SQLite migrations from `src/librarian/storage/migrations` in lexical order.
Each applied filename is recorded in `schema_migrations`.

## Policy

- Name migrations with a zero-padded sequence and a short description: `0002_run_queue.sql`.
- Treat applied migrations as immutable. Add a new migration for follow-up changes.
- Migrations must be idempotent where practical using `IF NOT EXISTS`.
- Keep migrations small enough to review. Split unrelated storage changes.
- Run `librarian migrate` before starting API or workers in deployed environments.
- Tests must assert that new migrations are discovered and applied in order.

## Local Commands

```bash
librarian db-backup .librarian/backups/pre-migrate.sqlite
librarian migrate
librarian db-check
librarian db-stats
librarian db-maintain
sqlite3 .librarian/librarian.sqlite "select * from schema_migrations"
sqlite3 .librarian/librarian.sqlite "pragma journal_mode; pragma busy_timeout;"
```

## Recovery

If a migration fails locally, restore from the backup or remove the incomplete database and rerun
`librarian migrate`. Do not manually insert rows into `schema_migrations` unless you have verified
the schema state.

```bash
librarian db-restore .librarian/backups/pre-migrate.sqlite --yes
librarian db-check
```
