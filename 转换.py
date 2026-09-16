#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音转 WAV —— 合并版（.aud / .amr / .mpeg 统一处理）

支持格式（按**文件头魔数**判断，不信任扩展名）
----------------------------------------------
  SILK V3   微信 / QQ 语音。文件头 b"#!SILK_V3" 或 b"\\x02#!SILK_V3"。
            常见于 .aud，也常被伪装成 .amr —— 用 pysilk 解码。
  AMR/AMR-WB 文件头 b"#!AMR" / b"#!AMR-WB" —— 用 ffmpeg 解码。
  其它      M4A / MP4 / AAC / MP3 等（例如伪装成 .mpeg 的 M4A）—— 用 ffmpeg 解码。

命名规则
--------
  输出: x.aud -> x.aud.wav      （原名后直接加 .wav）
  标记: x.aud -> x.aud1         （转换成功后，源文件改名，表示已处理）
  恢复: 运行 恢复.py，把 x.aud1 改回 x.aud，并删除 x.aud.wav

已有 wav 时的处理
-----------------
  源文件不比 wav 新 -> 只补标记，不重复转换（省时）
  源文件比 wav 新   -> 重新转换（源被改过，避免留下过期产物）

其它
----
  转换失败不改动源文件（先写临时文件，校验通过才落盘）
  支持 -n 预览，不动任何文件

用法
----
  python 转换.py                   处理本文件所在目录及其所有子目录
  python 转换.py "D:\\录音"         处理指定目录
  python 转换.py a.amr b.aud       处理指定文件
  python 转换.py -n                只预览，不改动任何文件
  python 转换.py --no-recursive    只处理当前层
  python 转换.py --rate 16000      指定输出采样率
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import wave

# 默认扫描的源文件扩展名（真实格式靠文件头判断，不看扩展名）
DEFAULT_EXTS = (".aud", ".amr", ".mpeg")

AMR_MAGIC = b"#!AMR"
AMRWB_MAGIC = b"#!AMR-WB"
SILK_MAGIC = b"#!SILK_V3"

# SILK 默认输出采样率：微信 SILK 内部就是 24000，按原样解出可保留全部质量
DEFAULT_SILK_RATE = 24000

SELF_PATH = os.path.abspath(sys.argv[0])
SELF_NAME = os.path.splitext(os.path.basename(SELF_PATH))[0]
SELF_DIR = os.path.dirname(SELF_PATH) or os.getcwd()

LABELS = {"ok": "完成", "mark": "标记", "skip": "跳过", "fail": "错误"}


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
    """清理命令行参数。

    资源管理器把文件/文件夹拖到 bat 上时路径末尾带反斜杠，而 MSVCRT 解析
    命令行时会把结尾的 `"...\\"` 变成 `...\\ "` —— 少一个闭合引号，前面
    还多出空格。这里逐层剥离空格、引号和尾部分隔符，还原成真实路径。
    """
    arg = arg.strip()
    while arg.endswith('"'):
        arg = arg[:-1].rstrip()
    while len(arg) > 3 and arg.endswith(("\\", "/")):
        arg = arg[:-1].rstrip()
    if len(arg) == 2 and arg.endswith(":"):
        arg += "\\"
    return arg


def parse_exts(text: str | None) -> tuple[str, ...]:
    """把 --ext 参数解析成小写扩展名元组。"""
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


# --------------------------------------------------------------------------- #
# 格式识别与依赖
# --------------------------------------------------------------------------- #
def peek_header(path: str, size: int = 16) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def detect_format(path: str) -> str:
    """返回 'silk' / 'amr' / 'amrwb' / 'other'。"""
    head = peek_header(path, 16)
    if head.startswith(AMRWB_MAGIC):
        return "amrwb"
    if head.startswith(AMR_MAGIC):
        return "amr"
    # 微信 SILK 有两种写法：b"#!SILK_V3" 和 b"\x02#!SILK_V3"（前面多一个字节）
    if SILK_MAGIC in head[:12]:
        return "silk"
    return "other"


