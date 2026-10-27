locals {
  default_tags = {
    Project   = "netcortex-lab"
    Ephemeral = "true"
    Owner     = var.owner
    ManagedBy = "terraform"
    Stack     = "lab"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.default_tags
  }
}

# Network Manager's home region: global networks live here and its events are
# delivered to EventBridge here, whatever region the TGW is in.
provider "aws" {
  alias  = "network_manager_home"
  region = "us-west-2"

  default_tags {
    tags = local.default_tags
  }
}
