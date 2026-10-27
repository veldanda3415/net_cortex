#!/bin/bash
source "$(dirname "$0")/../lib.sh"
require_inspection
ID=$(t .firewall_instance_ids.a)
state=$(aws ec2 describe-instances --instance-ids "$ID" --query 'Reservations[0].Instances[0].State.Name' --output text)
if [ "$state" = running ] || [ "$state" = pending ]; then report already "fw-a $ID is $state"; exit 0; fi
aws ec2 wait instance-stopped --instance-ids "$ID"
aws ec2 start-instances --instance-ids "$ID" >/dev/null
aws ec2 wait instance-running --instance-ids "$ID"
report changed "started fw-a $ID (its ruleset reloads at boot)"
