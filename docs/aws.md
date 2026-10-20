# Deploying to AWS

The production stack from [deploy.md](deploy.md) (the approval app, Postgres, Phoenix, Caddy and backups in Docker Compose) runs on one Graviton EC2 instance. Terraform creates everything, and GitHub Actions deploys every commit that passes CI on `main`.

```
                     ┌──────────────────────── VPC 10.42.0.0/16, one public subnet ────────────────────────┐
internet ──80/443──> │ EC2 t4g.small (ARM, Ubuntu 24.04), Elastic IP                                       │
                     │   Caddy ──> app ──> Postgres        Phoenix        backup (pg_dump every 24 h)       │
                     └──────────────────────────────────────────────────────────────────────────────────────┘
   GitHub Actions ──OIDC──> IAM role ──> ECR (image)   S3 (release bundle)   SSM Run Command (deploy.sh)
   instance role ──> SSM Parameter Store (settings, secrets)   ECR (pull)   S3 (backups, every 6 h)
   AWS Budgets + CloudWatch alarms ──> email
```

| What | How | Why |
| --- | --- | --- |
| Infrastructure | Terraform, in `infra/terraform/aws` | Reviewable, repeatable, and `terraform destroy` removes it all |
| Compute | One `t4g.small` (2 vCPU, 2 GiB, ARM, plus 2 GiB swap); `t4g.medium` (4 GiB) on the Paid plan for more projects | Graviton costs less than the same size on x86, and `t4g.small` is free-tier eligible |
| Access | SSM Session Manager | No SSH port and no SSH keys |
| Deploys | GitHub OIDC → IAM role → SSM Run Command | No AWS keys stored in GitHub; the role can deploy to this one instance and nothing else |
| Images | Copied from GHCR to ECR, tagged with the commit SHA | The exact image CI tested, immutable tags, vulnerability scan on push |
| Secrets | SSM Parameter Store, SecureString | Written to `.env` on the instance at each deploy; never in the image or the repository |
| Backups | Nightly `pg_dump` uploaded to S3 every 6 hours, kept 30 days, versioned | They survive losing the instance |
| Cost control | AWS Budget (counting usage before credits) and CloudWatch alarms | An email at 50% of the budget, and before the forecast passes it |

