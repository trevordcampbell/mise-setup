#!/bin/sh
# Forward flags parsed by Mise across the nested mise exec boundary.
set -eu
if [ "${usage_verbose:-false}" = true ]; then set -- "$@" --verbose; fi
if [ "${usage_json:-false}" = true ]; then set -- "$@" --json; fi
if [ -n "${usage_tool+x}" ]; then set -- "$@" "--tool=$usage_tool"; fi
if [ -n "${usage_limit+x}" ]; then set -- "$@" "--limit=$usage_limit"; fi
if [ -n "${usage_review+x}" ]; then set -- "$@" "--review=$usage_review"; fi
if [ -n "${usage_assessment+x}" ]; then set -- "$@" "--assessment=$usage_assessment"; fi
if [ -n "${usage_reason+x}" ]; then set -- "$@" "--reason=$usage_reason"; fi
exec "$HOME/.local/bin/mise" -C "$HOME" exec -- python "$HOME/.config/mise/scripts/maintenance.py" "$@"
