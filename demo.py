#!/usr/bin/env python3
"""固定输入演示：真实运行一个有界 UTXO 重组账本。

内容
----
1. 正常结果：构造主链 G→A 与更高累计 work 的侧枝 G→B→C，提交侧枝时发生
   **真实重组**，打印新 tip、撤销/激活路径、重组前后的 UTXO 差异。
2. 异常边界：在重组后的链上提交一个双花区块（花费已被侧枝花掉的输出），
   引擎实际抛出 ``BlockValidationError``（证明非法），并展示账本零部分变更；
   另外演示“预算耗尽”（100 块界，这里用小块界展示语义）与“非法输入”
   （bool 被当作整数）两个边界的实际异常类型。
3. 最终确认检查点：以 ``finalize_depth=2`` 建链，展示锚点前移与撤销日志裁剪；
   深重组缺历史时 ``submit`` 返回 ``requires_history``（区别于未知父块的
   ``OrphanBlockError``）；保存检查点字节流、在新账本恢复、继续接块，UTXO
   与“从未中断”的账本一致；最后真实损坏一个字节，恢复被
   ``CheckpointCorruptError`` 明确拒绝。

所有数据在本文件内确定性构造，无网络、无 sleep、无预录输出。
"""

from __future__ import annotations

import time

from reorg_ledger import (
    Block,
    BlockValidationError,
    ChainLimitError,
    CheckpointCorruptError,
    InputContractError,
    OrphanBlockError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def transfer(tx_id: str, prev: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (TxInput(prev, 0),), (TxOutput(amount, lock),))


def demo_finality_checkpoint() -> None:
    print("=== 3. 最终确认检查点：裁剪、requires_history、保存/恢复、继续接块 ===")

    # 用一个手算可核对的线性链：finalize_depth=2。
    # 每块 coinbase 铸 50；a1 花创世 50→45（费5），之后逐块花上一块输出。
    k = 2
    ledger = ReorgLedger(subsidy=50, max_blocks=100, finalize_depth=k)
    chain = [Block("G", None, 1, (coinbase("g-cb", 50, lock=1),))]
    prev_tx, amount = "g-cb", 50
    for i in range(1, 6):
        out_amount = amount - 5
        bid = f"p{i}"
        chain.append(Block(
            bid, chain[-1].block_id, 1,
            (coinbase(f"cb-{bid}", 50),
             transfer(f"tx-{bid}", prev_tx, out_amount)),
        ))
        prev_tx, amount = f"tx-{bid}", out_amount
    for b in chain:
        ledger.submit(b)

    # 手算边界：tip=p5（高度6），K=2 ⇒ 锚点=路径上倒數第3块 p3（高度4）；
    # 锚点之前的 G,p1,p2 的撤销日志已被裁剪。
    print(f"建链到 {ledger.tip_id}（高度 {ledger.height}，累计 work "
          f"{ledger.cumulative_work}）")
    print(f"  finalize_depth = {k}")
    print(f"  锚点(最终确认)  = {ledger.anchor_id}（高度 {ledger.finalized_height}）")
    print(f"  活跃路径(自锚点) = {list(ledger.active_path)}")
    print(f"  已裁剪脊链      = {[p.block_id for p in ledger.pruned_spine]}")
    print(f"  窗口内仍保留块   = {list(ledger.window_block_ids)}")
    print(f"  创世是否还在窗口 = {'G' in ledger}")

    # --- 边界：窗口内侧枝（fork 在 p4，在锚点 p3 之后）— 浅重组成功 ---
    shallow = Block(
        "s1", "p4", 100,
        (coinbase("cb-s1", 50), transfer("tx-s1", "tx-p4", 20)),
    )
    out = ledger.submit(shallow)
    print(f"窗口内侧枝 s1（fork=p4，work=100）: status={out.status}, "
          f"reorganized={out.reorg is not None and out.reorganized}, tip={ledger.tip_id}")
    # 沿新 tip 推进，让锚点前移，旧主枝部分块被裁剪。
    s2 = Block("s2", "s1", 1, (coinbase("cb-s2", 50),
                                transfer("tx-s2", "tx-s1", 15)))
    ledger.submit(s2)
    s3 = Block("s3", "s2", 1, (coinbase("cb-s3", 50),
                                transfer("tx-s3", "tx-s2", 10)))
    ledger.submit(s3)
    print(f"推进到 {ledger.tip_id} 后：锚点={ledger.anchor_id}, "
          f"已裁剪脊链={[p.block_id for p in ledger.pruned_spine]}")
    tip_utxos_before_save = ledger.snapshot_utxos()

    # --- 失败边界 A：深重组缺历史（父在已裁剪脊链中）→ requires_history ---
    deep = Block("d1", "G", 10_000,
                 (coinbase("cb-d1", 50), transfer("tx-d1", "g-cb", 40)))
    tip_before, path_before = ledger.tip_id, ledger.active_path
    outcome = ledger.submit(deep)
    print(f"深重组块 d1（父=已裁剪的 G，work 极大）:")
    print(f"  status               = {outcome.status}")
    h = outcome.history
    print(f"  缺的祖先/锚点/确认深度 = missing={h.missing_parent_id}, "
          f"anchor={h.anchor_id}@{h.anchor_height}, K={h.finalize_depth}")
    print(f"  零变更核对：tip 不变={ledger.tip_id == tip_before}, "
          f"路径不变={ledger.active_path == path_before}, "
          f"UTXO 不变={ledger.snapshot_utxos() == tip_utxos_before_save}")
    print(f"  深重组块未入树: {'d1' not in ledger}")

    # --- 失败边界 B：完全未知父块 → OrphanBlockError（与缺历史严格区分）---
    try:
        ledger.submit(Block("o1", "ghost", 1, (coinbase("cb-o1", 50),)))
    except OrphanBlockError as exc:
        print(f"未知父块 ghost → OrphanBlockError（parent={exc.parent_id}），"
              f"与 requires_history 区分")

    # --- 保存检查点 → 新账本恢复 → 继续接块 ---
    raw = ledger.checkpoint_bytes()
    print(f"保存检查点：{len(raw)} 字节（RLCK1 信封 + SHA-256 + 规范化 JSON）")
    restored = ReorgLedger.from_checkpoint(raw)
    print(f"恢复：tip={restored.tip_id}, 锚点={restored.anchor_id}, "
          f"高度={restored.height}, 活跃路径={list(restored.active_path)}")
    print(f"  UTXO 与保存时一致 = {restored.snapshot_utxos() == tip_utxos_before_save}")

    # 手算核对：继续接两块，用独立重放验证最终 UTXO。
    n1 = Block("n1", "s3", 1, (coinbase("cb-n1", 50),
                                transfer("tx-n1", "tx-s3", 5)))
    n2 = Block("n2", "n1", 1, (coinbase("cb-n2", 50),))
    restored.submit(n1)
    restored.submit(n2)
    print(f"恢复后继续接块 n1,n2 → tip={restored.tip_id}, "
          f"锚点={restored.anchor_id}, 活跃路径={list(restored.active_path)}")

    # 手算核对（不调用被测引擎生成期望值）。最终活跃链：
    #   G,p1,p2,p3,p4,s1,s2,s3,n1,n2（p5 在重组中被撤销，d1 被拒）。
    # 每块一个 coinbase：创世 g-cb 已被 tx-p1 花掉；被撤销的 p5 的 cb-p5
    # 不存在；其余 9 个 coinbase 输出都应在 tip UTXO 中。
    expected_cbs = ["cb-p1:0", "cb-p2:0", "cb-p3:0", "cb-p4:0",
                    "cb-s1:0", "cb-s2:0", "cb-s3:0", "cb-n1:0", "cb-n2:0"]
    # 转账输出链：每枚都被下一块花掉，只有最后一枚 tx-n1:0 仍在。
    transfer_chain = ["tx-p1:0", "tx-p2:0", "tx-p3:0", "tx-p4:0",
                      "tx-s1:0", "tx-s2:0", "tx-s3:0"]
    snap = restored.snapshot_utxos()
    existing = {str(op) for op in snap}
    cbs_ok = all(name in existing for name in expected_cbs)
    cbs_excluded_ok = "g-cb:0" not in existing and "cb-p5:0" not in existing
    transfers_alive = [n for n in transfer_chain if n in existing]
    # 金额：50 经 p1..p4 各 -5（→30），s1 -10（→20），s2/s3/n1 各 -5（→5）。
    last = snap[OutPoint("tx-n1", 0)]
    print(f"  手算：9 个现存 coinbase 输出全在   = {cbs_ok}")
    print(f"  手算：g-cb(已花)/cb-p5(被撤销)不在 = {cbs_excluded_ok}")
    print(f"  手算：转账链仅剩最后一枚 tx-n1:0  = {transfers_alive == []}，"
          f"tx-n1:0 在={'tx-n1:0' in existing}")
    print(f"  手算：tx-n1:0 金额 = {last.amount}（50-5*4-10-5-5-5 = "
          f"{50 - 5*4 - 10 - 5 - 5 - 5}），锁定高度={last.lock_height}")
    assert last.amount == 5 and last.lock_height == 0
    assert cbs_ok and cbs_excluded_ok
    assert transfers_alive == [] and "tx-n1:0" in existing

    # --- 实际错误：翻转一个字节 → 校验和失败，恢复被明确拒绝 ---
    damaged = bytearray(raw)
    damaged[len(damaged) // 2] ^= 0x01
    try:
        ReorgLedger.from_checkpoint(bytes(damaged))
    except CheckpointCorruptError as exc:
        print(f"损坏检查点（翻转 1 字节）→ CheckpointCorruptError(kind={exc.kind!r})")
    else:  # pragma: no cover - 演示路径必须实际触发
        raise SystemExit("expected checkpoint corruption rejection did not happen")


def main() -> None:
    started = time.perf_counter()

    print("=== 1. 正常结果：侧枝累计 work 反超，触发真实重组 ===")
    ledger = ReorgLedger(subsidy=50, max_blocks=100)

    # 创世：50 枚补贴，锁定到高度 1（高度 1 即可花费，边界锁定）。
    genesis = Block("G", None, 10, (coinbase("g-cb", 50, lock=1),))
    ledger.submit(genesis)

    # 主链 A：花掉创世币 50 → 输出 40 + 手续费 10，coinbase 铸 60。
    block_a = Block(
        "A", "G", 2,
        (coinbase("a-cb", 60), transfer("a-tx", "g-cb", 40)),
    )
    ledger.submit(block_a)
    print(f"提交 A 后 tip={ledger.tip_id}, 活跃链={list(ledger.active_path)}, "
          f"累计 work={ledger.cumulative_work}")
    utxos_before = ledger.snapshot_utxos()

    # 侧枝 B（G 的另一子块）：work=20，以祖先状态 G 独立验证：花同一枚创世币。
    block_b = Block(
        "B", "G", 20,
        (coinbase("b-cb", 50), transfer("b-tx", "g-cb", 45)),
    )
    result_b = ledger.submit(block_b)
    print(f"提交 B：became_tip={result_b.became_tip}, "
          f"reorganized={result_b.reorganized}")
    assert result_b.reorg is not None
    print(f"  分叉点      : {result_b.reorg.fork_id}")
    print(f"  撤销(旧→新) : {list(result_b.reorg.deactivated)}")
    print(f"  激活(分叉→尖): {list(result_b.reorg.activated)}")

    # 侧枝继续延伸（同链推进，不再产生撤销）。
    block_c = Block(
        "C", "B", 20,
        (coinbase("c-cb", 50), transfer("c-tx", "b-tx", 45)),
    )
    result_c = ledger.submit(block_c)
    print(f"提交 C：tip={ledger.tip_id}, reorganized={result_c.reorganized}, "
          f"活跃链={list(ledger.active_path)}, 累计 work={ledger.cumulative_work}")

    utxos_after = ledger.snapshot_utxos()
    removed = sorted(map(str, utxos_before.keys() - utxos_after.keys()))
    created = sorted(map(str, utxos_after.keys() - utxos_before.keys()))
    print(f"重组撤销的 UTXO: {removed}")
    print(f"重组新增的 UTXO: {created}")
    print(f"当前 UTXO 数量 : {len(utxos_after)}")
    print(f"a-tx:0（旧链独有）是否已消失: "
          f"{OutPoint('a-tx', 0) not in utxos_after}")
    print(f"c-tx:0（新链独有）是否存在  : "
          f"{OutPoint('c-tx', 0) in utxos_after}")

    print()
    print("=== 2. 实际触发的拒绝/异常边界 ===")

    # 边界 A：双花。a-tx 的币属于旧链，在当前链上不存在；即便直接构造一个
    # 花费 g-cb:0 的区块，该输出在新链状态中也早已被 b-tx 花掉。
    double_spend = Block(
        "D", "C", 1,
        (coinbase("d-cb", 50), transfer("d-tx", "g-cb", 50)),
    )
    tip_before = ledger.tip_id
    path_before = ledger.active_path
    try:
        ledger.submit(double_spend)
    except BlockValidationError as exc:
        print(f"双花区块被拒绝（BlockValidationError）:")
        print(f"  reason   = {exc.reason}")
        print(f"  定位     = block={exc.block_id}, tx={exc.tx_id}, "
              f"height={exc.height}, outpoint={exc.outpoint}")
        print(f"  说明     = {exc.message}")
    else:  # pragma: no cover - 演示路径必须实际触发
        raise SystemExit("expected double-spend rejection did not happen")
    print(f"拒绝后 tip 未变化: {ledger.tip_id == tip_before} "
          f"({ledger.tip_id}), 活跃链未变化: "
          f"{ledger.active_path == path_before}")
    print(f"被拒绝块未入树: {'D' not in ledger}")

    # 边界 B：有界预算耗尽（与“非法”严格区分的另一类异常）。
    small = ReorgLedger(subsidy=50, max_blocks=2)
    small.submit(Block("g0", None, 1, (coinbase("g0-cb", 50, lock=1),)))
    small.submit(Block("g1", "g0", 1, (coinbase("g1-cb", 50),)))
    try:
        small.submit(Block("g2", "g1", 1, (coinbase("g2-cb", 50),)))
    except ChainLimitError as exc:
        print(f"超长链被拒绝（ChainLimitError，预算耗尽≠非法）: {exc}")

    # 边界 C：非法输入契约——bool 不得作为整数，浮点 NaN 单独报错。
    try:
        TxOutput(True, 0)  # type: ignore[arg-type]
    except InputContractError as exc:
        print(f"契约错误（bool 拒绝当整数）: {exc}")

    print()
    demo_finality_checkpoint()

    elapsed = time.perf_counter() - started
    print()
    print(f"演示完成，实际耗时 {elapsed:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
