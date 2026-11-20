# 知识点地图（Knowledge Map）

K12 知识点体系，作为题目生成的**权威输入源**。由本项目依据 2022 版国家课程标准独立梳理，不依赖任何外部数据。

## 为什么需要它

题库的质量取决于源头的知识点质量。**先把知识点按权威课标梳理干净、结构化，再生成题目**，是源头治理——只有领域、目标、前置关系、易错点都明确的知识点，才能稳定产出高质量、不超纲、可校验的题目。本项目因此选择先建这套知识点地图，再接生成管道。

## 覆盖范围：63 册

| 学段 | 年级 | 科目 | 册数 |
|---|---|---|---|
| 幼儿园 | 小/中/大班 | 语文·数学·英语 | 9 |
| 小学 | 1-6 年级 × 上下册 | 语文·数学·英语 | 36 |
| 初中 | 7-9 年级 × 上下册 | 语文·数学·英语 | 18 |

## 目录

```
knowledge-map/
├── SCHEMA.md                       # 知识点字段规范（梳理的"合同"）
├── taxonomy/
│   ├── standards.json              # 2022课标领域/任务群/主题 + 学段划分
│   └── volumes.json                # 63册清单（含状态）
├── volumes/<stage>/<subject>/       # 逐册知识点文件（分治单位）
├── tools/
│   ├── gen_volumes.py              # 生成63册清单和占位文件
│   ├── validate.py                 # 校验结构/领域/ID/先修引用
│   ├── merge.py                    # 合并 → output/knowledge_points.jsonl
│   └── progress.py                 # 进度看板 + 生成梳理提示词
└── output/knowledge_points.jsonl   # 合并后的统一知识点清单
```

## 工作流：用 WorkBuddy 免费额度逐册梳理

梳理知识点是**知识密集型、量适中**（约 240 万 token），适合对话式完成；生成 10 万题才需要代码管道。

### 第 1 步：看进度，选下一册

```bash
python3 knowledge-map/tools/progress.py            # 看整体进度
python3 knowledge-map/tools/progress.py --next 5   # 列出接下来建议梳理的册
```

### 第 2 步：拿到该册的梳理提示词

```bash
python3 knowledge-map/tools/progress.py --prompt primary-g2-s1-math
```

把输出的提示词发给 WorkBuddy，它会按 SCHEMA 梳理该册全部知识点，写入对应 JSON 文件。

### 第 3 步：校验 + 标记完成

WorkBuddy 梳理完会写入文件并把 `status` 改为 `done`。然后：

```bash
python3 knowledge-map/tools/validate.py primary-g2-s1-math   # 校验单册
python3 knowledge-map/tools/validate.py                       # 校验全部
```

按提示修复错误，直到 0 错误。

### 第 4 步：合并 + 生成题目

```bash
python3 knowledge-map/tools/merge.py                          # 合并所有已完成册
cd .. && python3 scripts/run_from_map.py --try-run            # 端到端试跑
```

## 已完成样板册

- `kg-k1-full-math` 小班数学（12 点）— 幼儿园学段样板，用《指南》五大领域
- `primary-g1-s1-math` 一年级上册数学（20 点）— 小学学段样板，数学四领域完整填法
- `junior-g7-s1-chinese` 七年级上册语文（18 点）— 初中学段样板，语文六大任务群填法

这三册覆盖三个学段、三个学科，是后续逐册复制的黄金标准。

## 领域取值速查

- **数学**：数与代数 / 图形与几何 / 统计与概率 / 综合与实践
- **语文**：语言文字积累与梳理 / 实用性阅读与交流 / 文学阅读与创意表达 / 思辨性阅读与表达 / 整本书阅读 / 跨学科学习
- **英语**：主题 / 语篇 / 语言知识 / 文化知识 / 语言技能 / 学习策略
- **幼儿园**：健康 / 语言 / 社会 / 科学 / 艺术

（依据：教育部《义务教育课程标准（2022年版）》、《3-6岁儿童学习与发展指南》）
