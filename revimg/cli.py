# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 revimg contributors

"""The friendly command line.

Two ways in, one set of commands:

* ``revimg`` on its own opens a small menu, so the frozen executable is usable
  by double-clicking.  A file dragged onto the icon just opens that image and
  asks whether to lock or restore it.
* ``revimg encode/decode/info`` takes flags for scripting; anything missing
  (password, output name, strength) is asked for interactively instead of
  failing with a traceback.

Passwords are never echoed.  When the input is not a terminal the tool refuses
to guess and asks for ``-p`` / ``--password-file`` instead.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time

from PIL import Image, UnidentifiedImageError

from . import __version__, codec, console

#: Suffixes the encoder can write.  ``.png`` is the default because the carrier
#: must survive a save without being re-compressed.
_SAVE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
_IMAGE_SUFFIXES = _SAVE_SUFFIXES | {".gif", ".ppm", ".pgm", ".ico", ".jfif"}
_COMMANDS = {"encode", "decode", "info", "selftest", "menu", "bench"}

#: The three strengths offered in the wizard, with the promise each carries.
_LEVELS = (
    ("1", "标准", 3.2, "可扛约 69% 画面被毁，画质与容量平衡"),
    ("2", "更强", 4.0, "可扛约 75% 画面被毁，容量更小"),
    ("3", "较弱", 2.0, "可扛约 50% 画面被毁，画质最好"),
    ("4", "只打乱", None, "逐位精确往返，但不嵌数据、不抗破坏"),
)

_MODE_NAMES = {
    "fine": "完整还原（画质最好）",
    "fine+coarse": "完整还原 + 缩略图补洞（被裁/被涂处略糊）",
    "coarse": "仅冗余缩略图（整体偏糊，但画面干净）",
    "mosaic": "仅拼图还原（没有读到载荷）",
    "none": "没有还原",
}

_FAILURE_REASONS = {
    "authentication failed (wrong passphrase?)": "口令不对（也可能图片被改坏）",
    "payload not recoverable": "读到了页面，但残存数据不足以重建",
    "no page decoded": "口令不对，或图片被缩放/严重破坏（找不到可用页面）",
    "corrupt header": "文件头损坏",
    "undecodable thumbnail": "缩略图损坏",
    "geometry outside canvas": "几何位置超出画布",
}


class CliError(Exception):
    """An expected, explainable failure.  Printed without a traceback."""

    def __init__(self, message: str, hint: str | None = None, code: int = 1):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------- paths

def _resolve(text: str | pathlib.Path) -> pathlib.Path:
    raw = str(text).strip().strip('"').strip("'")
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    return path


def _normalise_output(path: pathlib.Path) -> pathlib.Path:
    if path.suffix == "":
        return path.with_suffix(".png")
    if path.suffix.lower() not in _SAVE_SUFFIXES:
        raise CliError(
            f"不支持的输出格式：{path.suffix}",
            "请用 .png（推荐）、.jpg、.bmp、.webp 或 .tif。",
        )
    return path


def _unique(path: pathlib.Path) -> pathlib.Path:
    """Never silently overwrite: ``a.png`` -> ``a-2.png`` -> ``a-3.png``."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 1000):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return path


def _default_output(src: pathlib.Path, kind: str) -> pathlib.Path:
    stem = src.stem
    if kind == "encode":
        name = f"{stem}_locked"
    else:
        # photo_locked.png -> photo_restored.png; photo_locked-2.png keeps the
        # collision suffix rather than becoming photo_locked-2_restored.png.
        match = re.fullmatch(r"(.*)_locked(?:-(\d+))?", stem)
        if match:
            suffix = f"-{match.group(2)}" if match.group(2) else ""
            name = f"{match.group(1)}_restored{suffix}"
        else:
            name = f"{stem}_restored"
    return _unique(src.with_name(f"{name}.png"))


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _open_image(text) -> tuple[Image.Image, pathlib.Path]:
    if not text:
        raise CliError("没有指定图片", "把图片拖到窗口里，或输入图片路径。")
    path = _resolve(text)
    if not path.exists():
        raise CliError(f"找不到文件：{path}", "检查路径是否正确；图片也可以直接拖进窗口。")
    if path.is_dir():
        raise CliError(f"这是一个文件夹，不是图片：{path}", "请选择一张图片文件。")
    try:
        with Image.open(path) as im:
            im.load()
            if im.width * im.height > 80_000_000:
                console.warn(f"图片很大（{im.width}×{im.height}），处理会比较慢。")
            return im.convert("RGB"), path
    except UnidentifiedImageError:
        raise CliError(
            f"无法识别这个文件：{path.name}",
            "支持 PNG / JPG / BMP / WebP / GIF / TIFF 等常见图片格式。",
        ) from None
    except OSError as exc:
        raise CliError(f"读取图片失败：{exc}", "文件可能损坏，或正被其他程序占用。") from None


