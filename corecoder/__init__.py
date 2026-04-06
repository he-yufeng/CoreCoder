"""CoreCoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.1.0"

from corecoder.agent import Agent
from corecoder.llm import LLM
from corecoder.config import Config
from corecoder.tools import ALL_TOOLS

__all__ = ["Agent", "LLM", "Config", "ALL_TOOLS", "__version__"]
