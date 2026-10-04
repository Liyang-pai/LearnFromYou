"""Small, version-pinned catalog. Only inference files are downloaded."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    directory: str
    family: str
    files: tuple[Asset, ...]
    languages: str
    description: str
    label: str
    source: str
    license_url: str
    revision: str
    downloadable: bool = True

    @property
    def size(self):
        return sum(f.size for f in self.files)


MODEL_LICENSE = "https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE"
WHISPER_LICENSE = "https://github.com/openai/whisper/blob/main/LICENSE"


def hf(repo, revision, name, size, sha):
    return Asset(name, size, sha, f"https://huggingface.co/{repo}/resolve/{revision}/{name}")


SENSE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSE_REV = "2365baeacb507f821a0c8120fcee3d484dba7a07"
PARA_REPO = "csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09"
PARA_REV = "906992d326ebf0c5171cde675aa0902be9e5bc6c"
SMALL_REPO = "csukuangfj/sherpa-onnx-whisper-small"
SMALL_REV = "8f3c18b358db4d1f2fc1eae49d75cd20989e4309"
TURBO_REPO = "csukuangfj/sherpa-onnx-whisper-turbo"
TURBO_REV = "2ca6ff69fc878651b770880507669577ac41c2ff"
WHISPER_TOKENS_SHA = "b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126"
LEGACY_ID = "sensevoice-legacy-yue"
LEGACY_DIRECTORY = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"

CATALOG = (
    Model("paraformer-zh-fp32", "Paraformer 中文完整版", "paraformer-zh-fp32", "paraformer", (
        hf(PARA_REPO, PARA_REV, "model.onnx", 822641426, "ed302fb061dcb65655b5240f5f8cd18d6d6c2f5c2b5cb63184d413d728bc1ec4"),
        hf(PARA_REPO, PARA_REV, "tokens.txt", 75354, "6c0e3b35cece259829e6cb5b8d90d13db88f61ea3a2953d11898e4b2bfd7a2e2"),
    ), "普通话 · 英文", "普通话讲授与中英术语混讲的优先候选。完整精度，需要更多计算资源。", "中文优先 · FP32", f"https://huggingface.co/{PARA_REPO}", MODEL_LICENSE, PARA_REV),
    Model("sensevoice-small-int8", "SenseVoice Small", "sensevoice-small-int8", "sense_voice", (
        hf(SENSE_REPO, SENSE_REV, "model.int8.onnx", 239233841, "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"),
        hf(SENSE_REPO, SENSE_REV, "tokens.txt", 315894, "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"),
    ), "普通话 · 英文 · 粤语 · 日语 · 韩语", "标准版轻量模型，适合希望快速开始、控制磁盘占用的用户。", "轻量快速 · INT8", f"https://huggingface.co/{SENSE_REPO}", MODEL_LICENSE, SENSE_REV),
    Model("whisper-small-int8", "Whisper Small", "whisper-small-int8", "whisper", (
        hf(SMALL_REPO, SMALL_REV, "small-encoder.int8.onnx", 112442483, "4cbe7b22fa9026b843b60a68640c747de05bafb1a11b57edc0e66c232d9f33a9"),
        hf(SMALL_REPO, SMALL_REV, "small-decoder.int8.onnx", 262226114, "acad50b5c782696e91b55914cc5ab4f756f1532f76e22aa6fc615f39fb69a8ee"),
        hf(SMALL_REPO, SMALL_REV, "small-tokens.txt", 816730, WHISPER_TOKENS_SHA),
    ), "中文 · 英文等多语言", "轻量多语言选择。默认按中文转写，英文术语保留原文。", "多语言 · INT8", f"https://huggingface.co/{SMALL_REPO}", WHISPER_LICENSE, SMALL_REV),
    Model("whisper-turbo-int8", "Whisper Large-v3 Turbo", "whisper-turbo-int8", "whisper", (
        hf(TURBO_REPO, TURBO_REV, "turbo-encoder.int8.onnx", 674716297, "b02dcdf54f348741e93fe732b67d933c8dcb6735655f710640143081db38878b"),
        hf(TURBO_REPO, TURBO_REV, "turbo-decoder.int8.onnx", 361080764, "20accd02388482eb3a46bd615631adfdc85e1eb2c7db9ea3f02a40ffe6b81547"),
        hf(TURBO_REPO, TURBO_REV, "turbo-tokens.txt", 816730, WHISPER_TOKENS_SHA),
    ), "中文 · 英文等多语言", "较大容量的准确性候选，适合有充足空间与计算资源的用户。", "准确性候选 · 较高计算需求", f"https://huggingface.co/{TURBO_REPO}", WHISPER_LICENSE, TURBO_REV),
    Model(LEGACY_ID, "现有 SenseVoice（临时兼容）", LEGACY_DIRECTORY, "sense_voice", (
        Asset("model.int8.onnx", 0, "", ""), Asset("tokens.txt", 0, "", ""),
    ), "现有粤语微调版本", "仅保留现有本地文件供开发过渡，不提供下载；本项目不新增粤语适配。", "开发兼容 · 后续移除", "https://huggingface.co/ASLP-lab/WSYue-ASR", MODEL_LICENSE, "legacy-local", False),
)

VAD = Asset("silero_vad.onnx", 643854,
            "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx")
