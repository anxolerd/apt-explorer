"""APT repository client.

Fetches and parses APT repository metadata using only the Python standard
library. An APT repository is laid out like this::

    <base_url>/dists/<suite>/Release
    <base_url>/dists/<suite>/<component>/binary-<arch>/Packages[.xz|.gz|.bz2]
    <base_url>/<Filename>        # the actual .deb, path given per-package

This module exposes a small, well-typed API that the TUI (or any other
front-end) can drive.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

USER_AGENT = "apt-explorer/1.0 (+https://localhost)"
DEFAULT_TIMEOUT = 30

# Preference order for compressed Packages indices. First that exists wins.
_INDEX_VARIANTS = (
    ("Packages.xz", "xz"),
    ("Packages.gz", "gz"),
    ("Packages.bz2", "bz2"),
    ("Packages", "none"),
)


class RepoError(Exception):
    """Raised for any repository access or parsing failure."""


# --------------------------------------------------------------------------
# Debian version comparison (dpkg --compare-versions semantics)
# --------------------------------------------------------------------------
# A Debian version is: [epoch:]upstream_version[-debian_revision]
# Comparison is NOT lexicographic (otherwise "9.5.9" > "13.3.0"). It follows
# the algorithm in deb-version(7): compare epochs numerically, then compare
# the upstream version and revision in alternating non-digit / digit chunks,
# where in non-digit chunks the character '~' sorts before everything
# (even the empty string) and letters sort before non-letters.


def _order(ch: str) -> int:
    """Ordering weight for a single character in a non-digit chunk."""
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    if ch == "":
        return 0
    # non-alphanumeric characters sort after letters
    return ord(ch) + 256


def _compare_fragment(a: str, b: str) -> int:
    """Compare two version fragments (upstream or revision) dpkg-style."""
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        # 1) compare the leading non-digit run
        while (i < la and not a[i].isdigit()) or (j < lb and not b[j].isdigit()):
            ca = a[i] if i < la and not a[i].isdigit() else ""
            cb = b[j] if j < lb and not b[j].isdigit() else ""
            oa, ob = _order(ca), _order(cb)
            if oa != ob:
                return -1 if oa < ob else 1
            if ca:
                i += 1
            if cb:
                j += 1
        # 2) compare the leading digit run numerically
        sa = i
        while i < la and a[i].isdigit():
            i += 1
        sb = j
        while j < lb and b[j].isdigit():
            j += 1
        na = int(a[sa:i]) if i > sa else 0
        nb = int(b[sb:j]) if j > sb else 0
        if na != nb:
            return -1 if na < nb else 1
    return 0


def compare_versions(v1: str, v2: str) -> int:
    """Return -1, 0, or 1 for v1 <, ==, or > v2 (Debian version ordering)."""
    def split(v: str) -> tuple[int, str, str]:
        epoch = 0
        if ":" in v:
            head, _, rest = v.partition(":")
            if head.isdigit():
                epoch = int(head)
                v = rest
        if "-" in v:
            upstream, _, revision = v.rpartition("-")
        else:
            upstream, revision = v, ""
        return epoch, upstream, revision

    e1, u1, r1 = split(v1)
    e2, u2, r2 = split(v2)
    if e1 != e2:
        return -1 if e1 < e2 else 1
    c = _compare_fragment(u1, u2)
    if c != 0:
        return c
    return _compare_fragment(r1, r2)


try:
    import functools

    _version_key = functools.cmp_to_key(compare_versions)
except Exception:  # pragma: no cover
    _version_key = None  # type: ignore


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _open(url: str, timeout: int = DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RepoError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RepoError(f"Cannot reach {url}: {exc.reason}") from exc
    except OSError as exc:
        raise RepoError(f"Network error for {url}: {exc}") from exc


def fetch_bytes(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Fetch a URL fully into memory."""
    with _open(url, timeout) as resp:
        return resp.read()


