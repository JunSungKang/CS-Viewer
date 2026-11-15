# Operations guide

This guide covers configuration, retention, log retrieval, and recovery for a single-host CS-Viewer deployment. Follow the [quick start](../README.md#quick-start) to provision the database and start the application. See [Architecture](architecture.md) for component boundaries and [Validation](validation.md) for development checks.

Run the commands below from the repository root with Python 3.14 or newer. The examples use the ignored `.local` directory introduced in the quick start; replace paths and module names with your own values.

## Deployment and credentials

ClickHouse Server is required and must be provisioned separately. The host agent accesses the selected Docker Engine and ClickHouse; the web service calls only the agent's limited API. Do not give the web container database credentials or the Docker socket.

Use a ClickHouse application account with `SELECT` and `INSERT` access to the application database. Keep a separate administrator account for initialization and retention changes. Store connection details in untracked JSON files inside the configured workspace, with file permissions restricted to the operating user.

Keep ClickHouse reachable through a private or otherwise protected connection. For a containerized database, provide persistent storage and manage its data, configuration, temporary files, and logs explicitly. The repository does not provide the operator's database container, credentials, or backup service.

CS-Viewer has no built-in web authentication or role-based access control. Anyone who can reach the web service can inspect the managed inventory and request lifecycle actions. Use loopback for local evaluation, or an authenticated reverse proxy or another trusted access boundary for shared deployments. TLS termination and certificate renewal are deployment responsibilities.

## Agent configuration

Start with [config.example.json](config.example.json) and the [configuration instructions](../README.md#3-configure-the-host-agent). Replace its inventory, image references, socket, daemon identity, and paths with values from your own environment.

`clickhouse_config`, `spool_path`, `log_state_path`, and `lock_path` must resolve inside `workspace_root`; use absolute paths. Run one agent for a given state directory. Deployment tools that manage the same containers must share the configured operations lock and preserve its inode rather than replacing the lock file.

The default collection settings are:

| Setting | Default | Purpose |
| --- | --- | --- |
| `interval` | 10 seconds | Collect service measurements and current inventory. |
| `log_poll_seconds` | 10 seconds | Poll registered containers' Docker logs. |
| `flush_seconds` | 30 seconds | Flush queued metrics and logs to ClickHouse. |
| `spool_limit_bytes` | 1 GiB | Bound the durable file outbox. |

The dashboard keeps the most recent 60 measurements per module in memory. When ClickHouse is available after restart, it restores up to 60 measurements per module from the preceding day. This display window does not limit the history retained in ClickHouse. Storage errors, log collection errors, and queued bytes are reported separately.

### Port display labels

Each service inventory entry may contain a `port_labels` object. Its keys identify **container listeners**, such as `3000/tcp`, rather than published host ports. A key must use a port from 1 to 65535 and one of `tcp`, `udp`, or `sctp`.

| Field | Values | Meaning |
| --- | --- | --- |
| `title` | Required, nonblank string of 1–80 characters. | Human-readable service or listener name. |
| `kind` | `http`, `https`, `redirect`, or `other`; defaults to `other`. | Declared purpose of this listener. |
| `group` | `entry` or `service`; defaults to `service`. | Public directory section. |
| `detail` | Optional string of up to 240 characters. | Additional operational explanation. |
| `redirect_to` | Integer from 1 to 65535, required only for `redirect`. | Destination **host port**, not a container listener number. |

Unknown fields and a `redirect_to` value on a non-redirect label are rejected. Labels describe existing behavior; they do not configure listeners, redirects, TLS, or firewall rules.

The agent attaches labels only to bindings currently published by Docker. Adding a label cannot create a port entry. The dashboard groups a redirect with its observed destination when the owner, binding address, transport, title, and section agree. Without matching metadata, it preserves the actual binding rather than guessing an application protocol or purpose from a port number. Host-only access is determined from the actual loopback binding address, not from a label.

## Retention policy

All three tables default to three calendar years:

| Table | Data |
| --- | --- |
| `service_samples` | Typed service measurements. |
| `container_logs` | Raw Docker logs and their source metadata. |
| `action_events` | Append-only lifecycle intent and result events. |

The administration CLI accepts an integer retention period from **3 to 100 years** for each table. Expiration uses `received_at`, the time CS-Viewer received the record, with calendar-year arithmetic. Monthly partitions and whole-part expiration can retain records longer than the configured minimum; the policy is not a precise per-row deletion deadline.

Inspect the actual server policy, preview a change, and apply it explicitly:

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json status
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5 --apply
```

The preview reports the old and proposed periods and the number of rows already eligible under the proposed policy. Review it before shortening retention. A longer period cannot restore deleted data.

The CLI changes TTL without immediately materializing it across the full table. The agent reads the server's actual TTL and does not shorten an expanded policy on restart. An unrecognized policy or a direct server change below three years is reported as a storage error.

## Retrieve raw logs

Raw logs are available through the local administration CLI using ClickHouse credentials. The web service does not expose a raw-log retrieval endpoint.

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-app.json logs --module your-module --limit 100
```

The default interval is the last 15 minutes. For a stable, repeatable range, provide timezone-qualified ISO timestamps:

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-app.json logs \
  --module your-module \
  --start 2026-01-01T00:00:00Z --end 2026-01-01T00:15:00Z \
  --limit 100
```

A query range must be positive and at most 31 days. `--limit` accepts 1–1,000 rows. Each page is bounded to 2 MiB of serialized JSON. The CLI applies a five-second ClickHouse execution setting to each query and may retry with a smaller page size; this is not a five-second deadline for the whole command.

The JSON response contains `start`, `end`, `page_limit`, `rows`, and `next_cursor`. When a cursor is returned, request the next page with the same module and explicit start/end values:

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-app.json logs \
  --module your-module \
  --start 2026-01-01T00:00:00Z --end 2026-01-01T00:15:00Z \
  --limit 100 --cursor 'replace-with-next_cursor'
```

The effective `page_limit` may be lower than the requested limit when large chunks would exceed the response bound. A full page can produce a cursor even if the next page is empty.

### Reconstructing bytes and records

Decode each row's `body` according to `encoding`: `utf8` contains text, while `base64` preserves non-UTF8 bytes. `body_bytes` is the length before encoding. Large records are split into identifiable chunks; group by `record_id` and order by `chunk` when reconstructing them.

For non-TTY output, `chunk` is a numeric chunk index. Repeated occurrences at the same timestamp are distinguished by stored occurrence metadata and record identity; the current CLI response does not include the `occurrence` column.

Docker has already combined stdout and stderr for a TTY container, so its rows use the `tty` stream. TTY bodies preserve the timestamped bytes returned by Docker, including timestamps embedded in the body. Their `event_time` is the first Docker timestamp in the collected segment, and `chunk` is a byte offset within that segment.

## Delivery guarantees and the outbox

The durable outbox stores pending metrics and logs as bounded files and retains them until acknowledgement. It defaults to 1 GiB. Stable batch tokens and record IDs make retries identifiable, but delivery is **at least once** and the database deduplication window is finite. An uncertain response followed by a later retry can therefore produce duplicates.

The raw-log CLI collapses identifiable duplicate `record_id`/`chunk` pairs. Account for the delivery model when writing analytical queries that require exact counts; do not treat ClickHouse sorting keys as uniqueness constraints.

Monitor storage errors, log errors, and queued bytes. A full outbox or filesystem failure is surfaced as an error instead of silently discarding unacknowledged files. Recent state collection can continue during a database outage, but archival progress depends on available queue capacity and eventual database recovery.

## Docker log collection and resume

The collector reads the Docker logs API for the registered inventory, including readable `local` and `json-file` logging drivers. Collection starts at the collector's initial cutover time; it does not backfill earlier output. Application logs written only to files are outside this collection path.

Offsets are persisted after durable enqueue. Completed polling windows advance the cursor even for quiet streams, avoiding repeated historical reads. Source logs that Docker has already rotated or deleted cannot be recovered. A collector outage longer than Docker's own log retention can leave gaps, so monitor both source retention and collector health.

Open partial records need special handling because Docker may append later fragments using the same earlier timestamp. The collector retains that timestamp and the already archived occurrence count. For non-TTY partial records, it verifies the stored fragment count and prefix SHA-256 before skipping previously archived fragments. For open TTY segments, it compares the archived prefix length and SHA-256 and stores only new bytes.

If rotation changes or truncates a required prefix, the collector reports an error and does not advance the cursor past an unsafe resume point. It does not silently assume that new fragments are old data. Long-lived open records can increase source reread costs while they remain incomplete.

## Lifecycle actions and recovery

A single host writer and a shared file lock serialize lifecycle controls. The agent validates the target identity and expected state and must record a durable intent in ClickHouse before invoking Docker. If that write fails, it rejects the action even if current telemetry remains available.

After an agent restart, a pending action from the previous process instance is reported as `unknown` and is not replayed automatically. Inspect the actual container state before deciding on another action. Request idempotency prevents a retry from blindly executing the same request again; it does not make an uncertain Docker result knowable.

Keep the outbox, log offsets, configuration, and shared lock consistent across upgrades and restarts. Do not clear pending state as a substitute for diagnosing an error. Database retention on a single disk is not a backup or replication guarantee; plan and validate external recovery separately.
