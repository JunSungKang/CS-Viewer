#!/bin/sh
set -eu
if [ "$#" != 1 ]; then
  echo 'Usage: run-agent.sh /absolute/workspace/path/agent.json' >&2
  exit 64
fi
cd "$(dirname "$0")/.."
exec "${CS_VIEWER_PYTHON:-python3.14}" -B -m backend.agent --config "$1"
