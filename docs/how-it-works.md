# How it works

Everything the [README](../README.md) leaves out: why killing an idle agent is
safe, what the guards check, how a parked pane survives a reboot, and how the
excerpt above the banner is built.

## Why killing an idle agent is safe

All three supported agents write their transcript to disk continuously. Once an
agent is idle, nothing is held only in memory. The one thing a kill can lose is
in-flight work, which is why a pane is only touched when its Herdr status is
`idle` or `done` **and** its transcript has been quiet past the threshold — and
the status is re-checked immediately before the kill.

**Claude Code** — `~/.claude/projects/<project>/<uuid>.jsonl`.
`claude --resume <uuid>` restores the full conversation.
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
`--permission-mode` and `--model` are replayed from the killed process.

**Codex CLI** — `~/.codex/sessions/<y>/<m>/<d>/rollout-<timestamp>-<uuid>.jsonl`.
`codex resume <uuid>` appends to the same file and keeps the same id, so the
mapping stays stable across any number of park/resume cycles. Flags captured
from the killed process are replayed on resume, including `--model`,
`--profile`, `--yolo`, and the sandbox and approval settings — a resumed session is never
*less* restricted than the one that was killed.

**Grok** — `~/.grok/sessions/<encoded-cwd>/<uuid>/`, with `updates.jsonl` as the
authoritative log. `--always-approve`, `--permission-mode`, `--sandbox` and
`--model` are replayed; `--cwd` is not, because the resume command sets it. Herdr has no Grok integration and reports no session id, so
the tool recovers it from Grok's own `active_sessions.json` by matching the
pane's process pid *and* cwd. Not restored by Grok on resume, by its design
rather than this tool's: background tasks, plan-mode state, and staged changes
under `--restore-code`. MCP servers are re-initialised from Grok's config.

### Reading the idle clock

File mtime is not a usable idle signal. Claude Code keeps rewriting bookkeeping
lines long after a conversation goes quiet, so a three-day-old session can show
an mtime of seconds ago. Only entries carrying their own timestamp count, and
the tool reads them from the tail of the file rather than parsing all of it.

### The clock the transcript cannot see

Nothing reaches the transcript until a turn completes. A prompt someone has
been composing for an hour therefore looks exactly like a pane they walked
away from: the agent is idle and has written nothing all that time. Parking it
throws the draft away, and resuming the session does not bring it back,
because it was never anywhere but the screen.

So the pane's terminal is read as a second clock. Every keystroke redraws the
agent's input line, and that redraw is a write to the pane's pts, which moves
its mtime. A pane is only a candidate when *both* clocks are past the
threshold. Measured on a live pane: an idle agent holding an unsubmitted
prompt did not touch its terminal for eight seconds, and one keystroke moved
it from three minutes to now.

The clock is read again immediately before the kill, not only when the pane is
classified, because a scan takes seconds and someone can start typing inside
them. A pane whose terminal cannot be found degrades to the transcript clock
alone rather than becoming unparkable, and `--force` skips the check.

Anything else drawn in the pane counts as activity too. That is the safe
direction to be wrong in: the cost is a pane that stays awake.

## Watchers and orchestrators

An orchestrator waiting on workers *looks* idle — its prompt is empty and its
own transcript is quiet — but killing it orphans the workers and loses the
wake-up they send back. Two guards cover that automatically, so watchers never
need manual pinning.

**Worker writes count as activity (Claude).** While sub-agents run, Claude
appends to `~/.claude/projects/<proj>/<sid>/subagents/*.jsonl`. Those writes
feed the idle clock exactly like the main transcript.

**Background jobs pin the pane (all agents).** An idle agent whose process tree
contains a child that started `BUSY_CHILD_MINUTES` or more *after* the agent
itself is treated as waiting on that job. Startup children — MCP servers,
helpers — are as old as the agent and never trigger it, and anything matching
`BUSY_IGNORE_TOKENS` is ignored. An agent that re-executed its own binary is
not mistaken for a background job.

If you rely on scheduled wake-ups with no process or file activity at all, keep
`HIBERNATE_AFTER_MINUTES` above 60, since those timers fire within the hour.

