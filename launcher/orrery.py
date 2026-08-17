#!/usr/bin/env python3
"""orrery.py — Orrery, a local launch pad for every dev server in the portfolio.

Serves launcher/static/index.html plus a JSON API on http://127.0.0.1:7345 and
starts, stops and watches the dev servers described by ../projects.json.

projects.json is the same file the hub renders README.md, api.json, resume.json
and llms.txt from. A project becomes launchable by growing an optional "dev"
block; there is no second project list to keep in sync. The "dev" block is
portable on purpose — a directory NAME, a command, a port — because the hub
repository is public. The machine-specific part (where the projects live) is
resolved at runtime, never committed:

    1. the ORRERY_ROOT environment variable
    2. "projects_root" in launcher/launcher.local.json   (git-ignored)
    3. the hub repository's parent directory              (the usual layout)

Run:  python orrery.py [--no-browser] [--port N]

Single-instance: if the port is already bound, open a browser at the running
instance and exit. All shared state goes through one lock; a malformed
projects.json or a missing root never crashes a request, it answers with an
error the UI can explain.

Three background threads do the work no request is allowed to do, because a
request that blocks on a socket, a subprocess or a disk is a board that stops
answering:

    watchdog     ports and process state, once a second
    health       a real HTTP GET against every running server, every ~5s, so
                 "the process is alive" and "the server answers" stop being the
                 same claim (only the second one earns a green dot)
    git (lazy)   branch, dirty count and ahead/behind per project directory,
                 refreshed on demand behind a 30s TTL, never on a request thread
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

HOST = "127.0.0.1"
PORT = 7345

LAUNCHER_DIR = Path(__file__).resolve().parent
HUB_DIR = LAUNCHER_DIR.parent
PROJECTS_FILE = HUB_DIR / "projects.json"
INDEX_FILE = LAUNCHER_DIR / "static" / "index.html"
LOCAL_FILE = LAUNCHER_DIR / "launcher.local.json"
EXAMPLE_FILE = LAUNCHER_DIR / "launcher.local.example.json"
STATE_DIR = LAUNCHER_DIR / "state"

MAX_LOG_LINES = 500          # ring buffer depth, per project
GRACE_SECS = 5.0             # graceful shutdown window before the forced tree kill
KILL_WAIT_SECS = 10.0        # how long taskkill /T /F gets to finish the job
WATCH_INTERVAL = 1.0         # background state machine tick
PROBE_TIMEOUT = 0.4          # per-address connect() timeout
PROBE_WORKERS = 16           # ports are probed in parallel, see probe_ports()
SETTLE_SECS = 1.5            # grace before a still-held port counts as an orphan
NO_PORT_READY_SECS = 1.5     # a portless project counts as up once it survives this
RELOCATE_SCAN = 60           # how many ports above the default to try when relocating
PROJECTS_TTL = 1.0           # projects.json re-read throttle, seconds
LOCAL_TTL = 1.0              # launcher.local.json re-read throttle, seconds
HOLDER_TTL = 15.0            # how long a "who holds this port" answer is reused
REAP_DEPTH = 5               # generations walked up from a listener when reaping

HEALTH_TICK = 1.0            # readiness loop cadence
HEALTH_INTERVAL = 5.0        # per-project gap between two HTTP GETs
HEALTH_RETRY = 1.5           # ...until one fails, then ask again this soon
HEALTH_TIMEOUT = 2.0         # hard ceiling on one readiness GET
HEALTH_WORKERS = 8           # one round covers every running project at once

GIT_TTL = 30.0               # how long one git reading is reused
GIT_TIMEOUT = 5.0            # hard ceiling on one git call
GIT_WORKERS = 2              # background refreshes; a request never runs git
GIT_WAIT = 2.0               # /api/context's bounded wait for a first reading
WHICH_TTL = 20.0             # how long the "is this editor installed" sweep lasts
OPEN_DEBOUNCE = 1.5          # one launch per (project, target) inside this window

DEV_KINDS = {"node", "python", "make", "static"}
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Commands that need a "--" before flags meant for the underlying tool.
SCRIPT_RUNNERS = {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd", "bun"}

# "Open in": what each target is, and what to look for on PATH. Never an
# absolute path — this file is public, and one hard-coded C:\Users\... here
# would publish the author's machine layout to everyone who reads the repo.
# Every one of these is discovered with shutil.which at request time;
# launcher.local.json's "open_commands" is the escape hatch for anything
# installed somewhere PATH does not reach.
OPEN_TARGETS: dict[str, dict] = {
    "browser": {"label": "Browser", "candidates": ()},   # the original behaviour
    "vscode": {"label": "VS Code", "candidates": ("code", "code.cmd")},
    "cursor": {"label": "Cursor", "candidates": ("cursor", "cursor.cmd")},
    "claude": {"label": "Claude Code", "candidates": ("claude", "claude.exe")},
    "folder": {"label": "Folder",
               "candidates": ("explorer",) if os.name == "nt"
               else ("open", "xdg-open")},
}
# Windows Terminal, so "claude" lands in a real terminal already cd'd into the
# project. Optional: without it the CLI still opens, in a plain console window.
TERMINAL_CANDIDATES = ("wt", "wt.exe")
SHELL_SUFFIXES = {".cmd", ".bat"}   # need COMSPEC to run inside another terminal

# CSI (colour, cursor), OSC (window title), and bare two-character escapes.
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)

LOCK = threading.RLock()     # guards PROCS, PORT_STATE and the projects.json cache
# Serializes start_project end to end. Deciding a port means probing it, which
# blocks, so the probe cannot happen under LOCK without freezing every status
# poll — but that gap is exactly where two concurrent starts both see the same
# port free and both claim it. START_LOCK closes the gap without holding LOCK
# across the probe. Ordering is always START_LOCK -> LOCK, never the reverse.
START_LOCK = threading.Lock()
PROCS: dict[str, "Proc"] = {}          # project id -> most recent launch
PORT_STATE: dict[int, bool] = {}       # port -> something is listening
HOLDERS: dict[int, tuple[float, str]] = {}   # port -> (resolved at, who holds it)
STOP_EVENT = threading.Event()
PROBE_POOL = ThreadPoolExecutor(max_workers=PROBE_WORKERS,
                                thread_name_prefix="orrery-probe")
# id -> (which run it belongs to, when it was taken, payload). The run is the
# Proc's started_at: a restart on another port must not inherit the old verdict.
HEALTH: dict[str, tuple[float, float, dict]] = {}
HEALTH_POOL = ThreadPoolExecutor(max_workers=HEALTH_WORKERS,
                                 thread_name_prefix="orrery-health")
WATCHDOG_ERROR: str | None = None      # last state-machine failure, shown in /api/health
HEALTH_ERROR: str | None = None        # last readiness-loop failure, same place
SERVER: ThreadingHTTPServer | None = None
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}  # anti DNS-rebinding
BOOT_TIME = time.monotonic()
BOOT_NOTES: list[str] = []             # what the orphan reaper did on this boot

_PROJECT_CACHE: tuple[float, float, dict | None, str | None] = (0.0, 0.0, None, None)
_LOCAL_CACHE: tuple[float, float, dict] = (0.0, -1.0, {})
_LOCAL_LOCK = threading.Lock()   # only ever taken alone, or inside LOCK, never around it

# git state. Its own lock, taken alone and never around LOCK: project_rows()
# reads the cache before it takes LOCK, exactly like the local config and the
# directory stats, so a slow worktree can never stall the log readers.
GIT_POOL = ThreadPoolExecutor(max_workers=GIT_WORKERS,
                              thread_name_prefix="orrery-git")
GIT_CACHE: dict[str, tuple[float, str, dict]] = {}   # id -> (read at, dir, payload)
GIT_PENDING: dict[str, Future] = {}                  # id -> refresh in flight
_GIT_LOCK = threading.Lock()

_TARGET_CACHE: tuple[float, dict] = (0.0, {})   # "Open in" availability sweep
_TARGET_LOCK = threading.Lock()

# (project id, target) -> when it was last launched. Every "open in" spawns a
# process, so a repeat inside OPEN_DEBOUNCE is treated as the second half of one
# double-click rather than as a second request. The board guards its own buttons;
# this covers what a button cannot — a page reloaded mid-click, a second tab, or
# anything else posting straight at the route.
_OPEN_RECENT: dict[tuple[str, str], float] = {}
_OPEN_LOCK = threading.Lock()

# ------------------------------------------------------------------ helpers
# (pure functions — no locking; callers hold LOCK around shared state)


def load_json(path: Path, default):
    """Read a JSON file; any failure returns the default, never raises."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: Path, payload) -> None:
    """Atomic write: tmp + os.replace, so a crash never truncates the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def strip_ansi(text: str) -> str:
    """Drop escape sequences at capture time so the ring buffer holds real text."""
    return ANSI_RE.sub("", text)


def hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


LOOPBACKS = ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1"))


def listening_address(port: int) -> tuple[int, str] | None:
    """The loopback address that accepts a connection on this port, or None.

    Both families have to be tried: Vite binds `localhost`, which on Windows
    resolves to ::1 only, so an IPv4-only probe reports a running dev server as
    down and hands its port to the next project that asks for it.

    OverflowError is caught alongside OSError on purpose: connect_ex raises it —
    not an OSError — for a port outside 0..65535, and an out-of-range port that
    reaches here would otherwise escape as a 500 with a raw internal message.
    Routes validate the port first; this is the backstop, not the check.

    Which address answered is returned, not just that one did, so the readiness
    probe can aim its HTTP request at the family that is actually there instead
    of handing the name back to a resolver that would try both again.
    """
    for family, address in LOOPBACKS:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(PROBE_TIMEOUT)
                if s.connect_ex((address, port)) == 0:
                    return family, address
        except (OSError, OverflowError, ValueError, TypeError):
            continue                     # no IPv6 stack, refused, or a bad port
    return None


def port_open(port: int) -> bool:
    """True when either loopback address accepts a connection on this port."""
    return listening_address(port) is not None


def pids_file() -> Path:
    """Where this launcher records its children, scoped to the port it owns so
    two launchers on different ports never reap each other's servers."""
    return STATE_DIR / f"pids-{PORT}.json"


def local_config() -> dict:
    """launcher.local.json, re-read at most once a second and only when its
    mtime moved.

    It used to be re-opened and re-parsed once per project per status poll, and
    every one of those reads happened inside the global lock — the same lock a
    child's log line takes for every line it emits. On a local disk that is only
    waste; pointed at a UNC or removable path it is a multi-second stall that
    backs up the log readers until the children's pipes fill.
    """
    global _LOCAL_CACHE
    now = time.monotonic()
    with _LOCAL_LOCK:
        checked_at, mtime, config = _LOCAL_CACHE
        if now - checked_at < LOCAL_TTL:
            return config
    try:
        stamp = LOCAL_FILE.stat().st_mtime
    except OSError:
        stamp = -1.0                     # absent is the normal case, and is stable
    with _LOCAL_LOCK:
        if stamp == _LOCAL_CACHE[1]:
            _LOCAL_CACHE = (now, stamp, _LOCAL_CACHE[2])
            return _LOCAL_CACHE[2]
    fresh = load_json(LOCAL_FILE, {}) if stamp != -1.0 else {}
    fresh = fresh if isinstance(fresh, dict) else {}
    with _LOCAL_LOCK:
        _LOCAL_CACHE = (now, stamp, fresh)
    return fresh


