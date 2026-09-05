"""CodeRouter — local-first, free-first, fallback-built-in LLM router."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from coderouter.errors import CodeRouterError

try:
    # Fork: distribution is `coderouter-t` (translate). Fall back to
    # `coderouter-cli` for backward compat when running from an older
    # install. Import name stays `coderouter`.
    try:
        __version__ = _pkg_version("coderouter-t")
    except PackageNotFoundError:
        __version__ = _pkg_version("coderouter-cli")
except PackageNotFoundError:  # pragma: no cover — package not installed (e.g. raw source checkout)
    __version__ = "0.0.0+unknown"

__all__ = ["CodeRouterError", "__version__"]
