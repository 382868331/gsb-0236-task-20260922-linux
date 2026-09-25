"""引擎场景测试：双花、coinbase 超额、锁定高度、无效侧枝、原子重组、选链、有界。"""

import unittest

from reorg_ledger import (
    BlockConflictError,
    Block,
    BlockValidationError,
    ChainLimitError,
    DuplicateBlockError,
    OrphanBlockError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import ReferenceLimit, ReferenceReject, replay


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def spend(tx_id: str, prev: tuple[str, int], amount_out: int,
          lock: int = 0) -> Transaction:
    return Transaction(
        tx_id,
        (TxInput(prev[0], prev[1]),),
        (TxOutput(amount_out, lock),),
    )


def block(block_id: str, parent: str | None, work: int,
          txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


def genesis(coin_lock: int = 1, amount: int = 50, block_id: str = "G",
            work: int = 10) -> Block:
    return block(block_id, None, work, (coinbase("gcb", amount, coin_lock),))


class BasicChainTests(unittest.TestCase):
    def test_genesis_tip_and_utxo(self) -> None:
        ledger = ReorgLedger()
        result = ledger.submit(genesis())
        self.assertTrue(result.became_tip)
        self.assertFalse(result.reorganized)
        self.assertEqual(ledger.tip_id, "G")
        self.assertEqual(ledger.height, 1)
        self.assertEqual(ledger.cumulative_work, 10)
        self.assertEqual(ledger.active_path, ("G",))
        utxos = ledger.snapshot_utxos()
        self.assertEqual(utxos[OutPoint("gcb", 0)].amount, 50)
        self.assertEqual(utxos[OutPoint("gcb", 0)].lock_height, 1)
        # 快照是副本，改动不影响账本。
        utxos.clear()
        self.assertEqual(len(ledger.snapshot_utxos()), 1)

    def test_linear_extension_and_spend(self) -> None:
        ledger = ReorgLedger()
        ledger.submit(genesis())
        # 普通交易：输入 50，输出 40，手续费 10；coinbase 可铸 50+10。
        a = block(
            "A", "G", 5,
            (coinbase("cba", 60), spend("ta", ("gcb", 0), 40)),
        )
        result = ledger.submit(a)
        self.assertEqual(ledger.tip_id, "A")
        self.assertFalse(result.reorganized)  # 同链延伸不是重组
        utxos = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("gcb", 0), utxos)
        self.assertEqual(utxos[OutPoint("ta", 0)].amount, 40)
        self.assertEqual(utxos[OutPoint("cba", 0)].amount, 60)

    def test_matches_independent_reference(self) -> None:
        ledger = ReorgLedger()
        g = genesis()
        a = block(
            "A", "G", 5,
            (coinbase("cba", 55), spend("ta", ("gcb", 0), 45, lock=2)),
        )
        b = block(
            "B", "A", 7,
            (coinbase("cbb", 50), spend("tb", ("ta", 0), 45, lock=0)),
        )
        for blk in (g, a, b):
            ledger.submit(blk)
        expected = replay([g, a, b])
        actual = {
            op: (u.amount, u.lock_height)
            for op, u in ledger.snapshot_utxos().items()
        }
        self.assertEqual(actual, dict(expected))


class RejectionTests(unittest.TestCase):
    def _fresh(self) -> ReorgLedger:
        ledger = ReorgLedger()
        ledger.submit(genesis())
        return ledger

    def test_double_spend_across_blocks(self) -> None:
        ledger = self._fresh()
        ledger.submit(block(
            "A", "G", 5,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)),
        ))
        # 同一枚输出再花一次：missing-input。
        bad = block(
            "B", "A", 5,
            (coinbase("cbb", 50), spend("tb", ("gcb", 0), 50)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertEqual(ctx.exception.block_id, "B")
        self.assertEqual(ctx.exception.tx_id, "tb")
        self.assertEqual(str(ctx.exception.outpoint), "gcb:0")
        self.assertNotIn("B", ledger)
        self.assertEqual(ledger.tip_id, "A")

    def test_duplicate_input_within_transaction(self) -> None:
        ledger = self._fresh()
        tx = Transaction(
            "tdup",
            (TxInput("gcb", 0), TxInput("gcb", 0)),
            (TxOutput(90, 0),),
        )
        bad = block("B", "G", 5, (coinbase("cbb", 50), tx))
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "duplicate-input")
        self.assertNotIn("B", ledger)

    def test_coinbase_overflow_boundary(self) -> None:
        ledger = self._fresh()
        # 手续费 = 1，允许 51；铸 52 必须拒绝。
        too_much = block(
            "A", "G", 5,
            (coinbase("cba", 52), spend("ta", ("gcb", 0), 49)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(too_much)
        self.assertEqual(ctx.exception.reason, "coinbase-overflow")
        self.assertEqual(ledger.tip_id, "G")
        # 边界：恰好 51 通过。
        exact = block(
            "A2", "G", 5,
            (coinbase("cba2", 51), spend("ta2", ("gcb", 0), 49)),
        )
        ledger.submit(exact)
        self.assertEqual(ledger.tip_id, "A2")

    def test_output_exceeds_input(self) -> None:
        ledger = self._fresh()
        bad = block(
            "A", "G", 5,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 51)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "output-exceeds-input")

    def test_lock_height_boundary(self) -> None:
        # 创世输出锁定到高度 3。
        ledger = ReorgLedger()
        ledger.submit(genesis(coin_lock=3))
        # 高度 2 花费 → 锁定拒绝。
        early = block(
            "A", "G", 1,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(early)
        self.assertEqual(ctx.exception.reason, "output-locked")
        self.assertEqual(ledger.tip_id, "G")
        # 空块延伸到高度 3（coinbase 铸 50，无手续费）。
        ledger.submit(block("A2", "G", 1, (coinbase("cba2", 50),)))
        ok = block(
            "B", "A2", 1,
            (coinbase("cbb", 50), spend("tb", ("gcb", 0), 50)),
        )
        ledger.submit(ok)  # lock_height == height 边界允许
        self.assertEqual(ledger.tip_id, "B")

    def test_coinbase_must_be_first_and_unique(self) -> None:
        ledger = self._fresh()
        bad_pos = block(
            "A", "G", 1,
            (spend("ta", ("gcb", 0), 50), coinbase("cba", 40)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad_pos)
        self.assertEqual(ctx.exception.reason, "coinbase-position")
        two = block(
            "A2", "G", 1,
            (coinbase("c1", 25), coinbase("c2", 25)),
        )
        with self.assertRaises(BlockValidationError) as ctx2:
            ledger.submit(two)
        self.assertEqual(ctx2.exception.reason, "multiple-coinbase")

    def test_invalid_side_branch_is_kept_out_and_mainchain_untouched(self) -> None:
        ledger = self._fresh()
        before = ledger.snapshot_utxos()
        bad = block(
            "S", "G", 100,
            (coinbase("cbs", 999),),  # 无手续费，铸 999 远超补贴 50
        )
        with self.assertRaises(BlockValidationError):
            ledger.submit(bad)
        self.assertEqual(ledger.tip_id, "G")
        self.assertEqual(ledger.snapshot_utxos(), before)
        self.assertNotIn("S", ledger)
        # 高 work 非法侧枝没有污染状态：正常块照常接收。
        good = block(
            "A", "G", 1,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)),
        )
        ledger.submit(good)
        self.assertEqual(ledger.tip_id, "A")

    def test_failed_submit_is_atomic_against_reference(self) -> None:
        ledger = self._fresh()
        path_before = ledger.active_path
        utxos_before = dict(ledger.snapshot_utxos())
        bad = block(
            "S", "G", 1,
            (coinbase("cbs", 51),),
        )
        with self.assertRaises(BlockValidationError):
            ledger.submit(bad)
        self.assertEqual(ledger.active_path, path_before)
        self.assertEqual(dict(ledger.snapshot_utxos()), utxos_before)
        with self.assertRaises(ReferenceReject):
            replay([genesis(), bad])


class OrphanAndDuplicateTests(unittest.TestCase):
    def test_orphan_is_buffered_and_connects_when_parent_arrives(self) -> None:
        ledger = ReorgLedger()
        child = block("C", "missing-parent", 1, (coinbase("cbc", 50),))
        result = ledger.submit(child)
        self.assertTrue(result.buffered)
        self.assertFalse(ledger.is_verified("C"))
        self.assertTrue(ledger.is_buffered_orphan("C"))
        self.assertIsNone(ledger.tip_id)
        # 缓冲块不计入累计 work 候选、不提前应用交易。
        self.assertEqual(ledger.snapshot_utxos(), {})
        ledger.submit(genesis())
        self.assertTrue(ledger.is_buffered_orphan("C"))
        parent = block("missing-parent", "G", 1, (coinbase("cbp", 50),))
        ledger.submit(parent)  # 父到达即按 ID 顺序递归接入 C
        self.assertTrue(ledger.is_verified("C"))
        self.assertEqual(ledger.active_path, ("G", "missing-parent", "C"))
        # OrphanBlockError 仍是公开异常类型（向后兼容的错误语义）。
        self.assertTrue(issubclass(OrphanBlockError, Exception))

    def test_duplicate_is_idempotent_conflict_and_second_genesis(self) -> None:
        ledger = ReorgLedger()
        g = genesis()
        first = ledger.submit(g)
        again = ledger.submit(g)  # 同内容：幂等
        self.assertEqual(again, first)
        with self.assertRaises(BlockConflictError):
            ledger.submit(
                block("G", None, 11, (coinbase("gcb", 50),))
            )
        self.assertIsInstance(BlockConflictError("x"), DuplicateBlockError)
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(genesis(block_id="G2"))
        self.assertEqual(ctx.exception.reason, "multiple-genesis")


class TieBreakTests(unittest.TestCase):
    def test_equal_work_picks_lexicographically_smaller_tip(self) -> None:
        ledger = ReorgLedger()
        ledger.submit(genesis(work=1))
        # 两条侧链累计 work 相同；tip id "A1" < "B0"。
        ledger.submit(block("A1", "G", 5, (coinbase("ca", 50),)))
        result_b = ledger.submit(block("B0", "G", 5, (coinbase("cb", 50),)))
        self.assertFalse(result_b.became_tip)
        self.assertFalse(result_b.reorganized)
        self.assertEqual(ledger.tip_id, "A1")
        # 反向提交顺序结论一致（选链与提交顺序无关）。
        ledger2 = ReorgLedger()
        ledger2.submit(genesis(work=1))
        ledger2.submit(block("B0", "G", 5, (coinbase("cb", 50),)))
        self.assertTrue(ledger2.submit(
            block("A1", "G", 5, (coinbase("ca", 50),))
        ).became_tip)
        self.assertEqual(ledger2.tip_id, "A1")


class ReorgTests(unittest.TestCase):
    def test_real_reorg_deactivate_activate_and_utxo(self) -> None:
        ledger = ReorgLedger()
        g = genesis(amount=50, coin_lock=1, work=10)
        a = block(
            "A", "G", 1,
            (coinbase("cba", 60), spend("ta", ("gcb", 0), 40)),
        )
        ledger.submit(g)
        ledger.submit(a)
        self.assertEqual(ledger.active_path, ("G", "A"))

        # 侧枝 B 累计 work 60 > 主链 11，触发真实重组。
        b = block(
            "B", "G", 50,
            (coinbase("cbb", 50), spend("tb", ("gcb", 0), 50, lock=2)),
        )
        result = ledger.submit(b)
        self.assertTrue(result.became_tip)
        self.assertTrue(result.reorganized)
        assert result.reorg is not None
        self.assertEqual(result.reorg.fork_id, "G")
        self.assertEqual(result.reorg.deactivated, ("A",))
        self.assertEqual(result.reorg.activated, ("B",))
        self.assertEqual(ledger.active_path, ("G", "B"))

        # 撤销账本：旧链 UTXO 消失，新链 UTXO 出现，与独立参考从创世重放一致。
        utxos = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("ta", 0), utxos)
        self.assertNotIn(OutPoint("cba", 0), utxos)
        self.assertIn(OutPoint("tb", 0), utxos)
        self.assertEqual(
            {op: (u.amount, u.lock_height) for op, u in utxos.items()},
            dict(replay([g, b])),
        )

        # 旧链并未丢失：在 A 上延续高 work 链 C，应再重组回去。
        c = block(
            "C", "A", 100,
            (coinbase("cbc", 50), spend("tc", ("ta", 0), 40, lock=0)),
        )
        result2 = ledger.submit(c)
        self.assertTrue(result2.reorganized)
        assert result2.reorg is not None
        self.assertEqual(result2.reorg.fork_id, "G")
        self.assertEqual(result2.reorg.deactivated, ("B",))
        self.assertEqual(result2.reorg.activated, ("A", "C"))
        self.assertEqual(ledger.active_path, ("G", "A", "C"))
        utxos2 = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("tb", 0), utxos2)
        self.assertEqual(
            {op: (u.amount, u.lock_height) for op, u in utxos2.items()},
            dict(replay([g, a, c])),
        )

    def test_side_branch_double_spend_flavor_is_valid_on_own_ancestor(self) -> None:
        """侧枝花掉同一枚父块输出是合法的独立历史；重组后它才是花费者。"""
        ledger = ReorgLedger()
        g = genesis(coin_lock=1)
        a = block("A", "G", 2, (coinbase("ca", 50),
                                spend("ta", ("gcb", 0), 30)))
        b = block("B", "G", 2, (coinbase("cb", 50),
                                spend("tb", ("gcb", 0), 20)))
        ledger.submit(g)
        ledger.submit(a)
        # 同分 tip id："A" 已胜出；B 以祖先状态 G 验证合法，应被树接收。
        result = ledger.submit(b)
        self.assertFalse(result.became_tip)
        self.assertFalse(result.reorganized)
        self.assertIn("B", ledger)
        self.assertEqual(ledger.tip_id, "A")
        # B 累积更多 work 后重组，同一枚 gcb:0 的花费者变为 tb。
        b2 = block("B2", "B", 5, (coinbase("cb2", 50),))
        result2 = ledger.submit(b2)
        self.assertTrue(result2.reorganized)
        utxos = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("ta", 0), utxos)
        self.assertIn(OutPoint("tb", 0), utxos)


