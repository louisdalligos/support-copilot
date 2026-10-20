# One VPC with one public subnet. No NAT gateway (it would cost more than the instance):
# the instance has a public IP, and the security group lets in only web traffic.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false # the instance gets an Elastic IP instead

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# The VPC's default security group allows traffic between its members; take every rule away.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

# HTTP (for the Let's Encrypt challenge and the redirect) and HTTPS only. There is no SSH:
# you get a shell through SSM Session Manager, which needs no open port.
resource "aws_security_group" "host" {
  name        = "${var.name}-host"
  description = "Web traffic to Caddy"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name}-host" }
}

resource "aws_vpc_security_group_ingress_rule" "web" {
  for_each = {
    http  = { port = 80, protocol = "tcp" }
    https = { port = 443, protocol = "tcp" }
    http3 = { port = 443, protocol = "udp" }
  }

  security_group_id = aws_security_group.host.id
  description       = each.key
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = each.value.port
  to_port           = each.value.port
  ip_protocol       = each.value.protocol
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.host.id
  description       = "Packages, images, AWS APIs, Lets Encrypt, Anthropic"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
