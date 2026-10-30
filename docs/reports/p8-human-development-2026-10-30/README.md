# P8 人工开发集诊断（非最终成绩）

2026-10-30。人工100条中97有效、3弃权；弃权为ecd-1018、ecd-0191、ecd-0556，覆盖率97%。三条全部保留在总量与弃权清单，不补标、不当模型错误，不参与无真值的P/R/F1/kappa分母。原始xlsx、文本和备注只在.local/p8-human-development。

本次直接分析0bb74bd候选版本及人工表返回前已存的预测：没有改rule/提示/threshold、没有新模型调用，没有用100条硬凑规则。三版本均按同一97条有效金标比较。kappa是人与规则/模型输出的一致性，不是两位人工标注者kappa。

主要发现：合并模型/混合一致率91.75%，但ECD单独85.96%低于多数类87.72%；规则ECD89.47%高于该基线，宏F1却更低。合成40条模型全对不代表真实语料泛化。ECD无human正例，return_request仅1例；相应可靠性与统计显著性未验证，不据此宣称模型整体优于规则。

指标口径：混淆矩阵行=人工金标、列=预测，顺序均order_status / return_request / human / clarify。precision、recall、F1的零分母记—，不是捏造的0；固定四类宏F1将未定义F1按0计算，同时给出只对本组存在金标类别的宏F1作解释。多数类基线是本开发集的描述性常量；若并列，按上述固定顺序选，不用模型表现选。最终测试常量应在开发阶段固定，不能看最终标签再换。

## 合成：40条有效 / 40条总量

人工分布：`{"order_status": 11, "clarify": 10, "human": 11, "return_request": 8}`；弃权0条。多数类并列：`['order_status', 'human']`。

| 版本 | 一致率/准确率 | 固定四类宏F1 | 有金标类别宏F1 | Cohen kappa |
|---|---:|---:|---:|---:|
|majority|27.50%|0.1078|0.1078|0.0000|
|rule|87.50%|0.8714|0.8714|0.8321|
|model|100.00%|1.0000|1.0000|1.0000|
|hybrid|100.00%|1.0000|1.0000|1.0000|

### 各类精确率/召回率/F1

| 版本 | 类别 | 金标数 | 预测数 | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|
|majority|order_status|11|40|0.2750|1.0000|0.4314|
|majority|return_request|8|0|—|0.0000|0.0000|
|majority|human|11|0|—|0.0000|0.0000|
|majority|clarify|10|0|—|0.0000|0.0000|
|rule|order_status|11|8|1.0000|0.7273|0.8421|
|rule|return_request|8|6|1.0000|0.7500|0.8571|
|rule|human|11|13|0.8462|1.0000|0.9167|
|rule|clarify|10|13|0.7692|1.0000|0.8696|
|model|order_status|11|11|1.0000|1.0000|1.0000|
|model|return_request|8|8|1.0000|1.0000|1.0000|
|model|human|11|11|1.0000|1.0000|1.0000|
|model|clarify|10|10|1.0000|1.0000|1.0000|
|hybrid|order_status|11|11|1.0000|1.0000|1.0000|
|hybrid|return_request|8|8|1.0000|1.0000|1.0000|
|hybrid|human|11|11|1.0000|1.0000|1.0000|
|hybrid|clarify|10|10|1.0000|1.0000|1.0000|

### 混淆矩阵（行金标，列预测）

固定顺序：`['order_status', 'return_request', 'human', 'clarify']`。

majority

```text
 11   0   0   0
  8   0   0   0
 11   0   0   0
 10   0   0   0
```

rule

```text
  8   0   2   1
  0   6   0   2
  0   0  11   0
  0   0   0  10
```

model

```text
 11   0   0   0
  0   8   0   0
  0   0  11   0
  0   0   0  10
```

hybrid

```text
 11   0   0   0
  0   8   0   0
  0   0  11   0
  0   0   0  10
```

## ECD：57条有效 / 60条总量

人工分布：`{"clarify": 50, "order_status": 6, "return_request": 1}`；弃权3条。多数类并列：`['clarify']`。

| 版本 | 一致率/准确率 | 固定四类宏F1 | 有金标类别宏F1 | Cohen kappa |
|---|---:|---:|---:|---:|
|majority|87.72%|0.2336|0.3115|0.0000|
|rule|89.47%|0.4014|0.5351|0.5698|
|model|85.96%|0.6362|0.8482|0.5738|
|hybrid|85.96%|0.6362|0.8482|0.5738|

### 各类精确率/召回率/F1

