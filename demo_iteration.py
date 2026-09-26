#!/usr/bin/env python3
"""多资产 + 发行授权迭代演示（真实计算，无 sleep、无预录输出）。

内容
----
1. 正常结果：创设发行授权 → 同块顺序发行再转账 → 显式销毁 → 额度耗尽；
   随后用高 work 侧枝触发真实重组，展示数量与授权随撤销机制恢复；全程用
   独立的 audit_assets 从交易历史与当前 UTXO 重算各资产已发行/已销毁/在外
   数量并核对守恒。
2. 实际触发的边界：无授权铸造（mint-without-authority）、未声明销毁
   （asset-burn-undeclared）、跨资产补短、额度结算不平（allowance-mismatch）。
"""

from __future__ import annotations

import time

from reorg_ledger import (
    Block,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
    audit_assets,
)


def coinbase(tx_id, amount, lock=0):
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def show_audit(ledger, label):
    chain = [ledger.get_block(b) for b in ledger.active_path]
    totals = audit_assets(chain, ledger.snapshot_utxos())
    for aid, t in sorted(totals.items()):
        print(f"  [{label}] 资产 {aid}: 已发行={t.issued} "
              f"已销毁={t.burned} 在外={t.outstanding} (守恒成立)")
    return totals


def main():
    started = time.perf_counter()
    ledger = ReorgLedger(subsidy=50, max_blocks=100)

    print("=== 1. 正常结果：发行授权 + 多资产转账 + 显式销毁 + 重组恢复 ===")
    ledger.submit(Block("G", None, 10, (coinbase("gcb", 50, lock=1),)))

    # 块 M：创设授权（资产 ID 由创设交易 ID + 输出位置确定为 "m1:1"），
    # 同块顺序发行 30（留后继 70）并把 12 转给另一输出。
    asset = "m1:1"
    create = Transaction(
        "m1",
        (TxInput("gcb", 0),),
        (
            TxOutput(30, 0),
            TxOutput(20, 0, issue_asset=asset, issue_allowance=100),
        ),
    )
    issue = Transaction(
        "m2",
        (TxInput("m1", 1),),
        (
            TxOutput(5, 0, assets={asset: 30}),
            TxOutput(10, 0, issue_asset=asset, issue_allowance=70),
        ),
    )
    transfer = Transaction(
        "m3",
        (TxInput("m2", 0),),
        (
            TxOutput(2, 0, assets={asset: 12}),
            TxOutput(2, 0, assets={asset: 18}),
        ),
    )
    ledger.submit(Block("M", "G", 5, (coinbase("mcb", 50), create, issue, transfer)))
    print(f"提交 M：tip={ledger.tip_id}，资产 {asset} 已发行 30 并完成同块转账")

    # 块 N：显式销毁 8；再发行 70，额度恰好耗尽（无后继授权）。
    burn = Transaction(
        "n1",
        (TxInput("m3", 0),),
        (TxOutput(1, 0, assets={asset: 4}),),
        burns={asset: 8},
    )
    drain = Transaction(
        "n2",
        (TxInput("m2", 1),),
        (TxOutput(5, 0, assets={asset: 70}),),
    )
    ledger.submit(Block("N", "M", 5, (coinbase("ncb", 50), burn, drain)))
    utxos = ledger.snapshot_utxos()
    print(f"提交 N：授权已耗尽消失: {OutPoint('m2', 1) not in utxos}")
    show_audit(ledger, "主链 N")

    # 侧枝：从 M 分叉，竞争同一授权（发行 50 留 20），work 反超触发重组。
    fork_issue = Transaction(
        "s1",
        (TxInput("m2", 1),),
        (
            TxOutput(5, 0, assets={asset: 50}),
            TxOutput(5, 0, issue_asset=asset, issue_allowance=20),
        ),
    )
    result = ledger.submit(Block("S", "M", 20, (coinbase("scb", 50), fork_issue)))
    print(f"提交侧枝 S：reorganized={result.reorganized}, "
          f"撤销={list(result.reorg.deactivated)}, "
          f"激活={list(result.reorg.activated)}")
    utxos = ledger.snapshot_utxos()
    print(f"  旧链铸币 n2:0 已撤销: {OutPoint('n2', 0) not in utxos}; "
          f"新授权 s1:1 剩余额度: "
          f"{utxos[OutPoint('s1', 1)].issue_allowance}")
    show_audit(ledger, "重组后 S")

    print()
    print("=== 2. 实际触发的拒绝边界（账本零变更） ===")

    def attempt(label, tx):
        tip_before, path_before = ledger.tip_id, ledger.active_path
        try:
            ledger.submit(
                Block(f"X-{label}", "S", 1, (coinbase(f"xcb-{label}", 50), tx))
            )
        except BlockValidationError as exc:
            print(f"  {label}: 拒绝 reason={exc.reason} "
                  f"(tx={exc.tx_id}); tip 未变: {ledger.tip_id == tip_before}, "
                  f"活跃链未变: {ledger.active_path == path_before}")
        else:
            raise SystemExit(f"expected rejection did not happen: {label}")

    # 无授权铸造：普通转账凭空多出资产。
    attempt("mint-without-authority", Transaction(
        "x1", (TxInput("s1", 0),),
        (TxOutput(2, 0, assets={asset: 60}),),
    ))
    # 未声明销毁：输入 50 资产只输出 40，差额未声明。
    attempt("asset-burn-undeclared", Transaction(
        "x2", (TxInput("s1", 0),),
        (TxOutput(2, 0, assets={asset: 40}),),
    ))
    # 跨资产补短：用不存在的另一种资产充数同样被拒。
    attempt("cross-asset-coverup", Transaction(
        "x3", (TxInput("s1", 0),),
        (TxOutput(2, 0, assets={asset: 50, "ghost:0": 9}),),
    ))
    # 额度结算不平：铸 10 + 后继 5 ≠ 剩余 20。
    attempt("allowance-mismatch", Transaction(
        "x4", (TxInput("s1", 1),),
        (
            TxOutput(2, 0, assets={asset: 10}),
            TxOutput(2, 0, issue_asset=asset, issue_allowance=5),
        ),
    ))

    elapsed = time.perf_counter() - started
    print()
    print(f"演示完成，实际耗时 {elapsed:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
