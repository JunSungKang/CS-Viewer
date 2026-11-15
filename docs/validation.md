# Validation

Run checks from the repository root. Python 3.14 or newer is required by the project; Node.js is used for frontend checks. No third-party Python or JavaScript packages are needed for these suites.

For installation prerequisites, see the [README](../README.md#requirements). For the behavior being checked, see [architecture](architecture.md) and [operations](operations.md).

## Local checks

```sh
python3.14 -B -m unittest discover -s tests -v
node --check web/app.js
node tests/test_ports.js
docker compose -f compose.platform.yaml config --quiet
```

The Python command runs unit and regression tests. Real-server integration tests are skipped unless a ClickHouse test connection is configured. A successful run with skips does not establish that the ClickHouse integration passed.

`node --check` checks JavaScript syntax. `tests/test_ports.js` executes the shipped frontend code in an in-memory DOM and checks behavior without browser access, network calls, or file writes. Compose validation checks the deployment model; it does not build images, start containers, or prove that an agent or database is reachable.

## Real ClickHouse integration tests

Use a dedicated development or test ClickHouse instance. Create a local credential JSON file with the connection fields documented in the [quick start](../README.md#2-initialize-clickhouse). Its account must be able to create and drop test databases and initialize their tables.

```sh
CS_TEST_CLICKHOUSE_CONFIG="$PWD/.local/clickhouse-test-admin.json" \
  python3.14 -B -m unittest discover -s tests -v
```

Each integration test creates a uniquely named `cs_test_*` database and drops it during normal teardown. Temporary outbox files are created under `.runtime/tests` in the checkout. If a test process is interrupted, inspect the test resources before cleaning up only those you own.

`CS_TEST_CLICKHOUSE_URL` is an alternative used by CI. That mode supplies the `default` user and the fixed test password `integration-only`; it is not a general substitute for a credential file. `CS_TEST_CLICKHOUSE_CONFIG` takes precedence when both variables are set.

## Regression coverage

| Area | Checks |
| --- | --- |
| Lifecycle controls | Request UUID replay and fingerprint conflicts; daemon, inventory, actual-ID, confirmation-name, and expected-state checks; shared locking; refusal when durable intent recording fails. |
| Uncertain outcomes | Pending requests after agent restart, result recording, and prevention of automatic action replay. |
| Collection and storage | Recent state during storage outages, bounded outbox behavior, restart recovery, lost acknowledgments, and stable retry tokens. |
| Raw logs | Docker framing, nanosecond timestamps, repeated content, partial records, prefix verification during resume, TTY chunks, byte preservation, and bounded CLI pagination. |
| Retention | Three-year defaults, invalid policies, preview versus explicit application, expansion and reduction, and calendar/leap-year boundaries. |
| Frontend port directory | Access/redirect pairing; container-owner, address, transport, title, and group isolation; unknown and dangling bindings; deduplication; loopback/IPv6 handling; text-only labels; expansion thresholds; error-first and slow-to-fast ordering. |

The frontend suite checks renderer behavior in a minimal DOM. It does not replace browser checks for CSS layout, navigation, narrow screens, or the monitoring-only view.

## Continuous integration

The [Tests workflow](../.github/workflows/quality.yml) runs the Python suite, JavaScript syntax check, and frontend behavior suite. It provisions a pinned ClickHouse Server image and uses Python 3.14. Consult the workflow for the exact image digest and resource limits.

The separate [Governance workflow](../.github/workflows/governance.yml) validates the repository's current-head review and delivery policy. Passing governance is separate from passing application tests.

## Optional Docker lifecycle smoke test

The [smoke script](../scripts/smoke-docker.py) exercises the web/API-to-agent control path against one owned disposable container. It verifies restart, stop, start, and duplicate-request handling, then checks ownership before removing the canary.

Run it only with a dedicated development agent configuration and database:

```sh
python3.14 -B scripts/smoke-docker.py \
  --config "$PWD/.local/agent-development.json" \
  --context desktop-linux
```

The configured socket and CLI context must identify the same Docker daemon. The script also requires the local platform Python image named in its source; that image is not published by this repository. Its temporary outbox, offsets, and inventory are isolated, but it inherits the supplied ClickHouse connection and writes control history to that database. Use a configuration prepared for this test rather than the production agent configuration.

## Browser and deployment checks

For UI changes, inspect the dashboard at desktop and mobile widths and in its monitoring-only view. Verify that all four navigation views remain usable, published ports are represented without horizontal overflow, and lifecycle controls preserve their confirmation and expected-state behavior. Use an owned canary for any actual lifecycle action.

For a deployment, verify the selected daemon and release, web health, fresh collection timestamps, storage and log errors, backlog, and the server's actual retention policy. A healthy web response alone does not prove that archive writes are succeeding. Preserve existing data and inspect unrelated services when replacing an agent or web release.

## Performance evaluation

Benchmark a representative workload before changing storage or ingestion settings. Record the following with the results:

- Source revision, server version, schema/codecs, hardware, container CPU/memory limits, and storage medium.
- Service count, sampling interval, log rate, record-size distribution, repetition, stream mix, and dataset size or duration.
- Query definitions, cold/warm cache conditions, repetitions, latency distribution, CPU, memory, and ingestion throughput.
- Compressed bytes, primary-index size, active parts, merge activity, backlog growth, and recovery after a simulated outage.

Compare alternatives under the same workload and include operational recovery costs. A small synthetic run does not establish multi-year storage capacity or production performance. Deployment-specific benchmark reports are not included in this repository; publish enough workload and measurement detail with any performance claim to make it reproducible.
