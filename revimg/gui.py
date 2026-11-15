# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""Drag-and-drop desktop app ("the simple version").

Two large drop zones are the whole interface: drop a picture on 加密 or 还原,
type a passphrase (it may be empty), and the result is written next to the
source file.  Nothing is uploaded, nothing needs configuring.

Design notes
------------
* Empty passphrase is allowed on purpose.  The core codec refuses an empty
  passphrase, so the GUI substitutes :data:`OPEN_PASSPHRASE`; the window says
  plainly that an empty passphrase means "scrambled only, no secrecy".
* Encoding and decoding run on a worker thread; the Tk main loop only polls a
  queue, so the window never freezes and no Tk call crosses threads.
* Every failure is translated into a sentence a person can act on -- wrong
  passphrase, image too small, not an image, not an encrypted carrier.
"""

from __future__ import annotations

import os
import pathlib
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk, UnidentifiedImageError

from . import __version__, codec

try:  # the frozen build always brings tkinterdnd2; a source run may not have it
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - optional at runtime
    DND_FILES = None
    TkinterDnD = None

#: What an empty passphrase means.  A fixed, well-known string keeps the codec
#: contract ("no empty passphrase") intact while still giving the GUI a
#: one-click "just scramble it" mode.  Command-line users can pass the same
#: string with ``-p revimg-open-v1``.
OPEN_PASSPHRASE = "revimg-open-v1"

IMAGE_FILETYPES = [
    ("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff *.gif"),
    ("所有文件", "*.*"),
]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
                  ".gif", ".ppm", ".pgm", ".ico", ".jfif"}

BG = "#f2f5fa"
CARD = "#ffffff"
INK = "#1f2937"
MUTED = "#6b7280"
FAINT = "#9aa4b2"
BORDER = "#d5dde8"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#e8f0ff"
GREEN = "#15803d"
AMBER = "#b45309"
RED = "#b91c1c"

_MODE_TEXT = {
    "fine": "完整还原（画质最好）",
    "fine+coarse": "完整还原 + 缩略图补洞",
    "coarse": "仅冗余缩略图（偏糊但干净）",
    "mosaic": "仅拼图还原（没读到载荷）",
    "none": "没有还原",
}


class GuiError(Exception):
    """A failure with a human-readable explanation."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# ------------------------------------------------------------------- helpers

def _enable_dpi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # per-monitor aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()        # older Windows
    except Exception:
        pass


def _resource(relative: str) -> pathlib.Path | None:
    base = getattr(sys, "_MEIPASS", None)
    root = pathlib.Path(base) if base else pathlib.Path(__file__).resolve().parent.parent
    path = root / relative
    return path if path.exists() else None


def _open_image(path: pathlib.Path) -> Image.Image:
    if not path.exists():
        raise GuiError(f"找不到文件：{path}", "文件可能被移动或删除了，重新拖一次即可。")
    if path.is_dir():
        raise GuiError("这是一个文件夹，不是图片。", "请拖一张图片文件进来。")
    try:
        with Image.open(path) as im:
            im.load()
            return im.convert("RGB")
    except UnidentifiedImageError:
        raise GuiError(
            f"无法识别这个文件：{path.name}",
            "支持 PNG / JPG / BMP / WebP / GIF / TIFF 等常见图片格式。",
        ) from None
    except OSError as exc:
        raise GuiError(f"读取图片失败：{exc}", "文件可能损坏，或正被其他程序占用。") from None


