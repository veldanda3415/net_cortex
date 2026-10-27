#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_inspection
ATT=$(t .security_attachment_id)
mode=$(aws ec2 describe-transit-gateway-vpc-attachments --transit-gateway-attachment-ids "$ATT" \
  --query 'TransitGatewayVpcAttachments[0].Options.ApplianceModeSupport' --output text)
if [ "$mode" = enable ]; then report already "appliance mode already on on $ATT"; exit 0; fi
aws ec2 modify-transit-gateway-vpc-attachment --transit-gateway-attachment-id "$ATT" --options ApplianceModeSupport=enable >/dev/null
report changed "appliance mode on on $ATT"