def disabled_ids(config: dict | None = None) -> set[str]:
    """Project ids hidden on this machine by launcher.local.json."""
    values = (local_config() if config is None else config).get("disabled")
    return {str(value) for value in values} if isinstance(values, list) else set()


def resolve_root() -> tuple[Path | None, str, str | None]:
    """(root, where it came from, error). root is None when unresolvable."""
    raw = (os.environ.get("ORRERY_ROOT") or "").strip()
    source = "ORRERY_ROOT"
    if not raw:
        value = local_config().get("projects_root")
        if isinstance(value, str) and value.strip():
            raw, source = value.strip(), "launcher.local.json"
    if not raw:
        raw, source = str(HUB_DIR.parent), "default (the hub's parent directory)"
    try:
        root = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as e:
        return None, source, f"{raw!r} is not a usable path ({e})"
    if not root.is_dir():
        return None, source, f"{root} does not exist or is not a directory"
    return root, source, None


def read_projects() -> tuple[dict | None, str | None]:
    """projects.json, re-read at most once a second. (data, error)."""
    global _PROJECT_CACHE
    now = time.monotonic()
    with LOCK:
        checked_at, mtime, data, error = _PROJECT_CACHE
        if data is not None and now - checked_at < PROJECTS_TTL:
            return data, error
    try:
        stamp = PROJECTS_FILE.stat().st_mtime
    except OSError:
        result = (None, f"{PROJECTS_FILE} not found")
        with LOCK:
            _PROJECT_CACHE = (now, 0.0, None, result[1])
        return result
    with LOCK:
        if data is not None and stamp == mtime:
            _PROJECT_CACHE = (now, mtime, data, error)
            return data, error
    try:
        with PROJECTS_FILE.open("r", encoding="utf-8") as fh:
            fresh = json.load(fh)
    except (OSError, ValueError) as e:
        with LOCK:
            _PROJECT_CACHE = (now, stamp, None, f"projects.json is unreadable: {e}")
            return None, _PROJECT_CACHE[3]
    if not isinstance(fresh, dict) or not isinstance(fresh.get("projects"), list):
        with LOCK:
            _PROJECT_CACHE = (now, stamp, None,
                              "projects.json has no \"projects\" array")
            return None, _PROJECT_CACHE[3]
    with LOCK:
        _PROJECT_CACHE = (now, stamp, fresh, None)
    return fresh, None


def safe_relative_dir(value: object) -> str | None:
    """A "dev.dir" is a directory NAME relative to the projects root.

    Absolute paths, drive letters and .. segments are rejected outright: the
    public repository must never carry a machine path, and a relative path that
    climbs out of the root would be a traversal.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or ":" in text:
        return None
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def normalize_dev(project: dict) -> tuple[dict | None, str | None]:
    """Validate a project's "dev" block. Returns (dev, why it was rejected).

    (None, None) means the project simply declares no dev block and is not
    launchable — that is not an error. (None, reason) means it declares one that
    cannot be used: the card would otherwise vanish from the board with nothing
    said anywhere, which hides the mistake instead of reporting it. The reason
    rides out in /api/health and the UI puts it on screen.
    """
    dev = project.get("dev")
    if dev is None:
        return None, None
    if not isinstance(dev, dict):
        return None, "\"dev\" is not an object"
    directory = safe_relative_dir(dev.get("dir"))
    if directory is None:
        return None, (f"dev.dir {dev.get('dir')!r} is not a plain directory name "
                      f"relative to the projects root (no absolute paths, no "
                      f"drive letters, no \"..\")")
    command = dev.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, "dev.command is missing or empty"
    try:
        tokens = shlex.split(command.strip())
    except ValueError as e:
        return None, f"dev.command does not parse: {e}"
    if not tokens:
        return None, "dev.command is empty once parsed"

    port = dev.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        port = None

    kind = dev.get("kind") if dev.get("kind") in DEV_KINDS else "node"

    url = dev.get("url")
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or \
                (parsed.hostname or "") not in LOCAL_HOSTNAMES:
            url = None                   # only ever point the browser at localhost
    else:
        url = None

    pattern = dev.get("ready_pattern")
    if isinstance(pattern, str) and pattern:
        try:
            re.compile(pattern)
        except re.error:
            pattern = None
    else:
        pattern = None

    port_env = dev.get("port_env")
    if not isinstance(port_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                                                         port_env or ""):
        port_env = None
    port_arg = dev.get("port_arg")
    if not isinstance(port_arg, str) or not re.fullmatch(r"-{1,2}[A-Za-z0-9-]+",
                                                         port_arg or ""):
        port_arg = None

    requires = dev.get("requires")
    requires = requires if isinstance(requires, str) and requires else None

    return {
        "dir": directory,
        "command": command.strip(),
        "tokens": tokens,
        "port": port,
        "url": url,
        "kind": kind,
        "ready_pattern": pattern,
        "port_env": port_env,
        "port_arg": port_arg,
        "requires": requires,
        # A project can only be moved off a busy port if it exposes a way to say so.
        "can_relocate": bool(port and (port_env or port_arg)),
    }, None


def local_projects(config: dict | None = None) -> list[dict]:
    """Projects that exist only on this machine.

    Some work cannot be described in a public repository at all: client code
    under NDA, anything whose folder name and run command are themselves the
    disclosure. Those live in launcher.local.json, which is git-ignored, and
    are merged in here so every downstream feature — start, stop, health, git,
    open, context — treats them exactly like any other project.

    Each entry is shaped like a projects.json entry: an id, a name, and a dev
    block. Nothing about them is ever written back to projects.json.
    """
    raw = (local_config() if config is None else config).get("local_projects")
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        project = dict(entry)
        project["local_only"] = True
        project.setdefault("name", project["id"])
        out.append(project)
    return out


def launchable(data: dict, config: dict | None = None) -> list[tuple[dict, dict]]:
    """[(project, normalized dev)] for every project that declares a dev block.

    `config` lets a caller pass launcher.local.json in already, so a request that
    walks every project reads that file once instead of once per project.

    Local-only projects are appended last and never shadow a real one: an id
    already in projects.json is skipped, because silently overriding published
    data from a git-ignored file is the kind of divergence nobody would think
    to look for.
    """
    disabled = disabled_ids(config)
    out = []
    seen = set()
    for project in data.get("projects", []):
        if not isinstance(project, dict) or project.get("id") in disabled:
            continue
        dev, _error = normalize_dev(project)
        if dev is not None:
            out.append((project, dev))
            seen.add(project.get("id"))
    for project in local_projects(config):
        if project["id"] in disabled or project["id"] in seen:
            continue
        dev, _error = normalize_dev(project)
        if dev is not None:
            out.append((project, dev))
            seen.add(project["id"])
    return out


def dev_errors(data: dict, config: dict | None = None) -> list[str]:
    """Every dev block that was written but cannot be used, and why."""
    disabled = disabled_ids(config)
    out = []
    for project in data.get("projects", []):
        if not isinstance(project, dict) or project.get("id") in disabled:
            continue
        _dev, error = normalize_dev(project)
        if error is not None:
            out.append(f"{project.get('id', '<no id>')}: {error}")
    return out


def project_dir(root: Path, project_id: str, dev: dict,
                config: dict | None = None) -> Path:
    """Where this project actually lives; launcher.local.json may override it."""
    overrides = (local_config() if config is None else config).get("project_dirs")
    if isinstance(overrides, dict):
        value = overrides.get(project_id)
        if isinstance(value, str) and value.strip():
            return Path(value.strip()).expanduser()
    return root.joinpath(*dev["dir"].split("/"))


def url_for(dev: dict, port: int | None) -> str | None:
    """The declared url, with its port swapped for the port actually in use."""
    raw = dev.get("url")
    if raw:
        parsed = urlparse(raw)
        if port and parsed.port and parsed.port != port:
            host = parsed.hostname or "localhost"
            return urlunparse(parsed._replace(netloc=f"{host}:{port}"))
        return raw
    return f"http://localhost:{port}" if port else None


def command_argv(dev: dict, port: int | None) -> tuple[list[str] | None, str | None]:
    """Resolve the command to an argv list. (argv, error).

    argv[0] is resolved through PATH here so the child never needs a shell:
    shell=True with data from a JSON file is exactly the hole this avoids.
    """
    tokens = list(dev["tokens"])
    exe = shutil.which(tokens[0])
    if exe is None:
        return None, (f"command not found on PATH: {tokens[0]}"
                      f" — is it installed?")
    argv = [exe] + tokens[1:]
    if port and dev.get("port_arg"):
        if Path(tokens[0]).name.lower() in SCRIPT_RUNNERS and "--" not in argv:
            argv.append("--")            # npm/pnpm: forward the flag to the script
        argv += [dev["port_arg"], str(port)]
    return argv, None


def probe_ports(ports: set[int]) -> dict[int, bool]:
    """Probe every port at once, always outside the lock.

    A closed loopback port here does not answer with a refusal, it swallows the
    SYN, so each probe costs the full timeout. Sequentially that is seconds per
    tick for a handful of projects; in parallel it is one timeout for all of them.
    """
    wanted = sorted(ports)
    if not wanted:
        return {}
    if len(wanted) == 1:
        return {wanted[0]: port_open(wanted[0])}
    return dict(zip(wanted, PROBE_POOL.map(port_open, wanted)))


def tasklist_image(pid: int) -> str | None:
    """The image name of a live PID, or None. Used to avoid killing a reused PID."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return "posix"
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip().splitlines()
    if not line or not line[0].startswith('"'):
        return None                      # "INFO: No tasks are running..."
    return line[0].split('","')[0].lstrip('"')


def listeners_on_ports(ports: set[int]) -> dict[int, int]:
    """{port: listening pid} for the ports asked about, from ONE netstat call.

    Resolving them one at a time would be one process spawn per port; the table
    is the same table every time, so it is read once and indexed.
    """
    if os.name != "nt" or not ports:
        return {}
    wanted = {str(port) for port in ports}
    try:
        # No "-p tcp" filter: that shows IPv4 only, and the servers that bind
        # the IPv6 loopback are exactly the ones worth finding here.
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, int] = {}
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP") or \
                parts[3].upper() != "LISTENING":
            continue
        text = parts[1].rsplit(":", 1)[-1]
        if text not in wanted:
            continue
        try:
            port, pid = int(text), int(parts[4])
        except ValueError:
            continue
        found.setdefault(port, pid)      # first LISTENING row wins (v4 before v6)
    return found


def pid_on_port(port: int) -> int | None:
    """Which PID is listening on a port. Used to name a holder and to sweep
    grandchildren that outlived the process Orrery actually spawned."""
    return listeners_on_ports({port}).get(port)


def describe_holder(port: int, pid: int | None = None) -> str:
    """A human name for whatever is sitting on a port we wanted."""
    pid = pid_on_port(port) if pid is None else pid
    if pid is None:
        return "a process Orrery did not start"
    image = tasklist_image(pid)
    return f"pid {pid}{f' ({image})' if image else ''}, not started by Orrery"


# ---- process identity (ctypes, still standard library)
#
# An image name is not an identity. shutil.which("npm") resolves to npm.CMD, a
# batch file, and CreateProcess runs batch files through cmd.exe — so the child
# Orrery records for six of the eight launchable projects IS cmd.exe, and the
# process actually listening is a node.exe grandchild. "The pid is live and its
# image is still cmd.exe" therefore matches almost any cmd.exe on the machine,
# which is exactly the wrong thing to hand to taskkill /T /F after a reboot has
# recycled the number. Creation time is the identity: a recycled pid always has
# a different one. The parent table turns a listening grandchild back into the
# process Orrery spawned.

