"""Prepare workspaces for Jenkins-to-GitHub-Actions conversion."""

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone

import yaml

from .github_client import GithubClient
from .state import State

logger = logging.getLogger(__name__)


def _run_git(args, cwd=None, quiet=False):
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if not quiet:
            logger.error("git %s failed: %s", args[0], result.stderr.strip())
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout.strip()


def find_jenkins_files(all_paths):
    """Filter a list of file paths for Jenkins-related files.

    Returns (jenkins_files, dependency_files) tuple.
    """
    jenkins_files = []
    deps = []
    jenkinsfile_dirs = set()

    for path in all_paths:
        basename = os.path.basename(path).lower()

        if basename == "jenkinsfile" or basename.startswith("jenkinsfile.") or basename.endswith(".jenkinsfile"):
            jenkins_files.append(path)
            jenkinsfile_dirs.add(os.path.dirname(path))
            continue

        parts = path.split("/")
        if parts[0] in ("vars", "resources"):
            deps.append(path)
            continue
        if parts[0] == "src" and path.lower().endswith((".groovy", ".java")):
            deps.append(path)
            continue

    for path in all_paths:
        if path in jenkins_files or path in deps:
            continue
        if path.lower().endswith(".groovy") and os.path.dirname(path) in jenkinsfile_dirs:
            deps.append(path)

    return jenkins_files, deps


def parse_jenkinsfile_refs(content):
    """Extract file references from Jenkinsfile content."""
    refs = set()
    for pattern in [
        r"""load\s+['"]([^'"]+)['"]""",
        r"""readFile\s*\(\s*['"]([^'"]+)['"]""",
        r"""evaluate\s*\(\s*readFile\s*\(\s*['"]([^'"]+)['"]""",
    ]:
        for match in re.finditer(pattern, content):
            refs.add(match.group(1))
    return refs


def _resolve_dependencies(bare_path, jenkins_files, ref="HEAD"):
    """Read Jenkinsfiles from a repo and find referenced files."""
    extra_deps = set()
    for jf in jenkins_files:
        try:
            content = _run_git(["show", f"{ref}:{jf}"], cwd=bare_path, quiet=True)
        except subprocess.CalledProcessError:
            continue
        refs = parse_jenkinsfile_refs(content)
        extra_deps.update(refs)
    return extra_deps


def _get_all_tree_paths(repo_path, ref="HEAD"):
    """List all file paths in a git tree."""
    output = _run_git(["ls-tree", "-r", "--name-only", ref], cwd=repo_path)
    return [p for p in output.splitlines() if p.strip()]


