# Learn From You 课后反馈报告 V2 · 开发说明

日期：2026-10-08。基线：`398467852331c66ea71b6a87a8edeecbd64c9da7`（GitHub 最新 main）。

开发分支：`feature/trial-report-v2`。

工作树：`C:\Users\binbin\.codex\worktrees\trial-report-v2\LearnFromYou-main`。它是在本轮第一份任务指令后刚创建的干净独立工作树，收到新版任务时继续使用，没有覆盖同名分支，也没有移动现有成果。

## 开发前审计与产品范围

- main 已有本地 ASR、可选文本审核、音频上传、课堂知识/疑问状态、受控发言、Windows/macOS TTS。
- PRD v0.1 的基线是旧 `b8e3c6b`；基础结算、证据反馈待补，Windows 播报的旧状态已过时。
- 本轮补基础结算、第一人称复述、轻量模拟理解验证、优缺点、建议和证据定位，不重新设计正式用户前端。
- 旧 #3 仅借鉴原文引用校验、文件缓存与分析失败回退的设计。没有合并、复制其报告模块，也不依赖 #1 的知识修订、事件认知分类或独立测验。
- 本轮新增的报告内追问由用户任务明确授权，是单个模拟学生的课后理解线索，不发展成 PRD 明确不做的作业考试或评分系统。没有增加 verified 状态，不把验证结果写回课堂。
- 最小修复 `policy.py` 的同主题不同问题误合并，保留同 ID、相似问题去重、已解决防复活、次数限制及发言门控；未修改其他核心认知策略。

## 实施顺序与验收点

1. 独立工作树、main 基线检查和基准测试。
2. 事件快照与确定性结算：结束立即返回，不依赖报告 LLM。
3. 学生复述与批量独立作答：结构和来源检查，最多三题。
4. 模拟验证、诊断与建议：谨慎结论、有效证据、阶段缓存与重试。
5. 前端与导出：七部分报告、证据高亮、失败保留、旧响应隔离。
6. 原有回归、报告专项、三组模拟案例及浏览器检查。

## 修改及新增文件

修改：`backend/session.py`、`app.py`、`policy.py`、`llm.py`；`frontend/app.js`、`index.html`、`style.css`；`README.md`。

新增：

- `backend/report_data.py`：结束课堂的数据快照、统计、原文/审核版本关系。
- `backend/report_models.py`：报告独立结构，不改 main 的 StudentState。
- `backend/report_prompts.py`：四个有明确身份与知识权限的提示。
- `backend/report_v2.py`：引用校验、后台生成、缓存、失败重试与 Markdown。
- `frontend/report-v2.js`：报告状态、七部分展示、证据定位、导出、过期响应隔离。
- `tests/test_report_v2.py`：统计、协议、生成失败、缓存、隔离与语义约束契约测试。
- `tests/report_v2_browser.cjs`：真实无头 Chrome 的界面检查，报告数据完全模拟。
- `scripts/report_v2_examples.py`：三组固定模拟课堂和模型夹具，不读取私人日志或访问网络。
- `docs/report-v2-examples/`：模拟报告 Markdown/JSON 和浏览器截图。

## 数据与统计口径

每个课堂事件新增不可变编号 `session_id:e000001`。旧事件类型、数据字段和录音协议保持兼容。

结束快照包含 `version`、`session_id`、`source_hash`、`lesson`、最终 `state`、`sources`、`learning_sources`、`questions`、`evidence`、`settlement`。只采集 ready/transcript/review_completed/state/reply/error/finished，报告不需要模型原始调试输出。

统计代码按事件 ID 去重，教师文本按唯一 t 来源计数。审核完成只更新对应来源的实际文本，不增加讲授次数。`raw_text`、实际 `text`、转写事件 ID 与审核事件 ID 同时保留。

