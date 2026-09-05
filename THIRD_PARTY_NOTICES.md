# 第三方代码与资产声明

本仓库的整体授权状态不会因本文件自动改变。下列组件保留各自的著作权和使用条件；在发布、内部商用、SaaS 或二次销售前，使用方必须完成授权审核。

审计时仓库根目录没有项目级 `LICENSE`。在理清上游权利、代码贡献权和预期发布模式后，应由项目权利人选择并正式加入项目许可证；本次审查不代替权利人做该法律选择。

## Data-Analysis-Agent

- 上游项目：[Zafer-Liu/Data-Analysis-Agent](https://github.com/Zafer-Liu/Data-Analysis-Agent)
- 审计基线：`a5f7a4faceb0cd9d19215097e9db8d4877daf68b`
- 著作权：Copyright © 2026 Zafer-Liu
- 上游条款：[CC BY-NC 4.0 及商用授权说明](https://github.com/Zafer-Liu/Data-Analysis-Agent/blob/a5f7a4faceb0cd9d19215097e9db8d4877daf68b/LICENSE)

本仓库的 `skills/*/SKILL.md`、`backend/reference_analysis/`、`backend/reference_clean/` 和 `backend/reference_output/` 包含来自该上游项目或与其同源的实现。上游条款允许署名后的学习、研究和非商业使用，但明确要求任何商业用途（包括企业内部用于产生商业利益）事先获得著作权人书面授权。

因此，在没有书面商业授权，或未用独立实现完整替换上述同源组件前，不应将当前整仓库宣称为可无条件商用。

## Apache ECharts 中国地图数据

- 上游项目：[Apache ECharts](https://github.com/apache/echarts)
- 文件：`frontend/vendor/echarts-china.min.js`
- 来源：[Apache ECharts 4.9.0 `map/js/china.js`](https://github.com/apache/echarts/blob/4.9.0/map/js/china.js)
- SHA-256：`6e763608cb7a0e2fa571ebca3127f6d10940068853b5b404c234feb2f5be15e6`
- 许可证：Apache License 2.0（文件中保留上游头部声明）

历史地图数据只用于保持离线图表兼容。面向中国境内用户发布地图前，运营方应换用当期权威地图数据，完成地图审核并展示审图号。

## pyecharts

`pyecharts` 2.1.x 用于点密度地图的地名坐标解析，按 [MIT License](https://github.com/pyecharts/pyecharts/blob/master/LICENSE) 发布。其坐标库是图表定位辅助数据，不取代正式地图审核。
