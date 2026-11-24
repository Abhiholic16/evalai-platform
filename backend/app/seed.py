"""Load repeatable, fictional ABC College demo records."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Assessment, Student

STUDENTS = [
    ("2401", "Ananya Rao", "BSC1", "A"), ("2402", "Kabir Shah", "BSC1", "A"),
    ("2403", "Mira Iyer", "BSC1", "A"), ("2404", "Dev Patel", "BSC1", "A"),
    ("2405", "Sara Thomas", "BSC1", "A"), ("2411", "Ishaan Mehta", "BSC1", "B"),
    ("2412", "Nisha Kapoor", "BSC1", "B"), ("2413", "Rohan Das", "BSC1", "B"),
    ("2421", "Tara Joseph", "BSC2", "A"), ("2422", "Arjun Bose", "BSC2", "A"),
]
ASSESSMENTS = [
    ("bio-ut1", "Unit Test 1", "BIO101", "Introduction to Biology", "UT1", "Fall 2026", "18 Nov 2026", 20, "BSC1", "A"),
    ("bio-ut2", "Unit Test 2", "BIO101", "Introduction to Biology", "UT2", "Fall 2026", "22 Nov 2026", 20, "BSC1", "A"),
    ("chem-mid", "Midterm Examination", "CHEM101", "Chemistry Fundamentals", "MID", "Fall 2026", "12 Nov 2026", 50, "BSC1", "B"),
    ("eco-sem", "Semester 1 Examination", "ECO201", "Ecology", "SEM1", "Fall 2026", "08 Nov 2026", 100, "BSC2", "A"),
]
SCORES = {"bio-ut1": {"2401": 14, "2402": 16, "2403": 11, "2405": 17},
          "bio-ut2": {"2401": 16, "2402": 15, "2403": 12, "2405": 18},
          "chem-mid": {"2411": 34, "2413": 29}, "eco-sem": {"2421": 72, "2422": 81}}


def seed_demo_data(db: Session) -> None:
    """Insert sample records once; safe to call each time the API starts."""
    if db.scalar(select(Student.id).limit(1)) is not None:
        return
    students = {}
    for roll, name, class_code, section in STUDENTS:
        year = "Year 1" if class_code == "BSC1" else "Year 2"
        admission_year = "2026" if class_code == "BSC1" else "2025"
        student = Student(roll_number=roll, name=name, class_code=class_code, section=section,
            email=f"{name.lower().replace(' ', '.')}@example.test", program="B.Sc. Biology",
            academic_year="2026–27" if class_code == "BSC1" else "2025–26", year=year,
            admission_year=admission_year)
        db.add(student)
        students[roll] = student
    db.flush()
    for code, title, subject, subject_name, test, term, date, maximum, class_code, section in ASSESSMENTS:
        for roll, score in SCORES[code].items():
            db.add(Assessment(student=students[roll], assessment_code=code, title=title,
                subject_code=subject, subject_name=subject_name, test_code=test, term=term,
                date=date, score=score, max_score=maximum, review_status="Simulated demo result"))
    db.commit()