| 指标 | 定义与限制 |
| --- | --- |
| 试讲总时长 | ready 到 finished，含停顿与结束等待，不含课前模型准备；缺时间则无法统计 |
| 教师有效发言 | 唯一 t 文本来源数，含尚未处理文本；ASR 短段不是自然发言轮次，也不是教学质量判断 |
| 教师提问 | 明确标注的 question_count 累加；任何片段缺标注则无法统计，无教师片段则 0 |
| AI 学生发言 | 实际交付的 reply 事件，候选、重试不计 |
| 学生独立疑问 | 检查全部学生发言的显式疑问，关联已有 question_id 去重；无法可靠关联时显示无法统计及限制，不猜独立数量 |
| 疑问出现次数 | 同一发言中同一疑问只计一次，重复追问可再次计数；与独立数量分开 |
| 有回应证据 | 提出之后有效解释来源或同问题的暂缓回应；回应不等于解释或解决 |
| 有内容解释 | resolution_sources 指向提出后的有效教师来源，排除仅暂缓处理 |
| 未记录明确回应 | 已关联疑问缺少上述回应；零不等于全部解决，未关联问题另列限制 |
| 系统标记已解决 | 实际已提出的疑问中最终 resolved 数量，不等于独立验证 |
| 系统尚未解决 | 实际提出但最终非 resolved，含已回应却暂缓的问题 |
| 课堂理解已验证 | main 未记录独立验证，显示无法统计；课后模拟验证另列 |
| 主要知识点 | 最终知识中有本次已处理教师来源的条目，不把 scope 或未处理内容算成已学习 |
| 未完成与失败 | finished.unprocessed_sources 和历史 error 事件；历史错误可能已恢复，不统称最终失败 |

录音积压、识别失败等即使没有形成 t 来源，也会保留 `audio_gap_events` 并提示反馈可能不完整；是否已经重讲由使用者复核。模型输入只含实际采用的 text；原始 ASR 文本留在审计快照和证据界面，不混入学生复述/作答上下文。

文字试讲的可选“本段提问次数”输入支持 0..50；空值为未知。麦克风/上传音频目前没有可靠的人工提问标注，因此通常显示无法统计。本轮没有用关键词或 LLM 猜测精确提问数。

## API 与生成流程

- `finished.data.settlement`：与课堂结束一起返回确定性结算。
- `GET /api/sessions/{id}/report-v2`：读取结算、各阶段结果、状态及 Markdown，不调用模型。
- `POST /api/sessions/{id}/report-v2`：返回 202，开始后台生成；重复请求复用任务或已完成缓存。
- 运行中的课堂阻止生成新报告。开始新课堂时取消旧报告任务；保留已完成阶段和基础结算。

缓存放在已忽略的 `logs/{id}.report-v2.json`，本地原子写入；不改原 JSONL，不新增数据库或历史课程产品。服务重启后同一课堂可读取本地缓存，中断阶段允许重试。

前端先显示结算，再由使用者主动点击生成；页面说明会发送课堂文本并产生 API 用量。状态为 idle/generating/ready/partial/failed，每个阶段另有 pending/generating/success/skipped/failed。生成失败不清空原课堂、结算或其他成功阶段。

## 学生复述与答案隔离

通常四次批量调用：

1. **recall**：第一人称复述的 explained/doubts/uncertain，以及最多三题 probes。只有问题和来源，不生成参考答案。
2. **answers**：全新无历史的请求，只收课堂上下文和 `{id, question}`。没有复述、参考答案、评分标准、出题评价或题目目标字段。
3. **verification**：检查作答与真实课堂规则的关系，不使用外部标准答案替换错误教学。结论为有理解证据/存在误解/尚未验证/证据不足。
4. **diagnosis**：有引用的观察、AI 推断、对应不足的下次动作；不输出总分，建议中的新例子不冒充本次事实。

没有可靠题目时省去作答和验证两次调用。每阶段最多 60 秒，含结构修复；普通请求最多 3500 输出 token，复述和诊断最多 5000。模型客户端结构错误最多修复一次，因此通常 2 或 4 次请求，最坏结构修复 4 或 8 次 HTTP 请求；失败重试另计。模型输出截断直接报错，不在同样限制下盲重试。

