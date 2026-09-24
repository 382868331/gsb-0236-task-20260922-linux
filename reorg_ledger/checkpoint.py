"""检查点序列化：字节流封装、校验和、指纹（仅标准库）。

磁盘/字节布局（ASCII 头部 + JSON 载荷）::

    RLCK1\\n<sha256-hex>\\n<canonical-json-payload>

* 头部 ``RLCK1`` 是 magic/版本信封；``sha256-hex`` 是载荷的 SHA-256，用于检测
  字节级损坏（翻转、截断、拼接错误）。
* 载荷内另带一个**语义指纹**（见 :func:`fingerprint`），覆盖锚点状态与恢复后
  重算出的 tip 状态，用于检测“字节合法但内容被篡改/内部不一致”。

校验和与指纹都不是密码学承诺（无密钥），它们的职责是把损坏数据变成明确的
:class:`~reorg_ledger.errors.CheckpointCorruptError`，而不是恢复出一个看似可用、
实则错误的账本。
"""

from __future__ import annotations

import hashlib
import json

from .errors import CheckpointCorruptError

__all__ = [
    "MAGIC",
    "VERSION",
    "encode_payload",
    "decode_payload",
    "fingerprint",
]

MAGIC = "reorg-ledger-checkpoint"
VERSION = 1

_PREFIX = b"RLCK1\n"
_DIGEST_LEN = 64  # sha256 hex


def _canonical_dumps(payload: object) -> bytes:
    """确定性 JSON：sort_keys + 紧凑分隔，保证指纹/校验和逐字节稳定。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encode_payload(payload: dict) -> bytes:
    """把载荷字典编码为带校验和的检查点字节流。"""
    body = _canonical_dumps(payload)
    digest = hashlib.sha256(body).hexdigest().encode("ascii")
    return _PREFIX + digest + b"\n" + body


def decode_payload(data: bytes | bytearray) -> dict:
    """解析检查点字节流并验证信封与校验和，返回载荷字典。

    任何损坏都抛 :class:`CheckpointCorruptError`（``kind`` 为稳定短码：
    ``bad-encoding`` / ``bad-wrapper`` / ``bad-checksum`` / ``bad-json``）。
    """
    if isinstance(data, str) or not isinstance(data, (bytes, bytearray)):
        raise CheckpointCorruptError(
            "bad-encoding",
            f"checkpoint must be bytes-like, got {type(data).__name__}",
        )
    raw = bytes(data)
    if not raw.startswith(_PREFIX):
        raise CheckpointCorruptError(
            "bad-wrapper", "missing RLCK1 magic prefix (not a ledger checkpoint)"
        )
    lines = raw.split(b"\n", 2)
    # split 结果应为 [b'RLCK1', digest, body]；body 自身允许含 '\n'。
    if len(lines) != 3 or len(lines[1]) != _DIGEST_LEN:
        raise CheckpointCorruptError(
            "bad-wrapper", "malformed envelope (digest line missing or truncated)"
        )
    digest_hex, body = lines[1], lines[2]
    try:
        actual = hashlib.sha256(body).hexdigest().encode("ascii")
    except Exception as exc:  # pragma: no cover - sha256 不会失败，防御性兜底
        raise CheckpointCorruptError("bad-checksum", str(exc)) from None
    if not _const_time_eq(actual, digest_hex):
        raise CheckpointCorruptError(
            "bad-checksum", "payload checksum mismatch (corrupt checkpoint)"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointCorruptError(
            "bad-json", f"payload is not valid UTF-8 JSON: {exc}"
        ) from None
    if not isinstance(payload, dict):
        raise CheckpointCorruptError(
            "bad-structure", "payload top-level value must be a JSON object"
        )
    return payload


def _const_time_eq(a: bytes, b: bytes) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0


def fingerprint(parts: dict) -> str:
    """对参与一致性核对的各部分计算确定性 SHA-256 指纹（hex）。"""
    return hashlib.sha256(_canonical_dumps(parts)).hexdigest()
