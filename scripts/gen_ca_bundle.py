"""生成 TLS CA bundle: certifi 根证书 + 本机证书存储。

用途: 后端的 httpx / OpenAI SDK 默认只信任 certifi 自带的根证书, 而企业网络、
安全软件或代理安装的可信根证书只存在于 Windows 证书存储, 于是 HTTPS 请求报
`CERTIFICATE_VERIFY_FAILED`, 上层表现为「AI 服务连接失败, 请检查 AI Base URL / 网络」。
本脚本把两者合并成一个 PEM, 由 dev.ps1 通过 `SSL_CERT_FILE` 注入后端进程。

非 Windows 上 `ssl.enum_certificates` 不存在, 退化为原样输出 certifi bundle(空操作)。

用法(须用后端 venv 的解释器, certifi 才可见):
    cd backend && uv run --no-sync python ../scripts/gen_ca_bundle.py [输出路径]
"""

from __future__ import annotations

import base64
import os
import ssl
import sys
import tempfile

import certifi

# 只导出可信根与中间 CA, 不导出个人证书。
_WINDOWS_STORES = ("ROOT", "CA")


def default_output() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "TSP", "ca-bundle.pem")


def _to_pem(der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----"


def build_bundle() -> tuple[str, int]:
    with open(certifi.where(), encoding="utf-8") as fh:
        parts = [fh.read().rstrip()]

    added = 0
    for store in _WINDOWS_STORES:
        try:
            entries = ssl.enum_certificates(store)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            break  # 非 Windows 或存储不可读: 保持 certifi 默认行为
        for cert, _encoding, _trust in entries:
            parts.append(_to_pem(cert))
            added += 1
    return "\n".join(parts) + "\n", added


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else default_output()
    pem, added = build_bundle()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(pem)
    print(f"CA bundle: {out} ({len(pem)} bytes, 本机证书 {added} 张)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
