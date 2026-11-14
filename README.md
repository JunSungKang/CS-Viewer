# CS-Viewer

[![Tests](https://github.com/JunSungKang/CS-Viewer/actions/workflows/quality.yml/badge.svg?branch=main)](https://github.com/JunSungKang/CS-Viewer/actions/workflows/quality.yml)
[![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)
![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB.svg)
![ClickHouse required](https://img.shields.io/badge/Storage-ClickHouse-FFCC01.svg)

**A self-hosted Docker operations dashboard with ClickHouse-backed metrics, raw container logs, and control history.**

CS-Viewer brings service response times, container health, resource usage, image layers, and mounts into one interface. A host agent collects data from an explicitly configured Docker inventory and handles start, stop, and restart requests. The application uses the Python standard library and plain JavaScript, with no third-party Python or JavaScript runtime packages.

The current host integration targets **macOS with Docker Desktop**. The dashboard interface and detailed technical guides are currently in Korean; this README provides the English project overview and setup guide.

> **Deployment access:** CS-Viewer has no built-in authentication or role-based access control. Anyone who can reach the web service can view the managed inventory and request lifecycle actions. The quick start binds the web service to loopback. Put shared deployments behind an authenticated reverse proxy or a trusted access boundary.

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

- **Operations dashboard:** host and Docker VM memory, HTTP response times, recent trends, restart counts, and OOM indicators.
- **Container controls:** inspect registered containers and start, stop, or restart them with request IDs, expected-state checks, and an audit trail.
- **Image layers:** inspect actual filesystem layer identities and shared base layers.
- **Mount inspection:** view bind mounts, volumes, source paths, destinations, and read/write access.
- **Persistent telemetry:** store typed service metrics, Docker stdout/stderr, and append-only control events in ClickHouse.
- **Configurable retention:** keep each data category for at least three calendar years, with explicit preview and application of retention changes.
- **Outage visibility:** keep recent state collection separate from storage, expose errors and backlog, and retry through a bounded durable file outbox.

## Architecture

| Component | Responsibility |
| --- | --- |
| Browser and web/API service | Render the dashboard and forward a limited set of requests to the local agent. |
| Host agent | Access the selected Docker Engine Unix socket, collect metrics and logs, and serialize lifecycle actions with a shared file lock. |
| ClickHouse Server | Persist service samples, raw container logs, and control events. |

The web service does not receive the Docker socket or database credentials. Container controls validate the configured daemon, actual container ID, inventory, confirmation name, expected state, and request UUID. The agent records a durable intent before invoking Docker. ClickHouse unavailability therefore prevents lifecycle actions while recent state collection can continue.

This is a single-host writer design. Arbitrary Docker API access, container creation or removal, command execution, and automated backup/restore are outside the application interface. Raw log retrieval is available through a local authenticated CLI, rather than a public web endpoint.

## Requirements

| Dependency | Purpose |
| --- | --- |
| macOS and Docker Desktop | Current host integration and a reachable Docker Engine Unix socket. |
| Python 3.14 or newer | Run the agent, local administration CLI, and web service from the checkout. |
| ClickHouse Server | Required persistent storage; version 26.8 LTS was used for the current implementation and integration tests. |
| Git | Obtain the source. |
| Node.js | Optional: run the JavaScript syntax check. |

Provision ClickHouse separately. The repository does not bundle the platform's ClickHouse deployment, credentials, or private base images. See the [ClickHouse installation documentation](https://clickhouse.com/docs/install) for server setup. Keep database data on persistent storage and use a private or otherwise protected connection.

## Quick start

Run these commands from a source checkout. No `pip install` or frontend build step is required.

### 1. Clone the repository

```sh
git clone https://github.com/JunSungKang/CS-Viewer.git
cd CS-Viewer
mkdir -p .local
chmod 700 .local
```

### 2. Initialize ClickHouse

Create `.local/clickhouse-admin.json` with an administrator connection to your server. Replace every placeholder with your own values; the endpoint below is an example.

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

Using your ClickHouse administrator client, create a separate application account after initialization:

```sql
CREATE USER cs_viewer IDENTIFIED WITH sha256_password BY 'replace-with-a-distinct-application-password';
GRANT SELECT, INSERT ON cs_viewer.* TO cs_viewer;
```

Create `.local/clickhouse-app.json` with the same connection fields, the `cs_viewer` user, and its password. Set its permissions to `0600`. The agent uses this account; retain administrator credentials for schema initialization, retention changes, and isolated integration tests.

### 3. Configure the host agent

```sh
cp docs/config.example.json .local/agent.json
docker context show
docker context inspect --format '{{ .Endpoints.docker.Host }}'
docker info --format '{{.ID}}'
```

Edit `.local/agent.json` to match your selected Docker context:

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

Replace the sample inventory and image references with your own. All state, lock, and credential paths must resolve inside `workspace_root`. Use a single agent for a given state directory and share the operations lock with other local deployment tools that manage the same containers.

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

Open <http://127.0.0.1:8080/>. The agent defaults to loopback port `20135`; adjust the web service's `CS_AGENT_URL` if you change the agent port. The supervisor restarts its agent child after an exit; it does not install an operating-system startup service.

## Docker deployment

[compose.platform.yaml](compose.platform.yaml) runs the web/API service with a read-only filesystem, dropped capabilities, a memory/CPU limit, and no Docker socket mount. It connects to the host agent through `host.docker.internal`.

The default `platform/python314:...` image is a local platform dependency, not a public image supplied by this repository. The Dockerfile targets an Ubuntu-based CPython 3.14 environment with the packaging tools it removes during the build. Supply a compatible `PYTHON_IMAGE` before building. The included Bake file targets `linux/arm64`; the local host architecture is used by the Compose build unless you configure otherwise.

The Compose example publishes host port `20035` on all interfaces and does not configure TLS or authentication. Review that binding and your access boundary before starting it. TLS termination and certificate renewal belong to your deployment's reverse proxy. For a source-only local evaluation, use the loopback quick start above.

## Data retention and recovery

| Table | Stored data | Default retention |
| --- | --- | --- |
| `service_samples` | Typed service measurements. | 3 calendar years |
| `container_logs` | Docker log bytes, stream, timestamps, and container metadata. | 3 calendar years |
| `action_events` | Append-only lifecycle intent and result events. | 3 calendar years |

Each table accepts **3–100 years**. Expiry uses receipt time and calendar-year arithmetic. Monthly partitions and whole-part expiration can retain some records longer than the configured minimum.

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json status
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5
python3.14 -B -m backend.admin --credentials .local/clickhouse-admin.json retention --table container_logs --years 5 --apply
```

The second command previews the impact; the third applies it. Shortening retention also requires `--apply`. Extending retention cannot recover already deleted data. The agent reads the actual server TTL and does not reset an expanded policy on restart.

Retrieve raw logs locally:

```sh
python3.14 -B -m backend.admin --credentials .local/clickhouse-app.json logs --module your-module --limit 100
```

The default range is the last 15 minutes. Use timezone-qualified `--start` and `--end` values for an explicit range of up to 31 days. Pages are limited to 1,000 rows and 2MiB of serialized JSON. Continue with `--cursor` while keeping the same module and explicit time range.

Log collection begins at the collector's first start; earlier logs are not backfilled. Non-UTF8 bytes are stored as base64, and long records are split into identifiable chunks. Docker combines TTY output into a single stream. Collection uses the Docker logs API for the registered inventory; application files outside stdout/stderr are not collected.

The durable outbox defaults to 1GiB and retains unacknowledged data. Transport is at-least-once with a finite deduplication window. Storage failures, full backlog, and source rotation that prevents safe resume are surfaced as errors. Docker logs that have already rotated out cannot be recovered. Retention on a single database disk is separate from an external backup strategy.

## Development and validation

```sh
python3.14 -B -m unittest discover -s tests -v
node --check web/app.js
docker compose -f compose.platform.yaml config --quiet
```

Without a configured test server, real ClickHouse integration tests are skipped. To include them, use a **dedicated test ClickHouse instance** and a credential file whose account can create and drop isolated test databases:

```sh
CS_TEST_CLICKHOUSE_CONFIG="$PWD/.local/clickhouse-test-admin.json" \
  python3.14 -B -m unittest discover -s tests -v
```

The integration tests create and drop uniquely named `cs_test_*` databases. CI uses Python 3.14 and a pinned ClickHouse service. Test coverage includes control idempotency, uncertain outcomes, storage recovery, retention boundaries, bounded spooling, Docker log framing, partial-record resume, and CLI pagination.

The optional [Docker control smoke test](scripts/smoke-docker.py) requires the local platform base image referenced in that script. It creates one owned disposable container, exercises its lifecycle, and removes that container. Review its prerequisites and use a suitable development environment before running it.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration example](docs/config.example.json)
- [Operations, retention, and recovery](docs/operations.md)
- [Validation](docs/validation.md)

These detailed guides are currently in Korean. Deployment-specific infrastructure and benchmark reports live outside this repository and are not prerequisites for reading the source or running the local quick start.

## Contributing

Bug reports and pull requests are welcome through [GitHub Issues](https://github.com/JunSungKang/CS-Viewer/issues) and [Pull Requests](https://github.com/JunSungKang/CS-Viewer/pulls).

For a bug report, include the expected and observed behavior, reproduction steps, and relevant Python, Docker, OS, and ClickHouse versions. Remove credentials and private logs from shared output. For a pull request, explain the behavior change, update affected documentation, and include the checks you ran. Add focused regression coverage when changing runtime behavior. Discuss substantial architecture changes in an issue first.

Contributions are made under the project's AGPL-3.0-only license. Preserve applicable copyright and third-party license notices.

## License

Copyright (C) 2026 CS-Viewer contributors.

CS-Viewer is licensed under the **GNU Affero General Public License, version 3 only**. SPDX identifier: **`AGPL-3.0-only`**. The project does not grant an option to use a later license version.

See [LICENSE](LICENSE) for the complete license text. Third-party software used alongside CS-Viewer remains subject to its own licenses.
