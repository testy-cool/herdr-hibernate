# Terminal Mode Reset Design

## Problem

Codex and Grok can leave terminal focus reporting enabled when the hibernator
stops them. Focus changes, including opening Windows Snipping Tool, then appear
in the hibernated pane as harmless but distracting text such as `^[[I` and
`^[[O`. The same text can remain visible after dismissing the hibernation stub
with Ctrl-C.

## Design

The hibernator will perform a small, targeted terminal reset before launching
the waiting stub. The generated stub will repeat the reset before displaying
its banner and when Ctrl-C dismisses it.

The reset will:

- restore normal terminal line handling with `stty sane` when a terminal is
  available;
- disable terminal focus reporting;
- disable bracketed paste and common mouse tracking modes that an abruptly
  stopped interactive agent may leave enabled.

The reset will not clear the screen, delete scrollback, filter input, or drain
queued keyboard data. Existing pane content will remain visible. Once focus
reporting is disabled, later focus changes will no longer generate visible
codes.

## Scope

The change applies to the shared stub path, so it covers every supported agent,
including Claude, Codex, and Grok. Existing durable stubs will receive the fix
when the normal watcher rewrites them from their saved state.

## Failure Handling

Terminal-reset commands are best effort. A missing terminal or unsupported
mode must not prevent hibernation, resume, or Ctrl-C dismissal.

## Tests

Regression tests will verify that:

- the command used to arm a pane resets terminal modes before starting the
  durable stub;
- the generated stub resets modes before its banner;
- the Ctrl-C exit path resets modes before returning to Bash;
- the reset does not clear the screen or drain user input.
