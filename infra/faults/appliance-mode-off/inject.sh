#!/bin/bash
# Appliance mode off on the security attachment: the TGW may send the two directions
# of a flow to firewalls in different AZs. Expect intermittent drops of NEW/INVALID
# packets in the firewall drop log (asymmetric routing through stateful firewalls).
source "$(dirname "$0")/../lib.sh"
require_inspection
ATT=$(t .security_attachment_id)
mode=$(aws ec2 describe-transit-gateway-vpc-attachments --transit-gateway-attachment-ids "$ATT" \
  --query 'TransitGatewayVpcAttachments[0].Options.ApplianceModeSupport' --output text)
if [ "$mode" = disable ]; then report already "appliance mode already off on $ATT"; exit 0; fi
aws ec2 modify-transit-gateway-vpc-attachment --transit-gateway-attachment-id "$ATT" --options ApplianceModeSupport=disable >/dev/null
report changed "appliance mode off on $ATT"
