# 本机 SenseVoice ASR

这里保存原有的模型、测试样本、转写脚本，以及录音链路调用的识别器。原 Python 虚拟环境留在项目根目录。

从项目根目录运行内置中文样本：

```sh
./.venv/bin/python asr/transcribe.py
```

识别 WAV/FLAC 文件：

```sh
./.venv/bin/python asr/transcribe.py /完整路径/录音.wav --threads 4
```

`models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/` 是原有模型目录，模型权重和测试音频保留。原模型说明指向 ASLP-lab/WSYue-ASR 的 sensevoice_small_yue，普通话效果需要实测。

新增 `models/silero_vad.onnx` 来自 sherpa-onnx 官方 release：
https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx

`recognizer.py` 中的 `Recognizer` 常驻加载模型，`AudioStream` 为每次录音维护独立 VAD 和重采样状态。网页发送真实采样率的 Float32 PCM，服务端转为 16kHz；不把 48kHz 数据直接当成 16kHz。
