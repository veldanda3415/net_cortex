#!/bin/bash
# IKE proposal mismatch: restrict tunnel 1's AWS side to DH group 21 while the router
# only offers group 20. The modification replaces the tunnel (a few minutes), then
# IKE fails with NO_PROPOSAL_CHOSEN. Expect a CloudTrail ModifyVpnTunnelOptions and
# IPsec DOWN in the tunnel activity log.
source "$(dirname "$0")/../lib.sh"
. "$FAULTS_DIR/vpn.sh"
if [ "$(tunnel_dh_groups)" = 21 ]; then report already "tunnel 1 phase 1 already DH 21"; exit 0; fi
modify_tunnel '{"Phase1DHGroupNumbers":[{"Value":21}]}'
report changed "tunnel 1 phase 1 DH groups [21] (router offers 20)"
