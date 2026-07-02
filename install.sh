#!/bin/sh
# sup-mem one-line installer (default path: SQLite FTS — no Docker, no model). I8.
#
#   curl -LsSf https://raw.githubusercontent.com/kiranjose/sup-mem/main/install.sh | sh
#
# Overrides:
#   SUP_MEM_SPEC   pip/uv install spec (default: "sup-mem"). For an unpublished
#                        checkout use e.g. "git+https://github.com/kiranjose/sup-mem".
#   SUP_MEM_NO_INIT set to skip the automatic `sup-mem init`.
set -eu

SPEC="${SUP_MEM_SPEC:-sup-mem}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# 1. Ensure uv is available (fast, reproducible installs — the reason we chose uv, §5).
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME); make it visible for the rest of this run.
  export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"
fi

# 2. Install sup-mem as an isolated uv tool (gives us the `sup-mem` command).
say "Installing sup-mem ($SPEC)..."
uv tool install --force "$SPEC"

# 3. Initialize the default store + register the hook and MCP server into Claude Code.
if [ -z "${SUP_MEM_NO_INIT:-}" ]; then
  say "Initializing memory (SQLite FTS) and registering with Claude Code..."
  sup-mem init
fi

say "Done. Restart Claude Code so it picks up the new hook + MCP server."
