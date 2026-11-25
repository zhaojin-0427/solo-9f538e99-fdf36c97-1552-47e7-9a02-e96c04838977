# 装车秤单同期名次复核

Flask + SQLite 的港口装车秤单复核页。支持按日期上传/整日替换 CSV、按“日期 + 单号”暂扣或解除、在同一读事务中查询全部秤单与未暂扣秤单的总数和降序名次区间，并在页面显示小计键访问数。

## 启动

### Docker Compose

```bash
docker compose up --build
```

浏览器打开：

- <http://127.0.0.1:8000/>
- 也映射了 <http://127.0.0.1:5000/> 到同一服务

SQLite 数据库保存在 Compose 命名卷 `scale-data` 的 `/data/scale.db`。

### 本地 Python

```bash
python3 -m pip install -r requirements.txt
DB_PATH=./data/scale.db python3 app.py
# 或
gunicorn --bind 0.0.0.0:8000 app:app
```

## CSV 格式

在页面选择日期后上传；该日期的旧秤单会被整个交易日的新文件替换，但暂扣记录不会删除。

支持表头：

```csv
单号,净重
A-1001,32000
A-1002,31500
A-1003,31500
```

也支持无表头两列：

```csv
A-1001,32000
A-1002,31500
```

规则：

- 单号在所选日期内唯一；同一 CSV 重复会整文件拒绝。
- 净重必须是整数，范围 `0` 到 `65535` 千克（含边界）。
- 支持 `utf-8-sig`、UTF-8 和中文 Excel 常见的 GB18030。
- 页面也可直接粘贴 CSV 文本。

示例文件：[`sample_tickets.csv`](sample_tickets.csv)。

## 暂扣与重报

- 暂扣表以 `(slip_date, ticket_no)` 为主键，不设指向秤单的外键。
- 整日替换秤单时不动暂扣表，因此：
  - 重报后暂扣仍有效；
  - 单号在一次或多次 CSV 中缺失，暂扣记录仍保留；
  - 该单以后重新出现时，会自动按暂扣状态排除出 `active` 口径。
- 目标单被暂扣时，“全部秤单”仍正常排名；“未暂扣秤单”显示区间总数和“暂扣”标记，名次单元格留空。

## 名次规则

给定区间 `[start, end]`（同年且包含单据日期，首尾均含）和目标净重 `w`：

- `higher = count(net_weight > w)`
- `equal = count(net_weight = w)`
- 名次区间为 `higher + 1` 到 `higher + equal`

例如两单同为 90 kg，且只有一单 100 kg，则 90 kg 的单共用第 `2-3` 名。

## 小计键与 576 上限

每个 `subtotals` 行只表示：

```text
一个日期规范段 × 一个二进重量档 × 一种口径(all/active) → ticket_count
```

实现使用两棵确定性的平衡/二分区间树：

- 日期：按当年实际天数（365/366）递归二分。任意年内区间最多覆盖 **15** 个互不相交的日期规范段（已用穷举测试验证所有起止日组合）。
- 重量：`0..65535` 是完整的 `2^16` 域。`> w` 的后缀最多拆成 **16** 个对齐二次幂重量档；另加目标重量叶子键 1 个、全重量根键（取总数）1 个，因此每个日期段、每种口径最多访问 `18` 个重量键。

总上限为：

```text
2 种口径 × 15 个日期段 × 18 个重量键 = 540 ≤ 576
```

若目标已暂扣，`active` 口径只取每段的总数根键，实际访问数通常更低。每次查询都用 `KeyMeter` 对每个请求的小计主键计数；数据库中不存在的空键也计一次，并在页面和 JSON 的 `meter.empty` 中显示。页面还可展开每个键的日期段、重量档、口径、读数和是否空键。

目标原单通过 `tickets` 的 `(slip_date, ticket_no)` 主键索引读取；暂扣标记通过 `holds` 主键读取。所有名次计数均来自小计键，不扫描原单表。

## 一致性和失败处理

- SQLite 连接使用显式事务。
- 上传替换在一个 `BEGIN IMMEDIATE` 事务内完成：回读当天旧原单、写回新原单、应用新旧小计差异，然后提交。
- 暂扣/解除在一个事务内写暂扣记录并同步修改 `active` 小计。
- 任一步失败即回滚，旧秤单、暂扣记录和小计仍保持上一已提交状态，查询可照常取数。
- 名次查询在一个读事务中读取目标原单、暂扣状态和全部小计键，因此看到的是同一个已提交快照。

## 主要路由

页面：

- `GET /`：上传、暂扣/解除、查询入口。
- `GET /day?date=YYYY-MM-DD`：回读当天原单和“原单缺失但仍有效”的暂扣。
- `GET /ticket?date=...&ticket_no=...`：只回读对应原单和暂扣状态。
- `GET|POST /query`：复核结果页。

表单/API：

- `POST /upload`，或 `POST /api/upload`
- `POST /hold`、`POST /release`
- `GET|POST /api/rank`、`GET|POST /api/query`
- `GET /healthz`

JSON 示例：

```bash
curl 'http://127.0.0.1:8000/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31'
```

返回中的 `result.all`、`result.active` 含 `total/higher/equal/rank_start/rank_end/rank_text`，`result.meter` 含 `accessed/empty/limit/within_limit/details`。

## 已做验证

在本环境中实际运行了：

```bash
PYTHONPATH=/tmp/pylibs:/workspace python3 -m unittest discover -s tests -p 'test_*.py'
```

结果：**14 个测试全部通过**，覆盖：

- 同重名次区间；
- 目标暂扣时 active 名次为空；
- 暂扣跨整日重报、单号缺失、再次出现后仍有效；
- 解除后恢复；
- CSV 表头/无表头、重复单号、非整数和重量边界；
- 365/366 年所有日期区间的日期段数不超过 15；
- 所有 65536 个重量值的后缀拆分不超过 16；
- 最坏键访问数 540，不超过 576；
- 注入小计失败后暂扣/重报事务回滚，旧数据仍可查询；
- Flask 页面、上传重定向、暂扣提示和访问数显示；
- `EXPLAIN QUERY PLAN` 确认目标查询使用主键索引。

还用 Flask 开发服务器和 Gunicorn（通过 `entrypoint.sh` 的 `python3 -m gunicorn`）实际请求验证了上传、同重查询、暂扣和最坏 540 键页面输出。

## 未完成 / 未在本环境验证的事项

- 当前环境没有 `docker` / `docker compose` 可执行文件，因此未能在这里真正执行 `docker compose up --build`；已用 PyYAML 解析两个 Compose 文件，并在本机用等价的 Gunicorn 命令验证应用启动。Docker 镜像构建需在有 Docker 的环境中最终确认。
- 未加入登录鉴权；需求未要求，当前定位为港口内网复核工具。
- 未做浏览器视觉回归；页面行为通过 Flask 测试客户端和真实 HTTP 请求验证。
