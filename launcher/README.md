# Orrery

A local launch pad for every dev server in this portfolio. It starts them, stops them, watches their
ports, probes them to see whether they are actually answering, reads their git state, tails their
output, and opens any of them in an editor or a Claude Code session — from one board, in the
browser, with no dependencies beyond the Python standard library.

Orrery does not keep its own list of projects. It reads `projects.json`, the same file that renders
the README, `api.json`, `resume.json` and `llms.txt`. A project becomes launchable the moment its
entry gains a `dev` block, and disappears again when the block is removed. One file describes the
portfolio and runs it.

```
python launcher/orrery.py      →  http://127.0.0.1:7345
```

---

## Run it

**Double-click `orrery.bat`** at the repo root. It prefers `pythonw` so no console window lingers,
starts the launcher and opens the board.

Or run it yourself:

```
python launcher/orrery.py
```

Orrery binds `127.0.0.1:7345` and nothing else — it is not reachable from the network. Only one
instance runs at a time; launching a second one just brings the existing board up.

Quitting from the header stops every server Orrery started. Closing the browser tab does not — the
launcher keeps running until you quit it, which is usually what you want.

---

## Point it at your projects

The hub repo is public, so no machine path is ever committed to it. Orrery resolves your projects
root at boot, in this order:

| # | Source | Notes |
|---|--------|-------|
| 1 | `ORRERY_ROOT` environment variable | Wins over everything. Good for a one-off run. |
| 2 | `launcher/launcher.local.json` → `"projects_root"` | Git-ignored. The normal way. |
| 3 | The folder containing this repository | The sensible default when your checkouts are siblings. |

Set it up once:

```
copy launcher\launcher.local.example.json launcher\launcher.local.json
```

then edit the copy — `projects_root` is the one line you have to change:

```json
{
  "projects_root": "C:/Users/you/Projects"
}
```

The example's two optional sections ship as `_example_project_dirs` and `_example_disabled`, so a
verbatim copy overrides nothing: rename a key to `project_dirs` or `disabled` to switch it on.
Both are otherwise invisible on the board by design, so whatever they end up doing is named in
`/api/health` and announced on screen — a hidden project is never a silently missing one.

| Key | What it does |
|-----|--------------|
| `projects_root` | The folder holding your checkouts. |
| `project_dirs` | `{ "<project id>": "<absolute path>" }` for a project that lives elsewhere. A path that does not exist shows as a red note on that project's card. |
| `disabled` | `["<project id>"]` to hide a project on this machine only. |

`launcher/launcher.local.json` and `launcher/state/` are both in `.gitignore`. If the root cannot
be resolved, the board says so and shows these two options — it does not crash and does not show
you a blank screen. If `projects.json` itself will not parse, it says *that* instead, with the
parser's message, rather than sending you off to set an environment variable.

---

## Make a project launchable

Add a `dev` block to that project's entry in `projects.json`. Portable fields only: `dir` is a
folder name relative to the projects root, never an absolute path.

```json
"dev": {
  "dir": "my_universe",
  "command": "npm run dev",
  "port": 3000,
  "url": "http://localhost:3000",
  "kind": "node",
  "ready_pattern": "ready in \\d+"
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `dir` | yes | Folder name under the projects root. |
| `command` | yes | The command to run, exactly as you would type it. Never passed through a shell with interpolated data. |
| `port` | no | The port the server wants. Drives collision detection. |
| `url` | no | What **Open** opens, and what the health probe GETs. Falls back to `http://localhost:<port>`. |
| `kind` | no | `node` \| `python` \| `make` \| `static`. Decides how a different port is passed (`PORT=` env, `--port` flag). |
| `ready_pattern` | no | Regex against stdout that means "the server is actually up". Without it, Orrery falls back to probing the port. |

Projects with no `dev` block simply do not appear on the board. Android/Gradle projects, restricted
client work and anything that is not a server are left out on purpose.

