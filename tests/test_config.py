"""Tests for the config module, especially repo_mapping resolution."""

import os
import tempfile

import pytest
import yaml

from bb2gh.config import Config


def _write_config(tmp_path, data):
    """Write a config dict to a YAML file and return the path."""
    path = tmp_path / "config.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return str(path)


@pytest.fixture
def base_config():
    """Minimal valid config dict."""
    return {
        "bitbucket": {
            "base_url": "https://bitbucket.example.com",
            "ssh_url": "ssh://git@bitbucket.example.com:7999",
            "token": "fake",
        },
        "github": {
            "base_url": "https://github.example.com/api/v3",
            "org": "default-org",
            "token": "fake",
        },
    }


class TestResolveTarget:
    def test_defaults_to_slug_and_default_org(self, tmp_path, base_config):
        """Without repo_mapping, returns default org and slug as-is."""
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        org, name = config.resolve_target("PROJ", "my-repo")
        assert org == "default-org"
        assert name == "my-repo"

    def test_global_name_template(self, tmp_path, base_config):
        """Global name_template applies to all repos."""
        base_config["repo_mapping"] = {
            "name_template": "{project_lower}-{slug}",
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        org, name = config.resolve_target("INFRA", "my-service")
        assert org == "default-org"
        assert name == "infra-my-service"

    def test_per_project_org(self, tmp_path, base_config):
        """Per-project github_org overrides the default org."""
        base_config["repo_mapping"] = {
            "projects": {
                "INFRA": {"github_org": "infra-team"},
            }
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        org, name = config.resolve_target("INFRA", "my-service")
        assert org == "infra-team"
        assert name == "my-service"  # default template is "{slug}"

    def test_per_project_name_template(self, tmp_path, base_config):
        """Per-project name_template overrides the global template."""
        base_config["repo_mapping"] = {
            "name_template": "{project_lower}-{slug}",
            "projects": {
                "INFRA": {
                    "github_org": "infra-team",
                    "name_template": "infra-{slug}",
                },
            },
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        # INFRA uses its own template
        org, name = config.resolve_target("INFRA", "my-service")
        assert org == "infra-team"
        assert name == "infra-my-service"

        # Other projects use the global template
        org, name = config.resolve_target("PLATFORM", "api")
        assert org == "default-org"
        assert name == "platform-api"

    def test_per_repo_override(self, tmp_path, base_config):
        """Explicit per-repo github_name overrides all templates."""
        base_config["repo_mapping"] = {
            "name_template": "{project_lower}-{slug}",
            "projects": {
                "INFRA": {
                    "github_org": "infra-team",
                    "repos": {
                        "legacy-monolith": {"github_name": "the-monolith"},
                    },
                },
            },
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        # Explicit override
        org, name = config.resolve_target("INFRA", "legacy-monolith")
        assert org == "infra-team"
        assert name == "the-monolith"

        # Non-overridden repo in same project uses global template
        # (no per-project template set, so falls back to global)
        org, name = config.resolve_target("INFRA", "other-repo")
        assert org == "infra-team"
        assert name == "infra-other-repo"

    def test_unmapped_project_uses_defaults(self, tmp_path, base_config):
        """Projects not listed in repo_mapping use the default org and global template."""
        base_config["repo_mapping"] = {
            "name_template": "{project_lower}-{slug}",
            "projects": {
                "INFRA": {"github_org": "infra-team"},
            },
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        org, name = config.resolve_target("OTHER", "some-repo")
        assert org == "default-org"
        assert name == "other-some-repo"

    def test_template_with_project_variable(self, tmp_path, base_config):
        """Template can use {project} (original case)."""
        base_config["repo_mapping"] = {
            "name_template": "{project}-{slug}",
        }
        path = _write_config(tmp_path, base_config)
        config = Config(path)

        org, name = config.resolve_target("MyProject", "api")
        assert name == "MyProject-api"


class TestConfigValidation:
    def test_missing_bitbucket_section(self, tmp_path):
        data = {"github": {"base_url": "x", "org": "y"}}
        path = _write_config(tmp_path, data)
        with pytest.raises(ValueError, match="bitbucket"):
            Config(path)

    def test_missing_github_section(self, tmp_path):
        data = {"bitbucket": {"base_url": "x", "ssh_url": "y"}}
        path = _write_config(tmp_path, data)
        with pytest.raises(ValueError, match="github"):
            Config(path)
