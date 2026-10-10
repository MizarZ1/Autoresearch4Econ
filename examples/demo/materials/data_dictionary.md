# AJR 2001 Table 4 数据字典

单位为国家横截面；作者分析文件共 163 行、10 列。首个任务限定
`baseco == 1` 的 64 个国家；文件是作者整理后的分析数据。

| 变量 | 定义、时期与单位 | 来源和解释 |
| --- | --- | --- |
| `logpgp95` | 1995 年 PPP 人均 GDP 的自然对数 | 世界银行 WDI 1999；因变量，不是增长率 |
| `avexpr` | 1985–1995 年征收风险保护指数的平均值，0–10 | Political Risk Services，经 IRIS 整理；越高表示保护越强、征收风险越低 |
| `logem4` | 欧洲定居者死亡率的自然对数；历史材料主要涉及 17–19 世纪，每千人每年的死亡数 | 作者依据士兵、主教和水手等历史记录整理；潜在工具变量，不是当代本地人口死亡率 |
| `lat_abst` | 首都纬度的绝对值除以 90，范围 0–1 | 文件标签注明首都；论文附录列来源 La Porta 等（1999）；0 为赤道，非时间变化指标 |
| `baseco` | 作者主样本标记，1 表示纳入 | 用于显式样本选择，不能把全部 163 行当成基准样本 |
| `shortnam` | 三字母国家标识 | 用于辨识国家，非数值解释变量 |
| `africa`、`asia` | 对应地区标记 | 作者标签；本任务初始规格不使用 |
| `rich4` | 作者新欧洲国家标记 | 排除这些国家会改变样本，须另行确认 |
| `loghjypl` | Hall–Jones 的每劳动者产出对数，论文对应 1988 年 | 本任务不使用；不得和 `logpgp95` 混用 |

本文由项目编写，依据实际 `.dta` 标签及[原论文 Table 4、附录 Table A1](https://economics.mit.edu/sites/default/files/publications/colonial-origins-of-comparative-development.pdf)（PDF 页 19、30）。
参考数据来自[作者档案](https://economics.mit.edu/people/faculty/daron-acemoglu/data-archive)。