After editing `projects.json`, run `python build.py` so the generated files stay in sync.

---

## A project that is not in the dataset yet

The section above assumes the project already has an entry in `projects.json`. A brand new one does
not, so it needs the entry first. Two paths, and the choice is only ever "should this be public":

**Public** — it belongs in the portfolio. Add a full entry to `projects.json`, then a `dev` block on
it. Minimum viable entry:

```json
{
  "id": "kebab-case-slug",
  "name": "Display Name",
  "tagline": "One line, under 90 characters",
  "lanes": ["fullstack"],
  "role": "sole author",
  "period": "2026",
  "technologies": ["TypeScript", "Next.js"],
  "visibility": "public",
  "links": {},
  "highlights": [],
  "featured": false,
  "dev": { "dir": "folder-name", "command": "npm run dev", "port": 3007,
           "url": "http://localhost:3007", "kind": "node" }
}
```

It will render on the GitHub profile and in `llms.txt`, so write the tagline like a human will read
it. Run `python build.py` afterwards. Leave `metrics` out unless the numbers were actually measured.

**Local only** — client work, anything under NDA, or a scratch project with no business being
published. Add it to `local_projects` in `launcher/launcher.local.json`, which git ignores. Same
shape, minus the portfolio fields:

```json
{ "id": "slug", "name": "Display Name", "lanes": ["fullstack"],
  "dev": { "dir": "folder-name", "command": "npm run dev", "port": 3102,
           "url": "http://localhost:3102", "kind": "node" } }
```

Nothing about it reaches the public repository, and its card is badged `local`.

**Picking a port.** The dataset uses one contiguous block: 3000 to 3006 are taken, so a new public
project takes 3007, then 3008. Local-only projects start at 3100. The job radar keeps 8765 because a
scheduled task points at it. Take the next free number rather than whatever the framework defaults
to, so every port on the board is guessable.

**Finding the command.** Read the project rather than assuming: `scripts.dev` or `scripts.start` in
`package.json` for node, the Makefile or the CLI entry point for python. Set `kind` to match, since
it decides whether a relocated port is passed as a `PORT=` env var or a `--port` flag.

---

## Ports are the interesting part

Every launchable project now owns its own port — one contiguous block, `3000`–`3006` plus `8765`
for Job Radar — so two projects can no longer fight over a default. The rest of the machine has not
agreed to that, though, and a stale `node` from a crashed terminal still can. Orrery makes it
visible instead of letting it fail at launch:

- The **Ports strip** under the page title lists every port in play and who holds it. Green means a
  project Orrery started is listening there. Amber means either a shared default or a port held by
  a process outside Orrery.
- Before spawning, Orrery probes the port. If it is busy and the project's `kind` supports it, the
  server starts on the next free port and the card says **Moved to 3001 — its default 3000 was
  taken**.
- If the port is held and cannot be moved, the start is refused with a 409 that names the holder,
  and the card shows **Port held** rather than pretending the project is running.
- A stopped project whose default is contested says so up front: *Shares port 3000 with complere,
  VapeMaxxx. First one up keeps it.*

---

## Two views

The header carries a **Grid | Orbital** toggle. The choice is remembered in `localStorage`; the grid
is the default and stays the one that renders every setup, offline and empty state. Both views read
the same rows, share the same actions, and the search box filters both.

### Grid

Each card carries the project's name, tagline, lane chips, a git line, its port, uptime, PID and the
result of the last health probe, the exact command that will run, and two rows of actions.

**Start / Stop** swap by state. **Restart** stops the whole process tree and brings it back.
**Open** launches the project URL in your browser. **Logs** opens the drawer. Below them sit the
four **Open in** buttons and **Copy context**.

### Orbital

