"""Tests for submodule URL remapping."""

from unittest.mock import MagicMock

import pytest

from bb2gh.config import Config
from bb2gh.submodules import remap_submodule_urls


@pytest.fixture
def mock_config():
    config = MagicMock(spec=Config)
    config.bb_ssh_url = "ssh://git@cph1-eud-rep001:7999"
    config.bb_base_url = "https://cph1-eud-rep001"
    config.gh_base_url = "https://gatehousesatcom.ghe.com/api/v3"
    config.bb_projects = ["SYS_YAHSAT_NGSP", "DCKR", "NGRM"]
    config.bb_verify_ssl = True
    config.should_migrate_repo = MagicMock(return_value=True)
    config.resolve_target = MagicMock(
        side_effect=lambda proj, slug: ("networks-ngsp", slug)
    )
    return config


SAMPLE_GITMODULES_SSH = """\
[submodule "cai_lib"]
\tpath = cai_lib
\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/cai_lib.git
[submodule "cai_def"]
\tpath = cai_def
\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/cai_def.git
"""

SAMPLE_GITMODULES_HTTP = """\
[submodule "cai_lib"]
\tpath = cai_lib
\turl = https://cph1-eud-rep001/scm/sys_yahsat_ngsp/cai_lib.git
"""


class TestRemapSubmoduleUrls:
    def test_remaps_ssh_urls(self, mock_config):
        result = remap_submodule_urls(SAMPLE_GITMODULES_SSH, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result
        assert "gatehousesatcom.ghe.com/networks-ngsp/cai_def.git" in result
        assert "cph1-eud-rep001" not in result

    def test_remaps_http_urls(self, mock_config):
        result = remap_submodule_urls(SAMPLE_GITMODULES_HTTP, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result
        assert "cph1-eud-rep001" not in result

    def test_preserves_non_url_lines(self, mock_config):
        result = remap_submodule_urls(SAMPLE_GITMODULES_SSH, mock_config)

        assert '[submodule "cai_lib"]' in result
        assert "\tpath = cai_lib" in result

    def test_leaves_unknown_project_urls(self, mock_config):
        mock_config.bb_projects = ["SYS_YAHSAT_NGSP"]
        content = (
            "[submodule \"other\"]\n"
            "\tpath = other\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/unknown_proj/other.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "cph1-eud-rep001:7999/unknown_proj/other.git" in result

    def test_leaves_excluded_repo_urls(self, mock_config):
        mock_config.should_migrate_repo = MagicMock(
            side_effect=lambda proj, slug: slug != "excluded-repo"
        )
        content = (
            "[submodule \"included\"]\n"
            "\tpath = included\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/included.git\n"
            "[submodule \"excluded\"]\n"
            "\tpath = excluded\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/excluded-repo.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/included.git" in result
        assert "cph1-eud-rep001:7999/sys_yahsat_ngsp/excluded-repo.git" in result

    def test_handles_uppercase_project_in_url(self, mock_config):
        content = (
            "[submodule \"cai_lib\"]\n"
            "\tpath = cai_lib\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/SYS_YAHSAT_NGSP/cai_lib.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result

    def test_no_changes_returns_same_content(self, mock_config):
        content = (
            "[submodule \"lib\"]\n"
            "\tpath = lib\n"
            "\turl = https://github.com/some/other.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert result == content

    def test_empty_content(self, mock_config):
        assert remap_submodule_urls("", mock_config) == ""

    def test_mixed_ssh_and_http(self, mock_config):
        content = (
            "[submodule \"a\"]\n"
            "\tpath = a\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/repo_a.git\n"
            "[submodule \"b\"]\n"
            "\tpath = b\n"
            "\turl = https://cph1-eud-rep001/scm/sys_yahsat_ngsp/repo_b.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
        assert "gatehousesatcom.ghe.com/networks-ngsp/repo_b.git" in result
        assert "cph1-eud-rep001" not in result

    def test_all_projects_when_bb_projects_is_none(self, mock_config):
        """When bb_projects is None (migrate all), remap all URLs."""
        mock_config.bb_projects = None
        content = (
            "[submodule \"x\"]\n"
            "\tpath = x\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/any_project/some_repo.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/some_repo.git" in result

    def test_uses_correct_org_per_project(self, mock_config):
        """Different projects should resolve to different GitHub orgs."""
        def resolve(proj, slug):
            orgs = {"SYS_YAHSAT_NGSP": "networks-ngsp", "DCKR": "networks-docker"}
            return orgs.get(proj, "default-org"), slug

        mock_config.resolve_target = MagicMock(side_effect=resolve)

        content = (
            "[submodule \"a\"]\n"
            "\tpath = a\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/repo_a.git\n"
            "[submodule \"b\"]\n"
            "\tpath = b\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/dckr/repo_b.git\n"
        )

        result = remap_submodule_urls(content, mock_config)

        assert "gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
        assert "gatehousesatcom.ghe.com/networks-docker/repo_b.git" in result
