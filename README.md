# 软件许可证席位履约采证台

离线采录租户签署权限与合同授权、设备激活租约、功能使用会话，以 SQLite 保留原始证据、业务实体、当前裁定和历次裁定。签署人及签署日期须落入授权范围。合同修订、设备转移及迟到会话均按事件时间重新裁定，不依赖真实许可证服务器。

## 运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m seatledger demo --db demo.sqlite
python -m seatledger audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入三个子系统：

```powershell
python -m seatledger grant-import --db demo.sqlite --file fixtures/grant-ok.json
python -m seatledger activation-import --db demo.sqlite --file fixtures/activation-ok.json
python -m seatledger usage-import --db demo.sqlite --file fixtures/usage-ok.json
python -m seatledger report --db demo.sqlite
```

每个入口另有 `*-bad.json` 夹具。演示依次导入六份正常及边界证据，显示修订、冲突、越权功能与超额结算如何改变裁定。导入相同事件 ID 和内容是幂等重放；同 ID 异内容整批回滚。`audit` 从原始事件重算决策并与当前裁定比较。

## 旧系统迁移（项目、联系人、审批关系）

`fixtures/migration-alias.json` 模拟两套旧系统（X/Y，均从 1 编号）的离线导出与未签署的合并别名。迁移域与席位、设备、会话、结算完全隔离：

- 记录身份由 `(来源系统, 租户, 源编号)` 复合确定，跨租户同号互不覆盖；`import_rows.rows` 同键保留最后一行，`migration.count` 只数复合键去重后的剩余行。
- 项目负责人/审批人/联系人别名必须解析到迁入后的同一真实主体；无法证明的多对一合并（仅姓名相同或只有未签署声明）冻结受影响关系，独立记录不丢失。跨租户共同主体须所有受影响租户各自签署映射；任一租户签署身份分离则其源编号保持私有。
- `migration.plan` 先做引用校验与拓扑导入（别名环分配 `staged:` 暂存 ID），项目与负责人/审批边在同一可回读版本内成立；悬挂引用整批报错。
- `import_capture` 以 SQLite 仅追加保存原始证据、版本快照、映射账本与修订清单。已交付清单不可覆写；迟到更正产生新版本并按依赖范围（变更行及引用它们的项目/别名）定位修订。
- `tests/test_migration_links.py` 覆盖同号不同租户、别名、循环引用、联合签署/身份分离两路映射、旧清单回读与事务回滚。

本仓只用合成离线证据，未对接真实授权服务器、计费系统或租户身份服务。