def _decompress(data: bytes, kind: str) -> bytes:
    if kind == "none":
        return data
    if kind == "gz":
        return gzip.decompress(data)
    if kind == "xz":
        return lzma.decompress(data)
    if kind == "bz2":
        return bz2.decompress(data)
    raise RepoError(f"Unknown compression: {kind}")


# --------------------------------------------------------------------------
# Deb822 (RFC822-style) stanza parsing
# --------------------------------------------------------------------------
def parse_stanzas(text: str) -> Iterator[dict[str, str]]:
    """Yield dicts for each blank-line-separated stanza in deb822 text.

    Handles folded (continuation) lines that begin with whitespace.
    """
    stanza: dict[str, str] = {}
    key: Optional[str] = None
    for raw in text.splitlines():
        if not raw.strip():
            if stanza:
                yield stanza
                stanza = {}
                key = None
            continue
        if raw[0] in " \t":
            # continuation of the previous field
            if key is not None:
                stanza[key] += "\n" + raw.strip()
            continue
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        key = k.strip()
        stanza[key] = v.strip()
    if stanza:
        yield stanza


# --------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------
@dataclass
class Package:
    """A single package version from a Packages index."""

    name: str
    version: str
    architecture: str
    filename: str  # path relative to repo base
    size: int
    sha256: Optional[str] = None
    md5sum: Optional[str] = None
    description: str = ""
    section: str = ""
    maintainer: str = ""
    depends: str = ""
    homepage: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def short_description(self) -> str:
        return self.description.splitlines()[0] if self.description else ""


@dataclass
class Release:
    """Parsed dists/<suite>/Release metadata."""

    suite: str
    origin: str = ""
    label: str = ""
    codename: str = ""
    components: list[str] = field(default_factory=list)
    architectures: list[str] = field(default_factory=list)
    description: str = ""


