# How GitHub Actions signs in to AWS to deploy.
#
#   github_auth = "oidc"           short-lived credentials from GitHub's OIDC provider; no AWS keys in GitHub.
#                                  Only jobs in the repository's `production` environment can assume the role.
#   github_auth = "oidc-existing"  the same, when the account already has the GitHub OIDC provider.
#   github_auth = "access-key"     an IAM user limited to the same deploy permissions, whose access key is
#                                  stored as a GitHub secret. For accounts that cannot create OIDC providers
#                                  (AWS's managed "projects" accounts deny iam:*Provider*).

locals {
  github_oidc     = var.github_auth != "access-key"
  github_oidc_url = "https://token.actions.githubusercontent.com"
  github_oidc_arn = try(aws_iam_openid_connect_provider.github[0].arn, data.aws_iam_openid_connect_provider.github[0].arn, "")
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_auth == "oidc" ? 1 : 0
  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.github_auth == "oidc-existing" ? 1 : 0
  url   = local.github_oidc_url
}

data "aws_iam_policy_document" "deploy_trust" {
  count = local.github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  count                = local.github_oidc ? 1 : 0
  name                 = "${var.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust[0].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  count  = local.github_oidc ? 1 : 0
  name   = "${var.name}-github-deploy"
  role   = aws_iam_role.deploy[0].id
  policy = data.aws_iam_policy_document.deploy.json
}

# access-key mode: create the user's access key yourself (IAM console) and store it as GitHub secrets,
# so the secret never passes through Terraform or its state.
resource "aws_iam_user" "deploy" {
  count = local.github_oidc ? 0 : 1
  name  = "${var.name}-github-deploy"
}

resource "aws_iam_user_policy" "deploy" {
  count  = local.github_oidc ? 0 : 1
  name   = "${var.name}-github-deploy"
  user   = aws_iam_user.deploy[0].name
  policy = data.aws_iam_policy_document.deploy.json
}

# Push one image, upload one release bundle, and run the deploy command on one instance.
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid       = "UploadRelease"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.main.arn}/releases/*"]
  }

  statement {
    sid     = "RunDeployCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.host.arn,
      "arn:${data.aws_partition.current.partition}:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "ReadCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"] # these actions do not support resource-level permissions
  }
}
