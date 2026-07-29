# herdr-hibernate

Auto-hibernates idle Claude Code tabs in Herdr. Each idle Claude tab holds
650–930 MB (the `claude` process plus its MCP server children). This tool kills
that process tree and leaves a tiny bash stub (a few MB) in the pane. Pressing
Enter in the pane resumes the exact session with full history via
`claude --resume <uuid>`.

Panes and tabs are **never closed** — only processes inside them are killed.

Hibernation **survives a reboot**: the stub is only a process, so it dies when
Herdr or the machine goes down, but the pane's resume instructions live on disk
and are re-armed automatically. See [Surviving restarts](#surviving-restarts).

## Install

As a [Herdr plugin](https://herdr.dev/docs/plugins) (Herdr ≥ 0.7.0, Python 3,
no other dependencies, no build step):

```bash
herdr plugin install bengemine/herdr-hibernate
```

Then, from any Herdr-managed pane, start the background watcher and shell hook:

```bash
herdr plugin action invoke bengemine.hibernate.install
```

Or clone it and run the script directly — the plugin wrapper is optional, the
executable is self-contained.

**Platforms:** tested on Linux and WSL2. macOS is untested — the watcher's
systemd path won't exist there, and while the nohup fallback should work, no
one has verified it yet. Reports and PRs welcome.

The first runs are **dry-run by default** (`DRY_RUN=1`): the tool only logs
what it *would* hibernate. Review `~/.config/herdr-hibernate/hibernate.log`,
then set `DRY_RUN=0` in the config to arm it.

## Why killing claude is safe

Claude Code writes its session transcript to
`~/.claude/projects/<project-dir>/<session-uuid>.jsonl` continuously; nothing
is held only in memory once the agent is idle. `claude --resume <uuid>`
restores the full conversation. The only thing a kill can lose is in-flight
work, which is why the tool only ever touches panes whose agent status is
`idle` or `done` AND whose transcript has been quiet past the threshold —
and re-checks the status immediately before killing.

## Usage

```bash
./herdr-hibernate scan               # one classification pass (obeys DRY_RUN)
./herdr-hibernate status             # table: every pane, idle time, verdict
./herdr-hibernate watch              # foreground loop, scan every SCAN_INTERVAL_SECONDS
./herdr-hibernate restore            # re-arm hibernated panes whose stub died
./herdr-hibernate restore --dry-run  # ...report only, change nothing
./herdr-hibernate now                # hibernate the FOCUSED pane (what the keybinding calls)
./herdr-hibernate hibernate w2:p3    # manual: skips idle-time threshold, keeps all safety rules
./herdr-hibernate hibernate w2:p3 --force   # ...also skip the just-resumed guard
./herdr-hibernate forget w2:p3       # drop a pane's hibernation record + stub
./herdr-hibernate install            # shell hook + background watcher
./herdr-hibernate uninstall          # stop + remove the background watcher
```

Run it from inside a Herdr-managed pane (`HERDR_ENV=1`) so the CLI reaches the
session and the tool knows its own pane (which it never hibernates).

## Hibernate on demand (keybinding)

`Ctrl-A` then `Shift-H` hibernates whatever pane is focused, right now — no
typing, no waiting for the idle threshold.

Set up in `~/.config/herdr/config.toml` (this plugin never edits your config
for you):

```toml
[[keys.command]]
key = "prefix+shift+h"
type = "plugin_action"
command = "bengemine.hibernate.now"
description = "hibernate focused pane"
```

Apply without restarting Herdr: `herdr server reload-config`.

(If you run the script standalone instead of as a plugin, use
`type = "shell"` with the absolute path to `herdr-hibernate now`.)

`now` asks Herdr which pane has focus (`herdr pane current`) rather than
trusting `$HERDR_PANE_ID`, because a detached binding runs outside any pane. It skips the idle threshold and the just-resumed guard — an
explicit key press is not a mistake — but keeps the guards that protect you:
it still refuses a pinned tab, a `working`/`blocked` agent, and anything where
killing claude would close the tab.

Feedback: on success the `💤` banner appears in the pane immediately. On
refusal you get a Herdr notification saying why, because a detached command
has nowhere to print.

**Right-click menu is not possible.** Herdr has a built-in pane menu, but its
config exposes no way to add entries to it — the only hook for custom actions
is `[[keys.command]]`. A keybinding is the closest thing available.

## Config — `~/.config/herdr-hibernate/config`

Created with defaults on first run. Plain `KEY=VALUE`, bash-sourceable.
`watch` re-reads it every pass, so edits (including flipping `DRY_RUN`)
take effect without a restart.

| Key | Default | Meaning |
|---|---|---|
| `HIBERNATE_AFTER_MINUTES` | `30` | Idle minutes (no transcript writes) before a pane qualifies. |
| `SCAN_INTERVAL_SECONDS` | `120` | Delay between scans in `watch` mode. |
| `PIN_MARKER` | `📌` | Any tab/pane label containing this is never hibernated. |
| `PINNED_TABS` | *(empty)* | Space-separated tab ids, never hibernated. |
| `DRY_RUN` | `1` | `1` = log only, touch nothing. Set `0` only after reviewing the log. |
| `KILL_GRACE_SECONDS` | `5` | Wait after SIGTERM before SIGKILL-ing survivors. |

| `FORGET_AFTER_MINUTES` | `15` | Grace period before a vanished pane's data is erased. `0` = erase on first sight. |
| `LOG_MAX_KB` | `512` | Rotate the log past this size (one backup kept). |

New keys added by an upgrade are appended to your existing config file, with
their comments and defaults — your own values are never overwritten.

## What is stored, and for how long

Everything lives under `~/.config/herdr-hibernate/`:

| Path | Contents | Lifetime |
|---|---|---|
| `state.json` | One record per **currently hibernated** pane: session uuid, tab id, cwd, label. | Deleted when the pane resumes or the tab closes. |
| `panes/<pane_id>.sh` | That pane's stub script — what survives reboots. | Deleted with its record. |
| `hibernate.log` | What the tool did. | Size-capped by `LOG_MAX_KB`, one rotation. |
| `config` | Your settings. | Permanent (it's yours). |

**Closing a tab erases it.** When a pane disappears, its record and stub script
are deleted outright — no archive, no graveyard file. The tool never holds data
for a tab that isn't open. Records are also erased when the session resumes,
when the pane gets reused by another agent, or when the session transcript is
gone (nothing left to resume).

`FORGET_AFTER_MINUTES` is a safety margin, not retention: a pane must be
*missing* that long before erasure, so a half-started Herdr reporting an
incomplete pane list cannot wipe tabs that are actually still open. If the pane
reappears, the pending erase is cancelled. Set it to `0` to erase on first
sight.

Stray stub scripts with no matching record are garbage-collected on every
sweep, so a crash mid-hibernation cannot leave files behind.

Not this tool's data: the conversations themselves are Claude Code's own
transcripts under `~/.claude/projects/`. This tool only **reads** their
timestamps to measure idle time — it never writes or deletes them, and their
retention is Claude Code's business, not ours.

## Dry-run first (default)

`DRY_RUN=1` ships as the default. A scan only writes lines like

```
WOULD hibernate KB Sync (w2:p4, idle 47m, ~880MB, pid 4372)
```

to the log. Review them, then set `DRY_RUN=0` in the config to arm real
hibernation.

## Pinning

Two mechanisms:

1. **`PINNED_TABS`** — a list of tab ids in the config. Simple, but **tab ids
   can change when Herdr restarts**, so a pin by id can silently detach from
   the tab it was meant to protect.
2. **Label marker** — put `📌` (or your `PIN_MARKER`) anywhere in the tab or
   pane label (`herdr tab rename <tab_id> "📌 my task"`). The marker travels
   with the label, so this is the **durable** mechanism. Prefer it for
   anything that must survive a Herdr restart.

## What is never hibernated

- Panes whose agent status is `working` or `blocked`.
- Pinned tabs (either mechanism above).
- The pane the reaper itself runs in.
- Non-claude agents (codex etc.) — v1 is Claude-only.
- Panes with no session uuid or no transcript file (they could not be resumed,
  so they are never killed).
- Panes already hibernated (stub waiting).

## How hibernation works

1. Idle test: agent status `idle`/`done` **and** transcript untouched for
   `HIBERNATE_AFTER_MINUTES`.
2. The pane's claude root process is found via `herdr pane process-info`
   (verified against the session uuid; a uuid mismatch or an ambiguous pane
   aborts).
3. Status is re-checked; then the whole process tree (claude + MCP children)
   gets SIGTERM, and SIGKILL after `KILL_GRACE_SECONDS`.
4. The record is written to `state.json` **and** a stub script is written to
   `panes/<pane_id>.sh` — both before anything is started, so a crash mid-way
   still leaves the pane recoverable.
5. That script is started in the pane's shell:
   `💤 hibernated 2h14m ago (freed ~880MB) — press Enter to resume`.
   It is one small bash process waiting on stdin.
6. The tab is renamed `💤 <old label>` so hibernated tabs are obvious.

## Resume

Press Enter in the pane. The stub restores the tab label and
`exec claude --resume <uuid>` in the pane's original cwd.

- Resume takes **10–20 s** (claude startup plus MCP servers spawning).
- The **first reply after resume is slower** than usual (cold prompt cache).
- Herdr's claude integration re-reports the session id on its own after
  resume; the tool notices claude is live again and clears the record and the
  stub script on the next scan. The stub deliberately does **not** delete them
  itself — if the resume fails, the pane stays armed and can be retried.
- **Ctrl-C** at the banner dismisses the stub and gives you a plain shell
  (it prints the `claude --resume <uuid>` command first, so nothing is lost).

## Surviving restarts

The stub is a process, so it dies with Herdr — a reboot would otherwise leave
you with a row of `💤` tabs that are just ordinary terminals, with no memory of
which session belonged to which tab. Three mechanisms prevent that:

1. **On-disk stub script per pane** — `panes/<pane_id>.sh` holds everything
   needed to resume: session uuid, cwd, tab id, original label. It is written
   at hibernation time and never depends on a running process.
2. **`.bashrc` hook** (installed by `install`) — when Herdr spawns a fresh
   shell in a pane, the hook checks for that pane's stub script and `exec`s it
   immediately. This needs no daemon, so hibernated tabs come back armed the
   instant Herdr starts, not whenever a timer next fires:

   ```bash
   if [ -n "${HERDR_PANE_ID:-}" ] && [ -z "${HERDR_HIBERNATE_STUB:-}" ] && [[ $- == *i* ]]; then
       _hb_stub_file="$HOME/.config/herdr-hibernate/panes/${HERDR_PANE_ID//[^A-Za-z0-9]/_}.sh"
       [ -s "$_hb_stub_file" ] && { export HERDR_HIBERNATE_STUB=1; exec bash "$_hb_stub_file"; }
   fi
   ```

   `HERDR_HIBERNATE_STUB` is the loop guard: a shell that came *from* a stub
   never re-arms itself.
3. **`restore`** — a sweep that re-arms any pane whose stub is missing. It runs
   automatically when the watcher starts and at the top of every scan, and can
   be run by hand at any time. It is never gated by `DRY_RUN`, because it only
   hands a pane back the session it already lost.

`restore` also repairs drift: if Herdr renumbers a pane, the record is matched
by tab id (then by label + cwd) and re-keyed. If the pane or the transcript is
truly gone, the record is erased — see
[What is stored](#what-is-stored-and-for-how-long).

Recovering an existing hibernated tab after a restart therefore needs nothing
from you. To check it worked:

```bash
./herdr-hibernate status     # every hibernated pane should read [stub armed]
```

## Limitations

- A hibernated pane has no agent process, so it loses its Herdr agent-status
  badge until resumed. The `💤` tab label is the visual substitute.
- The `.bashrc` hook is bash-only. If you switch Herdr's shell to zsh or fish,
  port the hook to that shell's rc file or reboot recovery falls back to the
  slower `restore` sweep.
- Reboot recovery keys on the pane id. If Herdr ever renumbers panes *and*
  changes tab ids at the same time, the hook cannot match — `restore` then
  falls back to label + cwd matching. A record it still cannot place looks
  identical to a closed tab, so it is erased after `FORGET_AFTER_MINUTES`.
  The session itself is not lost: `claude --resume` in that directory still
  lists it, since the transcript is Claude Code's own file.
- On WSL2 without systemd user units, `install` starts a `nohup` watcher that
  inherits the current pane's Herdr environment — after a Herdr restart,
  run `install` again from a live pane.
- Tab-id pins (`PINNED_TABS`) do not survive Herdr restarts; label-marker
  pins do.
- v1 handles Claude Code only.
- macOS is untested (see [Install](#install)).
- If a pane runs multiple claude processes, or a claude naming a different
  session uuid than Herdr reports, the tool refuses to act on it.
