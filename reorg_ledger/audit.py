"""独立资产审计：从交易历史与当前 UTXO 集重算各资产账目。

本模块**不被引擎使用**，是一个独立的对照实现：只把公开数据类当纯数据读取，
自行重放活跃链交易历史，逐资产累计“已发行 / 已销毁 / 在外数量”，并与调用方
提供的当前 UTXO 集合核对守恒（已发行 − 已销毁 == 在外数量）。用于测试与演示
中交叉验证引擎的多资产语义，不构成账本的权威状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LedgerStateError
from .models import Block, OutPoint, UTXO

__all__ = ["AssetTotals", "audit_assets"]


@dataclass(frozen=True, slots=True)
class AssetTotals:
    """单一资产的独立重算结果。"""

    asset_id: str
    issued: int
    burned: int
    outstanding: int


def audit_assets(
    blocks: list[Block] | tuple[Block, ...],
    utxos: dict[OutPoint, UTXO],
) -> dict[str, AssetTotals]:
    """重放 blocks（创世→tip 顺序）并核对 utxos 中的在外数量。

    返回 {asset_id: AssetTotals}。若守恒不成立（历史重算的在外的数量与给定
    UTXO 集不符，或发行/销毁账目不平），抛 LedgerStateError。
    """
    # 1) 独立重放交易历史，记录每个输出点的资产/授权内容。
    history: dict[OutPoint, UTXO] = {}
    issued: dict[str, int] = {}
    burned: dict[str, int] = {}

    for block in blocks:
        for tx in block.transactions:
            in_assets: dict[str, int] = {}
            for txin in tx.inputs:
                op = OutPoint(txin.prev_tx, txin.prev_index)
                prev = history.get(op)
                if prev is None:
                    raise LedgerStateError(
                        f"audit: tx {tx.tx_id!r} references unknown output "
                        f"{op} (history does not match the given blocks)"
                    )
                for aid, amount in prev.assets:
                    in_assets[aid] = in_assets.get(aid, 0) + amount
                del history[op]
            out_assets: dict[str, int] = {}
            for index, out in enumerate(tx.outputs):
                op = OutPoint(tx.tx_id, index)
                history[op] = UTXO(
                    out.amount, out.lock_height, out.assets,
                    out.issue_asset, out.issue_allowance,
                )
                for aid, amount in out.assets:
                    out_assets[aid] = out_assets.get(aid, 0) + amount
            burn_assets: dict[str, int] = {}
            for aid, amount in tx.burns:
                burn_assets[aid] = burn_assets.get(aid, 0) + amount
                burned[aid] = burned.get(aid, 0) + amount
            for aid in set(in_assets) | set(out_assets) | set(burn_assets):
                minted = (
                    out_assets.get(aid, 0)
                    + burn_assets.get(aid, 0)
                    - in_assets.get(aid, 0)
                )
                if minted > 0:
                    issued[aid] = issued.get(aid, 0) + minted

    # 2) 从给定 UTXO 集统计在外数量。
    outstanding: dict[str, int] = {}
    for utxo in utxos.values():
        for aid, amount in utxo.assets:
            outstanding[aid] = outstanding.get(aid, 0) + amount

    # 3) 核对守恒：已发行 − 已销毁 == 在外数量（逐资产）。
    totals: dict[str, AssetTotals] = {}
    for aid in sorted(set(issued) | set(burned) | set(outstanding)):
        entry = AssetTotals(
            asset_id=aid,
            issued=issued.get(aid, 0),
            burned=burned.get(aid, 0),
            outstanding=outstanding.get(aid, 0),
        )
        if entry.issued - entry.burned != entry.outstanding:
            raise LedgerStateError(
                f"audit: asset {aid!r} conservation violated: issued "
                f"{entry.issued} - burned {entry.burned} != outstanding "
                f"{entry.outstanding}"
            )
        totals[aid] = entry
    return totals