Left out on purpose, because each would cost more than the instance: RDS, a load balancer, a NAT gateway, EKS. See [Growing past one instance](#growing-past-one-instance).

## What you need

- An AWS account, and AWS CLI credentials for an identity allowed to create these resources.
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.10 or newer, the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), and the [GitHub CLI](https://cli.github.com/).
- On the AWS Free plan only free-tier instance types can run; the default, `t4g.small`, is one of them.

## 1. Create the infrastructure

```powershell
cd infra\terraform\aws
copy terraform.tfvars.example terraform.tfvars    # then fill in your emails and sign-in name
terraform init
terraform plan        # read it: about 45 resources, all new
terraform apply
```

Commit the `.terraform.lock.hcl` that `init` creates, so everyone gets the same provider versions. Never commit `terraform.tfvars` or `terraform.tfstate`: the state holds the generated passwords (both are git-ignored).

Then:

- **Confirm the subscription email** from AWS Notifications, or alarm emails will not arrive.
- **The address:** with no `domain` set, the app is at `https://<ip-with-dashes>.sslip.io`, which needs no DNS setup. With your own domain, create the two `A` records shown by `terraform output dns_records` (Terraform creates them itself if the domain is in Route 53 and you set `route53_zone_id`).

Wait a couple of minutes for the instance to finish its first boot (Docker, the AWS CLI, swap, the backup timer).

## 2. Connect GitHub

Give the deploy workflow its settings. The region and the site's address are plain variables; the role, instance, registry and bucket are secrets, because they contain your account ID and GitHub masks secrets in the logs of a public repository.

```powershell
cd ..\..\..
gh auth login
terraform -chdir=infra/terraform/aws output -raw github_settings
```

Run the `gh variable set` and `gh secret set` lines it prints.

Some accounts cannot create an OIDC provider; AWS's managed "projects" accounts deny it with a service control policy. There, set `github_auth = "access-key"` in `terraform.tfvars`: Terraform creates an IAM user that can only deploy, and the printed lines ask for its access key, which you create in the IAM console and store as two GitHub secrets. Those accounts also limit which regions you can use, so set `region` to the project's selected region. The deploy job runs in a GitHub environment named `production`, which is created on first use. To approve each deploy by hand, add yourself as a required reviewer in Settings > Environments > production.

## 3. Deploy

Push to `main`. When `ci` passes (it publishes the image for amd64 and arm64), the `deploy` workflow:

1. signs in to AWS with OIDC,
2. copies the image from GHCR to ECR,
3. uploads the compose files, the Caddyfile and the scripts to S3,
4. runs `infra/aws/deploy.sh` on the instance through SSM Run Command,
5. checks `https://<domain>/healthz` from the internet.

To deploy without a new commit, or to roll back, run it by hand: Actions > deploy > Run workflow, optionally with the full SHA of an earlier commit.

The first deploy takes a few minutes: it pulls Postgres, Phoenix and Caddy, and Caddy gets certificates from Let's Encrypt.

## 4. Sign in

```powershell
terraform -chdir=infra/terraform/aws output app_url
terraform -chdir=infra/terraform/aws output -raw admin_password
```

The sign-in name is your `admin_user`. The same sign-in protects Phoenix, at `terraform output phoenix_url`.

## Turning on Claude

The server starts with `copilot_model = "oracle"`, which is free and works on the labeled eval tickets. To use Claude:

```powershell
aws ssm put-parameter --name /support-copilot/prod/ANTHROPIC_API_KEY --type SecureString --value "sk-ant-..."
```

Set a monthly spend limit on the key in the Anthropic Console first. Then set `copilot_model = "claude"` in `terraform.tfvars`, run `terraform apply`, and run the deploy workflow. The key is not managed by Terraform, so it never appears in the state.

## Day-to-day

**A shell on the instance** (no SSH):

```powershell
aws ssm start-session --target (terraform -chdir=infra/terraform/aws output -raw instance_id)
```

Then, on the instance:

```bash
sudo -i
cd /opt/support-copilot
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml'
dc ps                       # all running and healthy
dc logs -f --tail 100 app   # the app's log
cat .deployed-image         # what is running
```

**Change a setting:** change it in Parameter Store (or in `terraform.tfvars`, then `terraform apply`), then run the deploy workflow. Every deploy rebuilds `.env` from Parameter Store.

**Backups** are in `s3://<bucket>/backups/` (`terraform output backup_bucket`). To restore the app database, on the instance:

```bash
aws s3 ls s3://<bucket>/backups/
aws s3 cp s3://<bucket>/backups/copilot-<stamp>.dump /tmp/restore.dump
dc exec -T postgres pg_restore --clean --if-exists -U copilot -d copilot < /tmp/restore.dump
dc restart app
```

Test a restore once, before you need one.

**Boot problems:** on the instance, `/var/log/cloud-init-output.log` has the first-boot log.

## What it costs

Rough monthly prices in Singapore, before credits. Check them in the [AWS Pricing Calculator](https://calculator.aws/) before you apply, as prices change:

| Item | About |
| --- | --- |
| EC2 `t4g.small`, running all month (`t4g.medium`: about twice that) | $12–15 |
| Public IPv4 address (the Elastic IP) | $3.60 |
| 20 GiB gp3 disk | $2 |
| ECR, S3, Parameter Store, CloudWatch alarms, Budgets, SNS email | cents, or free |
| **Total** | **about $18–22**, paid from the credits at first |

Ways to spend less:

- Stay on `t4g.small` while this is the only project; `t4g.medium` (4 GiB, Paid plan) fits two or three.
- **Stop the instance** when no one needs the demo: `aws ec2 stop-instances --instance-ids <id>`. The disk and the IP address are still billed (about $6 a month); start it again before an interview.
- `cpu_credits = "standard"` (the default) means the instance slows down rather than billing extra when it is busy for long. An alarm tells you when credits run low.

**Remove everything** when you are done:

```powershell
terraform -chdir=infra/terraform/aws destroy
aws ssm delete-parameter --name /support-copilot/prod/ANTHROPIC_API_KEY   # if you added it
```

The backup bucket and the image repository are kept if they are not empty, so backups are never deleted by accident. To delete them anyway, set `force_destroy = true` and apply before destroying.

## Security

- **No inbound access except the web.** The security group allows only 80 and 443. There is no SSH port; shells go through Session Manager and are logged in CloudTrail.
- **The containers cannot use the instance's AWS role.** The metadata service requires IMDSv2 with a hop limit of 1, which a container's network cannot reach.
- **Least-privilege roles.** The instance can read its own parameters, pull one repository and write backups. The deploy role can push one repository, upload release bundles and run commands on one instance, and only a job in this repository's `production` environment can assume it.
- **Encrypted at rest and in transit.** Encrypted disk and bucket, a bucket policy refusing plain HTTP, public access blocked, Let's Encrypt certificates for the site.
- **Secrets are generated, not typed.** Terraform generates the database password, the redaction key and the sign-in password; only their hash reaches Caddy.

## Growing past one instance

What would change, and when:

| When | Change |
| --- | --- |
| More projects on the same server | Give each its own app container and subdomain behind the same Caddy; move to `t4g.medium` (4 GiB) or `t4g.large` (8 GiB) when memory gets tight |
| Downtime during deploys matters | Two instances in an Auto Scaling group behind an Application Load Balancer, or ECS on Fargate |
| Data matters more than cost | Postgres on RDS, with automated backups and Multi-AZ |
| A team | Terraform state in S3 (`backend.tf.example`), separate staging and production, and sign-in through an identity provider instead of one shared password |
| Logs needed after an instance is gone | The CloudWatch agent shipping container logs |
