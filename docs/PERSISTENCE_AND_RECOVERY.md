# State Persistence, Concurrency & Recovery Model

This document outlines the state persistence architecture, database concurrency guarantees, restart recovery procedures, and backup/restore workflows for the Remote Desktop application.

---

## 1. Database Architecture & Concurrency Model

### Target Architecture: Single-Node SQLite with WAL Mode
The application default persistence engine is **SQLite 3** configured with:
- **`PRAGMA journal_mode = WAL`** (Write-Ahead Logging): Allows concurrent readers to query the database simultaneously without blocking writer transactions and without writer starvation.
- **`PRAGMA synchronous = NORMAL`**: Balances high transactional throughput with crash resilience.
- **`PRAGMA foreign_keys = ON`**: Enforces referential integrity cascades across all tables.

### Concurrency Characteristics & Limits
- **Single-Host Daemon / Container Model**: SQLite is designed for single-node vertical deployments where the FastAPI instance mounts `/data/rd_app.db` via a persistent volume.
- **Scale-Up Target**: Handles up to hundreds of concurrent WebRTC viewing sessions and control arbitrations on a single physical host without DB locks.
- **Scale-Out / Multi-Host Clustered Deployments**: For multi-node distributed deployments across multiple distinct host instances, standard SQLAlchemy connection configuration allows setting `DATABASE_URL=postgresql://user:pass@host:5432/rd_db` with zero code modifications.

---

## 2. Persistent State Domains

The following persistent states are guaranteed across process, server, and container restarts:

| Entity | Model | Persistence Location | Restart Invariant |
| :--- | :--- | :--- | :--- |
| **Users & Credentials** | `User` | SQLite (`/data/rd_app.db`) | Hashed passwords and role assignments remain intact. |
| **Session Metadata** | `UserSession` | SQLite (`/data/rd_app.db`) | Refresh token hashes, creation dates, expiration timestamps, and revocations remain preserved. |
| **File Metadata** | `FileRecord` | SQLite (`/data/rd_app.db`) | File IDs, filenames, sizes, MIME types, and uploader IDs remain preserved. |
| **Audit Logs** | `AuditEvent` | SQLite (`/data/rd_app.db`) | Complete append-only audit event trail is retained across restarts. |
| **Physical Files** | Binary blobs | Filesystem (`/app/uploads/`) | Raw uploaded files persist on the mounted `/app/uploads` volume with ownership boundaries enforced at runtime. |

---

## 3. Restart Recovery & Resource Invariants

When the backend process restarts:
1. **No Orphan WebRTC Peers**: Previous peer connections held in memory are cleared; active WebRTC peer count is initialized to `0`.
2. **Deterministic Capture Teardown**: Capture worker threads from prior runs do not leak; active capture worker count initializes to `0` and only instantiates on-demand when a viewer connects.
3. **Control Session Arbitration Reset**: Controller token is cleared on shutdown and re-arbitrated fairly among active clients upon reconnect.
4. **Client Self-Healing**: WebRTC client reconnection state machines handle backend restarts by retrying ICE negotiation and acquiring fresh stream feeds automatically.

---

## 4. Backup, Verification & Disaster Recovery

The system includes built-in atomic backup and restore capabilities via [`app/services/backup.py`](file:///d:/projects/RD/backend/app/services/backup.py) and the CLI tool [`scripts/backup_restore.py`](file:///d:/projects/RD/backend/scripts/backup_restore.py).

### Creating a Backup
```bash
python scripts/backup_restore.py backup --dir backups/
```
- Performs a live snapshot of the SQLite database using the `sqlite3.backup()` API.
- Traverses and bundles all files in the `uploads/` directory.
- Generates a `manifest.json` containing SHA256 checksums and UTC timestamps for every file.
- Bundles everything into an atomic `.zip` archive.

### Verifying Backup Integrity
```bash
python scripts/backup_restore.py verify backups/rd_backup_20260924_151203.zip
```
- Verifies archive structure.
- Checks cryptographic SHA256 hashes of all payloads against `manifest.json`.
- Runs SQLite `PRAGMA integrity_check` on the embedded database file.

### Restoring from Backup
```bash
python scripts/backup_restore.py restore backups/rd_backup_20260924_151203.zip
```
- Validates the archive integrity before performing any filesystem modifications.
- Atomically extracts and restores the SQLite database and all uploaded files to their target locations.
