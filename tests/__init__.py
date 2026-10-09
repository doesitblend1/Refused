"""Test package. If httpx is not installed (e.g. a sandbox with only the
drop-in httpx2 fork), alias it so the production code imports unchanged."""
import sys

try:
    import httpx  # noqa: F401
except ImportError:  # pragma: no cover
    import httpx2

    sys.modules["httpx"] = httpx2
