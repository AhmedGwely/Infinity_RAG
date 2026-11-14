"""Install light stand-ins for third-party packages that are absent in minimal sandboxes.

In the Docker image the real packages exist, so this module is a no-op there.
"""
import sys
import types


class _Any:
    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, n): return _Any()
    def __getitem__(self, k): return _Any()


class _Module(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


class _DeclarativeBase:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def install():
    import importlib.util
    for name in ["sqlalchemy", "sqlalchemy.orm", "sqlalchemy.dialects", "sqlalchemy.dialects.postgresql", "qdrant_client", "httpx", "redis"]:
        top = name.split(".")[0]
        if sys.modules.get(top) is not None and not isinstance(sys.modules[top], _Module):
            continue  # real package already imported
        if not isinstance(sys.modules.get(top), _Module) and importlib.util.find_spec(top) is not None:
            continue  # real package is installed
        module = _Module(name)
        module.__path__ = []  # behave like a package so submodule imports resolve
        if name == "sqlalchemy.orm":
            module.DeclarativeBase = _DeclarativeBase
        sys.modules[name] = module


install()
