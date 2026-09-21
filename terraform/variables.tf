variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Short project name, used as a prefix for resource names."
  type        = string
  default     = "entsoe-scraper"
}

variable "environment" {
  description = "Deployment environment name (e.g. dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC created to host the Lambda and the fck-nat instance."
  type        = string
  default     = "10.42.0.0/16"
}

variable "availability_zones" {
  description = "Availability zones to spread the private (Lambda) subnets across. The NAT instance uses the first one."
  type        = list(string)
  default     = ["eu-central-1a", "eu-central-1b"]
}

variable "fck_nat_ha_mode" {
  description = "Whether to run fck-nat in high-availability (autoscaling group) mode. Off by default to keep this assignment's footprint cheap; a single NAT instance is a single point of failure but is sufficient for a scheduled batch scrape."
  type        = bool
  default     = false
}

variable "fck_nat_instance_type" {
  description = "EC2 instance type for the fck-nat NAT instance(s)."
  type        = string
  default     = "t4g.micro"
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout."
  type        = number
  default     = 60
}

variable "lambda_memory_mb" {
  description = "Lambda function memory allocation."
  type        = number
  default     = 256
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda function's log group."
  type        = number
  default     = 30
}

variable "lambda_application_log_level" {
  description = "Level below which the function's own log records are dropped by Lambda rather than billed and stored. Set here rather than in code: AWS's guidance for JSON-formatted logs is to control the level through advanced logging controls, and a setLevel() call in the handler would override whatever is deployed."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"], var.lambda_application_log_level)
    error_message = "Must be one of TRACE, DEBUG, INFO, WARN, ERROR, FATAL."
  }
}

variable "lambda_system_log_level" {
  description = "Level for the Lambda runtime's own records (START/END/REPORT lines are always emitted regardless). WARN keeps routine runtime chatter out of the log group without hiding runtime problems."
  type        = string
  default     = "WARN"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARN"], var.lambda_system_log_level)
    error_message = "Must be one of DEBUG, INFO, WARN."
  }
}

variable "s3_raw_response_expiration_days" {
  description = "Days after which raw XML responses (kept for reprocessing) are expired from S3. The flattened CSVs are kept indefinitely."
  type        = number
  default     = 90
}

variable "security_token_parameter_name" {
  description = "Name of the SecureString SSM parameter holding the ENTSO-E API security token. Defaults to /<project>/<environment>/entsoe/security-token. Terraform never reads or writes its value -- create it once out of band (see the root README) so the credential stays out of terraform.tfstate."
  type        = string
  default     = null
}

variable "default_schedule_expression" {
  description = "EventBridge schedule expression applied to any discovered endpoint config that isn't listed in endpoint_schedules."
  type        = string
  default     = "cron(0 5 * * ? *)" # 05:00 UTC daily
}

variable "endpoint_schedules" {
  description = "Per-endpoint EventBridge schedule overrides, keyed by endpoint_name (the filename, minus .json, of lambda/config/endpoints/*.json)."
  type        = map(string)
  default = {
    generation_forecast_day_ahead = "cron(0 17 * * ? *)" # 17:00 UTC: after day-ahead auctions close
  }
}
