# Architecture

CS-Viewer is a single-host Docker operations dashboard. The current host integration targets macOS with Docker Desktop. A web/API service presents the dashboard, while a host agent accesses an explicitly configured Docker Engine and ClickHouse Server.

For installation, see the [README](../README.md). For configuration, retention changes, and recovery procedures, see the [operations guide](operations.md).

## Components and access boundaries

| Component | Responsibilities | Access |
| --- | --- | --- |
| Browser | Display service response times, container state, image layers, mounts, and control history; submit lifecycle requests. | Web/API service. |
| Web/API service | Serve static assets and forward the supported state, health, and lifecycle requests. | Local host agent; no Docker socket or database credentials. |
| Host agent | Validate the selected daemon and inventory, collect telemetry and logs, and execute supported lifecycle actions. | Configured Docker Engine Unix socket, ClickHouse, and workspace state files. |
| ClickHouse Server | Persist service measurements, raw container logs, and append-only lifecycle events. | Application and administrator accounts provisioned by the operator. |
| Local administration CLI | Initialize tables, inspect or change retention, and retrieve raw logs. | ClickHouse credentials supplied in a local JSON file. |

The agent listens on loopback. The web service accepts only a local agent URL and forwards a fixed set of operations rather than arbitrary Docker requests. Containerized deployments use `host.docker.internal` to reach the host agent.

The web/API service has **no built-in authentication or role-based access control**. Anyone who can reach it can inspect the managed inventory and request lifecycle actions. Shared deployments need an authenticated reverse proxy or another trusted access boundary. The local administration CLI authenticates to ClickHouse using its supplied database credentials; it does not add a dashboard login.

## Collection and persistence

The agent runs separate workers for current-state collection, Docker log polling, and archive writes. A storage failure is reported separately from current Docker and service state.

1. State collection refreshes the current snapshot and recent service trends. The in-memory trend cache holds up to 60 measurements per module.
2. Service samples and raw logs enter a bounded, durable file outbox inside the configured workspace. Log offsets and resume metadata are also maintained there using atomic file replacement.
3. The archive worker sends bounded batches to ClickHouse. It removes an outbox batch only after acknowledgment and retries unacknowledged batches with a stable deduplication token.
4. After storage becomes available, trend hydration reads up to the latest 60 measurements per module from the preceding day. Older records remain in ClickHouse until their retention policy expires.

ClickHouse is the only persistent database. The outbox and offset files are local durability and recovery state, not a second database. The web service reads the agent snapshot; it does not query ClickHouse directly. Raw log retrieval is available through the local administration CLI, with no raw-log endpoint exposed by the web service.

## Storage model

| Table | Contents | Sort order |
| --- | --- | --- |
| `service_samples` | Typed HTTP/TCP timing, response status, container counts, restarts, OOM/health indicators, CPU, and memory measurements. | Module, measurement time, batch ID. |
| `container_logs` | Event and receipt times, service/container identity, stream, record/chunk metadata, and original log bytes represented as UTF-8 or base64. | Module, event time, container ID, stream, record ID, chunk. |
| `action_events` | Append-only lifecycle intents and results, request fingerprints, agent-instance identity, and outcome status. | Request ID, event sequence. |

The tables use `MergeTree`, monthly partitions based on `received_at`, typed columns, and low-cardinality metadata where appropriate. The default compression codec is `ZSTD(1)`, with delta encoding on timestamp columns. Bounded batches limit individual ingestion work.

Each table defaults to three calendar years of retention. The administration CLI accepts explicit policies of 3–100 years and checks the server's actual TTL. Expiry uses receipt time and whole-part expiration, so some rows can remain longer than the configured period. Retention does not provide backup or replication; see the [operations guide](operations.md#retention-policy).

ClickHouse primary and sorting keys are **not uniqueness constraints**. Transport is at-least-once, and the deduplication window is finite. Identifiable raw-log duplicates are collapsed by the log query, but analytical consumers must still account for delivery semantics.

## Lifecycle request flow

Supported actions are `start`, `stop`, and `restart` for registered containers.

1. Validate the action, full container ID, and request UUID; compute a fingerprint of the requested operation.
2. Acquire the shared operations file lock and look up any previous event for that UUID. A matching request returns its recorded outcome; reusing the UUID with a different fingerprint is rejected.
3. Persist a pending intent directly to ClickHouse before invoking Docker. If the intent cannot be recorded, the action is refused.
4. Validate the daemon identity, actual container identity, configured inventory, confirmation name, and expected state; then invoke the supported Docker action.
5. Inspect the resulting state and append a result event. An outcome that cannot be confirmed is reported as `unknown` rather than assumed successful.

Pending requests from an earlier agent instance are displayed as `unknown` after restart and are not replayed. The shared file lock serializes cooperating lifecycle and deployment operations; it does not turn ClickHouse into a distributed lock service.

## Supported scope and limitations

- One host agent owns a state directory. A collector lease prevents concurrent agent processes from using that directory; multiple independent host writers require an additional coordination design.
- Log collection uses the Docker logs API for the registered inventory. It does not collect application log files outside stdout/stderr or recover source records already removed by Docker rotation. TTY output is a combined stream.
- Optional port labels describe existing published Docker bindings. They do not create listeners, change routing, or prove upstream availability. Dashboard response times measure the configured probe endpoint.
- The application does not expose container creation or deletion, `exec`, arbitrary filesystem access, or an unrestricted Docker Engine API.
- Automated backup and restore are outside the application interface. Operators provide database persistence, backups, TLS termination, and any shared-deployment authentication.

See [validation](validation.md) for the checks that exercise these contracts. The dashboard interface and some runtime messages remain in Korean; these guides describe the implementation in English.
