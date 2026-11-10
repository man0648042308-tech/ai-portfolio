#!/usr/bin/env python3
"""
folder_to_video.py — 读取文件夹中的图片，按规律排序后合成为一段 MP4 视频。

规律（可组合）：
  --sort   name   文件名自然排序（img2 排在 img10 之前）  [默认]
           mtime  文件修改时间
           exif   照片拍摄时间（EXIF DateTimeOriginal，没有则退回修改时间）
  --reverse       倒序
  --every N       每隔 N 张取一张（抽帧）
  --duration S    每张图片停留 S 秒
  --fade S        相邻图片交叉淡入淡出 S 秒（0 = 硬切）
  --zoom          轻微 Ken Burns 推镜效果
  --caption       在画面左下角叠加文件名 / 日期
  --audio FILE    背景音乐（自动按视频时长截断）

依赖：pip install pillow imageio-ffmpeg
（imageio-ffmpeg 自带 ffmpeg.exe，无需单独安装 FFmpeg；若系统已有 ffmpeg 也会优先使用。）
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:
    sys.exit("缺少 Pillow，请先运行：pip install pillow imageio-ffmpeg")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


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


def exif_time(p: Path) -> float:
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
            if raw:
                return dt.datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return p.stat().st_mtime


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


def zoom_frame(base: Image.Image, t: float, strength: float = 0.06) -> Image.Image:
    """t ∈ [0,1]，从 1.0 缓慢放大到 1+strength。"""
    w, h = base.size
    s = 1 + strength * t
    cw, ch = w / s, h / s
    left, top = (w - cw) / 2, (h - ch) / 2
    return base.resize((w, h), Image.BILINEAR, box=(left, top, left + cw, top + ch))


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
    ap.add_argument("--sort", choices=["name", "mtime", "exif"], default="name")
    ap.add_argument("--reverse", action="store_true")
    ap.add_argument("--every", type=int, default=1, help="每隔 N 张取一张")
    ap.add_argument("--recursive", action="store_true", help="包含子文件夹")
    ap.add_argument("--all-files", action="store_true", help="不看扩展名，尝试打开所有文件")
    ap.add_argument("--duration", type=float, default=2.0, help="每张停留秒数")
    ap.add_argument("--fade", type=float, default=0.5, help="交叉淡化秒数，0 为硬切")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--size", default="1920x1080", help="分辨率，如 1920x1080 / 1080x1920")
    ap.add_argument("--fit", choices=["blur", "black", "crop"], default="blur",
                    help="尺寸不一致时：模糊背景 / 黑边 / 裁剪铺满")
    ap.add_argument("--zoom", action="store_true", help="Ken Burns 缓慢推镜")
    ap.add_argument("--caption", choices=["none", "name", "date"], default="none")
    ap.add_argument("--audio", help="背景音乐文件")
    ap.add_argument("--crf", type=int, default=20, help="画质，越小越清晰（18~28）")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        sys.exit(f"文件夹不存在：{folder}")
    out = Path(args.output) if args.output else folder / "output.mp4"
    w, h = (int(v) for v in args.size.lower().split("x"))
    w, h = w - w % 2, h - h % 2  # H.264 需要偶数尺寸

    files = [p for p in collect_images(folder, args.recursive, args.all_files) if p.resolve() != out.resolve()]
    key = {"name": natural_key, "mtime": lambda p: p.stat().st_mtime, "exif": exif_time}[args.sort]
    files.sort(key=key, reverse=args.reverse)
    files = files[:: max(1, args.every)]
    if not files:
        sys.exit("没有找到图片。若文件没有扩展名，请加 --all-files")

    hold = max(1, round(args.duration * args.fps))
    fade = min(round(args.fade * args.fps), hold // 2)
    font = load_font(max(18, h // 28)) if args.caption != "none" else None

    cmd = [find_ffmpeg(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(args.fps), "-i", "-"]
    if args.audio:
        cmd += ["-i", args.audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    print(f"共 {len(files)} 张图片，排序={args.sort}{'(倒序)' if args.reverse else ''}，输出 → {out}")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def render(path: Path):
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
        return frame, text

    def frame_at(item, i: int) -> Image.Image:
        base, text = item
        img = zoom_frame(base, i / max(1, hold - 1)) if args.zoom else base
        return draw_caption(img, text, font) if text else img

    prev = None
    written = 0
    try:
        bases = (b for b in (render(p) for p in files) if b is not None)
        for idx, cur in enumerate(bases):
            # 当前图：前 fade 帧与上一张的尾部交叉淡化
            start = 0
            if prev is not None and fade:
                for i in range(fade):
                    a = (i + 1) / (fade + 1)
                    mix = Image.blend(frame_at(prev, hold - fade + i), frame_at(cur, i), a)
                    proc.stdin.write(mix.tobytes())
                start = fade
            end = hold - fade  # 末尾 fade 帧留给下一张做过渡
            for i in range(start, end):
                proc.stdin.write(frame_at(cur, i).tobytes())
            prev = cur
            written += 1
            print(f"\r  进度 {written}/{len(files)}", end="", flush=True)
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
