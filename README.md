# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。
- `sanction`：**处罚期（禁赛期）独立建档**，由案件决定自动生成，不允许直接创建。
  - 决定为禁赛时按`禁赛月数`生成新版本；改判保留旧版（`superseded`/`vacated`）。
  - 同案连续且**未撤销**的临时禁赛按自然日抵扣；重叠案件已经抵扣过的日期不重复扣。
  - 决定未给月数、或老案升级缺少处罚期时生成`pending_backfill`（待补），挡参赛；
    通过对处罚期执行`backfill`动作补录月数，补录走同一套抵扣与作废检查。
  - 决定/补录时自动作废处于禁赛生效窗口内的既有比赛成绩，原始数据与作废记录保留；
    已作废成绩在改判时不重复作废。
- `result`：比赛结果。登记时读取**比赛当时生效**的处罚期与复出检测进度，
  命中禁赛/待补/复出检测不足则拒绝登记，已有成绩不动。
- `comeback_test`：复出检测。禁赛期满后需要三次（检测日期须晚于处罚期结束日）才恢复参赛。

### 案件动作新增

- `provisional_suspend`（可再次临时禁赛，形成连续段）、`revoke_provisional`（撤销，撤销段不抵扣）。
- `decide` / `resolve_appeal`：`decision`为`sanction`时可带`suspension_months`（不带则进入待补），
  可带`decided_at`（默认今天）；为`no_sanction`时撤销同案现行处罚期。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`sanctions`/`results`/`comeback_tests`均支持）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/athletes/<id>/eligibility?as_of=YYYY-MM-DD`：当前资格视图，
  含处罚期版本、抵扣明细、复出检测和成绩（含作废标记）。
- `GET /api/audit`：读取审计记录。

处罚期生成/改判与成绩登记在同一个`BEGIN IMMEDIATE`事务内完成，
因此“决定改判”和“结果登记”并发到达时，不会出现一边恢复参赛、另一边仍显示禁赛的分裂状态。


请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
