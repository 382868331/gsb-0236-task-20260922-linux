"""核心引擎：有界 UTXO 区块树 + 撤销账本式重组 + 最终确认检查点。

设计要点
--------
1. **区块树**：所有通过祖先状态验证的区块都保留在内存树中（含非活跃侧枝）。
2. **祖先状态验证**：接收区块时，从检查点锚点（无检查点则从创世）沿其父链
   重放到父块，再按序应用本块交易。验证在临时状态上进行，失败时引擎零变更。
3. **选链**：累计 ``work`` 最大；累计 work 相同取 *tip 区块 ID 字典序较小*。
4. **原子重组**：tip 改变时，先在临时状态上重放整条新链（锚点之后部分），
   成功后一次性切换 tip / UTXO / 活跃路径；中途任何异常都保留旧状态。
5. **有界**：链长（含创世）超过 ``max_blocks``（默认 100）抛
   :class:`ChainLimitError`（预算耗尽），区别于 :class:`BlockValidationError`
   （被证明非法）。
6. **最终确认与检查点**（本迭代新增）：显式给定最终确认深度
   ``finalization_depth``（或调用 :meth:`ReorgLedger.finalize`）后，活跃链上
   获得超过该深度确认的块被最终化：保存锚点之后的 UTXO 集与已用交易 ID 集为
   检查点，并裁剪锚点及以下各块的块体（交易，即撤销日志），只保留块头
   （id/parent/work/高度）。此后验证与重组只需从锚点状态重放窗口内的块。
7. **恢复**：检查点序列化为带 sha256 信封的确定性 JSON，可落盘后由
   :meth:`ReorgLedger.load_checkpoint` 恢复，恢复后直接继续接块。
8. **深度重组缺历史**：候选链在锚点高度处与锚点不一致（分叉点被埋在已裁剪
   历史中）时，候选既不能被证明合法也不能被证明非法，抛
   :class:`RequiresHistoryError`（稳定状态码 ``requires_history``），与
   “父块完全未知”的 :class:`OrphanBlockError` 区分。

引擎内全部金额运算均为任意精度整数，不使用浮点。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .errors import (
    BlockValidationError,
    ChainLimitError,
    CheckpointError,
    CheckpointFormatError,
    DuplicateBlockError,
    InputContractError,
    LedgerError,
    LedgerStateError,
    OrphanBlockError,
    PrunedBlockError,
    RequiresHistoryError,
)
from .models import Block, OutPoint, Transaction, TxInput, TxOutput, UTXO

__all__ = [
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "CHECKPOINT_FORMAT",
    "CHECKPOINT_VERSION",
    "ReorgPath",
    "SubmitResult",
    "ReorgLedger",
]

DEFAULT_SUBSIDY = 50
MAX_CHAIN_BLOCKS = 100
CHECKPOINT_FORMAT = "reorg-ledger-checkpoint"
CHECKPOINT_VERSION = 1


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


@dataclass(slots=True)
class _Node:
    """区块树节点。

    最终化裁剪后 ``block`` 为 ``None``：只剩块头信息（id/parent/高度/work/
    累计 work），交易体已被删除。
    """

    block_id: str
    parent_id: str | None
    height: int
    work: int
    cumulative_work: int
    block: Block | None


class _UtxoState:
    """重放过程中的可变 UTXO 状态（内部类型，非公开契约）。"""

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


def _plain_nonneg_int(value: object, path: str) -> int:
    """构造参数的整数纯度契约（拒绝 bool / 浮点 / 其他类型 / 负数）。"""
    if isinstance(value, bool):
        raise InputContractError(
            f"{path}: bool is not accepted as an integer (got {value!r})"
        )
    if not isinstance(value, int):
        raise InputContractError(
            f"{path}: expected int, got {type(value).__name__}"
        )
    if value < 0:
        raise InputContractError(f"{path}: expected value >= 0, got {value}")
    return value


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
    finalization_depth:
        显式最终确认深度 D（非负整数）或 ``None``（默认，完全保持无检查点的
        旧行为）。配置后每次成功提交都会自动最终化：活跃链上确认数严格大于 D
        的最深块成为**检查点锚点**（高度 = tip 高度 − D），锚点及以下块体被
        裁剪，锚点之后的 UTXO 集与已用 txid 集保存为检查点。也可不配置而随时
        显式调用 :meth:`finalize`。
    """

    def __init__(
        self,
        subsidy: int = DEFAULT_SUBSIDY,
        max_blocks: int = MAX_CHAIN_BLOCKS,
        *,
        finalization_depth: int | None = None,
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
        if finalization_depth is not None:
            _plain_nonneg_int(
                finalization_depth, "finalization_depth"
            )
            if finalization_depth >= max_blocks:
                raise InputContractError(
                    f"finalization_depth ({finalization_depth}) must be smaller "
                    f"than max_blocks ({max_blocks})"
                )
        self._subsidy = subsidy
        self._max_blocks = max_blocks
        self._final_depth: int | None = finalization_depth
        self._nodes: dict[str, _Node] = {}
        self._tip_id: str | None = None
        self._state = _UtxoState()
        # 最终确认检查点状态。
        self._anchor_id: str | None = None
        self._anchor_height: int = 0
        self._checkpoint: _UtxoState | None = None
        self._anchor_generation: int = 0

    # ------------------------------------------------------------------ 查询

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def max_blocks(self) -> int:
        return self._max_blocks

    @property
    def finalization_depth(self) -> int | None:
        """配置的显式最终确认深度；``None`` 表示未配置（仅可显式 finalize）。"""
        return self._final_depth

    @property
    def anchor_id(self) -> str | None:
        """当前检查点锚点块 ID；尚未最终化时为 ``None``。"""
        return self._anchor_id

    @property
    def anchor_height(self) -> int:
        """当前检查点锚点高度；尚未最终化时为 0。"""
        return self._anchor_height

    @property
    def is_empty(self) -> bool:
        return self._tip_id is None

    @property
    def tip_id(self) -> str | None:
        return self._tip_id

    @property
    def tip(self) -> Block | None:
        if self._tip_id is None:
            return None
        block = self._nodes[self._tip_id].block
        if block is None:
            # depth=0 最终确认会把 tip 块体也裁剪；块头仍在，接块不受影响。
            raise PrunedBlockError(self._tip_id)
        return block

    @property
    def height(self) -> int:
        """活跃链高度（块数；空树为 0）。"""
        return 0 if self._tip_id is None else self._nodes[self._tip_id].height

    @property
    def cumulative_work(self) -> int:
        return 0 if self._tip_id is None else self._nodes[self._tip_id].cumulative_work

    @property
    def active_path(self) -> tuple[str, ...]:
        """活跃链区块 ID，自创世至 tip（裁剪块只给 ID）。"""
        if self._tip_id is None:
            return ()
        return self._path_to(self._tip_id)

    def __contains__(self, block_id: object) -> bool:
        return isinstance(block_id, str) and block_id in self._nodes

    def contains(self, block_id: str) -> bool:
        return block_id in self._nodes

    def is_pruned(self, block_id: str) -> bool:
        """块体是否已被最终确认裁剪（块头仍在树中）。未知块返回 ``False``。"""
        node = self._nodes.get(block_id)
        return node is not None and node.block is None

    def get_block(self, block_id: str) -> Block:
        if not isinstance(block_id, str):
            raise InputContractError(
                f"get_block: block_id must be str, got {type(block_id).__name__}"
            )
        try:
            node = self._nodes[block_id]
        except KeyError:
            raise KeyError(block_id) from None
        if node.block is None:
            raise PrunedBlockError(block_id)
        return node.block

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
        * 超出块界 → :class:`ChainLimitError`（预算耗尽）。
        * 祖先状态下证伪 → :class:`BlockValidationError`（证明无解/非法）。
        * ID 重复 → :class:`DuplicateBlockError`。
        * 候选链分叉点已被最终确认裁剪 →
          :class:`RequiresHistoryError`（``requires_history``，不可判定）。

        任何失败都不会造成部分变更。成功且新链 work 胜出时原子完成重组；
        配置了 ``finalization_depth`` 时，提交成功后自动推进检查点并裁剪。
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
            parent_node = None
        else:
            parent_node = self._nodes.get(block.parent_id)
            if parent_node is None:
                raise OrphanBlockError(block.block_id, block.parent_id)

        height = 1 if parent_node is None else parent_node.height + 1
        if height > self._max_blocks:
            # 预算/有界性拒绝：不做“合法与否”的判定，与证伪严格分开。
            raise ChainLimitError(block.block_id, height, self._max_blocks)

        candidate = _Node(
            block_id=block.block_id,
            parent_id=block.parent_id,
            height=height,
            work=block.work,
            cumulative_work=(
                block.work if parent_node is None
                else parent_node.cumulative_work + block.work
            ),
            block=block,
        )

        # 1) 以祖先状态验证：从检查点锚点（或创世）重放父链，再应用本块。
        candidate_path = self._path_to(block.block_id, candidate)
        suffix, base_state, base_height = self._resolution_base(
            candidate_path, candidate
        )
        ancestor_state = self._replay_suffix(
            suffix[:-1], candidate, base_state, base_height
        )
        _apply_block_to_state(ancestor_state, block, height, self._subsidy)

        # 2) 选链：全部已验证节点 + 候选节点，累计 work 降序、ID 字典序升序。
        new_tip_id = self._select_tip(candidate)

        # 3) 需要切换时，在临时状态重放整条新链（撤销旧链 + 应用新链）。
        new_state: _UtxoState | None = None
        reorg: ReorgPath | None = None
        old_tip_id = self._tip_id
        if new_tip_id != old_tip_id:
            new_path = self._path_to(new_tip_id, candidate)
            new_suffix, new_base, new_base_h = self._resolution_base(
                new_path, candidate
            )
            new_state = self._replay_suffix(
                new_suffix, candidate, new_base, new_base_h
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

        result = SubmitResult(
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

        # 5) 自动最终确认（仅推进，从不回退；失败不应发生——状态刚验证过）。
        if self._final_depth is not None:
            self._advance_finalization(self._final_depth)

        return result

    # ------------------------------------------------------- 最终确认/检查点

    def finalize(self, depth: int) -> str:
        """显式把活跃链最终确认到给定深度并裁剪撤销日志。

        ``depth`` 为非负整数：锚点高度 = tip 高度 − depth（锚点块的确认数
        严格大于 depth）。若账本已经最终化到同样或更深的位置，本次为空操作并
        返回当前锚点 ID。深度 0 表示连 tip 本身也最终化（此后任何重组都需要
        外部历史）。
        """
        d = _plain_nonneg_int(depth, "finalize.depth")
        if self._tip_id is None:
            raise CheckpointError("cannot finalize an empty ledger")
        if d >= self._max_blocks:
            raise InputContractError(
                f"finalize.depth ({d}) must be smaller than max_blocks "
                f"({self._max_blocks})"
            )
        anchor = self._advance_finalization(d)
        if anchor is None:
            raise CheckpointError(
                f"no block has more than {d} confirmation(s): active chain is "
                f"only {self.height} block(s) tall"
            )
        return anchor

    def _advance_finalization(self, depth: int) -> str | None:
        """把锚点推进到 ``tip 高度 − depth``（单调向前），裁剪被埋块体。"""
        assert self._tip_id is not None
        tip_node = self._nodes[self._tip_id]
        target_height = tip_node.height - depth
        if target_height < 1:
            # 还没有任何块获得严格大于 depth 的确认：无可最终化块。
            return self._anchor_id
        if self._anchor_id is not None and target_height <= self._anchor_height:
            # 已经最终化到同样或更深：空操作。
            return self._anchor_id

        active = self.active_path
        anchor_id = active[target_height - 1]
        if self._anchor_id is None:
            suffix_ids = active[:target_height]
            base_state = _UtxoState()
            base_height = 0
        else:
            suffix_ids = active[self._anchor_height:target_height]
            assert self._checkpoint is not None
            base_state = self._checkpoint.copy()
            base_height = self._anchor_height
        new_checkpoint = self._replay_suffix(
            suffix_ids, None, base_state, base_height
        )

        # 裁剪：所有高度不超过锚点的块只保留块头（含被埋的侧枝）。
        for node in self._nodes.values():
            if node.height <= target_height:
                node.block = None

        self._checkpoint = new_checkpoint
        self._anchor_id = anchor_id
        self._anchor_height = target_height
        self._anchor_generation += 1
        return anchor_id

    def to_checkpoint_bytes(self) -> bytes:
        """把当前检查点序列化为带 sha256 校验信封的确定性 JSON 字节。

        必须至少最终化过一次（存在锚点），否则抛 :class:`CheckpointError`。
        """
        payload = self._checkpoint_payload()
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        envelope = {
            "format": CHECKPOINT_FORMAT,
            "version": CHECKPOINT_VERSION,
            "data": json.loads(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        return json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )

    def save_checkpoint(self, path: str | os.PathLike[str]) -> None:
        """把检查点原子写入 ``path``（临时文件 + ``os.replace``）。"""
        if not isinstance(path, (str, os.PathLike)):
            raise InputContractError(
                f"save_checkpoint: path must be str or os.PathLike, got "
                f"{type(path).__name__}"
            )
        data = self.to_checkpoint_bytes()
        tmp_path = f"{os.fspath(path)}.tmp"
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, os.fspath(path))
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @classmethod
    def from_checkpoint_bytes(cls, data: bytes) -> "ReorgLedger":
        """从检查点字节恢复账本；任何损坏都抛 :class:`CheckpointFormatError`。

        校验失败时不会构造出半初始化对象。
        """
        if isinstance(data, str):
            raise InputContractError(
                "from_checkpoint_bytes: expected bytes, got str "
                "(encode it first)"
            )
        if not isinstance(data, (bytes, bytearray)):
            raise InputContractError(
                f"from_checkpoint_bytes: expected bytes, got "
                f"{type(data).__name__}"
            )
        payload = cls._parse_and_verify_envelope(bytes(data))
        return cls._ledger_from_payload(payload)

    @classmethod
    def load_checkpoint(cls, path: str | os.PathLike[str]) -> "ReorgLedger":
        """从 ``path`` 读取检查点并恢复（文件不存在时抛 ``FileNotFoundError``）。"""
        if not isinstance(path, (str, os.PathLike)):
            raise InputContractError(
                f"load_checkpoint: path must be str or os.PathLike, got "
                f"{type(path).__name__}"
            )
        with open(os.fspath(path), "rb") as fh:
            data = fh.read()
        return cls.from_checkpoint_bytes(data)

    def _checkpoint_payload(self) -> dict:
        if self._anchor_id is None or self._checkpoint is None:
            raise CheckpointError(
                "no finalized anchor to save (call finalize(depth) first or "
                "construct with finalization_depth)"
            )
        nodes = []
        for node_id in sorted(self._nodes, key=lambda b: (self._nodes[b].height, b)):
            node = self._nodes[node_id]
            entry: dict = {
                "block_id": node.block_id,
                "parent_id": node.parent_id,
                "height": node.height,
                "work": node.work,
                "cumulative_work": node.cumulative_work,
            }
            if node.block is not None:
                entry["block"] = _block_to_json(node.block)
            nodes.append(entry)
        utxos = [
            [op.tx_id, op.index, utxo.amount, utxo.lock_height]
            for op, utxo in sorted(
                self._checkpoint.utxos.items(),
                key=lambda kv: (kv[0].tx_id, kv[0].index),
            )
        ]
        return {
            "subsidy": self._subsidy,
            "max_blocks": self._max_blocks,
            "finalization_depth": self._final_depth,
            "anchor_id": self._anchor_id,
            "anchor_height": self._anchor_height,
            "anchor_generation": self._anchor_generation,
            "tip_id": self._tip_id,
            "applied_tx_ids": sorted(self._checkpoint.applied_tx_ids),
            "utxos": utxos,
            "nodes": nodes,
        }

    @staticmethod
    def _parse_and_verify_envelope(data: bytes) -> dict:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointFormatError(
                "checkpoint bytes are not valid UTF-8", reason="not-utf8"
            ) from exc
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CheckpointFormatError(
                f"checkpoint is not valid JSON at line {exc.lineno} col "
                f"{exc.colno}",
                reason="not-json",
            ) from exc
        if not isinstance(envelope, dict):
            raise CheckpointFormatError(
                "top-level checkpoint value must be an object",
                reason="bad-envelope",
            )
        if envelope.get("format") != CHECKPOINT_FORMAT:
            raise CheckpointFormatError(
                f"unexpected format {envelope.get('format')!r}; expected "
                f"{CHECKPOINT_FORMAT!r}",
                reason="bad-envelope",
            )
        if envelope.get("version") != CHECKPOINT_VERSION:
            raise CheckpointFormatError(
                f"unsupported checkpoint version {envelope.get('version')!r}; "
                f"expected {CHECKPOINT_VERSION}",
                reason="bad-version",
            )
        payload = envelope.get("data")
        digest = envelope.get("sha256")
        if not isinstance(payload, dict):
            raise CheckpointFormatError(
                "envelope.data must be an object", reason="bad-envelope"
            )
        if not isinstance(digest, str) or len(digest) != 64:
            raise CheckpointFormatError(
                "envelope.sha256 must be a 64-char hex string",
                reason="bad-checksum",
            )
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()
        if actual != digest.lower():
            raise CheckpointFormatError(
                "checksum mismatch: checkpoint bytes were modified, truncated "
                "or written by a different implementation",
                reason="bad-checksum",
            )
        return payload

    @classmethod
    def _ledger_from_payload(cls, payload: dict) -> "ReorgLedger":
        """在局部对象上重建并做结构自洽校验，全部通过才返回。"""
        try:
            subsidy = _require_int(payload, "subsidy", min_value=0)
            max_blocks = _require_int(payload, "max_blocks", min_value=1)
            fd_raw = payload.get("finalization_depth")
            if fd_raw is not None:
                final_depth = _require_int(payload, "finalization_depth",
                                          min_value=0)
                if final_depth >= max_blocks:
                    raise CheckpointFormatError(
                        "finalization_depth must be smaller than max_blocks",
                        reason="bad-structure",
                    )
            else:
                final_depth = None
            anchor_id = _require_str(payload, "anchor_id")
            anchor_height = _require_int(payload, "anchor_height", min_value=1)
            tip_id = _require_str(payload, "tip_id")
            generation = _require_int(payload, "anchor_generation", min_value=1)

            raw_nodes = payload.get("nodes")
            if not isinstance(raw_nodes, list):
                raise CheckpointFormatError(
                    "nodes must be a list", reason="bad-structure"
                )

            ledger = cls(
                subsidy=subsidy,
                max_blocks=max_blocks,
                finalization_depth=final_depth,
            )

            nodes: dict[str, _Node] = {}
            genesis_count = 0
            for entry in raw_nodes:
                if not isinstance(entry, dict):
                    raise CheckpointFormatError(
                        "each node must be an object", reason="bad-structure"
                    )
                node_id = _require_str(entry, "block_id")
                if node_id in nodes:
                    raise CheckpointFormatError(
                        f"duplicate node {node_id!r} in checkpoint",
                        reason="bad-structure",
                    )
                parent_id = entry.get("parent_id")
                if parent_id is not None and not isinstance(parent_id, str):
                    raise CheckpointFormatError(
                        f"node {node_id!r}: parent_id must be str or null",
                        reason="bad-structure",
                    )
                h = _require_int(entry, "height", min_value=1)
                work = _require_int(entry, "work", min_value=1)
                saved_cw = _require_int(entry, "cumulative_work", min_value=1)
                body = entry.get("block", None)
                if body is not None:
                    block = _block_from_json(
                        body, node_id, parent_id, work
                    )
                else:
                    block = None
                if parent_id is None:
                    genesis_count += 1
                    if h != 1:
                        raise CheckpointFormatError(
                            f"genesis {node_id!r} has height {h}, expected 1",
                            reason="bad-structure",
                        )
                nodes[node_id] = _Node(
                    block_id=node_id,
                    parent_id=parent_id,
                    height=h,
                    work=work,
                    cumulative_work=saved_cw,
                    block=block,
                )

            if genesis_count != 1:
                raise CheckpointFormatError(
                    f"checkpoint must contain exactly one genesis, found "
                    f"{genesis_count}",
                    reason="bad-structure",
                )
            if tip_id not in nodes:
                raise CheckpointFormatError(
                    f"tip {tip_id!r} is not among checkpoint nodes",
                    reason="bad-structure",
                )
            if anchor_id not in nodes:
                raise CheckpointFormatError(
                    f"anchor {anchor_id!r} is not among checkpoint nodes",
                    reason="bad-structure",
                )

            # 父链/高度/累计 work 自洽（块头足以校验，无需已裁剪历史）。
            for node in nodes.values():
                if node.parent_id is None:
                    expected_cw = node.work
                else:
                    parent = nodes.get(node.parent_id)
                    if parent is None:
                        raise CheckpointFormatError(
                            f"node {node.block_id!r} references missing parent "
                            f"{node.parent_id!r}",
                            reason="broken-chain",
                        )
                    if node.height != parent.height + 1:
                        raise CheckpointFormatError(
                            f"node {node.block_id!r} height {node.height} does "
                            f"not not follow parent height {parent.height}",
                            reason="bad-structure",
                        )
                    expected_cw = parent.cumulative_work + node.work
                if expected_cw != node.cumulative_work:
                    raise CheckpointFormatError(
                        f"node {node.block_id!r} cumulative_work "
                        f"{node.cumulative_work} disagrees with parent-chain sum "
                        f"{expected_cw}",
                        reason="bad-structure",
                    )

            anchor_node = nodes[anchor_id]
            if anchor_node.height != anchor_height:
                raise CheckpointFormatError(
                    f"anchor height field {anchor_height} disagrees with node "
                    f"height {anchor_node.height}",
                    reason="bad-structure",
                )

            # 裁剪线一致性：高度 ≤ 锚点的节点必须只剩块头；锚点之上的节点
            # （含非活跃侧枝）必须保留块体。
            for node in nodes.values():
                if node.height <= anchor_height:
                    if node.block is not None:
                        raise CheckpointFormatError(
                            f"node {node.block_id!r} at/below anchor must be "
                            f"pruned",
                            reason="bad-structure",
                        )
                elif node.block is None:
                    raise CheckpointFormatError(
                        f"node {node.block_id!r} above anchor is missing its "
                        f"block body",
                        reason="bad-structure",
                    )

            # 锚点必须在 tip 的祖先链上，且位于记录的锚点高度。
            tip_path = _header_path(nodes, tip_id)
            if tip_path[anchor_height - 1] != anchor_id:
                raise CheckpointFormatError(
                    f"anchor {anchor_id!r} is not an ancestor of tip {tip_id!r} "
                    f"at height {anchor_height}",
                    reason="bad-structure",
                )

            # 保存的 tip 必须是当前块头集合下的选链结果（累计 work 降序、
            # 区块 ID 升序），防止绕过选链语义的状态伪造。
            best_tip = min(
                nodes,
                key=lambda nid: (-nodes[nid].cumulative_work, nid),
            )
            if best_tip != tip_id:
                raise CheckpointFormatError(
                    f"saved tip {tip_id!r} is not the selected tip; "
                    f"expected {best_tip!r}",
                    reason="bad-structure",
                )

            # 重建锚点 UTXO/txid 检查点状态。
            utxo_state = _UtxoState()
            raw_utxos = payload.get("utxos")
            if not isinstance(raw_utxos, list):
                raise CheckpointFormatError(
                    "utxos must be a list", reason="bad-structure"
                )
            for item in raw_utxos:
                if (not isinstance(item, list) or len(item) != 4
                        or not all(isinstance(v, int) and not isinstance(v, bool)
                                   for v in item[1:])):
                    raise CheckpointFormatError(
                        "each utxo entry must be [tx_id, index, amount, "
                        "lock_height] of ints",
                        reason="bad-structure",
                    )
                txid, index, amount, lock = item
                if not isinstance(txid, str) or not txid:
                    raise CheckpointFormatError(
                        "utxo tx_id must be a non-empty str",
                        reason="bad-structure",
                    )
                op = OutPoint(txid, index)
                if op in utxo_state.utxos:
                    raise CheckpointFormatError(
                        f"duplicate checkpoint UTXO {op}", reason="bad-structure"
                    )
                utxo_state.utxos[op] = UTXO(amount, lock)

            raw_txids = payload.get("applied_tx_ids")
            if not isinstance(raw_txids, list) or not all(
                isinstance(t, str) and t for t in raw_txids
            ):
                raise CheckpointFormatError(
                    "applied_tx_ids must be a list of non-empty strings",
                    reason="bad-structure",
                )
            if len(set(raw_txids)) != len(raw_txids):
                raise CheckpointFormatError(
                    "applied_tx_ids contains duplicates", reason="bad-structure"
                )
            utxo_state.applied_tx_ids = set(raw_txids)

            # 挂载到新账本：先确认活跃窗口块体齐全，再用保留的活跃链窗口
            # 重放，tip 状态必须能由锚点状态确定地推出（结构自洽最后一道校验）。
            ledger._nodes = nodes
            ledger._tip_id = tip_id
            ledger._anchor_id = anchor_id
            ledger._anchor_height = anchor_height
            ledger._checkpoint = utxo_state
            ledger._anchor_generation = generation

            suffix = tip_path[anchor_height:]
            for node_id in suffix:
                if nodes[node_id].block is None:
                    raise CheckpointFormatError(
                        f"active block {node_id!r} above anchor lacks its body",
                        reason="bad-structure",
                    )

            tip_state = ledger._replay_suffix(
                suffix, None, utxo_state.copy(), anchor_height
            )
            ledger._state = tip_state
            return ledger
        except CheckpointFormatError:
            raise
        except (LedgerError, KeyError, ValueError, TypeError) as exc:
            # 数据类契约错误、JSON 形状错误等统一归类为损坏检查点。
            raise CheckpointFormatError(
                f"checkpoint content failed structural validation: {exc}"
            ) from exc

    # -------------------------------------------------------------- 内部工具

    def _lookup(self, node_id: str, candidate: _Node | None) -> _Node:
        if candidate is not None and node_id == candidate.block_id:
            return candidate
        return self._nodes[node_id]

    def _path_to(
        self,
        tip_id: str,
        candidate: _Node | None = None,
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

    def _resolution_base(
        self,
        path_ids: tuple[str, ...],
        candidate: _Node | None,
    ) -> tuple[tuple[str, ...], _UtxoState, int]:
        """给定创世→目标路径，返回 (需重放的锚点后后缀, 起始状态, 起始高度)。

        若路径在锚点高度处与检查点锚点不一致，说明分叉点被埋在已裁剪历史中，
        抛 :class:`RequiresHistoryError`。
        """
        if self._anchor_id is None:
            return path_ids, _UtxoState(), 0
        ah = self._anchor_height
        if len(path_ids) > ah and path_ids[ah - 1] == self._anchor_id:
            assert self._checkpoint is not None
            return path_ids[ah:], self._checkpoint.copy(), ah
        # 深分叉：用块头定位最后一个共同块（分叉点）。
        active = self.active_path
        fork_id: str | None = None
        shared = min(len(path_ids), len(active))
        for i in range(shared):
            if path_ids[i] == active[i]:
                fork_id = path_ids[i]
            else:
                break
        missing_height: int | None = None
        if fork_id is not None:
            fork_index = next(
                i for i in range(shared)
                if path_ids[i] == fork_id
            )
            if fork_index + 1 < len(path_ids):
                diverging = path_ids[fork_index + 1]
                missing_height = self._lookup(diverging, candidate).height
        raise RequiresHistoryError(
            block_id=path_ids[-1],
            anchor_id=self._anchor_id,
            anchor_height=ah,
            fork_id=fork_id,
            missing_height=missing_height,
        )

    def _replay_suffix(
        self,
        suffix_ids: tuple[str, ...],
        candidate: _Node | None,
        base_state: _UtxoState,
        base_height: int,
    ) -> _UtxoState:
        """从 ``base_state``（锚点高度 ``base_height`` 之后状态）重放后缀。"""
        state = base_state
        expected_height = base_height
        for node_id in suffix_ids:
            node = self._lookup(node_id, candidate)
            expected_height += 1
            if node.height != expected_height:
                raise LedgerStateError(
                    f"internal error: height mismatch replaying {node_id!r}"
                )
            if node.block is None:
                # 活动重放撞上只剩块头的被埋块：需要外部历史。
                raise RequiresHistoryError(
                    block_id=node_id,
                    anchor_id=self._anchor_id,
                    anchor_height=self._anchor_height,
                    fork_id=self._anchor_id,
                    missing_height=node.height,
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
            return candidate.block_id
        best_id = self._tip_id
        best_node = self._nodes[best_id]
        candidate_key = (-candidate.cumulative_work, candidate.block_id)
        best_key = (-best_node.cumulative_work, best_id)
        if candidate_key < best_key:
            return candidate.block_id
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


# ---------------------------------------------------------- 检查点 JSON 工具

def _block_to_json(block: Block) -> dict:
    txs = []
    for tx in block.transactions:
        txs.append({
            "tx_id": tx.tx_id,
            "inputs": [[tin.prev_tx, tin.prev_index] for tin in tx.inputs],
            "outputs": [[out.amount, out.lock_height] for out in tx.outputs],
        })
    return {
        "block_id": block.block_id,
        "parent_id": block.parent_id,
        "work": block.work,
        "transactions": txs,
    }


def _block_from_json(
    body: dict,
    expected_id: str,
    body_parent_expected: str | None,
    body_work_expected: int,
) -> Block:
    if not isinstance(body, dict):
        raise CheckpointFormatError("block body must be an object",
                                    reason="bad-structure")
    bid = body.get("block_id")
    if bid != expected_id:
        raise CheckpointFormatError(
            f"block body id {bid!r} disagrees with node id {expected_id!r}",
            reason="bad-structure",
        )
    if body.get("parent_id") != body_parent_expected:
        raise CheckpointFormatError(
            f"block {bid!r} body parent_id {body.get('parent_id')!r} "
            f"disagrees with header parent_id {body_parent_expected!r}",
            reason="bad-structure",
        )
    body_work = body.get("work")
    if isinstance(body_work, bool) or not isinstance(body_work, int) \
            or body_work < 1:
        raise CheckpointFormatError(
            f"block {bid!r} body work must be a positive int",
            reason="bad-structure",
        )
    if body_work != body_work_expected:
        raise CheckpointFormatError(
            f"block {bid!r} body work {body_work} disagrees with header "
            f"work {body_work_expected}",
            reason="bad-structure",
        )
    raw_txs = body.get("transactions")
    if not isinstance(raw_txs, list) or not raw_txs:
        raise CheckpointFormatError(
            f"block {bid!r}: transactions must be a non-empty list",
            reason="bad-structure",
        )
    txs = []
    for raw_tx in raw_txs:
        if not isinstance(raw_tx, dict):
            raise CheckpointFormatError(
                f"block {bid!r}: each transaction must be an object",
                reason="bad-structure",
            )
        tx_id = raw_tx.get("tx_id")
        if not isinstance(tx_id, str) or not tx_id:
            raise CheckpointFormatError(
                f"block {bid!r}: tx_id must be a non-empty string",
                reason="bad-structure",
            )
        raw_ins = raw_tx.get("inputs")
        raw_outs = raw_tx.get("outputs")
        if not isinstance(raw_ins, list) or not isinstance(raw_outs, list):
            raise CheckpointFormatError(
                f"tx {tx_id!r}: inputs/outputs must be lists",
                reason="bad-structure",
            )
        inputs = []
        for pair in raw_ins:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not isinstance(pair[0], str)):
                raise CheckpointFormatError(
                    f"tx {tx_id!r}: each input must be [prev_tx, prev_index]",
                    reason="bad-structure",
                )
            inputs.append(TxInput(pair[0], pair[1]))
        outputs = []
        for pair in raw_outs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise CheckpointFormatError(
                    f"tx {tx_id!r}: each output must be [amount, lock_height]",
                    reason="bad-structure",
                )
            outputs.append(TxOutput(pair[0], pair[1]))
        txs.append(Transaction(tx_id, tuple(inputs), tuple(outputs)))
    return Block(bid, body.get("parent_id"), body.get("work"), tuple(txs))


def _header_path(nodes: dict[str, _Node], tip_id: str) -> tuple[str, ...]:
    """仅凭块头自创世列路径；断链抛 bad-structure。"""
    ids: list[str] = []
    current: str | None = tip_id
    seen: set[str] = set()
    while current is not None:
        if current in seen or current not in nodes:
            raise CheckpointFormatError(
                f"header chain through {current!r} is broken",
                reason="broken-chain",
            )
        seen.add(current)
        ids.append(current)
        current = nodes[current].parent_id
    ids.reverse()
    return tuple(ids)


def _require_int(
    obj: dict,
    key: str,
    *,
    min_value: int | None = None,
) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckpointFormatError(
            f"field {key!r} must be an int, got {type(value).__name__}",
            reason="bad-structure",
        )
    if min_value is not None and value < min_value:
        raise CheckpointFormatError(
            f"field {key!r} must be >= {min_value}, got {value}",
            reason="bad-structure",
        )
    return value


def _require_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise CheckpointFormatError(
            f"field {key!r} must be a non-empty string",
            reason="bad-structure",
        )
    return value
