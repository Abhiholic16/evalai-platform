"""Database models for fictional students and assessments."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    roll_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    class_code: Mapped[str] = mapped_column(String(20), index=True)
    section: Mapped[str] = mapped_column(String(10), index=True)
    email: Mapped[str] = mapped_column(String(254))
    program: Mapped[str] = mapped_column(String(100))
    academic_year: Mapped[str] = mapped_column(String(20))
    year: Mapped[str] = mapped_column(String(20))
    admission_year: Mapped[str] = mapped_column(String(4))
    assessments: Mapped[list["Assessment"]] = relationship(back_populates="student", cascade="all, delete-orphan")


class Assessment(Base):
    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("student_id", "assessment_code", name="uq_student_assessment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    assessment_code: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(120))
    subject_code: Mapped[str] = mapped_column(String(20), index=True)
    subject_name: Mapped[str] = mapped_column(String(120))
    test_code: Mapped[str] = mapped_column(String(20), index=True)
    term: Mapped[str] = mapped_column(String(40))
    date: Mapped[str] = mapped_column(String(20))
    score: Mapped[int] = mapped_column(Integer)
    max_score: Mapped[int] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(30), default="Simulated")
    student: Mapped[Student] = relationship(back_populates="assessments")

    @property
    def student_roll_number(self) -> str:
        return self.student.roll_number

    @property
    def class_code(self) -> str:
        return self.student.class_code

    @property
    def section(self) -> str:
        return self.student.section


class AssessmentWorkspace(Base):
    """Faculty-created assessment context used to attach reference files and scans."""
    __tablename__ = "assessment_workspaces"

    assessment_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    class_code: Mapped[str] = mapped_column(String(20), index=True)
    section: Mapped[str] = mapped_column(String(10), index=True)
    subject_code: Mapped[str] = mapped_column(String(20), index=True)
    test_code: Mapped[str] = mapped_column(String(20), index=True)
    test_date: Mapped[str] = mapped_column(String(30), default="")
    rules: Mapped[str] = mapped_column(String(2000), default="")
    rubric_criteria: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    documents: Mapped[list["UploadedDocument"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    questions: Mapped[list["AssessmentQuestion"]] = relationship(back_populates="workspace", cascade="all, delete-orphan", order_by="AssessmentQuestion.position")
    evaluations: Mapped[list["AssessmentEvaluation"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class AssessmentQuestion(Base):
    """Faculty-authored question, answer key, and question-specific marking rubric."""
    __tablename__ = "assessment_questions"
    __table_args__ = (
        UniqueConstraint("assessment_code", "question_id", name="uq_assessment_question_id"),
        UniqueConstraint("assessment_code", "position", name="uq_assessment_question_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_code: Mapped[str] = mapped_column(ForeignKey("assessment_workspaces.assessment_code"), index=True)
    question_id: Mapped[str] = mapped_column(String(30))
    position: Mapped[int] = mapped_column(Integer)
    prompt: Mapped[str] = mapped_column(String(2000))
    model_answer: Mapped[str] = mapped_column(String(8000))
    maximum_marks: Mapped[int] = mapped_column(Integer)
    rubric_criteria: Mapped[list] = mapped_column(JSON, default=list)
    workspace: Mapped[AssessmentWorkspace] = relationship(back_populates="questions")


class AssessmentEvaluation(Base):
    """Immutable AI proposal with separately persisted faculty edits and final decision."""
    __tablename__ = "assessment_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_code: Mapped[str] = mapped_column(ForeignKey("assessment_workspaces.assessment_code"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("uploaded_documents.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="provisional", index=True)
    provider_mode: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    question_snapshot: Mapped[list] = mapped_column(JSON)
    answer_sources: Mapped[list] = mapped_column(JSON)
    ai_proposed: Mapped[list] = mapped_column(JSON)
    faculty_edited: Mapped[list | None] = mapped_column(JSON, nullable=True)
    final_approved: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status_message: Mapped[str] = mapped_column(String(500), default="AI-generated marks are provisional until faculty approval.")
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workspace: Mapped[AssessmentWorkspace] = relationship(back_populates="evaluations")
    student: Mapped[Student] = relationship()
    document: Mapped["UploadedDocument"] = relationship()


class StudentFeedback(Base):
    """Faculty-reviewable feedback created only from an approved evaluation."""
    __tablename__ = "student_feedback"
    __table_args__ = (UniqueConstraint("evaluation_id", name="uq_feedback_evaluation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("assessment_evaluations.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    assessment_code: Mapped[str] = mapped_column(ForeignKey("assessment_workspaces.assessment_code"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    content: Mapped[dict] = mapped_column(JSON)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    evaluation: Mapped[AssessmentEvaluation] = relationship()
    student: Mapped[Student] = relationship()


class EmailDelivery(Base):
    """One persisted delivery attempt; simulated attempts are never marked sent."""
    __tablename__ = "email_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("student_feedback.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider_mode: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str] = mapped_column(String(254))
    message: Mapped[str] = mapped_column(String(500))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UploadedDocument(Base):
    """Document metadata only; file bytes live under the configured upload directory."""
    __tablename__ = "uploaded_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_code: Mapped[str] = mapped_column(ForeignKey("assessment_workspaces.assessment_code"), index=True)
    student_id: Mapped[int | None] = mapped_column(ForeignKey("students.id"), nullable=True, index=True)
    document_type: Mapped[str] = mapped_column(String(30), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(100), unique=True)
    media_type: Mapped[str] = mapped_column(String(100))
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="uploaded", index=True)
    processing_mode: Mapped[str] = mapped_column(String(40), default="not_processed")
    status_message: Mapped[str] = mapped_column(String(500), default="Uploaded; extraction has not run.")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    workspace: Mapped[AssessmentWorkspace] = relationship(back_populates="documents")
    student: Mapped[Student | None] = relationship()
    pages: Mapped[list["ExtractedPage"]] = relationship(back_populates="document", cascade="all, delete-orphan", order_by="ExtractedPage.page_number")

    @property
    def student_roll_number(self) -> str | None:
        return self.student.roll_number if self.student else None


class ExtractedPage(Base):
    """One immutable raw extraction and optional faculty correction per page."""
    __tablename__ = "extracted_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("uploaded_documents.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(String(20000), default="")
    corrected_text: Mapped[str | None] = mapped_column(String(20000), nullable=True)
    question_answers: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(40))
    confidence_label: Mapped[str] = mapped_column(String(20), default="unknown")
    confidence_source: Mapped[str] = mapped_column(String(40), default="none")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    faculty_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    document: Mapped[UploadedDocument] = relationship(back_populates="pages")
