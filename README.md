# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链、“撤销旧链 → 应用新链”的原子重组，以及**孤块缓冲**（父块
未知时暂存、父块到达后递归接入）。不验证 PoW，区块 ID 为合成唯一字符串。
核心、测试、演示全部离线运行，无第三方依赖。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## 准确命令

```bash
# 必要测试（37 个，含确定性差分测试与孤块缓冲测试）
python -m unittest discover -s tests -v

# 固定输入演示（真实重组 + 孤块接入 + 实际触发的拒绝/异常边界，实测 < 0.1 秒）
python demo.py
```

仓库内解释器统一为 `./.venv/bin/python`（即
`./.venv/bin/python -m unittest discover -s tests -v`）。
```

## 目录结构

```
reorg_ledger/
  __init__.py     公开接口导出
  models.py       数据结构 + 入口契约校验（类型/取值范围）
  engine.py       核心算法：区块树、祖先状态验证、选链、原子重组
  errors.py       错误语义（异常分类与可定位字段）
tests/
  reference.py    独立参考实现（从创世逐链重放 + 独立选链），不调用被测核心
  test_models.py  入口契约测试
  test_engine.py  场景测试
  test_differential.py  固定种子随机树差分测试（参考实现作预言机）
  test_orphans.py 孤块缓冲测试（暂存/递归接入/ancestor_invalid/淘汰/全排列差分）
demo.py           固定输入演示
```

## 公开数据结构

均为不可变（frozen）数据类，构造时执行**入口契约校验**：

| 类型 | 字段 | 约束 |
|---|---|---|
| `OutPoint` | `tx_id: str`, `index: int` | 非空字符串；`index >= 0` |
| `TxInput` | `prev_tx: str`, `prev_index: int` | 指向一个 UTXO |
| `TxOutput` | `amount: int`, `lock_height: int` | `amount >= 1`；`lock_height >= 0` |
| `UTXO` | `amount: int`, `lock_height: int` | 与输出同构 |
| `Transaction` | `tx_id: str`, `inputs: tuple[TxInput,...]`, `outputs: tuple[TxOutput,...]` | 至少 1 个输出；`inputs` 为空即 **coinbase** |
| `Block` | `block_id: str`, `parent_id: str \| None`, `work: int`, `transactions: tuple[Transaction,...]` | `work >= 1`；至少 1 笔交易；不可自为父；`parent_id=None` 表示创世 |

## 接口

```python
from reorg_ledger import (
    ReorgLedger, Block, Transaction, TxInput, TxOutput,
)

ledger = ReorgLedger(subsidy=50, max_blocks=100, orphan_capacity=128)
result = ledger.submit(block)          # 提交区块（父缺失抛 OrphanBlockError）
receipt = ledger.receive(block)        # 接收区块（父缺失进孤块缓冲，见下文）

ledger.tip_id          # 当前 tip 区块 ID（空树为 None）
ledger.tip             # 当前 tip 的 Block
ledger.height          # 活跃链块数
ledger.cumulative_work # 活跃链累计 work
ledger.active_path     # tuple[str,...]：创世 → tip
ledger.snapshot_utxos()# dict[OutPoint, UTXO]：tip 处 UTXO 的副本
ledger.contains(bid)   # 区块是否在树中（含非活跃侧枝）
ledger.get_block(bid)  # 取块；不存在抛 KeyError
ledger.pending_orphans()    # tuple[str,...]：待验证区块 ID，按接收序
ledger.pending_count        # 待验证区中的孤块数量
ledger.orphan_status(bid)   # "pending" / "ancestor_invalid" / None
ledger.orphan_capacity      # 待验证区容量上限
```

`submit` 返回 `SubmitResult`：

```python
SubmitResult(block_id, block_height, tip_id, tip_height,
             cumulative_work, became_tip, reorganized, reorg)
