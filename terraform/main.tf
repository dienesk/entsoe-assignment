locals {
  name_prefix       = "${var.project}-${var.environment}"
  ssm_config_prefix = "/${var.project}/${var.environment}/entsoe/endpoints"
  lambda_dir        = "${path.module}/../lambda"

  common_tags = {
    Project     = var.project
    Environment = var.environment
  }
}

module "network" {
  source = "./modules/network"

  name_prefix           = local.name_prefix
  vpc_cidr              = var.vpc_cidr
  availability_zones    = var.availability_zones
  fck_nat_ha_mode       = var.fck_nat_ha_mode
  fck_nat_instance_type = var.fck_nat_instance_type
  tags                  = local.common_tags
}

module "storage" {
  source = "./modules/storage"

  name_prefix              = local.name_prefix
  raw_json_expiration_days = var.s3_raw_json_expiration_days
  tags                     = local.common_tags
}

module "lambda" {
  source = "./modules/lambda"

  name_prefix        = local.name_prefix
  lambda_src_dir     = "${local.lambda_dir}/src"
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  output_bucket_name = module.storage.bucket_id
  output_bucket_arn  = module.storage.bucket_arn
  ssm_config_prefix  = local.ssm_config_prefix
  timeout_seconds    = var.lambda_timeout_seconds
  memory_mb          = var.lambda_memory_mb
  log_retention_days = var.lambda_log_retention_days
  tags               = local.common_tags
}

module "scheduler" {
  source = "./modules/scheduler"

  name_prefix                 = local.name_prefix
  config_dir                  = "${local.lambda_dir}/config/endpoints"
  ssm_config_prefix           = local.ssm_config_prefix
  lambda_function_name        = module.lambda.function_name
  lambda_function_arn         = module.lambda.function_arn
  default_schedule_expression = var.default_schedule_expression
  endpoint_schedules          = var.endpoint_schedules
  tags                        = local.common_tags
}
