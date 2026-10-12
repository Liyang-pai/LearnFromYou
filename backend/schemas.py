from typing import Annotated, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

PROFILE_REQUIRED = '请生成或填写学生设定，再创建试讲嗷~'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeachingSettings(StrictModel):
    """生成和创建共用的教学约束；不包含课堂状态或对话。"""
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    points: str = Field(default="", max_length=3000)
    grade: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    subject: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    student_level: Literal["weak", "normal", "strong"] = "normal"
    lesson_type: Literal["new", "review", "application"] = "new"
    textbook: str = Field(default="", max_length=200)


class StudentProfileResult(StrictModel):
    grade: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    points: str = Field(min_length=1, max_length=3000)
    student_profile: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]

    @field_validator('points')
    @classmethod
    def nonblank_points(cls, value):
        if not value.strip():
            raise ValueError('准备讲的知识点不能为空白')
        return value


class Lesson(TeachingSettings):
    student_profile: str = Field(default='', max_length=3000, validate_default=True)
    # 只兼容用户显式传入的旧字段，不再自动生成默认基础或写入新日志。
    prerequisites: str | None = Field(default=None, max_length=2000, exclude=True)
    level: str | None = Field(default=None, max_length=300, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def migrate_explicit_legacy_basis(cls, data):
        if isinstance(data, dict) and 'student_profile' not in data:
            parts = [f'{label}：{data[key]}' for key, label in
                     [('prerequisites', '已有知识'), ('level', '理解基础')]
                     if isinstance(data.get(key), str) and data[key].strip()]
            data = {**data, 'student_profile': '\n'.join(parts)}
        return data

    @field_validator('student_profile')
    @classmethod
    def require_profile(cls, value):
        if not value.strip():
            raise ValueError(PROFILE_REQUIRED)
        return value


class Preparation(StrictModel):
    scope: list[str] = Field(max_length=12)
    boundary_note: str = Field(max_length=400)
    initial_prerequisites: list[Annotated[str, StringConstraints(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=24,
        description='只逐字引用画像中明确已知的完整陈述；没有明确前置知识时返回空数组')


def profile_preparation(profile: str):
    """将画像原文约束放进模型结果校验，使无依据摘录参与既有结构修复。"""
    class ProfilePreparation(Preparation):
        @model_validator(mode='after')
        def check_quotes(self):
            for quote in self.initial_prerequisites:
                if not quote.strip() or quote not in profile:
                    raise ValueError('前置知识必须逐字引用确认画像中的完整原文，不得改写或补充')
            return self
    return ProfilePreparation


class ReviewGlossary(StrictModel):
    # Ask the model for 40 terms, but tolerate a small overrun without a repair retry.
    terms: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] = Field(
        max_length=45, json_schema_extra={"maxItems": 40})


class CorrectedSource(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]


class ReviewResult(StrictModel):
    segments: list[CorrectedSource] = Field(min_length=1)


class Knowledge(StrictModel):
    id: str = Field(max_length=60)
    text: str = Field(min_length=1, max_length=400)
    status: Literal["tentative", "understood", "unclear", "conflict"]
    sources: list[str] = Field(min_length=1, max_length=12)
    supersedes: list[str] = Field(default_factory=list, max_length=12,
                                  description="本条明确纠正并替代的旧知识 ID；普通补充不填写")


class KnowledgeRevision(StrictModel):
    version: int
    previous: Knowledge
    replacement_id: str


class ClassroomEvent(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    kind: Literal["opening", "task", "request", "lesson_end", "organization"]
    text: str = Field(min_length=1, max_length=400)
    sources: list[str] = Field(min_length=1, max_length=12)


class QuestionUpdate(StrictModel):
    id: str = Field(max_length=60)
    topic: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=300)
    status: Literal["pending", "asked", "resolved", "deferred"]
    sources: list[str] = Field(min_length=1, max_length=12)
    resolution_sources: list[str] = Field(default_factory=list, max_length=12)


class Question(QuestionUpdate):
    attempts: int = 0


class Candidate(StrictModel):
    kind: Literal["silence", "answer", "question", "ack"] = "silence"
    text: str = Field(default="", max_length=600)
    question_id: str | None = None
    sources: list[str] = Field(default_factory=list, max_length=12)


class StudentResult(StrictModel):
    knowledge_updates: list[Knowledge] = Field(default_factory=list, max_length=16,
        description="仅定义、概念、规则、因果、方法和有依据的纠正结论；不包含教师提问或课堂组织事件")
    question_updates: list[QuestionUpdate] = Field(default_factory=list, max_length=12)
    event_updates: list[ClassroomEvent] = Field(default_factory=list, max_length=12)
    addressed: bool = False
    candidate: Candidate = Field(default_factory=Candidate)
    note: str = Field(default="", max_length=400)


class StudentState(StrictModel):
    version: int = 0
    knowledge: list[Knowledge] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list,
        validation_alias=AliasChoices("open_questions", "questions"))
    recent_events: list[ClassroomEvent] = Field(default_factory=list, max_length=20)
    knowledge_history: list[KnowledgeRevision] = Field(default_factory=list)

    @property
    def questions(self):
        """Compatibility for existing question lifecycle code and old log inputs."""
        return self.open_questions

    @questions.setter
    def questions(self, value):
        self.open_questions = value

    def learning_context(self):
        """Keep historical/conflicted content outside the current fact collection."""
        data = self.model_dump()
        data["knowledge"] = [k.model_dump() for k in self.knowledge
                             if k.status in ("tentative", "understood")]
        data["inactive_knowledge"] = [k.model_dump() for k in self.knowledge
                                      if k.status in ("conflict", "unclear")]
        return data
