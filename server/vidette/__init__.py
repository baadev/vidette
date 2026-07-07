"""Vidette — self-hosted video security that understands intent, not just motion."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vidette")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.1+dev"
