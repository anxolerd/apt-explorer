"""Interactive curses TUI for exploring APT repositories.

Layout is a Miller-columns style drill-down:

    Suite -> Component -> Architecture -> Package -> Version -> Details

Network work (fetching indices, downloading) happens on a worker thread so
the UI never blocks. Only stdlib is used.
"""

from __future__ import annotations

import curses
import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .repo import AptRepo, Package, RepoError, _version_key, download_deb, human_size

# Column identifiers, in drill-down order.
COL_SUITE = 0
COL_COMPONENT = 1
COL_ARCH = 2
COL_PACKAGE = 3
COL_VERSION = 4
COL_TITLES = ["Suite", "Component", "Arch", "Package", "Version"]


@dataclass
class _Column:
    title: str
    items: list[str]
    cursor: int = 0
    top: int = 0  # scroll offset
    loading: bool = False
    filter: str = ""  # active search filter (applied to items source)

    def visible_items(self) -> list[str]:
        return self.items

    def selected(self) -> Optional[str]:
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None


class _Worker:
    """Runs callables on a background thread, delivering results via a queue."""

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._results: "queue.Queue[tuple[str, object, Optional[Exception]]]" = (
            queue.Queue()
        )

    def submit(self, tag: str, fn: Callable[[], object]) -> None:
        t = threading.Thread(target=self._run, args=(tag, fn), daemon=True)
        t.start()

    def _run(self, tag: str, fn: Callable[[], object]) -> None:
        try:
            self._results.put((tag, fn(), None))
        except Exception as exc:  # deliver errors to UI thread
            self._results.put((tag, None, exc))

    def poll(self):
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None