def find_ffmpeg(explicit: str | None = None) -> str | None:
    """优先用 --ffmpeg 指定的，其次脚本同目录的 ffmpeg.exe，最后 PATH 里的。"""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    local = os.path.join(SELF_DIR, "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    return shutil.which("ffmpeg")


def wav_info(path: str) -> tuple[int, int, int]:
    """返回 (声道数, 采样率, 帧数)。不是合法 wav 时抛异常。"""
    with wave.open(path, "rb") as w:
        return w.getnchannels(), w.getframerate(), w.getnframes()


# --------------------------------------------------------------------------- #
# 两条解码通道
# --------------------------------------------------------------------------- #
def silk_to_wav(src: str, dst: str, rate: int) -> None:
    """用 pysilk 解码 SILK，写出 16bit 单声道 WAV。"""
    try:
        import pysilk  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "该文件是 SILK 格式，需要 pysilk 解码：pip install silk-python"
        ) from exc

    with open(src, "rb") as f:
        data = f.read()
    if not data:
        raise RuntimeError("文件为空")

    out = io.BytesIO()
    try:
        pysilk.decode(io.BytesIO(data), out, rate)
    except Exception as exc:
        raise RuntimeError(f"SILK 解码失败: {exc}") from exc

    pcm = out.getvalue()
    if not pcm:
        raise RuntimeError("SILK 解码结果为空")

    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def ffmpeg_to_wav(src: str, dst: str, ffmpeg: str | None, rate: int | None) -> None:
    """用 ffmpeg 解码 AMR / M4A / MP3 等，写出 16bit PCM WAV。"""
    if not ffmpeg:
        raise RuntimeError(
            "未找到 ffmpeg，无法解码该格式。"
            "请安装 ffmpeg 并加入 PATH，或把 ffmpeg.exe 放到脚本同目录。"
        )

    # 通用参数在前，-i 之后才是输出选项，顺序不能颠倒
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-i", src, "-vn", "-acodec", "pcm_s16le"]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd.append(dst)

    kwargs = {}
    if os.name == "nt":  # 避免弹出黑窗
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("ffmpeg 解码失败: " + (err[-1] if err else "未知错误"))


# --------------------------------------------------------------------------- #
# 扫描待处理文件
# --------------------------------------------------------------------------- #
def collect_sources(paths: list[str], exts: tuple[str, ...], recursive: bool) -> list[str]:
    """展开成待转换的源文件列表（保持顺序，按路径去重）。"""
    found: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            found.append(p)

    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    for name in sorted(files):
                        if os.path.splitext(name)[1].lower() in exts:
                            add(os.path.join(root, name))
            else:
                with os.scandir(p) as it:
                    for entry in sorted(it, key=lambda e: e.name):
                        if (entry.is_file()
                                and os.path.splitext(entry.name)[1].lower() in exts):
                            add(entry.path)
        elif os.path.isfile(p):
            add(p)
    return found


# --------------------------------------------------------------------------- #
# 核心：转换单个文件
# --------------------------------------------------------------------------- #
def wav_path_for(src: str) -> str:
    """x.aud -> x.aud.wav"""
    return src + ".wav"


def mark_path_for(src: str) -> str:
    """x.aud -> x.aud1"""
    return src + "1"


def convert_one(src: str, ffmpeg: str | None, rate: int | None,
                force: bool = False, dry_run: bool = False) -> tuple[str, str]:
    """转换单个文件，返回 (状态, 提示信息)，状态为 ok / mark / skip / fail。"""
    name = os.path.basename(src)
    wav = wav_path_for(src)
    mark = mark_path_for(src)
    wav_name = os.path.basename(wav)
    mark_name = os.path.basename(mark)

    if not os.path.isfile(src):
        return "fail", f"{name}: 找不到源文件"

    # 1) 已经转换过：标记还在
    if os.path.isfile(mark):
        return "skip", f"{name}: 已转换过（{mark_name} 已存在），无需处理"

    # 2) wav 已存在 -> 看源文件有没有被改过
    if os.path.isfile(wav) and not force:
        if os.path.getmtime(src) <= os.path.getmtime(wav):
            if dry_run:
                return "mark", f"{name}: 已有 {wav_name} 且源未修改，计划仅补标记 -> {mark_name}"
            try:
                os.replace(src, mark)
            except OSError as exc:
                return "fail", f"{name}: 补标记失败（{exc}）"
            return "mark", f"{name}: 已有 {wav_name}，未重复转换，仅补标记 -> {mark_name}"
        redo = f"（{name} 比 {wav_name} 新，重新转换）"
    else:
        redo = ""

    # 3) 预览
    if dry_run:
        action = "重新转换" if os.path.isfile(wav) else "转换"
        return "ok", f"{name}: 计划{action} -> {wav_name}，然后改名为 {mark_name}"

    fmt = detect_format(src)

    # 4) 解码到临时文件，校验通过才落盘
    tmp = f"{src}.{os.getpid()}.tmp.wav"
    try:
        if fmt == "silk":
            silk_to_wav(src, tmp, rate or DEFAULT_SILK_RATE)
        else:
            ffmpeg_to_wav(src, tmp, ffmpeg, rate)
        _nch, out_rate, frames = wav_info(tmp)
        if frames <= 0:
            raise RuntimeError("转换结果为空音频")
        os.replace(tmp, wav)
    except Exception as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return "fail", f"{name}: 转换失败（{exc}），已保留原文件"

    # 5) 打标记
    try:
        os.replace(src, mark)
    except OSError as exc:
        return "fail", f"{name}: 已生成 {wav_name}，但源文件改名失败: {exc}"

    seconds = frames / out_rate
    return "ok", (f"{name}: -> {wav_name}"
                  f"（{fmt}, {out_rate} Hz, {seconds:.2f}s），"
                  f"源文件改名为 {mark_name}{redo}")


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SELF_NAME,
        description="语音转 WAV（.aud / .amr / .mpeg 统一处理，成功后源文件改名为 <原名>1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  转换.py                    处理本文件所在目录及其子目录
  转换.py "D:\\录音"          只处理指定目录
  转换.py a.amr b.aud        只处理指定文件
  转换.py -n                 只预览，不改动任何文件
  转换.py --no-recursive     只处理当前层
  转换.py --rate 16000       指定输出采样率
