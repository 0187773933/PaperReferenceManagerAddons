#!/usr/bin/env bash
# Ask the running prma server whether papers are in the library.
#
#   Exists.sh 10.1111/j.2517-6161.1995.tb02031.x
#   Exists.sh "Controlling the false discovery rate" 10.1038/nature12373
#
# Each argument is one query : anything starting with "10." is sent as a DOI ,
# everything else as a title. Host/port via SERVER_HOST / SERVER_PORT.
# Exit 0 if every query was found , 1 otherwise. Needs curl + jq.
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $(basename "$0") <doi-or-title> [...]" >&2; exit 2; }

url="http://${SERVER_HOST:-127.0.0.1}:${SERVER_PORT:-9371}/exists"

body=$( jq -n '$ARGS.positional | to_entries
	| map( if .value | startswith( "10." )
	       then { id: .key , doi: .value , title: "" }
	       else { id: .key , doi: "" , title: .value } end )
	| { queries: . }' --args "$@" )

out=$( curl -sS -X POST "$url" -H 'Content-Type: application/json' -d "$body" \
	| jq -r 'if .error then error( .error ) else
	         .results[] | ( if .exists then "yes  " else "no   " end )
	                    + ( if .doi != "" then .doi else .title end ) end' )

echo "$out"
! grep -q '^no ' <<< "$out"
