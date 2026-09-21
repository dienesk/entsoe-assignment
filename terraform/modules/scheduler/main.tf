# Turns every lambda/config/endpoints/*.json file into:
#   1. an SSM parameter the Lambda reads at invocation time, and
#   2. an EventBridge schedule that invokes the Lambda for just that endpoint.
#
# This is the mechanism behind "reusable for other endpoints by changing
# configuration": adding a new scraped endpoint is dropping a new JSON file
# next to the existing two and running `terraform apply` -- no Terraform or
# Python code changes required for the common case. A custom schedule can be
# set via var.endpoint_schedules; otherwise var.default_schedule_expression
# applies.

locals {
  config_files = fileset(var.config_dir, "*.json")
  endpoint_configs = {
    for f in local.config_files :
    trimsuffix(f, ".json") => jsondecode(file("${var.config_dir}/${f}"))
  }
}

resource "aws_ssm_parameter" "endpoint_config" {
  for_each = local.endpoint_configs

  name        = "${var.ssm_config_prefix}/${each.key}"
  description = try(each.value.description, "ENTSO-E scraper endpoint config for ${each.key}")
  type        = "String"
  value       = jsonencode(each.value)

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "endpoint_schedule" {
  for_each = local.endpoint_configs

  name                = "${var.name_prefix}-${each.key}"
  description         = "Triggers the ${var.name_prefix} scraper for the '${each.key}' endpoint"
  schedule_expression = lookup(var.endpoint_schedules, each.key, var.default_schedule_expression)

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "endpoint_schedule" {
  for_each = local.endpoint_configs

  rule      = aws_cloudwatch_event_rule.endpoint_schedule[each.key].name
  target_id = "${var.name_prefix}-${each.key}"
  arn       = var.lambda_function_arn

  input = jsonencode({ endpoint_name = each.key })
}

resource "aws_lambda_permission" "allow_eventbridge" {
  for_each = local.endpoint_configs

  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.endpoint_schedule[each.key].arn
}
