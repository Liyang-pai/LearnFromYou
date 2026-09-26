# 本地 ASR 模型

模型权重不会放进 GitHub 仓库：当前模型约 233MB，超过 GitHub 普通 Git 文件限制，也不适合让下载者自动随代码克隆。

本项目使用 sherpa-onnx 的 SenseVoice 离线识别器。请准备一个兼容模型目录，至少包含：

```text
你的模型目录/
├── model.int8.onnx
└── tokens.txt
```

默认位置为：

```text
asr/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/
```

如果使用其他路径，请在项目根目录的 `.env` 设置：

```dotenv
ASR_MODEL_DIR=/绝对路径/你的模型目录
```

可从 sherpa-onnx 的官方预训练模型发布页下载与 `OfflineRecognizer.from_sense_voice` 兼容的 SenseVoice ONNX 模型：

https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models

下载并解压后，确认模型目录中存在上述两个文件。然后运行：

```sh
./.venv/bin/python asr/transcribe.py
```

成功输出 `Transcript:` 后，再启动网页。模型选择会影响普通话、粤语和英文术语的识别质量，请用实际试讲内容验证。
