"""Submodule URL remapping for Bitbucket-to-GitHub migration."""

import logging
import os
import re
import subprocess

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


def remap_submodule_urls(content, config):
    """Replace Bitbucket submodule URLs in .gitmodules with GitHub URLs.

    Only remaps URLs for repos that are in the migration scope
    (matching project list and include/exclude filters).
    """
    bb_ssh = config.bb_ssh_url.rstrip("/")
    bb_http = config.bb_base_url.rstrip("/")
    gh_base = config.gh_base_url.replace("/api/v3", "").rstrip("/")

    def _replace(match):
        project_key_raw = match.group(1)
        slug = match.group(2)
        for pk in [project_key_raw.upper(), project_key_raw]:
            if config.bb_projects and pk not in config.bb_projects:
                continue
            if not config.should_migrate_repo(pk, slug):
                continue
            gh_org, gh_repo = config.resolve_target(pk, slug)
            return f"{gh_base}/{gh_org}/{gh_repo}.git"
        return match.group(0)

    ssh_pat = re.escape(bb_ssh) + r"/([^/]+)/([^/]+?)\.git"
    content = re.sub(ssh_pat, _replace, content)

    http_pat = re.escape(bb_http) + r"/scm/([^/]+)/([^/]+?)\.git"
    content = re.sub(http_pat, _replace, content)

    return content


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