One canvas, drawn top-down like the instrument it is named after. The core sits in the middle and
every project is a body on a ring; the rings are lanes — `graphics` innermost, then `fullstack`,
`ai`, `systems`, with anything else on the rim. A project in several lanes sits in the innermost one
it belongs to, and each ring is named where it crosses the bottom of the frame. Bodies start evenly
spread around their band and then keep their own period: inner rings sweep faster, a healthy server
faster than a stopped one, so the picture drifts out of its starting formation the way an orrery
should.

| Body | Means |
|------|-------|
| small, dim, slow | Stopped |
| amber, pulsing | Starting, or up with its first probe still outstanding |
| accent, bright, larger, trailing an arc | Running **and** answering |
| amber, steady, with a halo it is not filling | Running but not responding |
| red, static, broken ring and a cross | Crashed |
| amber outline, dashed ring, no fill | Port held by something Orrery did not start |

- **Hover** a body for its name, port, state and uptime.
- **Click** to select it. A panel pins to the bottom of the window carrying exactly the same actions
  the card has. **Double-click** to start or stop without selecting first.
- `space` starts or stops the selection, `esc` deselects, `←` `→` step around the board.
- Names are drawn beside their bodies in muted ink, and any label that would land on another one is
  simply not drawn rather than smeared over it.

**An idle Orrery costs nothing.** There is one `requestAnimationFrame` loop and it is only allowed
to exist while the tab is visible, the orbital view is on screen, motion is not reduced, and at
least one project is starting, running or stopping. When the last server stops, the loop returns
without rescheduling itself and the picture freezes exactly where it stands — zero frames, not a
throttled trickle. Hover, selection and a status poll that actually changed something each ask for a
single frame and then it stops again. `prefers-reduced-motion: reduce` freezes the orbits entirely
and draws the same layout statically. The canvas is sized to `devicePixelRatio` and re-sized cleanly
with the window.

---

## Health is not liveness

A process being alive is not the same as a server working: `next dev` can hold a port for twenty
seconds before it serves anything, and it can survive a compile error that makes every request hang.
So the launcher probes.

Every running project that declares a `url` gets a plain HTTP `GET` with a hard **2s** timeout,
about every **5s**, from a background thread. A stopped project is never probed, and no HTTP handler
ever waits on one. Each row carries `{health, http_status, checked_at, latency_ms}`.

**Only a passing probe earns green.**

| Dot | State | Means |
|-----|-------|-------|
| muted | Stopped | Not running. **Start** is the primary action. |
| amber, pulsing | Starting | Spawned and waiting — or up, with the first probe still outstanding. |
| green | Running | The probe answered. The card gets its green top rail. |
| amber | No response | The process is up and the probe got nothing back. *Process up, not responding.* |
| amber | Port held | Something Orrery did not start owns that port. |
| red | Crashed | Exited on its own. The card shows the exit code; the logs keep the last output. |

The card's `probe` figure is the status code and the round trip (`200 · 8 ms`); hover it for the
whole sentence, including the reason a failing probe failed. The header chip follows the same rule —
green only when something is genuinely answering, amber when processes are up but silent.

If the launcher publishes no probe data at all, the board falls back to the older meaning of green —
a live process — rather than painting every card amber over a feature that is not there.

---

## Git status per project

Under the tagline, each card shows a compact git line read from the project's own directory: the
current branch, an amber dot with the number of uncommitted files when the tree is dirty, and arrow
counts when the branch is ahead of or behind its upstream. A clean tree is muted — just the branch
name. A detached HEAD says so.

**A directory that is not a repository shows nothing at all**, which is the point: the row is
information, not decoration. The launcher caches the answer for about 30s per project and refreshes
it lazily, with a hard timeout on every `git` call, so a repository on a slow or wedged path can
never stall a status poll.

---

## Open in

Four buttons per project, on the card and in the orbital panel:

| Button | Opens |
|--------|-------|
| Code | VS Code in the project directory (`code` on PATH) |
| Cursor | Cursor in the project directory (`cursor` on PATH) |
| Claude | Windows Terminal in the project directory, running the `claude` CLI — a Claude Code session already pointed at that project. Falls back to a plain console window when Windows Terminal is absent. |
| Folder | The project directory in the file manager |

