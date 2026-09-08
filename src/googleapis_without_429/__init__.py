"""Stay inside Google API quotas instead of recovering from 429 responses."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("googleapis-without-429")
except PackageNotFoundError:  # pragma: no cover - only when running from source
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
