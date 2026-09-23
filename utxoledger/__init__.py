"""有界 UTXO 链重组的撤销账本 —— 可复用库入口。

典型用法::

    from utxoledger import Block, BlockTree, Input, OutPoint, Output, Transaction

    tree = BlockTree(subsidy=50)
    tree.add_block(genesis_block)
    tree.add_block(block)
    print(tree.tip, tree.active_chain_ids())
    for u in tree.utxos():
        print(u)

子模块：
* :mod:`utxoledger.types`      —— 公开冻结数据结构。
* :mod:`utxoledger.errors`     —— 错误语义（校验错误 / 无解 / 预算耗尽）。
* :mod:`utxoledger.validation` —— 静态输入校验。
* :mod:`utxoledger.state`      —— 区块执行与撤销日志。
* :mod:`utxoledger.chain`      —— 区块树、tip 选择与原子重组。
"""

from __future__ import annotations

from .chain import (
    DEFAULT_COINBASE_SUBSIDY,
    DEFAULT_SEARCH_BUDGET,
    BlockTree,
)
from .errors import (
    BudgetExhaustedError,
    ChainLimitError,
    CoinbaseExcessError,
    DuplicateBlockError,
    DuplicateInputError,
    InvalidBlockError,
    InvalidTransactionError,
    LedgerError,
    LockHeightError,
    MissingOutPointError,
    NoSolutionError,
    UnknownParentError,
    ValidationError,
    ValueBalanceError,
)
from .state import Journal
from .types import Block, Input, OutPoint, Output, Reorg, Transaction, UTXO
from .validation import MAX_CHAIN_BLOCKS

__all__ = [
    "Block",
    "Input",
    "OutPoint",
    "Output",
    "Reorg",
    "Transaction",
    "UTXO",
    "Journal",
    "BlockTree",
    "MAX_CHAIN_BLOCKS",
    "DEFAULT_COINBASE_SUBSIDY",
    "DEFAULT_SEARCH_BUDGET",
    "LedgerError",
    "ValidationError",
    "InvalidBlockError",
    "DuplicateBlockError",
    "UnknownParentError",
    "ChainLimitError",
    "InvalidTransactionError",
    "DuplicateInputError",
    "MissingOutPointError",
    "LockHeightError",
    "ValueBalanceError",
    "CoinbaseExcessError",
    "NoSolutionError",
    "BudgetExhaustedError",
]

__version__ = "1.0.0"
