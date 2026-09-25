# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链与“撤销旧链 → 应用新链”的原子重组。不验证 PoW，区块 ID 为
合成唯一字符串。核心、测试、演示全部离线运行，无第三方依赖。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## 准确命令

```bash
# 必要测试（38 个，含孤块缓冲与确定性差分测试）
python -m unittest discover -s tests -v

# 固定输入演示（真实重组 + 实际触发的拒绝/异常边界，实测 < 0.1 秒）
python demo.py
```

## 目录结构

```
reorg_ledger/
  __init__.py     公开接口导出
  models.py       数据结构 + 入口契约校验（类型/取值范围）
  engine.py       核心算法：区块树、孤块缓冲、祖先状态验证、选链、原子重组
  errors.py       错误语义（异常分类与可定位字段）
tests/
  reference.py    独立参考实现（从创世逐链重放 + 独立选链），不调用被测核心
  test_models.py  入口契约测试
  test_engine.py  场景测试
  test_orphans.py 孤块缓冲/连坐/竞争/淘汰/全排列测试
  test_differential.py  固定种子随机树差分测试（参考实现作预言机）
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

ledger = ReorgLedger(subsidy=50, max_blocks=100, max_orphans=32)
result = ledger.submit(block)          # 提交区块（见下文错误语义）

ledger.tip_id          # 当前 tip 区块 ID（空树为 None）
ledger.tip             # 当前 tip 的 Block
ledger.height          # 活跃链块数
ledger.cumulative_work # 活跃链累计 work
ledger.active_path     # tuple[str,...]：创世 → tip
ledger.snapshot_utxos()# dict[OutPoint, UTXO]：tip 处 UTXO 的副本
ledger.contains(bid)   # 区块是否在树中（含非活跃侧枝）
ledger.get_block(bid)  # 取块；不存在抛 KeyError

# --- 孤块缓冲（父块未知的区块暂存于此，见下文“孤块缓冲”一节）---
ledger.max_orphans                 # 缓冲容量（默认 32）
ledger.orphan_count()              # 当前缓冲块数
ledger.orphan_buffer_available()   # 剩余槽位
ledger.buffered_orphan_ids()       # 缓冲 ID，按显式接收序
ledger.is_verified(bid)            # 已通过祖先状态验证并入树（含侧枝）
ledger.is_buffered_orphan(bid)     # 正在缓冲等待祖先
ledger.is_ancestor_invalid(bid)    # 因祖先非法被连坐否决
ledger.invalid_ancestor_of(bid)    # 否决该块的最近无效祖先 ID
```

`submit` 返回 `SubmitResult`：

```python
SubmitResult(block_id, block_height, tip_id, tip_height,
             cumulative_work, became_tip, reorganized, reorg,
             buffered=False, evicted_orphan_ids=())
```

- `buffered=True`：父块未知，区块仅通过不依赖 UTXO 的结构校验并进入缓冲；
  此时 `block_height=0`、`became_tip=False`，tip/UTXO 不变化。
- `evicted_orphan_ids`：本次提交导致缓冲满淘汰时，被淘汰块 ID（接收序，
  含被整棵移除的子树成员）；未发生淘汰为空元组。

发生重组时 `reorg` 为 `ReorgPath(fork_id, old_tip, new_tip,
deactivated, activated)`：

- `deactivated`：旧 tip 向下至分叉点之后的被撤销块（撤销顺序）
- `activated`：分叉点之后向上至新 tip 的被应用块（应用顺序）
- 同链向前延伸（无撤销块）时 `reorganized=False`、`reorg=None`

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

### 孤块缓冲（orphan buffer）

父块未知时，区块**不**直接报错，而是进入有上限的待验证区：

1. **只做不依赖 UTXO 的结构校验**：coinbase 位置/唯一性、交易内重复输入、
   块内交易 ID 与 OutPoint 自撞。输入存在性、锁定高度、金额守恒、跨块 txid
   复用、coinbase 补贴+手续费额度等祖先状态判定一律**推迟**。
2. 缓冲块**不计入累计 work 候选**（不参与选链、不会成为 tip），也**不提前
   应用任何交易**（UTXO 集不变）。
3. **父块到达并验证成功后**，按**块 ID 字典序**递归处理所有可接入后代
   （同一父块的兄弟孤块按 ID 排序，逐层接入）。
4. **父块最终无效**时（自身证伪或其更远祖先证伪），其整棵缓冲后代标记为
   `ancestor_invalid` 并移出缓冲；它们即使单独交易看似合法也不能接入，
   再次提交抛 `AncestorInvalidError`（属性 `block_id`、`ancestor_id`）。
5. **同内容区块幂等**：已验证块或缓冲中的同 ID 同内容区块重复 `submit`，
   返回**同一个** `SubmitResult`，不产生任何状态变化。**同 ID 异内容**抛
   `BlockConflictError`（是 `DuplicateBlockError` 的子类，旧捕获仍生效）。