# --------------------------------------------------------------------------
# Repository client
# --------------------------------------------------------------------------
class AptRepo:
    """Client for a single APT repository base URL."""

    def __init__(self, base_url: str, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # caches
        self._release_cache: dict[str, Release] = {}
        self._packages_cache: dict[tuple[str, str, str], list[Package]] = {}

    # -- URL helpers -----------------------------------------------------
    def dist_url(self, suite: str, *parts: str) -> str:
        tail = "/".join(p.strip("/") for p in parts)
        base = f"{self.base_url}/dists/{suite.strip('/')}"
        return f"{base}/{tail}" if tail else base

    def deb_url(self, pkg: Package) -> str:
        return f"{self.base_url}/{pkg.filename.lstrip('/')}"

    # -- Release ---------------------------------------------------------
    def get_release(self, suite: str) -> Release:
        if suite in self._release_cache:
            return self._release_cache[suite]
        text = fetch_bytes(self.dist_url(suite, "Release"), self.timeout).decode(
            "utf-8", "replace"
        )
        stanzas = list(parse_stanzas(text))
        if not stanzas:
            raise RepoError(f"Empty or invalid Release for suite '{suite}'")
        s = stanzas[0]
        rel = Release(
            suite=suite,
            origin=s.get("Origin", ""),
            label=s.get("Label", ""),
            codename=s.get("Codename", ""),
            components=s.get("Components", "").split(),
            architectures=s.get("Architectures", "").split(),
            description=s.get("Description", ""),
        )
        self._release_cache[suite] = rel
        return rel

    # -- Packages --------------------------------------------------------
    def get_packages(
        self,
        suite: str,
        component: str,
        arch: str,
        progress: Optional[Callable[[str], None]] = None,
    ) -> list[Package]:
        """Return all package versions for suite/component/arch."""
        key = (suite, component, arch)
        if key in self._packages_cache:
            return self._packages_cache[key]

        base = self.dist_url(suite, component, f"binary-{arch}")
        last_err: Optional[Exception] = None
        raw: Optional[bytes] = None
        for fname, kind in _INDEX_VARIANTS:
            url = f"{base}/{fname}"
            try:
                if progress:
                    progress(f"Fetching {fname} ...")
                data = fetch_bytes(url, self.timeout)
                raw = _decompress(data, kind)
                break
            except RepoError as exc:
                last_err = exc
                continue
        if raw is None:
            raise RepoError(
                f"No Packages index for {suite}/{component}/binary-{arch} "
                f"(last error: {last_err})"
            )

        text = raw.decode("utf-8", "replace")
        packages: list[Package] = []
        for st in parse_stanzas(text):
            name = st.get("Package")
            if not name:
                continue
            try:
                size = int(st.get("Size", "0"))
            except ValueError:
                size = 0
            packages.append(
                Package(
                    name=name,
                    version=st.get("Version", ""),
                    architecture=st.get("Architecture", arch),
                    filename=st.get("Filename", ""),
                    size=size,
                    sha256=st.get("SHA256"),
                    md5sum=st.get("MD5sum"),
                    description=st.get("Description", ""),
                    section=st.get("Section", ""),
                    maintainer=st.get("Maintainer", ""),
                    depends=st.get("Depends", ""),
                    homepage=st.get("Homepage", ""),
                    raw=st,
                )
            )
        self._packages_cache[key] = packages
        return packages

    # -- Aggregation helpers --------------------------------------------
    def package_names(
        self, suite: str, component: str, arch: str, **kw
    ) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.get_packages(suite, component, arch, **kw):
            seen.setdefault(p.name, None)
        return sorted(seen)

    def versions_of(
        self, suite: str, component: str, arch: str, name: str, **kw
    ) -> list[Package]:
        out = [
            p
            for p in self.get_packages(suite, component, arch, **kw)
            if p.name == name
        ]
        out.sort(key=lambda p: _version_key(p.version), reverse=True)
        return out


# --------------------------------------------------------------------------
# Download with progress + checksum verification
# --------------------------------------------------------------------------
@dataclass
class DownloadResult:
    path: str
    size: int
    verified: bool
    checksum_kind: str  # "sha256", "md5", or "none"


def download_deb(
    repo: AptRepo,
    pkg: Package,
    dest_dir: str,
    progress: Optional[Callable[[int, int], None]] = None,
    chunk: int = 65536,
) -> DownloadResult:
    """Download a package's .deb to dest_dir, verifying its checksum.

    ``progress`` is called as ``progress(downloaded_bytes, total_bytes)``.
    Raises RepoError on checksum mismatch (and removes the bad file).
    """
    if not pkg.filename:
        raise RepoError(f"Package {pkg.name} has no Filename field")

    os.makedirs(dest_dir, exist_ok=True)
    url = repo.deb_url(pkg)
    out_name = os.path.basename(pkg.filename)
    out_path = os.path.join(dest_dir, out_name)

    hasher = None
    checksum_kind = "none"
    if pkg.sha256:
        hasher = hashlib.sha256()
        checksum_kind = "sha256"
    elif pkg.md5sum:
        hasher = hashlib.md5()
        checksum_kind = "md5"

    with _open(url, repo.timeout) as resp:
        total = int(resp.headers.get("Content-Length", pkg.size or 0))
        done = 0
        tmp_path = out_path + ".part"
        with open(tmp_path, "wb") as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                if hasher:
                    hasher.update(buf)
                done += len(buf)
                if progress:
                    progress(done, total)

    verified = False
    if hasher:
        digest = hasher.hexdigest()
        expected = (pkg.sha256 or pkg.md5sum or "").lower()
        if digest.lower() != expected:
            os.remove(tmp_path)
            raise RepoError(
                f"Checksum mismatch for {out_name}: "
                f"expected {expected}, got {digest}"
            )
        verified = True

    os.replace(tmp_path, out_path)
    return DownloadResult(
        path=out_path,
        size=os.path.getsize(out_path),
        verified=verified,
        checksum_kind=checksum_kind,
    )


def human_size(n: int) -> str:
    """Format a byte count as a short human-readable string."""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
