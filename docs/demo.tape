# sup-mem README demo — render with:
#   uv run python docs/demo_seed.py && vhs docs/demo.tape
# Runs the real binaries against the FICTIONAL seeded store (/tmp/supmem-demo).
# No real org data, accounts, usernames, or paths appear in any frame.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 720
Set Padding 16
Set TypingSpeed 35ms
Set Theme "Dracula"

Env SUP_MEM_DATA_DIR "/tmp/supmem-demo"
Env SUP_MEM_RETRIEVAL_K "1"

# PATH set at runtime so no absolute home path is baked into the tape or frames.
Hide
Type `export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH" && clear`
Enter
Show

# 1 — the store, at a glance
Type "sup-mem doctor"
Enter
Sleep 4s
Type "clear"
Enter

# 2 — the hook: memory injected automatically, per prompt
Type `echo '{"prompt":"why is payments-api restarting?"}' | sup-mem-hook-userprompt`
Enter
Sleep 5s
Type "clear"
Enter

# 3 — bitemporal recall: what did we believe THEN vs now
Type `sup-mem recall "payments-api restart cause" --as-of 2026-06-15 --diff-now`
Enter
Sleep 5.5s

# 4 — tamper-evident provenance
Type "sup-mem verify"
Enter
Sleep 3.5s
