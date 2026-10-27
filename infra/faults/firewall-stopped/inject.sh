#!/bin/bash
# Firewall stopped in one AZ: stop fw-a. The AZ-a TGW subnet still routes to its ENI,
# so flows through AZ a are blackholed while AZ b stays healthy.
# Expect fw-a-silent, failing flows through AZ a, and a CloudTrail StopInstances.
source "$(dirname "$0")/../lib.sh"
require_inspection
ID=$(t .firewall_instance_ids.a)
state=$(aws ec2 describe-instances --instance-ids "$ID" --query 'Reservations[0].Instances[0].State.Name' --output text)
if [ "$state" != running ] && [ "$state" != pending ]; then report already "fw-a $ID is $state"; exit 0; fi
aws ec2 stop-instances --instance-ids "$ID" >/dev/null
report changed "stopped fw-a $ID"
