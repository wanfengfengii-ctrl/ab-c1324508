# 放疗质控剂量记录平台 (Radiotherapy Dose-Control Platform)

记录每个疗程各照射通道（channel）的分次实测剂量。核心保证：**并发上报、网络
重试、服务重启都不会造成重复累计，也不会突破处方上限。**

## 语义保证

| 场景 | 行为 |
|---|---|
| `PUT` 创建疗程 | 1–16 个唯一通道，处方剂量为**正规范十进制字符串**（如 `10`、`0.5`；`1.0`、`01`、`1E2` 一律拒绝） |
| 同内容重试创建 | 返回既有疗程（HTTP 200，`replayed: true`），不产生新数据 |
| 改写已存在疗程 | `409 course_conflict`，疗程不可变 |
| `POST` 分次剂量 | 每通道正增量、至少一个通道；全部校验通过才整次写入（单事务） |
| `deliveryId` + 同内容重试 | 回放**首次结果**（修订号、当时的各通道累计量、`replayed: true`），不重复累计 |
| `deliveryId` 异内容重用 | `409 delivery_id_conflict`，零写入 |
| `expectedRevision` 过期 | `409 revision_conflict`，零写入 |
| 任一通道累计 > 处方 | `422 over_prescription`，整次提交零写入（即使其他通道合法） |
| 每次成功接纳 | 修订号恰 +1，返回所有通道累计量 |
| 全部通道恰好达到处方 | 疗程转为 `complete`；此后仅允许原提交回放，新提交返回 `409 course_complete` |

## 正确性如何实现

- **单事务串行提交**：所有写操作在 SQLite 的 `BEGIN IMMEDIATE` 事务中完成
  （WAL 模式 + `busy_timeout`）。并发提交中只有一个能在当前修订号上成功，
  其余因修订号已变而整次失败——天然实现乐观并发控制。
- **幂等记录持久化**：`(course_id, delivery_id)` 为主键保存首次请求的内容
  与首次结果。重试先查幂等表：内容一致则回放，不一致即冲突；记录随数据库
  持久化，重启后仍然有效。
- **定点整数运算**：剂量以 10⁻⁶ 为单位存为整数，杜绝浮点误差导致的
  “恰好等于处方”误判；入参必须是规范十进制字符串，避免 `1.0` 与 `1`
  这类表示差异。
- **持久化**：所有状态位于 `/data/dosimetry.db`，挂载为 Docker 命名卷，
  容器重启后修订号、累计量、完成状态、幂等记录全部保留。

## API

### `PUT /api/courses/{courseId}`
```json
{
  "channels": [
    {"name": "A", "prescribed": "10"},
    {"name": "B", "prescribed": "5.5"}
  ]
}
```
返回 `201`（新建）/ `200`（同内容回放），body 含 `revision`、`status`、
`totals`（各通道当前累计量）。

### `POST /api/courses/{courseId}/deliveries`
```json
{
  "deliveryId": "frac-2026-10-05-01",
  "expectedRevision": 0,
  "increments": {"A": "2.5", "B": "1"}
}
```
成功返回当前 `revision`、`status`、全部通道 `totals`。

### `GET /api/courses/{courseId}` / `GET /health`
查询疗程状态；健康检查会实际查询数据库。

## 运行

```bash
# 构建并启动（宿主机端口可配置）
API_PORT=9090 docker compose up -d --build
curl http://localhost:9090/health

# 停止（保留数据卷）/ 清空数据
docker compose down
docker compose down -v
```

数据保存在命名卷 `dosimetry-data`（容器内 `/data`）。

## 一次性 verify 服务

汇总 **代码测试 + 镜像构建 + API 冒烟（含真实重启）**，退出码非 0 即失败：

```bash
scripts/verify.sh            # 需要 docker compose
scripts/verify_local.sh      # 无 Docker 时：本机 venv + 真实 uvicorn 重启
```

流程：构建 → pytest 46 项 → 启动 API → 冒烟阶段 1（创建疗程、12 路并发
竞争同一修订号、幂等回放、异内容冲突、超量零写入、分期填满到处方转
complete）→ **重启 API 容器/进程** → 冒烟阶段 2（校验修订号/累计量/
complete 状态/幂等记录在重启后仍然正确）。

## 项目结构

```
app/
  fixedpoint.py   # 规范十进制解析 + 1e-6 定点整数
  db.py           # SQLite schema / WAL / BEGIN IMMEDIATE 事务
  schemas.py      # 请求模型与字段校验
  service.py      # 疗程创建、分次提交、幂等回放、冲突判定
  main.py         # FastAPI 路由与错误映射
tests/            # pytest：定点数、API 语义、并发、重启持久化
verify/smoke.py   # 纯标准库 HTTP 冒烟（smoke1 / smoke2 两阶段）
scripts/          # verify.sh（Docker）、verify_local.sh（本机）
Dockerfile        # 含 HEALTHCHECK，非 root 运行，/data 卷
docker-compose.yml# api 服务 + 健康检查 + 持久化卷 + verify profile
```
