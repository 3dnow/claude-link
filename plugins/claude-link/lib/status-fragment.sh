#!/usr/bin/env bash
# shellcheck disable=SC2059
# (printf format strings contain ANSI escapes from $variables — constants,
# not user input, so SC2059's injection concern doesn't apply.)
# Emits a colored network status fragment for Claude Code's statusline.
# Reads /tmp/claude-link-status.json (written by the daemon).
#
# If $CC_NET_SESSION_ID (or $CLAUDE_LINK_SESSION_ID) is set and maps to a
# tracked PID, shows just that session's stats. Otherwise falls back to the
# aggregate of all claude processes.

f=/tmp/claude-link-status.json
[ -f "$f" ] || exit 0

reset=$'\033[0m'; dim=$'\033[2m'
red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'
blue=$'\033[34m'; cyan=$'\033[36m'

age=$(( $(date +%s) - $(stat -f %m "$f" 2>/dev/null || echo 0) ))
if [ "$age" -gt 8 ]; then
  printf "   ${dim}net?${reset}"
  exit 0
fi

sid="${CLAUDE_LINK_SESSION_ID:-${CC_NET_SESSION_ID:-}}"

IFS=$'\t' read -r src phase speed_in speed_out elapsed wait_el ping_ms active_n < <(
  jq -r --arg s "$sid" '
    (.sessions[$s].pid // 0) as $pid
    | (.pids[($pid|tostring)] // null) as $sess
    | (if $sess then "session" else "agg" end) as $src
    | (if $sess then $sess else .aggregate end) as $d
    | [$src,
       $d.phase // "idle",
       ($d.vis_in  // $d.speed_in  // 0),
       ($d.vis_out // $d.speed_out // 0),
       $d.elapsed // 0,
       $d.wait_elapsed // 0,
       .ping_ms // "",
       .active_sessions // 0]
    | @tsv
  ' "$f" 2>/dev/null
)

fmt() {
  local v=${1%.*}
  [ -z "$v" ] && v=0
  if   [ "$v" -lt 1024 ];    then printf "%dB/s" "$v"
  elif [ "$v" -lt 1048576 ]; then printf "%dK/s" $((v / 1024))
  else                            awk -v x="$v" 'BEGIN{printf "%.1fM/s", x/1048576}'
  fi
}

ping_frag=""
if [ -n "$ping_ms" ]; then
  pi=${ping_ms%.*}
  [ -z "$pi" ] && pi=0
  if   [ "$pi" -lt 100 ]; then pc="$green"
  elif [ "$pi" -lt 300 ]; then pc="$yellow"
  else                          pc="$red"
  fi
  if awk -v x="$ping_ms" 'BEGIN{exit !(x<10)}'; then
    ping_disp=$(awk -v x="$ping_ms" 'BEGIN{printf "%.1f", x}')
  else
    ping_disp="$pi"
  fi
  ping_frag="${pc}●${reset} ${dim}${ping_disp}ms${reset}"
else
  ping_frag="${red}●${reset} ${dim}offline${reset}"
fi

multi=""
if [ "$src" = "agg" ] && [ -n "$active_n" ] && [ "$active_n" -gt 1 ] 2>/dev/null; then
  multi=" ${dim}×${active_n}${reset}"
fi

# Display is driven by held speeds (vis_in/vis_out) so bursty traffic stays
# visible across slow statusline refreshes. Only when both held speeds are
# zero do we fall back to phase metadata.
phase_frag=""
si=${speed_in%.*};  [ -z "$si" ] && si=0
so=${speed_out%.*}; [ -z "$so" ] && so=0

if [ "$so" -gt 0 ] 2>/dev/null || [ "$si" -gt 0 ] 2>/dev/null; then
  up_part=""
  down_part=""
  [ "$so" -gt 0 ] && up_part="${blue}⬆${reset} ${dim}$(fmt "$so")${reset}"
  [ "$si" -gt 0 ] && down_part="${cyan}⬇${reset} ${dim}$(fmt "$si")${reset}"
  if [ -n "$up_part" ] && [ -n "$down_part" ]; then
    phase_frag="${up_part}   ${down_part}"
  else
    phase_frag="${up_part}${down_part}"
  fi
else
  case "$phase" in
    waiting)
      w=$(awk -v x="$wait_el" 'BEGIN{printf "%.1f", x}')
      phase_frag="${yellow}⏳${reset} ${dim}${w}s${reset}"
      ;;
    active)
      e=${elapsed%.*}
      [ -z "$e" ] && e=0
      if   [ "$e" -lt 60 ];   then disp="${e}s"
      elif [ "$e" -lt 3600 ]; then disp="$((e/60))m"
      else                         disp="$((e/3600))h"
      fi
      phase_frag="${dim}…${disp}${reset}"
      ;;
    off)
      phase_frag="${dim}net-off${reset}"
      ;;
    *)
      phase_frag=""
      ;;
  esac
fi

if [ -n "$phase_frag" ]; then
  printf "   %s%s   %s" "$phase_frag" "$multi" "$ping_frag"
else
  printf "   %s%s" "$ping_frag" "$multi"
fi
