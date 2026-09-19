variable "name_prefix" {
  type = string
}

variable "lambda_src_dir" {
  description = "Path to the lambda/src directory. Packaged as-is; endpoint configs are not bundled here -- the Lambda reads them from SSM at runtime (see the scheduler module)."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "output_bucket_name" {
  type = string
}

variable "output_bucket_arn" {
  type = string
}

variable "ssm_config_prefix" {
  type = string
}

variable "security_token_parameter_name" {
  description = "Name of the SecureString SSM parameter holding the ENTSO-E API security token. Created outside Terraform, so only the name is referenced here -- the value never enters state."
  type        = string
}

variable "timeout_seconds" {
  type = number
}

variable "memory_mb" {
  type = number
}

variable "log_retention_days" {
  type = number
}

variable "application_log_level" {
  description = "Level for logs emitted by the function's own code, applied by Lambda's advanced logging controls."
  type        = string
}

variable "system_log_level" {
  description = "Level for logs emitted by the Lambda runtime itself."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
