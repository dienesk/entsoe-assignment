# Minimal VPC for the scraper: one public subnet hosting the fck-nat NAT
# instance, and one private subnet per AZ hosting the Lambda function's ENIs.
# Private subnets share a single route table whose default route points at
# the fck-nat instance's network interface.

locals {
  private_subnet_count = length(var.availability_zones)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

# --- Public subnet: hosts the fck-nat NAT instance -------------------------

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 0)
  availability_zone       = var.availability_zones[0]
  map_public_ip_on_launch = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- Private subnets: host the Lambda function's ENIs ----------------------

resource "aws_subnet" "private" {
  count = local.private_subnet_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 1)
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, { Name = "${var.name_prefix}-private-${count.index}" })
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-private" })
}

resource "aws_route_table_association" "private" {
  count = local.private_subnet_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- fck-nat: NAT instance for the private subnets' internet egress --------
# https://github.com/AndrewGuenther/fck-nat via its companion Terraform module.
# ha_mode=false runs a single instance (cheaper; fine for a scheduled batch
# job); update_route_tables wires the private route table's default route to
# the NAT instance's ENI automatically.

module "fck_nat" {
  source = "git::https://github.com/RaJiska/terraform-aws-fck-nat.git?ref=v1.6.1"

  name      = "${var.name_prefix}-fck-nat"
  vpc_id    = aws_vpc.this.id
  subnet_id = aws_subnet.public.id

  ha_mode       = var.fck_nat_ha_mode
  instance_type = var.fck_nat_instance_type

  update_route_tables = true
  route_tables_ids = {
    private = aws_route_table.private.id
  }

  tags = var.tags
}
