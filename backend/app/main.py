"""EvalAI demo API: health, student records, and assessment lookups."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session, selectinload

from .database import Base, engine, get_db
from .models import Assessment, Student
from .schemas import AssessmentList, StudentDetail, StudentList
from .seed import seed_demo_data


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        seed_demo_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title="EvalAI API", description="Local demo API with fictional ABC College data.", lifespan=lifespan)

PROTOTYPE_FILE = Path(__file__).resolve().parents[2] / "evalai-prototypefinal"
INTEGRATION_SCRIPT = Path(__file__).resolve().parents[2] / "frontend-integration.js"


@app.get("/", include_in_schema=False)
def serve_prototype():
    """Serve the existing single-file frontend from the API's origin."""
    if not PROTOTYPE_FILE.is_file():
        raise HTTPException(status_code=404, detail="EvalAI prototype file was not found")
    return FileResponse(PROTOTYPE_FILE, media_type="text/html")


@app.get("/frontend-integration.js", include_in_schema=False)
def serve_frontend_integration():
    if not INTEGRATION_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="Frontend integration script was not found")
    return FileResponse(INTEGRATION_SCRIPT, media_type="text/javascript")


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy", "project": "EvalAI"}


def student_filters(statement, search, class_code, section, subject, test):
    if subject or test:
        statement = statement.join(Student.assessments)
    if search:
        term = f"%{search.strip()}%"
        statement = statement.where((Student.name.ilike(term)) | (Student.roll_number.ilike(term)))
    if class_code:
        statement = statement.where(Student.class_code == class_code)
    if section:
        statement = statement.where(Student.section == section)
    if subject:
        statement = statement.where(Assessment.subject_code == subject)
    if test:
        statement = statement.where(Assessment.test_code == test)
    return statement


@app.get("/api/students", response_model=StudentList)
def list_students(
    search: str | None = Query(default=None, min_length=1, max_length=100),
    class_code: str | None = Query(default=None, alias="class", min_length=1, max_length=20),
    section: str | None = Query(default=None, min_length=1, max_length=10),
    subject: str | None = Query(default=None, min_length=1, max_length=20),
    test: str | None = Query(default=None, min_length=1, max_length=20),
    limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    statement = student_filters(select(Student).distinct(), search, class_code, section, subject, test)
    records = db.scalars(statement.order_by(Student.roll_number).offset(offset).limit(limit)).all()
    count_statement = student_filters(select(Student.id).distinct(), search, class_code, section, subject, test)
    count = len(db.scalars(count_statement).all())
    return {"count": count, "records": records}


@app.get("/api/students/{roll_number}", response_model=StudentDetail)
def get_student(roll_number: str, db: Session = Depends(get_db)):
    student = db.scalar(select(Student).options(selectinload(Student.assessments)).where(Student.roll_number == roll_number))
    if student is None:
        raise HTTPException(status_code=404, detail="Student record not found")
    return student


@app.get("/api/assessments", response_model=AssessmentList)
def list_assessments(
    search: str | None = Query(default=None, min_length=1, max_length=100),
    class_code: str | None = Query(default=None, alias="class", min_length=1, max_length=20),
    section: str | None = Query(default=None, min_length=1, max_length=10),
    subject: str | None = Query(default=None, min_length=1, max_length=20),
    test: str | None = Query(default=None, min_length=1, max_length=20),
    limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    statement = select(Assessment).join(Assessment.student)
    if search:
        term = f"%{search.strip()}%"
        statement = statement.where((Student.name.ilike(term)) | (Student.roll_number.ilike(term)))
    if class_code:
        statement = statement.where(Student.class_code == class_code)
    if section:
        statement = statement.where(Student.section == section)
    if subject:
        statement = statement.where(Assessment.subject_code == subject)
    if test:
        statement = statement.where(Assessment.test_code == test)
    count_statement = select(func.count(distinct(Assessment.id))).select_from(Assessment).join(Assessment.student)
    if search:
        term = f"%{search.strip()}%"
        count_statement = count_statement.where((Student.name.ilike(term)) | (Student.roll_number.ilike(term)))
    if class_code:
        count_statement = count_statement.where(Student.class_code == class_code)
    if section:
        count_statement = count_statement.where(Student.section == section)
    if subject:
        count_statement = count_statement.where(Assessment.subject_code == subject)
    if test:
        count_statement = count_statement.where(Assessment.test_code == test)
    count = db.scalar(count_statement) or 0
    records = db.scalars(statement.order_by(Assessment.id).offset(offset).limit(limit)).all()
    return {"count": count, "records": records}
