# Database Migrations: Zero Downtime, Backfills, and Rollback

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## The Expand–Migrate–Contract Pattern

Schema changes are dangerous because code and schema deploy at different moments, and
during that window both the old and new application versions are live. The reliable
approach is to make every schema change backward compatible for at least one release, in
three separate deployments:

1. **Expand.** Add the new structure without removing anything: a new nullable column, a
   new table, a new index. The old code ignores it and keeps working.
2. **Migrate.** Deploy code that writes to both the old and new structures (dual write)
   while continuing to read from the old one. Backfill historical rows. Then switch reads
   to the new structure, verify, and keep dual writes in place as the escape hatch.
3. **Contract.** Once the old structure has been unread for a full release and confidence
   is established, stop writing it and drop it.

Compressing these into one deploy is what causes migration outages. Each phase is
independently deployable and independently revertible.

## Avoiding Locks and Downtime

Know which operations your database takes long or exclusive locks for, since this differs
by engine and version. Common hazards: adding a column with a non-constant default,
changing a column type, adding a constraint that validates existing rows, and building an
index without the concurrent option. On a large table any of these can block writes long
enough to cascade into connection-pool exhaustion and an application-wide outage.

Safer forms: add columns as nullable without a default and populate them separately; build
indexes concurrently; add constraints as not-valid first and validate in a second step;
set a short lock timeout so a migration that cannot acquire its lock fails fast instead of
queueing every subsequent query behind it. Rehearse migrations against a production-sized
copy — behavior on a small development table predicts nothing about a table with hundreds
of millions of rows.

## Backfill Strategy

Backfills should be batched, throttled, resumable, and idempotent. Process in bounded
chunks by primary key, commit each batch, and pause between batches so replication lag and
foreground query latency stay within budget. Record progress durably so an interrupted
backfill resumes rather than restarting.

Run backfills as a separate job, not inside the migration transaction that alters the
schema — a long-running transaction holds locks and bloats the write-ahead log. Monitor
replication lag throughout, and stop if it grows. After completion, verify with a
reconciliation query that counts mismatches between old and new representations before
switching reads.

## Rollback

Additive migrations are trivially reversible; destructive ones are not. Dropping a column
or rewriting values in place destroys the data needed to go back, which is precisely why
the contract phase is deferred until the new path is proven.

Every migration should be paired with a written down-path, and where the down-path cannot
restore data, that must be stated explicitly so the rollout is planned around it. Take a
verified backup before any destructive step and confirm the restore procedure is
exercised, not merely configured. Sequence the deploy so the application can tolerate both
the pre- and post-migration schema, which is what makes rolling the code back safe while
the schema stays forward.
