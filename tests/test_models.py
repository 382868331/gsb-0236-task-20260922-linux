"""入口契约校验测试：类型、整数纯度（拒绝 bool / float / NaN / Infinity）等。"""

import math
import unittest

from reorg_ledger import (
    Block,
    InputContractError,
    NonFiniteNumberError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)


def cb(tx_id: str, amount: int = 50) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, 0),))


class ModelContractTests(unittest.TestCase):
    def test_bool_rejected_everywhere(self) -> None:
        with self.assertRaises(InputContractError):
            TxOutput(True, 0)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            TxOutput(1, False)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            OutPoint("t", True)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            Block("b", None, True, (cb("g"),))  # type: ignore[arg-type]

    def test_float_rejected_with_distinct_nan_inf(self) -> None:
        with self.assertRaises(InputContractError):
            TxOutput(1.0, 0)  # type: ignore[arg-type]
        for bad in (math.nan, math.inf, -math.inf):
            with self.assertRaises(NonFiniteNumberError):
                TxOutput(bad, 0)  # type: ignore[arg-type]

    def test_strings_and_ranges(self) -> None:
        with self.assertRaises(InputContractError):
            TxOutput(0, 0)        # 金额必须 >= 1
        with self.assertRaises(InputContractError):
            TxOutput(-3, 0)
        with self.assertRaises(InputContractError):
            TxOutput(1, -1)       # 锁定高度非负
        with self.assertRaises(InputContractError):
            OutPoint("", 0)
        with self.assertRaises(InputContractError):
            TxInput("t", -1)
        with self.assertRaises(InputContractError):
            TxInput("t", 0, -1)      # 相对年龄要求非负
        with self.assertRaises(InputContractError):
            TxInput("t", 0, True)    # type: ignore[arg-type]
        # 默认值为 0（旧两参构造保持兼容）。
        self.assertEqual(TxInput("t", 0).min_age, 0)
        with self.assertRaises(InputContractError):
            Transaction("t", (), ())  # 至少一个输出
        with self.assertRaises(InputContractError):
            Block("b", None, 0, (cb("g"),))   # work 必须为正整数
        with self.assertRaises(InputContractError):
            Block("b", None, 1, ())           # 至少一笔交易
        with self.assertRaises(InputContractError):
            Block("b", "b", 1, (cb("g"),))    # 不可自为父
        with self.assertRaises(InputContractError):
            Transaction("t", "nope", (TxOutput(1, 0),))  # type: ignore[arg-type]

    def test_wrong_element_types(self) -> None:
        with self.assertRaises(InputContractError):
            Transaction("t", (("x", 0),), (TxOutput(1, 0),))  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            Block("b", None, 1, (("raw",),))  # type: ignore[arg-type]

    def test_ledger_constructor_contract(self) -> None:
        with self.assertRaises(InputContractError):
            ReorgLedger(subsidy=True)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            ReorgLedger(subsidy=-1)
        with self.assertRaises(InputContractError):
            ReorgLedger(max_blocks=0)
        with self.assertRaises(InputContractError):
            ReorgLedger().submit("not a block")  # type: ignore[arg-type]

    def test_no_silent_coercion_message_is_locatable(self) -> None:
        try:
            TxOutput(5, math.nan)  # type: ignore[arg-type]
        except NonFiniteNumberError as exc:
            self.assertIn("lock_height", str(exc))
        else:
            self.fail("expected NonFiniteNumberError")


if __name__ == "__main__":
    unittest.main()
