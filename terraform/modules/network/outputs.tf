output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "public_subnet_id" {
  value = aws_subnet.public.id
}

output "fck_nat_instance_public_ip" {
  description = "Public IP of the fck-nat instance (non-HA mode only; null in HA mode)."
  value       = module.fck_nat.instance_public_ip
}
