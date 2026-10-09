from typing import Annotated, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lesson(StrictModel):
    topic: str = Field(min_length=1, max_length=500)
    points: str = Field(default="", max_length=3000)
    prerequisites: str = Field(default="", max_length=2000)
    level: str = Field(default="普通初学者，保留日常语言与基本逻辑能力", max_length=300)


class Preparation(StrictModel):
    scope: list[str] = Field(max_length=12)
    boundary_note: str = Field(max_length=400)


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
