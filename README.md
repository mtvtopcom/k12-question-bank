# K12 题库工厂 (k12-question-bank)

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![进度](https://img.shields.io/badge/知识点地图-3%2F63-orange)

> 一套**配置驱动、可复用**的 K12 选择题生产流水线：先用权威课标建知识点地图，再用「模板 + LLM」双引擎批量生成、质检、导出。
> 零第三方依赖（Python 3.9+ 开箱即用），知识点源可插拔，输出可适配多种 App / 试卷格式。

## 为什么做这个项目

参考项目 `RedSamerai/learning-content` 提供了 K12 年级 × 科目 × 知识点的目录骨架，但实测发现三个问题：

1. **它不是题库**：4267 个内容文件里 `practice / question / answer` 字段出现次数是 **0**，只有讲解文本和音频；
2. **知识点有脏数据**：260 条串科数据（如「语文-数学广角」）、大量模板套话讲解，直接拿来出题会把错误放大 10 万倍；
3. **脚本不可复用**：路径写死 Windows D 盘，且只做讲解+音频，没有题目生成逻辑。

所以本项目做两件事：**① 先把知识点源头做扎实**（基于 2022 版国家课标梳理 63 册知识点地图）；**② 再用工程化管道批量出题**（而不是靠对话式 AI 一笔笔攒）。

## 核心设计

| 特性 | 说明 |
|---|---|
| **知识点地图先行** | 63 册权威知识点（幼儿园 9 + 小学 36 + 初中 18），每条带课标领域、学习目标、前置关系、易错点、权威来源 |
| **双引擎 + 母题变体** | 规则类科目（数学/英语/语文基础）走模板引擎，成本≈0；语义类走 LLM 生成母题，程序化扩展 1→8，把 API 调用压到 1/8 |
| **确定性 ID** | `id = sha256(topic_id + variant + seed)`，重跑幂等，断点续传不重复扣费 |
| **质量门禁** | 结构校验 → 答案一致性 → 语义去重（字符 n-gram Jaccard）→ 难度校准 → 抽检 |
| **配置驱动** | 年级、科目、配额、难度分布、模型、并发、导出格式全在 `config/config.yaml` |
| **可插拔** | 知识点源、生成引擎、LLM provider、导出器均为独立模块 |

## 快速开始

### 推荐路径：知识点地图 → 题目

```bash
# 1. 查看进度、领取下一册梳理任务
python3 knowledge-map/tools/progress.py --next 5

# 2. 生成某册的梳理提示词，交给 AI 梳理并写入 volumes/
python3 knowledge-map/tools/progress.py --prompt primary-g2-s1-math

# 3. 校验 → 合并 → 生成题目（端到端）
python3 knowledge-map/tools/validate.py
python3 knowledge-map/tools/merge.py
python3 scripts/run_from_map.py --try-run          # 试跑
python3 scripts/run_from_map.py --target 100000    # 全量（语义类需配 LLM key）
```

### 旧路径：清洗参考项目 → 出题

```bash
git clone --depth 1 https://github.com/RedSamerai/learning-content.git /tmp/learning-content-ref
python3 scripts/clean_topics.py        # 清洗 → data/topics.jsonl
python3 scripts/run_pilot.py           # 数学 500 题试点（无需 API key）
python3 scripts/verify.py              # 质量报告
export DEEPSEEK_API_KEY=sk-xxxx        # 全量语义类题目需要
python3 scripts/run_full.py --target 100000 --resume
```

> **注意**：LLM key 从环境变量读取，绝不落盘。provider 按 OpenAI 兼容格式编写，换 Qwen / 豆包只改 `base_url` 与 `model`。

## 知识点体系

按 2022 版义务教育课程标准（已联网核准）：

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
│   ├── cleaner.py / quality.py # 知识点清洗 / 质量门禁
│   ├── generators/             # base / template / llm / providers
│   ├── exporters.py            # JSONL / App JSON / CSV 导出
│   └── pipeline.py             # 主调度
├── knowledge-map/              # 【推荐入口】63册知识点地图
│   ├── SCHEMA.md               # 知识点字段规范
│   ├── taxonomy/               # 课标领域 + 63册清单
│   ├── volumes/                # 逐册知识点（分治单位）
│   ├── tools/                  # 校验 / 合并 / 进度
│   └── README.md
├── scripts/                    # clean_topics / run_pilot / run_full / run_from_map / verify
├── data/                       # 运行时数据（.db / topics.jsonl 已 gitignore）
└── output/                     # 导出样例（pilot / from_map）
```

## 当前进度

- 知识点地图：**3 / 63 册**（幼儿园小班数学、一年级上数学、七年级上语文），共 50 个知识点样板
- 端到端验证：知识点地图 → 56 题，100% 结构完整、0 重复
- 试点题库：数学 500 题目标 → 实际 328 题（模板去重后），结构/答案/解析 100% 通过

## 路线图

- [ ] 按册补齐剩余 60 册知识点地图
- [ ] 扩充模板题型（数学应用题、英语语法、语文文言文）
- [ ] 接入 LLM 引擎并抽检语义类题目质量
- [ ] 补量至 10 万题 + 全库质量报告

## License

[MIT](LICENSE) © 2026 mtvtopcom
