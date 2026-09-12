# Admin maintenance console

The circular maintenance workflow is available at `/admin`: Overview, Mirror,
Documents, AI analysis, Search index, and Jobs. Existing corpus statistics (including
laws), index diagnostics, sync/EcoData controls, and full run history retain their
original routes. Users and deployment settings remain separate administration tools.

## Recovering attachments after a mirror batch

Open the completed batch's **Audit this batch and fetch missing attachments** link,
or select that job in Documents → Source batch. This uses saved circular IDs across
departments and years. Choose **Fetch and index missing attachments**, preview the
selection, and start. A scan discovers links, retries known missing/error files,
refreshes keyword search, and indexes extracted attachments. A previously scanned
circular with no attachments is complete; one never scanned is not.

Unsupported formats and files yielding no text are visible limitations, not endlessly
retryable work. Missing body text has its own recovery action. Re-fetching files cannot
recover an upstream file that SBP no longer provides.

## Analysis and indexing

AI feature counts open eligible-document lists. Generation accepts existing legacy
values as complete, as the existing generation service does. A valid empty result is
complete. Features are explicit, and jobs execute feature-first across the selection;
relationships precede consolidation. Consolidation eligibility belongs to existing
chain bases, with oversized chains blocked. Run another assessment after relationships
create new chains. Generation timestamps do not prove freshness after source changes;
source-versioned AI invalidation is not implemented by this console.

Recorded index status compares the ledger's text hash, embedding fingerprint, and
chunker version to current text/configuration. **Verify search indexes** additionally
compares FTS content and vector chunk IDs/passages with expected contents. Verification
does not write the ledger. It does not validate numeric embedding quality. Repairs use
stored text, touch affected sources, and verify their result. Configuration-wide vector
mismatch blocks targeted repairs: use the existing controlled rebuild procedure.

## Batch contract

Read endpoints are `/api/admin/maintenance/documents` and `/jobs[/id]`. Writes are
explicit POSTs at `/api/circulars/maintenance/jobs` and `/jobs/{id}/cancel` in `main.py`.
Every route requires admin access. Admin GET routes remain read-only.

The server rechecks eligibility, freezes IDs in `SyncStatus.selection`, and persists
per-item outcomes in `progress`. AI items additionally name the feature. Batch size
groups progress; the selected list is finite even for “process all”. A single worker
runs inside the application under its circular writer lock and maintenance gate.
Request spacing is shared by attachment requests. Closing the browser does not cancel
work. Cancel finishes the active item; retry selects unfinished/failed IDs and rechecks
eligibility. Restart marks unfinished jobs interrupted and preserves completed items.

The new workbenches operate on circulars and their attachments. Law-specific batch
maintenance, automatic end-to-end scheduled pipelines, explicit AI regeneration with
source fingerprints, and a UI for global vector rebuilds remain outside this delivery.
Existing law operations and diagnostics remain available through their existing paths.