def create_workspace(config, state, gh, project_key, repo_slug,
                     source_branch, migration_branch, dry_run=False):
    """Create a Jenkins workspace for a single repo."""
    gh_org, gh_repo_name = state.get_github_target(project_key, repo_slug)
    if not gh_org:
        gh_org, gh_repo_name = config.resolve_target(project_key, repo_slug)

    repo_key = f"{project_key}/{repo_slug}"
    workspace_base = os.path.join(config.work_dir, "jenkins-workspaces")
    workspace_path = os.path.join(workspace_base, f"{gh_org}__{gh_repo_name}")

    # Discover Jenkins files from the bare clone (fast, no network)
    bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")
    if not os.path.exists(bare_path):
        logger.warning("Bare clone not found for %s, skipping", repo_key)
        return None

    # Determine source branch
    if not source_branch:
        try:
            source_branch = gh.get_default_branch(gh_repo_name, org_name=gh_org)
        except Exception:
            source_branch = "master"

    # Get file tree from bare clone
    try:
        ref = f"refs/heads/{source_branch}"
        all_paths = _get_all_tree_paths(bare_path, ref=ref)
    except subprocess.CalledProcessError:
        all_paths = _get_all_tree_paths(bare_path)

    jenkins_files, dep_files = find_jenkins_files(all_paths)

    if not jenkins_files:
        logger.info("No Jenkinsfiles found in %s, skipping", repo_key)
        return None

    # Resolve inline dependencies from Jenkinsfile content
    extra_refs = _resolve_dependencies(bare_path, jenkins_files, ref=ref)
    validated_extras = [p for p in extra_refs if p in all_paths]
    all_required = sorted(set(jenkins_files + dep_files + validated_extras))

    if dry_run:
        logger.info("[DRY RUN] Would prepare %s: %d Jenkinsfiles, %d dependencies",
                    repo_key, len(jenkins_files), len(all_required) - len(jenkins_files))
        for f in jenkins_files:
            logger.info("[DRY RUN]   Jenkinsfile: %s", f)
        return None

    # Create migration branch on GitHub
    try:
        gh.create_branch(gh_repo_name, migration_branch,
                         from_branch=source_branch, org_name=gh_org)
    except Exception:
        logger.warning("Could not create migration branch %s on %s/%s",
                       migration_branch, gh_org, gh_repo_name)

    # Create workspace via sparse checkout
    os.makedirs(workspace_base, exist_ok=True)
    if os.path.exists(workspace_path):
        import shutil
        shutil.rmtree(workspace_path)

    clone_url = gh.get_clone_url(gh_repo_name, org_name=gh_org,
                                 ssh_url=config.gh_ssh_url or None)

    env_no_lfs = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    subprocess.run(
        ["git", "clone", "--no-checkout", "--depth=1",
         f"--branch={source_branch}", clone_url, workspace_path],
        capture_output=True, text=True, check=True, env=env_no_lfs,
    )

    # Configure sparse checkout
    _run_git(["sparse-checkout", "init", "--no-cone"], cwd=workspace_path)
    sparse_file = os.path.join(workspace_path, ".git", "info", "sparse-checkout")
    with open(sparse_file, "w") as f:
        for path in all_required:
            f.write(path + "\n")

    _run_git(["checkout"], cwd=workspace_path)

    # Create migration branch locally
    _run_git(["checkout", "-b", migration_branch], cwd=workspace_path)

    # Create .github/workflows directory
    workflows_dir = os.path.join(workspace_path, ".github", "workflows")
    os.makedirs(workflows_dir, exist_ok=True)

    # Write metadata
    meta = {
        "source": {
            "bitbucket_project": project_key,
            "bitbucket_repo": repo_slug,
            "github_org": gh_org,
            "github_repo": gh_repo_name,
            "source_branch": source_branch,
            "migration_branch": migration_branch,
        },
        "jenkins_files": jenkins_files,
        "dependencies": [f for f in all_required if f not in jenkins_files],
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(workspace_path, ".bb2gh-jenkins-meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # Write per-repo manifest
    manifest = {
        "repo": {
            "github_org": gh_org,
            "github_repo": gh_repo_name,
            "clone_url": clone_url,
            "source_branch": source_branch,
            "migration_branch": migration_branch,
        },
        "jenkins_files": jenkins_files,
        "dependencies": [f for f in all_required if f not in jenkins_files],
        "all_files": all_required,
    }
    with open(os.path.join(workspace_path, ".bb2gh-jenkins-manifest.yaml"), "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

    # Track in state
    state.mark_jenkins_prepared(
        project_key, repo_slug, migration_branch,
        workspace_path, jenkins_files,
    )

    logger.info("Prepared workspace for %s: %d Jenkinsfiles, %d total files -> %s",
                repo_key, len(jenkins_files), len(all_required), workspace_path)
    return workspace_path


def prepare_jenkins_workspaces(config, only_repos=None, branch=None,
                               migration_branch_name="ci/github-actions-migration",
                               dry_run=False):
    """Prepare Jenkins workspaces for migrated repos."""
    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    state = State(config.work_dir)

    repos = state.get_migrated_repos()
    if not repos:
        logger.warning("No migrated repos found.")
        return 0, 0, 0

    prepared = 0
    skipped = 0
    failed = 0

    for project_key, repo_slug in repos:
        repo_key = f"{project_key}/{repo_slug}"
        if only_repos and repo_key not in only_repos:
            continue

        if state.is_jenkins_prepared(project_key, repo_slug):
            logger.info("Skipping already prepared: %s", repo_key)
            skipped += 1
            continue

        try:
            result = create_workspace(
                config, state, gh, project_key, repo_slug,
                branch, migration_branch_name, dry_run,
            )
            if result:
                prepared += 1
            else:
                skipped += 1
        except Exception:
            logger.exception("Failed to prepare %s", repo_key)
            failed += 1

    if not dry_run and prepared > 0:
        logger.warning(
            "WARNING: The sync loop (bb2gh sync) will delete the migration "
            "branch '%s' on its next cycle. Pause sync or merge your changes "
            "before the next sync.", migration_branch_name,
        )

    return prepared, skipped, failed


def generate_manifest(config, only_repos=None, branch=None,
                      migration_branch_name="ci/github-actions-migration",
                      output_path="jenkins-manifest.yaml"):
    """Generate a manifest file listing all repos with Jenkins files."""
    state = State(config.work_dir)
    repos = state.get_migrated_repos()
    if not repos:
        logger.warning("No migrated repos found.")
        return 0

    gh = GithubClient(config.gh_base_url, config.gh_token, config.gh_org)
    manifest_repos = []
    found = 0

    for project_key, repo_slug in repos:
        repo_key = f"{project_key}/{repo_slug}"
        if only_repos and repo_key not in only_repos:
            continue

        bare_path = os.path.join(config.work_dir, f"{project_key}__{repo_slug}.git")
        if not os.path.exists(bare_path):
            continue

        gh_org, gh_repo_name = state.get_github_target(project_key, repo_slug)
        if not gh_org:
            gh_org, gh_repo_name = config.resolve_target(project_key, repo_slug)

        # Determine source branch
        src_branch = branch
        if not src_branch:
            try:
                src_branch = gh.get_default_branch(gh_repo_name, org_name=gh_org)
            except Exception:
                src_branch = "master"

        try:
            ref = f"refs/heads/{src_branch}"
            all_paths = _get_all_tree_paths(bare_path, ref=ref)
        except subprocess.CalledProcessError:
            all_paths = _get_all_tree_paths(bare_path)

        jenkins_files, dep_files = find_jenkins_files(all_paths)
        if not jenkins_files:
            continue

        extra_refs = _resolve_dependencies(bare_path, jenkins_files, ref=ref)
        validated_extras = [p for p in extra_refs if p in all_paths]
        all_deps = sorted(set(dep_files + validated_extras))

        clone_url = gh.get_clone_url(gh_repo_name, org_name=gh_org,
                                     ssh_url=config.gh_ssh_url or None)

        manifest_repos.append({
            "github_org": gh_org,
            "github_repo": gh_repo_name,
            "clone_url": clone_url,
            "source_branch": src_branch,
            "jenkins_files": jenkins_files,
            "dependencies": all_deps,
        })
        found += 1
        logger.info("Found %d Jenkinsfiles in %s", len(jenkins_files), repo_key)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "migration_branch": migration_branch_name,
        "repos": manifest_repos,
    }

    with open(output_path, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

    logger.info("Manifest written to %s: %d repos with Jenkinsfiles", output_path, found)
    return found
