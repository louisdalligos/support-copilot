# The app's settings in SSM Parameter Store, read on the instance at every deploy and
# written into .env. Plain settings are String parameters; passwords and keys are SecureString.
#
# Terraform generates the passwords. They are in the Terraform state, so keep the state private.
# The Anthropic API key is not managed here; add it yourself (docs/aws.md) so it never touches the state.

locals {
  param_prefix = "/${var.name}/${var.environment}"
  domain       = var.domain != "" ? var.domain : "${replace(aws_eip.host.public_ip, ".", "-")}.sslip.io"

  config = {
    DOMAIN        = local.domain
    ACME_EMAIL    = var.acme_email
    ADMIN_USER    = var.admin_user
    COPILOT_MODEL = var.copilot_model
    POSTGRES_USER = "copilot"
  }

  secrets = {
    POSTGRES_PASSWORD     = random_password.postgres.result
    COPILOT_REDACTION_KEY = random_password.redaction_key.result
    ADMIN_PASSWORD_HASH   = bcrypt(random_password.admin.result, 12)
  }
}

resource "random_password" "postgres" {
  length  = 40
  special = false # it goes into connection URLs
}

resource "random_password" "redaction_key" {
  length  = 48
  special = false
}

resource "random_password" "admin" {
  length  = 24
  special = false
}

resource "aws_ssm_parameter" "config" {
  for_each = local.config

  name  = "${local.param_prefix}/${each.key}"
  type  = "String"
  value = each.value
}

resource "aws_ssm_parameter" "secret" {
  # A literal list: for_each cannot take keys derived from sensitive values.
  for_each = toset(["POSTGRES_PASSWORD", "COPILOT_REDACTION_KEY", "ADMIN_PASSWORD_HASH"])

  name  = "${local.param_prefix}/${each.key}"
  type  = "SecureString"
  value = local.secrets[each.key]

  lifecycle {
    # bcrypt() salts the hash differently on every run; keep the first one.
    ignore_changes = [value]
  }
}
