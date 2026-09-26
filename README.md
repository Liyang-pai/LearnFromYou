# 听懂了吗 · AI 学生试讲 MVP

一个本地语音识别、云端 Student LLM 的试讲练习工具。教师讲授时，AI 学生只根据明确的前置知识和本次课堂内容形成认知，不会因为课题名称就默认知道答案。

> 当前版本主要在 **macOS Apple Silicon** 上验证。网页与本地 ASR 可迁移到其他系统，但系统语音播报需要自行替换 `backend/speech.py` 中的 macOS `say` 调用。

## 下载者需要配置什么

| 项目 | 是否必须 | 说明 |
| --- | --- | --- |
| Python 3.10+ | 必须 | 推荐使用独立虚拟环境。当前开发环境为 Python 3.13。 |
| 本地 ASR 模型 | 必须 | GitHub 不包含模型权重。准备一个 sherpa-onnx SenseVoice 模型目录，内含 `model.int8.onnx` 与 `tokens.txt`；详情见 `asr/models/README.md`。 |
| DeepSeek 或兼容 LLM API 密钥 | 必须 | 将密钥写入本地 `.env`，不会上传或提交到 GitHub。 |
| Chrome 麦克风权限 | 试讲时必须 | 选择真实麦克风，不要选择 `Background Music (Virtual)` 等无教师语音的虚拟设备。 |
| 耳机 | 建议 | 降低学生语音播报被重新识别为教师语音的概率。 |

## 从零安装

```sh
git clone https://github.com/Liyang-pai/project.git
cd project
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

接着按 `asr/models/README.md` 放置本地模型。默认模型位置为：

```text
asr/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/
├── model.int8.onnx
└── tokens.txt
```

如将模型放在其他位置，在 `.env` 设置：

```dotenv
ASR_MODEL_DIR=/绝对路径/你的模型目录
```

## 启动

```sh
./.venv/bin/python run.py
```

使用 Chrome 打开 **http://127.0.0.1:8765**。macOS 可双击 `启动试讲.command`；若网页先于服务加载完成，稍等后刷新。终端保持开启，按 Control-C 关闭服务。

1. 输入每次要讲的主题、知识点及明确前置知识，点击“创建试讲”。
2. 范围分析完成后，点击“开启麦克风”，允许浏览器使用麦克风，并选择真实麦克风设备。建议戴耳机。
3. 自然讲授。转写按短段显示；教学内容进一步聚合后发送给学生模型。
4. 可以直接问“你理解了吗”，或停顿让学生提出相关疑问。
5. “学生静音”完全禁止学生发言，但继续识别和学习；“语音播报”只控制声音。
6. 结束试讲会处理末尾转写，并保留学生状态。创建下一次试讲不继承前次知识。

调试时展开“用文字测试同一条学习链路”，输入教师讲授或问题。它使用同一学生引擎，发送时立即形成教学片段，不替代麦克风功能。

## 本地与云端边界

麦克风音频只在本机处理，不上传，也不默认写入录音文件。DeepSeek 接收课堂转写、明确前置知识、学生状态及必要的历史上下文。模型密钥只由后端读取，不进入网页或日志。

`.env` 兼容原有 `Deepseek_API`，也支持 `DEEPSEEK_API_KEY`。可选设置见 `.env.example`。默认 API 地址为 `https://api.deepseek.com`、模型为 `deepseek-chat`。启动后修改配置需要重启服务。

## 模块

- `asr/recognizer.py`：常驻 SenseVoice、采样率处理、Silero VAD。没有云端 ASR。
- `backend/session.py`：音频、转写和认知更新的串行队列，会话与发言控制。
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
./.venv/bin/python asr/transcribe.py
```

启动服务后，执行真实 API 和录音数据链路测试（会使用少量 DeepSeek 配额，不会播放声音）：

```sh
./.venv/bin/python tests/live_smoke.py
```

`asr/transcribe.py /完整路径/录音.wav` 继续支持独立 WAV/FLAC 转写。现有模型说明指向粤语微调版本；应以实际课程检验普通话、术语及数字识别，不假定所有主题都已验证。

## 已知边界

首版只允许一个活动试讲，主要面向这台 Mac 的耳机演示。外放回声消除仅作为辅助，不保证复杂环境下学生语音不会被麦克风收回。识别是短段最终转写，不是逐字即时转写。

程序检查来源是否存在，无法仅凭引用编号证明一句话完全由课堂支持。Prompt 也不能让预训练模型真正忘记知识；需继续用未讲内容、错误但自洽的讲解和特殊术语做回归测试。

“当前理解”表示学生当前的认知，不代表标准答案或永久掌握。本版不提供教师评分、多学生、视觉、长期记忆或账号系统。