def _output_path(source: pathlib.Path, mode: str) -> pathlib.Path:
    """Same folder as the source, new name, never overwrite."""
    stem = source.stem
    if mode == "encode":
        name = f"{stem}_locked"
    else:
        # photo_locked.png -> photo_restored.png; photo_locked-2.png keeps the
        # collision suffix instead of turning into photo_locked-2_restored.png.
        match = re.fullmatch(r"(.*)_locked(?:-(\d+))?", stem)
        if match:
            suffix = f"-{match.group(2)}" if match.group(2) else ""
            name = f"{match.group(1)}_restored{suffix}"
        else:
            name = f"{stem}_restored"
    candidate = source.with_name(f"{name}.png")
    n = 2
    while candidate.exists():
        candidate = source.with_name(f"{name}-{n}.png")
        n += 1
    return candidate


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _password_for(raw: str) -> tuple[str, bool]:
    """Choose the passphrase; whitespace-only counts as empty."""
    return (raw, False) if raw.strip() else (OPEN_PASSPHRASE, True)


# ----------------------------------------------------------------- drop zones

class DropZone(tk.Frame):
    """A card that accepts a dragged file, or opens a file dialog on click."""

    def __init__(self, master, app: "RevimgApp", mode: str, title: str, subtitle: str):
        super().__init__(master, bg=CARD, height=162, cursor="hand2",
                         highlightthickness=2, highlightbackground=BORDER,
                         highlightcolor=ACCENT)
        self.app = app
        self.mode = mode
        self._enabled = True
        self._normal_bg = CARD
        self._hover_bg = ACCENT_SOFT
        self.grid_propagate(False)

        self._title = tk.Label(self, text=title, bg=CARD, fg=INK,
                               font=(app.font_family, 14, "bold"), cursor="hand2")
        self._title.pack(pady=(28, 4))
        self._sub = tk.Label(self, text=subtitle, bg=CARD, fg=MUTED,
                             font=(app.font_family, 9), justify="center",
                             cursor="hand2")
        self._sub.pack()
        self._cue = tk.Label(self, text="拖到这里，或点击选择", bg=CARD, fg=ACCENT,
                             font=(app.font_family, 9, "bold"), cursor="hand2")
        self._cue.pack(pady=(14, 0))

        self._parts = (self, self._title, self._sub, self._cue)
        for widget in self._parts:
            widget.bind("<Button-1>", self._click)
            if DND_FILES is not None:
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._drop)
                    widget.dnd_bind("<<DropEnter>>", self._enter)
                    widget.dnd_bind("<<DropLeave>>", self._leave)
                except Exception:
                    pass

    # -- state ---------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        cursor = "hand2" if enabled else "arrow"
        for widget in self._parts:
            widget.configure(cursor=cursor)
        if not enabled:
            self._paint(CARD, BORDER)
        self._cue.configure(fg=ACCENT if enabled else FAINT)

    def _paint(self, bg: str, border: str) -> None:
        self.configure(bg=bg, highlightbackground=border)
        for widget in self._parts[1:]:
            widget.configure(bg=bg)

    # -- events --------------------------------------------------------
    def _click(self, _event=None):
        if self._enabled:
            self.app.pick_file(self.mode)

    def _enter(self, _event=None):
        if self._enabled:
            self._paint(self._hover_bg, ACCENT)
        return "copy"

    def _leave(self, _event=None):
        if self._enabled:
            self._paint(CARD, BORDER)

    def _drop(self, event):
        self._leave()
        if self._enabled:
            self.app.handle_drop(self.mode, getattr(event, "data", ""))


# ------------------------------------------------------------------ the app

_Base = TkinterDnD.Tk if TkinterDnD is not None else tk.Tk


