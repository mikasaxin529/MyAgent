# 四课型栏目序列（借鉴部编版真实课件结构）

> 栏目序列源自第一PPT、百度文库、国家中小学智慧教育平台、21世纪教育网的真实课件调研。
> 生成 JSON 时按对应课型的序列组织 slides，每个栏目对应一个 slide，kind 字段标注栏目类型。
> 每个栏目挂一个核心素养条目，写入对应元素的隐含语义。

## 精读课文（标准 2 课时）

精读是核心课型，**分两课时，栏目结构完全不同**。第一课时重识字初读，第二课时重精读品析。

### 第 1 课时（低段约 20-30 页）

| 序 | 栏目 | kind | 元素建议 | 素养 |
|:--:|------|------|----------|------|
| 1 | 封面 | cover | heading(课题)+meta+image | — |
| 2 | 目录 | toc | list(本课时栏目) | 思维能力 |
| 3 | 学习目标 | objectives | list(目标，标competency) | — |
| 4 | 情境导入 | intro | image+paragraph(图片/谜语/问题) | 审美创造 |
| 5-6 | 初读感知 | reading | ruby-line(注音课文全文)+paragraph(朗读要求) | 语言运用 |
| 7-9 | 会认字 | word-cards | word-card(拼音/部首/结构/组词/造句/形近字) | 语言运用 |
| 10-12 | 会写字 | writing | revision(田字格)+strokes(笔顺) | 审美创造 |
| 13 | 词语理解 | words | list/table(释义/近反义词) | 语言运用 |
| 14-15 | 再读梳理 | outline | paragraph(分段/脉络/主要内容) | 思维能力 |
| 16 | 课堂小结 | summary | paragraph | — |
| 17 | 基础作业 | homework | list(抄写/朗读) | 语言运用 |

### 第 2 课时（约 15-30 页）

| 序 | 栏目 | kind | 元素建议 | 素养 |
|:--:|------|------|----------|------|
| 1 | 复习导入 | review | list(生字听写)+paragraph(内容回顾) | 语言运用 |
| 2-7 | 精读品析 | analysis | paragraph(逐段/逐句)+discussion(关键句赏析)，**5-8 页** | 思维能力 |
| 8 | 合作探究 | discuss | discussion(核心问题+小组交流) | 思维能力 |
| 9-10 | 朗读指导 | read-guide | ruby-line/poem(感情读/分角色/配乐) | 语言运用 |
| 11 | 写法总结 | writing-method | list(写作手法/表达方法) | 审美创造 |
| 12 | 拓展延伸 | extend | quote(相关阅读)+paragraph(仿写/生活链接) | 文化自信 |
| 13 | 板书设计 | board | board(课文脉络结构图) | 审美创造 |
| 14 | 课堂练习 | practice | list/table(当堂检测) | 思维能力 |
| 15 | 分层作业 | homework | list(基础+拓展+选做) | 语言运用 |
| 16 | 结束页 | end | divider | — |

## 识字写字课（1-2 课时，约 20-40 页）

低年级为主。识字方法多样，多用游戏化巩固。

| 栏目 | kind | 元素建议 | 素养 |
|------|------|----------|------|
| 激趣导入 | intro | image/paragraph(情境) | 审美创造 |
| 认读字音 | word-cards | word-card(带拼音大字) | 语言运用 |
| 字形分析 | word-analyze | word-card(加一加/减一减/换一换/字理) | 思维能力 |
| 书写指导 | writing | revision(田字格)+strokes(笔顺) | 审美创造 |
| 组词造句 | words | list(组词)+paragraph(造句) | 语言运用 |
| 巩固游戏 | game | discussion(摘苹果/开火车/找朋友) | 思维能力 |
| 作业 | homework | list | 语言运用 |

## 古诗词课（1-2 课时）

注重朗读与背诵，多配乐、配图。

| 栏目 | kind | 元素建议 | 素养 |
|------|------|----------|------|
| 封面/导入 | cover/intro | image+paragraph(诗人背景/故事) | 文化自信 |
| 初读节奏 | read-rhythm | poem(注音+节奏/停顿标注) | 语言运用 |
| 诗意解析 | meaning | paragraph(逐句关键词) | 思维能力 |
| 悟情意象 | feeling | discussion+quote(背景/想象画面/情感) | 文化自信 |
| 背诵指导 | recite | ruby-line(填空背/拍手背/配乐背) | 语言运用 |
| 拓展对比 | extend | quote(对比阅读)+list(同类积累) | 文化自信 |
| 板书 | board | board(意象结构) | 审美创造 |

## 口语交际 / 习作（1 课时）

注重"导-写-评-改"闭环，有星级评价量表。

| 栏目 | kind | 元素建议 | 素养 |
|------|------|----------|------|
| 情境导入 | intro | image+discussion(创设情境) | 审美创造 |
| 目标明确 | objectives | list(交际/写作目标) | 语言运用 |
| 方法支架 | scaffold | list/table(思维导图/提纲/审题) | 思维能力 |
| 范文赏析 | model | paragraph(佳作)+quote(下水文) | 审美创造 |
| 小组活动 | activity | discussion(角色扮演/同桌对话) | 语言运用 |
| 星级评价 | evaluation | evaluation(星级量表) | 思维能力 |
| 当堂练习 | practice | paragraph(练笔) | 语言运用 |
| 成果展示 | show | paragraph(分享) | 文化自信 |

## 备注

- 课型序列是**骨架**，可按课文实际增删栏目、调整顺序，但精读两课时不可合并。
- 页数参照 `stages.md` 学段约束：低段页数反而更多（生字教学细致、图多）。
- 每个栏目标注的素养是隐含语义指导，不强制写入 JSON，但教学目标必须标 competency。
