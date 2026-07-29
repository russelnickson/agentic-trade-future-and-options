#!/usr/bin/env bash
# git_push.sh — init (if needed), verify .env is ignored, commit, push to GitHub main.
#
# Usage:
#   ./git_push.sh
#   GITHUB_REPO=owner/trade ./git_push.sh          # create/link remote via gh
#   GIT_REMOTE_URL=git@github.com:owner/trade.git ./git_push.sh
#
# Safety:
#   - Refuses to proceed if .env / .env.production / .secrets.env would be committed
#   - Does not force-push
#   - Does not rewrite git config identity

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMMIT_MSG='feat: Production F&O execution engine and monitoring dashboard'
BRANCH="${GIT_BRANCH:-main}"

log() { printf '[git_push] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# 1) Ensure ignore rules for secrets
# ---------------------------------------------------------------------------
ensure_gitignore() {
  local gi=".gitignore"
  touch "$gi"
  local required=(".env" ".env.production" ".secrets.env" "venv/" "*.log" "*.parquet")
  local line
  for line in "${required[@]}"; do
    if ! grep -qxF "$line" "$gi" 2>/dev/null; then
      log "Adding '$line' to .gitignore"
      printf '%s\n' "$line" >>"$gi"
    fi
  done
}

ensure_gitignore

# ---------------------------------------------------------------------------
# 2) Initialize repository if needed
# ---------------------------------------------------------------------------
if [[ ! -d .git ]]; then
  log "No .git directory — initializing clean repository"
  git init -b "$BRANCH"
else
  log "Existing git repository detected"
fi

# Ensure we are on main (create/switch without destroying work)
current="$(git branch --show-current 2>/dev/null || true)"
if [[ -z "$current" ]]; then
  git checkout -B "$BRANCH"
elif [[ "$current" != "$BRANCH" ]]; then
  log "Switching from branch '$current' to '$BRANCH'"
  git checkout -B "$BRANCH"
fi

# ---------------------------------------------------------------------------
# 3) Status + secret guard
# ---------------------------------------------------------------------------
log "----- git status (porcelain) -----"
git status --porcelain=v1
log "----------------------------------"

if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  die ".env is tracked by git — remove it from the index before pushing: git rm --cached .env"
fi
if git ls-files --error-unmatch .env.production >/dev/null 2>&1; then
  die ".env.production is tracked by git — remove it: git rm --cached .env.production"
fi
if git ls-files --error-unmatch .secrets.env >/dev/null 2>&1; then
  die ".secrets.env is tracked by git — remove it: git rm --cached .secrets.env"
fi

# Also block if someone force-adds ignored env files in this invocation
if git check-ignore -q .env 2>/dev/null; then
  log ".env is ignored (OK)"
else
  # File may not exist yet — still require the ignore rule
  if grep -qxF '.env' .gitignore; then
    log ".env ignore rule present (file may be absent) — OK"
  else
    die ".env is not ignored by .gitignore"
  fi
fi

if [[ -f .env ]] && ! git check-ignore -q .env; then
  die ".env exists but is NOT ignored — aborting"
fi
if [[ -f .env.production ]] && ! git check-ignore -q .env.production; then
  die ".env.production exists but is NOT ignored — aborting"
fi
if [[ -f .secrets.env ]] && ! git check-ignore -q .secrets.env; then
  die ".secrets.env exists but is NOT ignored — aborting"
fi

# ---------------------------------------------------------------------------
# 4) Stage & commit
# ---------------------------------------------------------------------------
git add -A

# Extra belt-and-suspenders: unstage env files if somehow staged
git reset HEAD -- .env .env.production .secrets.env 2>/dev/null || true

if git diff --cached --name-only | grep -E '(^|/)\.env(\.production)?$' >/dev/null; then
  die "Refusing to commit: .env or .env.production is staged"
fi
if git diff --cached --name-only | grep -E '(^|/)\.secrets\.env$' >/dev/null; then
  die "Refusing to commit: .secrets.env is staged"
fi

if git diff --cached --quiet; then
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    log "Nothing new to commit — working tree clean relative to HEAD"
  else
    die "Nothing staged to commit (empty repository?)"
  fi
else
  log "Creating commit…"
  git commit -m "$COMMIT_MSG"
fi

log "----- git status after commit -----"
git status
log "Last commit: $(git log -1 --oneline 2>/dev/null || echo '(none)')"

# ---------------------------------------------------------------------------
# 5) Configure remote (GitHub) if missing
# ---------------------------------------------------------------------------
if ! git remote get-url origin >/dev/null 2>&1; then
  if [[ -n "${GIT_REMOTE_URL:-}" ]]; then
    log "Adding origin → $GIT_REMOTE_URL"
    git remote add origin "$GIT_REMOTE_URL"
  elif [[ -n "${GITHUB_REPO:-}" ]]; then
    command -v gh >/dev/null 2>&1 || die "gh CLI required to create/link GITHUB_REPO=$GITHUB_REPO"
    log "Creating/linking GitHub repo $GITHUB_REPO via gh"
    # create fails if repo exists; fall back to setting remote from gh view
    if gh repo view "$GITHUB_REPO" >/dev/null 2>&1; then
      url="$(gh repo view "$GITHUB_REPO" --json sshUrl -q .sshUrl 2>/dev/null || \
             gh repo view "$GITHUB_REPO" --json url -q .url)"
      git remote add origin "$url"
    else
      gh repo create "$GITHUB_REPO" --private --source=. --remote=origin --push=false
    fi
  else
    die "No 'origin' remote. Set GIT_REMOTE_URL=git@github.com:ORG/REPO.git or GITHUB_REPO=ORG/REPO"
  fi
fi

log "Remote origin: $(git remote get-url origin)"

# ---------------------------------------------------------------------------
# 6) Push to main on GitHub
# ---------------------------------------------------------------------------
log "Pushing branch '$BRANCH' to origin (no force)…"
git push -u origin "$BRANCH"

log "Done. Branch '$BRANCH' is on GitHub."
git status -sb
