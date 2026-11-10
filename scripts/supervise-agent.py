"""Foreground supervisor; the local operator chooses the permitted process launcher."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from backend.agent import load_config

if len(sys.argv) != 2:
    raise SystemExit('Usage: supervise-agent.py /workspace/path/agent.json')
config_path = Path(sys.argv[1]).resolve()
config = load_config(config_path)
root = Path(config['workspace_root']).resolve()
pidfile = (config_path.parent / 'agent-supervisor.pid').resolve()
if not pidfile.is_relative_to(root):
    raise SystemExit('PID file must stay in the workspace')
stop = threading.Event()
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: stop.set())
pidfile.write_text(str(os.getpid()) + '\n')
try:
    while not stop.is_set():
        child = subprocess.Popen([sys.executable, '-B', '-m', 'backend.agent', '--config', str(config_path)], cwd=repo)
        print(json.dumps({'event': 'agent.spawned', 'pid': child.pid}), flush=True)
        while child.poll() is None and not stop.wait(.5):
            pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if not stop.is_set():
            print(json.dumps({'event': 'agent.exited', 'returncode': child.returncode}), flush=True)
            stop.wait(5)
finally:
    if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
        pidfile.unlink()
