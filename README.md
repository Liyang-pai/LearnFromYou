# 讲给我听 · Learn From You

**当前仓库的前端是开发调试界面**，用于验证 ASR 转写效果与 AI 学生试讲链路。它不是面向用户的正式产品界面；正式前端将另行设计和开发。

一个本地语音识别、云端 Student LLM 的试讲练习工具。教师讲授时，AI 学生只根据明确的前置知识和本次课堂内容形成认知，不会因为课题名称就默认知道答案。

> 当前版本主要在 **macOS Apple Silicon** 上验证。ASR 模型管理、下载和 CPU 识别面向 macOS / Windows x64，CI 覆盖两端。系统语音播报仍保留 macOS `say` 临时实现，本次未适配 Windows。

## 下载者需要配置什么

| 项目 | 是否必须 | 说明 |
| --- | --- | --- |
| Python 3.11+ | 必须 | 推荐使用独立虚拟环境。当前开发环境为 Python 3.13。 |
| 本地 ASR 模型 | 必须 | GitHub 不包含任何模型文件。启动网页后，在「ASR 模型」页面下载并选择；支持安装多个。详情见 `asr/README.md`。 |
| DeepSeek 或兼容 LLM API 密钥 | 必须 | 将密钥写入本地 `.env`，不会上传或提交到 GitHub。 |
| Chrome 麦克风权限 | 试讲时必须 | 选择真实麦克风，不要选择 `Background Music (Virtual)` 等无教师语音的虚拟设备。 |
| 耳机 | 建议 | 降低学生语音播报被重新识别为教师语音的概率。 |

## 从零安装

```sh
git clone https://github.com/Liyang-pai/LearnFromYou.git
cd LearnFromYou
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

随后编辑 `.env`，至少填入：

```dotenv
DEEPSEEK_API_KEY=你的密钥
```

默认使用 DeepSeek：`LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-chat`。也可以填写兼容 OpenAI Chat Completions 接口的地址与模型名。旧版字段 `Deepseek_API` 仍可用，但新安装请使用 `DEEPSEEK_API_KEY`。

随后启动服务，在网页「ASR 模型」页面下载并选择模型。首次配置无需预先安装模型；选择成功后返回课堂。推荐优先尝试 Paraformer 中文完整版，轻量选择为标准 SenseVoice Small INT8；另有 Whisper Small / Turbo INT8。详见 [ASR 模型管理](asr/README.md)。

仅测试语音转文字时，可直接在模型页面的「ASR 转文字调试」点击「开始录音测试」，无需填写主题或配置 LLM 密钥。停顿后显示逐段转写、音频时长、识别耗时和实时系数；结束录音会处理尾段并释放模型，随后可切换模型再次测试。结果可清空或导出 JSON，音频不写入文件，也不调用 LLM / TTS。

Windows PowerShell 使用 `.venv\Scripts\python.exe` 替换下面的 `./.venv/bin/python`，复制环境示例可运行 `Copy-Item .env.example .env`。模型不需要单独安装 GPU 或 CUDA 环境。

## 启动

```sh
./.venv/bin/python run.py
```

使用 Chrome 打开 **http://127.0.0.1:8765**。macOS 可双击 `启动试讲.command`；若网页先于服务加载完成，稍等后刷新。终端保持开启，按 Control-C 关闭服务。

页面必须通过本机服务地址访问。直接打开 `frontend/index.html` 会跳转到默认地址 `http://127.0.0.1:8765/models`；若修改了 `PORT`，请使用相应端口的 HTTP 地址。若提示无法连接，请先启动服务。

更新代码后，需要在原终端按 Control-C，再重新启动服务并刷新网页。服务不会自动重新加载 Python 代码；只刷新网页可能出现新页面连接旧后端的情况。ASR 调试面板会检查后端是否支持调试接口，并在版本不匹配时提示重启。

1. 首次使用先在「ASR 模型」下载并选择模型；之后可以在试讲前切换。输入每次要讲的主题、知识点及明确前置知识，点击“创建试讲”。
2. “语音转文字审核”默认开启，可在创建前关闭，课堂中不可切换。开启时先准备本课术语表；准备完成后，点击“开启麦克风”，允许浏览器使用麦克风，并选择真实麦克风设备。建议戴耳机。
3. 自然讲授。转写按短段显示；教学内容聚合后，开启审核时先校正识别错误，再按顺序发送给学生模型。调试台保留原文、校正文本和审核耗时。
4. 可以直接问“你理解了吗”，或停顿让学生提出相关疑问。
5. “学生静音”完全禁止学生发言，但继续识别和学习；“语音播报”只控制声音。
6. 结束试讲会处理末尾转写，并保留学生状态。创建下一次试讲不继承前次知识。

调试时展开“用文字测试同一条学习链路”，输入教师讲授或问题。它使用同一学生引擎，发送时立即形成教学片段，不替代麦克风功能。文字输入不做审核，但会等待先前语音片段完成，保持讲授顺序。

