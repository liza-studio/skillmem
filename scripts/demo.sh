#!/usr/bin/env bash
# The 40-second demo: an agent stops repeating a mistake, and strength has to
# be earned. Runs against a throwaway database, so it is safe to run anywhere
# and every number on screen is real output, not a mock-up.
#
#   scripts/demo.sh            # play it in your terminal
#   scripts/demo.sh --record   # capture it for scripts/cast_to_svg.py
set -euo pipefail

DB="$(mktemp -d)/demo.db"
SM="${SKILLMEM_BIN:-skillmem}"
PAUSE="${DEMO_PAUSE:-1.2}"
RECORD=0
[[ "${1:-}" == "--record" ]] && { RECORD=1; PAUSE=0; }

say()  { printf '\033[90m# %s\033[0m\n' "$1"; }

# Echo the command the way a reader would type it — the absolute binary path
# and the throwaway --db are demo scaffolding, not part of the story.
# Long invocations wrap on flag boundaries, the way you would type them, so
# the rendered demo stays a readable width instead of one 300-column line.
show() {
  local cmd
  cmd="$(printf '%s ' "$@" | sed -e "s#$SM#skillmem#" -e "s# --db [^ ]*##")"
  if [[ ${#cmd} -le 84 ]]; then
    printf '\033[36m$ %s\033[0m\n' "${cmd% }"
  else
    printf '%s' "${cmd% }" | awk '{
      n = split($0, parts, / --/)
      printf "\033[36m$ %s \\\033[0m\n", parts[1]
      for (i = 2; i <= n; i++)
        printf "\033[36m    --%s%s\033[0m\n", parts[i], (i < n ? " \\" : "")
    }'
  fi
}
run()  { show "$@"; "$@"; echo; sleep "$PAUSE"; }

say "Session one. The agent just spent an hour on a bug. It writes down how."
run "$SM" --db "$DB" learn skill-sqlite-locked \
  --title "SQLite 'database is locked' under concurrent writers" \
  --trigger "sqlite3.OperationalError: database is locked in a worker" \
  --steps "Enable WAL (PRAGMA journal_mode=WAL) and set busy_timeout=5000" \
  --outcome success \
  --lessons "Default rollback journal serialises writers; WAL lets readers through"

say "Session two, fresh context. It knows nothing — except what it recalls."
run "$SM" --db "$DB" recall "worker crashes with database is locked" --limit 1 --no-reinforce

say "Did it actually help? Saying so is not evidence. Strength does not move."
run "$SM" --db "$DB" reinforce skill-sqlite-locked

say "The test passing is evidence. Now it moves."
run "$SM" --db "$DB" reinforce skill-sqlite-locked --evidence test_passed

say "And a rule that is rare by nature must not fade for being unused."
run "$SM" --db "$DB" pin skill-sqlite-locked

say "Other people's skill packs live here too, by the same rules."
show "$SM" --db "$DB" skills add DietrichGebert/ponytail
"$SM" --db "$DB" skills add DietrichGebert/ponytail | tail -2; echo; sleep "$PAUSE"
run "$SM" --db "$DB" skills ls

say "One database. Six agents. Nothing left the machine."
[[ $RECORD -eq 1 ]] || rm -rf "$(dirname "$DB")"
