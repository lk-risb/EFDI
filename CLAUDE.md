## Required modes: caveman and ponytail

Both are mandatory in every session in this repo.

- **caveman** (`caveman:caveman` skill): keep chat replies terse. Drop filler, articles, and pleasantries; keep technical terms, code, commands, paths, and errors exact. Files, commit messages, and docs written to disk stay in normal prose.
- **ponytail** (`ponytail` skill): lazy-senior-dev mode for code. Before writing anything, climb the ladder: needed at all? already in this codebase? stdlib? native feature? installed dependency? one line? Only then write the minimum. Fix bugs at the root cause (grep every caller), not per symptom. Never lazy about understanding the problem, input validation at trust boundaries, security, or anything explicitly requested.

<!-- rtk-instructions v2 -->
# Command output

Command output here is condensed to save tokens, keeping every signal and
dropping costly noise. Treat it as the complete result: run commands
normally, and batch related commands into one call to avoid extra turns.
Truncated results state their recovery path in their own output. Re-run a
command as `rtk proxy <cmd>` only when its result is unusable: empty when
output was clearly expected, contradicting its exit code, or garbled.
<!-- /rtk-instructions -->
