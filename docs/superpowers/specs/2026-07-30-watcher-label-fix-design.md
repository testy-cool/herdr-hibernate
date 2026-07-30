# Watcher and tab-label fix

## Goal

Prevent concurrent hibernation operations and always repair a tool-owned sleeping label when an agent wakes.

## Design

Use two advisory file locks:

- A watcher lock is held for the watcher's full lifetime. This makes watcher startup atomic.
- An operation lock is held during each state-changing scan, manual hibernate, restore, or forget operation. This prevents the watcher, F9, and future event hooks from changing the same state at once.

Store whether the tool added the tab marker and the exact marked label. On wake cleanup, restore the saved original label only when:

- this record added the marker;
- the current label exactly matches the recorded marked label; and
- no other hibernated pane belongs to the same tab.

The generated stub keeps its immediate rename for fast feedback. The reaper cleanup becomes the reliable fallback before it deletes the state record.

## Error handling

A second watcher exits without changing state. A competing one-shot operation waits for the operation lock. A changed user label is left untouched. Rename failures keep the state record so cleanup can retry.

## Tests

Tests use an imported script module with mocked Herdr calls and temporary state paths. They cover:

- only one watcher lock owner;
- exact marker cleanup after wake;
- user labels beginning with `💤` remain unchanged;
- user-renamed labels remain unchanged;
- a multi-pane tab keeps its marker while another pane is still hibernated;
- rename failure keeps the record for retry.

No test targets the live Herdr session.

## Instruction update

Add one communication rule to both `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`. It requires very simple English, enough context to understand the answer, and only details that affect the user.
