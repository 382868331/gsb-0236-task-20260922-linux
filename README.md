# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链与“撤销旧链 → 应用新链”的原子重组；并支持在**显式最终确认
深度**之外保存 UTXO 检查点、裁剪撤销日志（块体交易）、落盘恢复后继续接块，
以及深度重组缺历史时返回稳定状态码 ``requires_history``。不验证 PoW，区块 ID
为合成唯一字符串。核心、测试、演示全部离线运行，无第三方依赖。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## 准确命令

```bash
# 必要测试（69 个，含确定性差分测试与检查点/恢复/损坏拒绝测试）
python -m unittest discover -s tests -v

# 固定输入演示（真实重组 + 检查点落盘恢复 + 实际触发的拒绝/异常边界，实测 < 0.1 秒）
python demo.py
```

## 目录结构

```
reorg_ledger/
  __init__.py     公开接口导出
  models.py       数据结构 + 入口契约校验（类型/取值范围）
  engine.py       核心算法：区块树、祖先状态验证、选链、原子重组、检查点与恢复
  errors.py       错误语义（异常分类与可定位字段）
tests/
  reference.py    独立参考实现（从创世逐链重放 + 独立选链），不调用被测核心
  test_models.py  入口契约测试
  test_engine.py  场景测试
  test_checkpoint.py 最终确认边界/裁剪/恢复/深度重组/损坏检查点测试
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

ledger = ReorgLedger(subsidy=50, max_blocks=100)
result = ledger.submit(block)          # 提交区块（见下文错误语义）

ledger.tip_id          # 当前 tip 区块 ID（空树为 None）
ledger.tip             # 当前 tip 的 Block
ledger.height          # 活跃链块数
ledger.cumulative_work # 活跃链累计 work
ledger.active_path     # tuple[str,...]：创世 → tip（裁剪后下方只给 ID）
ledger.snapshot_utxos()# dict[OutPoint, UTXO]：tip 处 UTXO 的副本
ledger.contains(bid)   # 区块是否在树中（含非活跃侧枝的块头）
ledger.get_block(bid)  # 取块；不存在抛 KeyError；已裁剪抛 PrunedBlockError
```

### 最终确认、检查点与恢复（新增）

```python
# 方式一：构造时配置显式最终确认深度（keyword-only，默认 None = 旧行为）
ledger = ReorgLedger(finalization_depth=D)

# 方式二：不配置自动深度，随时显式最终化到指定确认深度
anchor_id = ledger.finalize(depth)

# 导出 / 落盘 / 恢复（class method，恢复成一个新的 ReorgLedger）
data: bytes = ledger.to_checkpoint_bytes()
ledger.save_checkpoint("ckpt.json")          # 临时文件 + os.replace 原子写
ledger2 = ReorgLedger.from_checkpoint_bytes(data)
ledger2 = ReorgLedger.load_checkpoint("ckpt.json")
ledger2.submit(next_block)                    # 恢复后直接继续接块
```

最终确认与裁剪的精确定义：

- **锚点**：活跃链上确认数**严格大于** D 的最深块，即锚点高度 = tip 高度 − D
  （tip 的确认数为 0）。锚点一旦建立只随 tip 前进**单调向前，从不回退**：
  任何会使最终化块失效的重组（分叉点低于锚点）都被
  `RequiresHistoryError` 拒绝，因此最终化不可被重组撤销。
- **检查点内容**：锚点之后（含锚点交易已应用完）的完整 UTXO 集（含每个输出
  的 `lock_height`）+ 链上已用 `tx_id` 集合 + 全部块头（id/parent/work/高度/
  累计 work）+ 窗口内未裁剪块的完整块体。
- **裁剪**：高度 ≤ 锚点高度的所有节点的块体（交易，即撤销日志）被删除，只留
  块头——**包括同高度被埋的非活跃侧枝**。此后验证/重组只从锚点状态重放窗口。
- **窗口内重组**：分叉点高度 ≥ 锚点高度（即候选链在锚点高度处就是锚点本身
  或其后代）时，锚点 UTXO 足以验证，重组照常原子完成。