class ChainLimitTests(unittest.TestCase):
    def test_100_block_boundary_is_budget_not_invalidity(self) -> None:
        ledger = ReorgLedger(max_blocks=3)
        ledger.submit(genesis())
        ledger.submit(block("A", "G", 1, (coinbase("ca", 50),)))
        ledger.submit(block("B", "A", 1, (coinbase("cb", 50),)))
        over = block("C", "B", 1, (coinbase("cc", 50),))
        with self.assertRaises(ChainLimitError) as ctx:
            ledger.submit(over)
        self.assertEqual(ctx.exception.depth, 4)
        self.assertEqual(ctx.exception.limit, 3)
        self.assertNotIn("C", ledger)
        self.assertEqual(ledger.tip_id, "B")
        # 侧枝在界内仍可接收（即使触发重组）。
        side = block("S", "A", 10, (coinbase("cs", 50),))
        ledger.submit(side)
        self.assertEqual(ledger.tip_id, "S")
        # 侧枝第 4 块同样触界。
        with self.assertRaises(ChainLimitError):
            ledger.submit(block("S2", "S", 1, (coinbase("cs2", 50),)))
        # 预算耗尽不是“非法”：BlockValidationError 与其互斥。
        self.assertFalse(issubclass(ChainLimitError, BlockValidationError))
        with self.assertRaises(ReferenceLimit):
            replay(
                [genesis(),
                 block("A", "G", 1, (coinbase("ca", 50),)),
                 block("B", "A", 1, (coinbase("cb", 50),)),
                 over],
                max_blocks=3,
            )

    def test_default_limit_is_100(self) -> None:
        self.assertEqual(ReorgLedger().max_blocks, 100)


if __name__ == "__main__":
    unittest.main()
