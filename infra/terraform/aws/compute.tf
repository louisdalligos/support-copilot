# One Graviton (ARM) instance running Docker Compose: Caddy, the app, Postgres, Phoenix, backups.

# Canonical publishes the current Ubuntu image IDs in Parameter Store.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

resource "aws_instance" "host" {
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region        = var.region
    param_prefix  = local.param_prefix
    bucket        = aws_s3_bucket.main.bucket
    ecr_registry  = split("/", aws_ecr_repository.app.repository_url)[0]
    swap_gib      = 2
    project_dir   = "/opt/${var.name}"
    project_name  = var.name
    backup_prefix = "backups/"
  })

  credit_specification {
    cpu_credits = var.cpu_credits
  }

  # IMDSv2 only, and a hop limit of 1 so containers cannot reach the instance's credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  tags = { Name = var.name }

  # A newer Ubuntu image or a changed boot script should not replace a running server.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  # The settings must exist before the first deploy reads them.
  depends_on = [aws_ssm_parameter.config, aws_ssm_parameter.secret]
}

# A fixed public IP, so DNS keeps working across stop and start.
resource "aws_eip" "host" {
  domain = "vpc"
  tags   = { Name = var.name }
}

resource "aws_eip_association" "host" {
  instance_id   = aws_instance.host.id
  allocation_id = aws_eip.host.id
}

resource "aws_route53_record" "app" {
  for_each = var.route53_zone_id != "" && var.domain != "" ? toset([var.domain, "phoenix.${var.domain}"]) : toset([])

  zone_id = var.route53_zone_id
  name    = each.key
  type    = "A"
  ttl     = 300
  records = [aws_eip.host.public_ip]
}

# The instance's role: Session Manager, its own settings, pulling the image, writing backups.
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name               = "${var.name}-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "host" {
  statement {
    sid       = "ReadSettings"
    actions   = ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.param_prefix}/*", "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.param_prefix}"]
  }

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "PullImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid       = "WriteBackups"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.main.arn}/backups/*"]
  }

  statement {
    sid       = "ReadReleasesAndBackups"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.main.arn}/releases/*", "${aws_s3_bucket.main.arn}/backups/*"]
  }

  statement {
    sid       = "ListForSync"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.main.arn]
  }
}

resource "aws_iam_role_policy" "host" {
  name   = "${var.name}-host"
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host.json
}

resource "aws_iam_instance_profile" "host" {
  name = "${var.name}-host"
  role = aws_iam_role.host.name
}
