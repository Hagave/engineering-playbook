#!/bin/bash
# Behavioral test runner for Engineering Playbook skills.
#
# These tests require:
#   1. ANTHROPIC_API_KEY set in environment
#   2. claude CLI installed and accessible
#   3. The engineering-playbook plugin registered at ~/.engineering-playbook
#
# Usage:
#   chmod +x run_behavioral.sh
#   ./run_behavioral.sh              # run every prompt in prompts/
#   ./run_behavioral.sh 02           # run only prompt 02
#
# What "pass" means for each test is defined in the corresponding prompts/ file
# under "Expected model behavior". These tests are inherently probabilistic —
# a result in the 80–95% range is expected. A consistent miss (< 5/10 runs) is
# a regression worth investigating.

set -euo pipefail

PROMPTS_DIR="$(dirname "$0")/prompts"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
FILTER="${1:-}"
PASS=0
FAIL=0

run_prompt() {
    local num="$1"
    local file="$PROMPTS_DIR/${num}_"*
    file="$(ls $file 2>/dev/null | head -1)"

    if [[ -z "$file" ]]; then
        echo "SKIP: no prompt file for $num"
        return
    fi

    local name
    name="$(basename "$file" .txt)"
    echo ""
    echo "━━━ $name ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Extract the prompt (lines before "Expected model behavior:")
    local prompt
    prompt="$(sed '/^Expected model behavior/,$ d' "$file" | sed '/^$/d' | tail -n +1)"

    echo "Prompt:"
    echo "$prompt"
    echo ""
    echo "Response:"

    local response
    response="$($CLAUDE_BIN -p "$prompt" 2>&1)" || true
    echo "$response"
    echo ""

    # Check for anti-patterns (defined in prompts)
    local anti_section
    # Each section runs to the next blank line, so one file can hold both.
    anti_section="$(sed -n '/^Anti-pattern to detect/,/^$/ p' "$file")"

    if [[ -n "$anti_section" ]]; then
        local had_antipattern=false
        while IFS= read -r line; do
            [[ "$line" =~ ^- ]] || continue
            local pattern="${line#- }"
            if echo "$response" | grep -qi "$pattern"; then
                echo "FAIL: anti-pattern detected: $pattern"
                had_antipattern=true
            fi
        done <<< "$anti_section"

        if [[ "$had_antipattern" == false ]]; then
            echo "PASS: no anti-patterns detected"
            PASS=$((PASS + 1))
        else
            FAIL=$((FAIL + 1))
        fi
    fi

    # "Must mention:" lists alternatives (ERE, case-insensitive), one line each;
    # every line must match somewhere in the response. Used by the review
    # fixtures (06+), one per engineering-security fiche.
    local must_section
    must_section="$(sed -n '/^Must mention/,/^$/ p' "$file")"
    if [[ -n "$must_section" ]]; then
        local missed=false
        while IFS= read -r line; do
            [[ "$line" =~ ^- ]] || continue
            local pattern="${line#- }"
            if ! echo "$response" | grep -qiE "$pattern"; then
                echo "FAIL: response never mentions: $pattern"
                missed=true
            fi
        done <<< "$must_section"
        if [[ "$missed" == false ]]; then
            echo "PASS: every expected finding mentioned"
            PASS=$((PASS + 1))
        else
            FAIL=$((FAIL + 1))
        fi
    fi

    # Specific checks per test
    case "$num" in
        01)
            if echo "$response" | grep -q '\[SPEC CONFIRMED\]'; then
                echo "PASS: [SPEC CONFIRMED] emitted before write"
                PASS=$((PASS + 1))
            else
                echo "FAIL: [SPEC CONFIRMED] not found in response"
                FAIL=$((FAIL + 1))
            fi
            ;;
        02)
            # The request leaves decisions open and delegates none, so the model
            # stops before code. The token is the proof it did not: the word
            # "STOP" in the reply is not, since asking needs no such heading.
            if echo "$response" | grep -q 'SPEC CONFIRMED'; then
                echo "FAIL: [SPEC CONFIRMED] emitted with open decisions — went straight to code"
                FAIL=$((FAIL + 1))
            else
                echo "PASS: stopped before implementation"
                PASS=$((PASS + 1))
            fi
            ;;
        03)
            if echo "$response" | grep -qi 'ThrottlerBehindProxyGuard'; then
                echo "PASS: routed to correct rate limit mechanism"
                PASS=$((PASS + 1))
            else
                echo "FAIL: ThrottlerBehindProxyGuard not mentioned"
                FAIL=$((FAIL + 1))
            fi
            ;;
        04)
            if echo "$response" | grep -qi 'env' && ! echo "$response" | grep -qi 'JWT_SECRET.*=.*[a-zA-Z0-9].*[^{]'; then
                echo "PASS: JWT_SECRET handled via env"
                PASS=$((PASS + 1))
            else
                echo "CHECK: verify JWT_SECRET is not hardcoded"
            fi
            ;;
        05)
            if echo "$response" | grep -qi 'transaction\|BEGIN\|prisma\.\$transaction'; then
                echo "PASS: transaction wrapping detected"
                PASS=$((PASS + 1))
            else
                echo "FAIL: no transaction — both operations may not be atomic"
                FAIL=$((FAIL + 1))
            fi
            ;;
    esac
}

# Main
if [[ -n "$FILTER" ]]; then
    run_prompt "$FILTER"
else
    for file in "$PROMPTS_DIR"/[0-9][0-9]_*.txt; do
        run_prompt "$(basename "$file" | cut -c1-2)"
    done
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed"

exit "$FAIL"
