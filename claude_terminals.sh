#!/bin/bash
# Manage claude terminals: list, send messages, or repeat on interval.
#
# Usage:
#   ./claude_terminals.sh                          # interactive: list, select, set interval & message
#   ./claude_terminals.sh send 007 "fix the tests" # one-shot send to ttys007

set -euo pipefail

send_to_tty() {
    local tty_path="$1"
    local message="$2"

    osascript <<ENDSCRIPT 2>/dev/null
set targetTTY to "$tty_path"
set sent to false

tell application "System Events"
    set iTerm2Running to (exists process "iTerm2")
    set terminalRunning to (exists process "Terminal")
end tell

if iTerm2Running then
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    try
                        if tty of s is targetTTY then
                            write text "$message" in s
                            set sent to true
                        end if
                    end try
                end repeat
            end repeat
        end repeat
    end tell
end if

if terminalRunning and not sent then
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                try
                    if tty of t is targetTTY then
                        set selected of t to true
                        set index of w to 1
                        set sent to true
                    end if
                end try
            end repeat
        end repeat
    end tell
    if sent then
        tell application "Terminal" to activate
        delay 0.5
        tell application "System Events"
            tell process "Terminal"
                keystroke "$message"
                delay 0.2
                key code 36
            end tell
        end tell
    end if
end if

return sent
ENDSCRIPT
}

parse_interval() {
    local input="$1"
    local num="${input%[smh]}"
    local unit="${input##*[0-9]}"
    case "$unit" in
        s) echo "$num" ;;
        m) echo $((num * 60)) ;;
        h) echo $((num * 3600)) ;;
        *) echo $((num * 60)) ;;  # default to minutes
    esac
}

list_terminals() {
    local idx=0
    TERMINAL_LIST=()

    CLAUDE_PIDS=$(pgrep -x claude 2>/dev/null || true)
    if [ -z "$CLAUDE_PIDS" ]; then
        echo "No claude sessions running."
        exit 0
    fi

    echo ""
    for pid in $CLAUDE_PIDS; do
        tty=$(ps -o tty= -p "$pid" 2>/dev/null | tr -d ' ')
        cpu=$(ps -o %cpu= -p "$pid" 2>/dev/null | tr -d ' ')

        active_children=$(pgrep -P "$pid" 2>/dev/null | while read -r cpid; do
            ps -o command= -p "$cpid" 2>/dev/null
        done | grep -c '/bin/zsh -c\|/bin/bash -c' || true)

        if [ "$active_children" -gt 0 ]; then
            state="ACTIVE"
        elif echo "$cpu" | awk '{gsub(/,/,"."); exit ($1 > 20) ? 0 : 1}'; then
            state="THINKING"
        else
            state="IDLE"
        fi

        cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
        project=$(basename "$cwd" 2>/dev/null || echo "?")

        idx=$((idx + 1))
        TERMINAL_LIST+=("$tty")
        echo "  [$idx] $tty  $state  $project ($cwd)"
    done
    echo ""
}

# One-shot send mode
if [ "${1:-}" = "send" ]; then
    TTY_NUM="${2:?Usage: $0 send <tty-number> [message]}"
    MESSAGE="${3:-continue}"
    TTY_PATH="/dev/ttys$TTY_NUM"

    if send_to_tty "$TTY_PATH" "$MESSAGE"; then
        echo "Sent '$MESSAGE' to ttys$TTY_NUM"
    else
        echo "Error: could not send to ttys$TTY_NUM"
        exit 1
    fi
    exit 0
fi

# Interactive mode
list_terminals

read -rp "Select terminals (e.g. 1,3 or 1-3 or 'all'): " selection

# Parse selection into list of TTYs
SELECTED_TTYS=()
if [ "$selection" = "all" ]; then
    SELECTED_TTYS=("${TERMINAL_LIST[@]}")
else
    IFS=',' read -ra parts <<< "$selection"
    for part in "${parts[@]}"; do
        part=$(echo "$part" | tr -d ' ')
        if [[ "$part" == *-* ]]; then
            start="${part%-*}"
            end="${part#*-}"
            for ((i=start; i<=end; i++)); do
                SELECTED_TTYS+=("${TERMINAL_LIST[$((i-1))]}")
            done
        else
            SELECTED_TTYS+=("${TERMINAL_LIST[$((part-1))]}")
        fi
    done
fi

if [ ${#SELECTED_TTYS[@]} -eq 0 ]; then
    echo "No terminals selected."
    exit 1
fi

echo "Selected: ${SELECTED_TTYS[*]}"
read -rp "Interval (e.g. 30s, 5m, 1h) [default: 5m]: " interval_input
INTERVAL_SECONDS=$(parse_interval "${interval_input:-5m}")

read -rp "Message [default: continue]: " message_input
MESSAGE="${message_input:-continue}"

echo ""
echo "Sending '$MESSAGE' to ${#SELECTED_TTYS[@]} terminal(s) every ${interval_input:-5m}. Ctrl+C to stop."
echo ""

while true; do
    timestamp=$(date '+%H:%M:%S')
    for tty in "${SELECTED_TTYS[@]}"; do
        if send_to_tty "/dev/$tty" "$MESSAGE"; then
            echo "  [$timestamp] Sent to $tty"
        else
            echo "  [$timestamp] Failed to send to $tty"
        fi
    done
    sleep "$INTERVAL_SECONDS"
done
