# bb2gh — Bitbucket Server to GitHub Enterprise Migration

A Python CLI tool for migrating repositories and pull requests from self-hosted Bitbucket Server (Data Center) to GitHub Enterprise, with continuous sync support for a smooth transition.

## Features

- **Bulk migration** — Clone all repos from Bitbucket and push to GitHub with full history, branches, and tags
- **Continuous sync** — Keep repos in lockstep by fetching from Bitbucket and pushing to GitHub on a configurable interval
- **PR migration** — Recreate open Bitbucket PRs on GitHub with title, description, comments, and reviewers
- **Project-to-org mapping** — Route Bitbucket projects to different GitHub orgs with flexible repo naming
- **Idempotent** — Safe to re-run; skips already-migrated repos and PRs
- **Docker support** — Run as a one-shot command or a long-lived sync service

## Prerequisites

| Requirement | Details |
|---|---|
| **Python** | 3.9+ |
| **Git** | Installed and on `PATH` |
| **Bitbucket Server** | A service account with **Project READ** access on each project you want to migrate |
| **GitHub Enterprise** | A personal access token with `repo` + `admin:org` scopes |
| **SSH key** | The machine running bb2gh needs SSH access to Bitbucket for `git clone` |

### Bitbucket Server Access

You do **not** need admin access to Bitbucket. Request a service account from your Bitbucket admin with:

- **Project READ** on every project you want to migrate

That single permission covers:
- Cloning repos via SSH
- Listing repos via REST API
- Reading pull requests, comments, and reviewer info

### GitHub Enterprise Access

You need a personal access token (PAT) with these scopes:

- `repo` — full control of private repositories
- `admin:org` — needed to create repos in organizations

If you are migrating to multiple GitHub orgs, the token must have access to all of them.

---

## Getting Started

### Step 1: Clone this repo

```bash
git clone <this-repo-url>
cd bitbucket--github
```

### Step 2: Install dependencies

```bash
pip install -r requirements.txt
pip install -e .
```

Verify the install:

```bash
bb2gh --help
```

### Step 3: Set up SSH access to Bitbucket

The tool clones repos from Bitbucket via SSH. Make sure the machine running bb2gh can reach your Bitbucket Server over SSH:

```bash
# Test connectivity (use your actual Bitbucket SSH host and port)
ssh -T git@bitbucket.mycompany.com -p 7999
```

If you are using a non-default SSH key, configure it in `~/.ssh/config`:

```
Host bitbucket.mycompany.com
  IdentityFile ~/.ssh/bb_migration_key
  Port 7999
```

### Step 4: Create your config file

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` with your actual values. At minimum, fill in:

```yaml
bitbucket:
  base_url: "https://bitbucket.mycompany.com"
  token: "YOUR_BITBUCKET_TOKEN"
  ssh_url: "ssh://git@bitbucket.mycompany.com:7999"
  # Optional: limit to specific projects (omit to migrate all accessible projects)
  projects:
    - PROJ1
    - PROJ2

github:
  base_url: "https://github.mycompany.com/api/v3"
  token: "YOUR_GITHUB_TOKEN"
  org: "my-org"
```

Alternatively, set tokens via environment variables instead of putting them in the file:

```bash
export BB_TOKEN="your-bitbucket-token"
export GH_TOKEN="your-github-token"
```

### Step 5: Configure project-to-org mapping (optional)

If your Bitbucket projects should land in different GitHub organizations, or you want to control repo naming, add the `repo_mapping` section:

```yaml
repo_mapping:
  # Default naming template for GitHub repos
  # Available variables: {project}, {project_lower}, {slug}
  name_template: "{project_lower}-{slug}"

  # Per-project overrides
  projects:
    INFRA:
      github_org: "infra-team"           # INFRA repos → infra-team org
      repos:
        legacy-monolith:
          github_name: "infra-monolith"  # Explicit rename for one repo
    PLATFORM:
      github_org: "platform-eng"         # PLATFORM repos → platform-eng org