## The parking sequence

1. Idle test: status `idle`/`done` and transcript quiet past the threshold.
2. The agent's root process is found via `herdr pane process-info` and verified
   against the session id. An id mismatch or an ambiguous pane aborts.
3. Status is re-checked, then the process tree gets SIGTERM, and SIGKILL after
   `KILL_GRACE_SECONDS`.
4. The state record **and** the pane's stub script are written before anything
   is killed, so a crash mid-way still leaves the pane recoverable.
5. The stub is started in the pane's shell as a child, never with `exec` — the
   pane's own shell has to outlive it, or the next hibernation would close the
   tab.
6. The tab is renamed `💤 <label>`.

Killing the agent also leaves the terminal in whatever modes it had enabled.
The stub clears the line discipline and the mouse, bracketed-paste and focus
reporting modes; focus reporting is the one that prints `^[[I` and `^[[O` into
an otherwise idle pane whenever the window gains or loses focus.

## What a parked pane still says

Claude Code, Codex and Grok all draw on the terminal's **alternate screen
buffer**. When the process exits the emulator restores the normal buffer and
every visible turn goes with it. That is the terminal's doing, and it happens
identically when you quit an agent by hand. There is no scrollback to preserve,
so the stub reprints the tail of the conversation from the transcript instead.

- **The last answered exchange, plus any prompt still unanswered.** Showing
  only the newest prompt and its reply says nothing when the newest prompt has
  no reply — a pane parked mid-turn, or one where the last thing typed was
  `test`, would read `you test` and stop.
- **The reply is looked up *after* its prompt**, never backwards from the end
  of the file, so an unanswered prompt is never captioned with the previous
  turn's answer.
- **Both turns print in full** by default. A reply cut off after a few lines is
  what sends you back into the pane to read the rest, which is the thing this
  exists to avoid.
- **Lists and code blocks keep their shape**, one command per line. Ordinary
  prose is reflowed, because a source line break mid-sentence carries nothing
  and the stub rewraps to the pane width anyway.
- **Wrapping happens when the stub prints**, so a pane resized long after
  parking still lines up. The banner sits *below* the excerpt so that
  `press Enter to resume` stays next to the cursor.
- **The banner block is packed, not folded.** It is a list of `·` groups
  rather than a paragraph, so folding it as prose split phrases mid-word and
  dropped the continuation at column 0. Groups stay whole, a line that will
  not fit starts a new one, and the block hangs under a shared indent that
  lines it up with the excerpt above. Only a single group wider than the pane
  is folded.
- Slash commands, hook output, task notifications, compaction handoffs and
  sub-agent turns are not the human talking and are skipped. Codex's
  memory-citation markup and markdown link targets are dropped for the same
  reason.
- A long tool-heavy stretch can bury the conversation past the read window — a
  27MB Codex rollout had exactly one real turn in its last megabyte — so the
  window is widened once when the exchange comes back incomplete.
- Grok gets no excerpt yet: its transcript schema is unverified here, and a
  wrong excerpt is worse than none. A missing reader degrades to a plain
  banner.

## Parking a pane you just resumed

Herdr repopulates the session id on its own schedule, so for a short while
after a resume it reports nothing — exactly when you might want to undo a
resume. The agent's own arguments are unambiguous about it, having been started
as `claude --resume <uuid>`, so those are read instead.

Two limits. Two different session ids visible in one pane is refused rather
than guessed at. And the arguments are only trusted for a process's first ten
minutes, which is the whole gap being covered, and bounds the one case where
they lie: a session cleared or forked after being resumed still names the id it
started with.

### Not re-parking it three seconds later

The opposite mistake is easier to make. A session that has just come back has
an old last-message timestamp — loading history writes nothing — so the idle
clock reads the whole length of the park, and the pane qualifies again
immediately. The guard against that is the agent's own age: nothing can be
idler than it is old.

Process start time cannot supply that age. The stub hands over with `exec`, so
the resumed agent inherits the stub's pid *and* its start time, and `/proc`
reports an agent that returned three seconds ago as being as old as the park.
The guard silently passed for any pane parked longer than the threshold, which
is every pane worth parking.

