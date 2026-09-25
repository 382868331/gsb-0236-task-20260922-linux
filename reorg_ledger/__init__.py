"""有界 UTXO 链重组的撤销账本（可复用库）。

公开接口：

* 数据结构：:class:`Block`、:class:`Transaction`、:class:`TxInput`、
  :class:`TxOutput`、:class:`UTXO`、:class:`OutPoint`
* 引擎：:class:`ReorgLedger`、:class:`SubmitResult`、:class:`ReorgPath`
* 异常：见 :mod:`reorg_ledger.errors`
* 常量：:data:`DEFAULT_SUBSIDY`、:data:`MAX_CHAIN_BLOCKS`

典型用法见仓库根目录 ``demo.py``。
"""

from __future__ import annotations

from .engine import (
    DEFAULT_MAX_ORPHANS,
    DEFAULT_SUBSIDY,
    MAX_CHAIN_BLOCKS,
    ReorgLedger,
    ReorgPath,
    SubmitResult,
)
from .errors import (
    BlockValidationError,
    ChainLimitError,
    DuplicateBlockError,
    InputContractError,
    LedgerError,
    LedgerStateError,
    NonFiniteNumberError,
    OrphanBlockError,
    AncestorInvalidError,
    BlockConflictError,
)
from .models import Block, OutPoint, Transaction, TxInput, TxOutput, UTXO

__all__ = [
    "Block",
    "Transaction",
    "TxInput",
    "TxOutput",
    "UTXO",
    "OutPoint",
    "ReorgLedger",
    "SubmitResult",
    "ReorgPath",
    "LedgerError",
    "InputContractError",
    "NonFiniteNumberError",
    "BlockValidationError",
    "OrphanBlockError",
    "ChainLimitError",
    "DuplicateBlockError",
    "LedgerStateError",
    "AncestorInvalidError",
    "BlockConflictError",
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "DEFAULT_MAX_ORPHANS",
]