class RevimgApp(_Base):
    def __init__(self):
        _enable_dpi()
        super().__init__()
        self.title(f"revimg {__version__} — 图片加密 / 还原")
        self.configure(bg=BG)
        self.minsize(760, 660)
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._last_output: pathlib.Path | None = None
        self._photo = None
        self._current: pathlib.Path | None = None

        self.font_family = self._pick_font()
        self._setup_dpi()
        self._build_ui()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- construction --------------------------------------------------
    def _pick_font(self) -> str:
        available = set(tkfont.families(self))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if name in available:
                return name
        return "TkDefaultFont"

    def _setup_dpi(self) -> None:
        try:
            scaling = self.winfo_fpixels("1i") / 72.0
            if 1.0 <= scaling <= 3.0:
                self.tk.call("tk", "scaling", scaling)
        except Exception:
            pass

    def _center(self) -> None:
        self.update_idletasks()
        width, height = 860, 720
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _button(self, parent, text, command, *, primary=False):
        bg = ACCENT if primary else CARD
        fg = "#ffffff" if primary else INK
        active_bg = ACCENT_DARK if primary else ACCENT_SOFT
        active_fg = "#ffffff" if primary else INK
        return tk.Button(
            parent, text=text, command=command,
            font=(self.font_family, 10, "bold" if primary else "normal"),
            bg=bg, fg=fg, activebackground=active_bg, activeforeground=active_fg,
            relief="flat", bd=0, padx=16, pady=7, cursor="hand2",
            highlightthickness=0 if primary else 1,
            highlightbackground=BORDER, disabledforeground=FAINT,
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=22, pady=16)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="revimg  图片加密 / 还原", bg=BG, fg=INK,
                 font=(self.font_family, 19, "bold")).pack(anchor="w")
        tk.Label(
            header,
            text="口令可以留空。把图片拖进下面任意一个框，结果直接存在原图旁边。",
            bg=BG, fg=MUTED, font=(self.font_family, 10),
        ).pack(anchor="w", pady=(3, 0))

        zones = tk.Frame(outer, bg=BG)
        zones.pack(fill="x", pady=(14, 0))
        zones.grid_columnconfigure(0, weight=1)
        zones.grid_columnconfigure(1, weight=1)
        self.enc_zone = DropZone(zones, self, "encode", "加密图片",
                                 "变成能正常打开的马赛克图")
        self.dec_zone = DropZone(zones, self, "decode", "还原图片",
                                 "用同一个口令恢复成照片")
        self.enc_zone.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.dec_zone.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        info = tk.Frame(outer, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        info.pack(fill="x", pady=(14, 0))
        self._preview = tk.Label(info, bg=CARD, width=132, height=88,
                                 text="预览", fg=FAINT,
                                 font=(self.font_family, 9))
        self._preview.pack(side="left", padx=12, pady=12)
        meta = tk.Frame(info, bg=CARD)
        meta.pack(side="left", fill="both", expand=True, pady=12, padx=(0, 12))
        self._name = tk.Label(meta, text="还没有选择图片", bg=CARD, fg=INK,
                              font=(self.font_family, 11, "bold"), anchor="w")
        self._name.pack(fill="x")
        self._detail = tk.Label(meta, text="拖一张图片进来，或点一下上面的框",
                                bg=CARD, fg=MUTED, font=(self.font_family, 9),
                                anchor="w", justify="left", wraplength=540)
        self._detail.pack(fill="x", pady=(4, 0))
        self._result = tk.Label(meta, text="", bg=CARD, fg=GREEN,
                                font=(self.font_family, 9), anchor="w",
                                justify="left", wraplength=540)
        self._result.pack(fill="x", pady=(4, 0))

        pw = tk.Frame(outer, bg=BG)
        pw.pack(fill="x", pady=(14, 0))
        tk.Label(pw, text="口令", bg=BG, fg=INK,
                 font=(self.font_family, 10, "bold")).pack(side="left")
        self._pw_var = tk.StringVar()
        self._pw_var.trace_add("write", lambda *_: self._on_password_change())
        self._pw_entry = tk.Entry(
            pw, textvariable=self._pw_var, show="•", font=(self.font_family, 11),
            relief="flat", bd=0, highlightthickness=2,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            bg=CARD, fg=INK, insertbackground=INK,
        )
        self._pw_entry.pack(side="left", fill="x", expand=True, padx=(12, 10), ipady=6)
        self._show_var = tk.BooleanVar(value=False)
        self._show_check = tk.Checkbutton(
            pw, text="显示", variable=self._show_var, command=self._toggle_password,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=INK,
            selectcolor=CARD, bd=0, highlightthickness=0, cursor="hand2",
            font=(self.font_family, 9),
        )
        self._show_check.pack(side="left")

        self._pw_hint = tk.Label(outer, text="", bg=BG, fg=AMBER,
                                 font=(self.font_family, 9), anchor="w")
        self._pw_hint.pack(fill="x", pady=(5, 0))

        status_row = tk.Frame(outer, bg=BG)
        status_row.pack(fill="x", pady=(10, 0))
        self._progress = ttk.Progressbar(status_row, mode="indeterminate", length=180)
        self._status = tk.Label(status_row, text="就绪：拖一张图片到上面开始",
                                bg=BG, fg=MUTED, font=(self.font_family, 10),
                                anchor="w", justify="left", wraplength=580)
        self._status.pack(side="left", fill="x", expand=True, padx=(0, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(10, 0))
        self._clear_btn = self._button(actions, "清除", self.reset)
        self._clear_btn.pack(side="left")
        self._copy_btn = self._button(actions, "复制路径", self._copy_path)
        self._copy_btn.pack(side="right")
        self._folder_btn = self._button(actions, "打开文件夹", self._open_folder)
        self._folder_btn.pack(side="right", padx=(0, 8))
        self._open_btn = self._button(actions, "打开结果", self._open_output, primary=True)
        self._open_btn.pack(side="right", padx=(0, 8))
        self._set_result_buttons(False)

        tk.Label(
            outer,
            text="加密后的图片能正常打开、预览；被裁剪或重新压缩后，仍能还原未被破坏的部分。",
            bg=BG, fg=FAINT, font=(self.font_family, 8),
        ).pack(side="bottom", anchor="w", pady=(12, 0))

        icon = _resource("assets/revimg.ico")
        if icon is not None:
            try:
                self.iconbitmap(str(icon))
            except Exception:
                pass
        if DND_FILES is None:
            for zone in (self.enc_zone, self.dec_zone):
                zone._cue.configure(text="点击选择图片")
            self._set_status("拖放组件未安装：点击上面的框选择文件即可。", "warn")
        self._on_password_change()

    # -- small UI helpers ---------------------------------------------
    def _set_status(self, text: str, kind: str = "info") -> None:
        colour = {"info": MUTED, "good": GREEN, "warn": AMBER, "bad": RED}.get(kind, MUTED)
        self._status.configure(text=text, fg=colour)

    def _set_result_buttons(self, enabled: bool) -> None:
        self._open_btn.configure(
            state="normal" if enabled else "disabled",
            bg=ACCENT if enabled else "#dde4ee",
            fg="#ffffff" if enabled else "#98a2b3",
            activebackground=ACCENT_DARK if enabled else "#dde4ee",
        )
        for button in (self._folder_btn, self._copy_btn):
            button.configure(
                state="normal" if enabled else "disabled",
                fg=INK if enabled else FAINT,
            )

    def _on_password_change(self) -> None:
        raw = self._pw_var.get()
        if raw:
            self._pw_hint.configure(
                text="口令只在本机内存里使用，不会上传，也不会写入任何文件。", fg=MUTED)
        else:
            self._pw_hint.configure(
                text="留空 = 只打乱、不加密：谁拿到这张图，用空口令就能还原。", fg=AMBER)

    def _toggle_password(self) -> None:
        self._pw_entry.configure(show="" if self._show_var.get() else "•")

    def _show_thumb(self, path: pathlib.Path, caption: str = "") -> None:
        try:
            with Image.open(path) as im:
                thumb = im.convert("RGB")
                thumb.thumbnail((128, 88), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(thumb)
            self._preview.configure(image=self._photo, text="", width=128, height=88)
        except Exception:
            self._photo = None
            self._preview.configure(image="", text=caption or "无预览")

    def set_file(self, path: pathlib.Path) -> None:
        self._current = path
        self._last_output = None
        try:
            with Image.open(path) as im:
                width, height = im.size
                im.thumbnail((128, 88), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(im.convert("RGB"))
            self._preview.configure(image=self._photo, text="", width=128, height=88)
            self._name.configure(text=path.name)
            size = path.stat().st_size if path.exists() else 0
            self._detail.configure(
                text=f"{width} × {height} · {_fmt_bytes(size)}\n{path.parent}")
            self._result.configure(text="")
            self._set_status("已选好图片：拖到上面的框里，或直接点框开始。", "info")
        except (UnidentifiedImageError, OSError) as exc:
            self._show_thumb_error()
            self._name.configure(text=path.name)
            self._detail.configure(text="不是能识别的图片")
            self._set_status(f"读不出这张图片：{exc}", "bad")

    def _show_thumb_error(self) -> None:
        self._photo = None
        self._preview.configure(image="", text="无法预览", width=132, height=88)

    # -- actions -------------------------------------------------------
    def pick_file(self, mode: str) -> None:
        if self._busy:
            return
        chosen = filedialog.askopenfilename(
            parent=self, title="选择图片", filetypes=IMAGE_FILETYPES,
        )
        if chosen:
            self.start(mode, pathlib.Path(chosen))

    def handle_drop(self, mode: str, data: str) -> None:
        if self._busy:
            return
        try:
            paths = list(self.tk.splitlist(data))
        except Exception:
            paths = [data]
        paths = [p for p in paths if p]
        if not paths:
            return
        if len(paths) > 1:
            self._set_status("一次处理一张，已使用第一张图片。", "warn")
        self.start(mode, pathlib.Path(paths[0]))

    def start(self, mode: str, path: pathlib.Path) -> None:
        if self._busy:
            return
        if not path.exists():
            self._error(GuiError(f"找不到文件：{path}", "重新拖一次，或点上面的框选择文件。"))
            return
        password = _password_for(self._pw_var.get())[0]
        self._current = path
        self._last_output = None
        self._result.configure(text="")
        self._set_result_buttons(False)
        self._set_busy(True, "正在加密…" if mode == "encode" else "正在还原…")
        self._set_status(
            "正在加密，请稍候…" if mode == "encode"
            else "正在还原，最坏情况约 15 秒，请稍候…",
            "info",
        )
        threading.Thread(
            target=self._work, args=(mode, path, password), daemon=True,
        ).start()
        self.after(60, self._poll)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.enc_zone.set_enabled(not busy)
        self.dec_zone.set_enabled(not busy)
        state = "disabled" if busy else "normal"
        self._pw_entry.configure(state=state)
        self._show_check.configure(state=state)
        self._clear_btn.configure(state=state)
        if busy:
            self._progress.pack(side="right", padx=(10, 0))
            self._progress.start(12)
            self._set_status(message, "info")
        else:
            self._progress.stop()
            self._progress.pack_forget()

    def _work(self, mode: str, path: pathlib.Path, password: str) -> None:
        try:
            image = _open_image(path)
            width, height = image.size
            if mode == "encode":
                cap = codec.capacity(width, height)
                if cap["slots"] < 4:
                    raise GuiError(
                        "图片太小，放不下载荷。",
                        "至少需要约 256×256 像素；建议长边 2000 像素以上。",
                    )
                carrier, rep = codec.encode(image, password)
                out = _output_path(path, "encode")
                carrier.save(out)
                out_w, out_h = carrier.size
                note = ""
                if rep.layers and rep.redundancy:
                    layer = rep.layers[0]
                    survive = 0.0 if rep.redundancy[0] <= 1 else 1 - 1 / rep.redundancy[0]
                    note = (f"缩略图 {layer['w']}×{layer['h']} · "
                            f"约 {survive:.0%} 画面被毁仍可还原")
                self._queue.put(("ok", {
                    "mode": mode, "out": out, "width": out_w, "height": out_h,
                    "note": note,
                }))
            else:
                recovered, rep = codec.decode(image, password)
                if recovered is None:
                    raise GuiError(
                        "还原失败：口令不对，或这张图里没有可还原的数据。",
                        "请确认：\n"
                        "· 口令和加密时完全一致（区分大小写、全角半角）\n"
                        "· 图片长边像素没有变过（被缩放过的图无法还原）\n"
                        "· 如果这是原图，请拖到「加密图片」框",
                    )
                out = _output_path(path, "decode")
                recovered.save(out)
                out_w, out_h = recovered.size
                note = _MODE_TEXT.get(rep.mode, rep.mode)
                if rep.pages_total:
                    note += f" · 存活页 {rep.pages_ok}/{rep.pages_total}"
                self._queue.put(("ok", {
                    "mode": mode, "out": out, "width": out_w, "height": out_h,
                    "note": note,
                }))
        except GuiError as exc:
            self._queue.put(("error", exc))
        except ValueError as exc:
            message = str(exc)
            if "too small" in message:
                hint = "至少需要约 256×256 像素；建议长边 2000 像素以上。"
                self._queue.put(("error", GuiError("图片太小，放不下载荷。", hint)))
            else:
                self._queue.put(("error", GuiError(f"处理失败：{message}", "")))
        except UnidentifiedImageError:
            self._queue.put(("error", GuiError(
                "无法识别这张图片。", "支持 PNG / JPG / BMP / WebP / GIF / TIFF。")))
        except MemoryError:
            self._queue.put(("error", GuiError(
                "内存不够，处理不了这么大的图片。", "换一张小一点的图片试试。")))
        except Exception as exc:  # noqa: BLE001 - never let a thread die silently
            self._queue.put(("error", GuiError(
                f"处理时出错：{exc}", "可以换一张图片再试；若反复出现，请保留原图。")))

    def _poll(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(80, self._poll)
            return
        if kind == "ok":
            self._finish_ok(payload)
        else:
            self._finish_error(payload)

    def _finish_ok(self, result: dict) -> None:
        self._set_busy(False)
        out: pathlib.Path = result["out"]
        self._last_output = out
        self._set_status(
            ("加密完成" if result["mode"] == "encode" else "还原完成")
            + f" · 已保存到 {out}", "good",
        )
        self._result.configure(
            text=("加密效果" if result["mode"] == "encode" else "还原方式")
            + f"：{result['note']}", fg=GREEN,
        )
        self._show_thumb(out)
        self._name.configure(text=out.name)
        try:
            size = _fmt_bytes(out.stat().st_size)
        except OSError:
            size = ""
        self._detail.configure(
            text=f"{result['width']} × {result['height']}  ·  {size}\n{out.parent}"
        )
        self._set_result_buttons(True)

    def _finish_error(self, error: GuiError) -> None:
        self._set_busy(False)
        self._set_status(f"失败：{error.message}", "bad")
        self.bell()
        text = error.message + (f"\n\n{error.hint}" if error.hint else "")
        messagebox.showerror("revimg", text, parent=self)

    def _error(self, error: GuiError) -> None:
        self._finish_error(error)

    def reset(self) -> None:
        if self._busy:
            return
        self._current = None
        self._last_output = None
        self._photo = None
        self._preview.configure(image="", text="预览", width=132, height=88)
        self._name.configure(text="还没有选择图片")
        self._detail.configure(text="拖一张图片进来，或点一下上面的框")
        self._result.configure(text="")
        self._set_result_buttons(False)
        self._set_status("就绪：拖一张图片到上面开始", "info")

    # -- output actions ------------------------------------------------
    def _open_output(self) -> None:
        if self._last_output is not None:
            try:
                os.startfile(str(self._last_output))
            except OSError as exc:
                self._finish_error(GuiError(f"打不开文件：{exc}"))

    def _open_folder(self) -> None:
        if self._last_output is not None:
            try:
                subprocess.Popen(["explorer", "/select,", str(self._last_output)])
            except OSError as exc:
                self._finish_error(GuiError(f"打不开文件夹：{exc}"))

    def _copy_path(self) -> None:
        if self._last_output is not None:
            self.clipboard_clear()
            self.clipboard_append(str(self._last_output))
            self._set_status("路径已复制到剪贴板。", "good")

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno(
                "revimg", "还在处理中，确定要退出吗？", parent=self,
            ):
                return
        self.destroy()


# ------------------------------------------------------------------ selftest

def _selftest_image(side: int = 640) -> Image.Image:
    image = Image.new("RGB", (side, side), (34, 66, 120))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, side, side // 2], fill=(80, 140, 200))
    draw.rectangle([side // 2, side // 2, side, side], fill=(200, 150, 60))
    for i in range(0, side, 80):
        draw.line([(0, i), (side, side - i)], fill=(240, 240, 240), width=3)
    draw.ellipse([side * 0.2, side * 0.2, side * 0.6, side * 0.6], fill=(200, 70, 70))
    return image


def _pump_until_idle(app: RevimgApp, timeout: float) -> bool:
    end = time.time() + timeout
    while app._busy and time.time() < end:
        app.update()
        time.sleep(0.01)
    for _ in range(4):
        app.update()
        time.sleep(0.01)
    return not app._busy


def _selftest(report_path: str | None) -> int:
    lines = [f"revimg {__version__} GUI selftest"]
    app = None
    workdir = None
    code = 1
    try:
        import shutil

        app = RevimgApp()
        app.withdraw()
        dnd = app.tk.call("package", "require", "tkdnd")
        lines.append(f"tkdnd: {dnd}")
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="revimg-gui-"))
        source = workdir / "sample.png"
        _selftest_image(640).save(source)
        lines.append(f"sample: {source}")

        app._pw_var.set("")
        # Simulate a real drop: tkdnd delivers paths as a Tcl list, so pass the
        # brace-quoted form the event would carry.
        app.handle_drop("encode", "{" + str(source) + "}")
        if not _pump_until_idle(app, 120) or app._last_output is None:
            raise RuntimeError("encode did not finish")
        encoded = pathlib.Path(app._last_output)
        if not encoded.exists():
            raise RuntimeError("encode wrote no file")
        lines.append(f"encode: {encoded.name} ({encoded.stat().st_size} B)")

        app.start("decode", encoded)
        if not _pump_until_idle(app, 120) or app._last_output is None:
            raise RuntimeError("decode did not finish")
        decoded = pathlib.Path(app._last_output)
        if not decoded.exists():
            raise RuntimeError("decode wrote no file")
        lines.append(f"decode: {decoded.name} ({decoded.stat().st_size} B)")

        with Image.open(encoded) as probe:
            wrong, _rep = codec.decode(probe.convert("RGB"), "wrong-passphrase")
        if wrong is not None:
            raise RuntimeError("wrong passphrase was accepted")
        lines.append("wrong passphrase: rejected")

        code = 0
        lines.append("OK")
    except Exception as exc:  # noqa: BLE001 - report instead of vanishing
        import traceback

        lines.append(f"FAIL: {exc}")
        lines.append(traceback.format_exc())
    finally:
        if app is not None:
            try:
                app.destroy()
            except Exception:
                pass
        if workdir is not None:
            try:
                import shutil

                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass
        target = pathlib.Path(report_path) if report_path else (
            pathlib.Path(tempfile.gettempdir()) / "revimg-gui-selftest.txt")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass
    return code


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--selftest":
        return _selftest(args[1] if len(args) > 1 else None)
    _enable_dpi()
    app = RevimgApp()
    if args and not args[0].startswith("-"):
        candidate = pathlib.Path(args[0])
        if candidate.exists():
            app.set_file(candidate)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