- **深度重组缺历史**：候选链在锚点高度处与锚点不一致（分叉点被埋进裁剪历史）
  时，候选**既不能被证明合法也不能被证明非法**，提交抛
  `RequiresHistoryError`（稳定状态码 `err.status == "requires_history"`），
  带 `block_id` / `anchor_id` / `anchor_height` / `fork_id` /
  `missing_height` 定位；父块 id **完全未知**（块头都没有）仍是
  `OrphanBlockError`。
- **恢复**：检查点是带 `sha256` 校验信封的**确定性 JSON**
  （常量 `CHECKPOINT_FORMAT` / `CHECKPOINT_VERSION`）。加载时重算摘要并做
  父链/高度/累计 work/锚点祖先关系/裁剪线/UTXO/重放合法性等结构自洽校验；
  任何篡改、截断、非 JSON、版本或信封不符都抛 `CheckpointFormatError`
  （`reason` ∈ `not-utf8`/`not-json`/`bad-envelope`/`bad-version`/
  `bad-checksum`/`broken-chain`/`bad-structure`），加载失败不返回对象。
- **恢复后**：`subsidy` / `max_blocks` / `finalization_depth` 一并恢复；锚点
  状态（UTXO、已用 txid）恢复，因此旧交易 ID 复用仍被拒、锁定高度仍生效，
  接块、窗口内重组、`max_blocks` 边界与自动锚点推进都继续工作。

新查询属性/方法：`anchor_id`、`anchor_height`、`is_pruned(bid)`。
`finalization_depth` 必须满足 `0 <= finalization_depth < max_blocks`（默认
`None` 不受此限），违反给 `InputContractError`。

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
| `OrphanBlockError` | 父块**完全未知**（块头都没有）；**既未证明合法也未证明非法**，不缓存孤儿 | `block_id`, `parent_id` |
| `RequiresHistoryError` | **深度重组/接块缺历史**：父块块头已知但分叉点被埋在最终确认裁剪线之下，不可判定；需要外部补齐历史后重试。稳定状态码 `"requires_history"` | `status`, `block_id`, `anchor_id`, `anchor_height`, `fork_id`, `missing_height` |
| `PrunedBlockError` | 访问的块体已被最终确认裁剪（块头仍在树中） | `block_id` |
| `CheckpointError` | 检查点操作错误（如未最终化即导出）的基类 | — |
| `CheckpointFormatError` | 检查点字节损坏/截断/信封或版本不符/结构自相矛盾 | `reason` 短码 |
| `ChainLimitError` | **预算耗尽**：链长超过 100，与“非法”严格区分 | `depth`, `limit` |
| `DuplicateBlockError` | 同一区块 ID 重复提交（块体已裁剪时块头仍在，判定不变） | `block_id` |

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

- **检查点之上的重放式验证**：无检查点时从创世、有检查点时从锚点状态沿链
  重放到目标状态。在 100 块的有界前提下，实现直接、正确性易论证，且
  “撤销 + 应用”天然原子。配置最终确认深度后，重放长度被限制在确认窗口内，
  锚点及以下的撤销日志（块体交易）随之裁剪；代价是深度重组（分叉点低于锚点）
  不可判定，必须由调用方补历史——本题明确该情形返回 `requires_history`。
- **检查点保存完整 UTXO 集而非增量 undo 记录**：锚点状态自描述，恢复后无需
  任何被裁历史即可接块；序列化只含块头 + 窗口块体 + 锚点 UTXO/txid 集。
- **UTXO 集以 dict 全量快照切换**：重组提交是一次引用替换，旧链状态不残留。
- **侧枝保留**：通过祖先验证的非活跃块留在树中；被最终确认深度埋掉后其块体
  与主链同高度块一并裁剪（块头保留，用于识别分叉点并给出 `requires_history`
  的定位）；非法块从不入树。
- **持久化只覆盖检查点这一种产物**：原子写（临时文件 + `os.replace` +
  `fsync`）+ sha256 信封防篡改/截断；不提供事件日志或逐条写入持久化。
- **锁定时间只实现绝对高度**一种形式；不支持时间戳、相对锁、脚本/签名。
- **金额单位无小数**：整数最小单位，全链路无浮点，因此**不存在浮点误差口径**
  ——所有余额/手续费比较都是精确整数比较。

## 测试与独立参考

- `tests/reference.py` 是**独立**的从创世重放实现与独立选链函数，只把数据类
  当纯数据读取，不 import 也不调用 `reorg_ledger.engine` 的任何算法。
