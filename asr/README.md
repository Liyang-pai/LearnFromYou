# 本地 ASR 模型管理

启动服务后，在网页顶部打开 **ASR 模型**（也可直接访问 `/models`）。无需事先放置模型即可打开配置页面。

1. 选择模型，点击「下载模型」。权重由本机后端从 sherpa-onnx 发布者的 Hugging Face 仓库下载，不随 GitHub 代码克隆。
2. 等待下载和 SHA-256 校验完成，点击「选择使用」。下载不会自动替你切换模型。
3. 返回试讲课堂。开始试讲时加载当前模型，创建、运行和结束处理中禁止切换；结束或断连后释放模型。

可安装多个模型，下载任务一次处理一个。取消或失败后可以重新下载，已校验的完整文件会复用，未完成文件会重下。网络连接需能访问 Hugging Face 和首次安装 VAD 使用的 GitHub；连接失败时检查网络或系统代理，再重试。

## 首批模型

| 界面名称 | CLI ID | 安装 / 下载合计 | 主要用途 |
| --- | --- | --- | --- |
| Paraformer 中文完整版 FP32（2024-03-09） | `paraformer-zh-fp32` | 823MB | 普通话和中英混讲优先候选 |
| SenseVoice Small INT8 标准版（2024-07-17） | `sensevoice-small-int8` | 240MB | 轻量快速，支持中英文及粤日韩 |
| Whisper Small INT8 | `whisper-small-int8` | 375MB | 较小容量的多语言选择 |
| Whisper Large-v3 Turbo INT8 | `whisper-turbo-int8` | 1.04GB | 较高计算需求的准确性候选 |

所有体积采用十进制 MB / GB，只下载推理所需文件，不同时下载另一套精度的权重；共享 VAD 另需 643854 字节。首次完整安装至少预留模型体积 + VAD + 100MiB 的空闲空间。磁盘体积不代表运行内存占用，较大的模型也可能在 CPU 上需要更长识别时间。

来源、固定 revision、文件大小和 SHA-256 保存在 `catalog.py`，界面提供来源及权重许可链接。SenseVoice / Paraformer 权重需遵循对应模型许可证，不能将运行库或项目代码的许可证直接视为权重许可证。推荐标签是选型建议，并非实测准确率排名。Whisper 固定使用中文转写任务，保留英文原文，不翻译成英文。

现有 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/` 仅作为第五个临时开发兼容项，在文件夹存在时显示，不提供下载，不新增粤语适配；后续可直接删除。首次升级时若没有本地选择配置，会沿用完整的现有模型；后续不会因删除当前模型而自动选择其他模型。

## 存储与手动删除

所有模型、缓存和本地选择配置均在项目的 `asr/models/` 下：

```text
asr/models/
├── .settings.json                     # 本机选择，不提交
├── _shared/silero_vad.onnx             # 所有模型共用的 VAD
├── paraformer-zh-fp32/
│   ├── model.onnx
│   ├── tokens.txt
│   └── .installed.json                # 固定版本与已校验文件状态
├── sensevoice-small-int8/
├── whisper-small-int8/
└── whisper-turbo-int8/
```

每个模型独占一个文件夹；使用页面的「打开文件夹」，在试讲结束后将不需要的模型文件夹移入 macOS 废纸篓或 Windows 回收站。页面会定期重新扫描，也可以点击「刷新状态」。删除当前选择会清除选择并提示重新配置，不静默换用其他模型。Windows 下使用中的文件可能被系统锁定，请先结束试讲。

不要删除 `_shared/`，它是共享 VAD；误删后点击任意已安装模型的「准备 VAD」。已有根目录的 `silero_vad.onnx` 保留原位作为开发兼容，新的下载使用 `_shared/`。

`.gitignore` 忽略整个 `/asr/models/`，包括小模型、词表、配置和下载临时文件。`scripts/check_model_files.py` 会检查 Git 索引，CI 拒绝模型目录中的任何文件。旧的模型 README 已移到目录外，其删除应随代码一起提交。不要使用 `git add -f` 强行添加模型。

`ASR_MODEL_DIR` 不再作为配置入口；模型存储目录固定相对于项目位置，网页选择无需修改 `.env` 或重启服务。CPU 线程数仍由 `.env` 的 `ASR_THREADS` 控制。

## 网页 ASR 调试

网页也是开发调试界面，正式用户前端将另行开发。模型页内的「ASR 转文字调试」可直接使用麦克风测试当前选择的模型，无需创建课堂或配置 LLM 密钥，不调用 LLM 或 TTS。转写按 VAD 短段显示，附音频时长、识别耗时和实时系数；「结束录音」会处理尾段并释放模型，之后可以切换其他模型。JSON 导出包含本次模型 ID、逐段文本与耗时，不包含音频。试讲与 ASR 调试共用模型锁，不能同时运行。

## 命令行转写

使用网页当前选择的模型：

```sh
python asr/transcribe.py /完整路径/录音.wav
```

仅为这次命令指定已安装的模型，不修改网页选择：

```sh
python asr/transcribe.py /完整路径/录音.wav --model whisper-turbo-int8 --threads 4
```

支持 WAV / FLAC、多声道转单声道及真实采样率。未提供音频参数时，仅在现有开发样本存在的机器上使用旧 `test_wavs/zh.wav`。CLI 是独立进程，比较模型时建议先结束网页试讲，避免重复占用内存。

## 同录音比较准确性与速度

准备带人工参考转写的 JSONL，至少覆盖 `plain`（普通话）、`mixed`（中英术语）、`numbers`（数字）和 `continuous`（连续讲授）四类。音频路径相对于该 JSONL 所在目录；测试数据和报告可放在 Git 忽略的 `logs/` 内：

```jsonl
{"category":"plain","audio":"plain.wav","text":"链表由节点组成。"}
{"category":"mixed","audio":"mixed.wav","text":"这里使用 API endpoint。","terms":["API","endpoint"]}
{"category":"numbers","audio":"numbers.wav","text":"从九点到五点，共八小时。","terms":["九点","五点","八小时"]}
{"category":"continuous","audio":"continuous.wav","text":"人工校对后的连续讲授完整转写。"}
```

```sh
python asr/benchmark.py logs/asr-cases/cases.jsonl --output logs/asr-comparison.json
```

默认比较全部已安装模型；可重复提供 `--model` 限定比较对象。不会自动下载。使用与麦克风相同的重采样、VAD 分段和识别适配器，记录参考文本、识别文本、字符错误率 CER、英文词错误率 WER、术语命中、加载耗时、识别耗时和实时系数。CER 去除空白与标点，但不把「3」转换为「三」；数字规范化差异需要人工审阅。RTF 越低越快，低于 1 表示推理比录音时长短，加载时间单独计算。

请用同一批真实课程录音和相同设备比较，再调整推荐标签，不用单个演示样本推断整体准确率。

## 测试与平台

```sh
python -m pytest -q
python scripts/check_model_files.py
```

默认下载测试使用模拟 HTTP，不需要下载大型模型、LLM 密钥或语音播报。已有权重及样本时额外运行真实 VAD / ASR 回归；缺失时跳过真实模型测试。CI 覆盖 macOS / Windows x64、Python 3.11 / 3.13，ASR 使用 `sherpa-onnx==1.13.8` 的 CPU 后端；中文路径和带空格路径均使用跨平台路径接口。

本次只涉及 ASR；TTS 仍保留现阶段 macOS `say` 临时实现，未做 Windows 适配。
