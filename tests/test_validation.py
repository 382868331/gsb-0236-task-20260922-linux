"""输入校验与错误语义测试。"""

from __future__ import annotations

import math
import unittest

from utxoledger import (
    Block,
    BlockTree,
    DuplicateInputError,
    InvalidBlockError,
    InvalidTransactionError,
    Output,
    Transaction,
    ValidationError,
)
from utxoledger.validation import validate_block
from factories import block, coinbase, genesis, tx


class ScalarValidationTests(unittest.TestCase):
    def test_bool_rejected_as_integer(self):
        # bool 是 int 子类，但契约要求显式拒绝。
        with self.assertRaises(ValidationError) as cm:
            validate_block(block("B", None, True))
        self.assertEqual(cm.exception.code, "type_error")
        self.assertIn("block=B", cm.exception.location)

        with self.assertRaises(ValidationError):
            BlockTree(subsidy=False)

        with self.assertRaises(ValidationError):
            validate_block(block("B", None, 1, [coinbase("x", value=True)]))

    def test_float_nan_infinity_rejected(self):
        for bad, code in ((float("nan"), "nan_value"),
                          (float("inf"), "infinity_value"),
                          (-float("inf"), "infinity_value")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as cm:
                    validate_block(block("B", None, bad))
                self.assertEqual(cm.exception.code, code)

    def test_plain_float_rejected_without_silent_rounding(self):
        with self.assertRaises(ValidationError) as cm:
            validate_block(block("B", None, 1.0))  # 不静默取整成 1
        self.assertEqual(cm.exception.code, "non_integer_number")

    def test_string_and_sign_rules(self):
        with self.assertRaises(ValidationError):
            validate_block(block("", None, 1))
        with self.assertRaises(ValidationError):
            validate_block(block("B", "", 1))  # 空 parent 字符串而不是 None
        with self.assertRaises(ValidationError) as cm:
            validate_block(block("B", None, 0))  # work 必须为正整数
        self.assertEqual(cm.exception.code, "value_error")
        with self.assertRaises(ValidationError):
            validate_block(block("B", None, -3))
        with self.assertRaises(InvalidBlockError):
            validate_block(block(123, None, 1))

    def test_errors_are_localizable(self):
        with self.assertRaises(ValidationError) as cm:
            # 重复输入属于静态错误，定位串需带 block 与 tx 名。
            validate_block(Block(block_id="B", parent=None, work=1,
                                 transactions=(
                tx("T", [("a", 0), ("a", 0)], [(10, 0)]),
            )))
        self.assertIsNotNone(cm.exception.location)
        self.assertIn("block=B", cm.exception.location)
        self.assertIn("tx=T", cm.exception.location)


class TransactionStructureTests(unittest.TestCase):
    def test_duplicate_input_within_transaction(self):
        t = tx("T", [("u", 0), ("u", 0)], [(10, 0)])
        with self.assertRaises(DuplicateInputError) as cm:
            validate_block(Block(block_id="B", parent=None, work=1,
                                 transactions=(coinbase("cb"), t)))
        self.assertIn("input=1", cm.exception.location)

    def test_coinbase_must_have_no_inputs(self):
        from utxoledger import Input, OutPoint
        bad = Transaction(
            txid="cb",
            inputs=(Input(OutPoint("u", 0)),),
            outputs=(Output(10, 0),),
            is_coinbase=True,
        )
        with self.assertRaises(InvalidTransactionError) as cm:
            validate_block(
                Block(block_id="B", parent=None, work=1, transactions=(bad,)))
        self.assertIn("不能有输入", cm.exception.message)

    def test_coinbase_must_be_first_and_unique(self):
        good = tx("T", [("u", 0)], [(5, 0)])
        with self.assertRaises(InvalidTransactionError):
            validate_block(Block(block_id="B", parent=None, work=1,
                                 transactions=(good, coinbase("cb"))))
        with self.assertRaises(InvalidTransactionError):
            validate_block(Block(block_id="B", parent=None, work=1,
                                 transactions=(coinbase("cb1"),
                                               coinbase("cb2"))))

    def test_empty_inputs_or_outputs_rejected(self):
        from utxoledger import Input, OutPoint
        from utxoledger.validation import validate_transaction
        with self.assertRaises(InvalidTransactionError):
            validate_transaction(
                Transaction(txid="t", inputs=(), outputs=(Output(1, 0),)),
                block_id="B", tx_index=0)
        with self.assertRaises(InvalidTransactionError):
            validate_transaction(
                Transaction(txid="t",
                            inputs=(Input(OutPoint("u", 0)),), outputs=()),
                block_id="B", tx_index=0)

    def test_duplicate_txid_within_block(self):
        b = Block(block_id="B", parent="G", work=1, transactions=(
            coinbase("cb"),
            tx("same", [("x", 0)], [(1, 0)]),
            tx("same", [("y", 0)], [(1, 0)]),
        ))
        tree = BlockTree()
        tree.add_block(genesis())
        with self.assertRaises(InvalidTransactionError) as cm:
            tree.add_block(b)
        self.assertEqual(cm.exception.code, "txid_replay")


if __name__ == "__main__":
    unittest.main()