The launcher resolves each executable with `shutil.which` **at request time** and answers `409` with
a plain sentence when one is missing, so a button never fails silently. The board remembers that
answer for the session and disables the button everywhere, with the launcher's own message as its
tooltip. If the launcher publishes what it found up front, the buttons start out disabled instead.
Nothing is hardcoded: a path can be overridden in `launcher.local.json`, which is git-ignored,
because this repository is public and machine paths stay out of it.

The request carries an id and a target name and nothing else. The directory always comes from the
resolved, validated project, never from the page.

---

## Copy context

**Copy context** puts a paste-ready briefing on the clipboard for whichever AI you are talking to:
name, tagline, description, stack, lanes, the local path, the current branch and whether the tree is
dirty, the run command and url, and any `requires` note. The button says *Copied* for a moment and
goes back to itself. It is the substitute for a desktop handoff that does not exist on Windows —
select the project, copy, paste into the chat, and the model starts the conversation already knowing
what it is looking at.

---

## Logs, keyboard, polling

**Log drawer** — monospace, incremental, ~500 lines per project. ANSI escape codes are stripped
rather than printed, `\r` progress bars collapse to their final state, and errors and warnings are
tinted. It follows the tail until you scroll up, then pauses and offers **Jump to latest**.

**Keyboard** — `/` focuses search (filters by name and lane, in both views), `Esc` closes the drawer,
then an open confirmation, then the orbital selection. In the orbital view, `space` starts or stops
the selected project and the arrow keys move between bodies.

**Polling** — status every 2s, logs every 1s while the drawer is open, health probes every 5s in the
launcher. All of them back off or stop as soon as the tab is hidden, no CSS animates in the
background, and the orbital loop cancels outright. Idling on this board should cost you nothing, and
it is written so you can check: `__orrery.frames()` in the console is a frame counter, and it stops
climbing the moment nothing is running.

---

## Screenshot

`launcher/screenshot.png` and `launcher/orbital.png` — **TODO**.

To capture them:

1. Start Orrery and start two or three servers, ideally including one that is still starting and one
   that is stopped, so the green rail, the amber dot and the Ports strip all appear.
2. Size the browser window to roughly **1440 × 900** and scroll to the top so the header, the Ports
   strip and the first row of cards are all in frame.
3. `Win` + `Shift` + `S`, drag over the page area only — no OS chrome, no bookmarks bar.
4. Save as `launcher/screenshot.png`, then switch to **Orbital**, select a body so the panel is up,
   and capture the same frame as `launcher/orbital.png`.
5. Add them above this list:
   `![Orrery](screenshot.png)` and `![Orbital view](orbital.png)`

---

## Files

```
launcher/
  orrery.py                     the launcher: HTTP server, process supervision, log ring buffers
  static/index.html             the board — one self-contained file, inline CSS and JS
  launcher.local.example.json   committed example of the machine-specific config
  launcher.local.json           your real config (git-ignored)
  state/                        recorded PIDs so a crashed launcher can clean up (git-ignored)
orrery.bat                      double-click entry point
projects.json                   the source of truth, including every dev block
```

---

## Notes on craft

- The frontend loads nothing from the network: no CDN, no external fonts, no frameworks. It is one
  file that works with the machine offline. The orbital view is a canvas and a few hundred lines of
  arithmetic, not a graphics library.
- Every string that arrives from `projects.json` is inserted as text, never as markup.
- Both views draw from the same design tokens; the canvas reads them off `:root` at boot rather than
  keeping a second copy of the palette.
- Killing a dev server means killing its whole tree — `npm run dev` spawns children that outlive a
  polite terminate. Orrery asks nicely first, then stops asking.
- Restricted client work is deliberately absent from the board. Nothing about those projects,
  including their paths or commands, belongs in a public repository.
