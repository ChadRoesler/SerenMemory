"""
SerenMemory - three-tier LLM memory with consolidation.

The Halls of Memory: ShortTerm (working), NearTerm (open loops), LongTerm
(consolidated). A small "consolidator" model does the dream-work of
promoting what matters and letting the rest go.

Standalone. Bring your own LLM (any OpenAI-compatible endpoint). Configure
a couple of values and you've got a memory system that matters.
"""
__version__ = "1.1.0"

from .app import create_app  # noqa: F401
from .config import load_config, MemoryConfig  # noqa: F401
