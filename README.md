# K12 题库工厂 (k12-question-bank)

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![进度](https://img.shields.io/badge/知识点地图-63%2F63%20%C2%B7%201225%20%E7%9F%A5%E8%AF%86%E7%82%B9-brightgreen)

> 一套**完全自研、配置驱动、可复用**的 K12 选择题生产流水线：依据 2022 版国家课程标准独立梳理知识点地图，再用「模板 + LLM」双引擎批量生成、质检、导出。
> 零第三方依赖（Python 3.9+ 开箱即用），不依赖任何外部项目的数据或代码。

## 项目定位

本项目从零开始构建，目标是工程化地生产大规模、高质量、结构化的 K12 选择题。核心理念是**源头治理**：先把知识点按权威课标梳理干净、结构化，再用代码管道批量出题——而不是逐题手工堆砌。

整个系统由两层构成：

1. **知识点地图（`knowledge-map/`）**：覆盖幼儿园 / 小学 / 初中 × 语文 / 数学 / 英语，共 63 册，每条知识点带课标领域、学习目标、难度、认知层级、前置关系、易错点、权威来源等 15 个字段。这是全项目唯一的知识点数据源。
2. **生成管道（`src/` + `scripts/`）**：读取知识点地图，用双引擎出题，经质量门禁校验后入库，导出为多种格式。

## 核心设计

| 特性 | 说明 |
|---|---|
| **知识点地图先行** | 63 册 / 1225 个知识点（幼儿园 9 + 小学 36 + 初中 18）已全部完成，依据国家课标独立梳理 |
| **双引擎 + 母题变体** | 规则类科目（数学/英语/语文基础）走模板引擎，成本≈0；语义类走 LLM 生成母题，程序化扩展 1→8，把 API 调用压到 1/8 |
| **确定性 ID** | `id = sha256(topic_id + variant + seed)`，重跑幂等，断点续传不重复扣费 |
| **质量门禁** | 结构校验 → 答案一致性 → 语义去重（字符 n-gram Jaccard）→ 难度校准 → 抽检 |
| **配置驱动** | 年级、科目、配额、难度分布、模型、并发、导出格式全在 `config/config.yaml` |
| **可插拔** | 知识点源、生成引擎、LLM provider、导出器均为独立模块 |

## 快速开始

```bash
# 1. 查看进度、领取下一册梳理任务
python3 knowledge-map/tools/progress.py --next 5

# 2. 生成某册的梳理提示词，交给 AI 梳理并写入 volumes/
python3 knowledge-map/tools/progress.py --prompt primary-g2-s1-math

# 3. 校验 → 合并 → 生成题目（端到端）
python3 knowledge-map/tools/validate.py
python3 knowledge-map/tools/merge.py
python3 scripts/run_from_map.py --try-run          # 试跑：仅模板引擎，无需 API key
python3 scripts/run_from_map.py --per-topic 24 --subject math   # 按科目生成

# 4. 全量生成（语义类题目需配 LLM key）
export DEEPSEEK_API_KEY=sk-xxxx
python3 scripts/run_full.py --target 100000 --resume

# 5. 题库质量验证
python3 scripts/verify.py
```

> **注意**：LLM key 从环境变量读取，绝不落盘。provider 按 OpenAI 兼容格式编写，换 Qwen / 豆包只改 `base_url` 与 `model`。

## 知识点体系

依据 2022 版义务教育课程标准（已联网核准）：

- **数学**：数与代数 / 图形与几何 / 统计与概率 / 综合与实践
- **语文**：六大学习任务群（语言文字积累与梳理 / 实用性阅读与交流 / 文学阅读与创意表达 / 思辨性阅读与表达 / 整本书阅读 / 跨学科学习）
- **英语**：六要素（主题 / 语篇 / 语言知识 / 文化知识 / 语言技能 / 学习策略）× 三主题范畴
- **学段**：S1(1-2年级) / S2(3-4) / S3(5-6) / S4(7-9)
- **幼儿园**：《3-6岁儿童学习与发展指南》五大领域（健康 / 语言 / 社会 / 科学 / 艺术）

单条知识点含 15 个字段，详见 [SCHEMA.md](knowledge-map/SCHEMA.md)。逐册梳理工作流见 [knowledge-map/README.md](knowledge-map/README.md)。

## 题目 Schema

统一结构，所有导出格式（JSONL / App JSON / CSV）都从它派生：

```json
{
  "id": "a1b2c3...",
  "topic_id": "math_g6_shanxing",
  "grade": "六年级上", "subject": "数学", "topic": "扇形统计图",
  "difficulty": 2, "bloom": "理解", "type": "single_choice",
  "stem": "某部分占整体的30%，对应扇形圆心角是多少度？",
  "options": ["30°", "108°", "90°", "72°"],
  "answer": "B",
  "explanation": "圆心角 = 360° × 30% = 108°",
  "source": "template|llm", "generator_version": "v1.0",
  "quality": {"dedup_hash": "...", "qa_passed": true}
}
```

## 目录结构

```
k12-question-bank/
├── config/config.yaml          # 全局配置
├── src/                        # 生成管道核心
│   ├── schema.py               # 统一题目 Schema
│   ├── config.py / storage.py  # 配置加载 / SQLite 断点续传
│   ├── quality.py              # 质量门禁 + 语义去重
│   ├── generators/             # base / template / llm / providers
│   ├── exporters.py            # JSONL / App JSON / CSV 导出
│   └── pipeline.py             # 主调度
├── knowledge-map/              # 权威知识点地图（唯一数据源）
│   ├── SCHEMA.md               # 知识点字段规范
│   ├── taxonomy/               # 课标领域 + 63册清单
│   ├── data/                   # 各册知识点定义（元组式，人写这一层）
│   ├── volumes/                # 生成的分册知识点 JSON（分治单位）
│   ├── tools/                  # build_volumes / validate / merge / progress
│   └── README.md
├── scripts/                    # run_from_map / run_full / verify
├── data/                       # 运行时数据（.db 已 gitignore）
└── output/                     # 导出样例（pilot / from_map）
```

## 当前进度

**知识点地图：63 / 63 册全部完成，共 1225 个知识点**

| 学段 | 语文 | 数学 | 英语 | 小计 |
|---|---:|---:|---:|---:|
| 幼儿园（小/中/大班） | 36 | 42 | 30 | 108 |
| 小学（1—6 年级） | 264 | 242 | 192 | 698 |
| 初中（7—9 年级） | 156 | 144 | 119 | 419 |
| **合计** | **456** | **428** | **341** | **1225** |

- 结构校验：`validate.py` 报 **0 错误 / 0 警告**（领域合法性、前置引用、ID 唯一、字段完整、难度与认知层级一致）
- **交叉验证**：与教育部课标全文逐条比对，156 条高风险事实点联网深查，修正后 `cross_validate.py` 报 **0 错误 / 0 警告**（详见 `knowledge-map/CROSS_VALIDATION.md`）
- 知识点分布：每册 10—27 个，平均 19.4 个；全部 63 册均有内容，无空壳
- 端到端验证：知识点地图 → 864 题，选项数、答案、解析结构 100% 完整

### 已知限制（下一步要解决）

模板引擎的题库是硬编码词表，**理论上限约 995 道不重复题**，无法支撑 10 万题目标。
数学类因参数化（数值随机）尚可扩量，语文/英语的拼音、成语、词汇、语法题型会大量重复并被去重门禁拦下。
规模化必须依赖 LLM 引擎——地图已把 764 个语义类知识点路由到 `llm`、458 个规则类路由到 `template`，
方向是对的，缺的是给模板引擎换上「读知识点内容」的内容感知型生成器。

## 路线图

- [x] 按册补齐全部 63 册知识点地图
- [ ] 内容感知型模板生成器（读取 knowledge point 的 name/concept/mistakes 出题）
- [ ] 接入 LLM 引擎并抽检语义类题目质量
- [ ] 补量至 10 万题 + 全库质量报告

## License

[MIT](LICENSE) © 2026 mtvtopcom