```

**How mapping resolution works:**

| What | Resolution order |
|---|---|
| **GitHub org** | Per-project `github_org` → default `github.org` |
| **Repo name** | Per-repo `github_name` → per-project `name_template` → global `name_template` → `{slug}` |

**Examples** (given the config above):

| Bitbucket | GitHub |
|---|---|
| `INFRA/my-service` | `infra-team/infra-my-service` |
| `INFRA/legacy-monolith` | `infra-team/infra-monolith` (explicit override) |
| `PLATFORM/api-gateway` | `platform-eng/platform-api-gateway` |
| `OTHER/some-tool` | `my-org/other-some-tool` (uses defaults) |

If you omit `repo_mapping` entirely, all repos go to `github.org` with their original Bitbucket slug as the name.

#### Migrate only a subset of repos from a project

If you only want specific repos from a project, use `include_repos` (allowlist) or `exclude_repos` (denylist):

```yaml
repo_mapping:
  projects:
    INFRA:
      github_org: "infra-team"
      # Only these repos from INFRA are migrated; everything else is skipped
      include_repos:
        - my-service
        - my-api
    PLATFORM:
      github_org: "platform-eng"
      # Migrate all repos EXCEPT these
      exclude_repos:
        - deprecated-tool
        - archived-spike
```

**Resolution:**
- If `include_repos` is set, only those repos migrate (acts as an allowlist).
- Otherwise, `exclude_repos` skips the listed repos.
- If both are set, `include_repos` wins and `exclude_repos` is ignored.
- Projects with no filter migrate all repos (the default).

### Step 6: Configure user mapping (optional)

Map Bitbucket usernames to GitHub usernames for PR reviewer assignments and author attribution:

```yaml
user_mapping:
  bb_jsmith: "gh-john-smith"
  bb_jdoe: "gh-jane-doe"
```

Unmapped users fall through with their Bitbucket username as-is.

### Step 7: Run the migration

Run these commands in order:

```bash
# 1. Bulk migrate all repos (creates GitHub repos, clones, pushes)
bb2gh --config config.yaml migrate

# 2. Start continuous sync (keeps repos in lockstep during transition)
bb2gh --config config.yaml sync

# 3. In a separate terminal, migrate open PRs
#    Always dry-run first to review what will be created:
bb2gh --config config.yaml migrate-prs --dry-run
bb2gh --config config.yaml migrate-prs
```

Use `-v` for debug logging on any command:

```bash
bb2gh --config config.yaml -v migrate
```

### Step 8: Cut over

Once your team is ready to switch to GitHub:

1. Stop the sync process (`Ctrl+C` or `docker compose down`)
2. Set Bitbucket repos to read-only (ask your Bitbucket admin)
3. Update CI/CD pipelines to point to GitHub
4. Notify your team to use GitHub going forward

---

## Docker Usage

### Build

```bash
docker compose build
```

### Run bulk migration

```bash
mkdir -p config && cp config.yaml config/

docker compose --profile migrate run migrate
```

### Run continuous sync as a background service

```bash
docker compose up -d sync
```

Check logs:

```bash
docker compose logs -f sync
```

### Run PR migration

```bash
docker compose --profile migrate-prs run migrate-prs
```

### Environment variables

Pass tokens via environment instead of the config file:

```bash
BB_TOKEN=xxx GH_TOKEN=yyy docker compose up -d sync
```

### SSH keys

By default, Docker Compose mounts `~/.ssh` into the container. Override with:

```bash
SSH_KEY_PATH=/path/to/keys docker compose --profile migrate run migrate
```

---

## CLI Reference

```
bb2gh [OPTIONS] COMMAND

Options:
  --config PATH   Path to config file (default: config.yaml)
  -v, --verbose   Enable debug logging

Commands:
  migrate       Bulk migrate repos from Bitbucket to GitHub
  sync          Continuously sync repos (runs until interrupted)
  migrate-prs   Migrate open pull requests from Bitbucket to GitHub
