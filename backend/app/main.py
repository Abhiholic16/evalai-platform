"""EvalAI API: faculty assessment and student record workflows."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from collections import defaultdict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from threading import Lock
from uuid import uuid4
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import distinct, func, select, update
from sqlalchemy.orm import Session, selectinload

from .database import Base, SessionLocal, engine, get_db, restrict_database_file_permissions
from .document_processing import (
    DocumentProcessingError, VisionProviderError, configured_vision_provider, process_document, split_questions,
)
from .evaluation import (
    EvaluationProviderError, configured_evaluation_provider, prepare_answers,
    redact_student_identifiers, result_total, source_fingerprint, validate_provider_results,
)
from .feedback import configured_email_provider, generate_feedback, render_feedback_pdf
from .security import COOKIE_NAME, SESSION_SECONDS, auth_settings, create_session, credentials_match, read_session
from .models import (
    Assessment, AssessmentEvaluation, AssessmentQuestion, AssessmentWorkspace, EmailDelivery, StudentFeedback,
    ExtractedPage, ProcessingJob, Student, UploadedDocument,
)
from .schemas import (
    AssessmentList, CorrectionInput, DocumentDetail, DocumentList, DocumentUploadResult,
    DocumentMatchInput, DocumentOut, EvaluationEditInput, EvaluationOut, ExtractedPageOut, StudentDetail, StudentOut,
    EvaluationRejectInput, FacultyLoginInput, FeedbackEditInput, FeedbackOut, EmailDeliveryOut, StudentList, WorkspaceCreate, WorkspaceOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth_settings()
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        # create_all does not add columns to an existing SQLite database. This
        # additive migration keeps stored workspaces and documents intact.
        with engine.begin() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(assessment_workspaces)")}
            additions = {
                "marking_mode": "VARCHAR(30) NOT NULL DEFAULT 'holistic_percentage'",
                "correctness_threshold": "FLOAT NOT NULL DEFAULT 0.5",
                "score_bands": "JSON NOT NULL DEFAULT '[]'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE assessment_workspaces ADD COLUMN {name} {definition}")
    restrict_database_file_permissions()
    yield


app = FastAPI(title="EvalAI API", description="Faculty assessment and student record API.", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

LOGIN_FAILURES: dict[str, deque[float]] = defaultdict(deque)
EMAIL_DELIVERY_LOCK = Lock()


def secure_cookie_enabled() -> bool:
    return os.getenv("EVALAI_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}


@app.middleware("http")
async def faculty_access_control(request: Request, call_next):
    path = request.url.path
    is_api = path.startswith("/api/")
    public_api = path in {"/api/health", "/api/auth/login", "/api/auth/session"}
    session = read_session(request.cookies.get(COOKIE_NAME)) if is_api and not public_api else None
    declared_length = request.headers.get("content-length", "")
    if path == "/api/documents" and request.method == "POST" and declared_length.isdigit() and int(declared_length) > MAX_UPLOAD_BYTES + 1_000_000:
        response = JSONResponse({"detail": "Upload request exceeds the 15 MB file limit."}, status_code=413)
    elif is_api and not public_api and session is None:
        response = JSONResponse({"detail": "Faculty sign-in is required."}, status_code=401)
    elif is_api and not public_api and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied, session["csrf"]):
            response = JSONResponse({"detail": "Request verification failed. Reload the faculty page and retry."}, status_code=403)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if not is_api:
        response.headers["Cache-Control"] = "no-store"
    if is_api:
        response.headers["Cache-Control"] = "no-store"
    if os.getenv("EVALAI_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/api/auth/session")
def get_auth_session(request: Request):
    try:
        username, _, _ = auth_settings()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    session = read_session(request.cookies.get(COOKIE_NAME))
    return {"authenticated": session is not None, "username": username if session else None,
            "csrf_token": session["csrf"] if session else None}


@app.post("/api/auth/login")
def faculty_login(payload: FacultyLoginInput, request: Request):
    try:
        auth_settings()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    client_ip = request.client.host if request.client else "unknown"
    failures = LOGIN_FAILURES[client_ip]
    cutoff = time.monotonic() - 60
    while failures and failures[0] < cutoff:
        failures.popleft()
    if len(failures) >= 10:
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait one minute and try again.")
    if not credentials_match(payload.username, payload.password):
        failures.append(time.monotonic())
        raise HTTPException(status_code=401, detail="The faculty username or password is incorrect.")
    failures.clear()
    token, csrf_token = create_session(payload.username)
    secure_cookie = secure_cookie_enabled()
    response = JSONResponse({"authenticated": True, "username": payload.username, "csrf_token": csrf_token})
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_cookie,
                        samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/auth/logout")
def faculty_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=secure_cookie_enabled(), samesite="strict")
    return response

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
PROTOTYPE_FILE = FRONTEND_DIR / "index.html"
INTEGRATION_SCRIPT = FRONTEND_DIR / "frontend-integration.js"
DOCUMENT_SCRIPT = FRONTEND_DIR / "document-workflow.js"
EVALUATION_SCRIPT = FRONTEND_DIR / "evaluation-workflow.js"
AUTH_SCRIPT = FRONTEND_DIR / "faculty-auth.js"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".doc", ".docx", ".xlsx"}
IMAGE_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif", "TIFF": "image/tiff"}


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


@app.get("/document-workflow.js", include_in_schema=False)
def serve_document_workflow():
    if not DOCUMENT_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="Document workflow script was not found")
    return FileResponse(DOCUMENT_SCRIPT, media_type="text/javascript")


@app.get("/evaluation-workflow.js", include_in_schema=False)
def serve_evaluation_workflow():
    if not EVALUATION_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="Evaluation workflow script was not found")
    return FileResponse(EVALUATION_SCRIPT, media_type="text/javascript")


FEEDBACK_SCRIPT = FRONTEND_DIR / "feedback-workflow.js"


@app.get("/feedback-workflow.js", include_in_schema=False)
def serve_feedback_workflow():
    if not FEEDBACK_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="Feedback workflow script was not found")
    return FileResponse(FEEDBACK_SCRIPT, media_type="text/javascript")


@app.get("/faculty-auth.js", include_in_schema=False)
def serve_faculty_auth_script():
    if not AUTH_SCRIPT.is_file():
        raise HTTPException(status_code=404, detail="Faculty sign-in script was not found")
    return FileResponse(AUTH_SCRIPT, media_type="text/javascript")


def upload_directory() -> Path:
    configured = os.getenv("EVALAI_UPLOAD_DIR")
    path = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[1] / "uploads"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    if os.name != "nt":
        resolved.chmod(0o700)
    return resolved


def safe_filename(value: str) -> str:
    filename = Path((value or "upload").replace("\\", "/")).name
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")
    if not filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail="Filename is empty or too long")
    return filename


def detect_upload_type(filename: str, content: bytes) -> tuple[str, str]:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Allowed files: PDF, PNG, JPG, WEBP, GIF, TIFF, DOC, DOCX, and XLSX")
    if extension == ".pdf":
        if b"%PDF-" not in content[:1024]:
            raise HTTPException(status_code=415, detail="The file extension says PDF, but its contents are not a PDF")
        return "application/pdf", extension
    if extension == ".doc":
        if not content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            raise HTTPException(status_code=415, detail="The file contents do not match the DOC file extension")
        return "application/msword", extension
    if extension in {".docx", ".xlsx"}:
        try:
            with ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
        except BadZipFile as exc:
            raise HTTPException(status_code=415, detail="The Office document is invalid") from exc
        marker = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
        if marker not in names:
            raise HTTPException(status_code=415, detail="The file contents do not match the Office file extension")
        return ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if extension == ".docx"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), extension
    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format
            if image.width * image.height > 30_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions exceed the 30 megapixel safety limit")
            image.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=415, detail="The uploaded image is invalid or unsupported") from exc
    media_type = IMAGE_MIME.get(image_format or "")
    extension_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff"}
    expected = extension_types.get(extension, "image/" + extension.lstrip("."))
    if not media_type or media_type != expected:
        raise HTTPException(status_code=415, detail="The file contents do not match the image extension")
    return media_type, extension


def workspace_out(workspace: AssessmentWorkspace) -> dict:
    questions = sorted(workspace.questions, key=lambda question: question.position)
    return {
        "assessment_code": workspace.assessment_code, "title": workspace.title,
        "class_code": workspace.class_code, "section": workspace.section,
        "subject_code": workspace.subject_code, "test_code": workspace.test_code,
        "test_date": workspace.test_date, "rules": workspace.rules,
        "question_source": "manual" if workspace.questions else "question_paper",
        "marking_mode": workspace.marking_mode,
        "correctness_threshold": workspace.correctness_threshold,
        "score_bands": workspace.score_bands or [],
        "rubric_criteria": workspace.rubric_criteria,
        "questions": questions,
        "total_marks": sum(question.maximum_marks for question in questions),
    }


@app.get("/api/assessment-workspaces", response_model=list[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db)):
    return db.scalars(select(AssessmentWorkspace).options(selectinload(AssessmentWorkspace.questions))
                      .order_by(AssessmentWorkspace.title)).all()


@app.delete("/api/assessment-workspaces/{assessment_code}")
def delete_workspace(assessment_code: str, db: Session = Depends(get_db)):
    """Delete one faculty workspace and only its owned processing/evaluation data."""
    workspace = db.scalar(select(AssessmentWorkspace).where(
        AssessmentWorkspace.assessment_code == assessment_code
    ).options(selectinload(AssessmentWorkspace.documents)))
    if workspace is None:
        raise HTTPException(status_code=404, detail="Assessment workspace not found")

    # Capture paths before the transaction removes metadata. Files are removed only
    # after commit, so a failed transaction cannot orphan unrelated records.
    files = []
    for document in workspace.documents:
        files.append(upload_directory() / document.stored_filename)
    try:
        # Stop queued/running work from being recreated by a stale worker: remove
        # jobs in the same transaction and mark the workspace gone before commit.
        db.query(ProcessingJob).filter(ProcessingJob.document_id.in_(
            select(UploadedDocument.id).where(UploadedDocument.assessment_code == assessment_code)
        )).delete(synchronize_session=False)
        db.query(EmailDelivery).filter(EmailDelivery.feedback_id.in_(
            select(StudentFeedback.id).where(StudentFeedback.assessment_code == assessment_code)
        )).delete(synchronize_session=False)
        db.query(StudentFeedback).filter(StudentFeedback.assessment_code == assessment_code).delete(synchronize_session=False)
        db.query(AssessmentEvaluation).filter(AssessmentEvaluation.assessment_code == assessment_code).delete(synchronize_session=False)
        db.query(UploadedDocument).filter(UploadedDocument.assessment_code == assessment_code).delete(synchronize_session=False)
        db.query(AssessmentQuestion).filter(AssessmentQuestion.assessment_code == assessment_code).delete(synchronize_session=False)
        db.delete(workspace)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": True, "assessment_code": assessment_code}


@app.get("/api/assessment-workspaces/{assessment_code}/review")
def workspace_review(assessment_code: str, db: Session = Depends(get_db)):
    """Return one authoritative review payload, including submissions not yet graded."""
    workspace = db.scalar(select(AssessmentWorkspace).options(
        selectinload(AssessmentWorkspace.questions), selectinload(AssessmentWorkspace.documents)
    ).where(AssessmentWorkspace.assessment_code == assessment_code))
    if workspace is None:
        raise HTTPException(status_code=404, detail="Assessment workspace not found")
    documents = db.scalars(select(UploadedDocument).options(
        selectinload(UploadedDocument.student), selectinload(UploadedDocument.pages)
    ).where(UploadedDocument.assessment_code == assessment_code)
        .order_by(UploadedDocument.created_at, UploadedDocument.id)).all()
    rolls = {doc.student.roll_number for doc in documents if doc.document_type == "answer_sheet" and doc.student}
    students = db.scalars(select(Student).where(Student.roll_number.in_(rolls)).order_by(Student.roll_number)).all() if rolls else []
    evaluations = db.scalars(select(AssessmentEvaluation).where(
        AssessmentEvaluation.assessment_code == assessment_code
    ).order_by(AssessmentEvaluation.created_at.desc())).all()
    latest = {}
    for evaluation in evaluations:
        latest.setdefault(evaluation.student.roll_number, evaluation)
    return {
        "workspace": workspace_out(workspace),
        "reference_documents": [DocumentDetail.model_validate(doc).model_dump(mode="json") for doc in documents if doc.document_type in {"question_paper", "model_answer", "rubric"}],
        "roster": [StudentOut.model_validate(student).model_dump() for student in db.scalars(select(Student).where(
            Student.class_code == workspace.class_code, Student.section == workspace.section
        ).order_by(Student.roll_number))],
        "unmatched_documents": [DocumentDetail.model_validate(doc).model_dump(mode="json") for doc in documents if doc.document_type == "answer_sheet" and not doc.student_id],
        "submissions": [{
            "student": StudentOut.model_validate(student).model_dump(),
            "answer_sheets": [DocumentDetail.model_validate(doc).model_dump(mode="json") for doc in documents if doc.student_id == student.id and doc.document_type == "answer_sheet"],
            "evaluation": evaluation_out(latest[student.roll_number]) if student.roll_number in latest else None,
        } for student in students],
    }


@app.post("/api/assessment-workspaces", response_model=WorkspaceOut)
def create_workspace(payload: WorkspaceCreate, db: Session = Depends(get_db)):
    if payload.class_code not in {"ENGG1", "ENGG2", "ENGG3", "ENGG4"} or payload.section not in {"A", "B"}:
        raise HTTPException(status_code=422, detail="Choose an Engineering year and Section A or B")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload.subject_code) or not re.fullmatch(r"[A-Za-z0-9_-]+", payload.test_code):
        raise HTTPException(status_code=422, detail="Subject and assessment codes may contain letters, numbers, hyphens, and underscores")
    code = payload.assessment_code or "asm-" + uuid4().hex[:16]
    workspace = db.get(AssessmentWorkspace, code)
    fields = payload.model_dump(exclude={"assessment_code", "questions", "total_marks", "question_source"})
    if workspace is None:
        workspace = AssessmentWorkspace(assessment_code=code, **fields)
        db.add(workspace)
    else:
        for field_name, value in fields.items():
            setattr(workspace, field_name, value)
    if "questions" in payload.model_fields_set:
        db.query(AssessmentQuestion).filter(AssessmentQuestion.assessment_code == code).delete()
        for question in payload.questions:
            db.add(AssessmentQuestion(assessment_code=code, **question.model_dump()))
    resume_waiting_submissions(db, code)
    db.commit()
    workspace = db.scalar(select(AssessmentWorkspace).options(selectinload(AssessmentWorkspace.questions))
                          .where(AssessmentWorkspace.assessment_code == code))
    return workspace_out(workspace)


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy", "project": "EvalAI"}


@app.get("/api/diagnostics/runtime")
def runtime_diagnostics(db: Session = Depends(get_db)):
    """Return non-secret effective configuration and the newest durable job state."""
    latest = db.scalar(select(UploadedDocument).order_by(UploadedDocument.created_at.desc()))
    job = latest.job if latest else None
    return {
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "database": str(engine.url.database),
        "providers": {"ocr": os.getenv("EVALAI_OCR_PROVIDER", "demo"),
                       "evaluation": os.getenv("EVALAI_EVALUATION_PROVIDER", "demo")},
        "external_processing_enabled": external_processing_enabled(),
        "gemini_api_key_present": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "latest_job": None if latest is None else {
            "document_id": latest.id, "filename": latest.original_filename,
            "status": latest.status, "processing_mode": latest.processing_mode,
            "status_message": latest.status_message, "created_at": latest.created_at.isoformat(),
            "job_state": job.state if job else None, "job_stage": job.stage if job else None,
            "attempts": job.attempts if job else None, "allow_external": job.allow_external if job else None,
            "detail": job.detail if job else None,
        },
    }


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


def get_document_or_404(db: Session, document_id: int) -> UploadedDocument:
    document = db.scalar(
        select(UploadedDocument).options(
            selectinload(UploadedDocument.pages), selectinload(UploadedDocument.student)
        ).where(UploadedDocument.id == document_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Uploaded document not found")
    return document


@app.post("/api/documents", response_model=DocumentUploadResult, status_code=201)
async def upload_document(
    assessment_code: str = Form(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$"),
    document_type: str = Form(pattern=r"^(question_paper|model_answer|rubric|answer_sheet)$"),
    student_roll_number: str | None = Form(default=None, max_length=20),
    synthetic_data: bool = Form(default=False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    workspace = db.get(AssessmentWorkspace, assessment_code)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Assessment workspace not found. Save the assessment setup first.")
    student = None
    if document_type == "answer_sheet" and student_roll_number:
        student = db.scalar(select(Student).where(Student.roll_number == student_roll_number))
        if student is None or student.class_code != workspace.class_code or student.section != workspace.section:
            raise HTTPException(status_code=422, detail="The selected student does not belong to this assessment class and section")
    elif student_roll_number:
        raise HTTPException(status_code=422, detail="Only answer sheets can be associated with an individual student")

    filename = safe_filename(file.filename or "upload")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Files must be 15 MB or smaller")
    media_type, extension = detect_upload_type(filename, content)
    if document_type == "answer_sheet" and extension in {".docx", ".xlsx"}:
        raise HTTPException(status_code=415, detail="Student answer sheets must be PDF or image files")

    digest = hashlib.sha256(content).hexdigest()
    # Serialize duplicate lookup and insert across API processes.
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    duplicate = db.scalar(select(UploadedDocument).where(
        UploadedDocument.assessment_code == assessment_code,
        UploadedDocument.document_type == document_type,
        UploadedDocument.sha256 == digest,
    ))
    if duplicate:
        return {"document": get_document_or_404(db, duplicate.id), "duplicate": True}
    if student and db.scalar(select(UploadedDocument.id).where(
        UploadedDocument.assessment_code == assessment_code,
        UploadedDocument.document_type == "answer_sheet",
        UploadedDocument.student_id == student.id,
    ).limit(1)):
        raise HTTPException(status_code=409, detail="This student already has an answer sheet in this assessment.")

    stored_filename = uuid4().hex + extension
    destination = upload_directory() / stored_filename
    try:
        with destination.open("xb") as stored_file:
            stored_file.write(content)
        if os.name != "nt":
            destination.chmod(0o600)
        document = UploadedDocument(
            assessment_code=assessment_code, student_id=student.id if student else None,
            document_type=document_type, original_filename=filename, stored_filename=stored_filename,
            media_type=media_type, file_size=len(content), sha256=digest,
        )
        document.job = ProcessingJob(
            # The environment is the deployment-wide safety switch.  The old
            # code only enabled Gemini for the hidden synthetic_data form field,
            # so ordinary uploads always took the local-only path.
            allow_external=synthetic_data or external_processing_enabled(),
            match_method="manual" if student else None,
        )
        db.add(document)
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise
    return {"document": get_document_or_404(db, document.id), "duplicate": False}


@app.get("/api/documents", response_model=DocumentList)
def list_documents(
    assessment_code: str | None = Query(default=None, max_length=40),
    student_roll_number: str | None = Query(default=None, max_length=20),
    db: Session = Depends(get_db),
):
    statement = select(UploadedDocument).options(selectinload(UploadedDocument.student))
    if assessment_code:
        statement = statement.where(UploadedDocument.assessment_code == assessment_code)
    if student_roll_number:
        student = db.scalar(select(Student).where(Student.roll_number == student_roll_number))
        if student is None:
            return {"count": 0, "records": []}
        statement = statement.where(UploadedDocument.student_id == student.id)
    rows = db.scalars(statement.order_by(UploadedDocument.created_at.desc())).all()
    return {"count": len(rows), "records": rows}


@app.get("/api/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: int, db: Session = Depends(get_db)):
    return get_document_or_404(db, document_id)


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: int, db: Session = Depends(get_db)):
    document = db.get(UploadedDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Uploaded document not found")
    stored_path = (upload_directory() / document.stored_filename).resolve()
    if stored_path.parent != upload_directory() or not stored_path.is_file():
        raise HTTPException(status_code=404, detail="Stored document file is unavailable")
    return FileResponse(stored_path, media_type=document.media_type, filename=document.original_filename,
                        content_disposition_type="inline")


@app.post("/api/documents/{document_id}/process", response_model=DocumentDetail, status_code=202)
def queue_uploaded_document(document_id: int, retry: bool = Query(default=False), db: Session = Depends(get_db)):
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    document = get_document_or_404(db, document_id)
    job = document.job
    if job is None:
        document.job = ProcessingJob(match_method="manual" if document.student_id else None)
    elif retry and job.state not in {"queued", "running"}:
        job.state = "queued"
        job.detail = "Waiting for the local processing worker."
        job.attempts = 0
        # Re-evaluate the deployment policy on every retry. This repairs jobs
        # created before external processing was enabled without changing the
        # original uploaded file or any student data.
        job.allow_external = external_processing_enabled()
    db.commit()
    return get_document_or_404(db, document_id)


@app.put("/api/documents/{document_id}/match", response_model=DocumentDetail)
def confirm_document_match(document_id: int, payload: DocumentMatchInput, db: Session = Depends(get_db)):
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    document = get_document_or_404(db, document_id)
    if document.document_type != "answer_sheet":
        raise HTTPException(status_code=422, detail="Only answer sheets can be matched.")
    if document.job and document.job.state == "running":
        raise HTTPException(status_code=409, detail="Wait for processing to finish before changing the match.")
    if document.student_id and db.scalar(select(AssessmentEvaluation.id).where(
        AssessmentEvaluation.document_id == document_id
    ).limit(1)):
        raise HTTPException(status_code=409, detail="This sheet has an evaluation. Resolve it before changing the match.")
    student = db.scalar(select(Student).where(
        Student.roll_number == payload.student_roll_number,
        Student.class_code == document.workspace.class_code,
        Student.section == document.workspace.section,
    ))
    if student is None:
        raise HTTPException(status_code=422, detail="Choose a student from this assessment's roster.")
    if another_submission(db, document, student.id):
        raise HTTPException(status_code=409, detail="This student already has a different answer sheet in this assessment.")
    document.student_id = student.id
    if document.job is None:
        document.job = ProcessingJob()
    document.job.match_method = "manual"
    document.job.match_detail = "Faculty confirmed this match."
    document.job.allow_external = external_processing_enabled()
    document.job.state = "queued"
    document.job.attempts = 0
    document.job.detail = "Waiting for the local processing worker."
    db.commit()
    return get_document_or_404(db, document_id)


def external_processing_enabled():
    """Return whether configured uploads may use the selected external provider."""
    return os.getenv("EVALAI_ALLOW_EXTERNAL", "").strip().lower() in {"1", "true", "yes"}


def can_use_external(document):
    return external_processing_enabled() and bool(document.job and document.job.allow_external)


def another_submission(db, document, student_id):
    return db.scalar(select(UploadedDocument.id).where(
        UploadedDocument.assessment_code == document.assessment_code,
        UploadedDocument.document_type == "answer_sheet",
        UploadedDocument.student_id == student_id,
        UploadedDocument.id != document.id,
    ).limit(1))


def extract_uploaded_document(document_id: int, db: Session, *, allow_external=False, progress=None, cached_pages=None):
    document = get_document_or_404(db, document_id)
    stored_path = (upload_directory() / document.stored_filename).resolve()
    if stored_path.parent != upload_directory() or not stored_path.is_file():
        document.status = "failed"
        document.status_message = "Stored file is missing; re-upload this document."
        db.commit()
        return get_document_or_404(db, document.id)

    extension = Path(document.original_filename).suffix.lower()
    if extension in {".doc", ".docx", ".xlsx"}:
        document.status = "stored_only"
        document.processing_mode = "office_file_not_processed"
        document.status_message = "File stored. Office document text extraction is not included; export it as PDF to process it."
        db.commit()
        return get_document_or_404(db, document.id)

    document.status = "processing"
    document.status_message = "Extracting pages in the local worker."
    db.commit()
    try:
        vision_provider, provider_mode, provider_message = (
            configured_vision_provider() if allow_external else
            (None, "local_only", "Scanned content requires local transcription or faculty correction.")
        )
        processed = process_document(stored_path, document.media_type, document.document_type, vision_provider,
                                     progress=progress, cached_pages=cached_pages)
        if progress is None:
            for page in processed["pages"]:
                save_extracted_page(db, document.id, page)
        document.status = processed["status"]
        document.processing_mode = (
            "gemini_vision" if any(page["source"] == "gemini_vision" for page in processed["pages"])
            else "pdf_text" if any(page["source"] == "pdf_text" for page in processed["pages"])
            else provider_mode
        )
        document.status_message = processed["message"]
        if document.status == "awaiting_provider" and not vision_provider:
            document.status_message = provider_message + " No answer text was generated."
        # Seed question records from an extractable question paper. This remains an
        # editable draft: marks use an explicit editable 10-mark default and answers
        # are never copied from the question paper.
        if document.document_type in {"question_paper", "model_answer"}:
            associate_references(db, document, processed["pages"])
            resume_waiting_submissions(db, document.assessment_code)
        db.commit()
    except DocumentProcessingError as exc:
        db.rollback()
        document = db.get(UploadedDocument, document_id)
        document.status = "failed"
        document.status_message = str(exc)
        db.commit()
    except VisionProviderError as exc:
        db.rollback()
        document = db.get(UploadedDocument, document_id)
        document.status = "failed"
        document.processing_mode = "gemini_vision"
        document.status_message = str(exc)
        db.commit()
    except Exception as exc:
        db.rollback()
        document = db.get(UploadedDocument, document_id)
        document.status = "failed"
        document.status_message = "Document processing failed unexpectedly. Check the backend log and retry."
        db.commit()
        raise HTTPException(status_code=500, detail=document.status_message) from exc
    return get_document_or_404(db, document_id)


def save_extracted_page(db, document_id, values):
    existing = db.scalar(select(ExtractedPage).where(
        ExtractedPage.document_id == document_id, ExtractedPage.page_number == values["page_number"],
    ))
    if existing is None:
        db.add(ExtractedPage(document_id=document_id, **values))
    elif not existing.faculty_verified and existing.corrected_text is None:
        for key, value in values.items():
            setattr(existing, key, value)


def associate_references(db, document, pages):
    current = db.scalars(select(AssessmentQuestion).where(
        AssessmentQuestion.assessment_code == document.assessment_code
    ).order_by(AssessmentQuestion.position)).all()

    def sections(source_pages):
        for page in source_pages:
            for item in page.get("question_answers") or split_questions(page.get("raw_text", "")):
                number = re.search(r"\d+", str(item.get("question", "")))
                text = str(item.get("raw_text", "")).strip()
                if number and text:
                    yield f"Q{int(number[0])}", text

    if document.document_type == "question_paper" and not current:
        drafts = dict(sections(pages))
        for position, (question_id, prompt) in enumerate(list(drafts.items())[:30], 1):
            question = AssessmentQuestion(
                assessment_code=document.assessment_code, question_id=question_id, position=position,
                prompt=prompt[:2000], model_answer="", maximum_marks=10, rubric_criteria=[],
            )
            current.append(question)
            db.add(question)
    if document.document_type == "model_answer":
        model_pages = pages
    else:
        stored_model = db.scalar(select(UploadedDocument).where(
            UploadedDocument.assessment_code == document.assessment_code,
            UploadedDocument.document_type == "model_answer",
            UploadedDocument.status.in_(["extracted", "partial"]),
        ).order_by(UploadedDocument.id.desc()))
        model_pages = [{"raw_text": p.raw_text, "question_answers": p.question_answers}
                       for p in stored_model.pages] if stored_model else []
    answers = dict(sections(model_pages))
    for question in current:
        if not question.model_answer and question.question_id.upper() in answers:
            question.model_answer = answers[question.question_id.upper()][:8000]


def resume_waiting_submissions(db, assessment_code):
    db.execute(update(ProcessingJob).where(
        ProcessingJob.state == "needs_attention", ProcessingJob.stage == "references",
        ProcessingJob.document_id.in_(select(UploadedDocument.id).where(UploadedDocument.assessment_code == assessment_code)),
    ).values(state="queued", attempts=0, detail="Assessment references updated. Waiting for the worker."))


@app.put("/api/documents/{document_id}/pages/{page_number}", response_model=ExtractedPageOut)
def correct_extracted_page(document_id: int, page_number: int, payload: CorrectionInput, db: Session = Depends(get_db)):
    page = db.scalar(select(ExtractedPage).where(
        ExtractedPage.document_id == document_id, ExtractedPage.page_number == page_number
    ))
    if page is None:
        raise HTTPException(status_code=404, detail="Extracted page not found")
    page.corrected_text = payload.corrected_text
    page.faculty_verified = True
    page.needs_review = False
    job = db.get(ProcessingJob, document_id)
    if job and job.state == "needs_attention":
        job.state, job.attempts = "queued", 0
        job.detail = "Correction saved. Waiting for the worker."
    db.commit()
    db.refresh(page)
    return page


def get_evaluation_or_404(db: Session, evaluation_id: int) -> AssessmentEvaluation:
    evaluation = db.scalar(select(AssessmentEvaluation).options(
        selectinload(AssessmentEvaluation.student),
    ).where(AssessmentEvaluation.id == evaluation_id))
    if evaluation is None:
        raise HTTPException(status_code=404, detail="Evaluation record not found")
    return evaluation


def evaluation_out(evaluation: AssessmentEvaluation) -> dict:
    marks = [question["maximum_marks"] for question in evaluation.question_snapshot]
    return {
        "id": evaluation.id, "assessment_code": evaluation.assessment_code,
        "student_roll_number": evaluation.student.roll_number,
        "student_name": evaluation.student.name, "document_id": evaluation.document_id,
        "status": evaluation.status, "provider_mode": evaluation.provider_mode,
        "model_name": evaluation.model_name, "status_message": evaluation.status_message,
        "question_snapshot": evaluation.question_snapshot, "answer_sources": evaluation.answer_sources,
        "ai_proposed": evaluation.ai_proposed, "faculty_edited": evaluation.faculty_edited,
        "final_approved": evaluation.final_approved,
        "ai_proposed_total": result_total(evaluation.ai_proposed),
        "faculty_edited_total": result_total(evaluation.faculty_edited) if evaluation.faculty_edited is not None else None,
        "final_approved_total": result_total(evaluation.final_approved) if evaluation.final_approved is not None else None,
        "total_marks": sum(marks), "rejection_reason": evaluation.rejection_reason,
        "created_at": evaluation.created_at, "updated_at": evaluation.updated_at,
        "decided_at": evaluation.decided_at,
    }


@app.post("/api/assessments/{assessment_code}/students/{roll_number}/evaluations", response_model=EvaluationOut)
def evaluate_student_answers(assessment_code: str, roll_number: str, db: Session = Depends(get_db)):
    active = db.scalar(select(ProcessingJob.document_id).join(UploadedDocument).join(Student).where(
        UploadedDocument.assessment_code == assessment_code, Student.roll_number == roll_number,
        ProcessingJob.state.in_(["queued", "running"]),
    ).limit(1))
    if active:
        raise HTTPException(status_code=409, detail="The background worker is already handling this submission.")
    return evaluate_submission(assessment_code, roll_number, db)


def evaluate_submission(assessment_code: str, roll_number: str, db: Session, *, document_id=None):
    workspace = db.scalar(select(AssessmentWorkspace).options(
        selectinload(AssessmentWorkspace.questions),
    ).where(AssessmentWorkspace.assessment_code == assessment_code))
    if workspace is None:
        raise HTTPException(status_code=404, detail="Assessment workspace not found")
    questions = sorted(workspace.questions, key=lambda question: question.position)
    if not questions:
        raise HTTPException(status_code=422, detail="Add or extract at least one question before evaluation")

    student = db.scalar(select(Student).where(Student.roll_number == roll_number))
    if student is None:
        raise HTTPException(status_code=404, detail="Student record not found")
    if student.class_code != workspace.class_code or student.section != workspace.section:
        raise HTTPException(status_code=422, detail="This student does not belong to the assessment class and section")

    document = db.scalar(select(UploadedDocument).options(
        selectinload(UploadedDocument.pages),
    ).where(
        UploadedDocument.assessment_code == assessment_code,
        UploadedDocument.student_id == student.id,
        UploadedDocument.document_type == "answer_sheet",
    ).order_by(UploadedDocument.created_at.desc()))
    if document is None:
        raise HTTPException(status_code=404, detail="Upload a student answer sheet before evaluation")
    if document_id is not None and document.id != document_id:
        raise HTTPException(status_code=409, detail="A different answer sheet is active for this student.")
    if (os.getenv("EVALAI_EVALUATION_PROVIDER", "demo").strip().lower() == "gemini"
            and not can_use_external(document)):
        raise HTTPException(status_code=422, detail="This upload is restricted to local processing. Faculty can review and mark it locally.")

    question_snapshot = [{
        "question_id": question.question_id, "position": question.position,
        "prompt": question.prompt, "model_answer": question.model_answer,
        "maximum_marks": question.maximum_marks, "rubric_criteria": question.rubric_criteria,
    } for question in questions]
    answer_map, answer_sources, missing_ids = prepare_answers(document, question_snapshot)
    visual_mode = os.getenv("EVALAI_EVALUATION_PROVIDER", "demo").strip().lower() == "gemini"
    if visual_mode:
        # Direct visual grading deliberately does not require extracted text. Keep page references
        # for the faculty UI and audit trail; extraction remains optional setup/audit data.
        answer_sources = [{"document_id": document.id, "page_number": page.page_number,
                           "source": "original_answer_sheet", "question_id": None}
                          for page in document.pages]
    policy = {"mode": workspace.marking_mode, "threshold": workspace.correctness_threshold,
              "bands": workspace.score_bands, "rules": workspace.rules, "rubric": workspace.rubric_criteria}
    fingerprint = source_fingerprint(assessment_code, document.id, question_snapshot + [policy], answer_map)
    legacy_fingerprint = source_fingerprint(assessment_code, document.id, question_snapshot, answer_map)
    existing_query = select(AssessmentEvaluation).options(
        selectinload(AssessmentEvaluation.student),
    ).where(
        AssessmentEvaluation.assessment_code == assessment_code,
        AssessmentEvaluation.student_id == student.id,
        AssessmentEvaluation.source_fingerprint.in_([fingerprint, legacy_fingerprint]),
        AssessmentEvaluation.status != "rejected",
    ).order_by(AssessmentEvaluation.created_at.desc())
    existing = db.scalar(existing_query)
    if existing is not None:
        return evaluation_out(existing)

    if len(missing_ids) == len(questions) and not visual_mode:
        missing_results = [
            {
                "question_id": question["question_id"], "maximum_marks": question["maximum_marks"],
                "proposed_marks": None,
                "reasoning": "Not evaluated because no readable answer text is available for this question.",
                "strengths": [], "improvement_areas": [],
                "uncertainty_flags": ["missing_or_unreadable_answer", "manual_review_required"],
                "requires_faculty_review": True,
            }
            for question in question_snapshot
        ]
        evaluation = AssessmentEvaluation(
            assessment_code=assessment_code, student_id=student.id, document_id=document.id,
            status="needs_manual_review", provider_mode="not_evaluated", model_name=None,
            source_fingerprint=fingerprint, question_snapshot=question_snapshot,
            answer_sources=answer_sources, ai_proposed=missing_results,
            status_message="No model call was made. Every question has missing or unreadable answer text; add a faculty correction before evaluation.",
        )
        return save_evaluation_once(db, evaluation, existing_query, question_snapshot, answer_map, policy)

    question_inputs = []
    requested_questions = []
    for question in question_snapshot:
        answer_text = answer_map.get(question["question_id"], "")
        if not answer_text and not visual_mode:
            continue
        requested_questions.append(question)
        question_inputs.append({
            **question, "student_answer": redact_student_identifiers(answer_text, student),
            "marking_mode": workspace.marking_mode,
            "correctness_threshold": workspace.correctness_threshold,
            "score_bands": workspace.score_bands or [],
        })

    try:
        provider = configured_evaluation_provider()
        rubric = (workspace.rubric_criteria or []) + [{
                "marking_mode": workspace.marking_mode,
                "correctness_threshold": workspace.correctness_threshold,
                "score_bands": workspace.score_bands or [],
            }]
        if visual_mode and hasattr(provider.evaluator, "evaluate_visual"):
            provider_response = provider.evaluator.evaluate_visual(
                (upload_directory() / document.stored_filename).resolve(), question_inputs, rubric, workspace.rules or "")
        else:
            provider_response = provider.evaluator.evaluate(question_inputs, rubric, workspace.rules or "")
        returned = validate_provider_results(provider_response, requested_questions, allow_unscored=visual_mode)
    except EvaluationProviderError as exc:
        raise HTTPException(status_code=503 if "not configured" in str(exc).lower() else 502, detail=str(exc)) from exc

    proposed = []
    for question in question_snapshot:
        result = returned.get(question["question_id"])
        if result is None:
            result = {
                "question_id": question["question_id"], "maximum_marks": question["maximum_marks"],
                "proposed_marks": None,
                "reasoning": "Not evaluated because no readable answer text is available for this question.",
                "strengths": [], "improvement_areas": [],
                "uncertainty_flags": ["missing_or_unreadable_answer", "manual_review_required"],
                "requires_faculty_review": True,
            }
        else:
            result = dict(result)
            if not question.get("model_answer"):
                result["uncertainty_flags"] = list(dict.fromkeys((result.get("uncertainty_flags") or []) + ["model_answer_not_provided"]))
                result["requires_faculty_review"] = True
        proposed.append(result)

    status_message = (
        "Simulated evaluation marks. These fixed placeholder marks do not assess answer quality; enable Gemini evaluation for rubric recommendations."
        if provider.mode == "demo_simulation" else
        "Gemini direct visual grading is provisional. Faculty review and approval are required before marks are official."
    )
    if missing_ids and not visual_mode:
        status_message += " One or more questions have no readable answer and remain unscored until corrected."
    evaluation = AssessmentEvaluation(
        assessment_code=assessment_code, student_id=student.id, document_id=document.id,
        status="provisional", provider_mode=provider.mode, model_name=provider.model_name,
        source_fingerprint=fingerprint, question_snapshot=question_snapshot,
        answer_sources=answer_sources, ai_proposed=proposed, status_message=status_message,
    )
    return save_evaluation_once(db, evaluation, existing_query, question_snapshot, answer_map, policy)


def save_evaluation_once(db, evaluation, existing_query, questions, answers, policy):
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    existing = db.scalar(existing_query)
    if existing is not None:
        return evaluation_out(existing)
    db.expire_all()
    document = db.get(UploadedDocument, evaluation.document_id)
    workspace = document.workspace
    current_policy = {"mode": workspace.marking_mode, "threshold": workspace.correctness_threshold,
                      "bands": workspace.score_bands, "rules": workspace.rules, "rubric": workspace.rubric_criteria}
    current = sorted(workspace.questions, key=lambda q: q.position)
    if (document.student_id != evaluation.student_id or policy != current_policy
            or [q["question_id"] for q in questions] != [q.question_id for q in current]
            or any(any(getattr(q, key) != value for key, value in snapshot.items())
                   for q, snapshot in zip(current, questions))
            or prepare_answers(document, questions)[0] != answers):
        raise HTTPException(status_code=409, detail="The source changed during evaluation. Retry with the saved inputs.")
    db.add(evaluation)
    db.commit()
    return evaluation_out(get_evaluation_or_404(db, evaluation.id))


@app.get("/api/evaluations", response_model=list[EvaluationOut])
def list_evaluations(
    assessment_code: str | None = Query(default=None, max_length=40),
    student_roll_number: str | None = Query(default=None, max_length=20),
    db: Session = Depends(get_db),
):
    statement = select(AssessmentEvaluation).options(selectinload(AssessmentEvaluation.student))
    if assessment_code:
        statement = statement.where(AssessmentEvaluation.assessment_code == assessment_code)
    if student_roll_number:
        student = db.scalar(select(Student).where(Student.roll_number == student_roll_number))
        if student is None:
            return []
        statement = statement.where(AssessmentEvaluation.student_id == student.id)
    evaluations = db.scalars(statement.order_by(AssessmentEvaluation.created_at.desc())).all()
    return [evaluation_out(evaluation) for evaluation in evaluations]


@app.get("/api/evaluations/{evaluation_id}", response_model=EvaluationOut)
def get_evaluation(evaluation_id: int, db: Session = Depends(get_db)):
    return evaluation_out(get_evaluation_or_404(db, evaluation_id))


@app.put("/api/evaluations/{evaluation_id}/edits", response_model=EvaluationOut)
def edit_evaluation(evaluation_id: int, payload: EvaluationEditInput, db: Session = Depends(get_db)):
    evaluation = get_evaluation_or_404(db, evaluation_id)
    if evaluation.status not in {"provisional", "edited"}:
        raise HTTPException(status_code=409, detail="Only a provisional evaluation can be edited")
    originals = {item["question_id"]: item for item in evaluation.ai_proposed}
    maxima = {item["question_id"]: item["maximum_marks"] for item in evaluation.question_snapshot}
    edited = {}
    for result in payload.question_results:
        if result.question_id not in originals or result.question_id in edited:
            raise HTTPException(status_code=422, detail="The edit must contain each assessment question exactly once")
        if result.maximum_marks != maxima[result.question_id]:
            raise HTTPException(status_code=422, detail="Question maximum marks cannot be changed during review")
        if originals[result.question_id]["proposed_marks"] is None and result.proposed_marks is not None:
            raise HTTPException(status_code=422, detail="Correct the missing answer text and create a new evaluation before entering marks")
        edited[result.question_id] = result.model_dump()
    if set(edited) != set(originals):
        raise HTTPException(status_code=422, detail="The edit must contain each assessment question exactly once")
    evaluation.faculty_edited = [edited[item["question_id"]] for item in evaluation.ai_proposed]
    evaluation.status = "edited"
    evaluation.status_message = "Faculty edits are saved separately; the original AI proposal is preserved. Approval is still required."
    db.commit()
    return evaluation_out(get_evaluation_or_404(db, evaluation.id))


@app.post("/api/evaluations/{evaluation_id}/approve", response_model=EvaluationOut)
def approve_evaluation(evaluation_id: int, db: Session = Depends(get_db)):
    evaluation = get_evaluation_or_404(db, evaluation_id)
    if evaluation.status == "approved":
        return evaluation_out(evaluation)
    if evaluation.status not in {"provisional", "edited"}:
        raise HTTPException(status_code=409, detail="This evaluation is not ready for approval")
    approved_results = evaluation.faculty_edited if evaluation.faculty_edited is not None else evaluation.ai_proposed
    if not approved_results or any(result["proposed_marks"] is None for result in approved_results):
        raise HTTPException(status_code=422, detail="Cannot approve while one or more answers are missing or unreadable. Correct the extraction and evaluate again.")

    workspace = db.get(AssessmentWorkspace, evaluation.assessment_code)
    maximum = sum(question["maximum_marks"] for question in evaluation.question_snapshot)
    score = result_total(approved_results)
    student = db.get(Student, evaluation.student_id)
    existing_mark = db.scalar(select(Assessment).where(
        Assessment.student_id == student.id,
        Assessment.assessment_code == evaluation.assessment_code,
    ))
    subject_names = {}
    official_mark = existing_mark or Assessment(
        student_id=student.id, assessment_code=workspace.assessment_code,
        title=workspace.title, subject_code=workspace.subject_code,
        subject_name=subject_names.get(workspace.subject_code, workspace.subject_code),
        test_code=workspace.test_code, term="Fall 2026", date=workspace.test_date or "Date not set",
        score=score, max_score=maximum,
    )
    official_mark.score = score
    official_mark.max_score = maximum
    official_mark.review_status = (
        "Faculty approved Gemini evaluation" if evaluation.provider_mode == "gemini"
        else "Faculty approved demo evaluation"
    )
    if existing_mark is None:
        db.add(official_mark)
    evaluation.final_approved = approved_results
    evaluation.status = "approved"
    evaluation.decided_at = datetime.now(timezone.utc)
    evaluation.status_message = "Faculty approved these marks. The approved score is now the official assessment record."
    db.commit()
    return evaluation_out(get_evaluation_or_404(db, evaluation.id))


@app.post("/api/evaluations/{evaluation_id}/reject", response_model=EvaluationOut)
def reject_evaluation(evaluation_id: int, payload: EvaluationRejectInput, db: Session = Depends(get_db)):
    evaluation = get_evaluation_or_404(db, evaluation_id)
    if evaluation.status == "approved":
        raise HTTPException(status_code=409, detail="An approved evaluation cannot be rejected; create a corrected evaluation instead")
    if evaluation.status == "rejected":
        return evaluation_out(evaluation)
    evaluation.status = "rejected"
    evaluation.rejection_reason = payload.reason
    evaluation.decided_at = datetime.now(timezone.utc)
    evaluation.status_message = "Faculty rejected this evaluation. It is not an official mark."
    db.commit()
    return evaluation_out(get_evaluation_or_404(db, evaluation.id))


def feedback_out(db: Session, feedback: StudentFeedback) -> dict:
    deliveries = db.scalars(select(EmailDelivery).where(EmailDelivery.feedback_id == feedback.id)
                             .order_by(EmailDelivery.id.desc())).all()
    latest = deliveries[0] if deliveries else None
    return {
        "id": feedback.id, "evaluation_id": feedback.evaluation_id,
        "assessment_code": feedback.assessment_code,
        "student_roll_number": feedback.student.roll_number, "student_name": feedback.student.name,
        "student_email": feedback.student.email, "status": feedback.status, "content": feedback.content,
        "delivery_status": latest.status if latest else "pending",
        "delivery_mode": latest.provider_mode if latest else None,
        "delivery_message": latest.message if latest else "Feedback has not been delivered.",
        "delivery_attempts": len(deliveries), "approved_at": feedback.approved_at,
        "created_at": feedback.created_at, "updated_at": feedback.updated_at,
    }


def get_feedback_or_404(db: Session, feedback_id: int) -> StudentFeedback:
    feedback = db.scalar(select(StudentFeedback).options(selectinload(StudentFeedback.student))
                         .where(StudentFeedback.id == feedback_id))
    if feedback is None:
        raise HTTPException(status_code=404, detail="Feedback record was not found")
    return feedback


@app.post("/api/evaluations/{evaluation_id}/feedback", response_model=FeedbackOut)
def create_feedback(evaluation_id: int, db: Session = Depends(get_db)):
    evaluation = db.get(AssessmentEvaluation, evaluation_id)
    if evaluation is None:
        raise HTTPException(status_code=404, detail="Evaluation record was not found")
    if evaluation.status != "approved" or not evaluation.final_approved:
        raise HTTPException(status_code=409, detail="Feedback can only be generated from faculty-approved evaluation results")
    feedback = db.scalar(select(StudentFeedback).where(StudentFeedback.evaluation_id == evaluation_id))
    if feedback is None:
        try:
            content = generate_feedback(evaluation)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        feedback = StudentFeedback(evaluation_id=evaluation.id, student_id=evaluation.student_id,
                                   assessment_code=evaluation.assessment_code, content=content, status="draft")
        db.add(feedback)
        db.commit()
        feedback = db.scalar(select(StudentFeedback).options(selectinload(StudentFeedback.student))
                             .where(StudentFeedback.id == feedback.id))
    return feedback_out(db, feedback)


@app.get("/api/feedback", response_model=list[FeedbackOut])
def list_feedback(student_roll_number: str | None = Query(default=None, max_length=20),
                  assessment_code: str | None = Query(default=None, max_length=40),
                  db: Session = Depends(get_db)):
    statement = select(StudentFeedback).options(selectinload(StudentFeedback.student))
    if student_roll_number:
        student = db.scalar(select(Student).where(Student.roll_number == student_roll_number))
        if student is None:
            return []
        statement = statement.where(StudentFeedback.student_id == student.id)
    if assessment_code:
        statement = statement.where(StudentFeedback.assessment_code == assessment_code)
    records = db.scalars(statement.order_by(StudentFeedback.created_at.desc())).all()
    return [feedback_out(db, feedback) for feedback in records]


@app.get("/api/feedback/{feedback_id}", response_model=FeedbackOut)
def get_feedback(feedback_id: int, db: Session = Depends(get_db)):
    return feedback_out(db, get_feedback_or_404(db, feedback_id))


@app.put("/api/feedback/{feedback_id}", response_model=FeedbackOut)
def edit_feedback(feedback_id: int, payload: FeedbackEditInput, db: Session = Depends(get_db)):
    feedback = get_feedback_or_404(db, feedback_id)
    if feedback.status != "draft":
        raise HTTPException(status_code=409, detail="Only draft feedback can be edited")
    original = feedback.content
    edited = payload.content
    if len(json.dumps(edited, ensure_ascii=False)) > 60000:
        raise HTTPException(status_code=413, detail="Feedback draft is too large")
    expected_content_keys = {"assessment_code", "score", "maximum_marks", "percentage", "overall_summary",
                             "overall_strengths", "improvement_areas", "questions", "source", "demo_result"}
    if set(edited) != expected_content_keys:
        raise HTTPException(status_code=422, detail="Feedback draft has an invalid structure")
    if (edited.get("assessment_code") != feedback.assessment_code or
            edited.get("score") != original["score"] or edited.get("maximum_marks") != original["maximum_marks"] or
            edited.get("percentage") != original["percentage"] or edited.get("source") != original["source"] or
            edited.get("demo_result") != original["demo_result"]):
        raise HTTPException(status_code=422, detail="Feedback edits cannot change approved marks or their source")
    questions = edited.get("questions")
    expected_question_keys = {"question_id", "prompt", "marks", "maximum_marks", "strengths", "missing_concepts", "suggestions"}
    if (not isinstance(questions, list) or any(not isinstance(question, dict) or set(question) != expected_question_keys for question in questions)
            or [q.get("question_id") for q in questions] != [q["question_id"] for q in original["questions"]]):
        raise HTTPException(status_code=422, detail="Feedback must retain every approved question in its original order")
    for candidate, baseline in zip(questions, original["questions"]):
        if candidate.get("marks") != baseline["marks"] or candidate.get("maximum_marks") != baseline["maximum_marks"]:
            raise HTTPException(status_code=422, detail="Question marks cannot be changed in feedback")
        for key in ("prompt", "strengths", "missing_concepts", "suggestions"):
            value = candidate.get(key)
            if key == "prompt":
                if value != baseline[key]:
                    raise HTTPException(status_code=422, detail="Question text cannot be changed in feedback")
            elif not isinstance(value, list) or len(value) > 20 or any(not isinstance(item, str) or len(item) > 500 for item in value):
                raise HTTPException(status_code=422, detail="Question feedback must contain short text items")
    for key in ("overall_summary",):
        if not isinstance(edited.get(key), str) or not edited[key].strip() or len(edited[key]) > 2000:
            raise HTTPException(status_code=422, detail="Overall summary must be non-empty and under 2,000 characters")
    for key in ("overall_strengths", "improvement_areas"):
        value = edited.get(key)
        if not isinstance(value, list) or len(value) > 40 or any(not isinstance(item, str) or len(item) > 500 for item in value):
            raise HTTPException(status_code=422, detail="Feedback lists must contain short text items")
    feedback.content = edited
    db.commit()
    return feedback_out(db, get_feedback_or_404(db, feedback.id))


@app.post("/api/feedback/{feedback_id}/approve", response_model=FeedbackOut)
def approve_feedback(feedback_id: int, db: Session = Depends(get_db)):
    feedback = get_feedback_or_404(db, feedback_id)
    if feedback.status == "approved":
        return feedback_out(db, feedback)
    if feedback.status != "draft":
        raise HTTPException(status_code=409, detail="Feedback is not a draft")
    evaluation = db.get(AssessmentEvaluation, feedback.evaluation_id)
    if evaluation is None or evaluation.status != "approved" or not evaluation.final_approved:
        raise HTTPException(status_code=409, detail="The source evaluation is no longer faculty-approved")
    feedback.status = "approved"
    feedback.approved_at = datetime.now(timezone.utc)
    db.commit()
    return feedback_out(db, get_feedback_or_404(db, feedback.id))


@app.get("/api/feedback/{feedback_id}/pdf")
def download_feedback_pdf(feedback_id: int, db: Session = Depends(get_db)):
    feedback = get_feedback_or_404(db, feedback_id)
    if feedback.status != "approved":
        raise HTTPException(status_code=409, detail="Approve feedback before downloading its report")
    data = render_feedback_pdf(feedback.student.name, feedback.student.roll_number, feedback.content)
    filename = f"EvalAI_Feedback_{feedback.student.roll_number}_{feedback.assessment_code}.pdf"
    return StreamingResponse(BytesIO(data), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/feedback/{feedback_id}/deliveries", response_model=list[EmailDeliveryOut])
def feedback_deliveries(feedback_id: int, db: Session = Depends(get_db)):
    feedback = get_feedback_or_404(db, feedback_id)
    records = db.scalars(select(EmailDelivery).where(EmailDelivery.feedback_id == feedback.id)
                         .order_by(EmailDelivery.id)).all()
    return records


@app.post("/api/feedback/{feedback_id}/deliver", response_model=FeedbackOut)
def deliver_feedback(feedback_id: int, db: Session = Depends(get_db)):
    # The selected deployment is single-process. Serialize the check-and-send window
    # so two faculty clicks cannot race past the successful-send check.
    with EMAIL_DELIVERY_LOCK:
        return _deliver_feedback_serialized(feedback_id, db)


def _deliver_feedback_serialized(feedback_id: int, db: Session):
    feedback = get_feedback_or_404(db, feedback_id)
    if feedback.status != "approved":
        raise HTTPException(status_code=409, detail="Approve feedback before delivery")
    previous = db.scalars(select(EmailDelivery).where(EmailDelivery.feedback_id == feedback.id)
                          .order_by(EmailDelivery.id.desc())).all()
    if any(item.status == "sent" for item in previous):
        raise HTTPException(status_code=409, detail="This approved feedback was already sent successfully; duplicate delivery is blocked")
    try:
        provider = configured_email_provider()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    attempt = EmailDelivery(feedback_id=feedback.id, status="pending", provider_mode=provider.mode,
                           recipient=feedback.student.email, message="Delivery attempt started.")
    db.add(attempt)
    db.commit()
    try:
        pdf = render_feedback_pdf(feedback.student.name, feedback.student.roll_number, feedback.content)
        message = provider.send(feedback.student.email,
                                f"EvalAI feedback: {feedback.assessment_code}",
                                feedback.content["overall_summary"], pdf)
        attempt.status = "simulated" if provider.mode == "simulation" else "sent"
        attempt.message = message
    except Exception:
        attempt.status = "failed"
        attempt.message = "Delivery failed. Check the configured email provider, then retry."
    attempt.completed_at = datetime.now(timezone.utc)
    db.commit()
    return feedback_out(db, get_feedback_or_404(db, feedback.id))


@app.get("/{requested_path:path}", include_in_schema=False)
def serve_frontend_deep_link(requested_path: str):
    """Return the app shell for browser history/deep links, never for API paths."""
    if (requested_path.startswith("api/") or requested_path.endswith((".js", ".css", ".map")) or
            not (requested_path in {"dashboard", "feedback", "setup", "classReview", "studentReview"} or
                 requested_path.startswith("assessment/"))):
        raise HTTPException(status_code=404, detail="Not found")
    if not PROTOTYPE_FILE.is_file():
        raise HTTPException(status_code=404, detail="EvalAI prototype file was not found")
    return FileResponse(PROTOTYPE_FILE, media_type="text/html")
