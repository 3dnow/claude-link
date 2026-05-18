#!/usr/bin/env bash
# Self-contained statusline demo for the README GIF. Cycles through phases
# in-place (\r) on a single line so the animation reads like a real TUI bar.
# No daemon / no /tmp files required.

set -u

# Clear screen + hide cursor so the GIF only shows the statusline animation.
printf '\033[2J\033[H\033[?25l'
trap "printf '\\033[?25h\\n'" EXIT

reset=$'\033[0m'; dim=$'\033[2m'
green=$'\033[32m'; yellow=$'\033[33m'
blue=$'\033[34m'; cyan=$'\033[36m'

header() {
  # The 'permanent' left portion: model, 5h usage, reset time.
  printf "${dim}[${reset}${cyan}Opus-4.7${reset}${dim}]${reset} ${dim}5h${reset} ${green}35%%${reset} ${dim}reset@${reset}${yellow}22:13${reset}"
}

frame() {
  # $1 = trailing fragment string (already colored)
  printf "\r\033[K%s   %s" "$(header)" "$1"
}

ping_ok="${green}●${reset} ${dim}4.2ms${reset}"

# Sequences
loop() {
  # Idle (settle)
  frame "${ping_ok}"; sleep 0.9
  frame "${ping_ok}"; sleep 0.8

  # Upload burst
  frame "${blue}⬆${reset} ${dim}42K/s${reset}   ${ping_ok}"; sleep 0.45
  frame "${blue}⬆${reset} ${dim}68K/s${reset}   ${ping_ok}"; sleep 0.45

  # Waiting for first byte
  frame "${yellow}⏳${reset} ${dim}0.5s${reset}   ${ping_ok}"; sleep 0.55
  frame "${yellow}⏳${reset} ${dim}1.1s${reset}   ${ping_ok}"; sleep 0.55
  frame "${yellow}⏳${reset} ${dim}1.7s${reset}   ${ping_ok}"; sleep 0.55

  # Streaming back (ramps up then trails)
  frame "${cyan}⬇${reset} ${dim}850B/s${reset}   ${ping_ok}"; sleep 0.45
  frame "${cyan}⬇${reset} ${dim}3K/s${reset}   ${ping_ok}";  sleep 0.45
  frame "${cyan}⬇${reset} ${dim}12K/s${reset}   ${ping_ok}"; sleep 0.45
  frame "${cyan}⬇${reset} ${dim}18K/s${reset}   ${ping_ok}"; sleep 0.45
  frame "${cyan}⬇${reset} ${dim}9K/s${reset}   ${ping_ok}";  sleep 0.45
  frame "${cyan}⬇${reset} ${dim}2K/s${reset}   ${ping_ok}";  sleep 0.45

  # Tail: another small upload (e.g. tool result echo)
  frame "${blue}⬆${reset} ${dim}1K/s${reset}   ${cyan}⬇${reset} ${dim}600B/s${reset}   ${ping_ok}"; sleep 0.5

  # Idle
  frame "${ping_ok}"; sleep 1.2
}

# Initial state
frame "${ping_ok}"
sleep 0.4

# Run a single cycle
loop

# Settle
frame "${ping_ok}"
sleep 0.6
printf "\n"
