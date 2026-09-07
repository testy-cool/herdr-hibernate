# Working on Herdr Hibernate

Use plain language in code comments and docs. Write for someone in high school.
Use common words, short sentences, and examples when they help.

## Know which copy you are changing

There are two copies of this project:

- Your source checkout is where you edit, test, and commit changes.
- The installed plugin is the copy the background watcher runs.

A commit in your source checkout does not change the installed copy.
Herdr records the installed folder in `~/.config/herdr/plugins.json`.
Find the entry with `plugin_id` set to `bengemine.hibernate`.
Its `plugin_root` gives the folder. Its `source.resolved_commit` gives the commit.

The Linux service is normally `herdr-hibernate.service`. Check its command with:

```sh
systemctl --user cat herdr-hibernate.service
```

The watcher keeps the code in memory. Restart that service after installing new
code. You do not need to restart Herdr itself.

## Make and test a change

Keep each change small. Preserve changes made by other people or agents.
Run the tests from the source checkout:

```sh
python3 -m unittest discover -s tests -q
git diff --check
```

Tests cover the generated Bash script, including its Enter-to-resume command.
They use stand-in agent programs. Passing them does not prove that a real agent
can recover its conversation. If you change how sessions are found or resumed,
also test a disposable session through a real sleep and wake cycle. Do not use
someone's working pane for that test.

Inspect the diff, then commit the files you changed. Use a short commit subject
that says what the change does. Push to your fork, `testy-cool/herdr-hibernate`.
Do not push to `bengemine/herdr-hibernate` or open a pull request there unless
explicitly asked. The upstream push URL is disabled on purpose.

## Install a local commit

This helper updates an existing Linux installation. For a first install, follow
the README. Run the helper from a separate source checkout with no unfinished
changes in either checkout:

```sh
./scripts/install-local --check
./scripts/install-local
```

The first command runs the tests and checks the paths without installing.
The second command:

1. Runs those checks again.
2. Saves the current plugin record and commit for rollback.
3. Installs the source checkout's current commit.
4. Updates only Hibernate's entry in Herdr's plugin registry.
5. Restarts only the Hibernate watcher.
6. Checks the installed commit, service, and Claude/Codex resume flags.

It prints the full source, installed, and backup paths. If a check fails after
installation starts, it tries to put back the old commit and plugin entry, then
restart the watcher. If rollback fails too, keep the backup and inspect the
reported error before trying again. `rollback.json` in the backup gives the old
commit and installed folder. `plugins.json` holds the old plugin records; do not
copy it over newer records for other plugins.

The helper does not push commits or wake agent panes.

## Keep machine settings local

The helper finds the installed folder in the plugin registry. Its own location
tells it where the source checkout is. Do not put your username or machine's
absolute paths in shared docs or scripts.

If your registry or service uses a different location or name, create
`.install-local.json` in the source checkout. Git ignores this file. For example:

```json
{
  "registry": "~/.config/herdr/plugins.json",
  "service": "herdr-hibernate.service"
}
```

## Understand parked panes

Supported agents are Claude, Codex, pi, and Grok. Agy is currently excluded.
Claude resumes with `--dangerously-skip-permissions`. Codex resumes with `--yolo`
or its equivalent long flag. The other agents keep their saved permission flags.

A parked pane runs a small Bash script, called a stub. Bash has already loaded
its resume command. Replacing that script on disk does not change the command
inside the running Bash process. Restarting the watcher does not change it
either. New or reloaded stubs get the new command.

Do not claim that an install updated commands inside already-running stubs.
Do not wake or reload someone's panes just to apply an update unless asked.

Session records and stub scripts live under `~/.config/herdr-hibernate/`.
They can contain private conversation text. Keep them out of Git.
