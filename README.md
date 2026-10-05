# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`reviewer2`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`（支持 `valid_from`/`valid_until` 有效期）、`GET /api/sources`、`GET /api/sources/{id}`：来源登记与分层查看。
- `POST /api/sources/{id}/status`：审查员把来源标记为 `active`/`doubtful`（存疑）/`withdrawn`（撤回），需提供至少 5 字的 `internal_note`，可选 `reason_public` 对外说明。
- `GET /api/sources/{id}/investigation`：内部调查说明与变更日志，仅 staff/reviewer，其他角色返回 403。
- `POST /api/events/{id}/confirm`：确认旧数据回填的来源关联，摘掉待确认标记。
- `POST /api/admin/upgrade-legacy`：手动重跑旧数据升级（staff），或用 `python3 app.py --upgrade`。
- `POST /api/objects/{id}/events`：来源与流转事件。引用撤回来源会被拒绝，引用存疑来源会挂 `needs_confirmation`。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转；`review_hold` 是挂起复核态，可退回 `under_review` 或复核后维持/推翻。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 来源撤回与存疑的级联规则

- 来源状态一更新，引用它的未作废流转事件立即标记 `voided`（记录作废人、时间与原因）；记录本身不删除。
- 藏品下未办结的主张（`under_review`/`negotiating`）自动退回 `under_review` 重算；已办结的 `resolved_return` 裁定进入新状态 `review_hold` 挂起等复核，并写入一条系统审查记录。
- 历史快照固化当时的来源状态和事件作废标记：撤回前的版本仍是“有效来源 + 未作废事件”，撤回后生成新版本。
- 两名审查员并发撤回同一条来源时，整段变更在 `BEGIN IMMEDIATE` 事务中先到先得；后到者收到 409 `source_status_conflict`，响应里带首位撤回人 ID、姓名与时间。撤回是终局，存疑可恢复为有效。
- 公众与主张人只能看到来源的公开字段（状态、公开原因、有效期），看不到 `internal_investigation` 和内部变更日志；作废事件不会出现在公众页。
- 旧库升级（启动时自动执行一次，幂等）：来源补有效期（以录入时间为生效起点）；早年间无来源的事件按发生日期匹配“当时唯一有效”的来源回填，候选为零或不唯一时只挂 `needs_confirmation` 不猜测；旧的手工关联同样挂待确认。
