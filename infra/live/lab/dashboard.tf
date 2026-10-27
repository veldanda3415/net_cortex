# One page that answers "is the lab healthy?" (the PR 3 exit test): every flow at
# 100% success, both NLB targets healthy, tunnel 1 up.

locals {
  traffic_ns = local.traffic_namespace

  dashboard_widgets = [
    {
      type = "metric", x = 0, y = 0, width = 12, height = 6
      properties = {
        title   = "Flow success rate (%)"
        region  = var.region
        view    = "timeSeries"
        stat    = "Average"
        period  = 60
        metrics = [for f in concat(local.http_flows, [local.iperf_flow]) : [local.traffic_ns, "SuccessRate", "Flow", f]]
        yAxis   = { left = { min = 0, max = 100 } }
        annotations = {
          horizontal = [{ label = "healthy", value = 100, color = "#2ca02c" }]
        }
      }
    },
    {
      type = "metric", x = 12, y = 0, width = 12, height = 6
      properties = {
        title   = "HTTP latency (ms, average of successes)"
        region  = var.region
        view    = "timeSeries"
        stat    = "Average"
        period  = 60
        metrics = [for f in local.http_flows : [local.traffic_ns, "LatencyMs", "Flow", f] if f != "onprem-to-partner"]
        yAxis   = { left = { min = 0 } }
      }
    },
    {
      type = "metric", x = 0, y = 6, width = 12, height = 6
      properties = {
        title  = "iperf3 on-prem -> app-a (UDP, 2 Mbps target)"
        region = var.region
        view   = "timeSeries"
        stat   = "Average"
        period = 60
        metrics = [
          [local.traffic_ns, "ThroughputMbps", "Flow", local.iperf_flow],
          [local.traffic_ns, "LossPercent", "Flow", local.iperf_flow, { yAxis = "right" }],
          [local.traffic_ns, "JitterMs", "Flow", local.iperf_flow, { yAxis = "right" }],
        ]
        yAxis = { left = { min = 0 }, right = { min = 0 } }
      }
    },
    {
      type = "metric", x = 12, y = 6, width = 6, height = 6
      properties = {
        title  = "NLB targets"
        region = var.region
        view   = "timeSeries"
        stat   = "Minimum"
        period = 60
        metrics = [
          ["AWS/NetworkELB", "HealthyHostCount", "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix, { color = "#2ca02c" }],
          ["AWS/NetworkELB", "UnHealthyHostCount", "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix, { color = "#d62728", stat = "Maximum" }],
        ]
        yAxis = { left = { min = 0, max = 2 } }
      }
    },
    {
      type = "metric", x = 18, y = 6, width = 6, height = 6
      properties = {
        title  = "VPN tunnel state (1 = up)"
        region = var.region
        view   = "timeSeries"
        stat   = "Minimum"
        period = 60
        metrics = [
          for i, ip in module.vpn.tunnel_outside_ips :
          ["AWS/VPN", "TunnelState", "TunnelIpAddress", ip, { label = "tunnel ${i + 1}" }]
        ]
        yAxis = { left = { min = 0, max = 1 } }
      }
    },
    {
      type = "metric", x = 0, y = 12, width = 12, height = 6
      properties = {
        title  = "Router BGP (state 1 = Established; prefixes received)"
        region = var.region
        view   = "timeSeries"
        stat   = "Minimum"
        period = 60
        metrics = concat([
          for n in local.bgp_neighbors : [
            [module.onprem_router.metric_namespace, "BgpPeerState", "Neighbor", n, { label = "${n} state" }],
            [module.onprem_router.metric_namespace, "PrefixesReceived", "Neighbor", n, { label = "${n} prefixes", yAxis = "right" }],
          ]
        ]...)
        yAxis = { left = { min = 0, max = 1 }, right = { min = 0 } }
      }
    },
    {
      type = "metric", x = 0, y = 18, width = 24, height = 6
      properties = {
        title  = "Inspection firewalls (tracked flows; dropped packets per minute)"
        region = var.region
        view   = "timeSeries"
        stat   = "Maximum"
        period = 60
        metrics = concat([
          for az in ["a", "b"] : [
            ["NetCortexLab/Firewall", "ConntrackCount", "Firewall", "fw-${az}", { label = "fw-${az} conntrack" }],
            ["NetCortexLab/Firewall", "ForwardDrops", "Firewall", "fw-${az}", { label = "fw-${az} drops", yAxis = "right", stat = "Sum" }],
          ]
        ]...)
        yAxis = { left = { min = 0 }, right = { min = 0 } }
      }
    },
    {
      type = "alarm", x = 12, y = 12, width = 12, height = 6
      properties = {
        title  = "Baseline alarms (all OK = healthy lab)"
        alarms = [for a in aws_cloudwatch_metric_alarm.baseline : a.arn]
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "lab" {
  dashboard_name = "netcortex-lab"
  dashboard_body = jsonencode({ widgets = local.dashboard_widgets })
}
