from setuptools import setup, find_packages

setup(
    name="bb2gh",
    version="1.0.0",
    description="Bitbucket Server to GitHub Enterprise migration tool",
    packages=find_packages(),
    install_requires=[
        "click>=8.0",
        "requests>=2.28",
        "PyGithub>=1.59",
        "pyyaml>=6.0",
        "gitpython>=3.1",
    ],
    entry_points={
        "console_scripts": [
            "bb2gh=bb2gh.cli:cli",
        ],
    },
    python_requires=">=3.9",
)