| 版本 | 类别 | 金标数 | 预测数 | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|
|majority|order_status|6|0|—|0.0000|0.0000|
|majority|return_request|1|0|—|0.0000|0.0000|
|majority|human|0|0|—|—|—|
|majority|clarify|50|57|0.8772|1.0000|0.9346|
|rule|order_status|6|9|0.5556|0.8333|0.6667|
|rule|return_request|1|0|—|0.0000|0.0000|
|rule|human|0|0|—|—|—|
|rule|clarify|50|48|0.9583|0.9200|0.9388|
|model|order_status|6|13|0.4615|1.0000|0.6316|
|model|return_request|1|1|1.0000|1.0000|1.0000|
|model|human|0|1|0.0000|—|0.0000|
|model|clarify|50|42|1.0000|0.8400|0.9130|
|hybrid|order_status|6|13|0.4615|1.0000|0.6316|
|hybrid|return_request|1|1|1.0000|1.0000|1.0000|
|hybrid|human|0|1|0.0000|—|0.0000|
|hybrid|clarify|50|42|1.0000|0.8400|0.9130|

### 混淆矩阵（行金标，列预测）

固定顺序：`['order_status', 'return_request', 'human', 'clarify']`。

majority

```text
  0   0   0   6
  0   0   0   1
  0   0   0   0
  0   0   0  50
```

rule

```text
  5   0   0   1
  0   0   0   1
  0   0   0   0
  4   0   0  46
```

model

```text
  6   0   0   0
  0   1   0   0
  0   0   0   0
  7   0   1  42
```

hybrid

```text
  6   0   0   0
  0   1   0   0
  0   0   0   0
  7   0   1  42
```

## 合并：97条有效 / 100条总量

人工分布：`{"order_status": 17, "clarify": 60, "human": 11, "return_request": 9}`；弃权3条。多数类并列：`['clarify']`。

| 版本 | 一致率/准确率 | 固定四类宏F1 | 有金标类别宏F1 | Cohen kappa |
|---|---:|---:|---:|---:|
|majority|61.86%|0.1911|0.1911|0.0000|
|rule|88.66%|0.8517|0.8517|0.7973|
|model|91.75%|0.9286|0.9286|0.8631|
|hybrid|91.75%|0.9286|0.9286|0.8631|

### 各类精确率/召回率/F1

| 版本 | 类别 | 金标数 | 预测数 | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|
|majority|order_status|17|0|—|0.0000|0.0000|
|majority|return_request|9|0|—|0.0000|0.0000|
|majority|human|11|0|—|0.0000|0.0000|
|majority|clarify|60|97|0.6186|1.0000|0.7643|
|rule|order_status|17|17|0.7647|0.7647|0.7647|
|rule|return_request|9|6|1.0000|0.6667|0.8000|
|rule|human|11|13|0.8462|1.0000|0.9167|
|rule|clarify|60|61|0.9180|0.9333|0.9256|
|model|order_status|17|24|0.7083|1.0000|0.8293|
|model|return_request|9|9|1.0000|1.0000|1.0000|
|model|human|11|12|0.9167|1.0000|0.9565|
|model|clarify|60|52|1.0000|0.8667|0.9286|
|hybrid|order_status|17|24|0.7083|1.0000|0.8293|
|hybrid|return_request|9|9|1.0000|1.0000|1.0000|
|hybrid|human|11|12|0.9167|1.0000|0.9565|
|hybrid|clarify|60|52|1.0000|0.8667|0.9286|

### 混淆矩阵（行金标，列预测）

固定顺序：`['order_status', 'return_request', 'human', 'clarify']`。

majority

```text
  0   0   0  17
  0   0   0   9
  0   0   0  11
  0   0   0  60
```

rule

```text
 13   0   2   2
  0   6   0   3
  0   0  11   0
  4   0   0  56
```

model

```text
 17   0   0   0
  0   9   0   0
  0   0  11   0
  7   0   1  52
```

hybrid

```text
 17   0   0   0
  0   9   0   0
  0   0  11   0
  7   0   1  52
```

## 分歧优先复查清单（不含原文/备注）

排序：先列与金标冲突的版本数，再按人工接管/业务意图风险，再按ID；这不是模型置信度。3版本共同错优先于单版本错。三条弃权不列错误榜。原因是开发诊断假设，不改人工标签，不用尚未批准的clarify子类当正式结论。

共16条至少一版本与金标不一致：9条涉及clarify与可执行业务边界、4条涉及否定/引用、3条涉及上下文或业务动作解释。模型的8个错误全在ECD，金标均clarify：7个被判物流/履约查询、1个被判人工；规则P2的错误主要涉及否定/引用与正向需求分离。不能把这些开发错误按ID加特判去刷分。

