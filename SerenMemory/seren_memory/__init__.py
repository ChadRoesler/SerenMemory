"""
SerenMemory - three-tier LLM memory with consolidation.

The Halls of Memory: ShortTerm (working), NearTerm (open loops), LongTerm
(consolidated). A small "consolidator" model does the dream-work of
promoting what matters and letting the rest go.

Standalone. Bring your own LLM (any OpenAI-compatible endpoint). Configure
a couple of values and you've got a memory system that matters.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Version flows from the git tag via setuptools-scm (written to _version.py
# at build time and recorded in the installed package's metadata). No manual
# bump, no sed step. The fallback only fires in a bare source checkout that
# was never installed; do `pip install -e .` to resolve it.
try:
    __version__: str = _pkg_version("seren-memory")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"

from .app import create_app  # noqa: F401,E402
from .config import load_config, MemoryConfig  # noqa: F401,E402