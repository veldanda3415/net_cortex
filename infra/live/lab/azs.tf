data "aws_availability_zone" "lab" {
  for_each = toset(var.az_ids)

  zone_id = each.value
}

locals {
  # [a, b] AZ names resolved from the pinned AZ IDs.
  az_names = [for id in var.az_ids : data.aws_availability_zone.lab[id].name]
}
