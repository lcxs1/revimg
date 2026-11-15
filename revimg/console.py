# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Terminal helpers for the friendly command line.

Everything here is deliberately dependency-free and Windows-aware: the same
code runs from a source checkout and from the frozen ``revimg.exe``, where
stdout may be a real console, a pipe, or a redirected file.  The rules are:

* colour only when attached to a console that understands ANSI (or a POSIX
  terminal), and only when ``NO_COLOR`` is unset;
* Chinese text survives redirection because non-tty streams are switched to
  UTF-8 on startup;
* prompts never echo passwords, and every prompt is cancellable with Ctrl-C;
* the spinner never leaves debris on screen, even when the task fails.
"""

from __future__ import annotations

import getpass
import os
import sys
import threading
import time
import unicodedata

__all__ = [
    "Cancelled",
    "Spinner",
    "ask",
    "ask_password",
    "confirm",
    "dim",
    "error",
    "field",
    "header",
    "hint",
    "interactive",
    "ok",
    "own_console",
    "pause",
    "set_color",
    "set_quiet",
    "setup",
    "section",
    "success",
    "title",
    "warn",
]

_TTY_OUT = False
_TTY_IN = False
_ANSI = False
_COLOR = True
_QUIET = False


# --------------------------------------------------------------------- setup

def _enable_windows_ansi() -> bool:
    """Ask the Windows console to interpret ANSI escapes.  False if impossible."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False                              # redirected: no console
        if not mode.value & 0x0004:                   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def setup() -> None:
    """Prepare streams and colour support.  Safe to call more than once."""
    global _ANSI, _TTY_OUT, _TTY_IN
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                # Keep Chinese readable when the user pipes or redirects output.
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    _TTY_OUT = bool(getattr(sys.stdout, "isatty", lambda: False)())
    _TTY_IN = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if os.name == "nt":
        _ANSI = _enable_windows_ansi()
    else:
        _ANSI = _TTY_OUT
    if os.environ.get("NO_COLOR"):
        _ANSI = False


def set_color(enabled: bool) -> None:
    global _COLOR
    _COLOR = bool(enabled)


def set_quiet(enabled: bool) -> None:
    global _QUIET
    _QUIET = bool(enabled)


def quiet() -> bool:
    return _QUIET


def interactive() -> bool:
    return _TTY_IN and _TTY_OUT


def own_console() -> bool:
    """True when this process is the only user of its console.

    That is exactly the case when ``revimg.exe`` was started from Explorer
    (double click or a file dropped on the icon) and the window would vanish
    the moment the program exits, so it is safe to wait for a keypress.
    """
    if os.name != "nt":
        return interactive()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        arr = (ctypes.c_uint * 4)()
        return kernel32.GetConsoleProcessList(arr, 4) <= 1
    except Exception:
        return False


