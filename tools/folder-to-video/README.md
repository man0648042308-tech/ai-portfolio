# folder-to-video

读取一个文件夹里的图片，按规律（文件名 / 修改时间 / 拍摄时间）排序，合成为 MP4 视频。
基于 **Python + Pillow + FFmpeg**，`imageio-ffmpeg` 自带 ffmpeg，Windows 上无需另装。

## 最快用法（Windows）

1. 安装 [Python](https://www.python.org/downloads/)（勾选 *Add python.exe to PATH*）。
2. 把 `folder_to_video.py` 和 `make_video.bat` 放在同一目录。
3. 用记事本打开 `make_video.bat`，修改 `FOLDER` 为你的图片文件夹，双击运行。
4. 视频生成在桌面 `Ori_video.mp4`。

## 命令行

```bash
pip install pillow imageio-ffmpeg
python folder_to_video.py "C:\path\to\Ori" --sort mtime --duration 2 --fade 0.5
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `--sort name/mtime/exif` | 文件名自然排序 / 修改时间 / 拍摄时间 | `name` |
| `--reverse` | 倒序 | |
| `--every N` | 每 N 张取 1 张 | `1` |
| `--recursive` | 包含子文件夹 | |
| `--all-files` | 不看扩展名，尝试打开所有文件 | |
| `--duration S` | 每张停留秒数 | `2` |
| `--fade S` | 交叉淡化秒数，`0` 为硬切 | `0.5` |
| `--fps` | 帧率 | `30` |
| `--size WxH` | 分辨率，竖屏用 `1080x1920` | `1920x1080` |
| `--fit blur/black/crop` | 尺寸不一时：模糊背景 / 黑边 / 裁剪铺满 | `blur` |
| `--zoom` | 缓慢推镜（Ken Burns） | |
| `--caption none/name/date` | 左下角显示文件名或日期 | `none` |
| `--audio FILE` | 背景音乐（按视频长度截断） | |
| `--crf` | 画质，越小越清晰 | `20` |
| `-o FILE` | 输出路径 | `<文件夹>/output.mp4` |

示例：做成延时摄影效果 —— `--sort mtime --duration 0.1 --fade 0 --fps 25`
