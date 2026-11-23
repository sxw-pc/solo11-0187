# 软件许可证席位履约采证台

离线采录合同授权、设备激活租约与功能使用会话，以 SQLite 保留原始证据、业务实体、当前裁定和历次裁定。合同修订、设备转移及迟到会话均按事件时间重新裁定，不依赖真实许可证服务器。

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

本仓只用合成离线证据，未对接真实授权服务器、计费系统或租户身份服务。
