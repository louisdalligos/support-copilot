# A budget so the bill never surprises you, and alarms that repair and report a sick instance.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Count usage before credits, so the alert fires on real spend while credits are paying for it.
  cost_types {
    include_credit = false
    include_refund = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# AWS hardware or network problem: move the instance to healthy hardware (same IP and disk).
resource "aws_cloudwatch_metric_alarm" "system_check" {
  alarm_name          = "${var.name}-system-check"
  alarm_description   = "AWS-side problem with the instance; recovering it."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  dimensions          = { InstanceId = aws_instance.host.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = ["arn:${data.aws_partition.current.partition}:automate:${var.region}:ec2:recover", aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# The operating system stopped responding: reboot it.
resource "aws_cloudwatch_metric_alarm" "instance_check" {
  alarm_name          = "${var.name}-instance-check"
  alarm_description   = "The instance stopped responding; rebooting it."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  dimensions          = { InstanceId = aws_instance.host.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = ["arn:${data.aws_partition.current.partition}:automate:${var.region}:ec2:reboot", aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# With standard CPU credits the instance slows down when the balance reaches zero.
resource "aws_cloudwatch_metric_alarm" "cpu_credits" {
  count = var.cpu_credits == "standard" ? 1 : 0

  alarm_name          = "${var.name}-cpu-credits-low"
  alarm_description   = "CPU credits are running out; the instance will slow down. Consider a bigger instance."
  namespace           = "AWS/EC2"
  metric_name         = "CPUCreditBalance"
  dimensions          = { InstanceId = aws_instance.host.id }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 20
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
