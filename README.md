# bb2gh — Bitbucket Server to GitHub Enterprise Migration

A Python CLI tool for migrating repositories and pull requests from self-hosted Bitbucket Server (Data Center) to GitHub Enterprise, with continuous sync support for a smooth transition.

## Features

- **Bulk migration** — Clone all repos from Bitbucket, push to GitHub (branches, tags, full history)
- **Continuous sync** — Fetch from Bitbucket and push to GitHub every 60 seconds
- **PR migration** — Recreate open Bitbucket PRs on GitHub with title, description, comments, and reviewers
- **Idempotent** — Safe to re-run; skips already-migrated repos and PRs
- **Docker support** — Run the sync as a long-lived service

## Quick Start

### 1. Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your Bitbucket and GitHub credentials
```

### 2. Install

```bash
pip install -r requirements.txt
pip install -e .
```

### 3. Migrate

```bash
# Bulk migrate all repos
bb2gh --config config.yaml migrate

# Start continuous sync (runs until interrupted)
bb2gh --config config.yaml sync

# Migrate open PRs (dry-run first)
bb2gh --config config.yaml migrate-prs --dry-run
bb2gh --config config.yaml migrate-prs
```

### Docker

```bash
# Copy your config
mkdir config && cp config.yaml config/

# Run bulk migration
docker compose --profile migrate run migrate

# Start continuous sync
docker compose up -d sync

# Migrate PRs
docker compose --profile migrate-prs run migrate-prs
```

## Configuration

See `config.yaml.example` for all options. Key settings:

| Setting | Description |
|---------|-------------|
| `bitbucket.base_url` | Bitbucket Server URL |
| `bitbucket.token` | Personal access token (or set `BB_TOKEN` env var) |
| `bitbucket.ssh_url` | SSH base URL for git clone |
| `bitbucket.projects` | Optional list of projects to migrate (omit for all) |
| `github.base_url` | GitHub Enterprise API URL |
| `github.token` | GitHub PAT with repo + admin:org (or set `GH_TOKEN` env var) |
| `github.org` | Target GitHub organization |
| `sync.interval_seconds` | Sync frequency (default: 60) |
| `user_mapping` | Bitbucket → GitHub username mapping for PR reviewers |

## How It Works

1. **`migrate`** — For each Bitbucket repo: creates a GitHub repo, bare-clones via SSH, cleans hidden refs, and pushes `--mirror`
2. **`sync`** — Loops every N seconds: `git fetch origin --prune` then `git push github --mirror` for each migrated repo
3. **`migrate-prs`** — For each open PR: creates a GitHub PR with metadata header, migrates comments, and assigns reviewers

## PR Migration Notes

- PRs are created under the service account (original author is noted in the PR body)
- Inline/file-level comments are migrated as regular PR comments
- Reviewer assignments use the `user_mapping` config (falls back to same username)
- Merged/closed PRs are not migrated (only open PRs)

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```
