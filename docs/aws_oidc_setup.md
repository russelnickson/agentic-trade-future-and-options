# AWS OIDC setup for GitHub Actions → EC2 (`ap-south-1`)

This guide configures **OpenID Connect (OIDC)** so GitHub Actions can assume an IAM role in **`ap-south-1` (Mumbai)** without long-lived AWS access keys. SSH deploy to EC2 still uses the instance Elastic IP + `fno-ec2-key.pem` via GitHub Secrets.

**Repo (this project):** `russelnickson/agentic-trade-future-and-options`  
**Branch allowed to assume the role:** `main`

---

## 1. IAM OIDC identity provider

In IAM → **Identity providers** → **Add provider**:

| Field | Value |
|-------|--------|
| Provider type | OpenID Connect |
| Provider URL | `https://token.actions.githubusercontent.com` |
| Audience | `sts.amazonaws.com` |

After create, note the provider ARN (used implicitly when you attach the trust policy to a role).

---

## 2. IAM role trust policy (OIDC)

Create a role (e.g. `github-actions-fno-deploy`) with **Custom trust policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:russelnickson/agentic-trade-future-and-options:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

Replace `<AWS_ACCOUNT_ID>` with your 12-digit account ID.

**Condition summary**

- **Provider:** `token.actions.githubusercontent.com`
- **Audience (`aud`):** `sts.amazonaws.com`
- **Subject (`sub`):** must match what GitHub actually issues for the job

| Job config | Typical `sub` |
|------------|----------------|
| Push to `main`, **no** `environment:` | `repo:russelnickson/agentic-trade-future-and-options:ref:refs/heads/main` |
| Job sets `environment: production` | `repo:russelnickson/agentic-trade-future-and-options:environment:production` |

This repo’s deploy job intentionally **omits** `environment:` so the trust policy above works as written.

If you later add `environment: production`, update the trust condition to:

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": "repo:russelnickson/agentic-trade-future-and-options:environment:production"
}
```

Or allow both with two statements / a broader pattern:

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": [
    "repo:russelnickson/agentic-trade-future-and-options:ref:refs/heads/main",
    "repo:russelnickson/agentic-trade-future-and-options:environment:*"
  ]
}
```

### Common failure

```text
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

Checklist:

1. IAM OIDC provider exists for `token.actions.githubusercontent.com` with audience `sts.amazonaws.com`.
2. Role trust `Federated` ARN account ID matches the role account.
3. Trust `sub` matches the job (see table — **environment vs ref**).
4. Secret `AWS_ROLE_ARN` is the full role ARN (`arn:aws:iam::ACCOUNT:role/NAME`), no typos/spaces.
5. Thumbprint on the OIDC provider is current (IAM usually manages this; recreate provider if stale).

---

## 3. IAM permission policy (deploy + EC2/SSM checks)

Attach an inline or managed policy to the same role, e.g. `github-actions-fno-deploy-permissions`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowSelfAssumeViaOidc",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DescribeEc2ForDeployTarget",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeAddresses",
        "ec2:DescribeTags",
        "ec2:DescribeRegions"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "aws:RequestedRegion": "ap-south-1"
        }
      }
    },
    {
      "Sid": "SsmConnectivityChecks",
      "Effect": "Allow",
      "Action": [
        "ssm:DescribeInstanceInformation",
        "ssm:GetConnectionStatus",
        "ssm:ListInstanceAssociations",
        "ssm:DescribeInstanceProperties"
      ],
      "Resource": "*"
    },
    {
      "Sid": "OptionalSsmRunCommandRead",
      "Effect": "Allow",
      "Action": [
        "ssm:ListCommands",
        "ssm:ListCommandInvocations",
        "ssm:GetCommandInvocation"
      ],
      "Resource": "*"
    },
    {
      "Sid": "OptionalSsmSendCommandScoped",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand"
      ],
      "Resource": [
        "arn:aws:ec2:ap-south-1:<AWS_ACCOUNT_ID>:instance/*",
        "arn:aws:ssm:ap-south-1::document/AWS-RunShellScript",
        "arn:aws:ssm:ap-south-1:<AWS_ACCOUNT_ID>:document/*"
      ]
    }
  ]
}
```

Replace `<AWS_ACCOUNT_ID>` again. Tighten `ssm:SendCommand` / `ec2:Describe*` further to a single instance ID when you have it.

**Role ARN (example shape — copy from IAM after create):**

```text
arn:aws:iam::<AWS_ACCOUNT_ID>:role/github-actions-fno-deploy
```

---

## 4. GitHub Actions secrets

**GitHub → repo → Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|-------------|--------|
| `AWS_ROLE_ARN` | ARN of the IAM role created above (OIDC deploy role). |
| `EC2_HOST` | Elastic Static IP of the EC2 instance in `ap-south-1`. |
| `EC2_USERNAME` | `ubuntu` |
| `EC2_SSH_KEY` | Full private key PEM contents of `fno-ec2-key.pem` (including `-----BEGIN … KEY-----` / `-----END … KEY-----`). |

### Workflow usage (reference)

```yaml
permissions:
  id-token: write   # required for OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ap-south-1
      - name: Verify caller identity
        run: aws sts get-caller-identity
      # SSH deploy example (uses EC2_* secrets; not AWS keys)
      - name: Deploy over SSH
        env:
          EC2_HOST: ${{ secrets.EC2_HOST }}
          EC2_USERNAME: ${{ secrets.EC2_USERNAME }}
          EC2_SSH_KEY: ${{ secrets.EC2_SSH_KEY }}
        run: |
          install -m 600 /dev/null "${RUNNER_TEMP}/fno-ec2-key.pem"
          printf '%s\n' "$EC2_SSH_KEY" > "${RUNNER_TEMP}/fno-ec2-key.pem"
          ssh -i "${RUNNER_TEMP}/fno-ec2-key.pem" \
            -o StrictHostKeyChecking=accept-new \
            "${EC2_USERNAME}@${EC2_HOST}" 'uname -a'
```

---

## 5. Security checklist

- Do **not** store AWS access keys in GitHub Secrets when OIDC is configured.
- Do **not** commit `fno-ec2-key.pem`, `.env`, or role ARNs with live credentials into git.
- Keep the trust `sub` locked to `ref:refs/heads/main` until you explicitly need other branches/environments.
- Prefer SSM Session Manager / SendCommand over opening broad SSH from the internet when practicable; keep `EC2_SSH_KEY` as a fallback for PM2/FastAPI deploys.

---

## 6. Quick validation

1. Push a workflow on `main` that only runs `aws sts get-caller-identity`.
2. Confirm AssumedRole ARN matches `AWS_ROLE_ARN`.
3. Confirm `EC2_HOST` SSH with `ubuntu` + `EC2_SSH_KEY` works from the runner.
