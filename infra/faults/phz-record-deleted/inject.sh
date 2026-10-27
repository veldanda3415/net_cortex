#!/bin/bash
# PHZ record deleted: remove app.lab.internal. svc resolves it every 5s, so expect an
# NXDOMAIN spike in the Resolver query logs, svc-to-app failing (on-prem uses the NLB
# name and is unaffected), and a CloudTrail ChangeResourceRecordSets.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/dns.sh"
if ! record_exists; then report already "$NAME already absent"; exit 0; fi
change_alias DELETE
report changed "deleted $NAME"