def _save_image(image: Image.Image, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        image.save(path, quality=92)
    elif suffix == ".webp":
        image.save(path, quality=95)
    else:
        image.save(path)
    return path


# -------------------------------------------------------------------- inputs

def _ask_image(what: str) -> str:
    return console.ask(f"{what}（把文件拖进来也可以）")


def _ask_output(args, src: pathlib.Path, kind: str) -> pathlib.Path:
    given = getattr(args, "output", None)
    if given:
        path = _normalise_output(_resolve(given))
    else:
        path = _default_output(src, kind)
        if console.interactive():
            raw = console.ask("输出文件（直接回车用默认）", default=str(path))
            path = _normalise_output(_resolve(raw))
    if path.exists() and not getattr(args, "yes", False):
        if console.interactive():
            if not console.confirm(f"{path.name} 已存在，覆盖它吗？", default=False):
                raise CliError("已取消，没有写入任何文件。")
        else:
            raise CliError(
                f"输出文件已存在：{path}",
                "加 --yes 覆盖，或用第二个参数指定别的输出路径。",
            )
    return path


def _password(args, *, confirm_again: bool, prompt: str) -> str:
    direct = getattr(args, "password", None)
    if direct:
        return direct

    pw_file = getattr(args, "password_file", None)
    if pw_file:
        path = _resolve(pw_file)
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise CliError(f"读不到口令文件：{exc}") from None
        if not lines or not lines[0]:
            raise CliError(f"口令文件是空的：{path}")
        return lines[0]

    env = os.environ.get("REVIMG_PASSWORD")
    if env:
        return env

    if not console.interactive():
        raise CliError(
            "没有提供口令",
            '非交互运行时请用 -p "口令"，或用 --password-file 指定文件。',
        )
    return console.ask_password(prompt, confirm_again=confirm_again)


def _level(args) -> tuple[float, bool]:
    if getattr(args, "lossless", False):
        return 3.2, True
    alpha = getattr(args, "alpha", None)
    if alpha is not None:
        if not 1.2 <= alpha <= 20.0:
            raise CliError("alpha 超出范围", "请取 1.2 到 20 之间的数（默认 3.2）。")
        return float(alpha), False
    if not console.interactive():
        return 3.2, False
    console.section("抗破坏强度")
    for key, name, _alpha_value, note in _LEVELS:
        print(f"  {key}. {name:<5} {note}")
    choice = console.ask("请选择", default="1")
    for key, _name, alpha_value, _note in _LEVELS:
        if choice == key:
            if alpha_value is None:
                return 3.2, True
            return alpha_value, False
    console.warn("没有这个选项，用标准强度。")
    return 3.2, False


# ------------------------------------------------------------------- reports

def _banner() -> None:
    console.header(__version__, "抗破坏的可逆打乱图片容器")
    console.tip("加密后就是一张普通图片，能预览、能发送；用口令还原。全程离线。")


def _encode_summary(out: pathlib.Path, rep, elapsed: float,
                    lossless: bool) -> None:
    console.success("加密完成", elapsed)
    console.field("输出", str(out))
    console.field("文件大小", _fmt_bytes(out.stat().st_size))
    console.field("画布", f"{rep.canvas[0]}×{rep.canvas[1]}（原图 {rep.original[0]}×{rep.original[1]}）")
    if lossless:
        console.field("模式", "无损打乱：不嵌数据，不抗破坏")
        console.tip("输出补齐到 32 的倍数，还原后的尺寸可能比原图略大，需要自己裁剪。")
    else:
        if rep.layers:
            layer = rep.layers[0]
            red = rep.redundancy[0] if rep.redundancy else 1.0
            survive = 0 if red <= 1 else 1 - 1 / red
            console.field("缩略图", f"{layer['w']}×{layer['h']}（用于兜底与补洞）")
            console.field("冗余", f"{red:.2f}× · 约 {survive:.0%} 画面被毁仍可还原")
    console.section("下一步")
    console.tip("用“文件 / 原图”方式发送这张图，任何平台都不要缩放它（长边像素必须不变）。")
    console.tip("对方打开本程序 → 还原图片 → 输入同一个口令。")
    console.tip("重要：图片一旦被缩小就基本救不回来；重要资料请保留原图备份。")


def _decode_summary(out: pathlib.Path, rep, elapsed: float) -> None:
    console.success("还原完成", elapsed)
    console.field("输出", str(out))
    console.field("还原方式", _MODE_NAMES.get(rep.mode, rep.mode))
    if rep.pages_total:
        console.field("存活页", f"{rep.pages_ok}/{rep.pages_total}（{rep.pages_ok / rep.pages_total:.0%}）")
    if rep.mode != "mosaic":
        console.field("缩略图", f"{rep.thumb[0]}×{rep.thumb[1]}")
        console.field("画布", f"{rep.canvas[0]}×{rep.canvas[1]} · 裁切偏移 {rep.crop_offset}")
    elif rep.canvas != (0, 0):
        console.field("输出尺寸", f"{rep.canvas[0]}×{rep.canvas[1]}")
    if rep.mode == "fine":
        console.tip("拼图层完整重建，这是最好的结果。")
    elif rep.mode == "fine+coarse":
        console.tip("被裁掉或涂掉的部分用冗余缩略图补上了，那些区域会偏糊。")
    elif rep.mode == "coarse":
        console.tip("拼图层没能重建，用的是冗余缩略图：整体偏糊，但画面干净。")
    elif rep.mode == "mosaic":
        console.tip("没读到载荷，只按栅格把打乱的画面拼了回来。")
        console.tip("无损模式（--lossless）出的图就是这样，属于正常；否则多半是图片被缩放过。")
        console.tip("无损模式的输出会补齐到 32 的倍数，请裁回原尺寸。")


def _decode_failure(rep) -> CliError:
    reason = _FAILURE_REASONS.get(rep.reason, rep.reason or "没有可还原的数据")
    return CliError(
        f"还原失败：{reason}",
        "请依次检查：1) 口令是否完全一致（区分大小写和全角半角）；"
        "2) 图片长边像素是否和加密时相同；3) 换回没有转发过的原始文件再试。",
        code=2,
    )


def _capacity_note(slots: int, w: int, h: int) -> None:
    if slots < 4:
        raise CliError(
            f"图片太小，放不下载荷（{w}×{h} 只有 {slots} 个页槽）",
            "至少需要约 256×256 像素；建议长边 2000 像素以上，抗破坏能力才够用。",
            code=2,
        )
    if max(w, h) < 1200:
        console.warn(
            f"图片偏小（{w}×{h}）：容量和抗破坏能力都会明显下降，建议长边 ≥ 2000 像素。"
        )


# ------------------------------------------------------------------ commands

def cmd_encode(args) -> int:
    src_text = getattr(args, "image", None) or _ask_image("要加密哪张图片")
    image, src = _open_image(src_text)
    width, height = image.size
    capacity = codec.capacity(width, height)
    _capacity_note(capacity["slots"], width, height)

    password = _password(args, confirm_again=True, prompt="请设置口令")
    alpha, lossless = _level(args)
    out = _ask_output(args, src, "encode")

    console.section("加密")
    console.field("原图", f"{src}  {width}×{height}")
    if lossless:
        console.field("模式", "无损打乱（不嵌数据）")
    else:
        console.field("强度", f"alpha {alpha:g} · 约 {100 * (1 - 1 / alpha):.0f}% 画面被毁仍可还原")

    started = time.perf_counter()
    with console.Spinner("打乱并嵌入数据"):
        try:
            carrier, rep = codec.encode(image, password, alpha_detail=alpha, lossless=lossless)
        except ValueError as exc:
            raise CliError(
                "这张图放不下要嵌入的数据",
                f"原图信息：{exc}。换一张更大的图，或降低抗破坏强度。",
            ) from None
    elapsed = time.perf_counter() - started

    out = _save_image(carrier, out)
    if console.quiet():
        print(out)
    else:
        _encode_summary(out, rep, elapsed, lossless)
    return 0


def cmd_decode(args) -> int:
    src_text = getattr(args, "image", None) or _ask_image("要还原哪张图片")
    image, src = _open_image(src_text)
    password = _password(args, confirm_again=False, prompt="请输入口令")
    out_path = _ask_output(args, src, "decode")

    console.section("还原")
    console.field("输入", f"{src}  {image.width}×{image.height}")

    started = time.perf_counter()
    with console.Spinner("寻找栅格并还原", note="最坏情况约 15 秒"):
        recovered, rep = codec.decode(image, password, force=getattr(args, "mode", "auto"))
    elapsed = time.perf_counter() - started

    if recovered is None:
        raise _decode_failure(rep)

    out = _save_image(recovered, out_path)
    if console.quiet():
        print(out)
    else:
        _decode_summary(out, rep, elapsed)

    if getattr(args, "also", False):
        coarse = getattr(recovered, "revimg_coarse", None)
        fine = getattr(recovered, "revimg_fine", None)
        for layer, name in ((coarse, "_coarse"), (fine, "_fine")):
            if layer is None:
                continue
            extra = _save_image(layer, out.with_name(out.stem + name + ".png"))
            if console.quiet():
                print(extra)
            else:
                console.field("附输出", str(extra))
    return 0


def cmd_info(args) -> int:
    # "info" is a report by definition, so quiet mode would print nothing at all.
    console.set_quiet(False)
    src_text = getattr(args, "image", None) or _ask_image("要查看哪张图片")
    image, src = _open_image(src_text)
    width, height = image.size
    capacity = codec.capacity(width, height)
    slots = capacity["slots"]
    alpha = float(getattr(args, "alpha", None) or 3.2)
    payload = int(slots * codec.BS / alpha)
    survive = 0 if alpha <= 1 else 1 - 1 / alpha

    if getattr(args, "json", False):
        print(json.dumps({
            "file": str(src), "width": width, "height": height,
            "canvas": capacity["canvas"], "pages": [capacity["pages_x"], capacity["pages_y"]],
            "slots": slots, "symbol_bytes": capacity["bytes_total"],
            "alpha": alpha, "payload_bytes": payload,
            "survives": round(survive, 4), "encodable": slots >= 4,
        }, ensure_ascii=False))
        return 0

    console.section("容量信息")
    console.field("文件", str(src))
    console.field("尺寸", f"{width}×{height}")
    if slots < 4:
        console.field("页槽", f"{slots}（不足 4 页，无法加密）")
        console.warn("图片太小：至少需要约 256×256 像素。")
        return 0

    console.field("页槽", f"{capacity['pages_x']}×{capacity['pages_y']} = {slots} 页")
    console.field("符号容量", f"{capacity['bytes_total']} B")
    console.field("画布", f"{capacity['canvas'][0]}×{capacity['canvas'][1]}（补齐到 128 的倍数）")
    console.field("可用载荷", f"约 {payload} B（alpha {alpha:g}，约 {survive:.0%} 画面被毁仍可还原）")

    console.section("不同强度的取舍")
    for _key, name, value, _note in _LEVELS[:3]:
        bytes_here = int(slots * codec.BS / value)
        console.field(name, f"载荷约 {bytes_here} B · 可扛约 {100 * (1 - 1 / value):.0f}% 被毁", width=6)
    console.tip("alpha 越大越抗破坏，但能装的缩略图越小，画质越低。")
    if max(width, height) < 1200:
        console.warn("图片偏小：建议长边 ≥ 2000 像素，否则裁剪/涂改容易失败。")
    return 0


def _selftest_image(side: int = 640) -> Image.Image:
    import numpy as np

    yy, xx = np.mgrid[0:side, 0:side].astype(np.float32)
    u, v = xx / (side - 1), yy / (side - 1)
    arr = np.stack([30 + 190 * u, 50 + 170 * v, 90 + 150 * (1 - u)], axis=-1)
    for cx, cy, radius, colour in (
        (0.28, 0.30, 0.16, (240, 80, 60)),
        (0.72, 0.62, 0.20, (30, 200, 120)),
        (0.45, 0.80, 0.10, (250, 220, 40)),
    ):
        mask = (u - cx) ** 2 + (v - cy) ** 2 < radius ** 2
        arr[mask] = colour
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def cmd_selftest(args) -> int:
    import tempfile

    import numpy as np

    from . import crypto, metrics

    if not getattr(args, "_from_menu", False):
        _banner()
    console.section("自检")
    password = "revimg-selftest"
    side = 640
    with tempfile.TemporaryDirectory(prefix="revimg-selftest-") as tmp:
        tmpdir = pathlib.Path(tmp)
        source = _selftest_image(side)
        console.ok(f"生成测试图 {side}×{side}")

        started = time.perf_counter()
        with console.Spinner("加密测试图"):
            carrier, rep = codec.encode(source, password, alpha_detail=3.2)
        console.ok(f"加密（{rep.canvas[0]}×{rep.canvas[1]} 画布）  {time.perf_counter() - started:.1f} 秒")

        carrier_path = _save_image(carrier, tmpdir / "carrier.png")
        with Image.open(carrier_path) as probe:
            probe.load()
            reloaded = probe.convert("RGB")
        console.ok("写出并重新读取载体图片")

        started = time.perf_counter()
        with console.Spinner("还原测试图"):
            recovered, drep = codec.decode(reloaded, password)
        if recovered is None:
            raise CliError("自检失败：正确口令也还原不出来", hint=drep.as_text(), code=3)
        elapsed = time.perf_counter() - started

        original = np.asarray(source, dtype=np.uint8)
        got = np.asarray(recovered, dtype=np.uint8)
        if got.shape != original.shape:
            raise CliError(
                f"自检失败：还原尺寸不对（{got.shape[1]}×{got.shape[0]}，应为 {side}×{side}）",
                code=3,
            )
        score = metrics.psnr(original, got)
        if score < 30.0:
            raise CliError(f"自检失败：往返画质过低（PSNR {score:.1f} dB）", code=3)
        console.ok(f"无破坏往返  PSNR {score:.1f} dB · {elapsed:.1f} 秒")

        master = crypto.master_key(password)
        sealed = crypto.seal(master, b"selftest", aad=b"12345678")
        if crypto.open_(crypto.master_key(password + "-wrong"), sealed, aad=b"12345678") is not None:
            raise CliError("自检失败：错误口令竟然通过了校验", code=3)
        console.ok("错误口令会被拒绝")

    console.success("自检通过：这台电脑可以正常加密和还原")
    return 0


def cmd_table(args) -> int:
    """Developer-facing attack matrix (kept for the acceptance gate / CI)."""
    from .bench import run, verdict

    names = args.only.split(",") if args.only else None
    rows, erep, enc_s = run(args.image, args.password, args.outdir, names, args.alpha)

    console.section("攻击矩阵")
    print(erep.as_text())
    print(f"encode time    : {enc_s:.2f}s\n")
    head = f"{'attack':<12}{'result':<8}{'PSNR':>7}{'SSIM':>8}{'mode':>13}{'pages':>10}{'sec':>6}"
    print(head)
    print("-" * len(head))
    for row in rows:
        print(f"{row['attack']:<12}{'ok' if row['ok'] else 'FAIL':<8}{row['psnr']:>7.2f}"
              f"{row['ssim']:>8.3f}{row['mode']:>13}{row['pages']:>10}{row['decode_s']:>6.2f}")

    if args.json:
        clean = [{k: v for k, v in row.items() if k not in ("image", "coarse")} for row in rows]
        pathlib.Path(args.json).write_text(json.dumps(clean, indent=2), encoding="utf-8")
    if args.acceptance:
        good, lines = verdict(rows, erep)
        print("\nacceptance baseline")
        print("\n".join(lines))
        print(f"\n{'ACCEPTED' if good else 'REJECTED'}")
        return 0 if good else 1
    return 0


def cmd_menu(args) -> int:
    return run_menu()


# ---------------------------------------------------------------------- menu

def _menu_args(**overrides) -> argparse.Namespace:
    base = dict(
        image=None, output=None, password=None, password_file=None,
        alpha=None, lossless=False, mode="auto", also=False, json=None,
        quiet=False, yes=False, no_color=False, _from_menu=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def run_menu() -> int:
    _banner()
    while True:
        console.section("主菜单")
        print("  1. 加密图片    把照片变成能正常打开的“马赛克图”")
        print("  2. 还原图片    用口令把马赛克图还原成照片")
        print("  3. 查看容量    看看一张图能装多少、能扛多少")
        print("  4. 自检        确认程序在这台电脑上工作正常")
        print("  0. 退出")
        try:
            choice = console.ask("请选择", default="1")
        except console.Cancelled:
            return 0
        if choice == "0":
            return 0
        try:
            if choice == "1":
                cmd_encode(_menu_args())
            elif choice == "2":
                cmd_decode(_menu_args())
            elif choice == "3":
                cmd_info(_menu_args())
            elif choice == "4":
                cmd_selftest(_menu_args())
            else:
                console.warn("没有这个选项，请输入 0 到 4。")
                continue
        except CliError as exc:
            _report_error(exc)
        except console.Cancelled:
            print()
            console.hint("已取消。")
        except KeyboardInterrupt:
            raise
        console.pause("按回车键返回主菜单…")


def _file_flow(path_text: str) -> int:
    path = _resolve(path_text)
    _banner()
    console.section("要对这张图片做什么？")
    console.field("图片", str(path))
    print("  1. 加密（变成打乱的马赛克图）")
    print("  2. 还原（用口令恢复成照片）")
    print("  0. 退出")
    try:
        choice = console.ask("请选择", default="1")
        if choice == "1":
            return cmd_encode(_menu_args(image=str(path)))
        if choice == "2":
            return cmd_decode(_menu_args(image=str(path)))
    except CliError as exc:
        _report_error(exc)
        return exc.code
    except console.Cancelled:
        print()
        console.hint("已取消。")
    return 0


# -------------------------------------------------------------------- parser

_EXAMPLES = """\
示例
  revimg                          打开交互菜单（双击 exe 就是这个）
  revimg 照片.jpg                  问你要加密还是还原，再问口令
  revimg encode 照片.jpg           加密；口令、强度、输出路径都会询问
  revimg encode 照片.jpg 封面.png -p "口令" --alpha 4
  revimg decode 封面.png           还原，默认输出 封面_restored.png
  revimg info 照片.jpg             查看容量与抗破坏能力

口令也可以放在 REVIMG_PASSWORD 环境变量或 --password-file 指向的文件里。
"""


def _add_globals(parser: argparse.ArgumentParser, *, copy: bool) -> None:
    default = argparse.SUPPRESS if copy else False
    parser.add_argument("--no-color", action="store_true", default=default,
                        help="关闭彩色输出")
    parser.add_argument("-q", "--quiet", action="store_true", default=default,
                        help="安静模式：只输出结果路径")
    parser.add_argument("-y", "--yes", action="store_true", default=default,
                        help="自动确认覆盖，不再询问")


class _Parser(argparse.ArgumentParser):
    """ArgumentParser with Chinese section titles, usage and error prefix."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self._positionals.title = "参数"
        self._optionals.title = "选项"
        for action in self._actions:
            if "--help" in action.option_strings:
                action.help = "显示帮助并退出"

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 参数错误：{message}\n")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_globals(common, copy=True)

    parser = _Parser(
        prog="revimg",
        description="抗破坏的可逆打乱图片容器：把照片变成能正常打开的“马赛克图”，用口令还原。",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"revimg {__version__}  (GPL-3.0-or-later)",
                        help="显示版本号并退出")
    _add_globals(parser, copy=False)
    sub = parser.add_subparsers(dest="cmd", metavar="命令")

    p = sub.add_parser(
        "encode", parents=[common],
        help="加密：把照片变成能正常打开的图片",
        description="加密：打乱画面并嵌入加密缩略图，输出仍是一张普通图片。",
    )
    p.add_argument("image", nargs="?", metavar="图片", help="原图路径（省略则询问）")
    p.add_argument("output", nargs="?", metavar="输出", help="输出路径（省略则自动命名）")
    p.add_argument("-p", "--password", metavar="口令", help="口令（省略则安全询问）")
    p.add_argument("--password-file", metavar="文件", help="从文件第一行读取口令")
    p.add_argument("--alpha", type=float, default=None, metavar="系数",
                   help="抗破坏强度：越大越抗毁、容量越小（默认 3.2）")
    p.add_argument("--lossless", action="store_true",
                   help="只打乱不嵌数据：逐位精确往返，但不抗破坏")
    p.set_defaults(func=cmd_encode)

    p = sub.add_parser(
        "decode", parents=[common],
        help="还原：用口令把图片恢复成照片",
        description="还原：尽力恢复；被裁掉或涂掉的地方用缩略图补洞。",
    )
    p.add_argument("image", nargs="?", metavar="图片", help="加密图的路径（省略则询问）")
    p.add_argument("output", nargs="?", metavar="输出", help="输出路径（省略则自动命名）")
    p.add_argument("-p", "--password", metavar="口令", help="口令（省略则安全询问）")
    p.add_argument("--password-file", metavar="文件", help="从文件第一行读取口令")
    p.add_argument("--mode", choices=("auto", "fine", "coarse"), default="auto",
                   help="强制使用某一路还原（默认 auto）")
    p.add_argument("--also", action="store_true", help="同时存出 _fine.png / _coarse.png")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser(
        "info", parents=[common],
        help="查看一张图能装多少数据、能扛多少损坏",
        description="报告页槽数量、符号容量与各强度下的可用载荷。",
    )
    p.add_argument("image", nargs="?", metavar="图片", help="图片路径（省略则询问）")
    p.add_argument("--alpha", type=float, default=3.2, metavar="系数",
                   help="按哪个强度估算（默认 3.2）")
    p.add_argument("--json", action="store_true", help="输出 JSON，便于脚本处理")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser(
        "selftest", parents=[common],
        help="自检：确认加密与还原在本机正常",
        description="用生成的测试图跑一次完整往返，并验证错误口令会被拒绝。",
    )
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("menu", parents=[common], help="打开交互菜单")
    p.set_defaults(func=cmd_menu)

    p = sub.add_parser(
        "bench", parents=[common],
        help="开发者：跑完整攻击矩阵与验收门",
        description="开发者工具：编码一次，施加各种破坏，测量还原质量。",
    )
    p.add_argument("image", help="用于基准测试的图片")
    p.add_argument("-p", "--password", default="test")
    p.add_argument("--alpha", type=float, default=3.2)
    p.add_argument("--only", default=None, help="只跑这些攻击（逗号分隔）")
    p.add_argument("--outdir", default=None, help="把中间图片写到这个目录")
    p.add_argument("--json", default=None, help="把结果写成 JSON")
    p.add_argument("--acceptance", action="store_true",
                   help="对照验收基线判定，失败时退出码非零")
    p.set_defaults(func=cmd_table)

    return parser


def _report_error(exc: CliError) -> None:
    console.error(exc.message)
    if exc.hint:
        console.hint(exc.hint)


def _prepare(args: argparse.Namespace) -> None:
    console.set_color(not getattr(args, "no_color", False))
    console.set_quiet(bool(getattr(args, "quiet", False)))


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    console.setup()
    if os.name == "nt":
        console.title("revimg — 抗破坏的图片加密")

    owned = console.own_console()
    try:
        if not raw:
            if console.interactive():
                code = run_menu()
            else:
                build_parser().print_help()
                code = 0
        elif raw[0] not in _COMMANDS and _looks_like_image(raw[0]):
            code = _file_flow(raw[0])
        else:
            parser = build_parser()
            args = parser.parse_args(raw)
            _prepare(args)
            if not getattr(args, "func", None):
                parser.print_help()
                code = 0
            else:
                code = int(args.func(args) or 0)
    except CliError as exc:
        _report_error(exc)
        code = exc.code
    except console.Cancelled:
        print()
        console.hint("已取消。")
        code = 130
    except KeyboardInterrupt:
        print()
        console.hint("已取消。")
        code = 130
    except BrokenPipeError:
        code = 0
    except Exception as exc:  # noqa: BLE001 - last-resort friendliness
        console.error(f"程序出错：{exc}")
        if os.environ.get("REVIMG_DEBUG"):
            import traceback

            traceback.print_exc()
        else:
            console.hint("设置环境变量 REVIMG_DEBUG=1 再运行，可以看到详细错误。")
        code = 1

    if owned and not console.quiet():
        console.pause("按回车键关闭窗口…")
    return code


def _looks_like_image(token: str) -> bool:
    if token.startswith("-"):
        return False
    try:
        if pathlib.Path(token).expanduser().is_file():
            return True
    except OSError:
        return False
    return pathlib.Path(token).suffix.lower() in _IMAGE_SUFFIXES


if __name__ == "__main__":
    raise SystemExit(main())
