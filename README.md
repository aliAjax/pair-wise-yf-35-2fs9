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
- `provisional_suspension`：临时禁赛段（系统随案件动作生成，可被撤销）。
- `sanction`：禁赛处罚期，独立建档、按版本保留；状态为 `active` / `superseded` / `pending_backfill`。
- `competition_result`：比赛结果（`registered` / `rejected` / `annulled`）。
- `comeback_test`：复出检测（阴性）。

## 禁赛处罚期规则

- 案件作出 `sanction` 决定时必须给出禁赛月数，系统按自然月生成处罚期，决定日为起算日。
- 同案**连续且未撤销**的临时禁赛可抵扣；撤销段不抵扣，间隔会把抵扣拆成多段。
- 跨案件重叠的临时禁赛日期不重复抵扣：先建的处罚期先占日期，后建的扣除已占用部分。
- 改判（appeal → resolve_appeal）生成新版本处罚期，旧版本标记 `superseded` 并完整保留；
  决定窗口内的已登记比赛成绩作废（`annulled`），改判后也不恢复。
- 登记比赛结果时按**比赛当日生效**的处罚期校验：命中区间或处于待补状态一律拒绝，
  原始成绩以 `rejected` 落库保留，不会被覆盖。
- 处罚期（含抵扣提前结束）届满后，需要三次日期晚于届满日的阴性复出检测才恢复参赛；
  届满当天的检测不计入。
- 缺少处罚期的历史案件在数据库升级后自动生成 `pending_backfill`（待补）并挡住参赛；
  补录走 `backfill` 动作，复用同一套月数/抵扣/作废检查。
- 改判与结果登记在同一把数据库写事务（`BEGIN IMMEDIATE`）下串行提交，
  不会出现一边已恢复参赛、另一边仍显示禁赛的分裂状态。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；别名如 `sanctions`、
  `competition_results`、`comeback_tests`、`provisional_suspensions`。
- `POST /api/<kind>`：创建对象；请求体为JSON（处罚期和临时禁赛段只能由系统生成）。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/athletes/<id>/eligibility?on=YYYY-MM-DD`：当前资格、处罚期版本与抵扣、
  复出检测和比赛结果汇总。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