The stub therefore stamps `panes/<pane_id>.awake` in the instant before it
execs the agent, and the agent's age is the smaller of that marker and what
`/proc` claims. Taking the smaller of the two keeps both honest: the marker is
missing for an agent started by hand, and stale for one that replaced a resumed
session. Markers age out at twice the idle threshold, since no record survives
a resume to own them.

## Surviving restarts

The stub is a process, so it dies with Herdr. Three mechanisms bring panes back
armed rather than leaving a row of `💤` tabs that are ordinary terminals.

**1. The on-disk stub script.** `panes/<pane_id>.sh` holds the session id, cwd,
tab id and original label. It never depends on a running process.

**2. The shell hook**, installed into `~/.bashrc` *and* `~/.zshrc`, since the
pane's shell is whichever one Herdr spawns. When Herdr starts a fresh shell in
a pane, the hook runs that pane's stub immediately — no daemon, no waiting for
a timer:

```bash
if [ -n "${HERDR_PANE_ID:-}" ] && [ -z "${HERDR_HIBERNATE_STUB:-}" ] && [[ $- == *i* ]]; then
    _hb_stub_file="$HOME/.config/herdr-hibernate/panes/${HERDR_PANE_ID//[^A-Za-z0-9]/_}.sh"
    [ -s "$_hb_stub_file" ] && HERDR_HIBERNATE_STUB=1 bash "$_hb_stub_file"
fi
```

`HERDR_HIBERNATE_STUB` is the loop guard: a shell that came *from* a stub never
re-arms itself.

The same block defines `hb-arm`, which is what parking a pane right now types
into it. The shell echoes that text, so it is the first thing you read above
the banner, and one word beats ninety characters of path and escapes. The alias
is only used when the pane's shell can actually run it — the shell must be one
the hook installs into, and it must have **started after its own rc was last
written**, because a running shell keeps the rc it read at startup and a hook
installed five minutes ago is invisible to every shell already open. Otherwise
the command is spelled out in full. As a backstop, a pane found at a bare shell
after being armed is re-armed with the spelled-out form, so a command the shell
cannot run is never retyped on every scan.

**3. `restore`.** A sweep that re-arms any pane whose stub is missing. It runs
when the watcher starts and at the top of every scan, and can be run by hand.
It is never gated by `DRY_RUN`, because it only hands a pane back the session
it already lost. It also repairs drift: a renumbered pane is matched by tab id,
then by label and cwd, and re-keyed. Stray stub scripts with no record are
garbage-collected every sweep.

Checking it worked:

```bash
./herdr-hibernate status     # every parked pane should read [stub armed]
```

## Storage and retention

Everything lives under `~/.config/herdr-hibernate/`:

| Path | Contents | Lifetime |
|---|---|---|
| `state.json` | One record per currently parked pane: session id, tab id, cwd, label. | Deleted when the pane resumes or the tab closes. |
| `panes/<pane_id>.sh` | The pane's stub script, including its last exchange. Owner-only (`0700`). | Deleted with its record. |
| `hibernate.log` | What the tool did. | Capped by `LOG_MAX_KB`, one rotation. |
| `config` | Your settings. | Permanent. |

Closing a tab erases its record and stub outright. Records also go when the
session resumes, when the pane is reused by another agent, or when the
transcript is gone and there is nothing left to resume.

`FORGET_AFTER_MINUTES` is a safety margin rather than retention: a pane must be
*missing* that long before erasure, so a half-started Herdr reporting an
incomplete pane list cannot wipe tabs that are still open. If the pane
reappears, the pending erase is cancelled.

## Pinning

`PINNED_TABS` is a list of tab ids in the config. It works, but **tab ids change
when Herdr restarts**, so a pin by id can silently detach from the tab it was
meant to protect.

The label marker is the durable one. Put `📌` (or your `PIN_MARKER`) anywhere in
a tab or pane label — `herdr tab rename <tab_id> "📌 my task"` — and it travels
with the label across restarts.
