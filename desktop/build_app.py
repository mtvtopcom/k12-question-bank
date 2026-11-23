#!/usr/bin/env python3
"""把桌面管理台打包成 macOS .app bundle（无需 Xcode / 第三方依赖）。

结构：
    desktop/K12知识点地图.app/
        Contents/
            Info.plist
            MacOS/launcher        # shell 启动器，找到 python3 并运行 app.py
            Resources/app.icns    # 可选图标
"""
from __future__ import annotations

import os
import stat
import sys
import plistlib

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "K12知识点地图"
BUNDLE = os.path.join(HERE, f"{APP_NAME}.app")
CONTENTS = os.path.join(BUNDLE, "Contents")
MACOS = os.path.join(CONTENTS, "MacOS")
RESOURCES = os.path.join(CONTENTS, "Resources")


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


def make_launcher() -> str:
    # app.py 在 bundle 外（desktop/src/app.py）。优先使用构建时写入的
    # 绝对路径，这样把 .app 复制到桌面 / Applications 也能启动；
    # 相对路径作为换机器、迁移项目后的兜底。
    import shlex
    abs_app_src = shlex.quote(os.path.join(HERE, "src", "app.py"))
    return f'''#!/bin/bash
# 由 build_app.py 生成，请勿手改
APP_SRC={abs_app_src}
if [ ! -f "$APP_SRC" ]; then
  APP_SRC="$(cd "$(dirname "$0")/../../../src" && pwd)/app.py"
fi
if [ ! -f "$APP_SRC" ]; then
  APP_SRC="$(cd "$(dirname "$0")/../.." && pwd)/src/app.py"
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
    """用标准库生成一个简单的 ICNS（单色 PNG 风格占位）；失败则跳过。"""
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


def build():
    for d in (MACOS, RESOURCES):
        os.makedirs(d, exist_ok=True)

    py = find_python()
    launcher = os.path.join(MACOS, "launcher")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(make_launcher())
    os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "com.k12.knowledgemap.console",
        "CFBundleVersion": "2.0.1",
        "CFBundleShortVersionString": "2.0.1",
        "CFBundleExecutable": "launcher",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "app.icns",
        "LSMinimumSystemVersion": "10.14",
        "NSHighResolutionCapable": True,
    }
    with open(os.path.join(CONTENTS, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    icon_ok = make_icon()
    print(f"已生成 {BUNDLE}")
    print(f"  构建时探测到可用 Python: {py}（运行时会重新探测）")
    print(f"  图标: {'app.icns' if icon_ok else '无（不影响运行）'}")
    print("双击 .app 即可启动；若 macOS 提示未验证开发者，右键 → 打开。")


if __name__ == "__main__":
    build()
