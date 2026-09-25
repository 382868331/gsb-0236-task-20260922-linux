"""核心引擎：有界 UTXO 区块树 + 撤销账本式重组。

设计要点
--------
1. **区块树**：所有通过祖先状态验证的区块都保留在内存树中（含非活跃侧枝）。
2. **祖先状态验证**：接收区块时，从创世沿其父链重放到父块，再按序应用本块
   交易。验证在临时状态上进行，失败时引擎状态零变更。
3. **选链**：累计 ``work`` 最大；累计 work 相同取 *tip 区块 ID 字典序较小*。
4. **原子重组**：tip 改变时，先在临时状态上从创世重放整条新链，成功后一次性
   切换 tip / UTXO / 活跃路径——即“撤销旧链再应用新链”的原子提交；中途任何
   异常都保留旧状态。
5. **有界**：链长（含创世）超过 ``max_blocks``（默认 100）抛
   :class:`ChainLimitError`（预算耗尽），区别于 :class:`BlockValidationError`
   （被证明非法）。

引擎内全部金额运算均为任意精度整数，不使用浮点。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    BlockValidationError,
    ChainLimitError,
    ConflictingBlockError,
    DuplicateBlockError,
    InputContractError,
    LedgerStateError,
    OrphanBlockError,
)
from .models import Block, OutPoint, Transaction, UTXO

__all__ = [
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "DEFAULT_ORPHAN_CAPACITY",
    "ReorgPath",
    "SubmitResult",
    "ReceiveResult",
    "ReorgLedger",
]

DEFAULT_SUBSIDY = 50
MAX_CHAIN_BLOCKS = 100
DEFAULT_ORPHAN_CAPACITY = 128


@dataclass(frozen=True, slots=True)
class ReorgPath:
    """一次重组的路径描述。

    * ``fork_id``       —— 新旧链的最后一个共同块。
    * ``deactivated``   —— 被撤销的旧链区块，顺序为 **旧 tip 向下** 至分叉点之后
      第一个块（即撤销应用顺序）。
    * ``activated``     —— 被应用的新链区块，顺序为分叉点之后第一个块 **向上到
      新 tip**（即应用顺序）。
    """

    fork_id: str
    old_tip: str
    new_tip: str
    deactivated: tuple[str, ...]
    activated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """``submit`` 的结果。区块被拒绝时不返回结果而是抛异常。

    高度与累计工作量字段描述**提交后活跃链 tip**（本块未必成为 tip）。
    """

    block_id: str
    block_height: int
    tip_id: str
    tip_height: int
    cumulative_work: int
    became_tip: bool
    reorganized: bool
    reorg: ReorgPath | None


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """``receive`` 的结果：孤块缓冲语义下的区块接收回执。

    * ``status`` —— ``"connected"``（已验证并接入区块树）、
      ``"buffered"``（父块未知，仅通过结构校验后放入待验证区）、
      ``"duplicate"``（同 ID 同内容幂等重复，无任何状态变更）、
      ``"ancestor_invalid"``（祖先已被证伪，直接标记，不入缓冲）。
    * ``submit`` —— 本块接入时的 :class:`SubmitResult`，未接入为 ``None``。
    * ``connected`` —— 本次调用新接入区块树的块 ID（含本块及被递归
      处理的缓冲后代，后代按块 ID 字典序处理）。
    * ``invalidated`` —— 本次被标为 ``ancestor_invalid`` 的缓冲块 ID。
    * ``evicted`` —— 缓冲满时按接收序被淘汰的整棵待验证子树的块 ID。
    """

    block_id: str
    status: str
    submit: SubmitResult | None
    connected: tuple[str, ...]
    invalidated: tuple[str, ...]
    evicted: tuple[str, ...]


@dataclass(slots=True)
class _PendingEntry:
    block: Block
    seq: int  # 显式接收序（单调递增）


@dataclass(slots=True)
class _Node:
    block: Block
    parent_id: str | None
    height: int
    cumulative_work: int

    @property
    def block_id(self) -> str:
        return self.block.block_id


class _UtxoState:
    """从创世重放过程中的可变 UTXO 状态（内部类型，非公开契约）。"""

    __slots__ = ("utxos", "applied_tx_ids")

    def __init__(self) -> None:
        self.utxos: dict[OutPoint, UTXO] = {}
        self.applied_tx_ids: set[str] = set()

    def copy(self) -> "_UtxoState":
        clone = _UtxoState()
        clone.utxos = self.utxos.copy()
        clone.applied_tx_ids = self.applied_tx_ids.copy()
        return clone

    def commit(self, other: "_UtxoState") -> None:
        self.utxos = other.utxos
        self.applied_tx_ids = other.applied_tx_ids


def _ve(
    reason: str,
    message: str,
    *,
    block: Block | None = None,
    height: int | None = None,
    tx: Transaction | None = None,
    outpoint: OutPoint | None = None,
) -> BlockValidationError:
    return BlockValidationError(
        reason,
        message,
        block_id=block.block_id if block is not None else None,
        height=height,
        tx_id=tx.tx_id if tx is not None else None,
        outpoint=outpoint,
    )


def _apply_block_to_state(
    state: _UtxoState,
    block: Block,
    height: int,
    subsidy: int,
) -> None:
    """在 ``state`` 上**严格按序**应用一个区块的全部交易。

    语义：

    * coinbase（零输入交易）必须位于索引 0 且至多一笔；其输出在索引 0 处
      即刻入账，因此同块后续交易可按序花费（仍受锁定高度约束）。
    * coinbase 的铸币上限 ``subsidy + 手续费`` 在块末结算；超额则整块回滚。
    * 失败时 ``state`` 不被部分修改（所有写入先发生在副本上）。
    """

    work_state = state.copy()
    total_fees = 0
    coinbase: Transaction | None = None

    for index, tx in enumerate(block.transactions):
        if tx.tx_id in work_state.applied_tx_ids:
            raise _ve(
                "duplicate-txid",
                "transaction id already used earlier on this chain",
                block=block, height=height, tx=tx,
            )

        if tx.is_coinbase:
            if coinbase is not None:
                raise _ve(
                    "multiple-coinbase",
                    "a block contains more than one coinbase transaction",
                    block=block, height=height, tx=tx,
                )
            if index != 0:
                raise _ve(
                    "coinbase-position",
                    "a zero-input (coinbase) transaction is only allowed at "
                    "transaction index 0",
                    block=block, height=height, tx=tx,
                )
            coinbase = tx
            for out_index, out in enumerate(tx.outputs):
                op = OutPoint(tx.tx_id, out_index)
                if op in work_state.utxos:
                    raise _ve(
                        "duplicate-txid",
                        "coinbase output point collides with an existing UTXO",
                        block=block, height=height, tx=tx, outpoint=op,
                    )
                work_state.utxos[op] = UTXO(out.amount, out.lock_height)
            work_state.applied_tx_ids.add(tx.tx_id)
            continue

        # --- 普通交易：输入唯一性 / 存在性 / 锁定高度 / 金额守恒 ---
        seen: set[OutPoint] = set()
        input_sum = 0
        resolved: list[OutPoint] = []
        for txin in tx.inputs:
            op = txin.outpoint
            if op in seen:
                raise _ve(
                    "duplicate-input",
                    "input is spent more than once within the same transaction",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            seen.add(op)
            utxo = work_state.utxos.get(op)
            if utxo is None:
                raise _ve(
                    "missing-input",
                    "input references an output that does not exist or is "
                    "already spent",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            if utxo.lock_height > height:
                raise _ve(
                    "output-locked",
                    f"output locked until height {utxo.lock_height} cannot be "
                    f"spent at height {height}",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            input_sum += utxo.amount
            resolved.append(op)

        output_sum = sum(o.amount for o in tx.outputs)
        if output_sum > input_sum:
            raise _ve(
                "output-exceeds-input",
                f"output sum {output_sum} exceeds input sum {input_sum}",
                block=block, height=height, tx=tx,
            )
        total_fees += input_sum - output_sum

        # --- 提交该交易：移除输入、新增输出（严格按序，后者可花前者输出） ---
        for op in resolved:
            del work_state.utxos[op]
        for out_index, out in enumerate(tx.outputs):
            op = OutPoint(tx.tx_id, out_index)
            if op in work_state.utxos:
                raise _ve(
                    "duplicate-txid",
                    "transaction output point collides with an existing UTXO "
                    "(transaction id reused on this chain)",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            work_state.utxos[op] = UTXO(out.amount, out.lock_height)
        work_state.applied_tx_ids.add(tx.tx_id)

    if coinbase is not None:
        minted = sum(o.amount for o in coinbase.outputs)
        allowance = subsidy + total_fees
        if minted > allowance:
            raise _ve(
                "coinbase-overflow",
                f"coinbase mints {minted} but subsidy+fees allowance is "
                f"{allowance} (subsidy={subsidy}, fees={total_fees})",
                block=block, height=height, tx=coinbase,
            )

    state.commit(work_state)


def _validate_block_structure(block: Block) -> None:
    """不依赖 UTXO 的区块结构校验（孤块入缓冲前的唯一验证）。

    只检查不查账本状态即可确定性判定的内容：coinbase 位置与数量、
    块内交易 ID 重复、同一交易内输入重复。金额守恒、双花、锁定高度等
    依赖祖先状态的检查留待接入时进行。
    """
    seen_tx_ids: set[str] = set()
    coinbase_seen = False
    for index, tx in enumerate(block.transactions):
        if tx.tx_id in seen_tx_ids:
            raise _ve(
                "duplicate-txid",
                "transaction id appears more than once within the block",
                block=block, tx=tx,
            )
        seen_tx_ids.add(tx.tx_id)
        if tx.is_coinbase:
            if coinbase_seen:
                raise _ve(
                    "multiple-coinbase",
                    "a block contains more than one coinbase transaction",
                    block=block, tx=tx,
                )
            if index != 0:
                raise _ve(
                    "coinbase-position",
                    "a zero-input (coinbase) transaction is only allowed at "
                    "transaction index 0",
                    block=block, tx=tx,
                )
            coinbase_seen = True
            continue
        seen_inputs: set[OutPoint] = set()
        for txin in tx.inputs:
            op = txin.outpoint
            if op in seen_inputs:
                raise _ve(
                    "duplicate-input",
                    "input is spent more than once within the same transaction",
                    block=block, tx=tx, outpoint=op,
                )
            seen_inputs.add(op)


class ReorgLedger:
    """有界 UTXO 重组账本。

    Parameters
    ----------
    subsidy:
        每个区块 coinbase 的固定补贴（非负整数）。coinbase 总产出不得超过
        ``subsidy + 区块内普通交易手续费之和``。
    max_blocks:
        任意一条链允许的最大块数（含创世），默认 100。超出抛
        :class:`~reorg_ledger.errors.ChainLimitError`。
    orphan_capacity:
        孤块待验证区的最大块数（默认 128）。父块未知的区块仅做结构校验后
        暂存于此；缓冲满时按显式接收序淘汰最早的整棵待验证子树。
    """

    def __init__(
        self,
        subsidy: int = DEFAULT_SUBSIDY,
        max_blocks: int = MAX_CHAIN_BLOCKS,
        orphan_capacity: int = DEFAULT_ORPHAN_CAPACITY,
    ) -> None:
        if isinstance(subsidy, bool):
            raise InputContractError("subsidy: bool is not accepted as an integer")
        if not isinstance(subsidy, int):
            raise InputContractError(
                f"subsidy: expected int, got {type(subsidy).__name__}"
            )
        if subsidy < 0:
            raise InputContractError(f"subsidy: expected value >= 0, got {subsidy}")
        if isinstance(max_blocks, bool):
            raise InputContractError("max_blocks: bool is not accepted as an integer")
        if not isinstance(max_blocks, int):
            raise InputContractError(
                f"max_blocks: expected int, got {type(max_blocks).__name__}"
            )
        if max_blocks < 1:
            raise InputContractError(
                f"max_blocks: expected value >= 1, got {max_blocks}"
            )
        if isinstance(orphan_capacity, bool):
            raise InputContractError(
                "orphan_capacity: bool is not accepted as an integer"
            )
        if not isinstance(orphan_capacity, int):
            raise InputContractError(
                f"orphan_capacity: expected int, got "
                f"{type(orphan_capacity).__name__}"
            )
        if orphan_capacity < 1:
            raise InputContractError(
                f"orphan_capacity: expected value >= 1, got {orphan_capacity}"
            )
        self._subsidy = subsidy
        self._max_blocks = max_blocks
        self._orphan_capacity = orphan_capacity
        self._nodes: dict[str, _Node] = {}
        self._tip_id: str | None = None
        self._state = _UtxoState()
        self._pending: dict[str, _PendingEntry] = {}
        self._pending_children: dict[str, set[str]] = {}
        self._pending_seq = 0
        self._ancestor_invalid: set[str] = set()

    # ------------------------------------------------------------------ 查询

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def max_blocks(self) -> int:
        return self._max_blocks

    @property
    def orphan_capacity(self) -> int:
        return self._orphan_capacity

    @property
    def pending_count(self) -> int:
        """待验证区中暂存的孤块数量。"""
        return len(self._pending)

    def pending_orphans(self) -> tuple[str, ...]:
        """待验证区中的块 ID，按显式接收序排列。"""
        return tuple(
            bid for bid, _ in sorted(
                self._pending.items(), key=lambda kv: kv[1].seq
            )
        )

    def orphan_status(self, block_id: str) -> str | None:
        """孤块状态：``"pending"`` / ``"ancestor_invalid"`` / ``None``（非孤块）。"""
        if block_id in self._pending:
            return "pending"
        if block_id in self._ancestor_invalid:
            return "ancestor_invalid"
        return None

    @property
    def is_empty(self) -> bool:
        return self._tip_id is None

    @property
    def tip_id(self) -> str | None:
        return self._tip_id

    @property
    def tip(self) -> Block | None:
        return None if self._tip_id is None else self._nodes[self._tip_id].block

    @property
    def height(self) -> int:
        """活跃链高度（块数；空树为 0）。"""
        return 0 if self._tip_id is None else self._nodes[self._tip_id].height

    @property
    def cumulative_work(self) -> int:
        return 0 if self._tip_id is None else self._nodes[self._tip_id].cumulative_work

    @property
    def active_path(self) -> tuple[str, ...]:
        """活跃链区块 ID，自创世至 tip。"""
        if self._tip_id is None:
            return ()
        return self._path_to(self._tip_id)

    def __contains__(self, block_id: object) -> bool:
        return isinstance(block_id, str) and block_id in self._nodes

    def contains(self, block_id: str) -> bool:
        return block_id in self._nodes

    def get_block(self, block_id: str) -> Block:
        if not isinstance(block_id, str):
            raise InputContractError(
                f"get_block: block_id must be str, got {type(block_id).__name__}"
            )
        try:
            return self._nodes[block_id].block
        except KeyError:
            raise KeyError(block_id) from None

    def is_active(self, block_id: str) -> bool:
        if block_id not in self._nodes:
            return False
        return block_id in set(self.active_path)

    def snapshot_utxos(self) -> dict[OutPoint, UTXO]:
        """活跃链 tip 处的 UTXO 集合副本（修改返回值不影响账本）。"""
        return self._state.utxos.copy()

    # ------------------------------------------------------------------ 提交

    def submit(self, block: Block) -> SubmitResult:
        """提交一个区块。

        * 父块缺失 → :class:`OrphanBlockError`（既未证明合法也未证明非法）。
        * 超出 100 块界 → :class:`ChainLimitError`（预算耗尽）。
        * 祖先状态下证伪 → :class:`BlockValidationError`（证明无解/非法）。
        * ID 重复 → :class:`DuplicateBlockError`。

        任何失败都不会造成部分变更。成功且新链 work 胜出时原子完成重组。
        """
        if not isinstance(block, Block):
            raise InputContractError(
                f"submit: expected Block, got {type(block).__name__}"
            )

        if block.block_id in self._nodes:
            raise DuplicateBlockError(block.block_id)

        if block.parent_id is None:
            if self._nodes:
                raise _ve(
                    "multiple-genesis",
                    "a parent-less block can only be the first (genesis) block; "
                    "genesis already exists",
                    block=block,
                )
        else:
            if block.parent_id not in self._nodes:
                raise OrphanBlockError(block.block_id, block.parent_id)

        return self._connect(block)

    def _connect(self, block: Block) -> SubmitResult:
        """把父块已在树中的区块验证并接入区块树（``submit`` 的核心段）。

        调用方须保证：区块 ID 未占用、父块存在（或创世情形已判定）。
        失败抛异常且引擎状态零变更；成功时按需原子重组。
        """
        parent_node = (
            None if block.parent_id is None
            else self._nodes[block.parent_id]
        )
        height = 1 if parent_node is None else parent_node.height + 1
        if height > self._max_blocks:
            # 预算/有界性拒绝：不做“合法与否”的判定，与证伪严格分开。
            raise ChainLimitError(block.block_id, height, self._max_blocks)

        candidate = _Node(
            block=block,
            parent_id=block.parent_id,
            height=height,
            cumulative_work=(
                block.work if parent_node is None
                else parent_node.cumulative_work + block.work
            ),
        )

        # 1) 以祖先状态验证：从创世重放父链，再应用本块（纯临时状态）。
        ancestor_state = self._replay_chain(
            () if parent_node is None else self._path_to(parent_node.block_id)
        )
        _apply_block_to_state(ancestor_state, block, height, self._subsidy)

        # 2) 选链：全部已验证节点 + 候选节点，累计 work 降序、ID 字典序升序。
        new_tip_id = self._select_tip(candidate)

        # 3) 需要切换时，先在临时状态重放整条新链（撤销旧链 + 应用新链）。
        new_state: _UtxoState | None = None
        reorg: ReorgPath | None = None
        old_tip_id = self._tip_id
        if new_tip_id != old_tip_id:
            new_state = self._replay_chain(
                self._path_to(new_tip_id, candidate), candidate
            )
            if old_tip_id is not None:
                fork_id, deactivated, activated = self._reorg_paths(
                    old_tip_id, new_tip_id, candidate
                )
                if deactivated:
                    # 同链向前延伸（无撤销块）不是重组。
                    reorg = ReorgPath(
                        fork_id=fork_id,
                        old_tip=old_tip_id,
                        new_tip=new_tip_id,
                        deactivated=deactivated,
                        activated=activated,
                    )

        # 4) 原子提交：以上全部成功后才修改自身状态。
        self._nodes[block.block_id] = candidate
        if new_tip_id != old_tip_id:
            assert new_state is not None
            self._state.commit(new_state)
            self._tip_id = new_tip_id

        return SubmitResult(
            block_id=block.block_id,
            block_height=self._nodes[block.block_id].height,
            tip_id=self._tip_id,  # type: ignore[arg-type]
            tip_height=self._nodes[self._tip_id].height if self._tip_id else 0,
            cumulative_work=(
                self._nodes[self._tip_id].cumulative_work if self._tip_id else 0
            ),
            became_tip=new_tip_id == block.block_id,
            reorganized=reorg is not None,
            reorg=reorg,
        )

    # ------------------------------------------------------------ 孤块缓冲

    def receive(self, block: Block) -> ReceiveResult:
        """接收一个区块（带孤块缓冲语义）。

        与 :meth:`submit` 的区别：父块未知时不抛
        :class:`OrphanBlockError`，而是只做不依赖 UTXO 的结构校验，然后把
        区块放入有上限的待验证区——暂存期间**不计入累计 work 候选、不应用
        任何交易**。父块到达并验证成功后，按块 ID 字典序递归处理可接入的
        缓冲后代。父块（或任一祖先）被证伪时，其缓冲后代标记为
        ``ancestor_invalid``，即使其自身交易单独看起来合法也**不会**接入。

        * 同 ID 同内容重复接收 → 幂等，返回 ``status="duplicate"``；
        * 同 ID 异内容 → :class:`~reorg_ledger.errors.ConflictingBlockError`；
        * 缓冲满 → 按显式接收序淘汰最早的整棵待验证子树，淘汰 ID 经
          ``ReceiveResult.evicted`` 返回；已验证的主链与侧链不受影响。

        任何失败都不会造成部分变更。
        """
        if not isinstance(block, Block):
            raise InputContractError(
                f"receive: expected Block, got {type(block).__name__}"
            )

        # --- 幂等 / 冲突：已接入、已缓冲、已标记三种既往记录 ---
        existing = self._nodes.get(block.block_id)
        if existing is not None:
            if existing.block == block:
                return ReceiveResult(
                    block.block_id, "duplicate", None, (), (), ()
                )
            raise ConflictingBlockError(block.block_id)
        pending_entry = self._pending.get(block.block_id)
        if pending_entry is not None:
            if pending_entry.block == block:
                return ReceiveResult(
                    block.block_id, "duplicate", None, (), (), ()
                )
            raise ConflictingBlockError(block.block_id)
        if block.block_id in self._ancestor_invalid:
            return ReceiveResult(
                block.block_id, "ancestor_invalid", None, (), (), ()
            )

        # --- 不依赖 UTXO 的结构校验（入缓冲前唯一一道验证） ---
        _validate_block_structure(block)

        if block.parent_id is None:
            if self._nodes:
                raise _ve(
                    "multiple-genesis",
                    "a parent-less block can only be the first (genesis) block; "
                    "genesis already exists",
                    block=block,
                )
            result = self._connect(block)
            connected, invalidated = self._drain_pending()
            return ReceiveResult(
                block.block_id, "connected", result,
                (block.block_id,) + connected, invalidated, (),
            )

        if block.parent_id in self._ancestor_invalid:
            # 祖先已被证伪：直接标记，不入缓冲、不验证交易。
            self._ancestor_invalid.add(block.block_id)
            return ReceiveResult(
                block.block_id, "ancestor_invalid", None, (), (), ()
            )

        if block.parent_id in self._nodes:
            try:
                result = self._connect(block)
            except (BlockValidationError, ChainLimitError):
                # 父块/本块被证伪（或超预算）：其缓冲后代标 ancestor_invalid。
                self._mark_descendants_invalid(block.block_id)
                raise
            connected, invalidated = self._drain_pending()
            return ReceiveResult(
                block.block_id, "connected", result,
                (block.block_id,) + connected, invalidated, (),
            )

        # --- 父块未知：入待验证区 ---
        evicted: list[str] = []
        while len(self._pending) >= self._orphan_capacity:
            evicted.extend(self._evict_oldest_subtree())
        self._pending_seq += 1
        self._pending[block.block_id] = _PendingEntry(block, self._pending_seq)
        self._pending_children.setdefault(block.parent_id, set()).add(
            block.block_id
        )
        return ReceiveResult(
            block.block_id, "buffered", None, (), (), tuple(evicted)
        )

    def _drain_pending(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """递归处理可接入的缓冲后代（按块 ID 字典序），返回 (接入ID, 标记ID)。

        每个后代在与 ``submit`` 相同的祖先状态验证下接入；验证失败（证伪或
        超预算）的块及其全部缓冲后代标记为 ``ancestor_invalid``，不中断其余
        无关节点的处理。
        """
        connected: list[str] = []
        invalidated: list[str] = []
        while True:
            ready = [
                bid for bid, entry in self._pending.items()
                if entry.block.parent_id in self._nodes
            ]
            if not ready:
                return tuple(connected), tuple(invalidated)
            for bid in sorted(ready):
                entry = self._pending.pop(bid)
                self._pending_children.get(entry.block.parent_id, set()).discard(
                    bid
                )
                try:
                    self._connect(entry.block)
                except (BlockValidationError, ChainLimitError):
                    invalidated.append(bid)
                    self._ancestor_invalid.add(bid)
                    invalidated.extend(self._mark_descendants_invalid(bid))
                else:
                    connected.append(bid)

    def _mark_descendants_invalid(self, root_id: str) -> list[str]:
        """把 ``root_id`` 的全部缓冲后代标记为 ``ancestor_invalid`` 并移出缓冲。"""
        invalidated: list[str] = []
        stack = sorted(self._pending_children.get(root_id, ()))
        while stack:
            bid = stack.pop()
            entry = self._pending.pop(bid, None)
            if entry is None:
                continue
            self._pending_children.get(entry.block.parent_id, set()).discard(bid)
            self._ancestor_invalid.add(bid)
            invalidated.append(bid)
            stack.extend(sorted(self._pending_children.get(bid, ())))
        return invalidated

    def _evict_oldest_subtree(self) -> list[str]:
        """淘汰显式接收序最早的整棵待验证子树，返回被淘汰的块 ID。

        子树根 = 接收序最早的待验证块沿父链向上（仍在待验证区内）的最高块；
        淘汰只影响待验证区，不触碰已验证的主链与侧链。
        """
        oldest_id = min(
            self._pending, key=lambda bid: self._pending[bid].seq
        )
        root_id = oldest_id
        while True:
            parent_id = self._pending[root_id].block.parent_id
            if parent_id is None or parent_id not in self._pending:
                break
            root_id = parent_id
        evicted: list[tuple[int, str]] = []
        stack = [root_id]
        while stack:
            bid = stack.pop()
            entry = self._pending.pop(bid, None)
            if entry is None:
                continue
            self._pending_children.get(entry.block.parent_id, set()).discard(bid)
            evicted.append((entry.seq, bid))
            stack.extend(sorted(self._pending_children.get(bid, ())))
        evicted.sort()
        return [bid for _, bid in evicted]

    # -------------------------------------------------------------- 内部工具

    def _lookup(self, node_id: str, candidate: _Node | None) -> _Node:
        if candidate is not None and node_id == candidate.block.block_id:
            return candidate
        return self._nodes[node_id]

    def _path_to(
        self, tip_id: str, candidate: _Node | None = None
    ) -> tuple[str, ...]:
        """自创世至 ``tip_id`` 的区块 ID 元组（允许包含尚未提交的候选节点）。"""
        ids: list[str] = []
        current_id: str | None = tip_id
        while current_id is not None:
            node = self._lookup(current_id, candidate)
            ids.append(current_id)
            current_id = node.parent_id
        ids.reverse()
        return tuple(ids)

    def _replay_chain(
        self,
        path_ids: tuple[str, ...],
        candidate: _Node | None = None,
    ) -> _UtxoState:
        """从空状态按给定创世→tip 路径重放，返回全新状态。"""
        state = _UtxoState()
        expected_height = 0
        for node_id in path_ids:
            node = self._lookup(node_id, candidate)
            expected_height += 1
            if node.height != expected_height:
                raise LedgerStateError(
                    f"internal error: height mismatch replaying {node_id!r}"
                )
            _apply_block_to_state(
                state, node.block, node.height, self._subsidy
            )
        return state

    def _select_tip(self, candidate: _Node) -> str:
        """累计 work 最大；同分取区块 ID 字典序较小者。

        候选区块加入前，旧树的活跃 tip 已是旧树最优；候选只能让其自身成为
        新 tip，因此只需与旧 tip 用 ``(累计work 降序, ID 升序)`` 键比较。
        """
        if self._tip_id is None:
            return candidate.block.block_id
        best_id = self._tip_id
        best_node = self._nodes[best_id]
        candidate_key = (-candidate.cumulative_work, candidate.block.block_id)
        best_key = (-best_node.cumulative_work, best_id)
        if candidate_key < best_key:
            return candidate.block.block_id
        return best_id

    def _reorg_paths(
        self,
        old_tip_id: str,
        new_tip_id: str,
        candidate: _Node,
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        old_path = self._path_to(old_tip_id)
        new_path = self._path_to(new_tip_id, candidate)
        old_set = set(old_path)
        fork_index = max(i for i, bid in enumerate(new_path) if bid in old_set)
        fork_id = new_path[fork_index]
        deactivated = tuple(
            bid for bid in reversed(old_path) if bid not in set(new_path)
        )
        activated = tuple(bid for bid in new_path[fork_index + 1:])
        return fork_id, deactivated, activated
