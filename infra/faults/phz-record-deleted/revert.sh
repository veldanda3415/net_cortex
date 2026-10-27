#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/dns.sh"
if record_exists; then report already "$NAME present"; exit 0; fi
change_alias UPSERT
report changed "restored $NAME -> NLB"