class App:
    def __init__(self, repo: AptRepo, suites: list[str], dest_dir: str):
        self.repo = repo
        self.dest_dir = dest_dir
        self.worker = _Worker()
        self.status = "Loading..."
        self.search_mode = False
        self.search_buf = ""
        self.focus = COL_SUITE
        self.packages_cache: list[Package] = []  # for current suite/comp/arch
        self.version_packages: list[Package] = []  # for focused package

        self.columns = [
            _Column(COL_TITLES[0], suites),
            _Column(COL_TITLES[1], []),
            _Column(COL_TITLES[2], []),
            _Column(COL_TITLES[3], []),
            _Column(COL_TITLES[4], []),
        ]
        self._all_package_names: list[str] = []
        self.quit = False

    # -- selection accessors --------------------------------------------
    def sel(self, col: int) -> Optional[str]:
        return self.columns[col].selected()

    # -- loading flows ---------------------------------------------------
    def load_release(self) -> None:
        suite = self.sel(COL_SUITE)
        if not suite:
            return
        self.columns[COL_COMPONENT].loading = True
        self.status = f"Loading Release for {suite}..."

        def task():
            rel = self.repo.get_release(suite)
            return rel

        self.worker.submit("release", task)

    def load_packages(self) -> None:
        suite = self.sel(COL_SUITE)
        comp = self.sel(COL_COMPONENT)
        arch = self.sel(COL_ARCH)
        if not (suite and comp and arch):
            return
        self.columns[COL_PACKAGE].loading = True
        self.status = f"Loading packages for {suite}/{comp}/{arch}..."

        def task():
            return self.repo.get_packages(suite, comp, arch)

        self.worker.submit("packages", task)

    def load_versions(self) -> None:
        name = self.sel(COL_PACKAGE)
        if not name:
            self.version_packages = []
            self.columns[COL_VERSION].items = []
            self.columns[COL_VERSION].cursor = 0
            return
        vers = [p for p in self.packages_cache if p.name == name]
        vers.sort(key=lambda p: _version_key(p.version), reverse=True)
        self.version_packages = vers
        col = self.columns[COL_VERSION]
        col.items = [p.version for p in vers]
        col.cursor = 0
        col.top = 0

    def current_package(self) -> Optional[Package]:
        idx = self.columns[COL_VERSION].cursor
        if 0 <= idx < len(self.version_packages):
            return self.version_packages[idx]
        return None

    # -- search filtering ------------------------------------------------
    def apply_package_filter(self, text: str) -> None:
        col = self.columns[COL_PACKAGE]
        text = text.lower()
        if text:
            col.items = [n for n in self._all_package_names if text in n.lower()]
        else:
            col.items = list(self._all_package_names)
        col.cursor = 0
        col.top = 0
        self.load_versions()

    # -- worker result handling -----------------------------------------
    def drain_worker(self) -> bool:
        changed = False
        while True:
            res = self.worker.poll()
            if res is None:
                break
            tag, value, err = res
            changed = True
            if err is not None:
                self.status = f"Error: {err}"
                if tag == "release":
                    self.columns[COL_COMPONENT].loading = False
                elif tag == "packages":
                    self.columns[COL_PACKAGE].loading = False
                elif tag == "download":
                    pass
                continue
            if tag == "release":
                self._on_release(value)
            elif tag == "packages":
                self._on_packages(value)
            elif tag == "download":
                self._on_download(value)
        return changed

    def _on_release(self, rel) -> None:
        self.columns[COL_COMPONENT].loading = False
        self.columns[COL_COMPONENT].items = rel.components or ["main"]
        self.columns[COL_COMPONENT].cursor = 0
        self.columns[COL_COMPONENT].top = 0
        self.columns[COL_ARCH].items = rel.architectures or ["amd64"]
        self.columns[COL_ARCH].cursor = 0
        self.columns[COL_ARCH].top = 0
        # clear downstream
        for c in (COL_PACKAGE, COL_VERSION):
            self.columns[c].items = []
            self.columns[c].cursor = 0
        self.status = (
            f"{rel.suite}: {len(rel.components)} components, "
            f"{len(rel.architectures)} archs"
        )

    def _on_packages(self, pkgs: list[Package]) -> None:
        self.columns[COL_PACKAGE].loading = False
        self.packages_cache = pkgs
        names: dict[str, None] = {}
        for p in pkgs:
            names.setdefault(p.name, None)
        self._all_package_names = sorted(names)
        self.columns[COL_PACKAGE].items = list(self._all_package_names)
        self.columns[COL_PACKAGE].cursor = 0
        self.columns[COL_PACKAGE].top = 0
        self.load_versions()
        self.status = (
            f"{len(self._all_package_names)} packages "
            f"({len(pkgs)} versions total)"
        )

    def _on_download(self, result) -> None:
        verify = (
            f"verified ({result.checksum_kind})"
            if result.verified
            else "NOT verified"
        )
        self.status = (
            f"Downloaded {os.path.basename(result.path)} "
            f"[{human_size(result.size)}, {verify}] -> {result.path}"
        )

    # -- download action -------------------------------------------------
    def start_download(self) -> None:
        pkg = self.current_package()
        if not pkg:
            self.status = "No package version selected"
            return
        self.status = f"Downloading {os.path.basename(pkg.filename)}..."

        prog_state = {"last": -1}

        def on_prog(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct != prog_state["last"]:
                prog_state["last"] = pct
                self.status = (
                    f"Downloading {os.path.basename(pkg.filename)}: "
                    f"{pct}% ({human_size(done)}/{human_size(total)})"
                )

        def task():
            return download_deb(self.repo, pkg, self.dest_dir, progress=on_prog)

        self.worker.submit("download", task)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _draw_column(win, col: _Column, x: int, width: int, height: int, active: bool):
    # header
    title = col.title
    if col.loading:
        title += " (loading)"
    header = f" {title} "[: width - 1]
    attr = curses.A_REVERSE if active else curses.A_BOLD
    try:
        win.addstr(0, x, header.ljust(width - 1)[: width - 1], attr)
    except curses.error:
        pass

    body_h = height - 1
    # scroll so cursor is visible
    if col.cursor < col.top:
        col.top = col.cursor
    elif col.cursor >= col.top + body_h:
        col.top = col.cursor - body_h + 1

    items = col.items
    for i in range(body_h):
        idx = col.top + i
        y = 1 + i
        if idx >= len(items):
            break
        text = items[idx]
        line = text[: width - 2]
        line = (" " + line).ljust(width - 1)[: width - 1]
        a = curses.A_NORMAL
        if idx == col.cursor:
            a = curses.A_REVERSE if active else curses.A_UNDERLINE
        try:
            win.addstr(y, x, line, a)
        except curses.error:
            pass


def _draw_details(win, app: App, y0: int, width: int, height: int):
    pkg = app.current_package()
    try:
        win.addstr(y0, 0, " Details ".ljust(width - 1)[: width - 1], curses.A_BOLD)
    except curses.error:
        pass
    if not pkg:
        return
    lines = [
        f"Package:     {pkg.name}",
        f"Version:     {pkg.version}",
        f"Arch:        {pkg.architecture}   Section: {pkg.section}",
        f"Size:        {human_size(pkg.size)}",
        f"SHA256:      {pkg.sha256 or '(none)'}",
        f"Maintainer:  {pkg.maintainer}",
        f"Homepage:    {pkg.homepage}",
        f"Filename:    {pkg.filename}",
        f"Depends:     {pkg.depends[:width - 14]}",
        f"Desc:        {pkg.short_description}",
    ]
    for i, text in enumerate(lines):
        if i + 1 >= height:
            break
        try:
            win.addstr(y0 + 1 + i, 0, text[: width - 1])
        except curses.error:
            pass


def _draw(stdscr, app: App):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 10 or w < 40:
        try:
            stdscr.addstr(0, 0, "Terminal too small (need >=40x10)")
        except curses.error:
            pass
        stdscr.refresh()
        return

    # top columns area takes ~55% of height
    cols_h = max(6, int(h * 0.55))
    ncols = len(app.columns)
    col_w = w // ncols

    cols_win = stdscr.subwin(cols_h, w, 0, 0)
    for i, col in enumerate(app.columns):
        x = i * col_w
        width = col_w if i < ncols - 1 else (w - x)
        _draw_column(cols_win, col, x, width, cols_h, active=(i == app.focus))

    # separator + details
    details_y = cols_h
    details_h = h - cols_h - 2
    if details_h > 1:
        _draw_details(stdscr, app, details_y, w, details_h)

    # search line (second from bottom)
    if app.search_mode:
        search_txt = f"/ {app.search_buf}"
        try:
            stdscr.addstr(h - 2, 0, search_txt.ljust(w - 1)[: w - 1], curses.A_BOLD)
        except curses.error:
            pass

    # status / help bar
    help_txt = (
        "arrows/hjkl move  Enter/l drill  / search  d download  r reload  q quit"
    )
    status = app.status[: w - 1]
    try:
        stdscr.addstr(h - 1, 0, status.ljust(w - 1)[: w - 1], curses.A_REVERSE)
    except curses.error:
        pass
    if not app.search_mode and details_h <= 1:
        pass
    # draw help just above status if room
    if not app.search_mode:
        try:
            stdscr.addstr(h - 2, 0, help_txt.ljust(w - 1)[: w - 1], curses.A_DIM)
        except curses.error:
            pass
    stdscr.refresh()


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
def _move_cursor(col: _Column, delta: int) -> None:
    if not col.items:
        return
    col.cursor = max(0, min(len(col.items) - 1, col.cursor + delta))


def _on_focus_change_downstream(app: App) -> None:
    """When the user changes a selection, refresh dependent columns."""
    if app.focus == COL_SUITE:
        app.load_release()
    elif app.focus in (COL_COMPONENT, COL_ARCH):
        app.load_packages()
    elif app.focus == COL_PACKAGE:
        app.load_versions()


def _handle_key(app: App, key: int, h: int) -> None:
    if app.search_mode:
        if key in (curses.KEY_ENTER, 10, 13):
            app.search_mode = False
        elif key == 27:  # ESC
            app.search_mode = False
            app.search_buf = ""
            app.apply_package_filter("")
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            app.search_buf = app.search_buf[:-1]
            app.apply_package_filter(app.search_buf)
        elif 32 <= key < 127:
            app.search_buf += chr(key)
            app.apply_package_filter(app.search_buf)
        return

    col = app.columns[app.focus]
    page = max(1, int(h * 0.4))

    if key in (ord("q"), ord("Q")):
        app.quit = True
    elif key in (curses.KEY_DOWN, ord("j")):
        _move_cursor(col, 1)
        _refresh_after_vertical(app)
    elif key in (curses.KEY_UP, ord("k")):
        _move_cursor(col, -1)
        _refresh_after_vertical(app)
    elif key == curses.KEY_NPAGE:
        _move_cursor(col, page)
        _refresh_after_vertical(app)
    elif key == curses.KEY_PPAGE:
        _move_cursor(col, -page)
        _refresh_after_vertical(app)
    elif key in (curses.KEY_HOME,):
        col.cursor = 0
        _refresh_after_vertical(app)
    elif key in (curses.KEY_END,):
        col.cursor = max(0, len(col.items) - 1)
        _refresh_after_vertical(app)
    elif key in (curses.KEY_LEFT, ord("h")):
        if app.focus > COL_SUITE:
            app.focus -= 1
    elif key in (curses.KEY_RIGHT, ord("l"), curses.KEY_ENTER, 10, 13):
        _drill_in(app)
    elif key in (ord("/"),):
        if app.focus == COL_PACKAGE or app.columns[COL_PACKAGE].items:
            app.focus = COL_PACKAGE
            app.search_mode = True
            app.search_buf = app.columns[COL_PACKAGE].filter
    elif key in (ord("d"), ord("D")):
        app.start_download()
    elif key in (ord("r"), ord("R")):
        _reload_current(app)


def _refresh_after_vertical(app: App) -> None:
    # moving within package column updates versions + details live
    if app.focus == COL_PACKAGE:
        app.load_versions()


def _drill_in(app: App) -> None:
    if app.focus == COL_SUITE:
        app.load_release()
        app.focus = COL_COMPONENT
    elif app.focus == COL_COMPONENT:
        app.load_packages()
        app.focus = COL_ARCH
    elif app.focus == COL_ARCH:
        app.load_packages()
        app.focus = COL_PACKAGE
    elif app.focus == COL_PACKAGE:
        app.load_versions()
        app.focus = COL_VERSION
    elif app.focus == COL_VERSION:
        app.start_download()


def _reload_current(app: App) -> None:
    if app.focus in (COL_COMPONENT, COL_SUITE):
        app.repo._release_cache.clear()
        app.load_release()
    else:
        suite = app.sel(COL_SUITE)
        comp = app.sel(COL_COMPONENT)
        arch = app.sel(COL_ARCH)
        if suite and comp and arch:
            app.repo._packages_cache.pop((suite, comp, arch), None)
            app.load_packages()


def _loop(stdscr, app: App):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(80)
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    # kick off initial release load for first suite
    if app.columns[COL_SUITE].items:
        app.load_release()

    while not app.quit:
        app.drain_worker()
        _draw(stdscr, app)
        h, _ = stdscr.getmaxyx()
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1
        if key == -1:
            continue
        _handle_key(app, key, h)


def run(repo: AptRepo, suites: list[str], dest_dir: str) -> None:
    """Entry point: launch the curses UI."""
    app = App(repo, suites, dest_dir)
    curses.wrapper(_loop, app)
