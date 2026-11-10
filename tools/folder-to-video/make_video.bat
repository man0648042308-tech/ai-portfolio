@echo off
chcp 65001 >nul
REM ==== 修改这里：图片文件夹 和 参数 ====
set "FOLDER=C:\Users\12868\Documents\Tencent Files\1286853952\nt_qq\nt_data\Pic\2026-11\Ori"
set "OPTS=--sort mtime --duration 2 --fade 0.5 --fit blur --size 1920x1080"
REM =====================================

where python >nul 2>nul || (echo 未找到 Python，请先安装：https://www.python.org/downloads/ （安装时勾选 Add to PATH） & pause & exit /b 1)
python -m pip install -q pillow imageio-ffmpeg
python "%~dp0folder_to_video.py" "%FOLDER%" %OPTS% -o "%USERPROFILE%\Desktop\Ori_video.mp4"
echo.
echo 视频已保存到桌面：Ori_video.mp4
pause
