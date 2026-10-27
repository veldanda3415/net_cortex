# NetCortex lab lifecycle. Run from infra/: make plan | up | down | status
# Uses your local AWS SSO profile; the account ID is read at run time, never committed.

AWS_PROFILE ?= aws-veldanda-admin
export AWS_PROFILE

LAB      := live/lab
TF       := terraform -chdir=$(LAB)
ACCOUNT   = $(shell aws sts get-caller-identity --query Account --output text)
BUCKET    = netcortex-tfstate-$(ACCOUNT)
TF_ARGS  ?=

.PHONY: init plan up down status

init:
	$(TF) init -input=false -backend-config="bucket=$(BUCKET)"

plan: init
	$(TF) plan -input=false -out=tfplan $(TF_ARGS)

# Applies the plan saved by `make plan` -- review it first.
up:
	$(TF) apply -input=false tfplan
	@rm -f $(LAB)/tfplan

down: init
	$(TF) destroy -input=false $(TF_ARGS)

status: init
	@echo "Resources in lab state: $$($(TF) state list | wc -l)"
	@aws ec2 describe-instances --region us-east-1 \
	  --filters Name=tag:Project,Values=netcortex-lab Name=instance-state-name,Values=pending,running \
	  --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId,State.Name]' --output table
	@aws ec2 describe-nat-gateways --region us-east-1 \
	  --filter Name=tag:Project,Values=netcortex-lab Name=state,Values=pending,available \
	  --query 'NatGateways[].[NatGatewayId,State]' --output table
