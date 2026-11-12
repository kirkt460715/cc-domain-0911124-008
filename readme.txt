# 物料账

接收来源偏移事件，登记物料收货、转换、损耗、出货与冻结。物料谱系、库存数量和召回路径相互关联，检验处置决定能否放行。

演练先收到依赖未满足的转换事件，再接收入库记录，随后检查出货、召回与物料守恒。

抽检方案在物料账内单独成域：从有限批量、可接受质量水平（AQL）、拒收质量水平（RQL）、生产方风险与使用方风险出发，用不放回超几何概率搜索单次或双次属性抽样方案，给出 OC 曲线、平均样本量与两端风险；约束无可行解时返回不可行报告而不是退回违约方案。批准后的方案冻结批量、抽样单位定义、检验规格与全部计算参数，再绑定实际来料批。抽样单位对应批内唯一实物标识，计件批量与账本重量分别登记并核对，千克不会被当作独立样品数。双次方案的首轮接收/拒收/继续区域与次轮累计缺陷判定按冻结方案推进，样本在批内不重复；样本不足、批量变化或样本来自其他批次时阻止签发并保留异常记录，放行证明保留所用方案与实际判定路径。

主要代码位于 chainledger，按 events、graph、views 划分职责；抽样数学在 graph/sampling_stats.py，方案冻结与执行在 graph/sampling.py；tests 保存领域行为、边界及存储回归用例。命令接收严格 JSON，详细操作名与字段可通过对应帮助入口及 service 中的注册表查询。抽检相关操作包括 sampling_design（仅搜索不落库）、sampling_limits（查询搜索上限与优选顺序）、sampling_plan（批准并冻结方案）、sampling_bind、sampling_inspect、sampling_advance（双次方案进入次轮）、sampling_decision、sampling_issue 与 sampling_certificate。示例中的机构、客户、案件和测量均为本地合成记录，不涉及真实个人资料。

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
