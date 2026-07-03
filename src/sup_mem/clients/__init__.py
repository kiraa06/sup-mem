"""Supported hosts and how to select one.

The active host at hook time is carried by the ``SUP_MEM_CLIENT`` env var, which registration
bakes into non-default clients' hook commands. Absent → Claude Code (the default, unchanged).
"""

from __future__ import annotations

import os

from sup_mem.clients.antigravity import AntigravityClient
from sup_mem.clients.base import ClientAdapter
from sup_mem.clients.claude import ClaudeClient
from sup_mem.clients.codex import CodexClient
from sup_mem.clients.gemini import GeminiClient

DEFAULT_CLIENT = "claude"

# Insertion order is the detection/registration order (Claude first — the flagship).
CLIENTS: dict[str, ClientAdapter] = {
    "claude": ClaudeClient(),
    "codex": CodexClient(),
    "gemini": GeminiClient(),
    "antigravity": AntigravityClient(),
}


def get_client(name: str) -> ClientAdapter:
    """The adapter for ``name``, falling back to Claude Code for any unknown value."""
    return CLIENTS.get(name, CLIENTS[DEFAULT_CLIENT])


def active_client_name() -> str:
    """Which host is invoking this hook, per ``SUP_MEM_CLIENT`` (default: claude)."""
    name = os.environ.get("SUP_MEM_CLIENT", "").strip().lower()
    return name if name in CLIENTS else DEFAULT_CLIENT


def detect_installed() -> list[str]:
    """Names of hosts that look present on this machine (config dir or binary on PATH)."""
    return [name for name, client in CLIENTS.items() if client.is_installed()]


__all__ = [
    "CLIENTS",
    "DEFAULT_CLIENT",
    "ClientAdapter",
    "active_client_name",
    "detect_installed",
    "get_client",
]
