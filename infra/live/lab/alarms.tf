# Baseline alarms (spec §7 PR 4). No actions: they are state for the dashboard and
# for NetCortex to read ("what was alarming, and since when"), not pages.
# Every threshold is "anything other than the healthy steady state", because the
# lab's healthy state is exact: tunnels up, 0 drops, 100% success.

locals {
  bgp_neighbors = [for c in var.tunnel_inside_cidrs : cidrhost(c, 1)]

  alarms = merge(
    {
      for i, ip in module.vpn.tunnel_outside_ips : "vpn-tunnel${i + 1}-down" => {
        description = "VPN tunnel ${i + 1} (${ip}) is down."
        namespace   = "AWS/VPN"
        metric      = "TunnelState"
        dimensions  = { TunnelIpAddress = ip }
        statistic   = "Maximum"
        comparison  = "LessThanThreshold"
        threshold   = 1
        missing     = "breaching"
      }
    },
    {
      for m in ["PacketDropCountBlackhole", "PacketDropCountNoRoute"] : "tgw-${lower(trimprefix(m, "PacketDropCount"))}-drops" => {
        description = "The TGW dropped packets (${m})."
        namespace   = "AWS/TransitGateway"
        metric      = m
        dimensions  = { TransitGateway = module.tgw.id }
        statistic   = "Sum"
        comparison  = "GreaterThanThreshold"
        threshold   = 0
        missing     = "notBreaching"
      }
    },
    {
      for m in ["ErrorPortAllocation", "PacketsDropCount"] : "nat-${lower(m)}" => {
        description = "The NAT gateway reports ${m}."
        namespace   = "AWS/NATGateway"
        metric      = m
        dimensions  = { NatGatewayId = module.security_vpc.nat_gateway_id }
        statistic   = "Sum"
        comparison  = "GreaterThanThreshold"
        threshold   = 0
        missing     = "notBreaching"
      }
    },
    {
      "nlb-unhealthy-targets" = {
        description = "At least one app target behind the NLB is unhealthy."
        namespace   = "AWS/NetworkELB"
        metric      = "UnHealthyHostCount"
        dimensions  = { TargetGroup = aws_lb_target_group.app.arn_suffix, LoadBalancer = aws_lb.app.arn_suffix }
        statistic   = "Maximum"
        comparison  = "GreaterThanThreshold"
        threshold   = 0
        missing     = "notBreaching"
      }
    },
    {
      # Missing data breaches: no metric at all means the router or its timer is gone.
      for n in local.bgp_neighbors : "bgp-${n}-down" => {
        description = "The router's BGP session to ${n} is not Established."
        namespace   = module.onprem_router.metric_namespace
        metric      = "BgpPeerState"
        dimensions  = { Neighbor = n }
        statistic   = "Minimum"
        comparison  = "LessThanThreshold"
        threshold   = 1
        missing     = "breaching"
      }
    },
    {
      # Missing data is left as-is: agents publish nothing until they finish booting.
      for f in concat(local.http_flows, [local.iperf_flow]) : "flow-${f}-degraded" => {
        description = "Synthetic flow ${f} is below 100% success."
        namespace   = local.traffic_namespace
        metric      = "SuccessRate"
        dimensions  = { Flow = f }
        statistic   = "Minimum"
        comparison  = "LessThanThreshold"
        threshold   = 100
        missing     = "missing"
      }
    },
  )
}

# Two of three one-minute periods: one dropped probe is noise, a fault isn't.
resource "aws_cloudwatch_metric_alarm" "baseline" {
  for_each = local.alarms

  alarm_name          = "netcortex-lab-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = each.value.statistic
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  treat_missing_data  = each.value.missing
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
}
