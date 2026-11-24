#!/usr/bin/env python3
"""给 urllib 准备一份可用的根证书。

python.org 的 macOS 安装包默认不安装 CA 证书（需要手动运行
「Install Certificates.command」），此时 ``ssl.create_default_context()`` 的证书库是空的，
任何 HTTPS 请求都会报::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

这里按「系统钥匙串 → /etc/ssl/cert.pem → certifi」的顺序收集根证书，去重、校验后
缓存到数据目录（默认 7 天），后续请求都用它。只是补齐本机缺失的信任锚，**不降低校验强度**。

如果仍然失败，通常是公司代理在替换证书：把代理的根证书装进钥匙串并设为「始终信任」即可。
"""
from __future__ import annotations

import os
import ssl
import subprocess
import time
from typing import List, Tuple

CACHE_TTL = 7 * 24 * 3600          # 缓存有效期（秒）
BEGIN = b"-----BEGIN CERTIFICATE-----"
END = b"-----END CERTIFICATE-----"
# 少于这个数量说明没取到真正的根证书库
MIN_CERTS = 5

KEYCHAINS = (
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
)
CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/usr/local/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
)


def _run(argv: List[str], timeout: float) -> bytes:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001 —— 缺 security 命令、超时都只是拿不到，不致命
        return b""
    return proc.stdout if proc.returncode == 0 else b""


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def keychain_pem() -> bytes:
    """导出钥匙串里所有证书（含系统根证书与管理员的代理根证书）。"""
    argv = ["/usr/bin/security", "find-certificate", "-a", "-p", *KEYCHAINS]
    login = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    if os.path.exists(login):
        argv.append(login)
    return _run(argv, 25)


def _split(pem: bytes) -> List[bytes]:
    """把一堆 PEM 拆成单个证书块，顺带丢掉解析不了的。"""
    blocks: List[bytes] = []
    for chunk in pem.split(END):
        if BEGIN not in chunk:
            continue
        block = chunk.strip() + b"\n" + END + b"\n"
        try:
            ssl.PEM_cert_to_DER_cert(block.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            continue
        blocks.append(block)
    return blocks


def collect() -> Tuple[bytes, int]:
    """汇总本机所有可信根证书，返回 (PEM 内容, 证书张数)。"""
    seen = set()
    parts: List[bytes] = []
    sources = [keychain_pem()]
    sources += [_read(p) for p in CA_FILES]
    for blob in sources:
        for block in _split(blob):
            if block in seen:
                continue
            seen.add(block)
            parts.append(block)
    try:  # certifi 兜底：部分用户装了 pip 包
        import certifi  # noqa: WPS433
        for block in _split(_read(certifi.where())):
            if block not in seen:
                seen.add(block)
                parts.append(block)
    except Exception:  # noqa: BLE001
        pass
    return b"".join(parts), len(parts)


def _fresh(path: str) -> bool:
    try:
        return time.time() - os.path.getmtime(path) < CACHE_TTL
    except OSError:
        return False


def prepare(cache_path: str) -> Tuple[ssl.SSLContext, str]:
    """返回 (SSL 上下文, 说明文字)。说明文字用于界面提示，可读即可。"""
    default = ssl.create_default_context()
    try:
        have = len(default.get_ca_certs())
    except Exception:  # noqa: BLE001
        have = 0
    if have:
        return default, f"使用 Python 自带证书库（{have} 张）"

    # 缓存命中就直接用，省掉每次启动调 security 的开销
    if _fresh(cache_path) and os.path.getsize(cache_path) > 1024:
        try:
            ctx = ssl.create_default_context(cafile=cache_path)
            return ctx, f"使用系统根证书缓存（{len(ctx.get_ca_certs())} 张）"
        except Exception:  # noqa: BLE001
            pass

    pem, count = collect()
    if count < MIN_CERTS:
        return default, "本机没有可用的根证书"
    try:
        folder = os.path.dirname(cache_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(pem)
        os.chmod(tmp, 0o644)
        os.replace(tmp, cache_path)
    except OSError:
        # 写不进数据目录也不要紧，直接用内存里的证书
        try:
            ctx = ssl.create_default_context(cadata=pem.decode("ascii"))
            return ctx, f"使用系统根证书（{count} 张，内存态）"
        except Exception:  # noqa: BLE001
            return default, "根证书载入失败"
    try:
        ctx = ssl.create_default_context(cafile=cache_path)
        return ctx, f"已载入系统根证书（{count} 张）"
    except Exception as exc:  # noqa: BLE001
        return default, f"根证书载入失败：{exc}"


def hint(reason: str = "") -> str:
    """按 OpenSSL 的报错细分给建议，别把「对方自签名」说成「本机缺证书」。"""
    text = (reason or "").lower()
    if "self signed" in text or "self-signed" in text:
        return ("对方证书是自签名的：常见于本地/内网服务，或公司代理替换了证书。"
                "若是公司代理，请把代理根证书装进「钥匙串访问」并设为「始终信任」；"
                "若是自建测试服务，请换成受信任的证书")
    if "hostname mismatch" in text or "doesn't match" in text or "no matching" in text:
        return "证书域名和 Base URL 不一致，检查 Base URL 有没有写错或多加了路径"
    if "expired" in text:
        return "对方证书已过期，请联系服务商或稍后重试"
    if "unable to get local issuer" in text or "unable to get issuer" in text \
            or "unable to verify the first" in text:
        return ("本机 Python 缺少根证书（python.org 安装包常见）。已自动改用系统证书库重试；"
                "若仍失败，请双击 /Applications/Python 3.x/Install Certificates.command，"
                "或确认公司代理的根证书已装进钥匙串并设为「始终信任」")
    return ("HTTPS 证书校验没通过。先确认网络没被代理劫持；"
            "本机 Python 缺根证书时可运行 Install Certificates.command 修复")