审核只校正明显的同音词、术语和断句错误，保留口语、重复、教师的知识错误及无法确定的表达；模型仍可能误改，请结合两份文本检查。术语表只供校正器使用，不计入学生知识。审核复用现有云端模型，会增加请求和等待时间；失败时提示并回退原文，课堂继续处理。默认术语准备上限 30 秒、每个语义片段审核上限 15 秒，包含格式修复重试，可通过 `ASR_REVIEW_GLOSSARY_TIMEOUT` 和 `ASR_REVIEW_TIMEOUT` 调整。

## 本地与云端边界

麦克风音频只在本机处理，不上传，也不默认写入录音文件。DeepSeek 接收课堂转写、明确前置知识、学生状态及必要的历史上下文。模型密钥只由后端读取，不进入网页或日志。

开启审核时，校正器另行接收主题、准备讲授的知识点、术语表、当前片段和最近 8 条已完成审核或直通的教师来源，不接收学生认知或学生发言。学生模型只接收实际采用的课堂文本，不接收术语表和原文审计字段。

`.env` 兼容原有 `Deepseek_API`，也支持 `DEEPSEEK_API_KEY`。可选设置见 `.env.example`。默认 API 地址为 `https://api.deepseek.com`、模型为 `deepseek-chat`。启动后修改配置需要重启服务。

## 模块

- `asr/catalog.py`、`manager.py`：固定模型清单、下载校验、本地选择与安装状态。
- `asr/recognizer.py`：SenseVoice / Paraformer / Whisper CPU 适配、采样率处理、Silero VAD。仅在试讲时加载，不使用云端 ASR。
- `backend/session.py`：音频、转写和认知更新的串行队列，会话与发言控制。
- `backend/review.py`：可选转写审核、独立术语准备、总等待时限与失败回退。
- `backend/prompts.py`：课前与课堂 Prompt。教学材料作为数据，不作为系统指令。
- `backend/schemas.py`、`policy.py`：结构化状态、来源校验、提问生命周期与静音规则。
- `backend/llm.py`：兼容接口，可替换地址和模型；无效 JSON 最多修复一次。
- `backend/speech.py`：macOS 本机语音，支持立即停止。
- `frontend/`：本地网页、麦克风 AudioWorklet、调试面板。

状态只在本次会话维护；`logs/会话编号.jsonl` 保存调试事件，属于本地调试记录，不是跨课程长期记忆。可从网页导出同样的事件记录。日志包含讲授文本和模型输入输出，需要时可以自行清理。

## 分段与互动默认值

- VAD 静音 0.6 秒结束一个语音段；连续语音最长 12 秒切段。
- 教学分段停顿 3 秒提交；超过 300 字后停顿 1 秒；已有稳定文本最长等待 20 秒。
- 点名后约 1 秒停顿即可处理；其他情况至少停顿 5 秒才考虑主动提问。
- 模型耗时额外计入实际响应时间。状态按序提交，旧候选发言会在新输入到达后撤销。
- 同一个问题最多提问两次；合理解释后关闭。静音期间关闭的疑问不会在解除静音后补播。
- 模型失败会暂停认知队列并保留待处理文本；点击“重试这一段”。结束时若仍有未处理内容，界面会明确列出。

## 测试

```sh
./.venv/bin/python -m pytest -q
./.venv/bin/python asr/transcribe.py /完整路径/录音.wav
```

启动服务后，执行真实 API 和录音数据链路测试（会使用少量 DeepSeek 配额，不会播放声音）：

```sh
./.venv/bin/python tests/live_smoke.py
```

单独检查真实模型的转写审核（会使用少量配额，不需要麦克风）：`./.venv/bin/python tests/live_review.py`。实际原文、校正结果、耗时及样例断言保存到 `logs/asr-review-quality.json`；这些有限样例不代表所有课堂内容都能正确校正。

`asr/transcribe.py /完整路径/录音.wav --model 模型ID` 支持选择已安装模型进行独立 WAV/FLAC 转写。默认测试使用模拟下载；真实模型测试在本地文件存在时运行。准确性比较工具与数据格式见 [ASR 说明](asr/README.md)，推荐标签尚未用真实课程录音验证。

整个 `asr/models/` 被 Git 忽略，小型 VAD 和本地配置也不会提交。CI 使用 `scripts/check_model_files.py` 拒绝任何被强行加入索引的模型目录文件。

## 已知边界

首版只允许一个活动试讲，主要面向这台 Mac 的耳机演示。外放回声消除仅作为辅助，不保证复杂环境下学生语音不会被麦克风收回。识别是短段最终转写，不是逐字即时转写。

程序检查来源是否存在，无法仅凭引用编号证明一句话完全由课堂支持。Prompt 也不能让预训练模型真正忘记知识；需继续用未讲内容、错误但自洽的讲解和特殊术语做回归测试。

“当前理解”表示学生当前的认知，不代表标准答案或永久掌握。本版不提供教师评分、多学生、视觉、长期记忆或账号系统。
