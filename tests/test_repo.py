"""Offline self-tests for apt-explorer.

Spins up a local HTTP server serving a hand-built mock APT repository
(Release + gzip/xz Packages indices + a fake .deb) and exercises the full
client path without touching the network. Run with::

    python3 tests/test_repo.py
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import lzma
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aptexplorer.repo import (  # noqa: E402
    AptRepo,
    RepoError,
    download_deb,
    human_size,
    parse_stanzas,
)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        FAILS.append(msg)


# --------------------------------------------------------------------------
# Build a mock repo on disk.
# --------------------------------------------------------------------------
def build_mock_repo(root: str) -> dict:
    deb_bytes = b"!<arch>\n" + b"fake-debian-package-contents" * 4
    deb_sha = hashlib.sha256(deb_bytes).hexdigest()
    deb_md5 = hashlib.md5(deb_bytes).hexdigest()

    # the .deb itself, under pool/
    pool_rel = "pool/main/c/curl/curl_8.5.0-2_amd64.deb"
    pool_abs = os.path.join(root, pool_rel)
    os.makedirs(os.path.dirname(pool_abs), exist_ok=True)
    with open(pool_abs, "wb") as fh:
        fh.write(deb_bytes)

    # bad-checksum deb (same contents, but index will lie about the hash)
    pool_rel_bad = "pool/main/b/bad/bad_1.0_amd64.deb"
    pool_abs_bad = os.path.join(root, pool_rel_bad)
    os.makedirs(os.path.dirname(pool_abs_bad), exist_ok=True)
    with open(pool_abs_bad, "wb") as fh:
        fh.write(deb_bytes)

    # Release
    dist_dir = os.path.join(root, "dists", "bookworm")
    os.makedirs(dist_dir, exist_ok=True)
    release = (
        "Origin: MockDebian\n"
        "Label: MockDebian\n"
        "Suite: stable\n"
        "Codename: bookworm\n"
        "Components: main contrib\n"
        "Architectures: amd64 arm64\n"
        "Description: Mock repo for tests\n"
    )
    with open(os.path.join(dist_dir, "Release"), "w") as fh:
        fh.write(release)

    # Packages index (two versions of curl + one bad pkg + another pkg)
    packages = f"""Package: curl
Version: 8.5.0-2
Architecture: amd64
Maintainer: Alice <alice@example.org>
Section: web
Depends: libc6 (>= 2.34), libcurl4 (= 8.5.0-2)
Homepage: https://curl.se
Filename: {pool_rel}
Size: {len(deb_bytes)}
SHA256: {deb_sha}
MD5sum: {deb_md5}
Description: command line tool for transferring data
 curl is a tool to transfer data from or to a server.

Package: curl
Version: 7.88.1-10
Architecture: amd64
Maintainer: Alice <alice@example.org>
Section: web
Filename: {pool_rel}
Size: {len(deb_bytes)}
SHA256: {deb_sha}
Description: command line tool for transferring data (older)

Package: bash
Version: 5.2.15-2
Architecture: amd64
Section: shells
Filename: {pool_rel}
Size: {len(deb_bytes)}
SHA256: {deb_sha}
Description: GNU Bourne Again SHell

