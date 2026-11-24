"""Database models for fictional students and assessments."""
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
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
