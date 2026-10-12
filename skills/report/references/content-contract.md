# 通用报告内容契约

## 内容组织

报告按需要包含分析目标与范围、核心结论、关键指标、发现与解释、图表和数据、行动建议、口径与局限。
不固定要求四个指标或三条建议，不为满足版式补造内容；简单问数保持简洁。
使用 submit_analysis_report 提交结构化内容，程序负责页面与 Word、HTML、PPT、Excel 渲染。
只复用当前已查询的数据与已核验结论，不写 HTML、CSS、图表脚本或执行代码。

## 指标与证据

metrics 指定 name 与 cell（result_id、从零开始的 row、实际 column），由服务端读取数值。
glossary 为指标、图表系列、表头及派生值提供定义、公式、单位与注意事项，并绑定 result_id 和 column。
正式指标解释沿用指标定义与版本；探索性指标说明假设与待核对事项，不能冒充正式口径。
findings 包含 text、type（fact/inference/risk）、evidence_refs；可补充 why、impact。
置信度只有在写明 confidence_reason 时才声明；未经独立验证的推测不能标为 high。
发现中的数字必须能在该条引用的结果中核对；比例、差值、预测等推导值先通过工具计算。

## 建议与局限

recommendations 包含行动 text、相关 evidence_refs，并尽可能明确 owner、scope 与 priority。
建议是拟议行动，不保证收益；不能凭相关性声称因果。
tables 为当前结果提供可读标题；所有使用到的查询结果都会保留，导出复用同一发布版本。
assumptions 与 limitations 明确时间边界、单位、抽样、数据缺口及仍需业务复核的解释。
未评估的质量显示未评估，零值显示零，缺失显示缺失，不借助默认值填满模板。
