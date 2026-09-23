"""接口边界（100 块上限）与撤销往返的补充测试。"""

from __future__ import annotations

import unittest

from utxoledger import BlockTree, ChainLimitError
from utxoledger.state import apply_block, undo_block
from factories import block, genesis
import reference as ref


class ChainLimitTests(unittest.TestCase):
    def test_default_limit_is_100_blocks(self):
        tree = BlockTree(subsidy=0)
        tree.add_block(genesis("G", "cbG", 0, 0))
        prev = "G"
        for i in range(1, 100):  # 填到恰好 100 块
            tree.add_block(block(f"B{i}", prev, 1, []))
            prev = f"B{i}"
        self.assertEqual(len(tree.active_chain_ids()), 100)
        # 第 101 块必须拒绝。
        with self.assertRaises(ChainLimitError) as cm:
            tree.add_block(block("B100", prev, 1, []))
        self.assertEqual(cm.exception.code, "chain_limit")
        self.assertEqual(tree.tip, "B99")
        self.assertEqual(len(tree), 100)

    def test_limit_counts_per_chain_not_total_nodes(self):
        # 上限按“祖先链长度”逐链计数：B 链 G,B1,B2 恰好 3 块合法，
        # B3 使链长变为 4，拒绝；A 链不受影响。
        tree = BlockTree(subsidy=50, max_chain_blocks=3)
        tree.add_block(genesis("G"))
        tree.add_block(block("A1", "G", 5, []))
        tree.add_block(block("B1", "G", 1, []))
        tree.add_block(block("B2", "B1", 1, []))
        self.assertEqual(len(tree), 4)
        with self.assertRaises(ChainLimitError):
            tree.add_block(block("B3", "B2", 1, []))
        self.assertEqual(tree.tip, "A1")


class TieBreakTests(unittest.TestCase):
    def test_lexicographic_tie_at_equal_cumulative_work(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G"))
        tree.add_block(block("zzz", "G", 7, []))
        tree.add_block(block("aaa", "G", 7, []))
        self.assertEqual(tree.tip, "aaa")  # 同分取字典序
        # 在落后侧上再追加 1 work 反超。
        tree.add_block(block("zzz2", "zzz", 1, []))
        self.assertEqual(tree.tip, "zzz2")


class UndoRoundTripTests(unittest.TestCase):
    """往返一致只作补充：apply 后 undo 必须精确回到原状态。"""

    def test_apply_undo_roundtrip(self):
        tree = BlockTree(subsidy=50)
        g = genesis("G", "cbG", 50, 0)
        tree.add_block(g)
        b = tree.get_block("G")
        state = {}
        used: set[str] = set()
        j1 = apply_block(state, used, b, 0, 50)
        self.assertIn(("cbG", 0), {(op.txid, op.output_index)
                                   for op in state})
        undo_block(state, used, b, j1)
        self.assertEqual(state, {})
        self.assertEqual(used, set())

    def test_reference_and_core_share_no_code(self):
        # 独立参考不得导入被测核心（强制隔离，防止“自测自”）。
        import inspect
        src = inspect.getsource(ref)
        self.assertNotIn("import utxoledger", src)
        self.assertNotIn("from utxoledger", src)


if __name__ == "__main__":
    unittest.main()
