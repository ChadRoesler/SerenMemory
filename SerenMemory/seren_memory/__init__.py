"""
SerenMemory - the right brain. Three-tier LLM memory with consolidation.

The Halls of Memory: ShortTerm (working), NearTerm (open loops), LongTerm
(consolidated). A small "consolidator" model does the dream-work of
promoting what matters and letting the rest go.

Standalone. Bring your own LLM (any OpenAI-compatible endpoint). Configure
a couple of values and you've got a memory system that matters.
"""
from __future__ import annotations

# Version flows from the git tag via setuptools-scm (written to _version.py at
# build time, read here). Fallback only fires in a bare source checkout that was
# never built. Mirrors SerenLoci/SCC so the family exposes __version__ alike.
try:
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout without a build
    __version__ = "0.0.0+unknown"

from .app import create_app  # noqa: F401,E402
from .config import load_config, MemoryConfig  # noqa: F401,E402