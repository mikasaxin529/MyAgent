# 课程 JSON Schema 契约

> 内容生成层与渲染层（脚本）之间的契约。生成 JSON 时**必读本文档**，
> 确保每页元素类型合法、必填字段齐全。schema.py 会校验，不合法退出码 2。

## 顶层结构

```json
{
  "version": "1.0",
  "meta": { ... },
  "slides": [ ... ],
  "lessonPlan": { ... },
  "handout": { ... }   // 可选
}
```

- `slides` 驱动 .pptx 与 .html；
- `lessonPlan`(+`handout`) 驱动 .docx 教案；
- 一份 JSON 出三份成品，天然分离。

## meta（元数据）

| 字段 | 必填 | 说明 |
|------|:----:|------|
| title | ✅ | 课文名，如"坐井观天" |
| grade | ✅ | 年级 1-6（int） |
| lessonType | ✅ | 课型：`精读`/`识字写字`/`古诗词`/`口语交际习作` |
| stage | 自动 | 学段，由 grade 派生：低段(1-2)/中段(3-4)/高段(5-6) |
| textbook | ✅ | 教材版本，如"部编版二年级上册" |
| periods | - | 总课时数（精读=2，其他 1-2） |
| coreCompetencies | - | 四大素养（默认全填） |
| objectives | ✅ | 教学目标数组，见下 |
| keyPoints | - | 教学重点 |
| difficulties | - | 教学难点 |

## objectives[]（教学目标）

```json
{ "content": "认读 10 个生字，会写 4 个字", "competency": "语言运用", "dimension": "知识与技能" }
```

- `competency` **必填**，取四素养之一：文化自信 / 语言运用 / 思维能力 / 审美创造（强制对齐新课标）。
- `dimension` 可选，取三维目标之一：知识与技能 / 过程与方法 / 情感态度价值观。
- 每个目标必须标 `competency`；docx 输出可按学校习惯切换标签格式。

## slides[]（每页 frame）

```json
{ "id": "s03", "kind": "word-cards", "title": "识字 · 生字朋友",
  "layout": "cards", "period": 1,
  "elements": [ ... ] }
```

- `id`：页码（s1, s2...），缺省自动补 `s{i+1}`。
- `kind`：栏目类型，用于标识（cover/word-cards/revision/精读品析/...）。
- `period`：所属课时（精读分 1/2；其他默认 1）。渲染器据此分文件输出并在封面标注"第X课时"。
- `elements`：本页内容元素数组，每个有 `type` 字段决定渲染方式。

## 元素类型全集（elements[].type）

### 通用文本

| type | 字段 | 示例 |
|------|------|------|
| heading | content, size(h1/h2/h3) | `{"type":"heading","content":"我会认","size":"h1"}` |
| paragraph | content, emphasize? | `{"type":"paragraph","content":"青蛙坐在井里。","emphasize":[{"start":0,"end":2}]}` |
| list | items[], ordered? | `{"type":"list","items":["水井","井底之蛙"],"ordered":false}` |
| quote | content, source? | `{"type":"quote","content":"天不过井口那么大","source":"青蛙"}` |
| table | headers[], rows[][] | `{"type":"table","headers":["字","音","义"],"rows":[["井","jǐng","水井"]]}` |

### 语文专用

| type | 字段 | 说明 |
|------|------|------|
| word-card | cards[] | 生字卡数组：`{char,pinyin,radical,strokes,strokeOrder,groups,sentence}` |
| ruby-line | text, ruby | 整行注音：text=汉字串，ruby=空格分隔拼音 |
| poem | stanzas[], title?, author? | 古诗：`{lines:[{text,ruby}]}` 每行带注音 |
| strokes | char, strokeOrder[] | 笔顺：笔画名数组如 ["横","横","撇","竖"] |
| revision | chars[] | 写字指导：`{char,pinyin,易错点,运笔要点}` + 田字格 |
| board | title, structure[] | 板书：`{node, children?[...]}` 树形 |
| discussion | question, hint?, form | 课堂互动：form=同桌互说/小组讨论/开火车/全班交流 |
| evaluation | rubric[] | 评价量表：`{criterion, levels:[{star,desc}]}` |

### 媒体/辅助

| type | 字段 | 说明 |
|------|------|------|
| image | src, caption? | 图片：src 为相对路径或 URL |
| note | content | 教师备注，HTML 默认隐藏可点开 |
| divider | - | 分隔页/过渡 |

## word-card.cards[] 字段

```json
{ "char": "井", "pinyin": "jǐng", "radical": "一", "strokes": 4,
  "strokeOrder": ["横","横","撇","竖"],
  "groups": ["水井","井底之蛙"], "sentence": "青蛙坐在井里看天。" }
```

## lessonPlan（教案，docx 数据源）

```json
{
  "title": "坐井观天（第 1 课时）",
  "base": { "textbook": "", "grade": "", "periods": "", "lessonType": "" },
  "objectives": [ { "content": "...", "competency": "...", "dimension": "..." } ],
  "keyPoints": [], "difficulties": [],
  "teachingProcess": [
    { "phase": "复习导入", "duration": "5分钟",
      "activities": [ { "teacher": "...", "student": "..." } ],
      "design": "设计意图：..." }
  ],
  "boardDesign": { "structure": "..." },
  "homework": { "levels": [ {"level":"基础","items":[]}, {"level":"提升","items":[]}, {"level":"拓展","items":[]} ] },
  "reflection": ""
}
```

教案 10 模块：课题名称 / 教材版本 / 教学目标 / 教学重难点 / 教学准备 / 课时安排 /
教学过程（分课时，每环节含教师活动+学生活动+设计意图）/ 板书设计 / 作业设计（分层三级）/ 教学反思。

## handout（分层作业，可选）

```json
{ "levels": [ {"level": "基础", "items": ["抄写生字","朗读课文"]},
              {"level": "拓展", "items": ["仿写句子"]} ] }
```

作业分层三级：基础类（抄写/朗读/背诵）+ 拓展类（仿写/思维导图/阅读链接）+ 挑战类（查资料/续编/动手制作）。

## 自检清单（生成后必过）

- [ ] lessonType 在枚举内
- [ ] 每个 objectives[].competency 是四素养之一
- [ ] 每页 elements[].type 在全集内
- [ ] 精读课 slides 按 period 1/2 分两课时
- [ ] 生字卡的 pinyin 声调标注正确（ā á ǎ à）
- [ ] 内容密度参照示例 JSON，不偷工减料
