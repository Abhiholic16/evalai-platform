"""Run the local SQLite worker with python -m app.worker."""
import argparse
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from threading import Timer
import time
import unicodedata

from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import select

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .database import Base, SessionLocal, engine
from .evaluation import prepare_answers
from .main import another_submission, can_use_external, evaluate_submission, extract_uploaded_document, save_extracted_page
from .models import ProcessingJob, Student, UploadedDocument

logger = logging.getLogger("evalai.worker")
JOB_TIMEOUT = 420
PAGE_FIELDS = ("page_number", "raw_text", "question_answers", "source",
               "confidence_label", "confidence_source", "needs_review")
ROLL_LABEL = re.compile(
    r"\broll[\s._-]*(?:number|no\.?|#)?\s*[:#=.-]?\s*([A-Z]*\d(?:[ -]?\d){3,17})(?![\w-])",
    re.IGNORECASE,
)
FILENAME_ROLL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]*[0-9]{4,18})(?![A-Za-z0-9])")


def normalize_roll(value):
    return re.sub(r"[\s-]+", "", unicodedata.normalize("NFKC", value)).upper()


def match_roll(db, document):
    job = document.job
    page = next((page for page in document.pages if page.page_number == 1), None)
    text = (page.corrected_text if page.corrected_text is not None else page.raw_text) if page else ""
    visible = {normalize_roll(value) for value in ROLL_LABEL.findall(unicodedata.normalize("NFKC", text))}
    filename = {normalize_roll(value) for value in FILENAME_ROLL.findall(document.original_filename)}
    job.detected_roll = next(iter(visible)) if len(visible) == 1 else None
    roster = list(db.scalars(select(Student).where(
        Student.class_code == document.workspace.class_code, Student.section == document.workspace.section,
    )))
    if job.match_method == "manual":
        if any(student.id == document.student_id for student in roster) and not another_submission(db, document, document.student_id):
            job.match_detail = ("Faculty matched this sheet despite a different detected roll."
                                if visible and visible != {normalize_roll(document.student_roll_number)}
                                else "Faculty confirmed this match.")
            return True
        job.match_detail = "The saved manual mapping conflicts with the current roster or another submission."
        return False
    if len(visible) > 1 or (visible and filename and not visible <= filename):
        detail = "Roll evidence conflicts. Confirm the student against the original."
    elif visible and page.source == "gemini_vision" and page.confidence_label != "high" and page.corrected_text is None:
        detail = "The first-page roll reading is uncertain. Confirm the student."
    else:
        candidate = visible or filename
        matches = [student for student in roster if normalize_roll(student.roll_number) in candidate]
        if len(candidate) == 1:
            job.detected_roll = next(iter(candidate))
        if len(candidate) == 1 and len(matches) == 1 and not another_submission(db, document, matches[0].id):
            document.student_id = matches[0].id
            job.match_method = "first_page" if visible else "filename"
            job.match_detail = "Exact roster match from the " + ("first page." if visible else "filename.")
            return True
        detail = ("No reliable roll number found. Confirm the student." if not candidate else
                  "The detected roll has no unique available match in this assessment roster.")
    document.student_id = None
    job.match_method = None
    job.match_detail = detail
    return False


def finish(db, job, state, stage, detail):
    job.state, job.stage, job.detail = state, stage, detail[:500]
    db.commit()


def process_job(db, document_id):
    document = db.get(UploadedDocument, document_id)
    job = document.job
    if not job or job.state != "running":
        return
    direct_visual = (document.document_type == "answer_sheet" and
                     os.getenv("EVALAI_EVALUATION_PROVIDER", "demo").strip().lower() == "gemini" and
                     can_use_external(document))
    # Gemini visual grading reads the original answer-sheet PDF pages directly.
    # Do not make it wait for, or fail on, the legacy transcription stage.
    if job.stage == "extraction" and not direct_visual:
        cached = {page.page_number: {field: getattr(page, field) for field in PAGE_FIELDS}
                  for page in document.pages if page.corrected_text is not None or (
                      page.source != "unavailable"
                      and not (page.source == "pdf_text" and re.fullmatch(r"\s*\d{1,3}\s*", page.raw_text))
                  )}
        job.pages_done = len(cached)
        db.commit()

        def progress(page, total):
            current = db.get(ProcessingJob, document_id)
            current.page_count = total
            current.pages_done += 1
            save_extracted_page(db, document_id, page)
            db.commit()

        document = extract_uploaded_document(document_id, db, progress=progress, cached_pages=cached,
                                             allow_external=can_use_external(document))
        job = document.job
    if document.document_type != "answer_sheet":
        successful = document.status == "extracted"
        if document.document_type == "question_paper" and not document.workspace.questions:
            return finish(db, job, "needs_attention", "extraction",
                          document.status_message + " Add questions manually if labels could not be read.")
        return finish(db, job, "complete" if successful else "needs_attention", "extraction", document.status_message)

    if not match_roll(db, document):
        stage = "extraction" if document.status in {"failed", "awaiting_provider", "partial"} else "matching"
        return finish(db, job, "needs_attention", stage, job.match_detail + " " + document.status_message)
    if document.status != "extracted" and not direct_visual and (
        not document.pages or any(p.source == "unavailable" and not p.faculty_verified for p in document.pages)
    ):
        return finish(db, job, "needs_attention", "extraction", document.status_message)
    job.stage = "references"
    db.commit()
    questions = document.workspace.questions
    if not questions or any(not q.model_answer.strip() and not q.rubric_criteria for q in questions):
        return finish(db, job, "needs_attention", "references", "Save a model answer or marking criteria for each question.")
    snapshot = [{"question_id": q.question_id} for q in questions]
    _, _, missing = prepare_answers(document, snapshot)
    if len(missing) == len(questions) and not direct_visual:
        return finish(db, job, "needs_attention", "matching", "No question answers were readable. Review and correct the extracted pages.")
    job.stage = "evaluation"
    job.detail = "Reading the original answer-sheet pages and preparing provisional marks." if direct_visual else "Preparing provisional marks."
    db.commit()
    try:
        evaluate_submission(document.assessment_code, document.student_roll_number, db, document_id=document_id)
    except HTTPException as exc:
        return finish(db, job, "needs_attention", "evaluation", str(exc.detail))
    finish(db, job, "complete", "evaluation", "Provisional marks are ready for faculty review.")


