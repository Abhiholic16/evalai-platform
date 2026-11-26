"""Validated API response schemas."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    assessment_code: str
    title: str
    subject_code: str
    subject_name: str
    test_code: str
    term: str
    date: str
    score: int = Field(ge=0)
    max_score: int = Field(gt=0)
    review_status: str
    student_roll_number: str
    class_code: str
    section: str


class FacultyLoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    roll_number: str
    name: str
    class_code: str
    section: str
    email: str
    program: str
    academic_year: str
    year: str
    branch: str
    semester: str
    admission_year: str


class StudentDetail(StudentOut):
    assessments: list[AssessmentOut]


class StudentList(BaseModel):
    source: str = "local prototype roster"
    count: int
    records: list[StudentOut]


class AssessmentList(BaseModel):
    source: str = "local prototype assessment records"
    count: int
    records: list[AssessmentOut]


class WorkspaceCreate(BaseModel):
    assessment_code: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=120)
    class_code: str = Field(min_length=1, max_length=20)
    section: str = Field(min_length=1, max_length=10)
    subject_code: str = Field(min_length=1, max_length=20)
    test_code: str = Field(min_length=1, max_length=20)
    test_date: str = Field(default="", max_length=30)
    question_source: str = Field(default="question_paper", pattern=r"^(question_paper|manual)$")
    rules: str = Field(default="", max_length=2000)
    marking_mode: str = Field(default="holistic_percentage", pattern=r"^(holistic_percentage|stepwise)$")
    correctness_threshold: float = Field(default=0.5, ge=0, le=1)
    score_bands: list[dict[str, str | int | float]] = Field(default_factory=lambda: [
        {"label": "Full credit", "minimum_correctness_percent": 50, "award_percent": 100},
        {"label": "Partial credit", "minimum_correctness_percent": 1, "award_percent": 50},
        {"label": "No credit", "minimum_correctness_percent": 0, "award_percent": 0},
    ], max_length=10)
    rubric_criteria: list[dict[str, str | int]] = Field(default_factory=list, max_length=30)
    questions: list["AssessmentQuestionInput"] = Field(default_factory=list, max_length=30)
    total_marks: int | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def total_matches_question_marks(self):
        if self.question_source == "manual" and not self.questions:
            raise ValueError("Manual question entry requires at least one valid question")
        if self.questions:
            question_ids = [question.question_id.lower() for question in self.questions]
            positions = [question.position for question in self.questions]
            if len(question_ids) != len(set(question_ids)):
                raise ValueError("Question IDs must be unique within an assessment")
            if len(positions) != len(set(positions)):
                raise ValueError("Question positions must be unique within an assessment")
            total = sum(question.maximum_marks for question in self.questions)
            if self.total_marks is not None and self.total_marks != total:
                raise ValueError("Total marks must equal the sum of question maximum marks")
            self.total_marks = total
        elif self.total_marks not in (None, 0):
            raise ValueError("Add assessment questions before setting total marks")
        return self


class AssessmentQuestionInput(BaseModel):
    question_id: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    position: int = Field(ge=1, le=30)
    prompt: str = Field(min_length=3, max_length=2000)
    model_answer: str = Field(default="", max_length=8000)
    maximum_marks: int = Field(default=10, gt=0, le=500)
    rubric_criteria: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("question_id", "prompt")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("model_answer")
    @classmethod
    def trim_optional_answer(cls, value: str) -> str:
        return value.strip()

    @field_validator("rubric_criteria")
    @classmethod
    def criteria_must_be_readable(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("Marking criteria cannot contain blank items")
        return cleaned


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    assessment_code: str
    title: str
    class_code: str
    section: str
    subject_code: str
    test_code: str
    test_date: str
    question_source: str = "question_paper"
    rules: str
    marking_mode: str = "holistic_percentage"
    correctness_threshold: float = 0.5
    score_bands: list[dict] = Field(default_factory=list)
    rubric_criteria: list[dict]
    questions: list["AssessmentQuestionOut"] = Field(default_factory=list)
    total_marks: int = 0


class AssessmentQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    question_id: str
    position: int
    prompt: str
    model_answer: str
    maximum_marks: int
    rubric_criteria: list[str]


class ExtractedPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    page_number: int
    raw_text: str
    corrected_text: str | None
    question_answers: list[dict]
    source: str
    confidence_label: str
    confidence_source: str
    needs_review: bool
    faculty_verified: bool


class ProcessingJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    state: str
    stage: str
    attempts: int
    page_count: int | None
    pages_done: int
    detail: str
    detected_roll: str | None
    match_method: str | None
    match_detail: str | None


class DocumentMatchInput(BaseModel):
    student_roll_number: str = Field(min_length=1, max_length=20)


class DocumentOut(BaseModel):
    job: ProcessingJobOut | None = None
    model_config = ConfigDict(from_attributes=True)
    id: int
    assessment_code: str
    student_roll_number: str | None
    document_type: str
    original_filename: str
    media_type: str
    file_size: int
    sha256: str
    status: str
    processing_mode: str
    status_message: str
    created_at: datetime | None = None


class DocumentDetail(DocumentOut):
    pages: list[ExtractedPageOut]


class DocumentList(BaseModel):
    count: int
    records: list[DocumentOut]


class DocumentUploadResult(BaseModel):
    document: DocumentOut
    duplicate: bool


class CorrectionInput(BaseModel):
    corrected_text: str = Field(max_length=20000)


class EvaluationQuestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1, max_length=30)
    maximum_marks: int = Field(gt=0, le=500)
    proposed_marks: int | None = Field(ge=0, le=500)
    reasoning: str = Field(min_length=1, max_length=2000)
    strengths: list[str] = Field(max_length=20)
    improvement_areas: list[str] = Field(max_length=20)
    uncertainty_flags: list[str] = Field(max_length=20)
    requires_faculty_review: bool

    @model_validator(mode="after")
    def marks_do_not_exceed_maximum(self):
        if self.proposed_marks is not None and self.proposed_marks > self.maximum_marks:
            raise ValueError("Proposed marks cannot exceed the question maximum")
        if not self.requires_faculty_review:
            raise ValueError("Faculty review is mandatory for every proposed mark")
        return self


class EvaluationLLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_results: list[EvaluationQuestionResult] = Field(min_length=1, max_length=30)


class EvaluationEditInput(BaseModel):
    question_results: list[EvaluationQuestionResult] = Field(min_length=1, max_length=30)


class EvaluationRejectInput(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def rejection_reason_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A short reason is required")
        return value


class EvaluationOut(BaseModel):
    id: int
    assessment_code: str
    student_roll_number: str
    student_name: str
    document_id: int
    status: str
    provider_mode: str
    model_name: str | None
    status_message: str
    question_snapshot: list[dict]
    answer_sources: list[dict]
    ai_proposed: list[EvaluationQuestionResult]
    faculty_edited: list[EvaluationQuestionResult] | None
    final_approved: list[EvaluationQuestionResult] | None
    ai_proposed_total: int
    faculty_edited_total: int | None
    final_approved_total: int | None
    total_marks: int
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None


class FeedbackEditInput(BaseModel):
    content: dict


class FeedbackOut(BaseModel):
    id: int
    evaluation_id: int
    assessment_code: str
    student_roll_number: str
    student_name: str
    student_email: str
    status: str
    content: dict
    delivery_status: str | None
    delivery_mode: str | None
    delivery_message: str | None
    delivery_attempts: int
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EmailDeliveryOut(BaseModel):
    id: int
    status: str
    provider_mode: str
    recipient: str
    message: str
    attempted_at: datetime
    completed_at: datetime | None
