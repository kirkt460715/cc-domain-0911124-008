# 物料账

接收来源偏移事件，登记物料收货、转换、损耗、出货与冻结。物料谱系、库存数量和召回路径相互关联，检验处置决定能否放行。

演练先收到依赖未满足的转换事件，再接收入库记录，随后检查出货、召回与物料守恒。

主要代码位于 chainledger，按 events、graph、views 划分职责；tests 保存领域行为、边界及存储回归用例。命令接收严格 JSON，详细操作名与字段可通过对应帮助入口及 service 中的注册表查询。示例中的机构、客户、案件和测量均为本地合成记录，不涉及真实个人资料。

使用 Python 3.11.2 与标准库，无第三方包。运行无需访问外部服务或提供账号。依赖清单 requirements.txt 为空，.python-version 固定解释器版本。

在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q chainledger
python3 -m chainledger demo
```

也可执行下面的统一检查，按顺序进行语法编译、领域回归和完整本地演练，任一环节失败即退出。运行写入均在临时目录或内存数据库，重复执行不需清理已有业务账。正常数据规模的检查可在 90 秒内完成。

```sh
sh scripts/check.sh
```
