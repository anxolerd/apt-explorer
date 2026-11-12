# apt-explorer

An interactive terminal UI (TUI) for exploring APT repositories and
downloading arbitrary `.deb` packages — pure Python standard library, **zero
external dependencies**.

Browse a repository's **suites → components → architectures → packages →
versions**, inspect package metadata, and download any `.deb` with automatic
SHA256/MD5 verification.

```
┌ Suite ──┬ Component ┬ Arch ──┬ Package ─────┬ Version ──────┐
│ stable  │ main      │ amd64  │ bash         │ 5.2.15-2+b7   │
│ testing │ contrib   │ arm64  │ coreutils    │ 5.2.15-2+b2   │
│ sid     │ non-free  │ i386   │ curl         │ ...           │
└─────────┴───────────┴────────┴──────────────┴───────────────┘
 Details
 Package:  curl
 Version:  8.5.0-2
 Size:     1.3 MB  SHA256: a1b2...
 ...
 arrows/hjkl move  Enter/l drill  / search  d download  r reload  q quit
```

## Requirements

- Python 3.9+ (tested on 3.14)
- A terminal with curses support (Linux/macOS/WSL)

No pip packages required.

## Usage

Run directly from the checkout:

```bash
./apt-explorer                       # Debian (default preset)
./apt-explorer --preset ubuntu --suite jammy
./apt-explorer --repo http://deb.debian.org/debian --suite bookworm
```

Or install it:

```bash
pip install .
apt-explorer
```

### Options

| Option | Description |
| ------ | ----------- |
| `--repo URL` | Base URL of the APT repo (overrides `--preset`) |
| `--preset {debian,debian-security,ubuntu}` | Use a known repository |
| `--suite SUITE` | Suite to show (repeatable; e.g. `bookworm`, `jammy`, `sid`) |
| `-d, --dest DIR` | Download directory (default: `./downloads`) |
| `--timeout SEC` | Network timeout (default: 30) |

### Key bindings (TUI)

| Key | Action |
| --- | ------ |
| `↑/↓` or `k/j` | Move selection |
| `←/→` or `h/l` | Move between columns / drill in |
| `Enter` | Drill into the selection (download on the Version column) |
| `/` | Incremental search within the package list |
| `d` | Download the selected package version |
| `r` | Reload the current index (bust cache) |
| `PgUp/PgDn`, `Home/End` | Fast scroll |
| `q` | Quit |

### Non-interactive mode (scripting)

Useful for automation or CI:

```bash
# List all package names in a suite/component/arch
./apt-explorer --suite bookworm --list-packages main amd64

# Show configured suites
./apt-explorer --preset ubuntu --list-suites

# Download a specific package version (or 'latest')
./apt-explorer --suite bookworm --download main amd64 curl latest -d /tmp/debs
```

The `--download` command prints the saved path on stdout and progress on
stderr, so it composes cleanly in pipelines.

## How it works

APT repositories follow a fixed layout:

```
<base>/dists/<suite>/Release
<base>/dists/<suite>/<component>/binary-<arch>/Packages[.xz|.gz|.bz2]
<base>/<Filename>      # the .deb, path taken from the package's Filename field
```

`apt-explorer`:

1. Parses `Release` to discover components and architectures.
2. Fetches and decompresses the `Packages` index (prefers `.xz`, falls back to
   `.gz`, `.bz2`, then plain).
3. Parses the deb822 stanzas into typed `Package` records.
4. On download, streams the `.deb` and verifies its SHA256 (or MD5) against the
   index before saving. A mismatch deletes the partial file and errors out.

## Project layout

```
apt-explorer            # executable launcher
aptexplorer/
  __init__.py
  repo.py               # repo client, parsing, download + verification
  tui.py                # curses UI
  cli.py                # argparse entry point + presets + scripting mode
tests/
  test_repo.py          # offline self-tests against a mock index
pyproject.toml
```

## License

MIT
