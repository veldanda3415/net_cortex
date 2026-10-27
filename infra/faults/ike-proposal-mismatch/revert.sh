#!/bin/bash
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/vpn.sh"
if [ "$(tunnel_dh_groups)" = 20 ]; then report already "tunnel 1 phase 1 already DH 20"; exit 0; fi
modify_tunnel '{"Phase1DHGroupNumbers":[{"Value":20}]}'
report changed "tunnel 1 phase 1 DH groups [20]"
