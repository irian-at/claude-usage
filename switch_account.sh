#!/bin/bash
# Switch Claude Code to the least-used account.
# 1. Checks usage across all accounts
# 2. Finds the account with lowest "all models" usage
# 3. Gracefully stops all running claude instances
# 4. Logs in with the best account

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USAGE_FILE="$SCRIPT_DIR/usage_latest.json"

echo "Checking usage across all accounts..."
python3 "$SCRIPT_DIR/check_usage.py"

if [ ! -f "$USAGE_FILE" ]; then
    echo "Error: usage_latest.json not found"
    exit 1
fi

# Find the best account: both session AND weekly must be below 100%.
# Among eligible accounts, pick the one with the lowest weekly usage.
BEST=$(python3 -c "
import json, sys
from datetime import datetime, timezone

data = json.load(open('$USAGE_FILE'))

eligible = []
blocked = []
for account in data['accounts']:
    if 'error' in account:
        continue
    email = account.get('email', '')
    for org in account.get('orgs', []):
        usage = org.get('usage', {})
        session = usage.get('five_hour') or {}
        weekly = usage.get('seven_day') or {}
        session_pct = session.get('utilization', 0)
        weekly_pct = weekly.get('utilization', 0)

        entry = {'email': email, 'name': account['name'], 'session_pct': session_pct, 'weekly_pct': weekly_pct}

        session_resets_at = session.get('resets_at')
        weekly_resets_at = weekly.get('resets_at')
        now = datetime.now(timezone.utc)
        reset_parts = []
        if session_pct >= 100 and session_resets_at:
            dt = datetime.fromisoformat(session_resets_at)
            mins = max(0, int((dt - now).total_seconds()) // 60)
            reset_parts.append(f'session resets in {mins // 60}h {mins % 60}m')
        if weekly_pct >= 100 and weekly_resets_at:
            dt = datetime.fromisoformat(weekly_resets_at)
            mins = max(0, int((dt - now).total_seconds()) // 60)
            reset_parts.append(f'weekly resets in {mins // 60}h {mins % 60}m')
        entry['reset_info'] = ', '.join(reset_parts)

        if session_pct >= 100 or weekly_pct >= 100:
            blocked.append(entry)
        else:
            eligible.append(entry)

if eligible:
    eligible.sort(key=lambda c: c['weekly_pct'])
    best = eligible[0]
    status = f'session {best[\"session_pct\"]:.0f}%, weekly {best[\"weekly_pct\"]:.0f}%'
    print(f'{best[\"email\"]}|{best[\"name\"]}|{status}')
else:
    print('All accounts are at their limits:', file=sys.stderr)
    for b in blocked:
        print(f'  {b[\"name\"]}: session {b[\"session_pct\"]:.0f}%, weekly {b[\"weekly_pct\"]:.0f}% ({b[\"reset_info\"]})', file=sys.stderr)
")

if [ -z "$BEST" ]; then
    echo ""
    echo "No account available right now. All accounts have hit their session or weekly limit."
    echo "Wait for the earliest reset and try again."
    exit 1
fi

BEST_EMAIL=$(echo "$BEST" | cut -d'|' -f1)
BEST_NAME=$(echo "$BEST" | cut -d'|' -f2)
BEST_STATUS=$(echo "$BEST" | cut -d'|' -f3)

# Check current account
CURRENT_EMAIL=$(claude auth status 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('email',''))" 2>/dev/null || echo "")

echo ""
echo "Best account: $BEST_NAME ($BEST_EMAIL) — $BEST_STATUS"
echo "Current account: $CURRENT_EMAIL"

if [ "$CURRENT_EMAIL" = "$BEST_EMAIL" ]; then
    echo "Already on the best account. Nothing to do."
    exit 0
fi

# Record which TTYs have claude running (so we can resume in them later)
CURRENT_TTY=$(tty 2>/dev/null | sed 's|^/dev/||' || echo "none")
RESUME_TTYS_FILE=$(mktemp)
CLAUDE_PIDS=$(pgrep -x claude 2>/dev/null || true)
if [ -n "$CLAUDE_PIDS" ]; then
    ps -o tty= $CLAUDE_PIDS 2>/dev/null | tr -d ' ' | grep -v '^$' | grep -v '^\?\?$' | sort -u | while IFS= read -r t; do
        [ "$t" != "$CURRENT_TTY" ] && echo "/dev/$t"
    done > "$RESUME_TTYS_FILE" || true
fi

echo ""
echo "Stopping all Claude Code instances..."

# Send SIGINT first (like Ctrl+C) to cancel any in-progress operations
pkill -INT -x claude 2>/dev/null || true
sleep 2

# Then SIGTERM for graceful shutdown
pkill -TERM -x claude 2>/dev/null || true
sleep 2

# Verify they're gone
REMAINING=$(pgrep -x claude 2>/dev/null | wc -l | tr -d ' ' || echo "0")
if [ "$REMAINING" -gt 0 ]; then
    echo "Warning: $REMAINING claude processes still running. Waiting..."
    sleep 3
    pkill -TERM -x claude 2>/dev/null || true
    sleep 2
fi

echo "All Claude instances stopped."
echo ""
echo "Logging in as $BEST_NAME ($BEST_EMAIL)..."
claude auth login --email "$BEST_EMAIL"

# Resume claude in terminals where it was running
if [ -s "$RESUME_TTYS_FILE" ]; then
    TTY_COUNT=$(wc -l < "$RESUME_TTYS_FILE" | tr -d ' ')
    echo ""
    echo "Resuming claude in $TTY_COUNT terminal(s)..."

    TTY_LIST=$(sed 's/.*/"&"/' "$RESUME_TTYS_FILE" | paste -sd, -)

    if pgrep -q iTerm2 2>/dev/null; then
        osascript <<ENDSCRIPT || echo "Failed to auto-resume in iTerm2."
tell application "iTerm2"
    set ttyList to {$TTY_LIST}
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                try
                    if tty of s is in ttyList then
                        write text "claude --resume" in s
                    end if
                end try
            end repeat
        end repeat
    end repeat
end tell
ENDSCRIPT
    elif pgrep -qx Terminal 2>/dev/null; then
        osascript <<ENDSCRIPT || echo "Failed to auto-resume in Terminal.app."
tell application "Terminal"
    set ttyList to {$TTY_LIST}
    repeat with w in windows
        repeat with t in tabs of w
            try
                if tty of t is in ttyList then
                    do script "claude --resume" in t
                end if
            end try
        end repeat
    end repeat
end tell
ENDSCRIPT
    else
        echo "Could not detect terminal app. Run 'claude --resume' in your terminals."
    fi
else
    echo ""
    echo "No terminals to resume in."
fi

rm -f "$RESUME_TTYS_FILE"
