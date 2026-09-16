variable "name_prefix" {
  type = string
}

variable "config_dir" {
  description = "Path to lambda/config/endpoints. Every *.json file here becomes one SSM parameter plus one scheduled trigger -- this is the whole 'add an endpoint by configuration' mechanism."
  type        = string
}

variable "ssm_config_prefix" {
  type = string
}

variable "lambda_function_name" {
  type = string
}

variable "lambda_function_arn" {
  type = string
}

variable "default_schedule_expression" {
  type = string
}

variable "endpoint_schedules" {
  type    = map(string)
  default = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