""",
    )
    p.add_argument("paths", nargs="*",
                   help=f"文件或目录，默认本文件所在目录：{SELF_DIR}")
    p.add_argument("--ext", default=None,
                   help="逗号分隔的源扩展名，默认 " + ",".join(DEFAULT_EXTS))
    p.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    p.add_argument("-f", "--force", action="store_true",
                   help="即使 wav 已存在也重新转换")
    p.add_argument("--rate", type=int, default=None,
                   help=f"输出采样率；SILK 默认 {DEFAULT_SILK_RATE}，其它格式保持原始采样率")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="只打印将要执行的操作，不修改任何文件")
    p.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    return p


def print_banner(paths: list[str], recursive: bool, dry_run: bool) -> None:
    log("=" * 62)
    log("  语音转 WAV（.aud / .amr / .mpeg）")
    log("  按文件头自动识别：SILK -> pysilk，AMR / M4A / MP3 等 -> ffmpeg")
    log("  输出 x.aud.wav，成功后源文件改名为 x.aud1")
    log("  若 wav 已存在且源未修改，则只补标记不重复转换")
    if dry_run:
        log("  ** 预览模式：不会修改任何文件 **")
    log(f"  {'递归' if recursive else '仅当前层'}处理：{'; '.join(paths)}")
    log("=" * 62)


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)

    if opts.ffmpeg is not None and not os.path.isfile(opts.ffmpeg):
        log(f"[错误] 指定的 ffmpeg 不存在: {opts.ffmpeg}")
        return 1

    args = [clean_arg(a) for a in opts.paths]
    paths = [a for a in args if a] or [SELF_DIR]
    exts = parse_exts(opts.ext)
    recursive = not opts.no_recursive

    print_banner(paths, recursive, opts.dry_run)

    files = collect_sources(paths, exts, recursive)
    if not files:
        log(f"未找到 {'/'.join(exts)} 文件。")
        return 0

    ffmpeg = find_ffmpeg(opts.ffmpeg)
    stats = {"ok": 0, "mark": 0, "skip": 0, "fail": 0}
    for src in files:
        try:
            status, message = convert_one(src, ffmpeg, opts.rate,
                                          force=opts.force, dry_run=opts.dry_run)
        except OSError as exc:  # 磁盘 / 权限等意外错误
            status, message = "fail", f"{os.path.basename(src)}: {exc}"
        stats[status] += 1
        log(f"[{LABELS[status]}] {message}")

    log("")
    if opts.dry_run:
        log(f"----- 预览结果: 计划 {stats['ok']} 个，补标记 {stats['mark']} 个，"
            f"跳过 {stats['skip']} 个，失败 {stats['fail']} 个 -----")
    else:
        log(f"----- 完成: 转换 {stats['ok']} 个，补标记 {stats['mark']} 个，"
            f"跳过 {stats['skip']} 个，失败 {stats['fail']} 个 -----")
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
        set_console_title(f"{SELF_NAME} - 转换")
        _rc = main()
    except KeyboardInterrupt:
        log("\n已中断")
        _rc = 130
    except Exception as exc:  # 双击运行时给出可读错误而不是堆栈
        log(f"[错误] 运行出错: {exc}")
        _rc = 1
    wait_at_exit()
    sys.exit(_rc)
