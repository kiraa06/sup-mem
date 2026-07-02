# Security

## Threat model, plainly

sup-mem is **local-only by default**. The hooks read your prompts and session transcripts
and write to `~/.sup-mem/` on your machine. Nothing is transmitted anywhere unless you
explicitly opt into a hosted embedder (`voyage`/`openai` extras), which the embedding
detector warns about ("network + cost + leaves your box").

What sup-mem touches:

| Surface | Access | Why |
|---|---|---|
| Host config for each host you `init`: `~/.claude/settings.json` + `~/.claude.json`, `~/.codex/{hooks.json,config.toml}`, `~/.gemini/settings.json` | merge-only writes (Codex's MCP block is append-only), timestamped `.sup-mem.bak` | registering hooks + the MCP server (`sup-mem init`) |
| Session transcripts of the hosts you enable (Claude Code `~/.claude/projects/**.jsonl`; Codex `~/.codex/history.jsonl`; Gemini's session file) | read-only | outcome attribution (Stop / AfterAgent) + pre-compaction capture |
| `~/.sup-mem/` | read/write | the store, ledger, archive, logs, config, HMAC key |

## Integrity guarantees (and their limits)

Every write to the memory store joins an HMAC-SHA256 hash chain keyed by `~/.sup-mem/key`
(0600). `sup-mem verify` re-walks the chain and cross-checks every memory row in both tiers —
a direct SQLite edit of a memory, a deleted chain event, or a resurrected purged row is
detected and named. It also runs nightly via `sup-mem maintain`.

Stated limit: this is **tamper-evident, not tamper-proof**. An attacker who can read the
key file can re-chain history. The threat it defends against is silent modification by
other local software (or a compromised dependency) that can write files but hasn't
specifically targeted sup-mem's key.

## Prompt-injection surface

Memories are injected into Claude's context verbatim. A malicious *memory* is therefore a
prompt-injection vector — but memories only enter the store via the `remember` tool,
`migrate-native`, or pre-compaction capture, all of which record provenance (source,
session, speaker) in the chain. The quarantine mechanism (repeatedly contradicted memories
stop being injected) limits blast radius; `sup-mem roi` makes anomalous memories visible.

## Reporting

Please report vulnerabilities via GitHub Security Advisories
(Security tab → "Report a vulnerability") rather than public issues.
