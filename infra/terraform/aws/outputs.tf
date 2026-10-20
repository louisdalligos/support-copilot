output "app_url" {
  value = "https://${local.domain}"
}

output "phoenix_url" {
  value = "https://phoenix.${local.domain}"
}

output "public_ip" {
  value = aws_eip.host.public_ip
}

output "dns_records" {
  description = "Create these if you use your own domain and it is not in Route 53."
  value       = var.domain != "" && var.route53_zone_id == "" ? "A ${local.domain} -> ${aws_eip.host.public_ip}\nA phoenix.${local.domain} -> ${aws_eip.host.public_ip}" : "none needed"
}

output "admin_user" {
  value = var.admin_user
}

output "admin_password" {
  description = "Show it with: terraform output -raw admin_password"
  value       = random_password.admin.result
  sensitive   = true
}

output "instance_id" {
  value = aws_instance.host.id
}

output "shell" {
  description = "A shell on the instance, through Session Manager (needs the session-manager-plugin)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.host.id}"
}

output "parameter_prefix" {
  value = local.param_prefix
}

output "backup_bucket" {
  description = "Its name contains the account ID. Show it with: terraform output -raw backup_bucket"
  value       = aws_s3_bucket.main.bucket
  sensitive   = true
}

output "github_settings" {
  description = "GitHub repository settings for the deploy workflow (GitHub CLI commands). Account details go in as secrets, so they are masked in public logs."
  sensitive   = true
  value = join("\n", concat(
    [
      "gh variable set AWS_REGION --body \"${var.region}\"",
      "gh variable set APP_URL --body \"https://${local.domain}\"",
      "gh secret set AWS_INSTANCE_ID --body \"${aws_instance.host.id}\"",
      "gh secret set AWS_ECR_REPOSITORY --body \"${aws_ecr_repository.app.repository_url}\"",
      "gh secret set AWS_RELEASE_BUCKET --body \"${aws_s3_bucket.main.bucket}\"",
    ],
    local.github_oidc ? tolist(["gh secret set AWS_DEPLOY_ROLE_ARN --body \"${try(aws_iam_role.deploy[0].arn, "")}\""]) : tolist([
      "# Create an access key for the IAM user ${try(aws_iam_user.deploy[0].name, "")} (IAM console > Users > Security credentials),",
      "# then run these two and paste each value when asked:",
      "gh secret set AWS_ACCESS_KEY_ID",
      "gh secret set AWS_SECRET_ACCESS_KEY",
    ]),
  ))
}
