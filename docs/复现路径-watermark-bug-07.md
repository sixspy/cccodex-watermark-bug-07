# 复现路径 · watermark-bug-07（差额台作废批读数）

> 本纸供二验独立照做。全部走本机，不需要外网。

## 0. 起服务

```bash
cd /workspace
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
rm -f /tmp/gap_repro.db
DATABASE_PATH=/tmp/gap_repro.db PORT=7013 python run.py
```

打开 http://127.0.0.1:7013/login ，`admin / 123456`。

种子数据（业务日 2026-11-14）：

| 批次 | 状态 |
|---|---|
| ord-20261114-01 | 待盖印 |
| pay-20261114-01 | 待盖印 |
| gmv-20261114-01 | 待盖印 |
| ord-20261113-01 | 已盖印（WM-ORD-131） |

## 1. 旧症状复现（改造前才看得到）

改造前 `app/services/gaps.py` 用 `gap_reports` 行数奇偶决定作废批是否计入：
连点两次“生成对账”，同一批会先被算进去、再被踢出去，摘要跟着漂。
现在这条路径已不存在，下面给的是修复后的对照路径。

## 2. 修复后的两条读数

1. 底栏点「缺口」→ 页面打开即冻结作废集合快照，差额条显示
   `冻结读数 · 快照#1`、作废 0 批。
2. 「批次」页把 `ord-20261114-01` 作废（原因随便填，如 `二验作废`）。
3. 回「缺口」页，差额条顶部提示：最新对账单与本页快照的作废集合相差
   `ord-20261114-01`。
4. 读数选 **冻结** → 「生成差额数」：
   - 摘要形如 `截至 2026-11-16：应盖印 4，已盖印 1，缺口 3（作废 0 批，读数 冻结·打开页快照#1，序 1）`
   - 作废批**没进**差额，缺口 3。
5. 读数选 **重扫** → 「生成差额数」：
   - 摘要形如 `…应盖印 3，已盖印 1，缺口 2（作废 1 批，读数 重扫·提交瞬间重扫，序 2）`
   - 作废批**进了**差额，缺口 2。
6. 再点一次「重扫」，序号变 3、摘要变 3，前一份序 2 的摘要原样还在
   （对账列表逐行可查，不会互相盖掉）。

## 3. 四处一起核

- **差额目录**：差额条数字 + 对账列表每行读数芯片（蓝=冻结 / 橙=重扫）。
- **对账列表**：每行有「作废卡片 / 核对 / 复验」三个按钮。
- **作废卡片**：点「作废卡片」，卡片上的 run_code 必须与对账单
  `作废批` 数逐张对上；卡片读的是同一份落库明细。
- **复验进度**：点「复验」，第一轮/第二轮缺口应与对账单缺口三者相等，
  结论显示「两轮对齐」，读数芯片与对账单一致。
- **编排任务**：复验记录下方「编排任务」表里有 `GAP-xxxx-Rx`（出数）与
  `VER-xxxx-x`（复验），可查序号、读数、状态。

接口侧可直连核对：

```bash
curl -c /tmp/c -X POST http://127.0.0.1:7013/api/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"123456"}'
curl -b /tmp/c http://127.0.0.1:7013/api/gap-reports/context
curl -b /tmp/c -X POST http://127.0.0.1:7013/api/gap-reports \
  -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-11-16","reading":"重扫"}'
curl -b /tmp/c http://127.0.0.1:7013/api/gap-reports/<报告号>/void-cards
curl -b /tmp/c -X POST http://127.0.0.1:7013/api/gap-reports/<报告号>/reconcile
curl -b /tmp/c -X POST http://127.0.0.1:7013/api/gap-reports/<报告号>/verify
curl -b /tmp/c http://127.0.0.1:7013/api/orchestration-tasks
```

## 4. 偏移必须被拒（三条硬拒）

| 做法 | 期望 |
|---|---|
| 直接改 `gap_reports.digest` | 核对 409「作废摘要与明细不符」 |
| 直接改 `gap_reports.void_count` | 核对 409「作废计数与明细不符」 |
| 把作废批改回 `待盖印` 再取作废卡片 | 409「作废卡片与差额数不同源」 |
| 只改 `gap_count`、不动明细 | 核对 409「缺口对不上…已拒绝」 |

改库示例：

```bash
sqlite3 /tmp/gap_repro.db "UPDATE gap_reports SET digest='bad' WHERE id=2;"
```

## 5. 并发抢重扫

两个标签页同时点「生成差额数（重扫）」，两条都 200，序号必须不同
（1、2、3…连续不撞），两份摘要各自保留自己的序号。
`gap_reports.seq` 上有唯一索引，兜住极端交错。

自动化对照：`pytest tests/test_gap_readings.py -q`（11 条读数/同源/并发用例；
全量 `pytest` 15 条）。
