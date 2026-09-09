"""Stay inside Google API quotas instead of recovering from 429 responses."""

from importlib.metadata import PackageNotFoundError, version

from googleapis_without_429.core import WeightedSlidingWindow
from googleapis_without_429.errors import QuotaTimeoutError, is_rate_limited
from googleapis_without_429.limiter import QuotaLimiter
from googleapis_without_429.profiles import DRIVE, SHEETS, ApiProfile
from googleapis_without_429.session import RateLimitedSession

try:
    __version__ = version("googleapis-without-429")
except PackageNotFoundError:  # pragma: no cover - only when running from source
    __version__ = "0.0.0.dev0"

__all__ = [
    "DRIVE",
    "SHEETS",
    "ApiProfile",
    "QuotaLimiter",
    "QuotaTimeoutError",
    "RateLimitedSession",
    "WeightedSlidingWindow",
    "__version__",
    "is_rate_limited",
]
