#!/usr/bin/env bash
# Smoke tests for the Codex adapter behavior layer.
# Run from repo root: ./tests/test_codex_behavior.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

FAIL=0

assert_exit() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS  $desc (exit=$actual)"
  else
    echo "FAIL  $desc (expected=$expected, got=$actual)"
    FAIL=$((FAIL+1))
  fi
}

assert_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "PASS  $desc"
  else
    echo "FAIL  $desc (missing '$needle' in output)"
    FAIL=$((FAIL+1))
  fi
}

assert_not_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "PASS  $desc"
  else
    echo "FAIL  $desc (unexpected '$needle' in output)"
    FAIL=$((FAIL+1))
  fi
}

echo "=== SessionStart ==="
SESSION_OUT=$(echo '{}' | python3 hooks/codex_session_start.py 2>&1)
SESSION_CODE=$?
assert_exit "SessionStart empty input → exit 0" 0 "$SESSION_CODE"
assert_contains "SessionStart outputs additionalContext" "additionalContext" "$SESSION_OUT"
assert_contains "SessionStart injects engineering-playbook non-negotiable rules" "No hallucination" "$SESSION_OUT"
assert_contains "SessionStart injects engineering-governance spec gate section" "Spec gate" "$SESSION_OUT"
assert_contains "SessionStart injects the skill routing table" "Skill routing" "$SESSION_OUT"
# Regression guard: Codex's default additionalContextLimit is ~2500 tokens
# (~9.8k chars) and silently middle-truncates anything larger, dropping
# exactly the routing table and spec-gate section injected here — verified
# against a real Codex 0.147.0 session, and confirmed by
# developers.openai.com/codex/hooks. .codex-plugin/hooks.json raises this
# hook's additionalContextLimit to 6000 tokens (~24k chars); check both
# that the config is still set and that content stays comfortably under it.
HOOKS_LIMIT=$(jq -r '.hooks.SessionStart[0].hooks[0].additionalContextLimit // empty' .codex-plugin/hooks.json)
if [[ "$HOOKS_LIMIT" == "6000" ]]; then
  echo "PASS  .codex-plugin/hooks.json sets SessionStart additionalContextLimit=6000"
else
  echo "FAIL  .codex-plugin/hooks.json SessionStart additionalContextLimit is '$HOOKS_LIMIT', expected 6000 — without it, Codex's default ~2500-token limit truncates the routing table again"
  FAIL=$((FAIL+1))
fi
CTX_LEN=$(echo "$SESSION_OUT" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['hookSpecificOutput']['additionalContext']))")
BUDGET_CHARS=$(( ${HOOKS_LIMIT:-2500} * 4 ))
if [[ "$CTX_LEN" -lt "$BUDGET_CHARS" ]]; then
  echo "PASS  SessionStart additionalContext ($CTX_LEN chars) stays under the configured budget (~$BUDGET_CHARS chars)"
else
  echo "FAIL  SessionStart additionalContext is $CTX_LEN chars — exceeds the configured additionalContextLimit budget (~$BUDGET_CHARS chars), will get truncated"
  FAIL=$((FAIL+1))
fi

echo "=== UserPromptSubmit ==="
PROMPT_OUT=$(echo '{"prompt":"hello"}' | python3 hooks/codex_user_prompt.py 2>&1)
PROMPT_CODE=$?
assert_exit "UserPromptSubmit → exit 0" 0 "$PROMPT_CODE"
assert_contains "UserPromptSubmit outputs additionalContext" "additionalContext" "$PROMPT_OUT"

echo "=== No hallucinated skill references ==="
FEATURE_PLANNING_HITS=$(grep -rl "engineering-feature-planning" hooks/codex_*.py .codex-plugin/plugin.json 2>/dev/null || true)
if [[ -z "$FEATURE_PLANNING_HITS" ]]; then
  echo "PASS  no reference to nonexistent engineering-feature-planning skill"
else
  echo "FAIL  engineering-feature-planning still referenced in: $FEATURE_PLANNING_HITS"
  FAIL=$((FAIL+1))
fi

echo "=== Skill registration parity ==="
SKILLS_TYPE=$(jq -r '.skills | type' .codex-plugin/plugin.json)
if [[ "$SKILLS_TYPE" == "array" ]]; then
  echo "PASS  .codex-plugin/plugin.json .skills is an array"
else
  echo "FAIL  .codex-plugin/plugin.json .skills should be an array, got $SKILLS_TYPE"
  FAIL=$((FAIL+1))
