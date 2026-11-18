# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 差额台读数（作废批口径）

作废批进不进差额，由**一条钉死的读数时机**决定，二选一：

- **冻结**：打开「缺口」页时冻结作废集合快照，之后出数都用这份快照；
- **重扫**：点「生成差额数」的提交瞬间重扫作废态。

两种读数都在一个写事务里一次落成（取序号 → 读作废实物 → 算数 → 落摘要），
并发抢重扫会串行入队，序号自增、摘要互不覆盖。作废明细、作废计数、
摘要三者同源：任何一处对不上（被人改库、作废批被改回非作废态）就拒出数，
差额目录 / 对账列表 / 作废卡片 / 复验进度四处的读数芯片同源同标。
每次出数与复验都写一条编排任务，可在页面「编排任务」表查到。

复现与二验步骤见 [`docs/复现路径-watermark-bug-07.md`](docs/复现路径-watermark-bug-07.md)。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
