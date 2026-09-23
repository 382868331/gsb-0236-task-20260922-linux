"""UTXO 语义测试：双花、coinbase 超额、锁定高度、侧枝、tip 选择。"""

from __future__ import annotations

import unittest

from utxoledger import (
    BlockTree,
    CoinbaseExcessError,
    DuplicateBlockError,
    LockHeightError,
    MissingOutPointError,
    UnknownParentError,
    ValueBalanceError,
)
from factories import block, coinbase, genesis, tx


class BasicLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tree = BlockTree(subsidy=50)
        self.tree.add_block(genesis("G", "cbG", value=50, lock_height=1))

    def test_genesis_tip_and_utxo(self):
        self.assertEqual(self.tree.tip, "G")
        self.assertEqual(self.tree.active_chain_ids(), ("G",))
        u = self.tree.utxo("cbG", 0)
        self.assertEqual(u.value, 50)
        self.assertEqual(u.lock_height, 1)

    def test_spend_after_lock_height(self):
        # 创世锁到高度 1，在高度 1 的块里可花。
        b1 = block("B1", "G", 1, [
            tx("t1", [("cbG", 0)], [(50, 0)]),
        ])
        self.tree.add_block(b1)
        self.assertIsNone(self.tree.utxo("cbG", 0))
        self.assertEqual(self.tree.utxo("t1", 0).value, 50)

    def test_lock_height_blocks_spend(self):
        # 创世锁到高度 2，高度 1 不可花。
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", value=50, lock_height=2))
        b1 = block("B1", "G", 1, [
            tx("t1", [("cbG", 0)], [(50, 0)]),
        ])
        with self.assertRaises(LockHeightError) as cm:
            tree.add_block(b1)
        self.assertEqual(cm.exception.block_height, 1)
        self.assertEqual(cm.exception.lock_height, 2)
        # 失败无副作用：树仍只有 G，tip 不变。
        self.assertEqual(tree.tip, "G")
        self.assertEqual(len(tree), 1)
        self.assertIsNotNone(tree.utxo("cbG", 0))

    def test_output_sum_exceeds_input_sum(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        b1 = block("B1", "G", 1, [
            tx("t1", [("cbG", 0)], [(51, 0)]),
        ])
        with self.assertRaises(ValueBalanceError) as cm:
            tree.add_block(b1)
        self.assertEqual((cm.exception.input_sum, cm.exception.output_sum),
                         (50, 51))

    def test_missing_outpoint(self):
        b1 = block("B1", "G", 1, [
            tx("t1", [("ghost", 9)], [(10, 0)]),
        ])
        with self.assertRaises(MissingOutPointError):
            self.tree.add_block(b1)
        self.assertEqual(self.tree.tip, "G")

    def test_coinbase_excess_without_fees(self):
        b1 = block("B1", "G", 1, [coinbase("cb1", 51)])
        with self.assertRaises(CoinbaseExcessError) as cm:
            self.tree.add_block(b1)
        self.assertEqual((cm.exception.claimed, cm.exception.cap), (51, 50))

    def test_coinbase_can_claim_subsidy_plus_fee(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        # t1 输入 50 输出 40 -> 手续费 10；coinbase 可取 60。
        b1 = block("B1", "G", 1, [
            coinbase("cb1", 60),
            tx("t1", [("cbG", 0)], [(40, 0)]),
        ])
        tree.add_block(b1)
        self.assertEqual(tree.tip, "B1")
        self.assertEqual(tree.utxo("cb1", 0).value, 60)
        self.assertEqual(tree.utxo("t1", 0).value, 40)

    def test_coinbase_excess_even_with_fee(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        b1 = block("B1", "G", 1, [
            coinbase("cb1", 61),  # 上限 60
            tx("t1", [("cbG", 0)], [(40, 0)]),
        ])
        with self.assertRaises(CoinbaseExcessError):
            tree.add_block(b1)


class DoubleSpendTests(unittest.TestCase):
    def test_same_outpoint_spent_twice_across_blocks(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        tree.add_block(block("B1", "G", 1, [
            tx("t1", [("cbG", 0)], [(25, 0), (25, 0)]),
        ]))
        # t2 和 t3 在同一个块里争抢 t1:0。
        bad = block("B2", "B1", 1, [
            tx("t2", [("t1", 0)], [(25, 0)]),
            tx("t3", [("t1", 0)], [(25, 0)]),
        ])
        with self.assertRaises(MissingOutPointError) as cm:
            tree.add_block(bad)
        self.assertIn("t3", str(cm.exception))
        self.assertEqual(tree.tip, "B1")
        self.assertEqual(len(tree), 2)

    def test_duplicate_input_rejected(self):
        # 同一交易里重复引用同一 OutPoint（在结构校验阶段拒绝）。
        from utxoledger import Block
        from factories import coinbase as cb
        b = Block(block_id="B", parent=None, work=1, transactions=(
            cb("cb", 10),
            tx("t", [("x", 0), ("x", 0)], [(5, 0)]),
        ))
        with self.assertRaises(Exception):
            BlockTree().add_block(b)


class TreeRulesTests(unittest.TestCase):
    def setUp(self):
        self.tree = BlockTree(subsidy=50)
        self.tree.add_block(genesis())

    def test_unknown_parent(self):
        with self.assertRaises(UnknownParentError):
            self.tree.add_block(block("X", "NOPE", 1, []))
        self.assertEqual(len(self.tree), 1)

    def test_duplicate_block(self):
        with self.assertRaises(DuplicateBlockError):
            self.tree.add_block(genesis())

    def test_second_genesis_rejected(self):
        from utxoledger import InvalidBlockError
        with self.assertRaises(InvalidBlockError):
            self.tree.add_block(block("G2", None, 1, []))

    def test_chain_height_limit(self):
        from utxoledger import ChainLimitError
        tree = BlockTree(subsidy=50, max_chain_blocks=3)
        tree.add_block(genesis("G"))
        tree.add_block(block("A1", "G", 1, []))
        tree.add_block(block("A2", "A1", 1, []))
        with self.assertRaises(ChainLimitError):
            tree.add_block(block("A3", "A2", 1, []))
        # 已达上限的链仍可在别的高度收到合法侧枝（不超过上限）。
        tree.add_block(block("B1", "G", 1, []))
        self.assertEqual(tree.active_chain_ids()[-1], "A2")


if __name__ == "__main__":
    unittest.main()