上下文超过 60000 字符时拒绝 AI 分析，保留结算，不静默截断证据。这个字符上限是保护阈值，不是精确 token 估计。没有已处理教师内容时不发送模型请求。

## 程序防护及局限

- 所有输出使用 Pydantic 严格结构，未知字段拒绝。
- 事件 ID 必须存在、属于当前课堂；引用必须逐字存在于实际 text。
- 学习引用只能来自已处理 teacher/prerequisite；学生自己的 reply、未处理文本、审核原文不是新知识来源。
- 复述知识 ID 必须存在，且引用与该知识实际来源对应；不能把 unclear/conflict 描述成已经能解释。
- 最终未解决疑问必须映射到 doubts/uncertain，不能凭空消失。
- 题目 ID、考点、作答、评估一一对应。直接复制课堂答案进题目被拒绝。
- 机械复述、没有作答依据或没有判断依据，不能被程序接受为理解证据。
- 验证增加题目任务要求、逐项作答检查和实际推理原文；只换变量/数字的复述不能直接通过，理由错误、部分完成及无法判断分别处理。旧积极结论缺少这些证据时保守降级。
- 负面诊断只保留有学生实际疑问及教师证据支持的未解决问题，或解释后仍困惑的证据；不将正常认知冲突直接判为失败。缺证据的不足及关联建议一起移除。
- 建议仅能关联本次真实不足，不得虚构问题编号。

这些规则不能让预训练模型真正忘记知识，也不能完全证明自然语言没有泄漏答案或引文语义必然支持结论。所有验证仍统一标为模拟验证，不能证明真实掌握。外部事实真伪不在本轮验证权限内；自洽错误教学可能被忠实复述而不被独立识错。

## 三组模拟样例

- [正确讲解](report-v2-examples/正确讲解.md)：自定义规则加一，学生对新输入给出结果和理由，保留课堂内应用证据。
- [错误讲解](report-v2-examples/错误讲解.md)：教师说二乘三等于七，学生保留这个错误理解；机械复述降级为尚未验证，不自动换成正确答案，也不虚构独立识错能力。
- [不完整讲解](report-v2-examples/不完整讲解.md)：删除规则未讲，保留未解疑问；没有可靠验证题，给出下次节点连接演示的具体建议。

这些输出都是固定模拟夹具的结果，不是自然语言模型质量验收。每份 Markdown 和 JSON 都明确标注模拟来源。

## Windows 启动与体验

在新工作树目录运行：

```powershell
cd C:\Users\binbin\.codex\worktrees\trial-report-v2\LearnFromYou-main
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 在本机编辑 .env 配置自己的密钥，不提交、不在聊天中发送密钥。
.\.venv\Scripts\python.exe run.py
```

Chrome 打开 `http://127.0.0.1:8765`。独立工作树不会复制旧 .env 或 ASR 模型；首次在模型页准备模型。若旧服务占用默认端口，请自行选择空闲 PORT，并在浏览器打开对应地址；本轮没有停止旧服务。原 bat 地址固定 8765，使用自定义端口时请直接运行 Python 并手动打开地址。

创建试讲 → 麦克风/上传音频/文字讲授 → 结束 → 查看基础结算 → 点击生成学生复述与反馈 → 点击证据 → 导出 Markdown。真实生成会使用自己的 API 配额。

本轮测试复用已有 Python 环境作为解释器，并通过新工作树 cwd 导入新代码，不复制密钥、模型，不修改旧工作树源码。

```powershell
# 运行自动化测试，不调用真实模型
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
# 生成模拟案例
.\.venv\Scripts\python.exe -B scripts\report_v2_examples.py
# 浏览器检查需要已安装 Playwright 和 Chrome
$env:LFY_TEST_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
$env:CHROME_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
node tests\report_v2_browser.cjs
```

验收结果见 [report-v2-validation.md](report-v2-validation.md)。只有独立 PR 的代码和模拟验收准备，未经用户许可不 push、不创建 PR、不修改或关闭旧 PR。
