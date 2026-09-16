#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音恢复 —— 合并版（还原 .aud / .amr / .mpeg）

功能
----
* 把转换生成的标记文件 x.aud1 / x.amr1 / x.mpeg1 改回 x.aud / x.amr / x.mpeg
* 同时删除对应的 wav：
    - 新规则   x.aud.wav
    - 旧规则   x.wav      （兼容早期 aud / mpeg 工具留下的产物）
  只有确认是真正的 WAV 文件（文件头 RIFF....WAVE）才会删，避免误删同名文件。
* 若 x.aud 已存在，跳过不覆盖（加 -f 才覆盖）
* 先还原源文件再删 wav：中途失败也不会丢数据
* 纯文件操作，不依赖 ffmpeg / pysilk

用法
----
  python 恢复.py                   处理本文件所在目录及其所有子目录
  python 恢复.py "D:\\录音"         处理指定目录
  python 恢复.py a.amr1            处理指定文件
  python 恢复.py -n                只预览，不改动任何文件
  python 恢复.py --no-recursive    只处理当前层
  python 恢复.py --keep-wav        还原但保留 wav
"""

from __future__ import annotations

import argparse
import os
import sys

# 与 转换.py 保持一致：标记文件的扩展名 = 源扩展名 + "1"
DEFAULT_EXTS = (".aud", ".amr", ".mpeg")
WAV_SUFFIX = ".wav"

SELF_PATH = os.path.abspath(sys.argv[0])
SELF_NAME = os.path.splitext(os.path.basename(SELF_PATH))[0]
SELF_DIR = os.path.dirname(SELF_PATH) or os.getcwd()

LABELS = {"ok": "完成", "skip": "跳过", "fail": "错误"}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def setup_stdout() -> None:
    """让中文输出在重定向时也保持 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def log(message: str) -> None:
    print(message, flush=True)


def clean_arg(arg: str) -> str:
    """清理命令行参数（同 转换.py，处理拖拽带来的尾部反斜杠）。"""
    arg = arg.strip()
    while arg.endswith('"'):
        arg = arg[:-1].rstrip()
    while len(arg) > 3 and arg.endswith(("\\", "/")):
        arg = arg[:-1].rstrip()
    if len(arg) == 2 and arg.endswith(":"):
        arg += "\\"
    return arg


def parse_exts(text: str | None) -> tuple[str, ...]:
    if not text:
        return DEFAULT_EXTS
    out: list[str] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        if part not in out:
            out.append(part)
    return tuple(out) or DEFAULT_EXTS


def is_wave(path: str) -> bool:
    """确认目标确实是 WAV 文件，避免误删同名但无关的文件。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"


# --------------------------------------------------------------------------- #
# 扫描待还原的标记文件
# --------------------------------------------------------------------------- #
def collect_markers(paths: list[str], exts: tuple[str, ...],
                    recursive: bool) -> list[str]:
    """展开成待还原的标记文件列表（保持顺序，按路径去重）。"""
    found: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            found.append(p)

    def is_marker(name: str) -> bool:
        low = name.lower()
        return any(low.endswith(ext + "1") for ext in exts)

    def marker_from_wav(name: str) -> str | None:
        """x.aud.wav -> x.aud1（这样把 wav 拖进来也能还原）。"""
        if not name.lower().endswith(WAV_SUFFIX):
            return None
        base = name[: -len(WAV_SUFFIX)]
        for ext in exts:
            if base.lower().endswith(ext):
                return base + "1"
        return None

    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    for name in sorted(files):
                        if is_marker(name):
                            add(os.path.join(root, name))
            else:
                with os.scandir(p) as it:
                    for entry in sorted(it, key=lambda e: e.name):
                        if entry.is_file() and is_marker(entry.name):
                            add(entry.path)
        elif os.path.isfile(p):
            if is_marker(os.path.basename(p)):
                add(p)
            else:
                guess = marker_from_wav(os.path.basename(p))
                if guess:
                    candidate = os.path.join(os.path.dirname(p), guess)
                    if os.path.isfile(candidate):
                        add(candidate)
    return found


# --------------------------------------------------------------------------- #
# 核心：还原单个文件
# --------------------------------------------------------------------------- #
def wav_candidates(src: str) -> list[str]:
    """同一条语音可能对应的 wav：新规则 x.aud.wav，旧规则 x.wav。"""
    return list(dict.fromkeys([src + WAV_SUFFIX, os.path.splitext(src)[0] + WAV_SUFFIX]))


def restore_one(mark: str, force: bool = False, remove_wav: bool = True,
                dry_run: bool = False) -> tuple[str, str]:
    """还原单个标记文件，返回 (状态, 提示信息)，状态为 ok / skip / fail。"""
    name = os.path.basename(mark)
    src = mark[:-1]                       # x.aud1 -> x.aud
    src_name = os.path.basename(src)

    if not os.path.isfile(mark):
        return "fail", f"{name}: 找不到备份文件"

    if os.path.isfile(src) and not force:
        return "skip", f"{name}: {src_name} 已存在，为避免覆盖已跳过（要覆盖请加 -f）"

    # 分类要处理的 wav：能确认是 WAV 的才敢删
    drops: list[str] = []
    keeps: list[str] = []
    for c in wav_candidates(src):
        if not os.path.isfile(c):
            continue
        (drops if is_wave(c) else keeps).append(c)

    if dry_run:
        plan = [f"{name} -> {src_name}"]
        if drops:
            verb = "删除" if remove_wav else "保留"
            plan.append(verb + " " + "、".join(os.path.basename(d) for d in drops))
        if keeps:
            plan.append("保留非 WAV 的 " + "、".join(os.path.basename(d) for d in keeps))
        if os.path.isfile(src):
            plan.append(f"覆盖已存在的 {src_name}")
        return "ok", f"{name}: " + "，".join(plan)

    if os.path.isfile(src):  # --force
        try:
            os.remove(src)
        except OSError as exc:
            return "fail", f"{name}: 无法覆盖 {src_name}（{exc}）"

    # 先还原源文件，再删 wav：中途失败也不会丢数据
    try:
        os.replace(mark, src)
    except OSError as exc:
        return "fail", f"{name}: 还原失败（{exc}）"

    note = ""
    if drops:
        if remove_wav:
            done, bad = [], []
            for d in drops:
                try:
                    os.remove(d)
                    done.append(os.path.basename(d))
                except OSError as exc:
                    bad.append(f"{os.path.basename(d)}({exc})")
            if done:
                note += "，已删除 " + "、".join(done)
            if bad:
                note += "，删除失败 " + "、".join(bad)
        else:
            note += "，已保留 " + "、".join(os.path.basename(d) for d in drops)
    if keeps:
        note += "，保留非 WAV 的 " + "、".join(os.path.basename(d) for d in keeps)

    return "ok", f"{name} -> {src_name}{note}"


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SELF_NAME,
        description="还原：把 .aud1 / .amr1 / .mpeg1 改回源文件，并删除对应的 wav",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  恢复.py                    处理本文件所在目录及其子目录
  恢复.py "D:\\录音"          只处理指定目录
  恢复.py a.amr1             只处理指定文件
  恢复.py a.aud.wav          按产物找备份并还原
  恢复.py -n                 只预览，不改动任何文件
""",
    )
    p.add_argument("paths", nargs="*",
                   help=f"文件或目录，默认本文件所在目录：{SELF_DIR}")
    p.add_argument("--ext", default=None,
                   help="逗号分隔的源扩展名，默认 " + ",".join(DEFAULT_EXTS))
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("-f", "--force", action="store_true",
                   help="允许覆盖已经存在的源文件")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="只打印将要执行的操作，不修改任何文件")
    p.add_argument("--keep-wav", action="store_true",
                   help="还原时保留 wav（默认会删除）")
    return p


