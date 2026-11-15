# CS-Viewer

[![Tests](https://github.com/JunSungKang/CS-Viewer/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/JunSungKang/CS-Viewer/actions/workflows/quality.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)
![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB.svg)
![ClickHouse required](https://img.shields.io/badge/Storage-ClickHouse-FFCC01.svg)

**A self-hosted dashboard for Docker operations, with persistent metrics, container logs, and control history in ClickHouse.**

CS-Viewer shows service response times, container health, resource usage, image layers, and storage mounts in one interface. A host agent collects data from an explicit container inventory and processes start, stop, and restart requests. The application uses the Python standard library and plain JavaScript; it has no third-party Python or JavaScript runtime dependencies.

The current host integration targets **macOS with Docker Desktop**. Project documentation is in English. The dashboard interface is currently in Korean.

> **Access boundary:** CS-Viewer has no built-in authentication or role-based access control. Anyone who can reach the web service can view the managed inventory and request container lifecycle actions. The quick start binds the web service to loopback. For a shared deployment, use an authenticated reverse proxy or another trusted access boundary.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Docker deployment](#docker-deployment)
- [Data retention and recovery](#data-retention-and-recovery)
- [Development and validation](#development-and-validation)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Service monitoring:** inspect host and Docker VM memory, HTTP response times, recent trends, restart counts, and out-of-memory indicators.
- **Port directory:** group published ports by purpose, pair configured redirect ports with their destinations, and distinguish loopback bindings from other host bindings.
- **Container controls:** inspect registered containers and start, stop, or restart them with request IDs, expected-state checks, and an audit trail.
- **Image and mount inspection:** compare filesystem layer identities and inspect volume and bind-mount paths, destinations, and access modes.
- **Persistent telemetry:** retain typed service measurements, Docker stdout/stderr bytes, and append-only control events in ClickHouse.
- **Configurable retention:** set each data category to 3–100 calendar years, with a preview before applying a change.
- **Storage outage visibility:** collect recent state independently of database writes, display storage and log errors, and retry delivery through a bounded durable file outbox.

## Architecture

| Component | Responsibility |
| --- | --- |
| Browser and web/API service | Render the dashboard and forward a limited set of requests to the local agent. |
| Host agent | Access the selected Docker Engine Unix socket, collect metrics and logs, and serialize lifecycle actions with a shared file lock. |
| ClickHouse Server | Persist service samples, raw container logs, and control events. |

The web service receives neither the Docker socket nor database credentials. Before a lifecycle action, the agent validates the configured daemon, actual container ID, inventory, confirmation name, expected state, and request UUID. It records a durable intent before calling Docker. If that intent cannot be persisted, the action is rejected; recent state collection can continue during a storage outage.

CS-Viewer supports a single host writer. Its application interface does not provide arbitrary Docker API access, container creation or removal, command execution, or automated backup and restore. Raw logs are retrieved through a local CLI using ClickHouse credentials. See the [architecture guide](docs/architecture.md) for component boundaries and data flow.

## Requirements

| Dependency | Purpose |
| --- | --- |
| macOS and Docker Desktop | Current host integration and a reachable Docker Engine Unix socket. |
| Python 3.14 or newer | Run the agent, local administration CLI, and web service from the checkout. |
| ClickHouse Server | Required persistent storage; version 26.8 LTS was used for the current implementation and integration tests. |
| Git | Obtain the source. |
| Node.js | Run the JavaScript syntax check and frontend behavior tests; not needed to serve the dashboard. |

Provision ClickHouse separately; this repository does not include a ClickHouse deployment, credentials, or the local platform base images used by its container examples. Refer to the [ClickHouse installation documentation](https://clickhouse.com/docs/install) for server setup. Use persistent database storage and a private or otherwise protected database connection.

## Quick start

This source-based setup runs the agent and web service on the host. It requires no `pip install` or frontend build step. Run subsequent commands from the checkout root.

### 1. Clone the repository

```sh
git clone https://github.com/JunSungKang/CS-Viewer.git
cd CS-Viewer
mkdir -p .local
chmod 700 .local
```

### 2. Initialize ClickHouse

Create `.local/clickhouse-admin.json` with an operator connection that can initialize the database and change its tables. Replace all example credentials and endpoints with values for your server.

```json
{
  "url": "http://127.0.0.1:8123",
  "user": "default",
  "password": "replace-with-your-admin-password",
  "database": "cs_viewer",
  "timeout": 8
}
```

```sh
chmod 600 .local/clickhouse-admin.json
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json init
```

After initialization, use your ClickHouse administrator client to create a separate application account:

```sql
CREATE USER cs_viewer IDENTIFIED WITH sha256_password BY 'replace-with-a-distinct-application-password';
GRANT SELECT, INSERT ON cs_viewer.* TO cs_viewer;
```

Create `.local/clickhouse-app.json` with the same connection fields, the `cs_viewer` user, and its distinct password. Set the file permissions to `0600`. The agent uses this account. Keep operator credentials separate for initialization and retention changes, and use separate test credentials for integration tests.

### 3. Configure the host agent

```sh
cp docs/config.example.json .local/agent.json
docker context show
docker context inspect --format '{{ .Endpoints.docker.Host }}'
docker info --format '{{.ID}}'
```

Edit `.local/agent.json` for the same Docker context inspected above:

| Setting | Value |
| --- | --- |
| `workspace_root` | Absolute path to this checkout, or an enclosing workspace that owns all agent state paths. |
| `docker_socket` | The context's Unix socket path, without the `unix://` prefix. |
| `daemon_id` | The ID returned by `docker info` for that same context. |
| `lock_path` | An absolute workspace path for the shared operations lock, such as `.local/deploy.lock` under the checkout. |
| `clickhouse_config` | Absolute path to `.local/clickhouse-app.json`. |
| `spool_path` | An absolute workspace path such as `.local/state/outbox`. |
| `log_state_path` | An absolute workspace path such as `.local/state/log-offsets`. |
| `services` | Your explicit container inventory, including module, role, logical name, and accepted container names. |
| `probes` | Your service HTTP/HTTPS checks; use `{}` if none are needed. |
| `bases` | Your local base-image references for layer grouping; use `{}` if none are configured. |

Replace the sample inventory and image references with your own. The state, lock, and credential paths listed above must be absolute and resolve inside `workspace_root`. Use one agent for the state directory, and share its operations lock with other local deployment tools that manage the same containers. Optional display-only `port_labels` are described in the [operations guide](docs/operations.md#port-display-labels).

### 4. Start the agent and web service

In one terminal, from the checkout:

```sh
python3.14 -B scripts/supervise-agent.py "$PWD/.local/agent.json"
```

In another terminal, from the same checkout:

```sh
CS_AGENT_URL=http://127.0.0.1:20135 \
  python3.14 -B -m backend.app --bind 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>. The agent defaults to loopback port `20135`; update `CS_AGENT_URL` if you change that port. The foreground supervisor restarts the agent child after an exit. It does not install a system startup service.

## Docker deployment

[compose.platform.yaml](compose.platform.yaml) is a platform deployment example for the web/API service. It uses a read-only filesystem, dropped capabilities, CPU and memory limits, and no Docker socket mount. The web service connects to the host agent through `host.docker.internal`; the agent and ClickHouse must be provided separately.

The default `platform/python314:...` base image is a local platform dependency and is not published by this repository. The [Dockerfile](Dockerfile) expects an Ubuntu-based CPython 3.14 environment with the packaging tools it removes during the build. Supply a compatible `PYTHON_IMAGE` before building. The [Bake configuration](docker-bake.hcl) targets `linux/arm64`; Compose uses the local build architecture unless configured otherwise.

The Compose example publishes host port `20035` on all interfaces without configuring TLS or authentication. Review the binding and access boundary before starting it. Configure TLS termination and certificate renewal in your reverse proxy. Use the loopback quick start for an initial local evaluation.

## Data retention and recovery

| Table | Stored data | Default retention |
| --- | --- | --- |
| `service_samples` | Typed service measurements. | 3 calendar years |
| `container_logs` | Docker log bytes, stream, timestamps, and container metadata. | 3 calendar years |
| `action_events` | Append-only lifecycle intent and result events. | 3 calendar years |

Each table accepts **3–100 years** independently. Expiry is based on receipt time and calendar-year arithmetic. Monthly partitioning and whole-part expiration can retain some records beyond the configured minimum.

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json status
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5 --apply
```

The second command previews the impact; the third applies the change. A reduction also requires `--apply`. Increasing retention does not recover deleted data. The agent reads the actual server TTL and does not shorten an expanded policy at startup.

Retrieve raw logs locally:

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-app.json logs --module your-module --limit 100
```

The default range is the last 15 minutes. Set timezone-qualified `--start` and `--end` values for an explicit range of up to 31 days. A page contains at most 1,000 rows and 2 MiB of serialized JSON. Continue with `--cursor` and the same module and explicit time range. See [raw log retrieval](docs/operations.md#retrieve-raw-logs) for pagination and byte reconstruction.

For each newly tracked container, collection begins when the collector first records its starting offset; earlier logs are not backfilled. Non-UTF-8 bytes are stored as base64, and long records are divided into identifiable chunks. Docker combines TTY output into one stream. The collector uses the Docker logs API for registered containers; it does not read application log files outside stdout/stderr.

The durable outbox defaults to 1 GiB and retains unacknowledged data. Delivery is at-least-once, with a finite deduplication window. Storage failures, a full outbox, and source rotation that prevents a safe resume are reported as errors. Logs already removed by Docker rotation cannot be recovered. A retention policy on one database disk does not provide an external backup; review the [recovery limitations](docs/operations.md#lifecycle-actions-and-recovery) when planning storage.

## Development and validation

```sh
python3.14 -B -m unittest discover -s tests -v
node --check web/app.js
node tests/test_ports.js
docker compose -f compose.platform.yaml config --quiet
```

Real ClickHouse integration tests are skipped when no test server is configured. To run them, use a **dedicated test ClickHouse instance** and credentials allowed to create and drop isolated test databases:

```sh
CS_TEST_CLICKHOUSE_CONFIG="$PWD/.local/clickhouse-test-admin.json" \
  python3.14 -B -m unittest discover -s tests -v
```

The tests create and drop uniquely named `cs_test_*` databases. CI runs Python 3.14, a pinned ClickHouse service, the Python suite, and both JavaScript checks. Coverage includes control idempotency, uncertain outcomes, storage recovery, retention boundaries, bounded spooling, Docker log framing, partial-record resume, CLI pagination, and port-directory rendering.

The optional [Docker control smoke test](scripts/smoke-docker.py) requires the local platform base image named in the script. It creates one owned disposable container, exercises its lifecycle, and removes it. It inherits the supplied configuration's ClickHouse connection and writes control events to that database. Use a dedicated development configuration and database. See the [validation guide](docs/validation.md) for prerequisites and reproducible validation guidance.

## Documentation

- [Architecture](docs/architecture.md): component boundaries, data flow, and lifecycle controls.
- [Configuration example](docs/config.example.json): agent settings and optional port display labels.
- [Operations](docs/operations.md): configuration, retention, log retrieval, and recovery.
- [Validation](docs/validation.md): local checks, integration tests, smoke testing, and benchmarking.

All guides are in English and describe the checked-in implementation. Deployment-specific infrastructure and private benchmark reports are not prerequisites for using the source or following the local quick start.

## Contributing

Submit bug reports through [GitHub Issues](https://github.com/JunSungKang/CS-Viewer/issues) and proposed changes through [Pull Requests](https://github.com/JunSungKang/CS-Viewer/pulls).

Include expected and observed behavior, reproduction steps, and relevant Python, Docker, operating-system, and ClickHouse versions in a bug report. Remove credentials and private logs from shared output. In a pull request, explain the resulting behavior, update the affected documentation, and list the checks performed. Add focused regression coverage for runtime changes. Discuss substantial architecture changes in an issue before implementing them.

Contributions are made under the project's AGPL-3.0-only license. Preserve applicable copyright and third-party license notices.

## License

Copyright (C) 2026 CS-Viewer contributors.

CS-Viewer is licensed under the **GNU Affero General Public License, version 3 only**. SPDX identifier: **`AGPL-3.0-only`**. The project does not grant an option to use a later license version.

See [LICENSE](LICENSE) for the complete license text. Third-party software used alongside CS-Viewer remains subject to its own licenses.
