"""最终确认检查点 / 撤销日志裁剪 / 恢复 / 深度重组缺历史 / 损坏检查点测试。

期望值来源（不调用被测实现生成）：

* UTXO 期望一律由 :mod:`tests.reference` 的独立全链重放给出；测试自己保留
  原始 ``Block`` 对象，即使引擎已裁剪块体也能独立重放整条链作预言机。
* 检查点边界（锚点高度、被裁块集合、分叉点）用提交序列的高度关系手算。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from reorg_ledger import (
    Block,
    BlockValidationError,
    ChainLimitError,
    CheckpointError,
    CheckpointFormatError,
    InputContractError,
    OrphanBlockError,
    OutPoint,
    PrunedBlockError,
    ReorgLedger,
    RequiresHistoryError,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import replay


def cb(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def sp(tx_id: str, prev: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(
        tx_id, (TxInput(prev, 0),), (TxOutput(amount, lock),)
    )


def blk(block_id: str, parent: str | None, work: int,
        txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


def linear_chain(n: int, *, work: int = 1, genesis_lock: int = 1,
                 spend_each: bool = True) -> list[Block]:
    """确定性线性链：创世 + n-1 块；每块花掉上一块转移输出并铸满补贴+费。

    返回的块列表同时供引擎提交与独立参考全链重放使用。
    """
    blocks = [blk("b1", None, work, (cb("cb1", 50, genesis_lock),))]
    last_out = "cb1"
    for h in range(2, n + 1):
        txs = [cb(f"cb{h}", 50)]
        if spend_each:
            # 输入 50（上一块的转移输出）→ 输出 49，手续费 1；
            # coinbase 50 <= 50+1。新输出锁到当前高度（本块即可继续花）。
            txs.append(sp(f"tx{h}", last_out, 49, lock=h))
            last_out = f"tx{h}"
        blocks.append(blk(f"b{h}", f"b{h-1}", work, tuple(txs)))
    return blocks


def submit_all(ledger: ReorgLedger, blocks: list[Block]) -> None:
    for b in blocks:
        ledger.submit(b)


def reference_utxos(blocks: list[Block]):
    return {op: pair for op, pair in replay(blocks).items()}


def engine_utxos(ledger: ReorgLedger):
    return {
        op: (u.amount, u.lock_height)
        for op, u in ledger.snapshot_utxos().items()
    }


class FinalizationBoundaryTests(unittest.TestCase):
    def test_anchor_appears_exactly_beyond_confirmation_depth(self) -> None:
        # D=2：高度 h 的块在 tip 高度 >= h+D 时进入“深度之外”，锚点=tip_h-D。
        ledger = ReorgLedger(finalization_depth=2)
        chain = linear_chain(5)

        ledger.submit(chain[0])
        self.assertIsNone(ledger.anchor_id)  # tip h1 - 2 < 1
        ledger.submit(chain[1])
        self.assertIsNone(ledger.anchor_id)  # tip h2 - 2 = 0
        ledger.submit(chain[2])
        self.assertEqual(ledger.anchor_id, "b1")   # tip h3 - 2 = 1
        self.assertEqual(ledger.anchor_height, 1)
        self.assertTrue(ledger.is_pruned("b1"))
        self.assertFalse(ledger.is_pruned("b2"))
        ledger.submit(chain[3])
        self.assertEqual(ledger.anchor_id, "b2")   # 推进，不回退
        self.assertTrue(ledger.is_pruned("b1"))
        self.assertTrue(ledger.is_pruned("b2"))
        self.assertFalse(ledger.is_pruned("b3"))
        ledger.submit(chain[4])
        self.assertEqual(ledger.anchor_id, "b3")
        # 裁剪边界：高度 <= 锚点的块体全部裁剪（含同高度的被埋侧枝，见下例）。
        pruned = {bid for bid in ledger.active_path if ledger.is_pruned(bid)}
        self.assertEqual(pruned, {"b1", "b2", "b3"})

    def test_buried_side_branch_keeps_header_but_prunes_body(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(5)
        submit_all(ledger, chain[:2])          # tip b2
        side = blk("s1", "b1", 1, (cb("cs1", 50),))
        ledger.submit(side)                     # 低 work 侧枝，未成为 tip
        self.assertIn("s1", ledger)
        self.assertEqual(ledger.get_block("s1").block_id, "s1")
        # 主链继续：tip b3 时锚点 b2（h2）；s1 同为高度 2，块体一并裁剪，
        # 但块头仍保留在树中。
        ledger.submit(chain[2])                 # tip b3，锚点 b2
        self.assertTrue(ledger.is_pruned("s1"))
        self.assertIn("s1", ledger)
        ledger.submit(chain[3])                 # tip b4，锚点 b3
        self.assertTrue(ledger.is_pruned("s1"))
        self.assertIn("s1", ledger)
        with self.assertRaises(PrunedBlockError):
            ledger.get_block("s1")
        # 在被埋侧枝上接续：分叉点 s1 位于锚点之下 → requires_history。
        with self.assertRaises(RequiresHistoryError) as ctx:
            ledger.submit(blk("s2", "s1", 1000, (cb("cs2", 50),)))
        self.assertEqual(ctx.exception.block_id, "s2")
        self.assertEqual(ctx.exception.fork_id, "b1")

    def test_pruned_body_get_block_raises_but_header_queries_work(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(3))
        self.assertEqual(ledger.anchor_id, "b2")
        self.assertIn("b1", ledger)
        self.assertTrue(ledger.contains("b1"))
        self.assertEqual(ledger.active_path[:2], ("b1", "b2"))
        with self.assertRaises(PrunedBlockError) as ctx:
            ledger.get_block("b1")
        self.assertEqual(ctx.exception.block_id, "b1")
        # 未裁剪块照常取体。
        self.assertEqual(ledger.get_block("b3").block_id, "b3")
        # 未知块仍是 KeyError，不与裁剪混淆。
        with self.assertRaises(KeyError):
            ledger.get_block("nope")

    def test_explicit_finalize_and_depth_zero(self) -> None:
        ledger = ReorgLedger()  # 不配置自动最终化
        chain = linear_chain(3)
        submit_all(ledger, chain)
        self.assertIsNone(ledger.anchor_id)
        anchor = ledger.finalize(1)
        self.assertEqual(anchor, "b2")
        self.assertTrue(ledger.is_pruned("b1"))
        # 重复/更深的空操作语义：同深度空操作。
        self.assertEqual(ledger.finalize(1), "b2")
        # depth=0：连 tip 自身也最终化裁剪。
        self.assertEqual(ledger.finalize(0), "b3")
        self.assertTrue(ledger.is_pruned("b3"))
        with self.assertRaises(PrunedBlockError):
            ledger.tip  # type: ignore[func-returns-value]
        # 空账本不能最终化。
        with self.assertRaises(CheckpointError):
            ReorgLedger().finalize(0)
        # 非法深度契约。
        for bad in (-1, False, 1.0, "1"):
            with self.assertRaises(InputContractError):
                ReorgLedger().finalize(bad)  # type: ignore[arg-type]

    def test_constructor_finalization_depth_contract(self) -> None:
        ReorgLedger(finalization_depth=0)
        ReorgLedger(max_blocks=3, finalization_depth=2)
        for bad in (-1, False, 1.0):
            with self.assertRaises(InputContractError):
                ReorgLedger(finalization_depth=bad)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            ReorgLedger(max_blocks=3, finalization_depth=3)
        with self.assertRaises(InputContractError):
            ReorgLedger(max_blocks=3, finalization_depth=4)

    def test_finalize_depth_must_be_below_max_blocks(self) -> None:
        ledger = ReorgLedger(max_blocks=3)
        submit_all(ledger, linear_chain(3))
        with self.assertRaises(InputContractError):
            ledger.finalize(3)


class InWindowReorgAfterPruneTests(unittest.TestCase):
    def test_reorg_with_fork_at_or_above_anchor_still_works(self) -> None:
        # D=1，链 b1..b4，锚点 b3；在 b3 上以高 work 分叉，分叉点即锚点。
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(5)
        submit_all(ledger, chain[:4])  # tip b4，锚点 b3
        self.assertEqual(ledger.anchor_id, "b3")

        x1 = blk("x1", "b3", 100, (cb("cx1", 50),))
        result = ledger.submit(x1)
        self.assertTrue(result.reorganized)
        assert result.reorg is not None
        self.assertEqual(result.reorg.fork_id, "b3")
        self.assertEqual(result.reorg.deactivated, ("b4",))
        self.assertEqual(result.reorg.activated, ("x1",))
        self.assertEqual(ledger.tip_id, "x1")
        # 新链 tip UTXO 与独立参考对 G..b3,x1 的全链重放一致。
        self.assertEqual(
            engine_utxos(ledger),
            reference_utxos([*chain[:3], x1]),
        )

    def test_reorg_fork_one_above_anchor(self) -> None:
        # 锚点 b2；b3 在窗口内，x 从 b3 分叉（fork=b3 > anchor）。
        ledger = ReorgLedger(finalization_depth=2)
        chain = linear_chain(5)
        submit_all(ledger, chain[:4])  # tip b4，锚点 b2
        x = blk("x", "b3", 100, (cb("cx", 50),))
        result = ledger.submit(x)
        self.assertTrue(result.reorganized)
        assert result.reorg is not None
        self.assertEqual(result.reorg.fork_id, "b3")
        self.assertEqual(ledger.tip_id, "x")
        self.assertEqual(
            engine_utxos(ledger),
            reference_utxos([*chain[:3], x]),
        )

    def test_invalid_block_in_window_still_rejected_atomically(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(4)
        submit_all(ledger, chain)  # tip b4，锚点 b3
        before_path = ledger.active_path
        before_utxos = engine_utxos(ledger)
        # 双花：tx4 的输出在上一区块（窗口内）已被……这里直接花 cb1（锚点前
        # 已被 tx2 花掉），从锚点重放即可判定 missing-input。
        bad = blk("bad", "b4", 1, (cb("cbad", 50), sp("tbad", "cb1", 1)))
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertNotIn("bad", ledger)
        self.assertEqual(ledger.active_path, before_path)
        self.assertEqual(engine_utxos(ledger), before_utxos)

    def test_lock_height_survives_checkpoint_boundary(self) -> None:
        # 锚点状态里的 UTXO 保留 lock_height：构造一个锁定到高度 4 的输出，
        # 在锚点之后、高度不足时仍不可花。
        ledger = ReorgLedger(finalization_depth=1)
        g = blk("g", None, 1, (cb("gcb", 50, 1),))
        a = blk("a", "g", 1, (cb("acb", 50), sp("ta", "gcb", 50, lock=5)))
        b = blk("b", "a", 1, (cb("bcb", 50),))
        ledger.submit(g)
        ledger.submit(a)
        ledger.submit(b)  # tip b(h3)，锚点 a(h2)；ta:0 锁到 5 保存在锚点 UTXO
        self.assertEqual(ledger.anchor_id, "a")
        early = blk("c", "b", 1, (cb("ccb", 50), sp("tc", "ta", 50)))
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(early)  # 高度 4 < lock 5
        self.assertEqual(ctx.exception.reason, "output-locked")
        # 高度 4 空块延伸，高度 5 时 lock==height 可花。
        ledger.submit(blk("d", "b", 1, (cb("dcb", 50),)))
        ok = blk("e", "d", 1, (cb("ecb", 50), sp("te", "ta", 50)))
        ledger.submit(ok)
        self.assertEqual(ledger.tip_id, "e")


class RequiresHistoryTests(unittest.TestCase):
    def _pruned_ledger(self) -> tuple[ReorgLedger, list[Block]]:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(4)
        submit_all(ledger, chain)  # tip b4，锚点 b3
        return ledger, chain

    def test_deep_reorg_below_anchor_returns_requires_history(self) -> None:
        ledger, _ = self._pruned_ledger()
        # 候选挂在 b1（锚点 b3 的祖先）上：分叉点 b1 被埋在裁剪历史中。
        deep = blk("deep", "b1", 1000, (cb("cdeep", 50),))
        with self.assertRaises(RequiresHistoryError) as ctx:
            ledger.submit(deep)
        err = ctx.exception
        self.assertEqual(err.status, "requires_history")
        self.assertEqual(err.block_id, "deep")
        self.assertEqual(err.anchor_id, "b3")
        self.assertEqual(err.anchor_height, 3)
        self.assertEqual(err.fork_id, "b1")
        self.assertEqual(err.missing_height, 2)
        self.assertIn("requires_history", str(err))

    def test_requires_history_is_neither_invalid_nor_orphan(self) -> None:
        ledger, _ = self._pruned_ledger()
        deep = blk("deep", "b1", 1000, (cb("cdeep", 50),))
        # 父块 b1 已知（块头在树中），因此不是 Orphan；也未被证伪。
        self.assertTrue(ledger.contains("b1"))
        with self.assertRaises(RequiresHistoryError):
            ledger.submit(deep)
        self.assertFalse(issubclass(RequiresHistoryError, OrphanBlockError))
        self.assertFalse(issubclass(RequiresHistoryError, BlockValidationError))
        # 完全未知父仍是 Orphan，二者不混淆。
        with self.assertRaises(OrphanBlockError):
            ledger.submit(blk("z", "unknown-parent", 1, (cb("cz", 50),)))

    def test_requires_history_is_atomic(self) -> None:
        ledger, _ = self._pruned_ledger()
        tip_before = ledger.tip_id
        path_before = ledger.active_path
        utxos_before = engine_utxos(ledger)
        with self.assertRaises(RequiresHistoryError):
            ledger.submit(blk("deep", "b2", 1000, (cb("cdeep", 999),)))
        self.assertNotIn("deep", ledger)
        self.assertEqual(ledger.tip_id, tip_before)
        self.assertEqual(ledger.active_path, path_before)
        self.assertEqual(engine_utxos(ledger), utxos_before)
        self.assertEqual(ledger.anchor_id, "b3")

    def test_requires_history_after_restore(self) -> None:
        ledger, _ = self._pruned_ledger()
        data = ledger.to_checkpoint_bytes()
        restored = ReorgLedger.from_checkpoint_bytes(data)
        with self.assertRaises(RequiresHistoryError) as ctx:
            restored.submit(blk("deep2", "b1", 1000, (cb("cd2", 50),)))
        self.assertEqual(ctx.exception.fork_id, "b1")

    def test_history_fork_exactly_at_anchor_is_resolvable(self) -> None:
        # 分叉点 == 锚点：锚点 UTXO 足以验证，不得报 requires_history。
        ledger, _ = self._pruned_ledger()
        at_anchor = blk("fork", "b3", 1000, (cb("cfork", 50),))
        result = ledger.submit(at_anchor)
        self.assertTrue(result.became_tip)


class CheckpointRoundTripTests(unittest.TestCase):
    def test_bytes_round_trip_preserves_state(self) -> None:
        ledger = ReorgLedger(finalization_depth=2)
        chain = linear_chain(6)
        submit_all(ledger, chain)  # tip b6，锚点 b4
        data = ledger.to_checkpoint_bytes()

        restored = ReorgLedger.from_checkpoint_bytes(data)
        self.assertEqual(restored.tip_id, "b6")
        self.assertEqual(restored.anchor_id, "b4")
        self.assertEqual(restored.anchor_height, 4)
        self.assertEqual(restored.subsidy, 50)
        self.assertEqual(restored.max_blocks, 100)
        self.assertEqual(restored.finalization_depth, 2)
        self.assertEqual(restored.active_path, ("b1", "b2", "b3", "b4",
                                                "b5", "b6"))
        self.assertEqual(engine_utxos(restored), engine_utxos(ledger))
        # 块头保留、块体裁剪状态一致。
        self.assertTrue(restored.is_pruned("b1"))
        self.assertTrue(restored.is_pruned("b4"))
        self.assertFalse(restored.is_pruned("b5"))

    def test_continue_blocks_after_restore_matches_full_replay(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(5)
        submit_all(ledger, chain[:4])  # 锚点 b3
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())

        # 恢复后继续接两块（含真实转账），与独立参考的全链（含已裁块）重放对齐。
        more = linear_chain(6)
        restored.submit(more[4])  # b5
        restored.submit(more[5])  # b6
        full_chain = more  # 确定性同构链：b1..b6
        self.assertEqual(restored.tip_id, "b6")
        self.assertEqual(
            engine_utxos(restored),
            reference_utxos(full_chain),
        )

    def test_applied_txid_history_survives_restore(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(3)
        submit_all(ledger, chain)  # 锚点 b2；cb1 是锚点之前的旧 txid
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        # 复用锚点前的 coinbase txid：必须凭检查点里的已用集合拒绝。
        reused = blk("r", "b3", 1, (cb("cb1", 50),))
        with self.assertRaises(BlockValidationError) as ctx:
            restored.submit(reused)
        self.assertEqual(ctx.exception.reason, "duplicate-txid")

    def test_checkpoint_anchor_utxos_match_independent_replay(self) -> None:
        ledger = ReorgLedger(finalization_depth=2)
        chain = linear_chain(5)
        submit_all(ledger, chain)  # tip b5，锚点 b3
        envelope = json.loads(ledger.to_checkpoint_bytes())
        saved = {
            OutPoint(txid, idx): (amount, lock)
            for txid, idx, amount, lock in envelope["data"]["utxos"]
        }
        # 独立参考只重放到锚点高度 b3，必须与检查点保存的 UTXO 完全一致。
        self.assertEqual(saved, dict(replay(chain[:3])))

    def test_save_and_load_file_round_trip(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ckpt.json")
            ledger.save_checkpoint(path)
            self.assertTrue(os.path.exists(path))
            loaded = ReorgLedger.load_checkpoint(path)
            self.assertEqual(loaded.tip_id, ledger.tip_id)
            self.assertEqual(engine_utxos(loaded), engine_utxos(ledger))
            # 保存是原子替换：同路径再保存一次仍然完整可读。
            loaded.submit(linear_chain(5)[4])
            loaded.save_checkpoint(path)
            again = ReorgLedger.load_checkpoint(path)
            self.assertEqual(again.tip_id, "b5")

    def test_save_without_anchor_is_contract_error(self) -> None:
        ledger = ReorgLedger()
        ledger.submit(linear_chain(3)[0])
        with self.assertRaises(CheckpointError):
            ledger.to_checkpoint_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CheckpointError):
                ledger.save_checkpoint(os.path.join(tmp, "x.json"))

    def test_max_blocks_still_enforced_after_restore(self) -> None:
        ledger = ReorgLedger(max_blocks=4, finalization_depth=1)
        submit_all(ledger, linear_chain(4))  # 已到界
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        over = linear_chain(5)[4]
        with self.assertRaises(ChainLimitError) as ctx:
            restored.submit(over)
        self.assertEqual(ctx.exception.limit, 4)

    def test_explicit_finalize_then_resume_and_reorg(self) -> None:
        ledger = ReorgLedger()
        chain = linear_chain(4)
        submit_all(ledger, chain)
        ledger.finalize(1)  # 锚点 b3
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        self.assertIsNone(restored.finalization_depth)  # 未配置自动深度
        fork = blk("f", "b3", 50, (cb("cf", 50),))
        result = restored.submit(fork)
        self.assertTrue(result.reorganized)
        self.assertEqual(
            engine_utxos(restored),
            reference_utxos([*chain[:3], fork]),
        )

    def test_side_branch_above_anchor_survives_restore_and_can_win(self) -> None:
        ledger = ReorgLedger(finalization_depth=2)
        chain = linear_chain(5)
        submit_all(ledger, chain[:4])       # tip b4，锚点 b2
        side = blk("s", "b3", 1, (cb("cs", 50),))  # 窗口内低 work 侧枝
        ledger.submit(side)
        self.assertFalse(ledger.is_pruned("s"))
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        self.assertIn("s", restored)
        # 侧枝延续高 work，反超 b4 触发真实重组，期望与独立全链重放一致。
        s2 = blk("s2", "s", 100, (cb("cs2", 50),))
        result = restored.submit(s2)
        self.assertTrue(result.reorganized)
        self.assertEqual(restored.tip_id, "s2")
        self.assertEqual(
            engine_utxos(restored),
            reference_utxos([*chain[:3], side, s2]),
        )

    def test_restore_after_genesis_only_depth_zero(self) -> None:
        ledger = ReorgLedger(finalization_depth=0)
        g = blk("g", None, 1, (cb("gc", 50, 1),))
        ledger.submit(g)
        self.assertEqual(ledger.anchor_id, "g")
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        self.assertTrue(restored.is_pruned("g"))
        nxt = blk("a", "g", 1, (cb("ac", 50), sp("ta", "gc", 50, lock=2)))
        restored.submit(nxt)
        self.assertEqual(restored.tip_id, "a")
        self.assertEqual(
            engine_utxos(restored), reference_utxos([g, nxt])
        )

    def test_explicit_finalize_is_monotonic(self) -> None:
        ledger = ReorgLedger()
        chain = linear_chain(5)
        submit_all(ledger, chain)          # tip b5
        self.assertEqual(ledger.finalize(2), "b3")
        # 更浅的最终确认（更大深度）不能回退锚点：空操作返回当前锚点。
        self.assertEqual(ledger.finalize(3), "b3")
        self.assertFalse(ledger.is_pruned("b4"))
        # 更深的最终确认（更小深度）向前推进。
        self.assertEqual(ledger.finalize(1), "b4")
        self.assertTrue(ledger.is_pruned("b4"))
        self.assertEqual(ledger.finalize(0), "b5")
        self.assertTrue(ledger.is_pruned("b5"))


class ExtraCheckpointSemanticsTests(unittest.TestCase):
    def test_checkpoint_bytes_are_deterministic(self) -> None:
        # 同一状态序列化两次必须字节一致（规范 JSON + 固定排序）。
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))
        self.assertEqual(ledger.to_checkpoint_bytes(),
                         ledger.to_checkpoint_bytes())

    def test_anchor_advances_automatically_after_restore(self) -> None:
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(4)
        submit_all(ledger, chain)          # tip b4，锚点 b3
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        restored.submit(chain[4] if len(chain) > 4 else blk(
            "b5", "b4", 1, (cb("cb5", 50), sp("tx5", "tx4", 49, lock=5))))
        self.assertEqual(restored.anchor_id, "b4")
        self.assertTrue(restored.is_pruned("b4"))
        # 旧锚点 b3 仍裁剪；窗口从 b5 开始。
        self.assertFalse(restored.is_pruned("b5"))

    def test_submit_after_finalize_depth_zero(self) -> None:
        ledger = ReorgLedger(finalization_depth=0)
        submit_all(ledger, linear_chain(2))  # tip b2，锚点 b2
        self.assertTrue(ledger.is_pruned("b2"))
        # 接块不受影响：新块挂在 tip 上，锚点 UTXO 即旧 tip 之后状态。
        nxt = blk("b3", "b2", 1, (cb("cb3", 50),))
        result = ledger.submit(nxt)
        self.assertEqual(ledger.tip_id, "b3")
        self.assertFalse(result.reorganized)
        self.assertEqual(ledger.anchor_id, "b3")
        # 但任何在锚点之下的分叉都不可判定。
        with self.assertRaises(RequiresHistoryError):
            ledger.submit(blk("old", "b1", 1, (cb("cold", 50),)))

    def test_low_work_deep_fork_is_still_requires_history(self) -> None:
        # 即使候选链累计 work 不足以成为 tip，也必须先以祖先状态验证；
        # 历史被裁时无法验证 → requires_history（不是静默接收，也不是判非法）。
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))  # tip b4，锚点 b3
        with self.assertRaises(RequiresHistoryError) as ctx:
            ledger.submit(blk("weak", "b1", 1, (cb("cweak", 999),)))
        self.assertEqual(ctx.exception.block_id, "weak")
        self.assertNotIn("weak", ledger)
        self.assertEqual(ledger.tip_id, "b4")

    def test_orphan_parent_below_anchor_but_header_known(self) -> None:
        # 父 id 是已裁剪块头 → requires_history 场景之一；未知 id 仍是 orphan。
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))
        self.assertTrue(ledger.contains("b1"))
        self.assertTrue(ledger.is_pruned("b1"))
        with self.assertRaises(RequiresHistoryError):
            ledger.submit(blk("c1", "b1", 1, (cb("cc1", 50),)))
        with self.assertRaises(OrphanBlockError):
            ledger.submit(blk("c2", "never-seen", 1, (cb("cc2", 50),)))

    def test_duplicate_submit_still_duplicate_after_prune(self) -> None:
        from reorg_ledger import DuplicateBlockError
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(4)
        submit_all(ledger, chain)
        # 已裁剪块的 ID 重复提交，仍是 DuplicateBlockError（块头还在树中）。
        with self.assertRaises(DuplicateBlockError):
            ledger.submit(chain[0])
        with self.assertRaises(DuplicateBlockError):
            ledger.submit(chain[3])

    def test_output_spent_before_anchor_is_missing_input_after_restore(self) -> None:
        # cb1:0 在高度 2 被 tx2 花掉（该块后被裁剪）；恢复后必须凭锚点 UTXO
        # 确定性判 missing-input，而不是 requires_history。
        ledger = ReorgLedger(finalization_depth=1)
        chain = linear_chain(4)
        submit_all(ledger, chain)           # 锚点 b3；cb1 的花费发生在 h2
        restored = ReorgLedger.from_checkpoint_bytes(ledger.to_checkpoint_bytes())
        bad = blk("x", "b4", 1,
                  (cb("cx", 50), sp("txbad", "cb1", 1)))
        with self.assertRaises(BlockValidationError) as ctx:
            restored.submit(bad)
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertEqual(str(ctx.exception.outpoint), "cb1:0")

    def test_checksum_tolerates_whitespace_reformatting(self) -> None:
        # 摘要是对规范化 data 计算的：外层 JSON 换缩进/空白不应导致拒绝；
        # 但改动 data 又不重算摘要必须拒绝。
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))
        envelope = json.loads(ledger.to_checkpoint_bytes())
        pretty = json.dumps(envelope, indent=4, ensure_ascii=False).encode("utf-8")
        restored = ReorgLedger.from_checkpoint_bytes(pretty)
        self.assertEqual(restored.tip_id, ledger.tip_id)
        envelope["data"]["subsidy"] = 0
        tampered = json.dumps(envelope).encode("utf-8")
        with self.assertRaises(CheckpointFormatError) as ctx:
            ReorgLedger.from_checkpoint_bytes(tampered)
        self.assertEqual(ctx.exception.reason, "bad-checksum")

    def test_save_checkpoint_path_contract(self) -> None:
        ledger = ReorgLedger(finalization_depth=0)
        ledger.submit(linear_chain(1)[0])
        with self.assertRaises(InputContractError):
            ledger.save_checkpoint(123)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            ReorgLedger.load_checkpoint(123)  # type: ignore[arg-type]


class CorruptCheckpointTests(unittest.TestCase):
    def _good_envelope(self) -> tuple[bytes, dict]:
        ledger = ReorgLedger(finalization_depth=1)
        submit_all(ledger, linear_chain(4))
        data = ledger.to_checkpoint_bytes()
        return data, json.loads(data)

    def test_bit_flip_and_truncation_rejected(self) -> None:
        data, _ = self._good_envelope()
        flipped = bytearray(data)
        flipped[len(flipped) // 2] ^= 0x01
        with self.assertRaises(CheckpointFormatError) as ctx:
            ReorgLedger.from_checkpoint_bytes(bytes(flipped))
        self.assertIn(ctx.exception.reason,
                      ("bad-checksum", "not-utf8", "not-json"))
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(b"")
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(data[: len(data) // 2])
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(b"not json at all")

    def test_bad_envelope_format_and_version(self) -> None:
        data, env = self._good_envelope()
        for mutated in (
            lambda e: e.update(format="wrong"),
            lambda e: e.update(version=999),
            lambda e: e.pop("sha256"),
        ):
            env2 = json.loads(data)
            mutated(env2)
            with self.assertRaises(CheckpointFormatError):
                ReorgLedger.from_checkpoint_bytes(
                    json.dumps(env2).encode("utf-8")
                )

    def test_payload_tamper_with_recomputed_checksum_rejected(self) -> None:
        _, env = self._good_envelope()
        original_payload = json.loads(json.dumps(env["data"]))

        def fresh_payload() -> dict:
            return json.loads(json.dumps(original_payload))

        def reseal(payload: dict) -> bytes:
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False
            ).encode("utf-8")
            out = dict(env)
            out["data"] = payload
            out["sha256"] = hashlib.sha256(body).hexdigest()
            return json.dumps(out, sort_keys=True).encode("utf-8")

        # 1) 断链：把 b4 的块头与块体父都改成不存在的块。
        p = fresh_payload()
        for node in p["nodes"]:
            if node["block_id"] == "b4":
                node["parent_id"] = "ghost"
                node["block"]["parent_id"] = "ghost"
        with self.assertRaises(CheckpointFormatError) as ctx:
            ReorgLedger.from_checkpoint_bytes(reseal(p))
        self.assertEqual(ctx.exception.reason, "broken-chain")

        # 1b) 只改块头父、不改块体父：块体/块头一致性矛盾，bad-structure。
        p = fresh_payload()
        for node in p["nodes"]:
            if node["block_id"] == "b4":
                node["parent_id"] = "ghost"
        with self.assertRaises(CheckpointFormatError) as ctx:
            ReorgLedger.from_checkpoint_bytes(reseal(p))
        self.assertEqual(ctx.exception.reason, "bad-structure")

        # 2) cumulative_work 与父链求和不符。
        p = fresh_payload()
        p["nodes"][-1]["cumulative_work"] += 1
        with self.assertRaises(CheckpointFormatError) as ctx:
            ReorgLedger.from_checkpoint_bytes(reseal(p))
        self.assertEqual(ctx.exception.reason, "bad-structure")

        # 3) 锚点不是 tip 的祖先（改 anchor_id 为侧块——这里改成 b4 本身，
        #    但其块体仍在，触发“锚点处必须已裁剪”的结构矛盾）。
        p = fresh_payload()
        p["anchor_id"] = "b4"
        p["anchor_height"] = 4
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 4) 重复节点。
        p = fresh_payload()
        p["nodes"].append(dict(p["nodes"][-1]))
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 5) 活动窗口块缺块体。
        p = fresh_payload()
        for node in p["nodes"]:
            if node["block_id"] == "b4":
                del node["block"]
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 6) 重复 UTXO / 重复 txid。
        p = fresh_payload()
        p["utxos"].append(list(p["utxos"][0]))
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))
        p = fresh_payload()
        p["applied_tx_ids"].append(p["applied_tx_ids"][0])
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 7) 两个创世 / 创世高度错误 / 字段类型错误。
        p = fresh_payload()
        for node in p["nodes"]:
            if node["block_id"] == "b1":
                node["parent_id"] = "root"
        # 引入 root 会断链；改为直接造第二个无父块更简单：
        p["nodes"][1]["parent_id"] = None
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        p = fresh_payload()
        p["subsidy"] = "50"
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 8) tip 不在节点集合中。
        p = fresh_payload()
        p["tip_id"] = "b99"
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

        # 9) 余额为 0 的非法 UTXO（违反 amount>=1 数据契约）。
        p = fresh_payload()
        p["utxos"][0][2] = 0
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(reseal(p))

    def test_wrong_input_type_contract(self) -> None:
        with self.assertRaises(InputContractError):
            ReorgLedger.from_checkpoint_bytes('{"x":1}')  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            ReorgLedger.from_checkpoint_bytes(123)  # type: ignore[arg-type]

    def test_missing_file_raises_filenotfound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                ReorgLedger.load_checkpoint(os.path.join(tmp, "absent.json"))

    def test_failed_load_leaves_no_partial_state(self) -> None:
        # classmethod 加载失败根本不返回对象；同时确认原账本不受损坏字节影响。
        data, _ = self._good_envelope()
        ledger = ReorgLedger.from_checkpoint_bytes(data)
        tip_before = ledger.tip_id
        bad = bytearray(data)
        bad[5] ^= 0xFF
        with self.assertRaises(CheckpointFormatError):
            ReorgLedger.from_checkpoint_bytes(bytes(bad))
        self.assertEqual(ledger.tip_id, tip_before)
        self.assertEqual(engine_utxos(ledger), engine_utxos(
            ReorgLedger.from_checkpoint_bytes(data)
        ))


if __name__ == "__main__":
    unittest.main()