def recover_jobs(db):
    recoverable = (UploadedDocument.status.in_(["uploaded", "processing"]) |
                   (UploadedDocument.status == "partial"))
    for document in db.scalars(select(UploadedDocument).where(recoverable)):
        if not document.job:
            document.job = ProcessingJob(match_method="manual" if document.student_id else None)
    for job in db.scalars(select(ProcessingJob).where(ProcessingJob.state == "running")):
        job.state = "queued" if job.attempts < 2 else "needs_attention"
        job.detail = "Worker interrupted. " + ("Waiting to resume." if job.state == "queued" else "Select retry to resume.")
    db.commit()


def claim_job(db):
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.state == "queued").order_by(ProcessingJob.document_id))
    if job is None:
        db.rollback()
        return None
    document_id = job.document_id
    job.state = "running"
    job.attempts += 1
    job.detail = "Extracting pages." if job.stage == "extraction" else "Checking evaluation requirements."
    db.commit()
    return document_id


def run_worker():
    filename = engine.url.database
    if not filename or filename == ":memory:" or engine.dialect.name != "sqlite":
        raise SystemExit("The local worker requires a file-backed SQLite database.")
    # This lock has no data and is released by the OS when the worker exits.
    with sqlite3.connect(str(Path(filename).resolve()) + ".worker-lock", timeout=0) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise SystemExit("A worker is already running for this database.") from None
        Base.metadata.create_all(engine)
        # A previous supervisor can die while its child is still exiting.
        with sqlite3.connect(str(Path(filename).resolve()) + ".worker-child-lock", timeout=JOB_TIMEOUT + 5) as child_lock:
            child_lock.execute("BEGIN EXCLUSIVE")
            with SessionLocal() as db:
                recover_jobs(db)
        logger.info("Local processing worker started.")
        while True:
            with SessionLocal() as db:
                document_id = claim_job(db)
            if document_id is None:
                time.sleep(0.5)
                continue
            failure = run_document_process(document_id)
            if failure:
                logger.warning("Document %d: %s", document_id, failure)
                with SessionLocal() as db:
                    record_interruption(db, document_id, failure)


def run_document_process(document_id):
    """Enforce a hard deadline across file parsing and provider calls."""
    timeout_message = "Processing exceeded seven minutes. Retry to resume from stored pages."
    with subprocess.Popen([sys.executable, "-m", "app.worker", "--document", str(document_id)]) as child:
        try:
            code = child.wait(timeout=JOB_TIMEOUT)
            if code == 2:
                return timeout_message
            if code:
                return "Processing stopped unexpectedly. Retry to resume from stored pages."
            return None
        except subprocess.TimeoutExpired:
            return timeout_message
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def record_interruption(db, document_id, message):
    job = db.get(ProcessingJob, document_id)
    if job.state == "running":
        finish(db, job, "queued" if job.attempts < 2 else "needs_attention", job.stage, message)


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.document is None:
        run_worker()
        return
    deadline = Timer(JOB_TIMEOUT, lambda: os._exit(2))
    deadline.daemon = True
    deadline.start()
    try:
        with sqlite3.connect(str(Path(engine.url.database).resolve()) + ".worker-child-lock", timeout=0) as lock:
            lock.execute("BEGIN EXCLUSIVE")
            with SessionLocal() as db:
                process_job(db, args.document)
    except Exception as exc:
        logger.error("Document %d processing stopped (%s).", args.document, type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        deadline.cancel()


if __name__ == "__main__":
    main()