| ID | 来源 | 金标 | rule | model | hybrid | 与金标冲突版本 | 诊断原因 |
|---|---|---|---|---|---|---:|---|
|ecd-0266|ecd_redacted|clarify|order_status|order_status|order_status|3|clarify与当前可执行动作边界|
|ecd-0294|ecd_redacted|clarify|order_status|order_status|order_status|3|clarify与当前可执行动作边界|
|ecd-1069|ecd_redacted|clarify|order_status|order_status|order_status|3|clarify与当前可执行动作边界|
|ecd-0175|ecd_redacted|clarify|clarify|order_status|order_status|2|clarify与当前可执行动作边界|
|ecd-0316|ecd_redacted|clarify|clarify|order_status|order_status|2|clarify与当前可执行动作边界|
|ecd-0980|ecd_redacted|clarify|clarify|order_status|order_status|2|clarify与当前可执行动作边界|
|ecd-1103|ecd_redacted|clarify|clarify|human|human|2|clarify与当前可执行动作边界|
|ecd-1169|ecd_redacted|clarify|clarify|order_status|order_status|2|clarify与当前可执行动作边界|
|ecd-0370|ecd_redacted|clarify|order_status|clarify|clarify|1|clarify与当前可执行动作边界|
|ecd-0092|ecd_redacted|return_request|clarify|return_request|return_request|1|上下文/业务动作解释|
|ecd-1072|ecd_redacted|order_status|clarify|order_status|order_status|1|上下文/业务动作解释|
|p2-0127|p2_synthetic|order_status|human|order_status|order_status|1|否定/引用|
|p2-0165|p2_synthetic|order_status|clarify|order_status|order_status|1|否定/引用|
|p2-0296|p2_synthetic|order_status|human|order_status|order_status|1|否定/引用|
|p2-0332|p2_synthetic|return_request|clarify|return_request|return_request|1|上下文/业务动作解释|
|p2-0559|p2_synthetic|return_request|clarify|return_request|return_request|1|否定/引用|

## clarify子类提案：待负责人确认，不应用于正式报告口径

原人工clarify60条保持不变；下述仅从既有备注生成诊断提案，不要求负责人重标。未按子类计算任何质量分数，也未把它们写回人工金标。

| 拟议子类 | 判定规则（按备注，不按模型预测） | 合成 | ECD | 总计 |
|---|---|---:|---:|---:|
| 信息不足或意图未定 | 无可执行诉求、信息不足、只有否定、混合需求无主次、未提出明确动作的泛化抱怨；作为余下clarify的诊断归类 | 9 | 6 | 15 |
| 超范围咨询 | 备注明确为超范围、售前、议价、赠品、优惠、商品规格/材质/尺寸/数量等；此规则优先于同条中的礼貌收尾 | 1 | 30 | 31 |
| 致谢闲聊/无新诉求确认 | 备注为致谢、闲聊、收尾、简短回应/确认；提议也包含没有新动作的已拍/下单/购买计划告知 | 0 | 14 | 14 |

边界需负责人确认：是否把无新请求的订单/购买计划告知纳入第三类；涉及ecd-0207、ecd-0313、ecd-0560，以及确认型ecd-0370、ecd-0152。若不接受这条边界，可以仅按备注调整派生诊断映射，不改变人工clarify根标签，也不要求重标。尚未确认时，不将14/15/31当已批准类别定义。

建议暂保留四个动作根类，先用上述三种原因解释clarify。致谢与超范围的响应动作确实不同，但直接新增第5根类会改变现有工单契约和人工标签语义；不能为了开发分数改成新类后仍沿用旧成绩。若后续要独立social/导购路由，须先另行确认规范和标注映射，再冻结新版本。本轮未改定义/已标数据。

## 最终200分层方案更新（未执行）

开发决策完成后先提交/推送并打冻结tag，之后才在未用ECD部分建立有限候选框、按冻结银标提示标层并抽样。当前没有冻结tag、没有候选最终文本或200条样本；没有为了找稀有类提前读出或调用模型。

拟按四个银标层各抽50条，共200条；记录去重采样单位、候选框ID/摘要、每层N_h与n_h、随机种子、纳入概率pi_h=n_h/N_h、权重w_h=N_h/n_h及弃权/排除流程。银标仅决定抽谁，成绩始终用人工最终标签。未加权结果描述刻意富集的测试集；如用权重估计候选框总体，应同时给加权混淆矩阵/指标并说明只对该候选框有效，不能直接外推全ECD自然分布。

约30条的保障必须分清：各银标层50可满足银标配额，但银标误差意味着不能保证人工最终每类也≥30。开发silver的human只有4/1200，人工ECD human为0，稀有类候选是否足够尚未验证。冻结后若候选层不足，或人工有效标签不足30，不伪造、不用合成补成ECD、不擅自重标/补样；在正式评分前报告覆盖缺口和预先约定的扩池/补样方案。最终抽样预算、真实层规模和权重当前都未实测，不能写“已满足每类30”。

## 未做/未验证

- clarify子类提案尚未获确认，未改变四类标签或据此发布子类成绩。
- 未调整规则/提示/阈值；未做按100条特判或其他开发集拟合。
- 未打冻结tag、未从未用ECD抽最终200、未执行最终分层/权重或稀有类覆盖验证。
- 未获得人工最终标签、未计算最终质量指标、未验证加权总体结论或最终一次运行流程。
- 开发语义诊断未证明模型总体胜出；ECD human召回无正例可评，return只有1例，不报安全能力结论。
- 未核验模型账单扣费、全面人工PII覆盖、容量/限频压测；前端未改未重跑，P10/S3未做。

完整机器可读指标见metrics.json。分析仅新增离线代码，当前候选预测来自0bb74bd，新增模型调用0。