```

### migrate

Clones each Bitbucket repo as a bare mirror and pushes to GitHub. Creates the target GitHub repo if it does not exist. Skips repos that have already been migrated (tracked in `state.json`).

### sync

Runs a loop that fetches from Bitbucket (`origin`) and pushes to GitHub (`github` remote) for every migrated repo. The interval is configured via `sync.interval_seconds` (default: 60s). Handles `SIGTERM`/`SIGINT` for graceful shutdown.

### migrate-prs

Reads open pull requests from Bitbucket and creates matching PRs on GitHub. Each migrated PR includes:
- A metadata header with the original author, creation date, and a link back to the Bitbucket PR
- All general comments (attributed to the original commenter)
- Reviewer assignments (mapped via `user_mapping`)

Use `--dry-run` to preview without creating anything.

---

## How It Works

```
Bitbucket Server                        GitHub Enterprise
┌──────────────┐     bb2gh migrate     ┌──────────────┐
│  PROJ/repo-a ├──── git clone ──────►│ org/repo-a    │
│  PROJ/repo-b ├──── --bare ────────►│ org/repo-b    │
│  INFRA/svc   ├──── + push mirror ──►│ infra/svc     │
└──────┬───────┘                       └──────▲───────┘
       │          bb2gh sync                  │
       └──── fetch origin ── push github ─────┘
              (every 60s)
```

1. **`migrate`** — For each Bitbucket repo: resolves the target GitHub org/name from the mapping config, creates the GitHub repo, bare-clones via SSH, cleans hidden refs (`refs/pull/*`), and pushes `--mirror`.

2. **`sync`** — Loops on a configurable interval: `git fetch origin --prune` then `git push github --mirror` for each migrated repo. The `--mirror` push ensures GitHub is an exact replica (all branches, tags, force-pushes). During the transition, Bitbucket is the source of truth.

3. **`migrate-prs`** — For each open PR: looks up the correct GitHub org/repo from state, creates a GitHub PR with metadata header, migrates comments, and assigns reviewers.

### State tracking

Migration progress is stored in `state.json` (inside `sync.work_dir`). This file tracks:
- Which repos have been migrated and their GitHub org/repo mapping
- Last sync timestamp per repo
- Bitbucket PR ID → GitHub PR number mappings

This makes every operation idempotent — re-running any command skips already-completed work.

---

## PR Migration Notes

- PRs are created under the service account — the original author is attributed in the PR body
- Inline/file-level Bitbucket comments are migrated as regular PR comments
- Only **open** PRs are migrated (merged/declined PRs are preserved in git history)
- Reviewer assignments use `user_mapping`; unmapped usernames pass through as-is

---

## Configuration Reference

| Setting | Required | Default | Description |
|---|---|---|---|
| `bitbucket.base_url` | Yes | — | Bitbucket Server URL (no trailing slash) |
| `bitbucket.token` | Yes* | `$BB_TOKEN` | Personal access token for REST API |
| `bitbucket.ssh_url` | Yes | — | SSH base URL for git clone (e.g. `ssh://git@host:7999`) |
| `bitbucket.projects` | No | all | List of project keys to migrate |
| `github.base_url` | Yes | — | GitHub Enterprise API URL |
| `github.token` | Yes* | `$GH_TOKEN` | PAT with `repo` + `admin:org` scopes |
| `github.org` | Yes | — | Default target GitHub organization |
| `sync.interval_seconds` | No | `60` | Seconds between sync cycles |
| `sync.work_dir` | No | `/data/mirror` | Directory for bare repo clones and state |
| `repo_mapping.name_template` | No | `{slug}` | Template for GitHub repo names |
| `repo_mapping.projects.<KEY>.github_org` | No | `github.org` | Override target org per project |
| `repo_mapping.projects.<KEY>.name_template` | No | global template | Override naming per project |
| `repo_mapping.projects.<KEY>.include_repos` | No | — | Allowlist: only listed repos migrate |
| `repo_mapping.projects.<KEY>.exclude_repos` | No | `[]` | Denylist: listed repos are skipped |
| `repo_mapping.projects.<KEY>.repos.<slug>.github_name` | No | template | Explicit repo name override |
| `user_mapping` | No | `{}` | Bitbucket → GitHub username map |

\* Can be set via environment variable instead.

---

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `git clone` fails with permission denied | Verify SSH key is configured and the Bitbucket service account has Project READ |
| `422` error when creating GitHub repo | Repo already exists (this is handled automatically) — or the token lacks `repo` scope |
| PR migration fails with `404` | The source or target branch was deleted; the PR cannot be recreated |
| Sync takes too long for many repos | Increase `sync.interval_seconds` or reduce the project list |
| `state.json` is corrupted | Delete it and re-run `migrate` (it will skip repos that already exist on GitHub) |
