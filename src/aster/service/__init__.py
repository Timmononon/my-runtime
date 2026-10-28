__all__ = ["ConfigurationError", "RunCoordinator"]


def __getattr__(name):
    if name in __all__:
        from .coordinator import ConfigurationError, RunCoordinator
        return {"ConfigurationError": ConfigurationError, "RunCoordinator": RunCoordinator}[name]
    raise AttributeError(name)
