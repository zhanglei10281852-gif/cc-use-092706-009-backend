# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 候鸟观测观察链

`/api/birds` 提供迁徙季观测证据合并能力：

- `POST /api/birds/evidence` 提交观测证据（含附件摘要、来源可信度 trusted/standard/low）。带相同 `client_ref` 的重复提交返回同一观察事件与同一条证据，不重复计数。
- `POST /api/birds/events/{id}/candidate-links` 按时间与空间容差生成候选关联，支持跨日迁徙；被驳回的关联不再重复推荐。
- `POST /api/birds/merge` 把多条观察事件合并成可复核观察链，合并后保留每条原始证据。
- 冲突物种不会自动丢弃：低可信来源只降低结论权重，冲突时进入人工复核队列 `GET /api/birds/review-queue`，由复核员 `POST /api/birds/review-items/{id}/decision` 通过或驳回。
- `POST /api/birds/evidence/{id}/withdraw` 与 `/restore` 撤销/恢复证据，撤销不删除记录，恢复后重新计入共识；全程可在 `/audit-trail` 复核。
- 角色可见范围：志愿者只见自己参与的观察链、复核员可见并裁定全部链、审计员全局只读、`administrator` 拥有全部权限。


## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。