```

发生重组时 `reorg` 为 `ReorgPath(fork_id, old_tip, new_tip,
deactivated, activated)`：

- `deactivated`：旧 tip 向下至分叉点之后的被撤销块（撤销顺序）
- `activated`：分叉点之后向上至新 tip 的被应用块（应用顺序）
- 同链向前延伸（无撤销块）时 `reorganized=False`、`reorg=None`

`receive` 返回 `ReceiveResult`：

```python
ReceiveResult(block_id, status, submit, connected, invalidated, evicted)
```

- `status`：`"connected"`（已验证接入）/ `"buffered"`（父未知，已暂存）/
  `"duplicate"`（同 ID 同内容幂等）/ `"ancestor_invalid"`（祖先已证伪）
- `connected`：本次新接入的块 ID（含按块 ID 字典序递归接入的缓冲后代）
- `invalidated`：本次被标为 `ancestor_invalid` 的缓冲块 ID
- `evicted`：缓冲满时按接收序淘汰的整棵待验证子树的块 ID

## 孤块缓冲（receive）

`submit` 保持原语义（父缺失抛 `OrphanBlockError`、不缓存孤儿）；`receive`
在其上增加孤块缓冲：

1. **父块未知时只做不依赖 UTXO 的结构校验**（coinbase 位置与数量、块内
   交易 ID 重复、交易内输入重复），通过后放入有上限的待验证区；结构错误
   即使父未知也立即抛 `BlockValidationError`。
2. **暂存期间不计入累计 work 候选、不应用任何交易**：tip、活跃链、UTXO
   完全不受缓冲内容影响。
3. **父块到达并验证成功后**，按块 ID 字典序递归处理可接入的缓冲后代，
   每个后代走与 `submit` 相同的祖先状态验证与原子重组。
4. **父块（或任一祖先）被证伪时**，其全部缓冲后代标记为
   `ancestor_invalid` 并移出待验证区；它们**不能**因为单独交易看起来合法
   而接入，重复接收幂等返回 `"ancestor_invalid"`。
5. **幂等与冲突**：同 ID 同内容重复接收幂等（`status="duplicate"`）；
   同 ID 异内容抛 `ConflictingBlockError`（`DuplicateBlockError` 子类，
   旧的统一捕获不受影响）。
6. **缓冲淘汰**：待验证区满（`orphan_capacity`，默认 128）时，按显式接收
   序淘汰**最早的整棵待验证子树**，淘汰 ID 经 `ReceiveResult.evicted`
   返回；淘汰只影响待验证区，不影响已验证的主链与侧链。被淘汰的块可
   重新接收。

## 协议语义（本题契约子集）

1. **按序交易**：块内交易按顺序应用，后一笔可花前一笔同块输出。
2. **coinbase**：零输入交易，必须位于索引 0 且每块至多一笔；其输出即刻
   入账。块末结算铸币上限：`sum(coinbase 输出金额) <= subsidy + 本块手续费`，
   其中手续费 = Σ(输入金额 − 输出金额)。超额整块拒绝。
3. **普通交易**：输入在同一交易内不可重复；每个输入必须引用一个存在且未花费
   的输出；输出金额之和 ≤ 输入金额之和。
4. **锁定高度**：被引用输出的 `lock_height <= 所在块高度` 时方可花费
   （相等允许，边界即如此定义）。
5. **交易 ID 唯一**：同一链历史中 `tx_id` 不可复用（含跨块）。
6. **选链**：累计 `work`（沿父链求和）最大者为活跃链；累计 work 相同取
   **tip 区块 ID 字典序较小**者。选链结果只取决于区块集合，与提交顺序无关。
7. **侧枝接收**：区块先在“从创世沿父链重放得到的祖先状态”上验证；通过即
   入树（即使未成为 tip），未通过则不入树、账本零变更。
8. **原子重组**：新 tip 产生时，在临时状态上从创世重放整条新链，成功后
   一次性切换 tip 与 UTXO 集合；中途异常保留旧状态（不会留下部分变更）。

## 输入上限

- 单链最多 **100** 个块（含创世，`max_blocks` 可在构造时改小用于测试）；
  第 101 块（任何使链长达到 101 的提交）抛 `ChainLimitError`，**该块既不
  入树也不改变 tip**。这是“搜索/处理预算耗尽”，不是非法判定。
- 金额、work、高度均为 Python 任意精度整数（无溢出），但必须满足各自下界。
- 本实现没有内建“每块交易数”上限；100 块界是唯一的链规模协议边界。

## 错误语义

所有异常继承 `LedgerError`：

| 异常 | 含义 | 关键属性 |
|---|---|---|
| `InputContractError` | 违反接口契约：类型错误、整数越界、结构不符。信息含字段路径 | — |
| `NonFiniteNumberError` | 传入 `NaN`/`±Infinity`（`InputContractError` 子类，单独分类） | — |
| `BlockValidationError` | **证明非法**：区块在祖先状态下被确定性证伪 | `reason` 短码 + `block_id`/`height`/`tx_id`/`outpoint` |
| `OrphanBlockError` | 父块未知；**既未证明合法也未证明非法**，不缓存孤儿 | `block_id`, `parent_id` |
| `ChainLimitError` | **预算耗尽**：链长超过 100，与“非法”严格区分 | `depth`, `limit` |
| `DuplicateBlockError` | 同一区块 ID 重复提交 | `block_id` |
| `ConflictingBlockError` | 同 ID **异内容**重复接收（`DuplicateBlockError` 子类） | `block_id` |

- **整数纯度**：所有整数参数拒绝 `bool`（`True/False` 不当作 1/0），拒绝
  浮点（不做静默取整）；浮点 `NaN/Infinity` 给 `NonFiniteNumberError`，
  有限浮点给 `InputContractError`。
- **`BlockValidationError.reason` 稳定短码**：`duplicate-input`、
  `missing-input`、`output-locked`、`output-exceeds-input`、
  `coinbase-overflow`、`coinbase-position`、`multiple-coinbase`、
  `duplicate-txid`、`multiple-genesis`。
- 失败的 `submit` 不产生未说明的部分变更：区块不入树、tip/UTXO/活跃路径
  保持提交前状态（测试 `test_failed_submit_is_atomic_against_reference`
  等逐一验证）。

## 设计取舍

- **重放式验证而非增量回滚日志**：每次接收与重组都从创世沿链重放到目标状态。
  在 100 块的有界前提下，实现直接、正确性易论证，且“撤销 + 应用”天然原子。
  代价是最坏复杂度约为 O(链长 × 交易数)；未采用全量 undo 日志做 O(1) 增量
  切换，因为题面只要求 100 块界内的正确性与原子性。
- **UTXO 集以 dict 全量快照切换**：重组提交是一次引用替换，旧链状态不残留。
- **侧枝保留**：通过祖先验证的非活跃块留在树中，之后可在其上接续更强链
  触发重组；非法块从不入树。
- **锁定时间只实现绝对高度**一种形式；不支持时间戳、相对锁、脚本/签名。
- **金额单位无小数**：整数最小单位，全链路无浮点，因此**不存在浮点误差口径**
  ——所有余额/手续费比较都是精确整数比较。

## 测试与独立参考

- `tests/reference.py` 是**独立**的从创世重放实现与独立选链函数，只把数据类
  当纯数据读取，不 import 也不调用 `reorg_ledger.engine` 的任何算法。
- 差分测试用固定种子（种子 0–6 等）在仓库代码内确定性生成区块树（线性链、
  分叉、乱序拓扑提交、work 反超），期望值（活跃链、tip、UTXO 集、累计 work、
  重组路径、各非法场景的拒绝短码）全部由独立参考给出；引擎只作为被比对对象。
- 往返一致性（UTXO 快照独立两次取得相等）仅作为补充断言
  （`test_roundtrip_only_as_supplement`），不用于推导期望值。
- 场景覆盖：分支双花（跨块重复花费、交易内重复输入）、coinbase 超额（含
  恰好等于补贴+手续费的通过边界）、锁定高度（`lock == height` 可花、
  `lock > height` 拒绝）、无效侧枝不入树且不污染主链、孤儿块、重复块、
  同分字典序决胜、真实重组与重组回旧链、100 块界、失败原子性、入口契约。
- 孤块缓冲覆盖：孙块先到后递归接入、父块双花导致后代 `ancestor_invalid`、
  两个孤块链竞争、缓冲淘汰顺序与淘汰 ID、幂等/冲突，以及**同一有效区块集
  全排列（5! = 120 种接收顺序）与父先到顺序的最终 tip/UTXO 对照**；每种
  排列的每一步接收后都用独立参考重放核对活跃链状态（重组原子性）。

## demo.py 输出内容

1. **正常结果**：主链 `G→A`（累计 work 12）被高 work 侧枝 `G→B→C`
   （累计 work 50）真实重组；打印 tip 切换、`ReorgPath` 的分叉点/撤销/激活
   序列、重组前后 UTXO 集合差。
2. **实际触发的边界**：在重组后链上提交双花块，真实抛出
   `BlockValidationError(reason='missing-input', block, tx, height, outpoint)`，
   并展示 tip/活跃链零变化、坏块不入树；另真实触发 `ChainLimitError`
   （小块界演示预算语义）与 `InputContractError`（bool 被拒作整数）。
3. **孤块缓冲正常路径**：孙块、子块先于父块到达被暂存（暂存期间 tip 与
   累计 work 不变），父块到达后按块 ID 顺序递归接入，活跃链延伸到孙块。
4. **实际边界**：`orphan_capacity=2` 的缓冲被第三个孤块触发淘汰，按接收序
   淘汰最早整棵待验证子树并打印淘汰 ID，主链 tip 不受影响。

## 已知限制

- 仅实现本题的有限协议子集：无签名/脚本验证、无时间戳与时间锁、无对等网络、
  无持久化、无内存池/费用市场、无难度调整，**不宣称与任何行业完整协议
  （Bitcoin 等）兼容**。
- `submit` 不缓存孤儿块（父块未知直接抛 `OrphanBlockError`）；孤块缓冲
  语义由 `receive` 提供，待验证区为纯内存结构，无持久检查点。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 重放式实现面向 ≤100 块的有界场景；远超该规模时性能不做承诺。
- 区块与交易结构由调用方负责构造，合成 ID 的全局唯一性由提交侧保证；同一 ID
  重复提交会被 `DuplicateBlockError` 拒绝。
