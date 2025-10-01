from __future__ import annotations

import sys
from pathlib import Path

import tomllib

# Resolve project root and ensure src/ is importable for autodoc
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

with (ROOT_DIR / "pyproject.toml").open("rb") as pyproject_file:
    project_metadata = tomllib.load(pyproject_file)

# Project information
project = "NyxMon"
author = "Jochen Wersdörfer"
release = project_metadata["project"]["version"]
version = release

# General configuration
extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinxcontrib.mermaid",
]

myst_enable_extensions = [
    "deflist",
    "tasklist",
    "html_image",
    "colon_fence",
    "fieldlist",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Options for HTML output
html_theme = "furo"
html_title = "NyxMon Documentation"
html_static_path = ["_static"]
html_css_files: list[str] = []

# Force the sidebar to show the global TOC on all pages
html_sidebars = {
    "**": [
        "sidebar/brand.html",
        "sidebar/search.html",
        "sidebar/scroll-start.html",
        "sidebar/navigation.html",
        "sidebar/scroll-end.html",
    ]
}

html_theme_options = {
    "source_repository": "https://github.com/ephes/nyxmon",
    "source_branch": "main",
    "source_directory": "docs/",
    "navigation_with_keys": True,
    "sidebar_hide_name": False,
}
