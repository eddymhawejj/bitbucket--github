"""Submodule URL remapping for Bitbucket-to-GitHub migration."""

import logging
import os
import re
import subprocess
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_REMAP_ENV = {
    "GIT_AUTHOR_NAME": "bb2gh",
    "GIT_AUTHOR_EMAIL": "bb2gh@migration",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "bb2gh",
    "GIT_COMMITTER_EMAIL": "bb2gh@migration",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}

_COMMIT_MSG = "bb2gh: remap submodule URLs for GitHub migration"


def _git(args, cwd, stdin_data=None, env_extra=None):
    cmd = ["git"] + args
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False,
        input=stdin_data, env=env,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout.strip()


def _build_bb_hostnames(config):
    """Extract all known Bitbucket hostnames from config."""
    hostnames = set(config.bb_ssh_hostnames)

    # Extract hostname from ssh_url: ssh://git@host:port -> host
    parsed = urlparse(config.bb_ssh_url)
    if parsed.hostname:
        hostnames.add(parsed.hostname)

    # Extract hostname from base_url: https://host -> host
    parsed = urlparse(config.bb_base_url)
    if parsed.hostname:
        hostnames.add(parsed.hostname)

    return hostnames


def _extract_submodule_urls(content):
    """Extract all url = ... values from .gitmodules content."""
    return re.findall(r"url\s*=\s*(.+)", content)


def _parse_bb_url(url, bb_hostnames):
    """Parse a URL and check if it's a Bitbucket URL.

    Returns (project_key, slug) if it's a BB URL, None otherwise.
    Handles:
      - ssh://git@host:port/project/repo.git
      - git@host:port/project/repo.git  (shouldn't exist for BB but just in case)
      - https://host/scm/project/repo.git
    """
    # SSH format: ssh://git@hostname:port/project/repo.git
    m = re.match(r"ssh://[^@]+@([^:/]+)[:/]\d*/([^/]+)/([^/]+?)\.git$", url)
    if m and m.group(1) in bb_hostnames:
        return m.group(2), m.group(3)

    # HTTP format: https://hostname/scm/project/repo.git
    m = re.match(r"https?://([^/]+)/scm/([^/]+)/([^/]+?)\.git$", url)
    if m and m.group(1) in bb_hostnames:
        return m.group(2), m.group(3)

    return None


def _is_already_github(url, gh_ssh_host, gh_https_base):
    """Check if a URL already points to GitHub."""
    if gh_ssh_host and (url.startswith(f"git@{gh_ssh_host}:") or url.startswith(f"ssh://git@{gh_ssh_host}/")):
        return True
    if gh_https_base and gh_https_base in url:
        return True
    return False


def remap_submodule_urls(content, config):
    """Replace Bitbucket submodule URLs in .gitmodules with GitHub URLs.

    If any Bitbucket URL cannot be resolved (project not migrated, repo
    excluded), the entire .gitmodules is left unchanged to avoid a mix
    of old and new URLs.
    """
    bb_hostnames = _build_bb_hostnames(config)
    gh_ssh_host = config.gh_ssh_host
    gh_https_base = config.gh_base_url.replace("/api/v3", "").rstrip("/")

    urls = _extract_submodule_urls(content)
    if not urls:
        return content

    # First pass: check ALL URLs can be resolved
    replacements = {}
    for url in urls:
        url = url.strip()

        # Already points to GitHub — skip
        if _is_already_github(url, gh_ssh_host, gh_https_base):
            continue

        parsed = _parse_bb_url(url, bb_hostnames)
        if parsed is None:
            # Not a Bitbucket URL we recognize — skip (external dependency)
            continue

        project_key_raw, slug = parsed
        resolved = None
        for pk in [project_key_raw.upper(), project_key_raw]:
            if config.bb_projects and pk not in config.bb_projects:
                continue
            if not config.should_migrate_repo(pk, slug):
                continue
            resolved = config.resolve_target(pk, slug)
            break

        if not resolved:
            logger.warning(
                "Cannot remap submodule URL %s — project %s/%s not in migration scope. "
                "Skipping .gitmodules rewrite entirely.",
                url, project_key_raw, slug,
            )
            return content  # Return unchanged

        gh_org, gh_repo = resolved
        is_ssh = url.startswith("ssh://")
        if is_ssh and gh_ssh_host:
            new_url = f"ssh://git@{gh_ssh_host}/{gh_org}/{gh_repo}.git"
        else:
            new_url = f"{gh_https_base}/{gh_org}/{gh_repo}.git"
        replacements[url] = new_url

    if not replacements:
        return content

    # Second pass: apply all replacements
    new_content = content
    for old_url, new_url in replacements.items():
        new_content = new_content.replace(old_url, new_url)

    return new_content


def remap_submodules_in_bare_repo(bare_repo_path, config):
    """Rewrite .gitmodules in all branches of a bare repo.

    Uses git plumbing to create deterministic commits (fixed timestamp)
    so repeated runs produce identical hashes when nothing changed on
    the source side — avoiding unnecessary force-pushes.

    Returns the number of branches remapped.
    """
    try:
        output = _git(
            ["for-each-ref", "--format=%(refname)", "refs/heads/"],
            cwd=bare_repo_path,
        )
    except subprocess.CalledProcessError:
        return 0

    if not output.strip():
        return 0

    remapped = 0
    for ref in output.strip().splitlines():
        if _remap_branch(bare_repo_path, ref, config):
            remapped += 1

    if remapped:
        logger.info(
            "Remapped submodule URLs on %d branch(es) in %s",
            remapped, os.path.basename(bare_repo_path),
        )

    return remapped


def _remap_branch(bare_repo_path, ref, config):
    """Remap .gitmodules on a single branch ref. Returns True if changed."""
    try:
        content = _git(["show", f"{ref}:.gitmodules"], cwd=bare_repo_path)
    except subprocess.CalledProcessError:
        return False

    new_content = remap_submodule_urls(content, config)
    if new_content == content:
        return False

    blob_hash = _git(
        ["hash-object", "-w", "--stdin"],
        cwd=bare_repo_path, stdin_data=new_content,
    )

    tree_listing = _git(["ls-tree", ref], cwd=bare_repo_path)
    new_lines = []
    for line in tree_listing.splitlines():
        if "\t.gitmodules" in line:
            meta, _ = line.split("\t", 1)
            parts = meta.split()
            new_lines.append(f"{parts[0]} {parts[1]} {blob_hash}\t.gitmodules")
        else:
            new_lines.append(line)

    new_tree = _git(
        ["mktree"],
        cwd=bare_repo_path, stdin_data="\n".join(new_lines) + "\n",
    )

    parent = _git(["rev-parse", ref], cwd=bare_repo_path)
    new_commit = _git(
        ["commit-tree", new_tree, "-p", parent, "-m", _COMMIT_MSG],
        cwd=bare_repo_path, env_extra=_REMAP_ENV,
    )

    _git(["update-ref", ref, new_commit], cwd=bare_repo_path)
    logger.debug("Remapped submodules on %s", ref)
    return True
