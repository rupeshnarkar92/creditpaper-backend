# Folder batch uploads

Drops containing multiple files are packaged as one ZIP in the browser. Paths
inside the folder are retained for matching. ZIP entries are stored without
recompression to avoid spending CPU recompressing PDFs and images.

Limits: 1,000 files per folder, 250 MB per browser-built ZIP, 256 MB archive on the server,
512 MB expanded content, and 50 MB per document. Larger folders are automatically
split into sequential ZIP batches, all registered before the first transfer.
Unsafe paths, symlinks, encrypted ZIP entries, duplicate paths, and excessive
expansion ratios are rejected. Files are read in memory, never extracted using
archive-supplied filesystem paths.

The frontend registers a tenant-scoped batch before transfer. After the encrypted
archive is stored and its durable `archive_import` job is committed, the API
returns HTTP 202. A worker imports one document at a time, persisting per-file
outcomes; existing parse workers retain their bounded concurrency. A separate
`archive_match` job waits for imports and parsing to finish before proposing slots.
Slot confirmation remains an analyst action.

The upload promise survives navigation inside the app. Closing or refreshing the
browser before HTTP 202 can interrupt transfer; network uploads are not resumable.
After HTTP 202, browser closure is safe. Worker restarts resume at stored
checkpoints. Replaying after a version commit is handled by duplicate detection.
An interrupted transfer can be acknowledged after its five-minute heartbeat expires.

Facts and Credit Paper remain locked while the batch is open or documents/jobs
are pending, and failures require acknowledgement or retry. Per-file import failures
and progress are available on the workspace after refresh. Retry incomplete import
reuses the encrypted archive and skips successful items. Parsing failures use the
existing document reprocessing action. Acknowledgements are recorded in the audit log.

## Deployment

Apply Alembic through `nn_139_archive_ingestion`. Restart both API and worker with
the new code. Hosted environments must run `uv run python scripts/process_jobs.py
--all-tenants --concurrency 4 --per-tenant 2` under a supervisor. Local environments
run the same command in a second terminal, from the repository root. The API
defaults to `PROCESSING_WORKER_MODE=external` and never processes jobs itself.
The worker forces external mode so nested dispatch cannot exceed its pool limit.
Stop with Ctrl+C; unfinished work is recovered after its lease expires. If no
worker is running, uploads stay safely queued and the generation tabs stay locked.
Restart the API once when switching from the old inline mode. Keep the worker
running independently of API reloads. `PROCESSING_WORKER_MODE=inline` is only a
legacy local/test opt-in. API and workers must share the same durable document volume and
encryption key versions. Archives are retained encrypted for retries; do not remove
them while batches are pending or retryable.

`uv run python scripts/verify_archive_ingestion.py` checks routines using the
runtime role and rolls back all fixture changes. It requires permission to assume
`creditpaper_runtime`. `--current-role` runs functional checks only and does not
claim tenant-isolation verification.