def title(text: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


# --------------------------------------------------------------------- colour

def _paint(text: str, *codes: str, stream=None) -> str:
    stream = stream or sys.stdout
    if not (_ANSI and _COLOR and getattr(stream, "isatty", lambda: False)()):
        return text
    return "".join(f"\x1b[{c}m" for c in codes) + text + "\x1b[0m"


def bold(text: str) -> str:
    return _paint(text, "1")


def dim(text: str, stream=None) -> str:
    return _paint(text, "2", stream=stream)


def cyan(text: str) -> str:
    return _paint(text, "36")


def green(text: str) -> str:
    return _paint(text, "32")


def yellow(text: str, stream=None) -> str:
    return _paint(text, "33", stream=stream)


def red(text: str, stream=None) -> str:
    return _paint(text, "31", stream=stream)


# ---------------------------------------------------------------------- text

def _width(text: str) -> int:
    """Display width, counting CJK characters as two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def header(version: str, subtitle: str) -> None:
    """The program banner, printed once."""
    if _QUIET:
        return
    bar = "-" * 58
    print()
    print(f" {bold(cyan('revimg'))} {dim(version)}  {subtitle}")
    print(dim(bar))


def section(text: str) -> None:
    if _QUIET:
        return
    print()
    print(bold(f" {text}"))


def field(label: str, value: str, width: int = 12) -> None:
    if _QUIET:
        return
    print(f"  {dim(_pad(label, width))} {value}")


def ok(text: str) -> None:
    if _QUIET:
        return
    print(f"  {green('√')} {text}")


def success(text: str, elapsed: float | None = None) -> None:
    if _QUIET:
        return
    tail = f"  {dim(f'{elapsed:.1f} 秒')}" if elapsed is not None else ""
    print(f"\n{green('√')} {bold(text)}{tail}")


def warn(text: str) -> None:
    if _QUIET:
        return
    print(f"{yellow('!', sys.stderr)} {text}", file=sys.stderr)


def error(text: str) -> None:
    print(f"{red('×', sys.stderr)} {text}", file=sys.stderr)


def hint(text: str) -> None:
    print(dim(f"  {text}", sys.stderr), file=sys.stderr)


def tip(text: str) -> None:
    if _QUIET:
        return
    print(dim(f"  · {text}"))


def pause(message: str = "按回车键继续…") -> None:
    """Wait for Enter; never raised into the caller."""
    try:
        input(f"\n{dim(message)}")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


# ------------------------------------------------------------------- prompts

class Cancelled(Exception):
    """The user pressed Ctrl-C or closed stdin while a prompt was open."""


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" {dim('[' + default + ']')}" if default else ""
    while True:
        try:
            raw = input(f"{cyan('?')} {prompt}{suffix}: ")
        except EOFError:
            raise Cancelled() from None
        raw = raw.strip()
        if raw:
            return raw
        if default is not None:
            return default
        warn("这一项不能为空，请重新输入。")


def confirm(prompt: str, default: bool = True) -> bool:
    marker = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            raw = input(f"{cyan('?')} {prompt} {dim(marker)}: ").strip().lower()
        except EOFError:
            raise Cancelled() from None
        if not raw:
            return default
        if raw in ("y", "yes", "是", "好"):
            return True
        if raw in ("n", "no", "否", "不"):
            return False
        warn("请输入 y 或 n。")


def _secret(prompt: str) -> str:
    try:
        if _TTY_IN:
            return getpass.getpass(f"{cyan('?')} {prompt}: ")
        line = sys.stdin.readline()
        if not line:
            raise Cancelled()
        return line.rstrip("\r\n")
    except (EOFError, OSError):
        raise Cancelled() from None


def ask_password(prompt: str = "口令", confirm_again: bool = False) -> str:
    """Read a passphrase without echo.  Empty input is rejected."""
    while True:
        first = _secret(prompt)
        if not first:
            warn("口令不能为空。")
            continue
        if len(first) < 6:
            warn("口令偏短，建议至少 6 个字符；本工具不做强度限制，请自己权衡。")
        if not confirm_again:
            return first
        again = _secret("再输入一次（防止打错）")
        if first != again:
            warn("两次输入不一致，请重新设置。")
            continue
        return first


# ------------------------------------------------------------------- spinner

class Spinner:
    """A one-line progress indicator for a blocking call.

    On a console it animates in place and erases itself; when output is
    redirected it prints the label once so logs still show what happened.
    """

    FRAMES = "|/-\\"

    def __init__(self, label: str, note: str = ""):
        self.label = label
        self.note = note
        self.enabled = _TTY_OUT and not _QUIET
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        self._last_len = 0

    def __enter__(self) -> "Spinner":
        self._start = time.perf_counter()
        if not _QUIET:
            if self.enabled:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            else:
                print(f"  正在{self.label}…", file=sys.stderr, flush=True)
        return self

    def _line(self, frame: str) -> str:
        elapsed = time.perf_counter() - self._start
        note = f"  {dim(self.note)}" if self.note else ""
        return f"\r  {cyan(frame)} 正在{self.label}…  {elapsed:4.1f} 秒{note}"

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(0.12):
            line = self._line(self.FRAMES[index % len(self.FRAMES)])
            pad = max(0, self._last_len - len(line))
            sys.stdout.write(line + " " * pad)
            sys.stdout.flush()
            self._last_len = max(self._last_len, len(line))
            index += 1

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            sys.stdout.write("\r" + " " * (self._last_len + 2) + "\r")
            sys.stdout.flush()
