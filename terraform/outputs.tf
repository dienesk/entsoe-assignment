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

output "scraped_endpoint_names" {
  description = "Endpoint configs discovered under lambda/config/endpoints, each with its own SSM parameter + EventBridge schedule."
  value       = module.scheduler.endpoint_names
}
