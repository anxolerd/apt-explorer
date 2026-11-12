"""apt-explorer: an interactive TUI for browsing APT repositories."""

from .repo import AptRepo, Package, Release, RepoError, download_deb, human_size

__version__ = "1.0.0"
__all__ = [
    "AptRepo",
    "Package",
    "Release",
    "RepoError",
    "download_deb",
    "human_size",
]
