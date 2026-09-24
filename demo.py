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
3. 最终确认检查点：配置显式最终确认深度后接块，观察锚点推进与撤销日志
   （块体交易）裁剪；把检查点落盘并在**新账本对象**上恢复，恢复后继续接块，
   UTXO 与独立全链重放一致；实际触发深度重组的 ``requires_history``、
   裁剪块体访问 ``PrunedBlockError`` 与损坏检查点 ``CheckpointFormatError``。

所有数据在本文件内确定性构造，无网络、无 sleep、无预录输出。
"""

from __future__ import annotations

import os
import tempfile
import time

from reorg_ledger import (
    Block,
    BlockValidationError,
    ChainLimitError,
    CheckpointFormatError,
    InputContractError,
    OutPoint,
    PrunedBlockError,
    ReorgLedger,
    RequiresHistoryError,
    Transaction,
    TxInput,
    TxOutput,
)


def independent_replay(blocks: list[Block], subsidy: int = 50) -> dict[str, tuple[int, int]]:
    """演示专用的独立全链重放（不调用被测引擎），返回 tip UTXO 供核对。"""
    utxos: dict[str, tuple[int, int]] = {}
    spent_tx: set[str] = set()
    for height, blk in enumerate(blocks, start=1):
        fees = 0
        coinbase_out = 0
        have_cb = False
        new = dict(utxos)
        for pos, tx in enumerate(blk.transactions):
            if not tx.inputs:
                assert pos == 0 and not have_cb
                have_cb = True
                coinbase_out = sum(o.amount for o in tx.outputs)
                for i, o in enumerate(tx.outputs):
                    new[f"{tx.tx_id}:{i}"] = (o.amount, o.lock_height)
                spent_tx.add(tx.tx_id)
                continue
            assert tx.tx_id not in spent_tx
            ins = 0
            for tin in tx.inputs:
                key = f"{tin.prev_tx}:{tin.prev_index}"
                amount, lock = new.pop(key)
                assert lock <= height
                ins += amount
            outs = sum(o.amount for o in tx.outputs)
            assert outs <= ins
            fees += ins - outs
            for i, o in enumerate(tx.outputs):
                new[f"{tx.tx_id}:{i}"] = (o.amount, o.lock_height)
            spent_tx.add(tx.tx_id)
        assert coinbase_out <= subsidy + fees
        utxos = new
    return utxos


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def transfer(tx_id: str, prev: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (TxInput(prev, 0),), (TxOutput(amount, lock),))


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

    elapsed = time.perf_counter() - started
    print()

    print("=== 3. 最终确认检查点：裁剪撤销日志、落盘恢复、深度重组缺历史 ===")
    # 独立的第二条链，显式最终确认深度 D=1。
    cp = ReorgLedger(subsidy=50, max_blocks=100, finalization_depth=1)

    g2 = Block("G2", None, 1, (coinbase("g2-cb", 50, lock=1),))
    p1 = Block("P1", "G2", 1,
               (coinbase("p1-cb", 50), transfer("t1", "g2-cb", 49, lock=2)))
    p2 = Block("P2", "P1", 1,
               (coinbase("p2-cb", 50), transfer("t2", "t1", 48, lock=3)))
    p3 = Block("P3", "P2", 1,
               (coinbase("p3-cb", 50), transfer("t3", "t2", 47, lock=4)))
    for blk in (g2, p1, p2, p3):
        cp.submit(blk)
    print(f"接收到 P3（高度 4）：tip={cp.tip_id}, "
          f"锚点={cp.anchor_id}(高度 {cp.anchor_height})，最终确认深度=1")
    print(f"  已裁剪块体: G2={cp.is_pruned('G2')}, P1={cp.is_pruned('P1')}, "
          f"P2={cp.is_pruned('P2')}；窗口内 P3 保留块体="
          f"{not cp.is_pruned('P3')}")
    try:
        cp.get_block("G2")
    except PrunedBlockError as exc:
        print(f"  取已裁块体被拒（PrunedBlockError）: {exc}")

    # 落盘 → 新对象恢复（模拟进程重启）。
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "checkpoint.json")
        cp.save_checkpoint(path)
        size = os.path.getsize(path)
        resumed = ReorgLedger.load_checkpoint(path)
        print(f"检查点已写入并恢复: {size} 字节; 恢复后 tip={resumed.tip_id}, "
              f"锚点={resumed.anchor_id}, 活跃链={list(resumed.active_path)}")

        # 恢复后继续接块：P4 花 t3→t4，锚点自动从 P2 推进到 P3。
        p4 = Block("P4", "P3", 1,
                   (coinbase("p4-cb", 50), transfer("t4", "t3", 46, lock=5)))
        resumed.submit(p4)
        print(f"恢复后接 P4：tip={resumed.tip_id}, 新锚点={resumed.anchor_id}, "
              f"P3 块体已裁剪={resumed.is_pruned('P3')}")

        # 期望 UTXO 由独立参考从创世全链重放给出（不读引擎状态）。
        expected = independent_replay([g2, p1, p2, p3, p4])
        actual = {str(op): (u.amount, u.lock_height)
                  for op, u in resumed.snapshot_utxos().items()}
        print(f"恢复接块后 UTXO 与独立全链重放一致: {actual == expected}")
        print(f"  tip UTXO = {dict(sorted(actual.items()))}")
        print(f"  手算校验：初始 50 + 4 块补贴 200 - 累计手续费 4 = 246；"
              f"实际合计={sum(v[0] for v in actual.values())}")

        # 失败边界 A：深度重组——挂在已裁剪的 G2 上的高 work 侧链，
        # 分叉点被埋 → requires_history（不可判定，非非法、非孤儿）。
        deep = Block("DEEP", "G2", 1000, (coinbase("deep-cb", 50),))
        tip_before = resumed.tip_id
        try:
            resumed.submit(deep)
        except RequiresHistoryError as exc:
            print(f"深度重组缺历史（RequiresHistoryError）: status={exc.status}, "
                  f"block={exc.block_id}, fork={exc.fork_id}, "
                  f"需要高度 {exc.missing_height} 及以下历史, "
                  f"锚点={exc.anchor_id}@{exc.anchor_height}")
        else:  # pragma: no cover - 演示路径必须实际触发
            raise SystemExit("expected requires_history did not happen")
        print(f"  拒绝后 tip 未变: {resumed.tip_id == tip_before}，"
              f"DEEP 未入树: {'DEEP' not in resumed}")
        # 完全未知父仍是 Orphan（与 requires_history 区分）。
        try:
            resumed.submit(Block("ORPH", "unknown-parent", 1,
                                 (coinbase("orph-cb", 50),)))
        except Exception as exc:  # OrphanBlockError
            print(f"  对照：父块完全未知 -> {type(exc).__name__}")

        # 失败边界 B：损坏检查点（翻转一个字节）必须被拒绝。
        raw = open(path, "rb").read()
        tampered = bytearray(raw)
        tampered[len(tampered) // 2] ^= 0x01
        try:
            ReorgLedger.from_checkpoint_bytes(bytes(tampered))
        except CheckpointFormatError as exc:
            print(f"损坏检查点被拒（CheckpointFormatError）: reason={exc.reason}")
        else:  # pragma: no cover - 演示路径必须实际触发
            raise SystemExit("expected corrupt-checkpoint rejection")

    print()
    print(f"演示完成，实际耗时 {time.perf_counter() - started:.3f} 秒"
          f"（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
