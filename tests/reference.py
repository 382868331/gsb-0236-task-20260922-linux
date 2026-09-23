"""独立参考实现：逐链从创世重放。

本模块是测试专用的“第二实现”，**不导入也不调用** 被测核心
``utxoledger`` 的任何算法，只读取区块数据对象的字段，然后用自己的
朴素逻辑重放。被测核心的期望值来自这里，而不是来自核心自身
（往返一致只能作为补充断言）。

参考实现刻意写得直白（无日志、无撤销、无树），与核心的撤销账本
走不同代码路径，以便互相暴露错误。
"""

from __future__ import annotations

from collections import namedtuple

# 参考实现自己的数据形状（与核心 dataclass 无类型关系）。
RefOut = namedtuple("RefOut", ["value", "lock_height"])


class RefReject(Exception):
    """参考实现判定链 / 交易非法。"""


def _as_chain(block_map: dict, tip: str) -> list:
    """从 tip 沿 parent 回到创世，返回 root -> tip 有序块列表。"""

    chain = []
    cur = tip
    seen = set()
    while True:
        if cur is None:
            break
        if cur not in block_map:
            raise RefReject(f"断链：{cur} 不在区块集合中")
        if cur in seen:
            raise RefReject(f"祖先关系出现环：{cur}")
        seen.add(cur)
        chain.append(block_map[cur])
        cur = block_map[cur].parent
    chain.reverse()
    if chain[0].parent is not None:
        raise RefReject("链顶端不是创世块（parent 非 None）")
    return chain


def replay(block_map: dict, tip: str, subsidy: int):
    """从创世重放到 ``tip``。

    :returns: ``(utxos, used_txids)``，utxos 为
        ``{(txid, index): RefOut(value, lock_height)}``。
    :raises RefReject: 任何契约违例。
    """

    chain = _as_chain(block_map, tip)
    utxos: dict[tuple[str, int], RefOut] = {}
    used: set[str] = set()

    for height, blk in enumerate(chain):
        # 结构规则独立重查一遍。
        if not isinstance(blk.work, int) or isinstance(blk.work, bool) \
                or blk.work <= 0:
            raise RefReject(f"块 {blk.block_id} work 非正整数")
        coinbases = [t for t in blk.transactions if t.is_coinbase]
        if len(coinbases) > 1:
            raise RefReject(f"块 {blk.block_id} 有多个 coinbase")
        if coinbases and blk.transactions[0] is not coinbases[0]:
            raise RefReject(f"块 {blk.block_id} coinbase 不在首位")

        fees = 0
        coin_outpoints: list[tuple[str, int, RefOut]] = []
        block_txids: set[str] = set()

        for tx in blk.transactions:
            if tx.txid in used or tx.txid in block_txids:
                raise RefReject(f"txid 重放：{tx.txid}")
            block_txids.add(tx.txid)
            if tx.is_coinbase:
                if tx.inputs:
                    raise RefReject("coinbase 带输入")
                total = 0
                for i, o in enumerate(tx.outputs):
                    if o.value < 0:
                        raise RefReject("负金额")
                    total += o.value
                # 上限等普通交易手续费算完后再判，先记下。
                for i, o in enumerate(tx.outputs):
                    coin_outpoints.append(
                        (tx.txid, i, RefOut(o.value, o.lock_height)))
                continue

            if not tx.inputs or not tx.outputs:
                raise RefReject(f"普通交易 {tx.txid} 缺输入或输出")
            ins = set()
            tin = 0
            for inp in tx.inputs:
                key = (inp.outpoint.txid, inp.outpoint.output_index)
                if key in ins:
                    raise RefReject(f"交易 {tx.txid} 输入重复")
                ins.add(key)
                if key not in utxos:
                    raise RefReject(
                        f"交易 {tx.txid} 花费不存在的输出 {key}")
                out = utxos[key]
                if height < out.lock_height:
                    raise RefReject(
                        f"交易 {tx.txid} 在高度 {height} 花费锁定至 "
                        f"{out.lock_height} 的输出 {key}")
                tin += out.value
            tout = 0
            for i, o in enumerate(tx.outputs):
                if o.value < 0 or o.lock_height < 0:
                    raise RefReject("负金额或负锁定高度")
                tout += o.value
            if tout > tin:
                raise RefReject(
                    f"交易 {tx.txid} 输出和 {tout} > 输入和 {tin}")
            fees += tin - tout
            # 正式落账。
            for inp in tx.inputs:
                del utxos[(inp.outpoint.txid, inp.outpoint.output_index)]
            for i, o in enumerate(tx.outputs):
                utxos[(tx.txid, i)] = RefOut(o.value, o.lock_height)
            used.add(tx.txid)

        if coinbases:
            cb = coinbases[0]
            claimed = sum(o.value for o in cb.outputs)
            if claimed > subsidy + fees:
                raise RefReject(
                    f"coinbase {cb.txid} 超额：{claimed} > "
                    f"{subsidy}+{fees}")
        for txid, i, out in coin_outpoints:
            utxos[(txid, i)] = out
        for tx in blk.transactions:
            used.add(tx.txid)

    return utxos, used


def best_tip(block_map: dict, subsidy: int, *, allow_invalid: bool = True):
    """独立选出最佳 tip。

    对每个候选块独立重放其整条链；非法链淘汰。
    选择规则：累计 work 最大，同分 block_id 字典序最小。

    :returns: ``(tip, valid_tips)``；``valid_tips`` 为
        ``{tip: (cum_work, utxos)}``。若所有候选链都非法，tip 为 None。
    """

    valid: dict[str, tuple[int, dict]] = {}
    for cand in block_map:
        try:
            chain = _as_chain(block_map, cand)
        except RefReject:
            continue
        work = 0
        for blk in chain:
            work += blk.work
        try:
            utxos, _ = replay(block_map, cand, subsidy)
        except RefReject:
            if not allow_invalid:
                raise
            continue
        valid[cand] = (work, utxos)
    if not valid:
        return None, valid
    tip = min(valid, key=lambda c: (-valid[c][0], c))
    return tip, valid
