# 题目输出格式说明（对接文档）

> 适用版本：**v1.3.5**　最后更新：2026-11-23
>
> 本文档面向要把「K12 题库工厂」产出接入自己系统的开发者，说明 **大模型出题提示词要求返回的 4 种格式**（JSON / TXT / Excel / CSV），以及 **仓库自带的导出格式**（JSONL / CSV / App JSON）。
>
> 配套文件：[JSON Schema](question-output.schema.json) · [示例文件](samples/) · [提示词实现](../desktop/src/prompt.py) · [统一题目 Schema](../src/schema.py)

---

## 目录

1. [两种数据出口](#1-两种数据出口)
2. [字段字典](#2-字段字典四种格式共用)
3. [JSON 格式](#3-json-格式)
4. [TXT 格式](#4-txt-格式)
5. [Excel 格式（TSV）](#5-excel-格式tsv)
6. [CSV 格式](#6-csv-格式)
7. [仓库导出格式（JSONL / CSV / App JSON）](#7-仓库导出格式)
8. [格式换算与入库](#8-格式换算与入库)
9. [接入校验清单](#9-接入校验清单)
10. [版本与兼容性](#10-版本与兼容性)

---

## 1. 两种数据出口

| 出口 | 由谁产生 | 字段风格 | 适用场景 |
|---|---|---|---|
| **AI 返回格式**（JSON / TXT / Excel / CSV） | 你把桌面端生成的提示词粘贴到 DeepSeek / 元宝 / ChatGPT 后，模型按指令输出 | `topic_id` / `topic_name` / `option_a`… | 把 AI 生成的题目批量导入自有题库、组卷系统、App |
| **仓库导出格式**（JSONL / CSV / App JSON） | `src/exporters.py` 读取 SQLite 题库后导出 | `id` / `grade` / `subject` / `topic` / `source`… | 直接消费管道产物，或作为入库目标格式 |

两者字段不同，**入库前需要做一次字段映射**，见 [第 8 节](#8-格式换算与入库)。

---

## 2. 字段字典（四种格式共用）

| 字段 | 类型 | 必填 | 说明 |
|---|---|:--:|---|
| `topic_id` | string | ✅ | 知识点 ID，如 `primary-g3-s1-math-K018`。用于溯源到 `knowledge-map/volumes/` 里的知识点 |
| `topic_name` | string | ✅ | 知识点名称，如「长方形、正方形的特征与周长」 |
| `stem` | string | ✅ | 题干，**不含选项**、不含「1.」等题号 |
| `options` | array[string] | ✅（仅 JSON） | 4 个选项的文本，顺序即 A/B/C/D，**不含「A.」前缀** |
| `option_a` … `option_d` | string | ✅（表格类） | 表格格式里的 4 个选项列，同样不含前缀 |
| `answer` | string | ✅ | 正确答案字母，只能是 `A` / `B` / `C` / `D` |
| `explanation` | string | ✅ | 解析：正确项为什么对、干扰项为什么错 |
| `difficulty` | integer | ✅ | 难度，只能是 `1` / `2` / `3` / `4`（见下表） |
| `bloom` | string | ✅ | 认知层级，只能是 `记忆` / `理解` / `应用` / `分析` / `评价` / `创造` |
| `type` | string | ✅ | 题型，当前固定 `single_choice`（单选） |

**难度取值**

| 值 | 界面显示 | 课标描述 | 含义 |
|:--:|---|---|---|
| 1 | 容易 | 基础识记 | 直接回忆、辨认 |
| 2 | 中等 | 理解应用 | 理解概念并在熟悉情境中使用 |
| 3 | 较难 | 分析综合 | 多步推理、综合运用 |
| 4 | 难 | 挑战探究 | 开放情境、迁移创新 |

> 说明：大模型偶尔会把 `difficulty` 写成字符串 `"2"`，入库前请统一 `int()`；`bloom` 必须是上面 6 个中文值之一。

---

## 3. JSON 格式

### 3.1 结构

- 顶层是 **JSON 数组**，每个元素是一道题；不要包在对象里，不要 Markdown 代码块。
- 选项用 `options` 数组（长度固定 4），不要同时给 `option_a` 等字段。
- 字符串内的换行写成 `\n`；中文标点保持原样。

### 3.2 示例

```json
[
  {
    "topic_id": "primary-g3-s1-math-K018",
    "topic_name": "长方形、正方形的特征与周长",
    "stem": "一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？",
    "options": ["26 米", "13 米", "40 米", "21 米"],
    "answer": "A",
    "explanation": "绕一圈即周长，(8+5)×2=26 米。13 米只是长加宽，忘了乘 2；40 米是误把长乘宽当作周长。",
    "difficulty": 2,
    "bloom": "应用",
    "type": "single_choice"
  },
  {
    "topic_id": "primary-g3-s1-chinese-K006",
    "topic_name": "成语的积累与运用",
    "stem": "下面哪个成语的意思是「做了多余的事，反而不恰当」？",
    "options": ["画蛇添足", "锦上添花", "雪中送炭", "一举两得"],
    "answer": "A",
    "explanation": "画蛇添足比喻做了多余的事反而把事情弄坏；锦上添花指好上加好，雪中送炭指及时帮助，一举两得指做一件事得到两方面好处。",
    "difficulty": 2,
    "bloom": "理解",
    "type": "single_choice"
  }
]
```

### 3.3 解析（Python）

```python
import json, re

def parse_json_output(text: str) -> list[dict]:
    t = text.strip()
    # 模型有时会包 ```json ... ```，先剥掉
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("["), t.rfind("]")
    if start < 0 or end < start:
        raise ValueError("未找到 JSON 数组")
    items = json.loads(t[start:end + 1])
    for q in items:
        assert len(q["options"]) == 4, q
        assert q["answer"] in "ABCD", q
        q["difficulty"] = int(q["difficulty"])
    return items
```

### 3.4 解析（JavaScript / Node）

```js
function parseJsonOutput(text) {
  const cleaned = text.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const start = cleaned.indexOf("["), end = cleaned.lastIndexOf("]");
  if (start < 0 || end < start) throw new Error("未找到 JSON 数组");
  return JSON.parse(cleaned.slice(start, end + 1)).map(q => ({
    ...q,
    difficulty: Number(q.difficulty),
  }));
}
```

### 3.5 注意事项

- `options` 顺序就是 A/B/C/D；**不要**在选项文本里再带「A.」。
- 模型可能输出 `null`、尾部逗号或注释（非严格 JSON），生产环境建议用宽容解析器（如 `json5`）或先按 [JSON Schema](question-output.schema.json) 校验再报错。
- 批量提示词（多个知识点）仍然返回**一个扁平数组**，用 `topic_id` 区分来源。

---

## 4. TXT 格式

### 4.1 结构

题与题之间以空行分隔；每道题固定 6 行，顺序不可变：

```
第 1 题
【题干】一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？
【选项】A. 26 米　B. 13 米　C. 40 米　D. 21 米
【答案】A
【解析】绕一圈即周长，(8+5)×2=26 米。13 米只是长加宽，忘了乘 2；40 米是误把长乘宽当作周长。
【难度】中等
【知识点】长方形、正方形的特征与周长（primary-g3-s1-math-K018）

第 2 题
【题干】下面哪个成语的意思是「做了多余的事，反而不恰当」？
【选项】A. 画蛇添足　B. 锦上添花　C. 雪中送炭　D. 一举两得
【答案】A
【解析】画蛇添足比喻做了多余的事反而把事情弄坏；锦上添花指好上加好，雪中送炭指及时帮助，一举两得指做一件事得到两方面好处。
【难度】中等
【知识点】成语的积累与运用（primary-g3-s1-chinese-K006）
```

### 4.2 解析（Python）

```python
import re

TAGS = ("题干", "选项", "答案", "解析", "难度", "知识点")

def parse_txt_output(text: str) -> list[dict]:
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for block in blocks:
        q = {"type": "single_choice"}
        m = re.search(r"^第\s*(\d+)\s*题\s*$", block, re.M)
        if not m:
            continue
        for tag in TAGS:
            mm = re.search(rf"【{tag}】([\s\S]*?)(?=\n【[^】]+】|\Z)", block)
            q[tag] = mm.group(1).strip() if mm else ""
        # 选项：A. xxx　B. xxx　C. xxx　D. xxx（分隔符可能是全角空格、空格或多个空格）
        opts = re.split(r"\s*[A-D][.．、]\s*", q["选项"])
        opts = [o.strip("　 \t") for o in opts if o.strip("　 \t")]
        q["options"] = opts
        q["stem"] = q.pop("题干")
        q["explanation"] = q.pop("解析")
        q["answer"] = q.pop("答案").strip()[:1].upper()
        q["difficulty"] = {"容易": 1, "中等": 2, "较难": 3, "难": 4}[q.pop("难度").strip()]
        kp = q.pop("知识点")                      # 「名称（topic_id）」
        km = re.search(r"[（(]([^（）()]+)[）)]\s*$", kp)
        q["topic_id"] = km.group(1) if km else ""
        q["topic_name"] = re.sub(r"[（(][^（）()]+[）)]\s*$", "", kp).strip()
        out.append(q)
    return out
```

### 4.3 注意事项

- 选项之间的分隔符是**全角空格 U+3000**（示例里 `26 米　B.` 中间那个），解析时按 `[A-D][.．、]` 切分更稳。
- 【解析】里可能包含换行，解析到下一个 `【…】` 或文本结尾为止。
- 【知识点】行的格式是 `名称（topic_id）`，括号可能是全角 `（）`，也可能是半角 `()`。
- TXT 适合人工阅读、贴进 Word；程序消费优先用 JSON / CSV。

---

## 5. Excel 格式（TSV）

### 5.1 为什么是 TSV

大模型不能直接生成二进制 `.xlsx`。桌面端选择「Excel」时，提示词要求模型输出**制表符分隔的表格（TSV）**，你复制整段内容到 Excel 的 A1 单元格，Excel 会自动按列拆分，再「另存为 .xlsx」即可。

### 5.2 结构

第一行是表头，其后每行一道题，共 12 列，顺序固定：

```
topic_id	topic_name	stem	option_a	option_b	option_c	option_d	answer	explanation	difficulty	bloom	type
primary-g3-s1-math-K018	长方形、正方形的特征与周长	一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？	26 米	13 米	40 米	21 米	A	绕一圈即周长，(8+5)×2=26 米。13 米只是长加宽，忘了乘 2；40 米是误把长乘宽当作周长。	2	应用	single_choice
primary-g3-s1-chinese-K006	成语的积累与运用	下面哪个成语的意思是「做了多余的事，反而不恰当」？	画蛇添足	锦上添花	雪中送炭	一举两得	A	画蛇添足比喻做了多余的事反而把事情弄坏；锦上添花指好上加好，雪中送炭指及时帮助，一举两得指做一件事得到两方面好处。	2	理解	single_choice
```

> 上面的空白是 **Tab（`\t`）**，不是空格。可直接下载 [`samples/questions.tsv`](samples/questions.tsv) 查看。

### 5.3 导入 Excel

**手工**：全选模型输出 → 复制 → Excel 选中 A1 → 粘贴 → 确认 12 列 → 「另存为」→ 格式选 `.xlsx`。

**程序**：

```python
import pandas as pd

df = pd.read_csv("questions.tsv", sep="\t", dtype={"difficulty": int}, encoding="utf-8")
print(df.columns.tolist())   # 12 列，顺序与表头一致
```

```python
# 不装 pandas 也能读
import csv
with open("questions.tsv", encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f, delimiter="\t"))
```

### 5.4 注意事项

- 字段内**不能出现 Tab**；解析里也不要换行，否则会多出一行、列数错位。
- 不要用引号包裹字段（与 CSV 不同）；如果内容本身含双引号，直接保留。
- Excel 可能把 `difficulty` 识别成数字、把 `topic_id` 识别成文本，导入数据库前显式转型。
- 若模型仍输出了 Markdown 表格（`| ... |`），按 `|` 切分并跳过 `---` 行即可容错。

---

## 6. CSV 格式

### 6.1 结构

遵循 RFC 4180：第一行表头，逗号分隔，共 12 列，列名与 TSV 完全相同。

```csv
topic_id,topic_name,stem,option_a,option_b,option_c,option_d,answer,explanation,difficulty,bloom,type
primary-g3-s1-math-K018,长方形、正方形的特征与周长,"一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？",26 米,13 米,40 米,21 米,A,"绕一圈即周长，(8+5)×2=26 米。13 米只是长加宽，忘了乘 2；40 米是误把长乘宽当作周长。",2,应用,single_choice
primary-g3-s1-chinese-K006,成语的积累与运用,"下面哪个成语的意思是「做了多余的事，反而不恰当」？",画蛇添足,锦上添花,雪中送炭,一举两得,A,"画蛇添足比喻做了多余的事反而把事情弄坏；锦上添花指好上加好，雪中送炭指及时帮助，一举两得指做一件事得到两方面好处。",2,理解,single_choice
```

### 6.2 解析

```python
import csv

with open("questions.csv", encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))
for r in rows:
    r["difficulty"] = int(r["difficulty"])
    r["options"] = [r["option_a"], r["option_b"], r["option_c"], r["option_d"]]
```

```js
// Node.js 18+（需 npm i csv-parse）
import { parse } from "csv-parse/sync";
import fs from "node:fs";

const rows = parse(fs.readFileSync("questions.csv", "utf8"), {
  columns: true,
  skip_empty_lines: true,
  bom: true,
});
```

### 6.3 注意事项

- 字段含 **英文逗号 `,`、双引号 `"` 或换行**时，整个字段用双引号包裹；字段内的 `"` 写成 `""`。
- 中文逗号 `，` 不是分隔符，不需要转义。
- 给 Excel 直接双击打开时，请使用 **UTF-8 with BOM**（`utf-8-sig`），否则中文可能乱码；程序读取用 `utf-8` 或 `utf-8-sig` 都行。
- `answer` 只填字母；不要写「A. 26 米」或选项全文。

---

## 7. 仓库导出格式

管道跑完后由 `src/exporters.py` 写出，位于 `output/`（该目录已在 `.gitignore` 中，不随仓库分发）。

### 7.1 JSONL（`questions.jsonl`）

每行一个 JSON 对象，字段固定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 题目 ID，`sha256(topic_id|variant|seed|source)[:20]`，重跑幂等 |
| `grade` | string | 年级，如 `三年级上`、`小班` |
| `subject` | string | 学科代码：`chinese` / `math` / `english` |
| `topic` | string | 知识点名称 |
| `difficulty` | int | 1—4 |
| `bloom` | string | 记忆 / 理解 / 应用 / 分析 / 评价 / 创造 |
| `stem` | string | 题干 |
| `options` | array[string] | 4 个选项（不含前缀） |
| `answer` | string | `A`—`D` |
| `answer_text` | string | 正确选项原文 |
| `explanation` | string | 解析 |
| `source` | string | `template` / `llm` |
| `chapter` | string | 章节 / 单元 |

示例见 [`samples/questions.export.jsonl`](samples/questions.export.jsonl)。

### 7.2 CSV（`questions.csv`）

列：`id,grade,subject,topic,difficulty,bloom,stem,options,answer,answer_text,explanation,source,chapter`

- 编码 **UTF-8 with BOM**，Excel 双击不乱码。
- `options` 不是 4 列，而是合并成一列，格式为 `A. 26 米 | B. 13 米 | C. 40 米 | D. 21 米`（` | ` 分隔）。

### 7.3 App JSON（`app_import/`）

按 `年级/科目/知识点.json` 分目录，面向学习类 App：

```json
{
  "id": "三年级上_math_长方形、正方形的特征与周长",
  "grade": "三年级上",
  "subject": "math",
  "topic": "长方形、正方形的特征与周长",
  "chapter": "图形与几何",
  "practice": [
    {
      "id": "a1b2c3d4e5f6a7b8c9d0",
      "type": "multiple_choice",
      "difficulty": 2,
      "question": "一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？",
      "options": ["26 米", "13 米", "40 米", "21 米"],
      "answer": "A",
      "answer_text": "26 米",
      "explanation": "绕一圈即周长，(8+5)×2=26 米。",
      "feedback": {
        "correct": "回答正确，继续保持！",
        "incorrect": "再想想，正确答案是 A. 26 米"
      }
    }
  ]
}
```

---

## 8. 格式换算与入库

把 **AI 返回**（`topic_id` / `option_a`…）映射成 **仓库导出**（`id` / `grade` / `subject` / `topic`…）：

```python
import hashlib

def make_id(topic_id: str, variant_index: int = 1, seed: int = 42, source: str = "llm") -> str:
    """与 src/schema.py 一致：同样输入永远得到同样 id，重跑幂等。"""
    raw = f"{topic_id}|{variant_index}|{seed}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

def to_bank_row(q: dict, grade: str, subject: str, chapter: str = "",
                variant_index: int = 1, seed: int = 42) -> dict:
    opts = q.get("options") or [q["option_a"], q["option_b"], q["option_c"], q["option_d"]]
    letter = q["answer"].strip().upper()
    idx = "ABCD".index(letter)
    return {
        "id": make_id(q["topic_id"], variant_index, seed, "llm"),
        "grade": grade,              # 如「三年级上」，来自知识点所属册
        "subject": subject,          # chinese / math / english
        "topic": q["topic_name"],
        "difficulty": int(q["difficulty"]),
        "bloom": q["bloom"],
        "stem": q["stem"].strip(),
        "options": opts,
        "answer": letter,
        "answer_text": opts[idx],
        "explanation": q["explanation"],
        "source": "llm",
        "chapter": chapter,
    }
```

知识点所属的 `grade` / `subject` / `chapter` 可从 `knowledge-map/volumes/**/*.json` 按 `topic_id` 反查：

```python
import json, pathlib

GRADE_CN = {"k1": "小班", "k2": "中班", "k3": "大班",
            "g1": "一年级", "g2": "二年级", "g3": "三年级",
            "g4": "四年级", "g5": "五年级", "g6": "六年级",
            "g7": "七年级", "g8": "八年级", "g9": "九年级"}
TERM_CN = {"s1": "上册", "s2": "下册", "full": ""}

index = {}
for p in pathlib.Path("knowledge-map/volumes").rglob("*.json"):
    vol = json.loads(p.read_text(encoding="utf-8"))
    grade_label = GRADE_CN.get(vol["grade"], vol["grade"]) + TERM_CN.get(vol.get("term"), "")
    for kp in vol["knowledge_points"]:
        index[kp["id"]] = {
            "grade": grade_label,
            "subject": vol["subject"],
            "chapter": kp.get("unit", ""),
        }
```

> 册文件字段：`volume_id` / `title` / `stage` / `subject` / `grade`（如 `g3`）/ `term`（`s1`/`s2`/`full`）/ `knowledge_points`；年级中文名需按上表换算。

### 推荐入库流程

```
AI 文本 → 按所选格式解析 → JSON Schema / 字段校验 → 去重 → 补 grade/subject/chapter → 写库
```

---

## 9. 接入校验清单

导入前逐条检查，可避免 90% 的脏数据：

- [ ] 顶层是数组（JSON）或行数 = 题目数（表格）
- [ ] 每题 `options` / `option_a..d` **恰好 4 个**，互不重复，非空
- [ ] `answer ∈ {A,B,C,D}`，且对应选项存在
- [ ] `difficulty ∈ {1,2,3,4}`（数字或可转数字的字符串）
- [ ] `bloom ∈ {记忆,理解,应用,分析,评价,创造}`
- [ ] `topic_id` 能在 `knowledge-map/volumes/` 中找到，`topic_name` 与档案一致
- [ ] `stem` 非空且不含选项；`explanation` 非空
- [ ] 题干去重（字符 n-gram Jaccard 或 `sha256(stem)`）

可直接用 JSON Schema 校验 JSON 输出：

```python
import json, jsonschema

schema = json.load(open("docs/question-output.schema.json", encoding="utf-8"))
jsonschema.validate(json.load(open("docs/samples/questions.json", encoding="utf-8")), schema)
```

---

## 10. 版本与兼容性

| 版本 | 变化 |
|---|---|
| v1.3.5 | 首次随文档发布 4 种格式规范与 JSON Schema；表格固定 12 列；JSON 固定 9 个字段 |

- 字段只增不改：后续若增加字段，按「可选」处理，旧解析器可忽略未知字段。
- `type` 目前固定 `single_choice`；未来支持多选时新增 `multi_choice`，`answer` 可能变成 `"AC"`，解析器请按字符串处理。
- 文档与代码不一致时，以 [`desktop/src/prompt.py`](../desktop/src/prompt.py) 的 `_format_block()` 为准，并欢迎提 Issue。
