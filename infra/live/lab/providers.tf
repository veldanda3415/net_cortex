provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "netcortex-lab"
      Ephemeral = "true"
      Owner     = var.owner
      ManagedBy = "terraform"
      Stack     = "lab"
    }
  }
}
