variable "name_prefix" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "availability_zones" {
  description = "AZs for the private subnets. The public (NAT) subnet is placed in the first AZ."
  type        = list(string)
}

variable "fck_nat_ha_mode" {
  type = bool
}

variable "fck_nat_instance_type" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
