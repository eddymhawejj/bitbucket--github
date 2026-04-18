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
    config.bb_ssh_hostnames = ["cph1-eud-rep001.satcom.global"]
    config.gh_base_url = "https://gatehousesatcom.ghe.com/api/v3"
    config.bb_projects = ["SYS_YAHSAT_NGSP", "SYS_COM", "DCKR", "NGRM"]
    config.bb_verify_ssl = True
    config.gh_ssh_host = "gatehousesatcom.ghe.com"
    config.should_migrate_repo = MagicMock(return_value=True)
    config.resolve_target = MagicMock(
        side_effect=lambda proj, slug: ("networks-ngsp", slug)
    )
    return config


class TestRemapSubmoduleUrls:
    def test_remaps_ssh_urls_to_ssh(self, mock_config):
        content = (
            "[submodule \"cai_lib\"]\n"
            "\tpath = cai_lib\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/cai_lib.git\n"
            "[submodule \"cai_def\"]\n"
            "\tpath = cai_def\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/cai_def.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result
        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/cai_def.git" in result
        assert "cph1-eud-rep001" not in result

    def test_remaps_fqdn_hostname_variant(self, mock_config):
        """URLs using the FQDN variant should also be matched."""
        content = (
            "[submodule \"lib\"]\n"
            "\tpath = lib\n"
            "\turl = ssh://git@cph1-eud-rep001.satcom.global:7999/sys_yahsat_ngsp/lib.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/lib.git" in result
        assert "cph1-eud-rep001" not in result

    def test_remaps_http_urls_to_https(self, mock_config):
        content = (
            "[submodule \"cai_lib\"]\n"
            "\tpath = cai_lib\n"
            "\turl = https://cph1-eud-rep001/scm/sys_yahsat_ngsp/cai_lib.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "https://gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result

    def test_preserves_non_url_lines(self, mock_config):
        content = (
            "[submodule \"cai_lib\"]\n"
            "\tpath = cai_lib\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/cai_lib.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert '[submodule "cai_lib"]' in result
        assert "\tpath = cai_lib" in result

    def test_skips_entirely_when_url_unresolvable(self, mock_config):
        """If any BB URL can't be resolved, return content unchanged."""
        mock_config.bb_projects = ["SYS_YAHSAT_NGSP"]
        content = (
            "[submodule \"known\"]\n"
            "\tpath = known\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/known.git\n"
            "[submodule \"unknown\"]\n"
            "\tpath = unknown\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/other_project/unknown.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        # Entire file should be unchanged
        assert result == content

    def test_skips_entirely_when_excluded_repo(self, mock_config):
        """If any BB URL points to an excluded repo, skip entirely."""
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

        assert result == content

    def test_ignores_already_github_urls(self, mock_config):
        """URLs already pointing to GitHub should be ignored."""
        content = (
            "[submodule \"bb_repo\"]\n"
            "\tpath = bb_repo\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/bb_repo.git\n"
            "[submodule \"gh_repo\"]\n"
            "\tpath = gh_repo\n"
            "\turl = ssh://git@gatehousesatcom.ghe.com/networks-ngsp/gh_repo.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/bb_repo.git" in result
        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/gh_repo.git" in result

    def test_ignores_external_urls(self, mock_config):
        """URLs pointing to external hosts (not BB) should be left alone."""
        content = (
            "[submodule \"bb_repo\"]\n"
            "\tpath = bb_repo\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/bb_repo.git\n"
            "[submodule \"external\"]\n"
            "\tpath = external\n"
            "\turl = https://github.com/some/external.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/bb_repo.git" in result
        assert "https://github.com/some/external.git" in result

    def test_handles_uppercase_project_in_url(self, mock_config):
        content = (
            "[submodule \"cai_lib\"]\n"
            "\tpath = cai_lib\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/SYS_YAHSAT_NGSP/cai_lib.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/cai_lib.git" in result

    def test_empty_content(self, mock_config):
        assert remap_submodule_urls("", mock_config) == ""

    def test_no_matching_urls(self, mock_config):
        content = (
            "[submodule \"lib\"]\n"
            "\tpath = lib\n"
            "\turl = https://github.com/some/other.git\n"
        )
        result = remap_submodule_urls(content, mock_config)
        assert result == content

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

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
        assert "https://gatehousesatcom.ghe.com/networks-ngsp/repo_b.git" in result
        assert "cph1-eud-rep001" not in result

    def test_mixed_hostnames_all_resolved(self, mock_config):
        """Both short and FQDN hostname variants should be resolved together."""
        content = (
            "[submodule \"a\"]\n"
            "\tpath = a\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/repo_a.git\n"
            "[submodule \"b\"]\n"
            "\tpath = b\n"
            "\turl = ssh://git@cph1-eud-rep001.satcom.global:7999/sys_com/repo_b.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/repo_b.git" in result
        assert "cph1-eud-rep001" not in result

    def test_uses_correct_org_per_project(self, mock_config):
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

        assert "ssh://git@gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
        assert "ssh://git@gatehousesatcom.ghe.com/networks-docker/repo_b.git" in result

    def test_falls_back_to_https_when_no_ssh_host(self, mock_config):
        mock_config.gh_ssh_host = ""
        content = (
            "[submodule \"a\"]\n"
            "\tpath = a\n"
            "\turl = ssh://git@cph1-eud-rep001:7999/sys_yahsat_ngsp/repo_a.git\n"
        )
        result = remap_submodule_urls(content, mock_config)

        assert "https://gatehousesatcom.ghe.com/networks-ngsp/repo_a.git" in result