Package: bad
Version: 1.0
Architecture: amd64
Section: misc
Filename: {pool_rel_bad}
Size: {len(deb_bytes)}
SHA256: {'0' * 64}
Description: package whose index checksum is wrong
"""
    bindir = os.path.join(dist_dir, "main", "binary-amd64")
    os.makedirs(bindir, exist_ok=True)
    raw = packages.encode("utf-8")
    with open(os.path.join(bindir, "Packages.gz"), "wb") as fh:
        fh.write(gzip.compress(raw))
    with open(os.path.join(bindir, "Packages.xz"), "wb") as fh:
        fh.write(lzma.compress(raw))

    # contrib/binary-amd64 left intentionally empty-ish (only plain Packages)
    contribdir = os.path.join(dist_dir, "contrib", "binary-amd64")
    os.makedirs(contribdir, exist_ok=True)
    with open(os.path.join(contribdir, "Packages"), "wb") as fh:
        fh.write(b"")

    return {
        "deb_sha": deb_sha,
        "deb_size": len(deb_bytes),
        "pool_rel": pool_rel,
    }


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass


def serve(root: str):
    handler = lambda *a, **k: _QuietHandler(*a, directory=root, **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_parse_stanzas():
    print("parse_stanzas:")
    text = "A: 1\nB: two\n cont\n\nA: 3\n"
    stanzas = list(parse_stanzas(text))
    check(len(stanzas) == 2, "two stanzas parsed")
    check(stanzas[0]["A"] == "1", "field value parsed")
    check(stanzas[0]["B"] == "two\ncont", "folded continuation joined")
    check(stanzas[1]["A"] == "3", "second stanza parsed")


def test_human_size():
    print("human_size:")
    check(human_size(0) == "0 B", "zero bytes")
    check(human_size(1536) == "1.5 KB", "1.5 KB")
    check(human_size(5 * 1024 * 1024) == "5.0 MB", "5 MB")


def test_compare_versions():
    from aptexplorer.repo import compare_versions as cv

    print("compare_versions (Debian ordering):")
    # the original bug: string sort put 9.5.9 above 13.3.0
    check(cv("13.3.0", "9.5.9") == 1, "13.3.0 > 9.5.9 (the reported bug)")
    check(cv("10.0.0", "9.0.0") == 1, "10.0.0 > 9.0.0")
    check(cv("1.0", "1.0") == 0, "equal versions")
    check(cv("2.0", "2.0-1") == -1, "no revision < with revision")
    check(cv("1:0", "2.0") == 1, "epoch dominates")
    check(cv("1.0~rc1", "1.0") == -1, "~ sorts before release")
    check(cv("1.0~beta", "1.0~rc1") == -1, "~beta < ~rc1")
    check(cv("1.0.10", "1.0.9") == 1, "numeric chunk 10 > 9")
    check(cv("2.1.0-2", "2.1.0-10") == -1, "revision 2 < 10 numeric")
    # sorting a realistic grafana set
    vers = ["9.5.9", "13.3.0", "10.0.0", "9.0.0", "13.1.10", "13.1.9"]
    import functools
    srt = sorted(vers, key=functools.cmp_to_key(cv), reverse=True)
    check(srt[0] == "13.3.0", f"newest is 13.3.0, got {srt[0]}")
    check(srt == ["13.3.0", "13.1.10", "13.1.9", "10.0.0", "9.5.9", "9.0.0"],
          f"full order correct: {srt}")


def run_live_tests(repo: AptRepo, info: dict, dest: str):
    print("get_release:")
    rel = repo.get_release("bookworm")
    check(rel.codename == "bookworm", "codename parsed")
    check(rel.components == ["main", "contrib"], "components parsed")
    check(rel.architectures == ["amd64", "arm64"], "architectures parsed")

    print("get_packages / listing:")
    names = repo.package_names("bookworm", "main", "amd64")
    check(names == ["bad", "bash", "curl"], f"sorted unique names: {names}")

    print("versions_of:")
    vers = repo.versions_of("bookworm", "main", "amd64", "curl")
    check(len(vers) == 2, "two curl versions")
    check(vers[0].version == "8.5.0-2", "newest version first")
    check(vers[0].sha256 == info["deb_sha"], "sha256 captured from index")
    check(vers[0].short_description.startswith("command line"), "short desc")

    print("download_deb (good checksum, sha256):")
    pkg = vers[0]
    seen = {"calls": 0}
    result = download_deb(
        repo, pkg, dest, progress=lambda d, t: seen.__setitem__("calls",
                                                                 seen["calls"] + 1)
    )
    check(result.verified, "sha256 verified")
    check(result.checksum_kind == "sha256", "checksum kind sha256")
    check(os.path.exists(result.path), "file written to dest")
    check(result.size == info["deb_size"], "size matches")
    check(seen["calls"] > 0, "progress callback fired")

    print("download_deb (bad checksum -> error + cleanup):")
    bad = repo.versions_of("bookworm", "main", "amd64", "bad")[0]
    err = None
    try:
        download_deb(repo, bad, dest)
    except RepoError as exc:
        err = exc
    check(err is not None, "checksum mismatch raised RepoError")
    bad_path = os.path.join(dest, os.path.basename(bad.filename))
    check(not os.path.exists(bad_path + ".part"), "partial file cleaned up")

    print("missing index -> RepoError:")
    err = None
    try:
        repo.get_packages("bookworm", "contrib", "arm64")
    except RepoError as exc:
        err = exc
    check(err is not None, "missing Packages raises RepoError")

    print("reload cache bust:")
    repo._packages_cache.pop(("bookworm", "main", "amd64"), None)
    names2 = repo.package_names("bookworm", "main", "amd64")
    check(names2 == names, "re-fetch returns same names")


def main() -> int:
    test_parse_stanzas()
    test_human_size()
    test_compare_versions()

    with tempfile.TemporaryDirectory() as root:
        info = build_mock_repo(root)
        httpd = serve(root)
        try:
            port = httpd.server_address[1]
            base = f"http://127.0.0.1:{port}"
            repo = AptRepo(base, timeout=10)
            with tempfile.TemporaryDirectory() as dest:
                run_live_tests(repo, info, dest)
        finally:
            httpd.shutdown()

    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILED")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("RESULT: ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
