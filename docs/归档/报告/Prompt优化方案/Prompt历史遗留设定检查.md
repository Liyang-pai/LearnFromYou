# Prompt 历史遗留设定检查

检查日期：2026-10-15。对象为会话 `6a06f0778766` 末轮实际发送的学生 Prompt，结合当前源码、历史提交和改造说明核对。仅检查和记录，没有修改运行代码或原始日志。

## 总体结论

没有发现已删除的独立“检验学生理解”流程仍作为当前学生任务运行，也没有发现旧四状态规则仍要求把教师明确撤回的知识设成 conflict。当前实际 Prompt 正文与 `prompts.STUDENT` 一致，实际输出 Schema 与当前 `StudentResult.model_json_schema()` 一致。

确实有历史防御措辞、旧数据通道与新通道重叠，以及作用没有明确定义的字段。它们适合清理，但不能统称为已经废弃的功能。长 Prompt 的主要负担仍是重复教师原文与不断增长的知识状态。

## 一、可精简的历史防御措辞

### 1. “不能生成 verified”

- 位置：`backend/prompts.py:79`，understood 的说明末尾。
- 当前状态枚举只有 tentative / understood / unclear / conflict / invalid，Schema 已排除 verified，另有 `test_e_verified_is_not_a_model_writable_status` 检查。
- 历史提交 `47eb623` 已出现“understood 不是独立测验验证、不能产生 verified”的说明。这是防止把课堂推断理解当成验证掌握的历史防御措辞；不能据此认定 verified 曾是正式可写状态。
- 建议：可以去掉具体名字“verified”，保留“understood 仅代表课堂推断理解，不代表外部验证或永久掌握”。节省很小，主要是让语义更集中。

### 2. “年级只作辅助”

- 位置：`backend/prompts.py:77`，unclear 的判定说明。
- 本次系统追加数据只有画像、主题和知识点；当轮 payload 没有 grade，只有 student_level=normal。年级可能间接体现在画像里，但本次没有独立的年级输入。
- 另一条现行规则要求年级、学科和水平不能授权补充未声明知识，两者未必冲突，但“辅助什么”没有讲清楚。
- 建议：合并为“仅按确认画像、当前有效知识及实际讲授判断理解，不从年级或水平标签推断额外知识”。属于措辞统一，不是发现年级功能已废弃。

## 二、历史接口留下的重复输入

### 3. previous_student_utterances_not_knowledge_sources

- 位置：`backend/session.py:342`，发送 `self.history[-8:]`。
- 初始提交 `905a274` 已有该字段，当时还没有 `conversation` 消息历史。
- `3d6e025` 引入 conversation 后，旧字段仍保留。现在实际学生回复既进入 assistant 历史，又进入这个字段。本次末轮该字段为 279 字符，assistant 历史为 156 字符。
- 它不是完全相同的数据：旧字段还带 kind、sources，保留最近 8 次学生发言；conversation 则保留最近 16 条师生消息。长课堂中两者覆盖范围可能不同。
- 建议：统一为一份带角色、来源和发言类型的对话记录或引用结构，再取消旧字段。不能直接认定其“未使用”或无条件删除。

### 4. explicit_prerequisites

- 位置：`backend/session.py:339`。
- 旧版从独立 prerequisites 输入取得前置知识。现在从画像摘录、将 p 条目写入认知状态，再从当前有效 knowledge 筛出 p 条目发送到此字段。
- 因而此字段已经适配了新状态逻辑，不是忘记更新的废弃字段；但与 `current_state.knowledge` 的 p 条目重复，适合合并。
- 建议：让模型只从当前有效知识中的 p 条目取得前置知识，并同步改写 Prompt 的知识来源说明。保留原始画像的性格、理解困难和边界信息，不能把整个画像当成可删的旧配置。

## 三、作用不明确，应收口的字段

### 5. student_level=weak/normal/strong

- 位置：`backend/session.py:338`；设置定义在 `backend/schemas.py:17`。
- 三档水平在画像生成提示词中有明确作用，影响生成的前置知识与理解基础。因此产品设置本身没有废弃。
- 但课堂 Prompt 不解释这三个标签应如何影响当轮行为，且同时禁止凭水平自行补充知识。画像确认后继续单独发送标签，容易产生第二套隐含判断标准；这属于可能的歧义，未通过模型实验确认它实际造成了问题。
- 建议：课堂仅采用确认画像中的具体基础与困难，移除课堂 payload 的裸标签；画像生成仍保留此设置。若确实需要影响表达或推理能力，应先明确规则再保留，不能把标签用作随意装错或调整提问次数的依据。

## 四、仍然有效，不能当作遗留功能删除

| 内容 | 核对结果 |
|---|---|
| 五种知识状态、冲突与失效的区别 | 当前认知代码和状态结构仍在使用 |
| dependencies、revision、independent_sources、correction_sources | 用于依赖传播、独立依据与教师纠错校验 |
| pending_reviews、knowledge_history | 仍用于知识复核与纠正历史；本次为空不代表功能废弃 |
| response_request_updates | 静音期间记录教师请求，恢复后按最新状态回应 |
| question_release_request | 仍用于教师明确邀请下的暂缓疑问例外放行 |
| addressed | 仍参与发言资格与等待时长判断；与请求意图存在重叠，但并未废弃 |
| event_updates、recent_events | 仍记录并展示课堂事件，防止把组织语言当知识 |
| 不给教师评分、不随机装错、不调用未学常识 | 仍是当前学生角色的有效约束，禁止性措辞不等于功能残留 |

相关证据包括 `backend/policy.py`、`backend/cognition.py`、`backend/session.py`，以及现有 cognition / knowledge_machine / mute_resume / question_release 测试。本次阅读了代码及测试定义，没有执行收费模型回放，也没有通过新实验确认删改后的行为。

## 五、旧功能已清出实际 Prompt 的部分

- 旧独立理解检验使用的 Assessment 系列结果结构，没有进入本次实际输出 Schema。
- 独立出题、参考答案、测验判定、评分任务没有进入本次学生 Prompt。
- 旧请求字段 `level`、`prerequisites` 只在 Lesson 输入兼容层处理并排除序列化，没有以这两个旧字段名进入本次 payload；`explicit_prerequisites` 是当前不同用途的字段。
- 旧问题字段 `questions` 的兼容别名没有被发送；状态发送的是 open_questions。
- 旧“教师纠正后把旧知识设 conflict”“新知识一般先 tentative”等规则已由五状态规则替换，没有发现仍混在实际 Prompt 中。

另外发现旧文档存在版本差异：`docs/V1认知状态改造说明.md` 仍描述四状态与纠正后 conflict；`docs/student-profile.md` 的早期描述仍说初始知识为空，而当前代码会播种 p 前置知识。这些文档文字并没有自动进入本次 Prompt，不能把文档过期当成实际模型输入过期。

## 建议清理顺序

1. 合并 verified 和年级相关防御措辞；收益小，但规则更清晰。
2. 明确课堂 student_level 的用途，倾向仅由确认画像决定学生基础。
3. 统一两套学生历史输入；同步保留必要元数据与历史覆盖范围。
4. 合并 p 前置知识的重复入口；同步更新知识来源规则。
5. 与教师原文去重一起验证。大部分 token 节省仍来自上下文去重，而非删除少数历史措辞。
