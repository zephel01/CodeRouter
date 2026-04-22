"""CodeRouter — local-first, free-first, fallback-built-in LLM router."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from coderouter.errors import CodeRouterError

try:
    __version__ = _pkg_version("coderouter")
except PackageNotFoundError:  # pragma: no cover — package not installed (e.g. raw source checkout)
    __version__ = "0.0.0+unknown"

__all__ = ["CodeRouterError", "__version__"]
