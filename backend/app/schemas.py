"""Validated API response schemas."""
from pydantic import BaseModel, ConfigDict, Field


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


class StudentOut(BaseModel):
    id: int
    roll_number: str
    name: str
    class_code: str
    section: str
    email: str
    program: str
    academic_year: str
    year: str
    admission_year: str


class StudentDetail(StudentOut):
    assessments: list[AssessmentOut]


class StudentList(BaseModel):
    source: str = "fictional ABC College SQLite demo data"
    count: int
    records: list[StudentOut]


class AssessmentList(BaseModel):
    source: str = "fictional ABC College SQLite demo data"
    count: int
    records: list[AssessmentOut]
