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


class KnowledgeDependency(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    revision: int = Field(ge=1, description="引用上下文中该知识的修订号；本轮新知识使用 1")


class Knowledge(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=400)
    status: Literal["tentative", "understood", "unclear", "conflict", "invalid"]
    sources: list[str] = Field(min_length=1, max_length=12)
    conditions: str = Field(default="", max_length=300, description="适用范围；空字符串表示未限定")
    reason: str = Field(default="", max_length=400, description="必填的简短状态依据，指出理解链条、跳步或障碍")
    revision: int = Field(default=1, ge=1, description="输出已有条目的当前修订号，程序负责递增")
    dependencies: list[KnowledgeDependency] = Field(default_factory=list, max_length=16,
        description="实际采用的推导前提；独立依据成立时移除不再需要的依赖并解释原因")
    independent_sources: list[str] = Field(default_factory=list, max_length=12,
        description="移除旧依赖必须提供本轮教师独立解释的来源；不能因前提失效就删除依赖")
    correction_sources: list[str] = Field(default_factory=list, max_length=12,
        description="教师明确指出旧说法有误、撤回或替代的 t 来源；仅说不同规则或应该如何不是纠正，必须为空并记录 conflict")
    supersedes: list[str] = Field(default_factory=list, max_length=12,
                                  description="明确撤回/纠正旧说法才填写；教师仅提出不同说法（即使说应该）时为空，记录 conflict")


class KnowledgeRevision(StrictModel):
    version: int
    previous: Knowledge
    replacement_id: str
    invalidated: bool = False
    change_reason: str = ""
    change_sources: list[str] = Field(default_factory=list)


class KnowledgeConflict(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    left_id: str = Field(min_length=1, max_length=60)
    right_id: str = Field(min_length=1, max_length=60)
    conditions: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=400)
    sources: list[str] = Field(min_length=1, max_length=24)
    status: Literal["open", "resolved"] = "open"
    resolution: str = Field(default="", max_length=400)
    resolution_sources: list[str] = Field(default_factory=list, max_length=12)


class KnowledgeReview(StrictModel):
    dependency_revisions: dict[str, int] = Field(default_factory=dict)
    knowledge_id: str
    revision: int
    reason: str
    trigger_sources: list[str] = Field(default_factory=list)
    status: Literal["pending", "failed"] = "pending"
    error: str = ""


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


class ResponseRequestUpdate(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=400)
    sources: list[str] = Field(min_length=1, max_length=12)
    status: Literal['pending', 'dismissed'] = 'pending'
    reason: str = Field(default='', max_length=400)
    resolution_sources: list[str] = Field(default_factory=list, max_length=12)
    kind: Literal['answer', 'invite_questions', 'check_understanding', 'check_audio'] = 'answer'
    invitation_source_id: str | None = None
    invitation_quote: str = Field(default='', max_length=500)


class ResponseRequest(ResponseRequestUpdate):
    status: Literal['pending', 'answered', 'dismissed'] = 'pending'
    input_revision: int = 0  # Set by the program, never supplied by the model.


class QuestionReleaseRequest(StrictModel):
    question_id: str = Field(min_length=1, max_length=60)
    response_request_id: str = Field(min_length=1, max_length=60)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class Candidate(StrictModel):
    question_release_request: QuestionReleaseRequest | None = None
    response_request_ids: list[str] = Field(default_factory=list,
        description='实际回应的教师请求 ID；question 仅关联 invite_questions；silence 填 []；发出前不算 answered')
    knowledge_ids: list[str] = Field(default_factory=list, max_length=16,
        description="实际用于推导答案的可用知识前提 ID；仅复述、描述障碍或对比冲突双方不算采用前提，这些条目不列入；无推理前提时填 []，来源仍填 sources")
    kind: Literal["silence", "answer", "question", "ack"] = "silence"
    text: str = Field(default="", max_length=600)
    question_id: str | None = Field(default=None,
        description='question 必须引用当前未解决疑问，或在本轮 question_updates 中同时登记的疑问 ID')
    sources: list[str] = Field(default_factory=list, max_length=12)


class StudentResult(StrictModel):
    response_request_updates: list[ResponseRequestUpdate] = Field(default_factory=list, max_length=12)
    conflict_updates: list[KnowledgeConflict] = Field(default_factory=list, max_length=12)
    knowledge_updates: list[Knowledge] = Field(default_factory=list, max_length=16,
        description="仅定义、概念、规则、因果、方法和有依据的纠正结论；不包含教师提问或课堂组织事件")
    question_updates: list[QuestionUpdate] = Field(default_factory=list, max_length=12,
        description='新增追问必须在这里登记，ID 与 candidate.question_id 一致；已有未解决疑问可直接引用')
    event_updates: list[ClassroomEvent] = Field(default_factory=list, max_length=12)
    addressed: bool = False
    candidate: Candidate = Field(default_factory=Candidate)
    note: str = Field(default="", max_length=400)


class ResumeResult(StrictModel):
    response_request_updates: list[ResponseRequestUpdate] = Field(default_factory=list, max_length=12)
    question_updates: list[QuestionUpdate] = Field(default_factory=list, max_length=12)
    candidate: Candidate = Field(default_factory=Candidate)
    note: str = Field(default='', max_length=400)


class KnowledgeReviewResult(StrictModel):
    knowledge_updates: list[Knowledge] = Field(min_length=1, max_length=16)
    note: str = Field(default="", max_length=400)


class StudentState(StrictModel):
    schema_version: Literal[2] = 2
    response_requests: list[ResponseRequest] = Field(default_factory=list)
    conflicts: list[KnowledgeConflict] = Field(default_factory=list)
    pending_reviews: list[KnowledgeReview] = Field(default_factory=list)
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
        """仅确定且已完成复核的知识进入事实集合，旧日志不在此迁移。"""
        data = self.model_dump()
        blocked = {r.knowledge_id for r in self.pending_reviews}
        conflicted = {kid for c in self.conflicts if c.status == "open"
                      for kid in (c.left_id, c.right_id)}
        blocked |= conflicted
        data["knowledge"] = [k.model_dump() for k in self.knowledge
                             if k.status == "understood" and k.id not in blocked]
        data["tentative_knowledge"] = [k.model_dump() for k in self.knowledge
                                       if k.status == "tentative" and k.id not in blocked]
        data["inactive_knowledge"] = [k.model_dump() for k in self.knowledge
                                      if k.status in ("conflict", "unclear", "invalid") or k.id in blocked]
        return data