fi
MANIFEST_COUNT=$(jq '.skills | length' .codex-plugin/plugin.json)
DIR_COUNT=$(find skills -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
assert_exit "skills array count matches skills/ directory count ($DIR_COUNT)" "$DIR_COUNT" "$MANIFEST_COUNT"

echo "=== PreToolUse — non-code Write ==="
WRITE_OUT=$(echo '{"tool_name":"Write","tool_input":{"file_path":"README.md","content":"# title\nshort"}}' \
  | python3 hooks/codex_context_guard.py 2>&1)
WRITE_CODE=$?
assert_exit "PreToolUse non-code write → exit 0" 0 "$WRITE_CODE"

echo "=== PreToolUse — Bash rm -rf ==="
NEUTRAL_TRANSCRIPT=$(mktemp)
echo '{"role":"user","content":"run a safe shell command"}' > "$NEUTRAL_TRANSCRIPT"
BASH_OUT=$(echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/test"},"session_id":"t","transcript_path":"'"$NEUTRAL_TRANSCRIPT"'"}' \
  | python3 hooks/codex_context_guard.py 2>&1)
BASH_CODE=$?
# Codex 0.147.0 REMOVED the "exit 2 with the reason on stderr" deny contract:
# a PreToolUse hook always exits 0 and its verdict travels in the stdout
# payload. This suite still asserted the removed contract, which is why these
# four checks failed against a guard that behaves exactly as documented in
# hooks/codex_context_guard.py and pinned by tests/structural/test_hook_contract.py.
# The deny itself is the permissionDecision field: a reason alone is ignored
# by Codex and the command runs.
assert_exit "PreToolUse destructive Bash → exit 0 (0.147 contract)" 0 "$BASH_CODE"
assert_contains "Bash deny is a Codex deny" '"permissionDecision":"deny"' "$BASH_OUT"
assert_contains "Bash deny mentions destructive" "Destructive" "$BASH_OUT"
assert_contains "Bash deny hands the command to the user" "run the command themselves" "$BASH_OUT"
assert_not_contains "Bash deny is never a Codex ask" '"permissionDecision":"ask"' "$BASH_OUT"

echo "=== PreToolUse — Bash commit attribution ==="
# Regression guard: this check used to run via a subprocess call into
# context_guard.py (delegated destructive-Bash handling). The Python
# rewrite calls playbook_core.check_ai_attribution in-process instead --
# must keep blocking the same way.
ATTR_OUT=$(echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"fix\" -m \"Co-Authored-By: Claude <noreply@anthropic.com>\""},"session_id":"t","transcript_path":"'"$NEUTRAL_TRANSCRIPT"'"}' \
  | python3 hooks/codex_context_guard.py 2>&1)
ATTR_CODE=$?
rm "$NEUTRAL_TRANSCRIPT"
assert_exit "PreToolUse commit with Claude attribution → exit 0 (0.147 contract)" 0 "$ATTR_CODE"
assert_contains "Commit attribution deny is a Codex deny" '"permissionDecision":"deny"' "$ATTR_OUT"
assert_contains "Commit attribution deny mentions Co-Authored-By" "Co-Authored-By" "$ATTR_OUT"

echo "=== PreToolUse — git push publishing a commit with attribution ==="
# Codex has no PostToolUse here, so the push is its only check on history: a
# commit whose message came from a file never shows the trailer in a command.
PUSH_DIR=$(mktemp -d)
git init -q --bare "$PUSH_DIR/remote.git"
git init -q -b main "$PUSH_DIR/repo"
git -C "$PUSH_DIR/repo" -c user.email=t@example.com -c user.name=t commit -q --allow-empty -m "first"
git -C "$PUSH_DIR/repo" remote add origin "$PUSH_DIR/remote.git"
git -C "$PUSH_DIR/repo" push -q -u origin main 2>/dev/null
printf 'feat: x\n\nCo-Authored''-By: Claude <noreply@anthropic.com>\n' > "$PUSH_DIR/msg.txt"
git -C "$PUSH_DIR/repo" -c user.email=t@example.com -c user.name=t commit -q --allow-empty -F "$PUSH_DIR/msg.txt"
PUSH_OUT=$(echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"},"session_id":"t","cwd":"'"$PUSH_DIR/repo"'"}' \
  | python3 hooks/codex_context_guard.py 2>&1)
rm -rf "$PUSH_DIR"
assert_contains "Push of a commit with attribution is a Codex deny" '"permissionDecision":"deny"' "$PUSH_OUT"
assert_contains "Push of a commit with attribution is denied" "Push denied" "$PUSH_OUT"

echo "=== PreToolUse — Bash safe (no deny) stdout clean ==="
SAFE_OUT=$(echo '{"tool_name":"Bash","tool_input":{"command":"echo hello"},"session_id":"t"}' \
  | python3 hooks/codex_context_guard.py 2>/tmp/codex-stderr.log)
SAFE_CODE=$?
assert_exit "PreToolUse Bash safe → exit 0" 0 "$SAFE_CODE"
if [[ "$SAFE_OUT" == '{"continue":true}' ]]; then
  echo "PASS  PreToolUse Bash safe emits the minimal pass-through payload"
else
  echo "FAIL  PreToolUse Bash safe should emit {\"continue\":true}, got: $SAFE_OUT"
  FAIL=$((FAIL+1))
fi

echo "=== Spec gate: PreToolUse records, Stop enforces (flush-lag fix) ==="
TMP_HOME=$(mktemp -d)
HOME_BACKUP="$HOME"
export HOME="$TMP_HOME"
SESSION_ID="spec-test"

# a) PreToolUse on a code write with no marker anywhere: must record
#    spec_pending, never deny.
PRE_OUT=$(echo "{\"tool_name\":\"Write\",\"session_id\":\"$SESSION_ID\",\"tool_input\":{\"file_path\":\"src/server.ts\",\"content\":\"line\nline\"}}" \
  | python3 hooks/codex_context_guard.py 2>&1)
PRE_CODE=$?
assert_exit "PreToolUse code write never denies (deferred to Stop)" 0 "$PRE_CODE"
STATE_FILE="$TMP_HOME/.codex/hooks/.state-$SESSION_ID.json"
if [[ -f "$STATE_FILE" ]] && grep -Eq '"spec_pending":[[:space:]]*true' "$STATE_FILE"; then
  echo "PASS  PreToolUse recorded spec_pending=true"
else
  echo "FAIL  expected spec_pending=true in $STATE_FILE"
  FAIL=$((FAIL+1))
fi

# b) Stop with spec_pending=true and no token in the transcript: must block.
NO_TOKEN_TRANSCRIPT=$(mktemp)
echo '{"role":"user","content":"add a health endpoint"}' > "$NO_TOKEN_TRANSCRIPT"
echo '{"role":"assistant","content":"done"}' >> "$NO_TOKEN_TRANSCRIPT"
STOP_BLOCK_OUT=$(echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$NO_TOKEN_TRANSCRIPT\"}" \
  | python3 hooks/codex_stop.py 2>&1)
STOP_BLOCK_CODE=$?
rm "$NO_TOKEN_TRANSCRIPT"
assert_exit "Stop with spec_pending and no token → exit 0" 0 "$STOP_BLOCK_CODE"
assert_contains "Stop blocks with decision" '"decision":"block"' "$STOP_BLOCK_OUT"

# c) Stop again, now with the token flushed to the transcript: spec_pending
#    must clear, but the write in (a) also set pending_validation=true, and
#    no validation command has run yet — Stop must still block, now for the
#    validation gate instead of the spec gate.
TOKEN_TRANSCRIPT=$(mktemp)
echo '{"role":"user","content":"add a health endpoint"}' > "$TOKEN_TRANSCRIPT"
echo '{"role":"assistant","content":"[SPEC CONFIRMED] — unambiguous single-line fix"}' >> "$TOKEN_TRANSCRIPT"
STOP_VALIDATION_OUT=$(echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$TOKEN_TRANSCRIPT\"}" \
  | python3 hooks/codex_stop.py 2>&1)
STOP_VALIDATION_CODE=$?
assert_exit "Stop with spec confirmed but no validation → exit 0" 0 "$STOP_VALIDATION_CODE"
assert_contains "Stop blocks for validation gate" "validation gate" "$STOP_VALIDATION_OUT"
if grep -Eq '"spec_pending":[[:space:]]*false' "$STATE_FILE"; then
  echo "PASS  Stop cleared spec_pending after token confirmed"
else
  echo "FAIL  expected spec_pending=false in $STATE_FILE after confirmation"
  FAIL=$((FAIL+1))
fi

# c2) Run a validation command (PreToolUse records it), then Stop again:
#     must pass clean now that both gates are satisfied.
echo "{\"tool_name\":\"Bash\",\"session_id\":\"$SESSION_ID\",\"tool_input\":{\"command\":\"npm run test\"}}" \
  | python3 hooks/codex_context_guard.py > /dev/null 2>&1
STOP_PASS_OUT=$(echo "{\"session_id\":\"$SESSION_ID\",\"transcript_path\":\"$TOKEN_TRANSCRIPT\"}" \
  | python3 hooks/codex_stop.py 2>&1)
STOP_PASS_CODE=$?
rm "$TOKEN_TRANSCRIPT"
assert_exit "Stop with spec confirmed and validation run → exit 0" 0 "$STOP_PASS_CODE"
assert_not_contains "Stop does not block once both gates are satisfied" '"decision":"block"' "$STOP_PASS_OUT"

# d) Regression guard: a turn with no code write never blocks, even with no
#    marker anywhere in the transcript (the pre-fix Stop hook blocked here).
CONVO_TRANSCRIPT=$(mktemp)
echo '{"role":"user","content":"what does this function do?"}' > "$CONVO_TRANSCRIPT"
echo '{"role":"assistant","content":"it parses the config file"}' >> "$CONVO_TRANSCRIPT"
NO_WRITE_OUT=$(echo "{\"session_id\":\"no-write-test\",\"transcript_path\":\"$CONVO_TRANSCRIPT\"}" \
  | python3 hooks/codex_stop.py 2>&1)
NO_WRITE_CODE=$?
rm "$CONVO_TRANSCRIPT"
assert_exit "Stop with no code write this session → exit 0" 0 "$NO_WRITE_CODE"
assert_not_contains "Stop never blocks a read-only turn" '"decision":"block"' "$NO_WRITE_OUT"

# d2) Regression guard: the hook-injected SessionStart/UserPromptSubmit
#     preamble documents both trigger strings as instructional examples
#     ("emit `[SPEC CONFIRMED]` ...", "state that you are invoking
#     playbook-off"). A transcript shaped like Codex's real
#     envelope, containing only that injected text (developer role) and a
#     world_state entry (which was found live to re-serialize the skill
#     catalog, including playbook-off's name) — with no genuine
#     user/assistant confirmation or invocation — must not trigger either
#     scan. This is the exact false-positive found via a live session.
SELFREF_TRANSCRIPT=$(mktemp)
cat > "$SELFREF_TRANSCRIPT" <<'JSONL'
{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"Spec gate: emit [SPEC CONFIRMED] before writing code. To suspend: state that you are invoking playbook-off."}]}}
{"type":"world_state","payload":{"state":{"skills":["playbook-off","engineering-implement"]}}}
{"type":"response_item","payload":{"type":"function_call_output","output":"grep found [SPEC CONFIRMED] in a tool output"}}
{"type":"response_item","payload":{"type":"reasoning","content":[{"type":"reasoning_text","text":"the transcript mentions playbook-off"}]}}
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"add a health endpoint"}]}}
JSONL
SELFREF_OUT=$(python3 -c "
import sys
sys.path.insert(0, 'hooks')
import playbook_core
lines = open('$SELFREF_TRANSCRIPT').read().split('\n')
print('spec_token=' + str(playbook_core.has_spec_token(lines)))
print('suspended=' + str(playbook_core.is_suspended(lines)))
")
rm "$SELFREF_TRANSCRIPT"
assert_contains "Self-referential preamble does not satisfy spec token" "spec_token=False" "$SELFREF_OUT"
assert_contains "Self-referential preamble/world_state does not trigger suspension" "suspended=False" "$SELFREF_OUT"

# e) Suspension (/playbook-off): a code write with no spec token
#    and no validation must still pass through PreToolUse and Stop cleanly
#    once the transcript shows the suspend skill was the most recent of the
#    suspend/resume names.
SUSPEND_SESSION="suspend-test"
SUSPEND_TRANSCRIPT=$(mktemp)
echo '{"role":"user","content":"turn off governance, invoking playbook-off"}' > "$SUSPEND_TRANSCRIPT"
echo '{"role":"assistant","content":"Acknowledged: playbook-off"}' >> "$SUSPEND_TRANSCRIPT"
SUSPEND_PRE_OUT=$(echo "{\"tool_name\":\"Write\",\"session_id\":\"$SUSPEND_SESSION\",\"transcript_path\":\"$SUSPEND_TRANSCRIPT\",\"tool_input\":{\"file_path\":\"src/x.ts\",\"content\":\"a\nb\"}}" \
  | python3 hooks/codex_context_guard.py 2>&1)
SUSPEND_PRE_CODE=$?
assert_exit "PreToolUse code write while suspended → exit 0" 0 "$SUSPEND_PRE_CODE"
SUSPEND_STATE_FILE="$TMP_HOME/.codex/hooks/.state-$SUSPEND_SESSION.json"
if [[ ! -f "$SUSPEND_STATE_FILE" ]] || ! grep -Eq '"spec_pending":[[:space:]]*true' "$SUSPEND_STATE_FILE"; then
  echo "PASS  PreToolUse does not record spec_pending while suspended"
else
  echo "FAIL  PreToolUse recorded spec_pending while playbook-off was active"
  FAIL=$((FAIL+1))
fi
SUSPEND_STOP_OUT=$(echo "{\"session_id\":\"$SUSPEND_SESSION\",\"transcript_path\":\"$SUSPEND_TRANSCRIPT\"}" \
  | python3 hooks/codex_stop.py 2>&1)
SUSPEND_STOP_CODE=$?
assert_exit "Stop while suspended → exit 0" 0 "$SUSPEND_STOP_CODE"
assert_not_contains "Stop never blocks while suspended" '"decision":"block"' "$SUSPEND_STOP_OUT"

# f) Resume (engineering-playbook): after the suspend skill is followed by a
#    resume skill later in the transcript, gates are active again.
echo '{"role":"user","content":"resume: engineering-playbook"}' >> "$SUSPEND_TRANSCRIPT"
RESUME_PRE_OUT=$(echo "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"rm -rf /tmp/whatever\"},\"session_id\":\"$SUSPEND_SESSION\",\"transcript_path\":\"$SUSPEND_TRANSCRIPT\"}" \
  | python3 hooks/codex_context_guard.py 2>&1)
RESUME_PRE_CODE=$?
rm "$SUSPEND_TRANSCRIPT"
assert_exit "PreToolUse destructive Bash after resume → exit 0 (0.147 contract)" 0 "$RESUME_PRE_CODE"

export HOME="$HOME_BACKUP"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$TMP_HOME"

echo "=== PreToolUse — governance disabled ==="
TMP_HOME=$(mktemp -d)
mkdir -p "$TMP_HOME/.codex"
cat > "$TMP_HOME/.codex/config.toml" <<EOF
[playbook]
governance = false
EOF
HOME_BACKUP="$HOME"
export HOME="$TMP_HOME"
GOV_OUT=$(echo '{"tool_name":"Write","tool_input":{"file_path":"src/x.ts","content":"hello\nworld"}}' \
  | python3 hooks/codex_context_guard.py 2>&1)
GOV_CODE=$?
export HOME="$HOME_BACKUP"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$TMP_HOME"
assert_exit "PreToolUse with governance=false → exit 0" 0 "$GOV_CODE"

echo "=== Stop — stop_hook_active=true (avoid loop) ==="
# Isolate HOME: codex_stop.py always reads/writes the session state file on
# this path, even with no session_id (falls back to a cwd-derived slug) —
# without isolation this leaks a stray .state-*.json into the real
# ~/.codex/hooks/ on whoever's machine runs the suite.
TMP_HOME=$(mktemp -d)
HOME_BACKUP="$HOME"
export HOME="$TMP_HOME"
LOOP_OUT=$(echo '{"stop_hook_active":true}' | python3 hooks/codex_stop.py 2>&1)
LOOP_CODE=$?
export HOME="$HOME_BACKUP"
python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$TMP_HOME"
assert_exit "Stop with stop_hook_active=true → exit 0 (no block)" 0 "$LOOP_CODE"

echo "=== PreCompact / PostCompact ==="
PRECOMPACT_OUT=$(echo '{"hook_event_name":"PreCompact","trigger":"auto","session_id":"t"}' | python3 hooks/codex_compact.py 2>&1)
PRECOMPACT_CODE=$?
assert_exit "PreCompact → exit 0" 0 "$PRECOMPACT_CODE"
assert_contains "PreCompact reports trigger" "trigger: auto" "$PRECOMPACT_OUT"
POSTCOMPACT_OUT=$(echo '{"hook_event_name":"PostCompact","trigger":"manual","session_id":"t"}' | python3 hooks/codex_compact.py 2>&1)
POSTCOMPACT_CODE=$?
assert_exit "PostCompact → exit 0" 0 "$POSTCOMPACT_CODE"
assert_contains "PostCompact reports trigger" "trigger: manual" "$POSTCOMPACT_OUT"
assert_contains "PostCompact notes governance was re-primed" "re-primed" "$POSTCOMPACT_OUT"
HOOKS_EVENTS=$(jq -r '.hooks | keys | join(",")' .codex-plugin/hooks.json)
assert_contains "hooks.json registers PreCompact" "PreCompact" "$HOOKS_EVENTS"
assert_contains "hooks.json registers PostCompact" "PostCompact" "$HOOKS_EVENTS"

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "$FAIL TESTS FAILED"
  exit 1
fi