def print_banner(paths: list[str], recursive: bool, remove_wav: bool,
                 dry_run: bool) -> None:
    log("=" * 62)
    log("  语音恢复（.aud1 / .amr1 / .mpeg1 -> 源文件）")
    log("  还原源文件" + ("，并删除对应的 wav" if remove_wav else "（保留 wav）"))
    log("  若源文件已存在则跳过，需要覆盖请加 -f")
    if dry_run:
        log("  ** 预览模式：不会修改任何文件 **")
    log(f"  {'递归' if recursive else '仅当前层'}处理：{'; '.join(paths)}")
    log("=" * 62)


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)

    args = [clean_arg(a) for a in opts.paths]
    paths = [a for a in args if a] or [SELF_DIR]
    exts = parse_exts(opts.ext)
    recursive = not opts.no_recursive
    remove_wav = not opts.keep_wav

    print_banner(paths, recursive, remove_wav, opts.dry_run)

    files = collect_markers(paths, exts, recursive)
    if not files:
        log("未找到 " + " / ".join(ext + "1" for ext in exts) + " 文件。")
        return 0

    stats = {"ok": 0, "skip": 0, "fail": 0}
    for mark in files:
        try:
            status, message = restore_one(mark, force=opts.force,
                                          remove_wav=remove_wav,
                                          dry_run=opts.dry_run)
        except OSError as exc:  # 磁盘 / 权限等意外错误
            status, message = "fail", f"{os.path.basename(mark)}: {exc}"
        stats[status] += 1
        log(f"[{LABELS[status]}] {message}")

    log("")
    if opts.dry_run:
        log(f"----- 预览结果: 计划 {stats['ok']} 个，跳过 {stats['skip']} 个，"
            f"失败 {stats['fail']} 个 -----")
    else:
        log(f"----- 完成: 还原 {stats['ok']} 个，跳过 {stats['skip']} 个，"
            f"失败 {stats['fail']} 个 -----")
    return 1 if stats["fail"] else 0


def set_console_title(text: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def wait_at_exit() -> None:
    """双击运行时让窗口停住方便看结果；从命令行/管道调用时不停。"""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    _rc = 1
    try:
        set_console_title(f"{SELF_NAME} - 恢复")
        _rc = main()
    except KeyboardInterrupt:
        log("\n已中断")
        _rc = 130
    except Exception as exc:  # 双击运行时给出可读错误而不是堆栈
        log(f"[错误] 运行出错: {exc}")
        _rc = 1
    wait_at_exit()
    sys.exit(_rc)
