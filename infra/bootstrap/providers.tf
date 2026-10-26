provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "netcortex-lab"
      Owner     = var.owner
      ManagedBy = "terraform"
      # Bootstrap is the persistent root; the nightly reaper must never treat it as lab debris.
      Ephemeral = "false"
      Stack     = "bootstrap"
    }
  }
}