_WINAPI: dict | None = None


def winapi() -> dict | None:
    """Bind the two kernel32 calls this needs, once. None off Windows."""
    global _WINAPI
    if _WINAPI is not None:
        return _WINAPI or None
    if os.name != "nt":
        _WINAPI = {}
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        filetime = ctypes.POINTER(wintypes.FILETIME)
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime,
                                        filetime, filetime)
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        entry_ptr = ctypes.POINTER(PROCESSENTRY32W)
        k32.Process32FirstW.argtypes = (wintypes.HANDLE, entry_ptr)
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = (wintypes.HANDLE, entry_ptr)
        k32.Process32NextW.restype = wintypes.BOOL
        _WINAPI = {"ctypes": ctypes, "wintypes": wintypes, "k32": k32,
                   "entry": PROCESSENTRY32W,
                   "invalid": ctypes.c_void_p(-1).value}
    except Exception:                    # no ctypes, locked-down host, anything
        _WINAPI = {}
        return None
    return _WINAPI


def process_created(pid: int) -> int | None:
    """The process's creation time as a raw FILETIME, or None if unreadable.

    Two live processes never share both a pid and a creation time, so this is
    the one cheap fact that tells "still the child I started" from "that number
    belongs to something else now".
    """
    api = winapi()
    if api is None:
        return None
    ctypes, wintypes, k32 = api["ctypes"], api["wintypes"], api["k32"]
    handle = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not k32.GetProcessTimes(handle, ctypes.byref(created),
                                   ctypes.byref(exited), ctypes.byref(kernel),
                                   ctypes.byref(user)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    except Exception:
        return None
    finally:
        k32.CloseHandle(handle)


def process_parents() -> dict[int, int]:
    """{pid: parent pid} for every process, from one Toolhelp snapshot (~7ms)."""
    api = winapi()
    if api is None:
        return {}
    ctypes, k32 = api["ctypes"], api["k32"]
    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # SNAPPROCESS
    if not snapshot or snapshot == api["invalid"]:
        return {}
    try:
        entry = api["entry"]()
        entry.dwSize = ctypes.sizeof(api["entry"])
        parents: dict[int, int] = {}
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
        return parents
    except Exception:
        return {}
    finally:
        k32.CloseHandle(snapshot)


def working_set_bytes(pid: int) -> int:
    """Resident memory for one process, via psapi. 0 when it cannot be read."""
    api = winapi()
    if api is None:
        return 0
    ctypes, wintypes = api["ctypes"], api["wintypes"]
    k32 = api["k32"]

    class COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION: enough to read counters,
    # and granted for our own children without elevation.
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0
    try:
        counters = COUNTERS()
        counters.cb = ctypes.sizeof(COUNTERS)
        if not ctypes.WinDLL("psapi").GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    except Exception:
        return 0
    finally:
        k32.CloseHandle(handle)


def children_map(parents: dict[int, int]) -> dict[int, list[int]]:
    """Invert {pid: parent} once so descendants can be walked downward."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        if parent > 0 and parent != pid:
            children.setdefault(parent, []).append(pid)
    return children


def tree_memory(pid: int, children: dict[int, list[int]],
                depth: int = REAP_DEPTH) -> int:
    """Resident memory for a process AND its descendants.

    The tracked pid is almost never where the memory is. Six of the eight
    launchable projects run through npm, so Orrery's child is a cmd.exe sitting
    at a few megabytes while the node.exe grandchild holds hundreds. Reporting
    the parent alone would make a 700 MB dev server look free, which is exactly
    backwards on a laptop that is already struggling.

    Walks DOWN from the pid rather than testing every process on the machine:
    a status poll touches the four or five processes in this tree, not the two
    hundred in the session. The caller's Toolhelp snapshot is reused, so the
    whole readout costs no extra syscall beyond one OpenProcess per descendant.
    """
    total = 0
    seen: set[int] = set()
    frontier = [(pid, 0)]
    while frontier:
        current, level = frontier.pop()
        if current in seen or level > depth:
            continue
        seen.add(current)
        total += working_set_bytes(current)
        for child in children.get(current, ()):
            frontier.append((child, level + 1))
    return total


def is_ancestor_of(candidate: int, pid: int, parents: dict[int, int],
                   depth: int = REAP_DEPTH) -> bool:
    """True when `candidate` is `pid` itself or within `depth` parents of it.

    Bounded on purpose: every chain eventually reaches explorer.exe, and an
    unbounded walk would call the whole session a descendant.
    """
    seen: set[int] = set()
    current = pid
    for _ in range(depth + 1):
        if current == candidate:
            return True
        if current in seen or current <= 0:
            return False
        seen.add(current)
        current = parents.get(current, 0)
    return False


def kill_tree(pid: int) -> str | None:
    """Kill a process AND its children. npm/next/vite outlive a plain terminate."""
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=KILL_WAIT_SECS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as e:
            return str(e) or type(e).__name__
        if result.returncode != 0:
            return (result.stderr or result.stdout or "").strip() or \
                f"taskkill exited {result.returncode}"
        return None
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError) as e:
        return str(e) or type(e).__name__
    return None

# ------------------------------------------------------------------ processes


class Proc:
    """One launch of one project: the child, its log ring, and its state."""

    def __init__(self, project_id: str, dev: dict, port: int | None,
                 popen: subprocess.Popen, argv: list[str], cwd: Path,
                 relocated_from: int | None, can_signal: bool,
                 seq_start: int = 0) -> None:
        self.id = project_id
        self.dev = dev
        self.port = port
        self.popen = popen
        self.argv = argv
        self.cwd = cwd
        self.relocated_from = relocated_from
        self.can_signal = can_signal     # is a graceful signal even deliverable?
        self.state = "starting"
        self.stopping = False
        self.exit_code: int | None = None
        self.started_at = time.monotonic()
        self.started_iso = datetime.now().isoformat(timespec="seconds")
        self.ended_at: float | None = None
        self.ready_hint = False
        self.image: str | None = None
        self.created: int | None = None   # FILETIME, the child's real identity
        # Line numbers continue across relaunches of the same project: a drawer
        # polling with the previous run's cursor would otherwise sit empty until
        # the new run had produced more lines than the old one.
        self.seq = seq_start
        self.first_seq = seq_start + 1   # oldest line still in the ring buffer
        self.lines: deque[dict] = deque(maxlen=MAX_LOG_LINES)
        self.ready_re = re.compile(dev["ready_pattern"]) if dev.get("ready_pattern") \
            else None
        self.reader: threading.Thread | None = None

    # -- called from the reader thread and from start/stop

    def note(self, text: str) -> None:
        """Append a launcher-authored line (marked so it reads as ours)."""
        self.append(f"[orrery] {text}")

    def append(self, text: str) -> None:
        clean = strip_ansi(text).rstrip("\r\n")
        with LOCK:
            self.seq += 1
            if len(self.lines) == MAX_LOG_LINES:
                self.first_seq = self.lines[0]["n"] + 1
            elif not self.lines:
                self.first_seq = self.seq
            self.lines.append({"n": self.seq, "t": hms(), "line": clean})
            if self.ready_re is not None and not self.ready_hint and \
                    self.ready_re.search(clean):
                self.ready_hint = True

    def alive(self) -> bool:
        return self.popen.poll() is None

    def uptime(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return max(0, int(end - self.started_at))

    def snapshot_logs(self, since: int) -> tuple[list[dict], int, int]:
        """Lines after `since`, the new cursor, and how many were missed.

        Missed lines are real: the ring buffer holds the last MAX_LOG_LINES, and
        a relaunch starts a fresh buffer, so a drawer that was away for a while
        is told the size of the gap rather than being quietly shortchanged.
        """
        with LOCK:
            rows = [row for row in self.lines if row["n"] > since]
            missed = max(0, self.first_seq - since - 1) if since > 0 else 0
            return rows, self.seq, missed


def pump(proc: Proc) -> None:
    """Drain the child's merged stdout on a dedicated thread.

    The HTTP threads never touch the pipe: a full pipe would otherwise wedge the
    child and every request that waited on it.
    """
    stream = proc.popen.stdout
    if stream is None:
        return
    try:
        for raw in stream:
            proc.append(raw)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def write_pids_unlocked() -> None:
    """Record live children so a crashed launcher can clean up on next boot."""
    payload = {}
    for pid_key, proc in PROCS.items():
        if proc.alive():
            payload[pid_key] = {
                "pid": proc.popen.pid,
                "port": proc.port,
                "image": proc.image,
                "created": proc.created,   # FILETIME: rules out pid reuse outright
                "started": proc.started_iso,
                "launcher_pid": os.getpid(),
            }
    try:
        save_json(pids_file(), payload)
    except OSError:
        pass


def reap_orphans() -> None:
    """Adopt and clean up children a previous launcher left behind.

    This function hands a pid to `taskkill /T /F`, which kills a whole tree, so
    the bar for "this is my old child" is set at proof, not resemblance:

      1. A port must have been recorded. Without one there is nothing that ties
         the number to a server, so the entry is dropped, never killed.
      2. That port must still be held, and the process holding it must be the
         recorded pid or a descendant of it within REAP_DEPTH generations. The
         port is the identity; the pid alone never was. (Six of the eight
         launchable projects run npm, so the recorded pid is a cmd.exe whose
         node.exe grandchild is the actual listener — hence the walk.)
      3. The recorded creation time must still match, which rules out pid reuse
         outright. Older state files carry no creation time; they fall back to
         the image-name check, still gated on 1 and 2.

    Anything that fails a check is reported in BOOT_NOTES and left running.
    """
    stale = load_json(pids_file(), {})
    if not isinstance(stale, dict) or not stale:
        return
    ports = {meta["port"] for meta in stale.values()
             if isinstance(meta, dict) and isinstance(meta.get("port"), int)}
    listeners = listeners_on_ports(ports)
    parents = process_parents()

    for project_id, meta in stale.items():
        if not isinstance(meta, dict):
            continue
        pid = meta.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        port = meta.get("port")
        if not isinstance(port, int):
            BOOT_NOTES.append(
                f"{project_id}: pid {pid} was recorded without a port, so there "
                f"is nothing to identify it by; left alone")
            continue
        image = tasklist_image(pid)
        if image is None:
            BOOT_NOTES.append(f"{project_id}: stale pid {pid}, already gone")
            continue

        recorded_created = meta.get("created")
        created = process_created(pid)
        if isinstance(recorded_created, int):
            if created is None:
                BOOT_NOTES.append(
                    f"{project_id}: pid {pid} ({image}) could not be identified "
                    f"— its creation time is unreadable; left alone")
                continue
            if created != recorded_created:
                BOOT_NOTES.append(
                    f"{project_id}: pid {pid} was recycled — the process using it "
                    f"now ({image}) started later than the one recorded; left alone")
                continue
        elif isinstance(meta.get("image"), str) and \
                image.lower() != meta["image"].lower():
            BOOT_NOTES.append(
                f"{project_id}: pid {pid} now belongs to {image}, "
                f"not the recorded {meta['image']}; left alone")
            continue

        holder = listeners.get(port)
        if holder is None:
            BOOT_NOTES.append(
                f"{project_id}: nothing is listening on port {port} any more, so "
                f"pid {pid} ({image}) is not the server that was recorded; left alone")
            continue
        if not is_ancestor_of(pid, holder, parents):
            other = tasklist_image(holder)
            BOOT_NOTES.append(
                f"{project_id}: port {port} is held by pid {holder}"
                f"{f' ({other})' if other else ''}, which did not come from "
                f"pid {pid}; left alone")
            continue

        error = kill_tree(pid)
        BOOT_NOTES.append(
            f"{project_id}: killed orphaned pid {pid} ({image}), which still held "
            f"port {port}"
            if error is None else
            f"{project_id}: could not kill orphaned pid {pid}: {error}")
    try:
        save_json(pids_file(), {})
    except OSError:
        pass


def choose_port(dev: dict, taken: set[int], fresh: dict[int, bool]) -> int | None:
    """The first free port at or above the declared one, skipping other defaults."""
    base = dev["port"]
    if base is None:
        return None
    for candidate in range(base + 1, min(base + 1 + RELOCATE_SCAN, 65536)):
        if candidate in taken:
            continue
        if fresh.get(candidate) if candidate in fresh else port_open(candidate):
            continue
        return candidate
    return None


def start_project(project: dict, dev: dict, root: Path,
                  wanted_port: int | None) -> tuple[int, dict]:
    """Spawn one dev server. Returns (http status, payload)."""
    project_id = str(project.get("id"))

    # One start at a time. The whole decision - is it already running, is
    # the port free, which port do we take - has to be atomic with the spawn,
    # or two clicks a moment apart both pass the same checks and both claim
    # the same port: one server wins the bind, the other dies of EADDRINUSE
    # (or quietly moves itself and makes Orrery's reported port a lie), and a
    # double start of one project leaves an untracked child nothing can stop.
    with START_LOCK:
        with LOCK:
            existing = PROCS.get(project_id)
            # A finished record sticks around so its logs and exit code survive, so
            # "busy" means the child is still alive, or a stop is still in flight —
            # never just "this project was stopped once".
            if existing is not None and (existing.alive() or
                                         (existing.stopping and existing.ended_at is None)):
                return 409, {"error": f"{project.get('name', project_id)} is already "
                                      f"{'stopping' if existing.stopping else 'running'}",
                             "id": project_id, "pid": existing.popen.pid,
                             "port": existing.port}

        cwd = project_dir(root, project_id, dev)
        if not cwd.is_dir():
            return 404, {"error": f"project directory not found: {cwd}",
                         "id": project_id,
                         "hint": "check ORRERY_ROOT or launcher.local.json"}

        # Port selection. Probing blocks, so it happens before the lock is taken and
        # the result is handed straight into the (short) critical section.
        relocated_from = None
        note = None
        explicit = isinstance(wanted_port, int) and not isinstance(wanted_port, bool)
        port = wanted_port if explicit else dev["port"]
        if port is not None:
            with LOCK:
                ours = {p.port: p.id for p in PROCS.values() if p.alive() and p.port}
                taken = set(ours)
            fresh = probe_ports({port})
            if fresh.get(port) or port in taken:
                owner = ours.get(port)
                holder = f"{owner} (started by Orrery)" if owner is not None \
                    else describe_holder(port)
                # An explicitly requested port is never silently swapped: the caller
                # asked for that one, so it gets a straight answer instead.
                if explicit or not dev["can_relocate"]:
                    return 409, {
                        "error": f"port {port} is already in use by {holder}"
                                 + ("" if explicit else
                                    f", and {project.get('name', project_id)} has no "
                                    f"way to run on a different port"),
                        "id": project_id, "port": port, "held_by": holder,
                        "can_relocate": dev["can_relocate"]}
                declared = set()          # no lock: read_projects takes its own
                data, _ = read_projects()
                if data is not None:
                    declared = {d["port"] for p, d in launchable(data)
                                if d["port"] and p.get("id") != project_id}
                moved = choose_port(dev, taken | declared | {port}, fresh)
                if moved is None:
                    return 409, {
                        "error": f"port {port} is held by {holder} and no free port "
                                 f"was found in the {RELOCATE_SCAN} above it",
                        "id": project_id, "port": port, "held_by": holder,
                        "can_relocate": True}
                note = f"port {port} was held by {holder}; using {moved} instead"
                relocated_from, port = port, moved

        argv, error = command_argv(dev, port)
        if argv is None:
            return 500, {"error": error, "id": project_id}

        env = os.environ.copy()
        env["NO_COLOR"] = "1"                # the ring buffer holds text, not escapes
        env["FORCE_COLOR"] = "0"
        env["BROWSER"] = "none"              # vite/next must not open their own tab
        env["ORRERY"] = "1"
        if port and dev["port_env"]:
            env[dev["port_env"]] = str(port)

        kwargs: dict = {}
        if os.name == "nt":
            # New process group so a signal can be aimed at this child alone, and no
            # console window so a pythonw launch does not flash one per server.
            # Having no console is also why CTRL_BREAK cannot reach it: console
            # control events only travel to processes sharing the sender's console.
            # The tree kill is the real mechanism on Windows, so stop() does not
            # burn a grace window waiting for a signal that cannot be delivered.
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            can_signal = False
        else:
            kwargs["start_new_session"] = True
            can_signal = True

        with LOCK:                           # holds the port claim across the spawn
            try:
                popen = subprocess.Popen(
                    argv, cwd=str(cwd), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    close_fds=True, **kwargs)
            except (OSError, ValueError) as e:
                return 500, {"error": f"could not start {dev['command']}: "
                                      f"{e or type(e).__name__}", "id": project_id}
            previous = PROCS.get(project_id)
            proc = Proc(project_id, dev, port, popen, argv, cwd, relocated_from,
                        can_signal, previous.seq if previous is not None else 0)
            PROCS[project_id] = proc

    proc.note(f"{dev['command']} in {cwd}"
              + (f" on port {port}" if port else ""))
    if note:
        proc.note(note)
    if dev["requires"]:
        proc.note(f"needs: {dev['requires']}")
    proc.image = tasklist_image(popen.pid)
    proc.created = process_created(popen.pid)   # read now, while it is certainly ours
    proc.reader = threading.Thread(target=pump, args=(proc,),
                                   name=f"orrery-log-{project_id}", daemon=True)
    proc.reader.start()
    with LOCK:
        write_pids_unlocked()

    return 200, {"ok": True, "id": project_id, "pid": popen.pid, "port": port,
                 "status": "starting", "relocated_from": relocated_from,
                 "url": url_for(dev, port), "note": note}


def stop_project(project_id: str, timeout: float = GRACE_SECS) -> tuple[int, dict]:
    """Terminate a child and everything it spawned. Graceful, then forced."""
    with LOCK:
        proc = PROCS.get(project_id)
        if proc is None:
            return 404, {"error": f"{project_id} was not started by Orrery",
                         "id": project_id}
        finalize_unlocked(proc, time.monotonic())   # report the real state below
        if not proc.alive():
            return 409, {"error": f"{project_id} is not running", "id": project_id,
                         "status": proc.state, "last_exit_code": proc.exit_code}
        if proc.stopping:
            return 409, {"error": f"{project_id} is already stopping",
                         "id": project_id}
        proc.stopping = True
        proc.state = "stopping"
        pid = proc.popen.pid

    # Everything below blocks, so the lock is released first.
    proc.note("stopping")
    try:                                 # ask nicely first, always
        if os.name == "nt":
            proc.popen.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ValueError, AttributeError, ProcessLookupError):
        pass                             # already gone, or nothing listening
    if proc.can_signal:
        # Only wait when the signal could actually be acted on. A console-less
        # Windows child cannot receive CTRL_BREAK, and measuring that wait cost
        # a flat five seconds per stop for a shutdown that never happened.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.alive():
            time.sleep(0.1)

    forced = False
    if proc.alive():
        # npm/next/vite all leave a node.exe behind a plain terminate, so the
        # whole tree goes at once while the parent still knows its children.
        forced = True
        error = kill_tree(pid)
        if error is not None:
            proc.note(f"taskkill: {error}")
        deadline = time.monotonic() + KILL_WAIT_SECS
        while time.monotonic() < deadline and proc.alive():
            time.sleep(0.1)
    try:
        code = proc.popen.wait(timeout=2)
    except subprocess.TimeoutExpired:
        code = proc.popen.poll()
    if proc.reader is not None:
        proc.reader.join(timeout=2)

    swept = sweep_port(proc)
    if swept:
        proc.note(swept)

    with LOCK:
        proc.exit_code = code
        proc.ended_at = time.monotonic()
        proc.state = "stopped"
        write_pids_unlocked()
    proc.note(f"stopped (exit {code})" + (" after a forced tree kill" if forced
                                          else ""))
    return 200, {"ok": True, "id": project_id, "exit_code": code, "forced": forced,
                 "swept": swept}


def sweep_port(proc: Proc) -> str | None:
    """Last resort: a grandchild that survived its parent still holds the port.

    Only ever fires for a port Orrery itself launched on, and only once the
    process it spawned is already gone.
    """
    if not proc.port:
        return None
    deadline = time.monotonic() + SETTLE_SECS
    while time.monotonic() < deadline:
        if not port_open(proc.port):
            return None
        time.sleep(0.2)
    orphan = pid_on_port(proc.port)
    if orphan is None or orphan == os.getpid():
        return None
    with LOCK:
        if any(other.alive() and other.popen.pid == orphan
               for other in PROCS.values()):
            return None                  # that is another Orrery child, leave it
    image = tasklist_image(orphan)
    error = kill_tree(orphan)
    return (f"port {proc.port} was still held by pid {orphan}"
            f"{f' ({image})' if image else ''}; "
            + ("killed it" if error is None else f"could not kill it: {error}"))


def stop_everything() -> list[str]:
    """Stop every child Orrery started, in parallel.

    Sequentially this would be one grace window per project, and "Stop all"
    would sit there for half a minute. Used by /api/stop-all, /api/quit and by
    the shutdown path.

    START_LOCK is held throughout so a start cannot register a child between the
    snapshot and the kills — on the quit path that child would be orphaned, and
    "Stop all" would report success while a server kept running.
    """
    with START_LOCK:
        with LOCK:
            ids = [project_id for project_id, proc in PROCS.items() if proc.alive()]
        if not ids:
            return []
        stopped: list[str] = []

        def worker(project_id: str) -> None:
            code, _payload = stop_project(project_id)   # takes LOCK, never START_LOCK
            if code == 200:
                with LOCK:
                    stopped.append(project_id)

        threads = [threading.Thread(target=worker, args=(project_id,),
                                    name=f"orrery-stop-{project_id}", daemon=True)
                   for project_id in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=GRACE_SECS + KILL_WAIT_SECS + 10)
        return sorted(stopped)

# ------------------------------------------------------------------ watchdog


def finalize_unlocked(proc: Proc, now: float) -> None:
    """Record the exit of a child that is no longer running. Caller holds LOCK.

    Both the watchdog and every /api/projects response run this, so a server
    that dies between two ticks is never reported as a plain "stopped" with no
    exit code for the rest of the second.
    """
    if proc.state in ("stopped", "crashed", "stopping"):
        return                           # already final, or stop_project owns it
    code = proc.popen.poll()
    if code is None:
        return
    proc.exit_code = code
    proc.ended_at = now
    # A dev server that ends on its own has crashed, whatever it says on the way
    # out; only a clean zero is reported as an ordinary stop.
    proc.state = "stopped" if code == 0 else "crashed"


def refresh_holders(held: set[int], now: float) -> None:
    """Name whoever holds a port Orrery wanted but did not take.

    "something Orrery did not start" is not an answer anybody can act on, so the
    pid and image are resolved and cached here rather than in the request path:
    netstat and tasklist are process spawns, far too slow for a 2s poll, and the
    answer barely changes. One netstat covers every held port at once.
    """
    with LOCK:
        stale = {port for port in held
                 if port not in HOLDERS or now - HOLDERS[port][0] > HOLDER_TTL}
        gone = set(HOLDERS) - held
    if gone:
        with LOCK:
            for port in gone:
                HOLDERS.pop(port, None)
    if not stale:
        return
    listeners = listeners_on_ports(stale)        # blocking, outside the lock
    described = {port: describe_holder(port, listeners.get(port))
                 for port in stale}
    with LOCK:
        for port, text in described.items():
            HOLDERS[port] = (now, text)


def refresh_state() -> None:
    """One tick of the state machine: probe ports, then apply the transitions."""
    data, _ = read_projects()
    wanted: set[int] = set()
    if data is not None:
        for _project, dev in launchable(data):
            if dev["port"]:
                wanted.add(dev["port"])
    with LOCK:
        for proc in PROCS.values():
            if proc.port:
                wanted.add(proc.port)
    fresh = probe_ports(wanted)          # blocking, deliberately outside the lock
    now = time.monotonic()
    with LOCK:
        PORT_STATE.update(fresh)
        ours = {proc.port for proc in PROCS.values() if proc.alive() and proc.port}
        for proc in PROCS.values():
            finalize_unlocked(proc, now)
            if proc.state != "starting" or not proc.alive():
                continue
            ready = proc.ready_hint
            if not ready and proc.port:
                ready = bool(PORT_STATE.get(proc.port))
            if not ready and not proc.port and not proc.ready_re:
                ready = now - proc.started_at > NO_PORT_READY_SECS
            if ready:
                proc.state = "running"
    refresh_holders({port for port in wanted
                     if fresh.get(port) and port not in ours}, now)


def watchdog() -> None:
    global WATCHDOG_ERROR
    while not STOP_EVENT.is_set():
        try:
            refresh_state()
            WATCHDOG_ERROR = None
        except Exception as e:
            # A watchdog that dies freezes every card at "starting", so it can
            # never be allowed to raise — but a silent failure is worse than a
            # loud one, so the last error rides along in /api/health.
            WATCHDOG_ERROR = f"{type(e).__name__}: {e}"
        STOP_EVENT.wait(WATCH_INTERVAL)

# ------------------------------------------------------------------ readiness
#
# A live process is not a working server. `npm run dev` can survive a syntax
# error and never serve a byte; a framework can bind its port and then fail every
# request; a port stays open for exactly as long as something holds the socket,
# which is not the same question as "does this answer". None of that is visible
# from the process table or from connect(), so every running project with a url
# gets a real HTTP GET, and only a probe that comes back earns a green dot.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Answer a redirect instead of following it.

    A dev server that redirects "/" at an identity provider would otherwise turn
    a local readiness check into an unattended outbound request every five
    seconds. A 3xx already proves the server is answering, which is the entire
    question, so nothing is gained by chasing it: returning None makes urllib
    raise HTTPError carrying the 3xx, and it is classified like any other reply.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ProxyHandler({}) on purpose: with http_proxy set in the environment, urllib
# would route a probe of 127.0.0.1 through a proxy — off the machine entirely.
HEALTH_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            NoRedirect)


def probe_health(url: str) -> dict:
    """One readiness GET. Never raises: the verdict IS the return value.

    HEALTH_TIMEOUT is a ceiling on the whole probe, not on each socket operation,
    and getting that right takes both steps below. `localhost` resolves to two
    addresses on Windows, and a timeout handed to urllib is spent once per
    address — so a single GET at a port nobody holds costs two full timeouts,
    and the loop's promise quietly doubles. Connecting first says which family is
    actually there in a fraction of the budget, and the GET then goes straight at
    that address with what is left of it, under the original Host header.
    """
    started = time.monotonic()
    sent = started

    def result(health: str, status: int | None, detail: str | None) -> dict:
        # Latency is the HTTP exchange rather than the connect probe in front of
        # it — "how long did this server take to answer" is the useful number.
        # A probe that never got as far as a request reports the wait it did
        # spend, because that is the only wait there was.
        return {"health": health, "http_status": status, "detail": detail,
                "latency_ms": int((time.monotonic() - sent) * 1000)}

    parsed = urlparse(url)               # belt and braces, as everywhere else
    if parsed.scheme not in ("http", "https") or \
            (parsed.hostname or "") not in LOCAL_HOSTNAMES:
        return result("error", None, f"refusing to probe a non-local url: {url}")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return result("error", None, f"unusable url: {url}")

    found = listening_address(port)
    if found is None:
        return result("unreachable", None, f"nothing is listening on port {port}")
    address = found[1]
    target = urlunparse(parsed._replace(
        netloc=f"[{address}]:{port}" if ":" in address else f"{address}:{port}"))
    remaining = max(0.25, HEALTH_TIMEOUT - (time.monotonic() - started))

    request = urllib.request.Request(target, method="GET", headers={
        "User-Agent": "Orrery readiness probe", "Accept": "*/*",
        "Connection": "close",
        # The name the project declared, so a dev server that filters by Host
        # (vite's allowedHosts, and every framework that copied it) sees what it
        # would see from the browser rather than a raw loopback address.
        "Host": parsed.netloc})
    sent = time.monotonic()
    try:
        response = HEALTH_OPENER.open(request, timeout=remaining)
    except urllib.error.HTTPError as e:
        # A status is an answer. A dev server that only serves /app returns 404
        # at the root and is working perfectly; only a 5xx says the thing behind
        # the socket is broken, which deserves a different colour from silence.
        status = e.code
        e.close()
        return result("error" if status >= 500 else "ok", status,
                      f"HTTP {status}" if status >= 400 else None)
    except urllib.error.URLError as e:
        reason = e.reason
        detail = str(reason) if reason is not None else (str(e) or "no answer")
        # A socket-level failure (refused, timed out, reset) is the server not
        # being there; anything else is the probe itself going wrong.
        return result("unreachable" if isinstance(reason, OSError) else "error",
                      None, detail or type(reason).__name__)
    except (TimeoutError, OSError) as e:
        return result("unreachable", None, str(e) or type(e).__name__)
    except Exception as e:              # a reply that is not HTTP at all
        return result("error", None, str(e) or type(e).__name__)

    try:
        status = response.status
    finally:
        response.close()
    # The body is deliberately never read: the status line is the whole answer,
    # and HEALTH_TIMEOUT is a per-socket-operation timeout, not a total one, so
    # a server trickling bytes could hold a reader open long past it.
    return result("ok", status, None)


def probe_one(target: tuple[str, float, str]) -> tuple[str, float, dict]:
    """Probe one target and stamp the verdict. Runs on the health pool."""
    project_id, run, url = target
    payload = probe_health(url)
    payload["url"] = url
    payload["checked_at"] = datetime.now().isoformat(timespec="seconds")
    return project_id, run, payload


def health_round() -> None:
    """One readiness tick: pick what is due, probe it in parallel, record it."""
    now = time.monotonic()
    with LOCK:
        live: dict[str, tuple[float, str]] = {}
        for project_id, proc in PROCS.items():
            # Never probe something that is not up — a stopped project is not
            # unhealthy, it is absent, and a stopping one is on its way out.
            # A starting one IS probed: the gap between "the port is bound" and
            # "the server answers" is the exact window this feature exists to
            # show, and it is also how the first green arrives the moment it is
            # true rather than up to HEALTH_INTERVAL later.
            if not proc.alive() or proc.state not in ("starting", "running"):
                continue
            url = url_for(proc.dev, proc.port)
            if url:
                live[project_id] = (proc.started_at, url)
        for gone in set(HEALTH) - set(live):
            HEALTH.pop(gone, None)       # a stopped card never keeps a green dot
        due = []
        for project_id, (run, url) in live.items():
            record = HEALTH.get(project_id)
            if record is None or record[0] != run:
                due.append((project_id, run, url))   # nothing known about this run
                continue
            # A server that answered is asked again at the slow cadence; one
            # that did not is asked again soon, so a dev server that needed two
            # more seconds to compile turns green two seconds later and not five.
            gap = HEALTH_INTERVAL if record[2].get("health") == "ok" \
                else HEALTH_RETRY
            if now - record[1] >= gap:
                due.append((project_id, run, url))
    if not due:
        return

    # Parallel, and always outside the lock. One unreachable server would
    # otherwise hold LOCK for the full timeout — the same lock Proc.append()
    # takes for every line every child prints, and that every status poll needs.
    results = list(HEALTH_POOL.map(probe_one, due))

    now = time.monotonic()
    with LOCK:
        for project_id, run, payload in results:
            proc = PROCS.get(project_id)
            # The run may have stopped, or been restarted onto another port,
            # while the probe was in flight; that verdict is about a server that
            # no longer exists.
            if proc is not None and proc.alive() and proc.started_at == run:
                HEALTH[project_id] = (run, now, payload)


def health_loop() -> None:
    global HEALTH_ERROR
    while not STOP_EVENT.is_set():
        try:
            health_round()
            HEALTH_ERROR = None
        except Exception as e:
            HEALTH_ERROR = f"{type(e).__name__}: {e}"   # loud, never fatal
        STOP_EVENT.wait(HEALTH_TICK)

# ----------------------------------------------------------------------- git


def git_read(directory: Path) -> dict:
    """branch / dirty / ahead / behind for one directory. Never raises.

    One `git status --porcelain=v2 --branch` answers all of it: v2 is the format
    git documents as machine-readable, and its header lines carry the branch, the
    upstream and the ahead/behind counts, so the obvious three-call version
    (status --porcelain, rev-parse --abbrev-ref HEAD, rev-list --left-right
    --count @{u}...HEAD) collapses into a single spawn. On Windows the spawn is
    the expensive part, and this runs for every project.

    Every failure mode has an answer: no git, no repository, no upstream, a
    detached HEAD, an unborn branch, and a call that hangs (killed at
    GIT_TIMEOUT). "Not a repository" is not an error — the card simply says
    nothing about git — so it is the one failure that returns a clean blank.
    """
    blank = {"is_repo": False, "branch": None, "detached": False,
             "upstream": None, "dirty": 0, "ahead": None, "behind": None,
             "error": None,
             "checked_at": datetime.now().isoformat(timespec="seconds")}
    exe = shutil.which("git")
    if exe is None:
        return {**blank, "error": "git is not on PATH"}
    if not directory.is_dir():
        return {**blank, "error": f"directory not found: {directory}"}

    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"      # never take index.lock: a background
                                         # status must not collide with the
                                         # commit he is making in that repo
    env["GIT_TERMINAL_PROMPT"] = "0"     # never stop to ask for credentials
    env["LC_ALL"] = "C"                  # fatal messages in English, so the
    env["LANGUAGE"] = ""                 # "not a repository" test is not localised
    try:
        result = subprocess.run(
            [exe, "status", "--porcelain=v2", "--branch"],
            cwd=str(directory), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        # Reported as a repository on purpose: a directory that is not one fails
        # in milliseconds, so a call slow enough to hit the ceiling is a real
        # worktree that is busy, locked, or on a disk that went away.
        return {**blank, "is_repo": True,
                "error": f"git did not answer within {GIT_TIMEOUT:g}s"}
    except (OSError, subprocess.SubprocessError) as e:
        return {**blank, "error": f"git could not be run: {e or type(e).__name__}"}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "not a git repository" in stderr.lower():
            return blank
        return {**blank, "is_repo": True,
                "error": stderr.splitlines()[0] if stderr else
                f"git exited {result.returncode}"}

    branch = upstream = oid = None
    detached = False
    ahead = behind = None
    dirty = 0
    for line in (result.stdout or "").splitlines():
        if not line.startswith("# "):
            if line.strip():
                dirty += 1               # one line per changed or untracked path
            continue
        parts = line[2:].split()
        if len(parts) < 2:
            continue
        if parts[0] == "branch.head":
            if parts[1] == "(detached)":
                detached = True
            else:
                branch = parts[1]
        elif parts[0] == "branch.oid":
            oid = parts[1]
        elif parts[0] == "branch.upstream":
            upstream = parts[1]
        elif parts[0] == "branch.ab" and len(parts) > 2:
            # Present only when the upstream ref exists locally; without it
            # ahead/behind stay None, which is "unknown", not "zero".
            try:
                ahead, behind = abs(int(parts[1])), abs(int(parts[2]))
            except ValueError:
                ahead = behind = None
    if detached:
        branch = oid[:7] if oid and oid != "(initial)" else "detached"
    return {"is_repo": True, "branch": branch, "detached": detached,
            "upstream": upstream, "dirty": dirty, "ahead": ahead,
            "behind": behind, "error": None,
            "checked_at": datetime.now().isoformat(timespec="seconds")}


def git_worker(project_id: str, directory: Path) -> dict:
    payload = git_read(directory)
    with _GIT_LOCK:
        GIT_CACHE[project_id] = (time.monotonic(), str(directory), payload)
        GIT_PENDING.pop(project_id, None)
    return payload


def git_snapshot(project_id: str, directory: Path,
                 wait: float = 0.0) -> dict | None:
    """The cached git reading for a project, refreshed lazily in the background.

    git never runs on the calling thread. A status walk is milliseconds on a
    small worktree and seconds on one with a large node_modules, and this is
    reached from /api/projects, which the board polls every two seconds — so the
    request gets whatever is already known (None the very first time) and asks
    for a refresh that lands in time for the next poll. One refresh per project
    at a time; the pool is bounded, so a repository on a disk that stopped
    answering cannot grow threads.

    `wait` is for /api/context alone, where the answer is the whole point of the
    click and a first reading is worth a bounded pause.
    """
    now = time.monotonic()
    key = str(directory)
    with _GIT_LOCK:
        entry = GIT_CACHE.get(project_id)
        if entry is not None and entry[1] != key:
            entry = None                 # the directory was re-pointed under us
        stale = entry is None or now - entry[0] >= GIT_TTL
        pending = GIT_PENDING.get(project_id)
        if stale and not STOP_EVENT.is_set() and \
                (pending is None or pending.done()):
            try:
                pending = GIT_POOL.submit(git_worker, project_id, directory)
                GIT_PENDING[project_id] = pending
            except RuntimeError:         # pool already shut down, on the way out
                pending = None
    if entry is None and wait > 0 and pending is not None:
        try:
            pending.result(timeout=wait)
        except Exception:
            pass                         # bounded wait, never a failed request
        with _GIT_LOCK:
            entry = GIT_CACHE.get(project_id)
            if entry is not None and entry[1] != key:
                entry = None
    return entry[2] if entry is not None else None


def git_summary(git: dict | None) -> str:
    """The git reading as one clause of English, for /api/context."""
    if git is None:
        return "not read yet"
    if git.get("error"):
        return f"unavailable ({git['error']})"
    if not git.get("is_repo"):
        return "not a git repository"
    head = git.get("branch") or "unknown branch"
    parts = [f"detached at {head}" if git.get("detached") else f"on {head}"]
    dirty = git.get("dirty") or 0
    parts.append(f"{dirty} uncommitted file{'' if dirty == 1 else 's'}"
                 if dirty else "clean")
    ahead, behind = git.get("ahead"), git.get("behind")
    if ahead or behind:
        counts = ([f"{ahead} ahead"] if ahead else []) + \
                 ([f"{behind} behind"] if behind else [])
        parts.append(" and ".join(counts) +
                     (f" {git['upstream']}" if git.get("upstream") else ""))
    return ", ".join(parts)

# --------------------------------------------------------------- "open in"
#
# Editors, a terminal and the file manager, launched at a project's directory.
# Two rules hold the whole section up: argv is always a list, never a string
# through a shell, and the directory is always the resolved project directory
# the dataset named — the request body chooses WHICH project, never WHERE.


def local_open_override(target: str, config: dict | None = None) -> str | None:
    """launcher.local.json's escape hatch: {"open_commands": {"cursor": "..."}}."""
    values = (local_config() if config is None else config).get("open_commands")
    if isinstance(values, dict):
        value = values.get(target)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_command(name: str) -> str | None:
    """which(), honouring "~" and taking a full path as written if given one."""
    try:
        return shutil.which(os.path.expanduser(name))
    except (OSError, ValueError, TypeError):
        return None


def resolve_target(target: str,
                   config: dict | None = None) -> tuple[str | None, str | None]:
    """(executable, why it is unavailable) for one target, resolved right now.

    Resolved per request rather than at boot: an editor installed while the board
    was open should work on the next click, not after a restart. The answer is
    never a hardcoded path — PATH first, launcher.local.json second, and a plain
    409 with the reason when neither has it, instead of a silent no-op.
    """
    spec = OPEN_TARGETS.get(target)
    if spec is None:
        return None, f"unknown target {target!r}"
    if target == "browser":
        return "", None                  # webbrowser, not an executable
    override = local_open_override(target, config)
    if override:
        exe = resolve_command(override)
        if exe:
            return exe, None
        return None, (f"launcher.local.json points open_commands.{target} at "
                      f"{override!r}, which is not a runnable file")
    for candidate in spec["candidates"]:
        exe = resolve_command(candidate)
        if exe:
            return exe, None
    return None, (f"{spec['label']} is not on PATH (looked for "
                  f"{', '.join(spec['candidates'])}). Install it, or set "
                  f"open_commands.{target} in launcher.local.json.")


def resolve_terminal(config: dict | None = None) -> tuple[str | None, str | None]:
    """Windows Terminal, if it is here. Optional — there is a fallback."""
    override = local_open_override("terminal", config)
    for candidate in ((override,) if override else TERMINAL_CANDIDATES):
        exe = resolve_command(candidate)
        if exe:
            return exe, None
    return None, "Windows Terminal (wt) is not on PATH"


def target_availability(config: dict | None = None) -> dict:
    """{target: {label, available, command, reason}} — what the buttons can do.

    Cached for WHICH_TTL: shutil.which stats every PATH entry once per extension,
    and /api/health is polled every ten seconds. The launch path never reads this
    cache; it resolves again for real.
    """
    global _TARGET_CACHE
    now = time.monotonic()
    with _TARGET_LOCK:
        checked_at, snapshot = _TARGET_CACHE
        if snapshot and now - checked_at < WHICH_TTL:
            return snapshot
    config = local_config() if config is None else config
    fresh: dict[str, dict] = {}
    for target, spec in OPEN_TARGETS.items():
        exe, reason = resolve_target(target, config)
        fresh[target] = {"label": spec["label"], "available": reason is None,
                         "command": exe or None, "reason": reason}
    terminal, _why = resolve_terminal(config)
    fresh["claude"]["terminal"] = terminal
    fresh["claude"]["note"] = (None if terminal else
                               "Windows Terminal was not found; Claude opens in "
                               "a plain console window instead.")
    with _TARGET_LOCK:
        _TARGET_CACHE = (now, fresh)
    return fresh


def claude_argv(exe: str, directory: Path,
                config: dict | None = None) -> tuple[list[str], str | None, bool]:
    """How to put a Claude Code session in a project's directory.

    (argv, the terminal used or None, whether it needs its own console.)
    Windows Terminal when it is there — `wt -d <dir> claude` opens a real tab
    already pointed at the project — and a plain new console window when it is
    not, which is the same session in a plainer frame.
    """
    text = str(directory)
    terminal, _why = resolve_terminal(config)
    # wt parses its own command line and ';' is its pane separator: a directory
    # holding one would be split into two panes rather than opened. Vanishingly
    # rare, and a silently wrong window is worse than the plain console.
    if terminal and ";" not in text:
        if Path(exe).suffix.lower() in SHELL_SUFFIXES:
            # A .cmd shim is not an image wt can launch on its own.
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return [terminal, "-d", text, comspec, "/k", exe], terminal, False
        return [terminal, "-d", text, exe], terminal, False
    return [exe], None, True


def open_debounced(project_id: str, target: str) -> bool:
    """True when this exact launch already happened moments ago.

    wt.exe takes about a second to put a window on screen, and every click that
    lands inside that second would otherwise be its own Windows Terminal tab
    running its own Claude Code session in the same directory. Keyed on the pair
    so opening the folder does not suppress opening the editor. The table only
    ever holds one entry per pair, and stale ones are dropped as it is read.
    """
    now = time.monotonic()
    with _OPEN_LOCK:
        for key, at in list(_OPEN_RECENT.items()):
            if now - at > OPEN_DEBOUNCE:
                _OPEN_RECENT.pop(key, None)
        pair = (project_id, target)
        last = _OPEN_RECENT.get(pair)
        if last is not None and now - last < OPEN_DEBOUNCE:
            return True
        _OPEN_RECENT[pair] = now
        return False


def open_in(target: str, directory: Path,
            config: dict | None = None) -> tuple[int, dict]:
    """Launch one target at one directory. Returns (http status, payload)."""
    exe, reason = resolve_target(target, config)
    if exe is None:
        return 409, {"error": reason, "target": target, "available": False}
    if not directory.is_dir():
        return 404, {"error": f"project directory not found: {directory}",
                     "target": target,
                     "hint": "check ORRERY_ROOT or launcher.local.json"}

    terminal = None
    if target == "claude":
        argv, terminal, own_console = claude_argv(exe, directory, config)
    else:
        argv, own_console = [exe, str(directory)], False

    kwargs: dict = {}
    if os.name == "nt":
        # Its own process group, so nothing aimed at Orrery's children ever
        # reaches an editor he is working in. A new console only where one is
        # actually wanted; everywhere else no window at all, so a .cmd shim like
        # code.cmd does not flash a black box on its way to the editor.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | (getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if own_console
               else getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    else:
        kwargs["start_new_session"] = True
    if not own_console:
        # Never inherit this server's handles. A console the user is meant to
        # type into is the one case where redirecting stdio would break the
        # thing being launched, so it is left alone there.
        kwargs.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
    try:
        # shell=False, always: argv is a list built here, and the only variable
        # in it is a directory this process resolved from the dataset.
        subprocess.Popen(argv, cwd=str(directory), close_fds=True, **kwargs)
    except (OSError, ValueError) as e:
        return 500, {"error": f"could not open {OPEN_TARGETS[target]['label']}: "
                              f"{e or type(e).__name__}", "target": target}
    return 200, {"ok": True, "target": target, "command": exe,
                 "dir": str(directory), "terminal": terminal}

# ------------------------------------------------------------------ payloads


def project_rows(root: Path, data: dict) -> list[dict]:
    """One row per launchable project, straight from in-memory state.

    Everything that touches a disk — launcher.local.json, and a stat per project
    directory — happens up here, before the lock. The same lock is taken by
    Proc.append() for every line every child prints, so a slow stat under it
    (a project_dirs override on a dead network share, say) would stall the log
    readers until the children's pipes filled and the dev servers themselves
    blocked. A status poll must never be able to do that.
    """
    config = local_config()
    pairs = launchable(data, config)
    directories = {str(project.get("id")): project_dir(root, str(project.get("id")),
                                                       dev, config)
                   for project, dev in pairs}
    exists = {pid: directory.is_dir() for pid, directory in directories.items()}
    names = {str(project.get("id")): project.get("name", project.get("id"))
             for project, _dev in pairs}
    # Cache reads only — git itself runs on its own pool. Up here with the other
    # disk work for the same reason: never under the lock.
    repos = {pid: git_snapshot(pid, directories[pid])
             for pid, present in exists.items() if present}

    # One Toolhelp snapshot for the whole poll, taken only when something is
    # actually running. Nothing started means no snapshot and no OpenProcess
    # calls, so an idle Orrery stays free.
    with LOCK:
        live_pids = {proc.id: proc.popen.pid for proc in PROCS.values()
                     if proc.alive()}
    kids = children_map(process_parents()) if live_pids else {}
    memory = {pid: tree_memory(child_pid, kids)
              for pid, child_pid in live_pids.items()} if kids else {}

    rows = []
    now = time.monotonic()
    with LOCK:
        for proc in PROCS.values():
            finalize_unlocked(proc, now)     # never report a stale state
        holders = {proc.port: proc.id for proc in PROCS.values()
                   if proc.alive() and proc.port}
        for project, dev in pairs:
            project_id = str(project.get("id"))
            proc = PROCS.get(project_id)
            live = proc is not None and proc.alive()
            port = proc.port if proc is not None and live else dev["port"]
            default_port = dev["port"]
            status = "stopped" if proc is None else proc.state

            held_by = None
            held_by_kind = None
            in_use_by_other = False
            if not live and default_port:
                owner = holders.get(default_port)
                if owner is not None:
                    # A sibling Orrery project, not a stranger — say which one.
                    held_by = f"{names.get(owner, owner)} (started by Orrery)"
                    held_by_kind, in_use_by_other = "orrery", True
                elif PORT_STATE.get(default_port):
                    cached = HOLDERS.get(default_port)
                    held_by = cached[1] if cached else "a process Orrery did not start"
                    held_by_kind, in_use_by_other = "external", True
            if status == "stopped" and held_by_kind == "external":
                status = "external"      # up, but not by us: say so, do not pretend

            # Only ever the verdict for the run that is on screen: a record from
            # the previous launch would put a green dot on a server that is not
            # the one running now.
            record = HEALTH.get(project_id) if live else None
            health = record[2] if (record is not None and proc is not None
                                   and record[0] == proc.started_at) else None

            directory = directories[project_id]
            rows.append({
                "id": project_id,
                "name": project.get("name", project_id),
                "tagline": project.get("tagline", ""),
                "lanes": project.get("lanes", []),
                "visibility": project.get("visibility"),
                "dev": {k: dev[k] for k in
                        ("dir", "command", "port", "url", "kind", "ready_pattern",
                         "port_env", "port_arg", "requires", "can_relocate")},
                "status": status,
                "busy": status in ("starting", "stopping"),
                "pid": proc.popen.pid if (proc is not None and live) else None,
                "port": port,
                "default_port": default_port,
                "port_in_use_by_other": in_use_by_other,
                "held_by": held_by,
                "held_by_kind": held_by_kind,
                "relocated_from": proc.relocated_from if (proc is not None and live)
                                  else None,
                "uptime_secs": proc.uptime() if (proc is not None and live) else 0,
                # Resident memory for the whole process tree, not just the
                # tracked child: with npm in the way the child is a cmd.exe and
                # the hundreds of megabytes are one or two generations down.
                # 0 when stopped, or when the reading could not be taken.
                "memory_bytes": memory.get(project_id, 0),
                "local_only": bool(project.get("local_only")),
                "url": url_for(dev, port),
                # Alive is not working: null until a probe has answered, then
                # {health, http_status, latency_ms, checked_at, url, detail}.
                # Only health == "ok" means the server actually served.
                "health": health,
                # null until the first background reading lands (a poll or two),
                # then {is_repo, branch, detached, upstream, dirty, ahead,
                # behind, error, checked_at}. is_repo false: say nothing.
                "git": repos.get(project_id),
                "last_exit_code": proc.exit_code if proc is not None else None,
                "log_cursor": proc.seq if proc is not None else 0,
                "has_logs": bool(proc is not None and proc.seq),
                "dir_exists": exists[project_id],
                "error": None if exists[project_id]
                         else f"directory not found: {directory}",
            })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def find_launchable(project_id: str, data: dict,
                    config: dict | None = None) -> tuple[dict, dict] | None:
    """(project, dev) for an id, or None. The one place an id is trusted."""
    for project, dev in launchable(data, config):
        if str(project.get("id")) == project_id:
            return project, dev
    return None


def context_text(project: dict, dev: dict, directory: Path,
                 git: dict | None) -> str:
    """A paste-ready briefing about one project, for any AI that is asked about it.

    There is no ChatGPT desktop app and no protocol handler to hand a project to
    on Windows, so the substitute is a block of text the clipboard can carry into
    whatever chat is already open: what the project is, what it is built from,
    where it lives, what state its worktree is in, and how to run it.
    """
    project_id = str(project.get("id"))
    with LOCK:
        proc = PROCS.get(project_id)
        live = proc is not None and proc.alive()
        state = proc.state if proc is not None else "stopped"
        port = proc.port if (proc is not None and live) else dev["port"]
        record = HEALTH.get(project_id) if live else None
        health = record[2] if (record is not None and proc is not None
                               and record[0] == proc.started_at) else None
    url = url_for(dev, port)

    lines = [f"Project: {project.get('name', project_id)} (id: {project_id})"]
    tagline = str(project.get("tagline") or "").strip()
    if tagline:
        lines.append(tagline)
    lanes = [str(lane) for lane in project.get("lanes") or [] if lane]
    stack = [str(item) for item in project.get("technologies") or [] if item]
    lines.append("")
    if lanes:
        lines.append(f"Lanes: {', '.join(lanes)}")
    if stack:
        lines.append(f"Stack: {', '.join(stack)}")
    role = str(project.get("role") or "").strip()
    period = str(project.get("period") or "").strip()
    if role or period:
        lines.append("Role: " + ", ".join(part for part in (role, period) if part))

    description = str(project.get("description") or "").strip()
    if description:
        lines += ["", "What it is:", description]

    if state == "stopped" and proc is not None:
        running = f"not running (last exit code {proc.exit_code})" \
            if proc.exit_code is not None else "not running"
    elif not live:
        running = "not running" if state == "stopped" else state
    elif state == "running":
        running = f"running on port {port}" if port else "running"
        if health is None:
            running += ", not probed yet"
        elif health["health"] == "ok":
            running += f", answering ({health.get('http_status')} in " \
                       f"{health.get('latency_ms')} ms)"
        elif health["health"] == "unreachable":
            running += ", process up but not answering HTTP"
        else:
            running += f", answering with errors ({health.get('detail')})"
    else:
        running = state

    lines += ["", "Local setup:",
              f"- Path: {directory}",
              f"- Git: {git_summary(git)}",
              f"- Run: {dev['command']}   (from that directory)"]
    if url:
        lines.append(f"- URL: {url}")
    lines.append(f"- Right now: {running}")
    if dev.get("requires"):
        lines.append(f"- Needs first: {dev['requires']}")
    repo = (project.get("links") or {}).get("repo") \
        if isinstance(project.get("links"), dict) else None
    if isinstance(repo, str) and repo:
        lines.append(f"- Repo: {repo}")
    return "\n".join(lines) + "\n"


def root_payload() -> dict:
    root, source, error = resolve_root()
    return {
        "root": str(root) if root is not None else None,
        "root_source": source,
        "root_ok": root is not None,
        "root_error": error,
        "hint": "Set the ORRERY_ROOT environment variable, or create "
                "launcher/launcher.local.json with "
                "{\"projects_root\": \"<path to the folder holding the projects>\"} "
                "— copy launcher.local.example.json to start.",
        "example_file": str(EXAMPLE_FILE),
        "local_file": str(LOCAL_FILE),
    }

# ------------------------------------------------------------------ handler


class Handler(BaseHTTPRequestHandler):
    server_version = "Orrery/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib name
        pass  # quiet: runs headless under pythonw

    # ---- plumbing

    def send_json(self, code: int, payload: dict | list) -> None:
        """/api/projects answers with a bare array; every error answers with an
        object carrying {"error": ...}."""
        self.drain_body()  # keep-alive safety: never leave request-body bytes unread
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def drain_body(self) -> None:
        """Consume any unread request body so leftover bytes are never parsed as
        the next request line on a reused (HTTP/1.1 keep-alive) connection."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

    def check_host(self) -> bool:
        """Reject requests whose Host header is not this server (DNS rebinding)."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host in ALLOWED_HOSTS:
            return True
        self.close_connection = True
        self.send_json(403, {"error": "forbidden host"})
        return False

    def read_body(self) -> dict | None:
        """Parse the JSON request body; None means already-answered 400."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        self._body_read = True
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "invalid JSON body"})
            return None
        if not isinstance(data, dict):
            self.send_json(400, {"error": "body must be a JSON object"})
            return None
        return data

    def wants_project(self, body: dict) -> tuple[dict, dict] | None:
        """Resolve {"id": ...} to (project, dev); None means already-answered."""
        project_id = body.get("id")
        if not isinstance(project_id, str) or not project_id:
            self.send_json(400, {"error": "missing key: id"})
            return None
        data, error = read_projects()
        if data is None:
            self.send_json(500, {"error": error})
            return None
        found = find_launchable(project_id, data)
        if found is not None:
            return found
        self.send_json(404, {"error": f"no launchable project with id "
                                      f"{project_id!r}"})
        return None

    def wanted_port(self, body: dict) -> tuple[int | None, bool]:
        """(port, ok) from an optional {"port": N}; answers 400 itself when bad.

        Shared by /api/start and /api/restart so no route can reach the socket
        layer with a port it never checked: connect_ex raises OverflowError, not
        OSError, for a value outside 0..65535, which would surface as a 500 with
        a raw internal message instead of a plain 400.
        """
        value = body.get("port")
        if value is None:
            return None, True
        if not isinstance(value, int) or isinstance(value, bool) \
                or not 1 <= value <= 65535:
            self.send_json(400, {"error": "port must be an integer 1-65535"})
            return None, False
        return value, True

    # ---- routes: GET

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._body_read = False          # new request on a reused connection
        if not self.check_host():
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                self.serve_index()
            elif path == "/api/projects":
                self.api_projects()
            elif path == "/api/logs":
                self.api_logs(parse_qs(parsed.query))
            elif path == "/api/context":
                self.api_context(parse_qs(parsed.query))
            elif path == "/api/health":
                self.api_health()
            elif path in ("/api/targets", "/api/open-targets"):
                # Two names for one answer: the board asks for it on its own at
                # boot, and it also rides along in /api/health under the same
                # key, so a button never has to guess whether it can do anything.
                snapshot = target_availability()
                self.send_json(200, {"targets": snapshot,
                                     "open_targets": snapshot})
            elif path == "/api/root":
                self.send_json(200, root_payload())
            else:
                self.send_json(404, {"error": f"not found: {path}"})
        except Exception as e:           # never kill the thread
            self.fail_safe(e)

    def serve_index(self) -> None:
        self.drain_body()
        try:
            body = INDEX_FILE.read_bytes()   # fresh from disk on every request
        except OSError:
            self.send_json(404, {"error": "launcher/static/index.html not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def api_projects(self) -> None:
        root, source, error = resolve_root()
        if root is None:
            payload = root_payload()
            payload["error"] = f"the projects root could not be resolved: {error}"
            self.send_json(503, payload)
            return
        data, read_error = read_projects()
        if data is None:
            self.send_json(500, {"error": read_error,
                                 "hint": "orrery.py reads ../projects.json"})
            return
        self.send_json(200, project_rows(root, data))

    def api_logs(self, query: dict) -> None:
        project_id = (query.get("id") or [""])[0]
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        with LOCK:
            proc = PROCS.get(project_id)
        if proc is None:
            self.send_json(200, {"id": project_id, "lines": [], "cursor": 0,
                                 "status": "stopped", "dropped": 0})
            return
        lines, cursor, dropped = proc.snapshot_logs(max(0, since))
        self.send_json(200, {"id": project_id, "lines": lines, "cursor": cursor,
                             "status": proc.state, "dropped": dropped,
                             "exit_code": proc.exit_code})

    def api_context(self, query: dict) -> None:
        """A briefing block for the clipboard. See context_text()."""
        project_id = (query.get("id") or [""])[0]
        if not project_id:
            self.send_json(400, {"error": "missing query parameter: id"})
            return
        data, error = read_projects()
        if data is None:
            self.send_json(500, {"error": error})
            return
        config = local_config()
        found = find_launchable(project_id, data, config)
        if found is None:
            self.send_json(404, {"error": f"no launchable project with id "
                                          f"{project_id!r}"})
            return
        project, dev = found
        root, _source, root_error = resolve_root()
        if root is None:
            self.send_json(503, {"error": f"the projects root could not be "
                                          f"resolved: {root_error}",
                                 **root_payload()})
            return
        directory = project_dir(root, project_id, dev, config)
        # The one caller allowed to wait for git, and only for as long as
        # GIT_WAIT: the branch is half the point of the block, this is a click
        # rather than a poll, and the wait is bounded whatever git does.
        git = git_snapshot(project_id, directory, wait=GIT_WAIT)
        self.send_json(200, {"id": project_id,
                             "text": context_text(project, dev, directory, git),
                             "git": git})

    def api_health(self) -> None:
        with LOCK:
            running = sum(1 for proc in PROCS.values() if proc.alive())
        config = local_config()
        data, _read_error = read_projects()
        payload = {"ok": True, "uptime": int(time.monotonic() - BOOT_TIME),
                   "running_count": running, "pid": os.getpid(), "port": PORT,
                   "boot_notes": list(BOOT_NOTES), "watchdog_error": WATCHDOG_ERROR,
                   "health_error": HEALTH_ERROR,
                   # Which "Open in" buttons can do anything on this machine, and
                   # the reason for every one that cannot — a button that does
                   # nothing when clicked is worse than one that says why.
                   "open_targets": target_availability(config),
                   "git_available": shutil.which("git") is not None,
                   # A dev block that cannot be used drops its project off the
                   # board; without this the card just vanishes and nothing says why.
                   "dev_errors": dev_errors(data, config) if data is not None else [],
                   # launcher.local.json hides projects and redirects directories.
                   # Both are invisible on the board by design, so they are named
                   # here — an unexplained missing card is a bug hunt otherwise.
                   "disabled": sorted(disabled_ids(config)),
                   "overridden_dirs": sorted(
                       k for k in (config.get("project_dirs") or {})
                       if isinstance(config.get("project_dirs"), dict)),
                   "local_file_present": LOCAL_FILE.is_file()}
        payload.update(root_payload())
        self.send_json(200, payload)

    # ---- routes: POST

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._body_read = False          # new request on a reused connection
        if not self.check_host():
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":  # forces a CORS preflight cross-site
            self.send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/start":
                self.api_start()
            elif path == "/api/stop":
                self.api_stop()
            elif path == "/api/restart":
                self.api_restart()
            elif path == "/api/open":
                self.api_open()
            elif path == "/api/stop-all":
                self.api_stop_all()
            elif path == "/api/quit":
                self.api_quit()
            else:
                self.send_json(404, {"error": f"not found: {path}"})
        except Exception as e:
            self.fail_safe(e)

    def api_start(self) -> None:
        body = self.read_body()
        if body is None:
            return
        found = self.wants_project(body)
        if found is None:
            return
        project, dev = found
        root, _source, error = resolve_root()
        if root is None:
            self.send_json(503, {"error": f"the projects root could not be "
                                          f"resolved: {error}", **root_payload()})
            return
        wanted, ok = self.wanted_port(body)
        if not ok:
            return
        code, payload = start_project(project, dev, root, wanted)
        self.send_json(code, payload)

    def api_stop(self) -> None:
        body = self.read_body()
        if body is None:
            return
        project_id = body.get("id")
        if not isinstance(project_id, str) or not project_id:
            self.send_json(400, {"error": "missing key: id"})
            return
        code, payload = stop_project(project_id)
        self.send_json(code, payload)

    def api_restart(self) -> None:
        body = self.read_body()
        if body is None:
            return
        found = self.wants_project(body)
        if found is None:
            return
        project, dev = found
        project_id = str(project.get("id"))
        root, _source, error = resolve_root()
        if root is None:
            self.send_json(503, {"error": f"the projects root could not be "
                                          f"resolved: {error}", **root_payload()})
            return
        wanted, ok = self.wanted_port(body)   # same check /api/start makes
        if not ok:
            return
        with LOCK:
            proc = PROCS.get(project_id)
            was_live = proc is not None and proc.alive()
        if was_live:
            code, payload = stop_project(project_id)
            if code != 200:
                self.send_json(code, payload)
                return
        code, payload = start_project(project, dev, root, wanted)
        payload["restarted"] = True
        self.send_json(code, payload)

    def api_open(self) -> None:
        """{"id": ..., "target": ...} — the browser, an editor, a terminal, or
        the folder. The target is optional and defaults to "browser", which is
        what this route has always done and what the grid still asks for."""
        body = self.read_body()
        if body is None:
            return
        target = body.get("target")
        target = "browser" if target is None else target
        if not isinstance(target, str) or target not in OPEN_TARGETS:
            self.send_json(400, {"error": f"target must be one of: "
                                          f"{', '.join(OPEN_TARGETS)}",
                                 "targets": list(OPEN_TARGETS)})
            return
        found = self.wants_project(body)     # rejects any id not in the dataset
        if found is None:
            return
        project, dev = found
        project_id = str(project.get("id"))
        if target != "browser":
            config = local_config()
            root, _source, error = resolve_root()
            if root is None:
                self.send_json(503, {"error": f"the projects root could not be "
                                              f"resolved: {error}",
                                     **root_payload()})
                return
            # The directory is always the resolved project directory, never
            # anything the request said.
            directory = project_dir(root, project_id, dev, config)
            if open_debounced(project_id, target):
                # The second half of a double-click, or a page that reloaded
                # mid-click. One window was already asked for; two would be one
                # Claude Code session per click in the same directory.
                self.send_json(200, {"ok": True, "id": project_id, "target": target,
                                     "debounced": True,
                                     "dir": str(directory),
                                     "note": f"{OPEN_TARGETS[target]['label']} was "
                                             f"already opened a moment ago"})
                return
            code, payload = open_in(target, directory, config)
            if code != 200:
                with _OPEN_LOCK:     # a failed launch must never block the retry
                    _OPEN_RECENT.pop((project_id, target), None)
            payload.setdefault("id", project_id)
            self.send_json(code, payload)
            return
        with LOCK:
            proc = PROCS.get(project_id)
            port = proc.port if (proc is not None and proc.alive()) else dev["port"]
        url = url_for(dev, port)
        if not url:
            self.send_json(400, {"error": f"{project.get('name', project_id)} "
                                          f"declares no url or port"})
            return
        parsed = urlparse(url)             # belt and braces: never open the wider web
        if parsed.scheme not in ("http", "https") or \
                (parsed.hostname or "") not in LOCAL_HOSTNAMES:
            self.send_json(400, {"error": f"refusing to open a non-local url: {url}"})
            return
        try:
            opened = webbrowser.open(url)
        except Exception as e:
            self.send_json(500, {"error": str(e) or type(e).__name__})
            return
        self.send_json(200, {"ok": True, "id": project_id, "target": "browser",
                             "url": url, "opened": bool(opened)})

    def api_stop_all(self) -> None:
        body = self.read_body()
        if body is None:
            return
        stopped = stop_everything()
        self.send_json(200, {"ok": True, "stopped": stopped})

    def api_quit(self) -> None:
        body = self.read_body()
        if body is None:
            return
        stopped = stop_everything()      # never orphan a child on the way out
        self.send_json(200, {"ok": True, "stopped": stopped})
        STOP_EVENT.set()
        if SERVER is not None:
            threading.Timer(0.3, SERVER.shutdown).start()

    # ---- routes: anything but GET/POST -> same 404 JSON as unknown paths

    def api_404(self) -> None:
        self._body_read = False
        self.send_json(404, {"error": f"not found: {urlparse(self.path).path}"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = api_404

    # ---- error fallback

    def fail_safe(self, exc: Exception) -> None:
        try:
            self.send_json(500, {"error": str(exc) or type(exc).__name__})
        except Exception:
            pass  # client already gone / headers already sent

# --------------------------------------------------------------------- main


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A browser walking away from a keep-alive connection is not an error.

        Without this, every closed tab prints a ConnectionResetError traceback
        to stderr — noise that buries anything that actually matters.
        """
        if isinstance(sys.exc_info()[1], (ConnectionResetError,
                                          ConnectionAbortedError,
                                          BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def launcher_port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((HOST, PORT)) == 0


def open_browser() -> None:
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass


def main() -> None:
    global SERVER, PORT, ALLOWED_HOSTS
    ap = argparse.ArgumentParser(description="Orrery — local dev server launch pad")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser tab")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"port for the launcher itself (default {PORT})")
    args = ap.parse_args()
    if not 1 <= args.port <= 65535:
        print(f"error: --port {args.port} is out of range", file=sys.stderr)
        sys.exit(2)
    PORT = args.port
    ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}

    if launcher_port_in_use():
        if not args.no_browser:
            open_browser()
        sys.exit(0)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    reap_orphans()                       # children a crashed launcher left behind

    try:
        SERVER = Server((HOST, PORT), Handler)
    except OSError:                      # lost a startup race: already running
        if not args.no_browser:
            open_browser()
        sys.exit(0)

    threading.Thread(target=watchdog, name="orrery-watchdog", daemon=True).start()
    # Its own thread, not a step inside the watchdog tick: one unreachable
    # server would otherwise push every port probe and every state transition
    # HEALTH_TIMEOUT seconds late.
    threading.Thread(target=health_loop, name="orrery-health", daemon=True).start()

    def shutdown(_signum=None, _frame=None) -> None:
        STOP_EVENT.set()
        if SERVER is not None:
            threading.Thread(target=SERVER.shutdown, daemon=True).start()

    if hasattr(signal, "SIGBREAK"):      # console close / Ctrl+Break on Windows
        try:
            signal.signal(signal.SIGBREAK, shutdown)
        except (ValueError, OSError):
            pass

    if not args.no_browser:
        open_browser()                   # socket is already listening
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        stop_everything()                # SIGINT must not orphan a dev server
        SERVER.server_close()
        # Every pool thread is non-daemon and joined at interpreter exit, so a
        # probe or a git call still in flight would hold the process open for
        # its whole timeout after the board said goodbye.
        for pool in (PROBE_POOL, HEALTH_POOL, GIT_POOL):
            pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
