"""ai-common-repo gateway package.

Public surface kept small and explicit so adapters/CLI import a stable API."""
from __future__ import annotations

from .core.config import AppConfig, load_config
from .service import MemoryService

__all__ = ["AppConfig", "load_config", "MemoryService", "__version__"]
__version__ = "0.1.0"