- 差分测试用固定种子（种子 0–6 等）在仓库代码内确定性生成区块树（线性链、
  分叉、乱序拓扑提交、work 反超），期望值（活跃链、tip、UTXO 集、累计 work、
  重组路径、各非法场景的拒绝短码）全部由独立参考给出；引擎只作为被比对对象。
- 检查点测试自己保留原始 `Block` 对象，因此即使引擎已裁剪块体，仍能用独立
  参考对**包含被裁块的整条链**重放，作为恢复后 tip UTXO 的预言机；检查点保存
  的锚点 UTXO 与参考只重放到锚点高度的结果逐一相等。
- 往返一致性（UTXO 快照独立两次取得相等、检查点字节确定性）仅作为补充断言，
  不用于推导期望值。
- 场景覆盖：分支双花（跨块重复花费、交易内重复输入）、coinbase 超额（含
  恰好等于补贴+手续费的通过边界）、锁定高度（`lock == height` 可花、
  `lock > height` 拒绝，且锁高跨越检查点边界仍生效）、无效侧枝不入树且不污染
  主链、孤儿块、重复块、同分字典序决胜、真实重组与重组回旧链、100 块界、
  失败原子性、入口契约；以及最终确认锚点边界（tip−D 的逐块推进）、同高度被埋
  侧枝裁剪、窗口内/锚点处/锚点之上重组、深度重组 `requires_history`（含低
  work 候选、恢复后场景、原子性）、检查点落盘/字节往返、恢复后接块与独立全链
  重放一致、恢复后旧 txid 拒绝、`max_blocks` 恢复后仍生效、损坏检查点
  （翻转/截断/非 JSON/信封与版本/断链/累计 work 不符/锚点关系矛盾/重复节点/
  活动窗口缺块体/重复 UTXO 与 txid/字段类型错误等）分层拒绝。

## demo.py 输出内容

1. **正常结果**：主链 `G→A`（累计 work 12）被高 work 侧枝 `G→B→C`
   （累计 work 50）真实重组；打印 tip 切换、`ReorgPath` 的分叉点/撤销/激活
   序列、重组前后 UTXO 集合差。
2. **实际触发的边界**：在重组后链上提交双花块，真实抛出
   `BlockValidationError(reason='missing-input', block, tx, height, outpoint)`，
   并展示 tip/活跃链零变化、坏块不入树；另真实触发 `ChainLimitError`
   （小块界演示预算语义）与 `InputContractError`（bool 被拒作整数）。
3. **检查点与恢复**：显式最终确认深度 D=1 下接块，展示锚点推进、块体裁剪与
   `PrunedBlockError`；检查点原子落盘并在**新账本对象**上恢复，恢复后继续接块，
   tip UTXO 与独立全链重放一致并用手算守恒式（50 + 4×50 − 累计手续费 4 = 246）
   核对；真实触发深度重组 `RequiresHistoryError(status='requires_history')`
   （并对照父块完全未知的 `OrphanBlockError`）与翻转字节后的
   `CheckpointFormatError(reason='bad-checksum')`。

## 已知限制

- 仅实现本题的有限协议子集：无签名/脚本验证、无时间戳与时间锁、无对等网络、
  无内存池/费用市场、无难度调整，**不宣称与任何行业完整协议（Bitcoin 等）
  兼容**。
- **最终确认是显式且本地的**：锚点只由调用方给定的确认深度（或显式
  `finalize`）推进，没有难度/多数算力意义上的最终性；锚点之下发生的分叉无法
  在本地判定，按题意返回 `requires_history`，需要调用方自行取得被裁历史块
  （在未裁剪的账本上）重放，本库不内置历史补全/换锚协议。
- 检查点格式为本库自定义的单版本（`CHECKPOINT_VERSION=1`）JSON；不承诺跨版本
  兼容迁移，也不做多文件/增量快照。
- 不缓存孤儿块：父块未知直接抛 `OrphanBlockError`，父块到达后需重新提交子块。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 重放式实现面向 ≤100 块的有界场景（最终确认后只需重放确认窗口）；远超该
  规模时性能不做承诺。
- 区块与交易结构由调用方负责构造，合成 ID 的全局唯一性由提交侧保证；同一 ID
  重复提交会被 `DuplicateBlockError` 拒绝。
