"""Command-line entry point for apt-explorer.

Usage examples::

    apt-explorer                                # interactive, Debian default
    apt-explorer --repo http://deb.debian.org/debian --suite bookworm
    apt-explorer --preset ubuntu --suite jammy
    apt-explorer --repo <url> --suite sid --list-packages main amd64
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .repo import AptRepo, RepoError, download_deb, human_size

# Known public repositories and sensible default suites.
PRESETS: dict[str, dict] = {
    "debian": {
        "url": "http://deb.debian.org/debian",
        "suites": ["stable", "testing", "unstable", "bookworm", "bullseye"],
    },
    "debian-security": {
        "url": "http://security.debian.org/debian-security",
        "suites": ["bookworm-security", "bullseye-security"],
    },
    "ubuntu": {
        "url": "http://archive.ubuntu.com/ubuntu",
        "suites": ["noble", "jammy", "focal", "mantic"],
    },
}

DEFAULT_PRESET = "debian"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apt-explorer",
        description="Interactive TUI to explore APT repositories and "
        "download .deb packages.",
    )
    p.add_argument(
        "--repo",
        help="Base URL of the APT repo (e.g. http://deb.debian.org/debian). "
        "Overrides --preset.",
    )
    p.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help=f"Use a known repository preset (default: {DEFAULT_PRESET}).",
    )
    p.add_argument(
        "--suite",
        action="append",
        dest="suites",
        help="Suite/distribution to show (repeatable). "
        "Defaults to the preset's suites.",
    )
    p.add_argument(
        "-d",
        "--dest",
        default=os.path.join(os.getcwd(), "downloads"),
        help="Directory to save downloaded .deb files "
        "(default: ./downloads).",
    )
    p.add_argument(
        "--timeout", type=int, default=30, help="Network timeout in seconds."
    )
    p.add_argument(
        "--version", action="version", version=f"apt-explorer {__version__}"
    )

    # Non-interactive helpers (handy for scripting / testing).
    p.add_argument(
        "--list-suites",
        action="store_true",
        help="Print the configured suites and exit.",
    )
    p.add_argument(
        "--list-packages",
        nargs=2,
        metavar=("COMPONENT", "ARCH"),
        help="Non-interactive: list package names for the first suite's "
        "COMPONENT/ARCH and exit.",
    )
    p.add_argument(
        "--download",
        nargs=4,
        metavar=("COMPONENT", "ARCH", "PACKAGE", "VERSION"),
        help="Non-interactive: download a specific package version and exit. "
        "Use VERSION='latest' for the newest.",
    )
    return p


def _resolve_config(args) -> tuple[str, list[str]]:
    if args.repo:
        url = args.repo
        suites = args.suites or ["stable"]
    else:
        preset_name = args.preset or DEFAULT_PRESET
        preset = PRESETS[preset_name]
        url = preset["url"]
        suites = args.suites or list(preset["suites"])
    return url, suites


def _cmd_list_packages(repo: AptRepo, suite: str, component: str, arch: str) -> int:
    try:
        names = repo.package_names(suite, component, arch)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for n in names:
        print(n)
    print(f"# {len(names)} packages in {suite}/{component}/{arch}",
          file=sys.stderr)
    return 0


def _cmd_download(
    repo: AptRepo,
    suite: str,
    component: str,
    arch: str,
    name: str,
    version: str,
    dest: str,
) -> int:
    try:
        versions = repo.versions_of(suite, component, arch, name)
    except RepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not versions:
        print(f"error: package '{name}' not found", file=sys.stderr)
        return 1
    if version == "latest":
        pkg = versions[0]
    else:
        pkg = next((p for p in versions if p.version == version), None)
        if pkg is None:
            avail = ", ".join(p.version for p in versions[:10])
            print(
                f"error: version '{version}' not found for {name}. "
                f"Available: {avail}",
                file=sys.stderr,
            )
            return 1

    def prog(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        print(
            f"\r{name} {pkg.version}: {pct}% "
            f"({human_size(done)}/{human_size(total)})",
            end="",
            file=sys.stderr,
            flush=True,
        )

    try:
        result = download_deb(repo, pkg, dest, progress=prog)
    except RepoError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    verify = (
        f"verified ({result.checksum_kind})"
        if result.verified
        else "unverified"
    )
    print(f"\nSaved {result.path} [{human_size(result.size)}, {verify}]",
          file=sys.stderr)
    print(result.path)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    url, suites = _resolve_config(args)
    repo = AptRepo(url, timeout=args.timeout)

    if args.list_suites:
        for s in suites:
            print(s)
        return 0

    if args.list_packages:
        component, arch = args.list_packages
        return _cmd_list_packages(repo, suites[0], component, arch)

    if args.download:
        component, arch, name, version = args.download
        return _cmd_download(
            repo, suites[0], component, arch, name, version, args.dest
        )

    # Interactive TUI.
    try:
        from .tui import run
    except Exception as exc:  # e.g. curses unavailable
        print(f"error: cannot start TUI: {exc}", file=sys.stderr)
        return 1

    print(f"apt-explorer {__version__}  repo={url}", file=sys.stderr)
    print(f"suites: {', '.join(suites)}", file=sys.stderr)
    print("Starting interactive browser...", file=sys.stderr)
    try:
        run(repo, suites, args.dest)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
