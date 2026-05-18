#!/usr/bin/env bash
# Stop hook: mark this session idle. Preserves the claude_pid mapping using
# the same parent-walk heuristic as prompt-submit.sh.

payload=$(cat 2>/dev/null)
sid=$(printf '%s' "$payload" | jq -r '.session_id // "default"' 2>/dev/null)
[ -z "$sid" ] || [ "$sid" = "null" ] && sid="default"

claude_pid=0
pid=$$
hops=0
while [ "$pid" != "1" ] && [ "$hops" -lt 30 ]; do
    if [ "$pid" != "$$" ]; then
        comm=$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')
        args=$(ps -o args= -p "$pid" 2>/dev/null)
        if [ "$comm" = "claude" ]; then
            claude_pid=$pid
            break
        fi
        case "$args" in
            *"/share/claude/versions/"*|*"claude daemon"*)
                claude_pid=$pid
                break
                ;;
        esac
    fi
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -z "$pid" ] && break
    hops=$((hops + 1))
done

ts=$(/usr/bin/env python3 -c 'import time; print(time.time())')
tmp=$(mktemp /tmp/claude-link-state.XXXXXX)
printf '{"phase":"idle","ts":%s,"sid":"%s","pid":%s}\n' "$ts" "$sid" "$claude_pid" > "$tmp"
mv "$tmp" "/tmp/claude-link-state-${sid}.json"
exit 0
