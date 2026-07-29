# AWS access keys for GitHub Actions → EC2 (`ap-south-1`)

GitHub Actions authenticates to AWS with **IAM user access keys** stored in repo secrets, then deploys to EC2 over SSH (Elastic IP + `fno-ec2-key.pem`).

**Repo:** `russelnickson/agentic-trade-future-and-options`  
**Region:** `ap-south-1` (Mumbai)

> Prefer rotating keys periodically. Do not commit keys into the repo or `.env` checked into git.

---

## 1. IAM user for deploy

1. IAM → **Users** → **Create user** (e.g. `github-actions-fno-deploy`).
2. Attach a minimal policy (or use console **Attach policies directly**). Example inline policy for identity check + optional EC2 describe:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "IdentityCheck",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    },
    {
      "Sid": "OptionalEc2Read",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeAddresses"
      ],
      "Resource": "*"
    }
  ]
}
```

The current workflow only needs `sts:GetCallerIdentity` for the verify step; the real deploy is SSH.

3. **Security credentials** → **Create access key** → Application running outside AWS → create.
4. Copy **Access key ID** and **Secret access key** once (secret is shown only once).

---

## 2. GitHub Secrets

Repo → **Settings → Secrets and variables → Actions**:

| Secret | Value |
|--------|--------|
| `AWS_ACCESS_KEY_ID` | `AKIA…` (no quotes / spaces / newlines) |
| `AWS_SECRET_ACCESS_KEY` | Secret key string (no quotes / newlines) |
| `EC2_HOST` | Elastic IP of the EC2 instance |
| `EC2_USERNAME` | `ubuntu` |
| `EC2_SSH_KEY` | Full PEM of `fno-ec2-key.pem` including `BEGIN` / `END` lines |

You can remove the unused `AWS_ROLE_ARN` secret if it remains from the old OIDC setup.

---

## 3. Workflow behaviour

`.github/workflows/deploy.yml`:

1. Run pytest on `main` / `workflow_dispatch`.
2. Configure AWS credentials from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (`ap-south-1`).
3. `aws sts get-caller-identity`.
4. SSH to `EC2_HOST` as `ubuntu` with `EC2_SSH_KEY`, pull `main`, install deps, `pm2 restart fno-worker`.

Trigger manually:

```bash
gh workflow run "Deploy to AWS EC2" --ref main
gh run watch
```

---

## 4. Common failures

| Error | Fix |
|-------|-----|
| `AWS_ACCESS_KEY_ID secret is empty` | Add the secret; ensure name matches exactly |
| `looks invalid (expected AKIA…)` | Use IAM **user** keys, not temporary `ASIA…` session keys; strip quotes |
| `InvalidClientTokenId` / `SignatureDoesNotMatch` | Wrong secret, rotated key, or trailing newline in secret |
| SSH timeout / connection refused | Instance running, SG allows port 22 from GitHub runners (or your IP for manual SSH), `EC2_HOST` is the current Elastic IP |
| `Permission denied (publickey)` | `EC2_SSH_KEY` must match the instance key pair |

---

## 5. Security notes

- Store keys only in GitHub Secrets (or local `.env`, never committed).
- Scope the IAM user to the smallest policy that still lets the job pass.
- Rotate access keys if leaked; delete unused keys in IAM.
- Keep SSH locked down (your IP + whatever is required for Actions).
