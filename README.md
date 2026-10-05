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

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`GET /api/sources`、`GET /api/sources/{id}`：来源登记与查看（仅工作人员/审查员）。
- `POST /api/sources/{id}/status`：撤回或标成存疑（`withdrawn`/`disputed`），可带 `expected_status` 乐观锁；撤回为终态，先到生效，后来者会看到先撤回的审查员。
- `POST /api/objects/{id}/events`：来源与流转事件；引用已撤回来源会被拒绝。
- `POST /api/objects/{id}/events/{event_id}/confirm-source`：确认旧数据回填的来源关联。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `POST /api/claims/{id}/resume`：挂起裁定的复核（`uphold` 维持 / `reopen` 重开）。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。
- `--upgrade`：旧数据升级，为缺来源的事件按发生时间回填当时有效的一条来源并挂待确认。

来源状态更新会联动：未了结的流转事件作废重算（对公众隐藏），已办结的返还裁定挂起等复核；历史版本保留当时记录。内部调查说明仅工作人员/审查员可见，越权查看按角色返回 403。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
