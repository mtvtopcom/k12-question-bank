#!/usr/bin/env python3
"""把桌面工作台打包成 macOS .app bundle（无需 Xcode / 第三方依赖）。

结构：
    desktop/K12 题库工厂.app/
        Contents/
            Info.plist
            MacOS/launcher        # shell 启动器，找到 python3 并运行 app.py
            Resources/app.icns    # 由 assets/logo.png 生成的多尺寸图标
            Resources/logo.png    # 项目 Logo 副本
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
LOGO_SRC = os.path.join(PROJECT_ROOT, "assets", "logo.png")
APP_NAME = "K12 题库工厂"
APP_VERSION = "1.8.0"
BUNDLE = os.path.join(HERE, f"{APP_NAME}.app")
CONTENTS = os.path.join(BUNDLE, "Contents")
MACOS = os.path.join(CONTENTS, "MacOS")
RESOURCES = os.path.join(CONTENTS, "Resources")
BUNDLE_PROJECT = os.path.join(RESOURCES, "project")


def find_python() -> str:
    """探测一个带 tkinter 的 python3，优先当前解释器。"""
    candidates = [sys.executable, "/opt/homebrew/bin/python3",
                  "/usr/local/bin/python3", "/usr/bin/python3"]
    seen = set()
    for p in candidates:
        if not p or p in seen or not os.path.exists(p):
            continue
        seen.add(p)
        try:
            import subprocess
            r = subprocess.run([p, "-c", "import tkinter"],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return "python3"


def make_launcher(release: bool = False) -> str:
    # app.py 在 bundle 外（desktop/src/app.py）。查找顺序：
    # 1) .app 仍在项目 desktop/ 下 → 用项目源码（开发时改代码立即生效）
    # 2) 构建时写入的绝对路径（.app 单独拖到桌面 / Applications 时兜底）
    # 3) bundle 内自带的源码副本（Release 分发用，整个 .app 可独立移动）
    # Release 构建会跳过第 2 步，避免把打包机器上的绝对路径带进分发包。
    import shlex
    abs_fallback = ""
    if not release:
        abs_fallback = (
            'if [ ! -f "$APP_SRC" ]; then\n'
            f'  APP_SRC={shlex.quote(os.path.join(HERE, "src", "app.py"))}\n'
            "fi\n"
        )
    return f'''#!/bin/bash
# 由 build_app.py 生成，请勿手改
APP_SRC="$(cd "$(dirname "$0")/../../../src" 2>/dev/null && pwd)/app.py"
{abs_fallback}if [ ! -f "$APP_SRC" ]; then
  APP_SRC="$(cd "$(dirname "$0")/../Resources/project/desktop/src" 2>/dev/null && pwd)/app.py"
fi
for PY in "$WORKBUDDY_PYTHON" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  [ -n "$PY" ] || continue
  if "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    exec "$PY" "$APP_SRC" "$@"
  fi
done
echo "未找到带 tkinter 的 Python 3，请安装 python.org 或 Homebrew Python" >&2
exit 1
'''


def make_icon():
    """用项目 Logo 生成多尺寸 ICNS；系统工具缺失时回退占位图标。"""
    if os.path.exists(LOGO_SRC):
        iconset = os.path.join(HERE, "build", "app.iconset")
        shutil.rmtree(iconset, ignore_errors=True)
        os.makedirs(iconset, exist_ok=True)
        sizes = [
            ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
        ]
        try:
            for name, px in sizes:
                subprocess.run(
                    ["/usr/bin/sips", "-z", str(px), str(px), LOGO_SRC,
                     "--out", os.path.join(iconset, name)],
                    check=True, capture_output=True)
            subprocess.run(
                ["/usr/bin/iconutil", "-c", "icns", iconset,
                 "-o", os.path.join(RESOURCES, "app.icns")],
                check=True, capture_output=True)
            shutil.copy2(LOGO_SRC, os.path.join(RESOURCES, "logo.png"))
            return True
        except Exception as e:
            print(f"[图标] 系统工具生成失败，改用占位图标: {e}")
    return _make_placeholder_icon()


def _make_placeholder_icon():
    """用标准库生成一个简单的 ICNS（无 Logo 时的兜底）。"""
    try:
        import struct
        import zlib
        size = 256
        # 生成纯色带圆角感觉的 RGBA 图（简单渐变蓝）
        raw = bytearray()
        for y in range(size):
            raw.append(0)
            for x in range(size):
                # 简单的对角渐变 + 圆角裁切
                r = int(30 + 90 * x / size)
                g = int(90 + 70 * y / size)
                b = 200
                a = 255
                if (x < 24 and y < 24) or (x > size - 24 and y > size - 24):
                    a = 0
                raw += bytes((r, g, b, a))

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(
                ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
               + chunk(b"IEND", b""))
        # ICNS: 一个 ic08 (256x256 PNG) 条目
        entry = b"ic08" + struct.pack(">I", 8 + len(png)) + png
        icns = b"icns" + struct.pack(">I", 8 + len(entry)) + entry
        with open(os.path.join(RESOURCES, "app.icns"), "wb") as f:
            f.write(icns)
        return True
    except Exception as e:
        print(f"[跳过图标] {e}")
        return False


def bundle_sources():
    """把运行所需的数据与源码复制进 .app，分发后整个 .app 可独立移动/运行。"""
    shutil.rmtree(BUNDLE_PROJECT, ignore_errors=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo",
                                    "output", "backups", "build", "*.app")
    for rel in ("desktop/src", "desktop/original", "knowledge-map", "src", "assets"):
        src = os.path.join(PROJECT_ROOT, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(BUNDLE_PROJECT, rel),
                            dirs_exist_ok=True, ignore=ignore)
    # 出厂快照与知识点地图必须存在，否则 .app 独立运行时会缺数据
    required = [
        os.path.join(BUNDLE_PROJECT, "knowledge-map", "volumes"),
        os.path.join(BUNDLE_PROJECT, "desktop", "original", "library.json.gz"),
    ]
    missing = [p for p in required if not os.path.exists(p)]
    if missing:
        raise SystemExit("打包失败，缺少运行数据：" + ", ".join(missing))


def build(release: bool = False):
    for d in (MACOS, RESOURCES):
        os.makedirs(d, exist_ok=True)

    # 清掉旧名字的 bundle，避免桌面上出现两个入口
    legacy = os.path.join(HERE, "K12知识点地图.app")
    if os.path.exists(legacy):
        shutil.rmtree(legacy)

    py = find_python()
    launcher = os.path.join(MACOS, "launcher")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(make_launcher(release))
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.k12.questionfactory",
        "CFBundleVersion": APP_VERSION,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleExecutable": "launcher",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "app.icns",
        "LSMinimumSystemVersion": "10.14",
        "NSHighResolutionCapable": True,
        # 固定浅色外观：不跟随 macOS 深色模式，始终使用白天配色
        "NSRequiresAquaSystemAppearance": True,
    }
    with open(os.path.join(CONTENTS, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    icon_ok = make_icon()
    bundle_sources()
    print(f"已生成 {BUNDLE}")
    print(f"  版本: {APP_VERSION}{'（Release 构建）' if release else ''}")
    print(f"  构建时探测到可用 Python: {py}（运行时会重新探测）")
    print(f"  图标: {'app.icns' if icon_ok else '无（不影响运行）'}")
    print("  .app 已内置源码与知识点数据，可连同项目或单独移动运行。")
    print("双击 .app 即可启动；若 macOS 提示未验证开发者，右键 → 打开。")


if __name__ == "__main__":
    build(release="--release" in sys.argv[1:])
