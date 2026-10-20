variable "region" {
  description = "AWS region. Singapore is the closest to Manila."
  type        = string
  default     = "ap-southeast-1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "support-copilot"
}

variable "environment" {
  description = "Environment name, used in tags and the SSM parameter path."
  type        = string
  default     = "prod"
}

variable "instance_type" {
  description = "An ARM (Graviton) instance type: the image is built for arm64. t4g.small (2 GiB) is free-tier eligible; t4g.medium (4 GiB) needs the Paid plan."
  type        = string
  default     = "t4g.small"

  validation {
    condition     = can(regex("^[a-z][0-9]+g[a-z]*\\.", var.instance_type))
    error_message = "Use a Graviton (ARM) type such as t4g.small, t4g.medium or m7g.medium."
  }
}

variable "cpu_credits" {
  description = "standard: the instance slows down when it runs out of CPU credits. unlimited: it keeps going and AWS bills the extra."
  type        = string
  default     = "standard"

  validation {
    condition     = contains(["standard", "unlimited"], var.cpu_credits)
    error_message = "cpu_credits must be standard or unlimited."
  }
}

variable "root_volume_gb" {
  description = "Size of the encrypted gp3 root disk."
  type        = number
  default     = 20
}

variable "domain" {
  description = "The app's domain, e.g. copilot.example.com. Leave empty to use <ip>.sslip.io, which needs no DNS setup."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "If your domain is in Route 53, its hosted zone ID: the DNS records are then created for you."
  type        = string
  default     = ""
}

variable "acme_email" {
  description = "Email for Let's Encrypt certificate notices."
  type        = string
}

variable "alert_email" {
  description = "Email for budget and health alerts. Confirm the subscription email AWS sends."
  type        = string
}

variable "admin_user" {
  description = "The sign-in name for the app and Phoenix. It is recorded as the reviewer on every decision."
  type        = string
  default     = "admin"
}

variable "copilot_model" {
  description = "oracle is free and works on the labeled eval tickets; claude needs the ANTHROPIC_API_KEY parameter."
  type        = string
  default     = "oracle"

  validation {
    condition     = contains(["oracle", "claude"], var.copilot_model)
    error_message = "copilot_model must be oracle or claude."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly budget for the whole account; you get an email at 50% spent and when the forecast passes 100%."
  type        = number
  default     = 20
}

variable "backup_retention_days" {
  description = "How long database backups are kept in S3."
  type        = number
  default     = 30
}

variable "github_repository" {
  description = "owner/name of the GitHub repository allowed to deploy."
  type        = string
  default     = "louisdalligos/support-copilot"
}

variable "github_environment" {
  description = "The GitHub environment the deploy job runs in. Only that environment can assume the deploy role."
  type        = string
  default     = "production"
}

variable "github_auth" {
  description = "How GitHub Actions signs in to deploy: oidc, oidc-existing (the account already has the GitHub OIDC provider) or access-key. See github.tf."
  type        = string
  default     = "oidc"

  validation {
    condition     = contains(["oidc", "oidc-existing", "access-key"], var.github_auth)
    error_message = "github_auth must be oidc, oidc-existing or access-key."
  }
}

variable "force_destroy" {
  description = "Let terraform destroy delete the backup bucket and image repository even when they are not empty."
  type        = bool
  default     = false
}
