output "output_bucket_name" {
  description = "S3 bucket holding the scraped CSVs and raw JSON responses."
  value       = module.storage.bucket_id
}

output "lambda_function_name" {
  value = module.lambda.function_name
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "fck_nat_instance_public_ip" {
  value = module.network.fck_nat_instance_public_ip
}

output "security_token_parameter_name" {
  description = "SecureString SSM parameter the Lambda reads the ENTSO-E API token from. Terraform grants access to it but does not create it; see the root README."
  value       = local.security_token_parameter
}

output "scraped_endpoint_names" {
  description = "Endpoint configs discovered under lambda/config/endpoints, each with its own SSM parameter + EventBridge schedule."
  value       = module.scheduler.endpoint_names
}
