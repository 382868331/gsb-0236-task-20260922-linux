#!/usr/bin/env python3
"""固定输入演示：有界 UTXO 链重组的撤销账本。

运行：``python demo.py``（无需参数、无需网络、无第三方依赖）。

演示内容（全部由确定性代码现场计算，不 sleep、不预录结果）：
1. 正常路径：两条竞争链，侧枝先以祖先状态验证、暂不成为 tip；
   随后侧枝累计 work 反超，触发真实重组（撤销旧链再应用新链）。
2. 异常边界：一个 work 极高但 coinbase 超额的区块被拒绝，
   树与活动 tip 保持原样（原子性）；另演示 NaN 数值输入被拒绝。
"""

from __future__ import annotations

import time

from utxoledger import (
    BlockTree,
    CoinbaseExcessError,
    LedgerError,
    ValidationError,
)
from utxoledger.types import Block, Input, OutPoint, Output, Transaction

SUBSIDY = 50


def cb(txid: str, value: int) -> Transaction:
    return Transaction(txid=txid, inputs=(),
                       outputs=(Output(value, 0),), is_coinbase=True)


def spend(txid: str, src: str, outs: tuple[tuple[int, int], ...]) -> Transaction:
    return Transaction(
        txid=txid,
        inputs=(Input(OutPoint(src, 0)),),
        outputs=tuple(Output(v, lh) for v, lh in outs),
    )


def show_utxos(tree: BlockTree, title: str) -> None:
    print(f"  {title}")
    total = 0
    for u in tree.utxos():
        total += u.value
        print(f"    {u.outpoint.txid}:{u.outpoint.output_index} "
              f"value={u.value} lock_height={u.lock_height}")
    print(f"    -- UTXO 合计 = {total}")


def main() -> int:
    started = time.perf_counter()
    tree = BlockTree(subsidy=SUBSIDY)

    # 创世块（高度 0）：coinbase 50。
    genesis = Block("G", None, 1, (cb("cbG", 50),))
    tree.add_block(genesis)
    print(f"[0] add G            -> tip={tree.tip}  chain={tree.active_chain_ids()}")

    # A 链：A1/A2，累计 work = 1 + 10 + 10 = 21。
    a1 = Block("A1", "G", 10, (cb("cbA1", 50), spend("a1", "cbG", ((50, 0),))))
    tree.add_block(a1)
    a2 = Block("A2", "A1", 10, (
        cb("cbA2", 50),
        Transaction("a2", (Input(OutPoint("a1", 0)),),
                    (Output(30, 0), Output(20, 0))),
    ))
    r = tree.add_block(a2)
    print(f"[1] add A1,A2        -> tip={tree.tip}  chain={tree.active_chain_ids()}")
    show_utxos(tree, "A 链 UTXO：")

    # B 侧枝第一块：累计 13 < 21，合法但不成为 tip（祖先状态验证通过即入树）。
    # b1 只输出 40，留下 10 手续费，coinbase 可取 60。
    b1 = Block("B1", "G", 12, (cb("cbB1", 60), spend("b1", "cbG", ((40, 0),))))
    r1 = tree.add_block(b1)
    print(f"[2] add B1(侧枝)     -> tip={tree.tip}（B1 累计 work=13 < 21，"
          f"is_reorg={r1.is_reorg}）")

    # B2 让 B 链累计 work = 25 > 21：真实重组。
    b2 = Block("B2", "B1", 12, (cb("cbB2", 50), spend("b2", "b1", ((40, 0),))))
    r2 = tree.add_block(b2)
    print(f"[3] add B2           -> tip={tree.tip}（B 链累计 work=25 > 21）")
    print(f"    重组路径: 撤销旧链 {list(r2.deactivated)} "
          f"-> 应用新链 {list(r2.activated)}")
    show_utxos(tree, "重组后 B 链 UTXO（a* 已随撤销消失，b* 为重放结果）：")

    # 独立硬期望（不调用核心得到，直接按固定输入人工核算）：
    expected = {("cbB1", 0): 60, ("cbB2", 0): 50, ("b2", 0): 40}
    actual = {(u.outpoint.txid, u.outpoint.output_index): u.value
              for u in tree.utxos()}
    assert actual == expected, f"UTXO 与人工核算不符：{actual} != {expected}"
    assert tree.active_chain_ids() == ("G", "B1", "B2")
    print("    [校验] 与固定输入的人工核算完全一致 ✓")

    # ------------------------------------------------------------------
    # 异常边界 1：work 极高（1000）但 coinbase 超额的区块必须被拒绝，
    # 且拒绝是原子的——块不入树，tip 不变。
    # ------------------------------------------------------------------
    nodes_before = len(tree)
    tip_before = tree.tip
    evil = Block("EVIL", "B2", 1000, (cb("cbEvil", 1_000),))
    print("\n[4] 提交高 work 但 coinbase 超额的区块 EVIL(cb=1000, 上限=50) ...")
    try:
        tree.add_block(evil)
    except CoinbaseExcessError as exc:
        print(f"    实际拒绝: {type(exc).__name__}: {exc.message}")
        print(f"    claimed={exc.claimed} cap={exc.cap}")
    assert len(tree) == nodes_before and tree.tip == tip_before
    assert "EVIL" not in tree
    print(f"    原子性: tip 仍为 {tree.tip}，树节点数仍为 {nodes_before} ✓")

    # ------------------------------------------------------------------
    # 异常边界 2：数值输入 NaN 被显式拒绝（不静默纠正）。
    # ------------------------------------------------------------------
    try:
        tree.add_block(Block("NAN", "B2", float("nan"), ()))
    except ValidationError as exc:
        print(f"\n[5] NaN work 输入: 实际拒绝 code={exc.code} "
              f"location={exc.location} ✓")

    # 汇总异常类型均可被公共基类捕获。
    assert issubclass(CoinbaseExcessError, LedgerError)

    elapsed = time.perf_counter() - started
    print(f"\n完成：演示为现场真实计算，耗时 {elapsed:.3f}s（上限约 8s）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