6. **缓冲有上限**：`max_orphans`（默认 32，构造可配，必须为非负整数）。
   缓冲满时，按**显式接收序**淘汰**最早的整棵待验证子树**（从其父块不在
   缓冲区的根开始，连同其全部缓冲后代一起移除），直到能容纳新区块；被淘汰
   ID 通过本次 `SubmitResult.evicted_orphan_ids`（接收序）返回。淘汰只影响
   缓冲，**不影响已验证的主链与侧链**；被淘汰块之后需要由调用方重新提交。
7. 对同一有效区块集合，无论父先到还是后代先到、无论接收排列如何，最终的
   tip 与 tip 处 UTXO 集合都相同（全排列测试与父先到基线 + 独立参考对照）。

## 输入上限

- 单链最多 **100** 个块（含创世，`max_blocks` 可在构造时改小用于测试）；
  第 101 块（任何使链长达到 101 的提交）抛 `ChainLimitError`，**该块既不
  入树也不改变 tip**。这是“搜索/处理预算耗尽”，不是非法判定。
- 金额、work、高度均为 Python 任意精度整数（无溢出），但必须满足各自下界。
- 本实现没有内建“每块交易数”上限；100 块界是唯一的链规模协议边界。
- 孤块缓冲最多持有 **32** 个区块（``max_orphans`` 可在构造时改小用于测试；0 表示不缓存）；满时按上文规则淘汰最早整棵待验证子树。

## 错误语义

所有异常继承 `LedgerError`：

| 异常 | 含义 | 关键属性 |
|---|---|---|
| `InputContractError` | 违反接口契约：类型错误、整数越界、结构不符。信息含字段路径 | — |
| `NonFiniteNumberError` | 传入 `NaN`/`±Infinity`（`InputContractError` 子类，单独分类） | — |
| `BlockValidationError` | **证明非法**：区块在祖先状态下被确定性证伪 | `reason` 短码 + `block_id`/`height`/`tx_id`/`outpoint` |
| `OrphanBlockError` | 父块未知语义的公开异常类型（保留向后兼容；当前孤块改为缓冲，不再主动抛出） | `block_id`, `parent_id` |
| `AncestorInvalidError` | 某祖先已证明非法，该块被连坐否决（后代交易再看似合法也不接入） | `block_id`, `ancestor_id` |
| `BlockConflictError` | 同 ID 区块以不同内容重复提交（`DuplicateBlockError` 子类）；同内容重提幂等 | `block_id` |
| `ChainLimitError` | **预算耗尽**：链长超过 100，与“非法”严格区分 | `depth`, `limit` |

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
  `lock > height` 拒绝）、无效侧枝不入树且不污染主链、孤块缓冲（孙块先到、
  父块 double spend 连坐 ancestor_invalid、两条孤块链 work 竞争、缓冲满淘汰
  最早整棵子树）、同内容幂等/同 ID 异内容拒绝、有效区块集全部小规模排列与
  父先到基线及独立参考对照（tip + UTXO）、同分字典序决胜、真实重组与重组回
  旧链、100 块界、失败原子性、入口契约。

## demo.py 输出内容

1. **正常结果**：主链 `G→A`（累计 work 12）被高 work 侧枝 `G→B→C`
   （累计 work 50）真实重组；打印 tip 切换、`ReorgPath` 的分叉点/撤销/激活
   序列、重组前后 UTXO 集合差。
0. **孤块缓冲边界**：孙块先于父/祖父到达只缓冲不验证；容量 2 时第三棵
   孤块子树到达，按接收序淘汰最早整棵子树并返回淘汰 ID；父块 coinbase 超额
   时其缓冲后代被标 ancestor_invalid，后代单独重提抛 AncestorInvalidError。
2. **实际触发的边界**：在重组后链上提交双花块，真实抛出
   `BlockValidationError(reason='missing-input', block, tx, height, outpoint)`，
   并展示 tip/活跃链零变化、坏块不入树；另真实触发 `ChainLimitError`
   （小块界演示预算语义）与 `InputContractError`（bool 被拒作整数）。

## 已知限制

- 仅实现本题的有限协议子集：无签名/脚本验证、无时间戳与时间锁、无对等网络、
  无持久化、无内存池/费用市场、无难度调整，**不宣称与任何行业完整协议
  （Bitcoin 等）兼容**。
- 父块未知的区块进入有上限孤块缓冲（默认 32）；缓冲满按显式接收序淘汰最早整棵待验证子树，被淘汰块需由调用方重新提交；不做持久化的孤块重传。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 重放式实现面向 ≤100 块的有界场景；远超该规模时性能不做承诺。
- 区块与交易结构由调用方负责构造；同 ID 同内容重复提交幂等，同 ID 异内容
  提交被 `BlockConflictError` 拒绝。
