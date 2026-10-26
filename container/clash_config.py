from __future__ import annotations

import base64
import pathlib
import sys


def normalize_subscription(raw: bytes) -> str:
    """把订阅内容整理成只监听本机 7890 的 mihomo 配置。"""
    text = _as_config_text(raw)
    lines_out: list[str] = []
    seen_mixed = False
    seen_allow = False
    seen_bind = False
    skipping_tun = False
    for line in text.splitlines():
        if skipping_tun:
            if not line.strip() or line[:1] in {" ", "\t"} or line.lstrip().startswith("#"):
                continue
            skipping_tun = False
        if not line.strip() or line[:1] in {" ", "\t"} or line.lstrip().startswith("#"):
            lines_out.append(line)
            continue
        key = line.split(":", 1)[0].strip()
        if key in {"port", "socks-port", "redir-port", "tproxy-port", "external-controller", "external-ui"}:
            continue
        if key == "tun":
            skipping_tun = True
            continue
        if key == "mixed-port":
            lines_out.append("mixed-port: 7890")
            seen_mixed = True
            continue
        if key == "allow-lan":
            lines_out.append("allow-lan: false")
            seen_allow = True
            continue
        if key == "bind-address":
            lines_out.append("bind-address: 127.0.0.1")
            seen_bind = True
            continue
        lines_out.append(line)
    header: list[str] = []
    if not seen_mixed:
        header.append("mixed-port: 7890")
    if not seen_allow:
        header.append("allow-lan: false")
    if not seen_bind:
        header.append("bind-address: 127.0.0.1")
    lines_out.extend(["tun:", "  enable: false"])
    return "\n".join([*header, *lines_out]).rstrip() + "\n"


def _as_config_text(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").lstrip("\ufeff")
    if _looks_like_config(text):
        return text
    compact = "".join(text.split())
    try:
        decoded = base64.b64decode(compact, validate=False)
    except Exception as exc:
        raise ValueError("subscription is not a mihomo config") from exc
    decoded_text = decoded.decode("utf-8", "replace")
    if not _looks_like_config(decoded_text):
        raise ValueError("subscription is not a mihomo config")
    return decoded_text


def _looks_like_config(text: str) -> bool:
    return "proxies:" in text or "proxy-providers:" in text


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: clash_config.py SOURCE DEST", file=sys.stderr)
        return 2
    source = pathlib.Path(args[0])
    destination = pathlib.Path(args[1])
    try:
        text = normalize_subscription(source.read_bytes())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
