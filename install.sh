#!/bin/sh
# claude-memory one-line installer (default path: SQLite FTS — no Docker, no model). I8.
#
#   curl -LsSf https://raw.githubusercontent.com/kiranjose/claude-memory/main/install.sh | sh
#
# Overrides:
#   CLAUDE_MEMORY_SPEC   pip/uv install spec (default: "claude-memory"). For an unpublished
#                        checkout use e.g. "git+https://github.com/kiranjose/claude-memory".
#   CLAUDE_MEMORY_NO_INIT set to skip the automatic `claude-memory init`.
set -eu

SPEC="${CLAUDE_MEMORY_SPEC:-claude-memory}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# 1. Ensure uv is available (fast, reproducible installs — the reason we chose uv, §5).
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME); make it visible for the rest of this run.
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
fi

# 2. Install claude-memory as an isolated uv tool (gives us the `claude-memory` command).
say "Installing claude-memory ($SPEC)..."
uv tool install --force "$SPEC"

# 3. Initialize the default store + register the hook and MCP server into Claude Code.
if [ -z "${CLAUDE_MEMORY_NO_INIT:-}" ]; then
  say "Initializing memory (SQLite FTS) and registering with Claude Code..."
  claude-memory init
fi

say "Done. Restart Claude Code so it picks up the new hook + MCP server."
