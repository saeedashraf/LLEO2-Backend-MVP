"""
LLEO v2 — Large Language Models for Earth Observation
A modular satellite imagery analysis package

v2 changes:
- Single-call Pydantic router (1 LLM call instead of 4-5)
- Modular routers (chat, results, billing, health)
- Centralized services layer
- Same API contract — zero frontend changes
"""

__version__ = "2.0.0"
__author__ = "Sureal.ai"

from .core.base_processor import BaseProcessor
from .processors import get_processor, register_processor, PROCESSORS

__all__ = [
    "BaseProcessor",
    "get_processor",
    "register_processor",
    "PROCESSORS",
    "__version__",
]
