"""Model protocol adapters. No network activity occurs at import time."""

from .deepseek import DeepSeekResponsesClient, ProviderError

__all__ = ["DeepSeekResponsesClient", "ProviderError"]
