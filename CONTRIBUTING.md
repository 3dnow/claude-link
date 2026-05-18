# Contributing to claude-link

Thanks for the interest. Short, opinionated guide.

## What helps most

In rough order of impact:

1. **Linux/Windows port** — see [#1](https://github.com/3dnow/claude-link/issues/1). Abstract `NettopFeed` behind an interface so the platform-specific bit is replaceable. Linux candidates: `nethogs` (sudo gated), eBPF (`bpftrace`), or `/proc/<pid>/net/dev` polling for interface-level stats.
2. **Real-world threshold tuning** — if your token stream looks different from what `PHASE_IN_BPS` / `PHASE_OUT_BPS` assume, open an issue with a sample. Defaults were chosen from one user's data.
3. **Per-session statusline integration recipes** — if you already have a custom statusline (powerline, ccstatusline, etc.) and got `claude-link-fragment` to compose cleanly with it, drop a snippet in `docs/integrations/`.

## Local dev loop

```bash
git clone https://github.com/3dnow/claude-link
cd claude-link

# Test the plugin in your live Claude Code without installing it
claude --plugin-dir ./plugins/claude-link

# Daemon: run in foreground for live debugging
python3 plugins/claude-link/lib/cc-netd.py
# Watch:
tail -F /tmp/claude-link-status.json | jq .

# Statusline fragment standalone (uses /tmp/claude-link-status.json):
bash plugins/claude-link/lib/status-fragment.sh; echo
```

## Tests

```bash
python3 -m pytest tests/ -v
```

Tests cover the pure-logic part (`PidTracker`). Anything that shells out to `nettop` / `ps` / `curl` is integration-tested by hand on macOS; CI verifies syntax + JSON validity but not full runtime behavior.

## Code style

- Python: stdlib only, no external deps. The daemon must run on macOS's system Python 3.9.
- Bash: pass shellcheck. `set -u`. Quote everything.
- Comments: explain *why*, not *what*. The `_run` method's PTY explanation is the bar.

## Sending changes

- One PR per topic. Don't bundle unrelated changes.
- If you change daemon classify() thresholds, add or update a test that pins the new behavior.
- For platform support PRs, keep the public daemon JSON schema the same so the fragment script doesn't need to change.

## Reporting bugs

Open an issue. Include:

- macOS version (`sw_vers`)
- Claude Code version (`claude --version`)
- Daemon log tail (`tail -20 $CLAUDE_PLUGIN_DATA/claude-link.log` or `~/.claude/cc-net/cc-netd.log`)
- A line from `/tmp/claude-link-status.json` showing the misbehavior

## Code of conduct

Be excellent to each other. Disagreements go through PRs and threaded discussion, not personal attacks.
