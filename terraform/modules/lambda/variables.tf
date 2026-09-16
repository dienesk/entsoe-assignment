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

variable "timeout_seconds" {
  type = number
}

variable "memory_mb" {
  type = number
}

variable "log_retention_days" {
  type = number
}

variable "tags" {
  type    = map(string)
  default = {}
}
