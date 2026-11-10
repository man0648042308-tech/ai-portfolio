#!/usr/bin/env python3
"""
folder_to_video.py — 读取文件夹中的图片，按规律排序后合成为一段 MP4 视频。

最简单：加 --auto，脚本会先“读图”分析（横竖比例、是否连拍/延时序列、重复图、
小表情包等），自动选出最流畅好看的参数；--analyze 只打印分析报告不生成视频。

手动规律（可组合，手动指定的参数优先于 --auto 的推荐）：
  --sort   name   文件名自然排序（img2 排在 img10 之前）
           mtime  文件修改时间
           exif   照片拍摄时间（EXIF DateTimeOriginal，没有则退回修改时间）
  --reverse       倒序
  --every N       每隔 N 张取一张（抽帧）
  --duration S    每张图片停留 S 秒
  --fade S        相邻图片交叉淡入淡出 S 秒（0 = 硬切）
  --zoom          Ken Burns 缓慢推拉镜头（奇偶张交替推近/拉远）
  --caption       在画面左下角叠加文件名 / 日期
  --audio FILE    背景音乐（自动按视频时长截断）

依赖：pip install pillow imageio-ffmpeg
（imageio-ffmpeg 自带 ffmpeg.exe，无需单独安装 FFmpeg；若系统已有 ffmpeg 也会优先使用。）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:
    sys.exit("缺少 Pillow，请先运行：pip install pillow imageio-ffmpeg")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

# 手动模式下的默认值（--auto 时由分析结果决定）
DEFAULTS = dict(sort="name", duration=2.0, fade=0.5, fps=30, size="1920x1080", fit="blur", zoom=False)


# ---------------------------------------------------------------- 工具函数
def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("找不到 ffmpeg，请运行：pip install imageio-ffmpeg")


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def exif_dt(p: Path) -> float | None:
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
            if raw:
                return dt.datetime.strptime(str(raw).strip().rstrip("\x00"), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return None


def exif_time(p: Path) -> float:
    t = exif_dt(p)
    return t if t is not None else p.stat().st_mtime


def collect_images(folder: Path, recursive: bool, all_files: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    files = [p for p in it if p.is_file()]
    if all_files:
        return files  # QQ 缓存里有时没有扩展名，交给 Pillow 去识别
    return [p for p in files if p.suffix.lower() in IMAGE_EXTS]


def load_font(size: int):
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑
        r"C:\Windows\Fonts\simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def smooth(x: float) -> float:
    """smoothstep 缓动：开头结尾慢、中间快，过渡更柔和。"""
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


# ---------------------------------------------------------------- 读图分析
def probe(p: Path) -> dict | None:
    """读取一张图的尺寸、是否动图、16×16 灰度缩略图（用于判重和相邻相似度）。"""
    try:
        with Image.open(p) as im:
            w, h = im.size
            if im.getexif().get(274, 1) in (5, 6, 7, 8):  # EXIF 旋转 90°
                w, h = h, w
            anim = getattr(im, "n_frames", 1) > 1
            im.seek(0)
            im.draft("L", (64, 64))  # JPEG 快速解码小图
            th = ImageOps.exif_transpose(im).convert("L").resize((16, 16), Image.BILINEAR)
            pix = th.tobytes()
    except Exception:
        return None
    mean = sum(pix) / len(pix)
    small = bytes(pix[r * 16 + c] for r in range(0, 16, 2) for c in range(0, 16, 2))
    ahash = sum(1 << i for i, v in enumerate(small) if v > mean)
    return dict(path=p, w=w, h=h, anim=anim, pix=pix, ahash=ahash, exif=exif_dt(p))


def pix_diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def analyze(files: list[Path], args) -> tuple[list[Path], dict, list[str]]:
    """返回 (保留的文件, 推荐参数, 报告行)。"""
    total = len(files)
    infos, report = [], []
    for i, p in enumerate(files, 1):
        info = probe(p)
        if info:
            infos.append(info)
        print(f"\r  读图分析 {i}/{total}", end="", flush=True)
    print()
    unreadable = total - len(infos)

    rec: dict = {}
    # 1) 排序：多数照片带拍摄时间 → 按拍摄时间；否则按修改时间（QQ 文件名是哈希，按名字排没有意义）
    with_exif = sum(1 for x in infos if x["exif"])
    if args.sort is None:
        rec["sort"] = "exif" if infos and with_exif / len(infos) >= 0.5 else "mtime"

    # 2) 过滤：小图（表情包/缩略图）、动图表情
    min_side = args.min_side if args.min_side is not None else 300
    small = sum(min(x["w"], x["h"]) < min_side for x in infos)
    anim = sum(x["anim"] and min(x["w"], x["h"]) >= min_side for x in infos) if not args.keep_gif else 0
    kept = [x for x in infos if min(x["w"], x["h"]) >= min_side and (args.keep_gif or not x["anim"])]

    sort = args.sort or rec.get("sort", DEFAULTS["sort"])
    key = {"name": lambda x: natural_key(x["path"]),
           "mtime": lambda x: x["path"].stat().st_mtime,
           "exif": lambda x: x["exif"] or x["path"].stat().st_mtime}[sort]
    kept.sort(key=key, reverse=args.reverse)

    # 3) 节奏：相邻图片很像（连拍 / 延时 / 录屏帧）→ 序列；否则幻灯片
    diffs = [pix_diff(a["pix"], b["pix"]) for a, b in zip(kept, kept[1:])]
    med = statistics.median(diffs) if diffs else 99.0
    is_seq = med < 14 and len(kept) >= 10

    # 4) 去重：幻灯片按画面相似去重（QQ 里同一张图常被存多次）；序列只去掉字节完全相同的文件，保留细微变化
    dup = 0
    if not args.keep_dupes:
        seen: dict = {}
        uniq = []
        for x in kept:
            if is_seq:
                k = hashlib.md5(x["path"].read_bytes()).hexdigest()
                if k in seen:
                    dup += 1
                    continue
                seen[k] = True
            else:
                if any(pix_diff(x["pix"], o) < 3 for o in seen.get(x["ahash"], [])):
                    dup += 1
                    continue
                seen.setdefault(x["ahash"], []).append(x["pix"])
            uniq.append(x)
        kept = uniq
    kept = kept[:: max(1, args.every)]
    n = len(kept)

    # 5) 画面方向：按多数图片的宽高比决定横屏 / 竖屏 / 方形
    ratios = [x["w"] / x["h"] for x in kept] or [16 / 9]
    portrait = sum(r < 0.85 for r in ratios)
    square = sum(0.85 <= r <= 1.18 for r in ratios)
    landscape = n - portrait - square
    if portrait >= max(landscape, square):
        size, target = "1080x1920", 9 / 16
    elif square > landscape:
        size, target = "1080x1080", 1.0
    else:
        size, target = "1920x1080", 16 / 9

    # 6) 填充方式：八成以上比例接近 → 裁剪铺满（无黑边）；否则模糊背景
    close = sum(abs(r / target - 1) < 0.08 for r in ratios)
    fit = "crop" if n and close / n >= 0.8 else "blur"

    # 7) 参数：序列 → 快速连播 + 帧间柔化；幻灯片 → 停留 + 缓动淡化 + 推拉镜头
    if is_seq:
        mode = "序列（连拍/延时）"
        duration = 0.2 if n < 150 else 0.12
        fade, zoom = round(duration / 2, 3), False
    else:
        mode = "幻灯片"
        duration = round(min(3.0, max(1.5, 150 / max(n, 1))), 2)  # 控制总时长约 2~3 分钟
        fade, zoom = round(min(0.8, duration / 3), 2), True

    for k, v in dict(size=size, fit=fit, duration=duration, fade=fade, zoom=zoom, fps=30).items():
        rec[k] = v

    hold = duration
    est = n * hold - max(0, n - 1) * fade
    report += [
        "=" * 46,
        f"共 {total} 个文件：可读 {len(infos)}，无法读取 {unreadable}",
        f"过滤：小于 {min_side}px 的小图/表情 {small}，动图 {anim}，重复 {dup}  →  保留 {n} 张",
        f"方向：横 {landscape} / 竖 {portrait} / 方 {square}   比例接近画面的 {close}/{n}",
        f"带拍摄时间 {with_exif}/{len(infos)}   相邻差异中位数 {med:.1f}（<14 视为连续序列）",
        f"判定：{mode}",
        f"推荐：--sort {sort} --size {size} --fit {fit} --duration {duration} --fade {fade}"
        + (" --zoom" if zoom else ""),
        f"预计时长：{est:.0f} 秒",
        "=" * 46,
    ]
    return [x["path"] for x in kept], rec, report


# ---------------------------------------------------------------- 渲染
def fit_frame(img: Image.Image, w: int, h: int, mode: str) -> Image.Image:
    """把任意尺寸图片放进 w×h 画布。"""
    img = ImageOps.exif_transpose(img).convert("RGB")
    if mode == "crop":
        return ImageOps.fit(img, (w, h), Image.LANCZOS)
    fg = ImageOps.contain(img, (w, h), Image.LANCZOS)
    if mode == "blur":
        bg = ImageOps.fit(img, (w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
        bg = Image.blend(bg, Image.new("RGB", (w, h)), 0.35)
    else:  # black
        bg = Image.new("RGB", (w, h))
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def zoom_frame(base: Image.Image, t: float, zoom_in: bool, strength: float = 0.07) -> Image.Image:
    """t ∈ [0,1]。推近：1 → 1+strength；拉远：反向。带轻微平移，亚像素采样避免抖动。"""
    w, h = base.size
    t = t if zoom_in else 1 - t
    s = 1 + strength * t
    cw, ch = w / s, h / s
    dx = (w - cw) * (0.5 + (0.15 if zoom_in else -0.15) * t)
    dy = (h - ch) * 0.5
    return base.resize((w, h), Image.BICUBIC, box=(dx, dy, dx + cw, dy + ch))


def draw_caption(img: Image.Image, text: str, font) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    pad = img.height // 40
    x, y = pad, img.height - pad - font.size
    d.text((x, y), text, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    return img


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="把文件夹里的图片按规律合成视频")
    ap.add_argument("folder", help="图片所在文件夹")
    ap.add_argument("-o", "--output", help="输出文件，默认 <文件夹>/output.mp4")
    ap.add_argument("--auto", action="store_true", help="先读图分析，自动选择最流畅好看的参数")
    ap.add_argument("--analyze", action="store_true", help="只打印读图分析报告，不生成视频")
    ap.add_argument("--sort", choices=["name", "mtime", "exif"])
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--every", type=int, default=1, help="每隔 N 张取一张")
    ap.add_argument("--recursive", action="store_true", help="包含子文件夹")
    ap.add_argument("--all-files", action="store_true", help="不看扩展名，尝试打开所有文件")
    ap.add_argument("--min-side", type=int, help="短边小于此像素的图片跳过（--auto 默认 300）")
    ap.add_argument("--keep-gif", action="store_true", help="--auto 时保留动图（默认当表情包过滤）")
    ap.add_argument("--keep-dupes", action="store_true", help="--auto 时保留重复图片")
    ap.add_argument("--duration", type=float, help="每张停留秒数（默认 2）")
    ap.add_argument("--fade", type=float, help="交叉淡化秒数，0 为硬切（默认 0.5）")
    ap.add_argument("--fps", type=int, help="帧率（默认 30）")
    ap.add_argument("--size", help="分辨率，如 1920x1080 / 1080x1920")
    ap.add_argument("--fit", choices=["blur", "black", "crop"],
                    help="尺寸不一致时：模糊背景 / 黑边 / 裁剪铺满（默认 blur）")
    ap.add_argument("--zoom", action="store_true", default=None, help="Ken Burns 缓慢推拉镜头")
    ap.add_argument("--no-zoom", dest="zoom", action="store_false", help="关闭推拉镜头")
    ap.add_argument("--caption", choices=["none", "name", "date"], default="none")
    ap.add_argument("--audio", help="背景音乐文件")
    ap.add_argument("--crf", type=int, default=20, help="画质，越小越清晰（18~28）")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        sys.exit(f"文件夹不存在：{folder}")
    out = Path(args.output) if args.output else folder / "output.mp4"

    files = [p for p in collect_images(folder, args.recursive, args.all_files) if p.resolve() != out.resolve()]
    if not files:
        sys.exit("没有找到图片。若文件没有扩展名，请加 --all-files")

    if args.auto or args.analyze:
        files, rec, report = analyze(files, args)
        print("\n".join(report))
        if args.analyze:
            return
        for k, v in rec.items():  # 手动指定的参数优先
            if getattr(args, k) is None:
                setattr(args, k, v)
    else:
        if args.min_side:
            files = [p for p in files if (lambda i: i and min(i["w"], i["h"]) >= args.min_side)(probe(p))]
    for k, v in DEFAULTS.items():
        if getattr(args, k) is None:
            setattr(args, k, v)
    if not (args.auto or args.analyze):
        key = {"name": natural_key, "mtime": lambda p: p.stat().st_mtime, "exif": exif_time}[args.sort]
        files.sort(key=key, reverse=args.reverse)
        files = files[:: max(1, args.every)]
    if not files:
        sys.exit("过滤后没有剩下图片，可调小 --min-side 或加 --keep-gif")

    w, h = (int(v) for v in args.size.lower().split("x"))
    w, h = w - w % 2, h - h % 2  # H.264 需要偶数尺寸
    hold = max(1, round(args.duration * args.fps))
    fade = min(round(args.fade * args.fps), hold // 2)
    font = load_font(max(18, h // 28)) if args.caption != "none" else None

    cmd = [find_ffmpeg(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(args.fps), "-i", "-"]
    if args.audio:
        cmd += ["-i", args.audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest",
                "-af", "afade=t=in:d=1"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    print(f"共 {len(files)} 张图片，排序={args.sort}{'(倒序)' if args.reverse else ''}，"
          f"{w}x{h} 每张 {args.duration}s 淡化 {args.fade}s{' 推拉镜头' if args.zoom else ''}，输出 → {out}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def render(idx: int, path: Path):
        try:
            with Image.open(path) as im:
                im.seek(0)
                frame = fit_frame(im, w, h, args.fit)
        except Exception as e:
            print(f"  跳过 {path.name}: {e}")
            return None
        text = None
        if args.caption == "name":
            text = path.stem
        elif args.caption == "date":
            text = dt.datetime.fromtimestamp(exif_time(path)).strftime("%Y-%m-%d %H:%M")
        return frame, text, idx % 2 == 0

    def frame_at(item, i: int) -> Image.Image:
        base, text, zoom_in = item
        img = zoom_frame(base, i / max(1, hold - 1), zoom_in) if args.zoom else base
        return draw_caption(img, text, font) if text else img

    prev = None
    written = 0
    try:
        bases = (b for b in (render(i, p) for i, p in enumerate(files)) if b is not None)
        for cur in bases:
            # 当前图：前 fade 帧与上一张的尾部交叉淡化（缓动曲线）
            start = 0
            if prev is not None and fade:
                for i in range(fade):
                    a = smooth((i + 1) / (fade + 1))
                    mix = Image.blend(frame_at(prev, hold - fade + i), frame_at(cur, i), a)
                    proc.stdin.write(mix.tobytes())
                start = fade
            end = hold - fade  # 末尾 fade 帧留给下一张做过渡
            for i in range(start, end):
                proc.stdin.write(frame_at(cur, i).tobytes())
            prev = cur
            written += 1
            print(f"\r  渲染 {written}/{len(files)}", end="", flush=True)
        if prev is not None and fade:  # 最后一张把预留的尾帧补上
            for i in range(hold - fade, hold):
                proc.stdin.write(frame_at(prev, i).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    print()
    if proc.returncode != 0:
        sys.exit("ffmpeg 编码失败")
    print(f"完成：{out}")


if __name__ == "__main__":
    main()
