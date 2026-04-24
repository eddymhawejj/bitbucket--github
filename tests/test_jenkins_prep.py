"""Tests for Jenkins workspace preparation."""

from unittest.mock import MagicMock

import pytest

from bb2gh.jenkins_prep import find_jenkins_files, parse_jenkinsfile_refs


class TestFindJenkinsFiles:
    def test_finds_jenkinsfile(self):
        paths = ["README.md", "Jenkinsfile", "src/main.py"]
        jf, deps = find_jenkins_files(paths)
        assert jf == ["Jenkinsfile"]
        assert deps == []

    def test_finds_jenkinsfile_case_insensitive(self):
        paths = ["jenkinsfile", "JENKINSFILE"]
        jf, deps = find_jenkins_files(paths)
        assert len(jf) == 2

    def test_finds_jenkinsfile_with_suffix(self):
        paths = ["Jenkinsfile.deploy", "Jenkinsfile.staging", "README.md"]
        jf, deps = find_jenkins_files(paths)
        assert len(jf) == 2
        assert "Jenkinsfile.deploy" in jf
        assert "Jenkinsfile.staging" in jf

    def test_finds_dot_jenkinsfile(self):
        paths = ["pipelines/build.jenkinsfile", "deploy.jenkinsfile"]
        jf, deps = find_jenkins_files(paths)
        assert len(jf) == 2

    def test_finds_nested_jenkinsfile(self):
        paths = ["ci/Jenkinsfile", "ci/pipelines/Jenkinsfile.nightly"]
        jf, deps = find_jenkins_files(paths)
        assert len(jf) == 2

    def test_finds_vars_directory(self):
        paths = ["Jenkinsfile", "vars/myHelper.groovy", "vars/deploy.groovy"]
        jf, deps = find_jenkins_files(paths)
        assert jf == ["Jenkinsfile"]
        assert "vars/myHelper.groovy" in deps
        assert "vars/deploy.groovy" in deps

    def test_finds_src_groovy(self):
        paths = ["Jenkinsfile", "src/org/company/Pipeline.groovy"]
        jf, deps = find_jenkins_files(paths)
        assert "src/org/company/Pipeline.groovy" in deps

    def test_finds_resources(self):
        paths = ["Jenkinsfile", "resources/config.yaml"]
        jf, deps = find_jenkins_files(paths)
        assert "resources/config.yaml" in deps

    def test_finds_colocated_groovy(self):
        paths = ["ci/Jenkinsfile", "ci/helpers.groovy", "other/utils.groovy"]
        jf, deps = find_jenkins_files(paths)
        assert "ci/helpers.groovy" in deps
        assert "other/utils.groovy" not in deps

    def test_no_jenkinsfiles(self):
        paths = ["README.md", "src/main.py", "Makefile"]
        jf, deps = find_jenkins_files(paths)
        assert jf == []
        assert deps == []

    def test_empty_paths(self):
        jf, deps = find_jenkins_files([])
        assert jf == []
        assert deps == []


class TestParseJenkinsfileRefs:
    def test_extracts_load_single_quotes(self):
        content = "load 'scripts/deploy.groovy'"
        refs = parse_jenkinsfile_refs(content)
        assert "scripts/deploy.groovy" in refs

    def test_extracts_load_double_quotes(self):
        content = 'load "scripts/deploy.groovy"'
        refs = parse_jenkinsfile_refs(content)
        assert "scripts/deploy.groovy" in refs

    def test_extracts_readfile(self):
        content = "def config = readFile('config/settings.yaml')"
        refs = parse_jenkinsfile_refs(content)
        assert "config/settings.yaml" in refs

    def test_extracts_evaluate_readfile(self):
        content = """evaluate(readFile('scripts/helper.groovy'))"""
        refs = parse_jenkinsfile_refs(content)
        assert "scripts/helper.groovy" in refs

    def test_multiple_references(self):
        content = """
        load 'scripts/build.groovy'
        def cfg = readFile('config.yaml')
        load "scripts/deploy.groovy"
        """
        refs = parse_jenkinsfile_refs(content)
        assert len(refs) == 3
        assert "scripts/build.groovy" in refs
        assert "config.yaml" in refs
        assert "scripts/deploy.groovy" in refs

    def test_no_references(self):
        content = """
        pipeline {
            agent any
            stages {
                stage('Build') {
                    steps { sh 'make build' }
                }
            }
        }
        """
        refs = parse_jenkinsfile_refs(content)
        assert refs == set()

    def test_empty_content(self):
        refs = parse_jenkinsfile_refs("")
        assert refs == set()
