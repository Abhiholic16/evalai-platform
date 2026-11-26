from fastapi.testclient import TestClient
import pymupdf
from io import BytesIO
from types import SimpleNamespace
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import LOGIN_FAILURES, app, extract_uploaded_document
from app.models import ProcessingJob, UploadedDocument
from app.worker import claim_job, process_job, recover_jobs
from app.document_processing import VisionProviderError, VisionResult
from app.evaluation import EvaluationProvider, EvaluationProviderError
from app.schemas import EvaluationLLMResponse, EvaluationQuestionResult
from app.seed import seed_assessment_workspaces, seed_demo_data

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def override_get_db():
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_test_data(monkeypatch, tmp_path):
    """Keep ordinary tests local even if a developer has provider credentials set."""
    monkeypatch.setenv("EVALAI_OCR_PROVIDER", "demo")
    monkeypatch.setenv("EVALAI_EVALUATION_PROVIDER", "demo")
    monkeypatch.setenv("EVALAI_EMAIL_PROVIDER", "simulation")
    monkeypatch.setenv("EVALAI_FACULTY_USERNAME", "faculty-test")
    monkeypatch.setenv("EVALAI_FACULTY_PASSWORD", "faculty-test-password-123")
    monkeypatch.setenv("EVALAI_SESSION_SECRET", "test-session-secret-please-use-32-bytes-minimum")
    monkeypatch.setenv("EVALAI_COOKIE_SECURE", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("EVALAI_ALLOW_EXTERNAL", raising=False)
    monkeypatch.setenv("EVALAI_UPLOAD_DIR", str(tmp_path / "uploads"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestingSession() as session:
        seed_demo_data(session)
        seed_assessment_workspaces(session)
    LOGIN_FAILURES.clear()
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    login = client.post("/api/auth/login", json={"username": "faculty-test", "password": "faculty-test-password-123"})
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]


def test_health_and_sample_records():
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "healthy", "project": "EvalAI"}
    response = client.get("/api/students")
    assert response.json()["count"] == 10
    assert response.json()["records"][0]["roll_number"] == "2401"


def test_faculty_authentication_csrf_and_protected_document_routes():
    client.cookies.clear()
    assert client.get("/api/students").status_code == 401
    assert client.post("/api/assessment-workspaces", json={"title": "blocked"}).status_code == 401
    bad_login = client.post("/api/auth/login", json={"username": "faculty-test", "password": "wrong-password"})
    assert bad_login.status_code == 401
    login = client.post("/api/auth/login", json={"username": "faculty-test", "password": "faculty-test-password-123"})
    csrf = login.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    assert client.get("/api/auth/session").json()["authenticated"] is True
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/documents/1/process").status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/api/documents/1/process").status_code == 404
    assert client.get("/docs").status_code == 404


def test_faculty_login_rate_limit_and_session_cookie_flags():
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "faculty-test", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "faculty-test", "password": "wrong-password"}).status_code == 429
    LOGIN_FAILURES.clear()
    response = client.post("/api/auth/login", json={"username": "faculty-test", "password": "faculty-test-password-123"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert client.get("/api/students").status_code == 200


def test_root_serves_existing_prototype():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "EvalAI · Faculty assessment workspace" in response.text
    assert '<script src="/frontend-integration.js"></script>' in response.text
    script = client.get("/frontend-integration.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "Loading student records" in script.text
    document_script = client.get("/document-workflow.js")
    assert document_script.status_code == 200
    assert "Faculty-corrected text" in document_script.text
    evaluation_script = client.get("/evaluation-workflow.js")
    assert evaluation_script.status_code == 200
    assert "Approve final marks" in evaluation_script.text
    feedback_script = client.get("/feedback-workflow.js")
    assert feedback_script.status_code == 200
    assert "Simulated · email was not sent" in feedback_script.text
    auth_script = client.get("/faculty-auth.js")
    assert auth_script.status_code == 200
    assert "X-CSRF-Token" in auth_script.text
    assert '<script src="/faculty-auth.js"></script>' in response.text


def test_frontend_deep_links_serve_app_shell():
    response = client.get("/assessment/demo")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_search_by_name_or_roll_and_class_section_filters():
    assert client.get("/api/students", params={"search": "ananya"}).json()["count"] == 1
    assert client.get("/api/students", params={"search": "2413"}).json()["records"][0]["name"] == "Rohan Das"
    response = client.get("/api/students", params={"class": "ENGG1", "section": "B"})
    assert [s["roll_number"] for s in response.json()["records"]] == ["2411", "2412", "2413"]


def test_subject_test_filters_and_student_detail():
    response = client.get("/api/students", params={"subject": "CHEM101", "test": "MID"})
    assert [s["roll_number"] for s in response.json()["records"]] == ["2411", "2413"]
    assert len(client.get("/api/students/2401").json()["assessments"]) == 2
    assert client.get("/api/students/9999").status_code == 404


def test_assessment_filters_and_validation():
    response = client.get("/api/assessments", params={"class": "ENGG2", "subject": "ECO201", "test": "SEM1"})
    assert response.json()["count"] == 2
    assert response.json()["records"][0]["student_roll_number"] in {"2421", "2422"}
    assert client.get("/api/assessments", params={"search": "Rohan Das"}).json()["count"] == 1
    assert client.get("/api/students", params={"limit": 0}).status_code == 422
    assert client.get("/api/students", params={"search": ""}).status_code == 422


def make_pdf(*page_texts):
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    output = document.tobytes()
    document.close()
    return output


def upload_answer_sheet(content=None, filename="2401_answer.pdf", media_type="application/pdf", roll="2401"):
    return client.post("/api/documents", data={
        "assessment_code": "bio-ut1", "document_type": "answer_sheet", "student_roll_number": roll,
    }, files={"file": (filename, content or make_pdf("Q1. Water cycle answer written by student."), media_type)})


def extract_for_api_test(document_id, *, allow_external=False):
    """Legacy review and grading tests exercise extraction without auto-grading."""
    response = client.post(f"/api/documents/{document_id}/process")
    assert response.status_code == 202
    with TestingSession() as db:
        extract_uploaded_document(document_id, db, allow_external=allow_external)
        db.get(ProcessingJob, document_id).state = "complete"
        db.commit()
    return client.get(f"/api/documents/{document_id}")


def drain_jobs():
    while True:
        with TestingSession() as db:
            document_id = claim_job(db)
            if document_id is None:
                return
            process_job(db, document_id)


def test_external_processing_policy_is_persisted_for_normal_uploads(monkeypatch):
    monkeypatch.setenv("EVALAI_ALLOW_EXTERNAL", "1")
    monkeypatch.setenv("EVALAI_OCR_PROVIDER", "gemini")
    upload = upload_answer_sheet(filename="normal-upload.pdf")
    assert upload.status_code == 201
    with TestingSession() as db:
        assert db.get(UploadedDocument, upload.json()["document"]["id"]).job.allow_external is True


def test_upload_pdf_process_text_duplicate_and_faculty_correction():
    content = make_pdf("Q1. Student wrote that plants use sunlight.", "Q2. A second page answer.")
    first = upload_answer_sheet(content)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    doc_id = first.json()["document"]["id"]
    duplicate = upload_answer_sheet(content)
    assert duplicate.status_code == 201
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["document"]["id"] == doc_id

    processed = extract_for_api_test(doc_id).json()
    assert processed["status"] == "extracted"
    assert len(processed["pages"]) == 2
    assert processed["pages"][0]["page_number"] == 1
    assert processed["pages"][0]["source"] == "pdf_text"
    assert "plants use sunlight" in processed["pages"][0]["raw_text"]
    assert processed["pages"][0]["needs_review"] is True

    corrected = client.put(f"/api/documents/{doc_id}/pages/1", json={"corrected_text": "Faculty correction."})
    assert corrected.status_code == 200
    assert corrected.json()["raw_text"] == processed["pages"][0]["raw_text"]
    assert corrected.json()["corrected_text"] == "Faculty correction."
    assert corrected.json()["faculty_verified"] is True


def test_scan_without_provider_never_claims_transcription():
    response = upload_answer_sheet(make_pdf("", ""))
    doc_id = response.json()["document"]["id"]
    processed = extract_for_api_test(doc_id).json()
    assert processed["status"] == "awaiting_provider"
    assert len(processed["pages"]) == 2
    assert processed["pages"][0]["raw_text"] == ""
    assert processed["pages"][0]["source"] == "blank"
    assert "transcription" in processed["status_message"]


def test_image_upload_and_invalid_file_and_class_match():
    image_buffer = BytesIO()
    Image.new("RGB", (40, 50), "white").save(image_buffer, format="PNG")
    response = client.post("/api/documents", data={"assessment_code": "bio-ut1", "document_type": "question_paper"},
        files={"file": ("questions.png", image_buffer.getvalue(), "image/png")})
    assert response.status_code == 201
    image_doc = extract_for_api_test(response.json()['document']['id']).json()
    assert image_doc["status"] == "awaiting_provider"
    assert image_doc["pages"][0]["page_number"] == 1
    assert image_doc["pages"][0]["raw_text"] == ""

    invalid = client.post("/api/documents", data={"assessment_code": "bio-ut1", "document_type": "question_paper"},
        files={"file": ("virus.pdf", b"not a PDF", "application/pdf")})
    assert invalid.status_code == 415
    wrong_student = upload_answer_sheet(roll="2421")
    assert wrong_student.status_code == 422


def test_provider_result_and_provider_failure_without_paid_call(monkeypatch):
    class FakeVision:
        def transcribe_page(self, _png):
            return VisionResult("Q1. Visible student handwriting.", "medium", [
                {"question": "Q1", "raw_text": "Visible student handwriting.", "confidence": "medium", "uncertain_spans": []}
            ], [])

    monkeypatch.setattr("app.main.configured_vision_provider", lambda: (FakeVision(), "gemini_vision", "fake provider"))
    png = BytesIO()
    Image.new("RGB", (40, 50), "white").save(png, format="PNG")
    upload = upload_answer_sheet(png.getvalue(), "synthetic.png", "image/png")
    page_result = extract_for_api_test(upload.json()["document"]["id"], allow_external=True).json()
    assert page_result["status"] == "extracted"
    assert page_result["pages"][0]["source"] == "gemini_vision"
    assert page_result["pages"][0]["confidence_label"] == "medium"
    assert page_result["pages"][0]["question_answers"][0]["question"] == "Q1"

    class BrokenVision:
        def transcribe_page(self, _png):
            raise VisionProviderError("Transcription failed: provider unavailable")

    monkeypatch.setattr("app.main.configured_vision_provider", lambda: (BrokenVision(), "gemini_vision", "fake provider"))
    png = BytesIO()
    Image.new("RGB", (41, 50), "white").save(png, format="PNG")
    upload = upload_answer_sheet(png.getvalue(), "synthetic-error.png", "image/png", roll="2402")
    failed = extract_for_api_test(upload.json()["document"]["id"], allow_external=True).json()
    assert failed["status"] == "failed"
    assert failed["pages"][0]["raw_text"] == ""
    assert "failed" in failed["status_message"].lower()


def test_upload_size_limit_and_assessment_context_creation():
    oversized = upload_answer_sheet(b"%PDF-" + b"x" * (15 * 1024 * 1024))
    assert oversized.status_code == 413
    created = client.post("/api/assessment-workspaces", json={
        "title": "Ecology quiz", "class_code": "ENGG2", "section": "A",
        "subject_code": "ECO201", "test_code": "UT1", "test_date": "2026-11-30",
        "rules": "Use clear units.", "rubric_criteria": [{"name": "Evidence", "points": 5}],
    })
    assert created.status_code == 200
    assert created.json()["assessment_code"].startswith("asm-")


def test_damaged_pdf_fails_clearly_and_original_file_is_available():
    response = upload_answer_sheet(b"%PDF-1.7\nnot a valid PDF structure")
    assert response.status_code == 201
    document_id = response.json()["document"]["id"]
    original = client.get(f"/api/documents/{document_id}/file")
    assert original.status_code == 200
    assert original.content.startswith(b"%PDF-")
    processed = extract_for_api_test(document_id).json()
    assert processed["status"] == "failed"
    assert "damaged" in processed["status_message"].lower()


def test_document_not_found_and_invalid_correction_are_reported():
    assert client.get("/api/documents/99999").status_code == 404
    assert client.post("/api/documents/99999/process").status_code == 404
    assert client.put("/api/documents/99999/pages/1", json={"corrected_text": "Correction"}).status_code == 404


def test_workspace_review_includes_ungraded_submissions_and_optional_answer_key():
    created = client.post("/api/assessment-workspaces", json={
        "assessment_code": "manual-stepwise", "title": "Manual question fallback",
        "class_code": "ENGG1", "section": "A", "subject_code": "BIO101", "test_code": "UT1",
        "marking_mode": "stepwise", "correctness_threshold": 0.65,
        "score_bands": [
            {"label": "Full", "minimum_correctness_percent": 65, "award_percent": 100},
            {"label": "Partial", "minimum_correctness_percent": 1, "award_percent": 50},
            {"label": "Zero", "minimum_correctness_percent": 0, "award_percent": 0},
        ],
        "questions": [{
            "question_id": "Q1", "position": 1, "prompt": "Explain photosynthesis.",
            "maximum_marks": 10, "model_answer": "", "rubric_criteria": ["Correct concept: 4 marks", "Reasoning: 6 marks"],
        }],
    })
    assert created.status_code == 200
    workspace = created.json()
    assert workspace["total_marks"] == 10
    assert workspace["marking_mode"] == "stepwise"
    assert workspace["correctness_threshold"] == 0.65
    assert workspace["questions"][0]["model_answer"] == ""

    uploaded = client.post("/api/documents", data={
        "assessment_code": "manual-stepwise", "document_type": "answer_sheet", "student_roll_number": "2401",
    }, files={"file": ("2401_answer.pdf", make_pdf("Q1. Plants use light to turn carbon dioxide and water into glucose."), "application/pdf")})
    assert uploaded.status_code == 201
    document_id = uploaded.json()["document"]["id"]
    extract_for_api_test(document_id)

    review = client.get("/api/assessment-workspaces/manual-stepwise/review")
    assert review.status_code == 200
    payload = review.json()
    assert payload["workspace"]["questions"][0]["prompt"] == "Explain photosynthesis."
    assert len(payload["submissions"]) == 1
    submission = payload["submissions"][0]
    assert submission["student"]["roll_number"] == "2401"
    assert submission["answer_sheets"][0]["id"] == document_id
    assert submission["answer_sheets"][0]["pages"][0]["raw_text"]

    evaluation = client.post("/api/assessments/manual-stepwise/students/2401/evaluations")
    assert evaluation.status_code == 200
    assert "model_answer_not_provided" in evaluation.json()["ai_proposed"][0]["uncertainty_flags"]


def test_question_paper_extraction_creates_editable_questions_without_rubric():
    created = client.post("/api/assessment-workspaces", json={
        "assessment_code": "paper-first", "title": "Paper-first assessment",
        "class_code": "ENGG1", "section": "A", "subject_code": "BIO101", "test_code": "UT1",
        "questions": [], "marking_mode": "holistic_percentage", "correctness_threshold": 0.7,
    })
    assert created.status_code == 200
    uploaded = client.post("/api/documents", data={
        "assessment_code": "paper-first", "document_type": "question_paper",
    }, files={"file": ("paper.pdf", make_pdf("1. Explain photosynthesis. [5 marks]\n40)\n2. Describe a food web. [5 marks]"), "application/pdf")})
    assert uploaded.status_code == 201
    document_id = uploaded.json()["document"]["id"]
    processed = extract_for_api_test(document_id)
    assert processed.status_code == 200
    assert processed.json()["status"] == "extracted"
    workspace = next(item for item in client.get("/api/assessment-workspaces").json() if item["assessment_code"] == "paper-first")
    assert [question["question_id"] for question in workspace["questions"]] == ["Q1", "Q2"]
    assert all(question["model_answer"] == "" and question["rubric_criteria"] == [] for question in workspace["questions"])
    assert workspace["total_marks"] == 20
    model_upload = client.post("/api/documents", data={
        "assessment_code": "paper-first", "document_type": "model_answer",
    }, files={"file": ("answers.pdf", make_pdf("Q01. Plants convert light energy into chemical energy.\n5 examples are available.\nQ02. Food webs show feeding relationships."), "application/pdf")})
    assert model_upload.status_code == 201
    assert extract_for_api_test(model_upload.json()['document']['id']).status_code == 200
    refreshed = next(item for item in client.get("/api/assessment-workspaces").json() if item["assessment_code"] == "paper-first")
    assert "chemical energy" in refreshed["questions"][0]["model_answer"]
    assert "5 examples are available." in refreshed["questions"][0]["model_answer"]
    assert "feeding relationships" in refreshed["questions"][1]["model_answer"]


def question_payload(question_id, position, prompt, answer, maximum, criteria):
    return {
        "question_id": question_id, "position": position, "prompt": prompt,
        "model_answer": answer, "maximum_marks": maximum, "rubric_criteria": criteria,
    }


def make_all_biology_answers(name_and_roll=""):
    return make_pdf(
        f"Q1. {name_and_roll} Plants use light energy and carbon dioxide to produce glucose and oxygen.",
        "Q2. Decomposers break down dead matter and return nutrients to the environment.",
        "Q3. Much energy is used by organisms and released as heat, leaving less for the next trophic level.",
    )


def evaluation_result(question, marks=None, maximum=None):
    maximum = maximum or question["maximum_marks"]
    return EvaluationQuestionResult(
        question_id=question["question_id"], maximum_marks=maximum, proposed_marks=marks if marks is not None else maximum - 1,
        reasoning="The answer addresses the supplied criterion with visible supporting content.",
        strengths=["Relevant concept identified"], improvement_areas=["Add a precise supporting example"],
        uncertainty_flags=["faculty_review_required"], requires_faculty_review=True,
    )


def create_approved_evaluation(monkeypatch):
    class FakeEvaluator:
        def evaluate(self, question_inputs, *_args):
            return EvaluationLLMResponse(question_results=[evaluation_result(question) for question in question_inputs])

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "test-model", FakeEvaluator()))
    upload = upload_answer_sheet(make_all_biology_answers())
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)
    evaluation = client.post("/api/assessments/bio-ut1/students/2401/evaluations").json()
    return evaluation


def approve_evaluation_for_feedback(monkeypatch):
    evaluation = create_approved_evaluation(monkeypatch)
    approved = client.post(f"/api/evaluations/{evaluation['id']}/approve")
    assert approved.status_code == 200
    return approved.json()


def test_assessment_questions_validate_mark_total_and_rubric():
    base = {"title": "Rubric test", "class_code": "ENGG1", "section": "A", "subject_code": "BIO101", "test_code": "UT1"}
    questions = [
        question_payload("Q1", 1, "Explain photosynthesis.", "Plants convert light into chemical energy.", 8, ["Explains light use"]),
        question_payload("Q2", 2, "Describe nutrient recycling.", "Decomposers return nutrients.", 12, ["Explains decomposition"]),
    ]
    valid = client.post("/api/assessment-workspaces", json={**base, "questions": questions, "total_marks": 20})
    assert valid.status_code == 200
    assert valid.json()["total_marks"] == 20
    assert [question["question_id"] for question in valid.json()["questions"]] == ["Q1", "Q2"]
    listed = client.get("/api/assessment-workspaces")
    listed_workspace = next(item for item in listed.json() if item["assessment_code"] == valid.json()["assessment_code"])
    assert listed_workspace["total_marks"] == 20
    mismatch = client.post("/api/assessment-workspaces", json={**base, "title": "Mismatch", "questions": questions, "total_marks": 19})
    assert mismatch.status_code == 422
    no_criteria = [question_payload("Q1", 1, "Explain photosynthesis.", "Plants convert light.", 8, [])]
    no_rubric = client.post("/api/assessment-workspaces", json={**base, "title": "Optional rubric", "questions": no_criteria})
    assert no_rubric.status_code == 200
    assert no_rubric.json()["questions"][0]["rubric_criteria"] == []


def test_demo_evaluation_is_labelled_incomplete_answers_stay_unscored_and_marks_stay_unofficial():
    uploaded = upload_answer_sheet(make_pdf("Q1. Plants use sunlight to make sugars."))
    document_id = uploaded.json()["document"]["id"]
    assert extract_for_api_test(document_id).json()["status"] == "extracted"
    response = client.post("/api/assessments/bio-ut1/students/2401/evaluations")
    assert response.status_code == 200
    evaluation = response.json()
    assert evaluation["status"] == "provisional"
    assert evaluation["provider_mode"] == "demo_simulation"
    assert "fixed placeholder" in evaluation["status_message"].lower()
    assert evaluation["ai_proposed_total"] == 5
    assert evaluation["ai_proposed"][0]["proposed_marks"] == 5
    assert all(item["proposed_marks"] is None for item in evaluation["ai_proposed"][1:])
    assert client.get("/api/students/2401").json()["assessments"][0]["score"] == 14


def test_missing_or_unreadable_answers_are_not_sent_to_provider(monkeypatch):
    upload = upload_answer_sheet(make_pdf("", ""))
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)
    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: pytest.fail("provider must not be called for blank answers"))
    evaluation = client.post("/api/assessments/bio-ut1/students/2401/evaluations").json()
    assert evaluation["status"] == "needs_manual_review"
    assert evaluation["provider_mode"] == "not_evaluated"
    assert evaluation["ai_proposed_total"] == 0
    assert all(item["proposed_marks"] is None for item in evaluation["ai_proposed"])


def test_mocked_gemini_evaluation_is_question_wise_and_redacts_student_identifiers(monkeypatch):
    captured = {}

    class FakeEvaluator:
        def evaluate(self, question_inputs, general_rubric, rules):
            captured["inputs"] = question_inputs
            captured["general_rubric"] = general_rubric
            captured["rules"] = rules
            return EvaluationLLMResponse(question_results=[
                evaluation_result(question) for question in question_inputs
            ])

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "test-model", FakeEvaluator()))
    upload = upload_answer_sheet(make_all_biology_answers("Ananya Rao 2401"))
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)
    response = client.post("/api/assessments/bio-ut1/students/2401/evaluations")
    assert response.status_code == 200
    evaluation = response.json()
    assert evaluation["provider_mode"] == "gemini"
    assert evaluation["model_name"] == "test-model"
    assert len(evaluation["ai_proposed"]) == 3
    assert evaluation["ai_proposed_total"] == 17
    assert evaluation["final_approved"] is None
    provider_text = " ".join(question["student_answer"] for question in captured["inputs"])
    assert "Ananya Rao" not in provider_text and "2401" not in provider_text
    assert "[student identifier]" in provider_text
    assert all(source["document_id"] == document_id and source["page_number"] in {1, 2, 3}
               for source in evaluation["answer_sources"])


def test_provider_error_and_invalid_structured_marks_fail_safely(monkeypatch):
    upload = upload_answer_sheet(make_all_biology_answers())
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)

    class BrokenEvaluator:
        def evaluate(self, *_args):
            raise EvaluationProviderError("The evaluation provider timed out or could not be reached. Retry later.")

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "fake", BrokenEvaluator()))
    failed = client.post("/api/assessments/bio-ut1/students/2401/evaluations")
    assert failed.status_code == 502
    assert "timed out" in failed.json()["detail"]

    def wrong_marks():
        class InvalidEvaluator:
            def evaluate(self, question_inputs, *_args):
                results = [evaluation_result(question) for question in question_inputs]
                first = results[0].model_dump()
                first["proposed_marks"] = question_inputs[0]["maximum_marks"] + 1
                results[0] = SimpleNamespace(**first)
                return SimpleNamespace(question_results=results)
        return InvalidEvaluator()

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "fake", wrong_marks()))
    invalid = client.post("/api/assessments/bio-ut1/students/2401/evaluations")
    assert invalid.status_code == 502
    assert "maximum" in invalid.json()["detail"].lower()


def test_faculty_edits_preserve_ai_proposal_and_approval_creates_official_mark(monkeypatch):
    class FakeEvaluator:
        def evaluate(self, question_inputs, *_args):
            return EvaluationLLMResponse(question_results=[evaluation_result(question) for question in question_inputs])

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "test-model", FakeEvaluator()))
    upload = upload_answer_sheet(make_all_biology_answers())
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)
    created = client.post("/api/assessments/bio-ut1/students/2401/evaluations").json()
    ai_original = created["ai_proposed"]
    edits = [dict(result, proposed_marks=result["proposed_marks"] - 1,
                  reasoning="Faculty reviewed the original and made a correction.") for result in ai_original]
    edited_response = client.put(f"/api/evaluations/{created['id']}/edits", json={"question_results": edits})
    assert edited_response.status_code == 200
    edited = edited_response.json()
    assert edited["status"] == "edited"
    assert edited["ai_proposed"] == ai_original
    assert edited["faculty_edited_total"] == 14

    approved = client.post(f"/api/evaluations/{created['id']}/approve")
    assert approved.status_code == 200
    record = approved.json()
    assert record["status"] == "approved"
    assert record["ai_proposed"] == ai_original
    assert record["final_approved_total"] == 14
    assert client.get(f"/api/evaluations/{created['id']}").json()["final_approved"] == edits
    official = client.get("/api/students/2401").json()["assessments"]
    result_row = next(row for row in official if row["assessment_code"] == "bio-ut1")
    assert result_row["score"] == 14
    assert result_row["review_status"] == "Faculty approved Gemini evaluation"


def test_rejected_evaluation_is_not_recorded_as_an_official_mark():
    workspace = client.post("/api/assessment-workspaces", json={
        "title": "Rejected practice", "class_code": "ENGG1", "section": "A",
        "subject_code": "BIO101", "test_code": "UT1", "questions": [
            question_payload("Q1", 1, "Explain photosynthesis.", "Plants use light to make glucose.", 10, ["Explains energy conversion"])
        ], "total_marks": 10,
    }).json()
    upload = client.post("/api/documents", data={
        "assessment_code": workspace["assessment_code"], "document_type": "answer_sheet", "student_roll_number": "2401",
    }, files={"file": ("2401_answer.pdf", make_pdf("Q1. Plants use light to make glucose."), "application/pdf")})
    document_id = upload.json()["document"]["id"]
    extract_for_api_test(document_id)
    evaluation = client.post(f"/api/assessments/{workspace['assessment_code']}/students/2401/evaluations").json()
    rejected = client.post(f"/api/evaluations/{evaluation['id']}/reject", json={"reason": "Rubric setup needs correction."})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["final_approved"] is None
    assert all(row["assessment_code"] != workspace["assessment_code"] for row in client.get("/api/students/2401").json()["assessments"])


def test_feedback_requires_approved_marks_and_persists_faculty_edits(monkeypatch):
    evaluation = create_approved_evaluation(monkeypatch)
    assert client.post(f"/api/evaluations/{evaluation['id']}/feedback").status_code == 409
    assert client.post(f"/api/evaluations/{evaluation['id']}/approve").status_code == 200
    created = client.post(f"/api/evaluations/{evaluation['id']}/feedback")
    assert created.status_code == 200
    feedback = created.json()
    assert feedback["status"] == "draft"
    assert feedback["content"]["source"] == "faculty_approved_evaluation"
    assert feedback["content"]["score"] == 17
    assert feedback["content"]["questions"][0]["strengths"] == ["Relevant concept identified"]
    assert feedback["content"]["questions"][0]["missing_concepts"] == ["Add a precise supporting example"]
    edited_content = feedback["content"]
    edited_content["overall_summary"] = "Faculty-edited summary for this student."
    edited = client.put(f"/api/feedback/{feedback['id']}", json={"content": edited_content})
    assert edited.status_code == 200
    assert edited.json()["content"]["overall_summary"] == "Faculty-edited summary for this student."
    tampered = edited.json()["content"]
    tampered["score"] += 1
    assert client.put(f"/api/feedback/{feedback['id']}", json={"content": tampered}).status_code == 422
    approved = client.post(f"/api/feedback/{feedback['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert client.get(f"/api/feedback/{feedback['id']}").json()["content"]["overall_summary"] == "Faculty-edited summary for this student."
    assert client.get("/api/feedback", params={"student_roll_number": "2401"}).json()[0]["id"] == feedback["id"]


def test_feedback_pdf_requires_approval_and_downloads_report(monkeypatch):
    approved = approve_evaluation_for_feedback(monkeypatch)
    feedback = client.post(f"/api/evaluations/{approved['id']}/feedback").json()
    assert client.get(f"/api/feedback/{feedback['id']}/pdf").status_code == 409
    client.post(f"/api/feedback/{feedback['id']}/approve")
    response = client.get(f"/api/feedback/{feedback['id']}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    pdf = pymupdf.open(stream=response.content, filetype="pdf")
    content = "\n".join(page.get_text() for page in pdf)
    assert "Faculty Feedback Report" in content
    assert "Approved score: 17 / 20" in content
    assert "Confidential student feedback" in content


def test_simulated_email_is_never_reported_as_sent_and_attempts_persist(monkeypatch):
    approved = approve_evaluation_for_feedback(monkeypatch)
    feedback = client.post(f"/api/evaluations/{approved['id']}/feedback").json()
    assert client.post(f"/api/feedback/{feedback['id']}/deliver").status_code == 409
    client.post(f"/api/feedback/{feedback['id']}/approve")
    first = client.post(f"/api/feedback/{feedback['id']}/deliver")
    assert first.status_code == 200
    assert first.json()["delivery_status"] == "simulated"
    assert "No email was sent" in first.json()["delivery_message"]
    second = client.post(f"/api/feedback/{feedback['id']}/deliver")
    assert second.status_code == 200
    assert second.json()["delivery_status"] == "simulated"
    assert second.json()["delivery_attempts"] == 2
    history = client.get(f"/api/feedback/{feedback['id']}/deliveries").json()
    assert len(history) == 2
    assert all(item["status"] == "simulated" and item["provider_mode"] == "simulation" for item in history)


def test_email_retry_after_failure_and_duplicate_success_block(monkeypatch):
    approved = approve_evaluation_for_feedback(monkeypatch)
    feedback = client.post(f"/api/evaluations/{approved['id']}/feedback").json()
    client.post(f"/api/feedback/{feedback['id']}/approve")

    class FakeEmailProvider:
        mode = "smtp"
        def __init__(self, should_fail): self.should_fail = should_fail
        def send(self, *_args):
            if self.should_fail: raise RuntimeError("mock SMTP failure")
            return "Mock SMTP accepted message."

    providers = iter([FakeEmailProvider(True), FakeEmailProvider(False)])
    monkeypatch.setattr("app.main.configured_email_provider", lambda: next(providers))
    failed = client.post(f"/api/feedback/{feedback['id']}/deliver")
    assert failed.status_code == 200
    assert failed.json()["delivery_status"] == "failed"
    retried = client.post(f"/api/feedback/{feedback['id']}/deliver")
    assert retried.status_code == 200
    assert retried.json()["delivery_status"] == "sent"
    assert retried.json()["delivery_attempts"] == 2
    duplicate = client.post(f"/api/feedback/{feedback['id']}/deliver")
    assert duplicate.status_code == 409
    history = client.get(f"/api/feedback/{feedback['id']}/deliveries").json()
    assert [item["status"] for item in history] == ["failed", "sent"]
    assert client.get(f"/api/feedback/{feedback['id']}").json()["delivery_status"] == "sent"


def test_full_assessment_to_simulated_feedback_delivery_workflow(monkeypatch):
    class FakeEvaluator:
        def evaluate(self, question_inputs, *_args):
            return EvaluationLLMResponse(question_results=[evaluation_result(question) for question in question_inputs])

    monkeypatch.setattr("app.main.configured_evaluation_provider", lambda: EvaluationProvider("gemini", "mock-model", FakeEvaluator()))
    setup = client.post("/api/assessment-workspaces", json={
        "title": "End-to-end workflow", "class_code": "ENGG1", "section": "A",
        "subject_code": "BIO101", "test_code": "UT1", "total_marks": 10,
        "questions": [question_payload("Q1", 1, "Explain photosynthesis.", "Plants use light to make sugars.", 10, ["Explains use of light", "Mentions glucose"])],
    })
    assert setup.status_code == 200
    code = setup.json()["assessment_code"]
    upload = client.post("/api/documents", data={
        "assessment_code": code, "document_type": "answer_sheet", "student_roll_number": "2401",
    }, files={"file": ("answer.pdf", make_pdf("Q1. Plants use light energy to make glucose."), "application/pdf")})
    assert upload.status_code == 201
    document_id = upload.json()["document"]["id"]
    extracted = extract_for_api_test(document_id)
    assert extracted.status_code == 200 and extracted.json()["pages"][0]["source"] == "pdf_text"
    proposed = client.post(f"/api/assessments/{code}/students/2401/evaluations")
    assert proposed.status_code == 200 and proposed.json()["status"] == "provisional"
    official = client.post(f"/api/evaluations/{proposed.json()['id']}/approve")
    assert official.status_code == 200 and official.json()["status"] == "approved"
    draft = client.post(f"/api/evaluations/{proposed.json()['id']}/feedback")
    assert draft.status_code == 200 and draft.json()["status"] == "draft"
    feedback = client.post(f"/api/feedback/{draft.json()['id']}/approve")
    assert feedback.status_code == 200 and feedback.json()["status"] == "approved"
    report = client.get(f"/api/feedback/{feedback.json()['id']}/pdf")
    assert report.status_code == 200 and report.content.startswith(b"%PDF-")
    delivery = client.post(f"/api/feedback/{feedback.json()['id']}/deliver")
    assert delivery.status_code == 200 and delivery.json()["delivery_status"] == "simulated"
    assert "No email was sent" in delivery.json()["delivery_message"]


def test_durable_auto_matching_and_exception_confirmation(monkeypatch):
    from app.models import Student

    with TestingSession() as db:
        for roll in ("269021001", "269021002", "269021003"):
            db.add(Student(roll_number=roll, name="Fictional Student " + roll[-1], email=roll + "@example.test",
                           class_code="ENGG1", section="A", program="Engineering", year="Year 1",
                           academic_year="2026-27", admission_year="2026"))
        db.commit()
    monkeypatch.setattr("app.main.configured_vision_provider", lambda: pytest.fail("native local text must stay local"))
    files = [
        upload_answer_sheet(make_pdf("Roll No: 269 021 001\nQ1. Plants use light to make glucose.", "2"), "sheet.pdf", roll=""),
        upload_answer_sheet(make_pdf("Roll No: 251081011\nQ1. A fictional mismatch."), "269021002.pdf", roll=""),
        upload_answer_sheet(make_pdf("Roll No: 269021003\nQ1. Second student answer."), "269021001.pdf", roll=""),
    ]
    assert all(result.status_code == 201 and result.json()["document"]["job"]["state"] == "queued" for result in files)
    ids = [result.json()["document"]["id"] for result in files]
    assert client.get(f"/api/documents/{ids[0]}").json()["pages"] == []
    # A fresh session claims and runs the job independently of the upload request.
    with TestingSession() as db:
        assert claim_job(db) == ids[0]
    with TestingSession() as db:
        recover_jobs(db)
    drain_jobs()
    result = client.get("/api/assessment-workspaces/bio-ut1/review").json()
    matched = result["submissions"][0]["answer_sheets"][0]
    assert matched["job"]["detected_roll"] == "269021001"
    assert matched["job"]["match_method"] == "first_page"
    assert matched["job"]["attempts"] == 2
    assert matched["job"]["pages_done"] == 2
    assert matched["pages"][1]["source"] == "blank"
    assert result["submissions"][0]["evaluation"]["status"] == "provisional"
    assert {doc["id"] for doc in result["unmatched_documents"]} == set(ids[1:])
    assert all(doc["job"]["state"] == "needs_attention" for doc in result["unmatched_documents"])
    assert client.put(f"/api/documents/{ids[1]}/match", json={"student_roll_number": "269021002"}).status_code == 200
    drain_jobs()
    manually_matched = client.get(f"/api/documents/{ids[1]}").json()
    assert manually_matched["job"]["match_method"] == "manual"
    assert manually_matched["job"]["detected_roll"] == "251081011"
    assert client.post(f"/api/documents/{ids[0]}/process?retry=true").status_code == 202
    drain_jobs()
    evaluations = client.get("/api/evaluations?assessment_code=bio-ut1").json()
    assert len([item for item in evaluations if item["document_id"] == ids[0]]) == 1


def test_references_wake_waiting_submissions_and_associate_in_either_order():
    workspace = client.post("/api/assessment-workspaces", json={
        "assessment_code": "async-references", "title": "Synthetic assessment",
        "class_code": "ENGG1", "section": "A", "subject_code": "BIO101", "test_code": "UT1",
    }).json()

    def upload(kind, text, filename):
        response = client.post("/api/documents", data={"assessment_code": workspace["assessment_code"], "document_type": kind},
                               files={"file": (filename, make_pdf(text), "application/pdf")})
        assert response.status_code == 201
        return response.json()["document"]["id"]

    answer = upload("answer_sheet", "Roll Number: 2401\nQ1. Plants turn light into chemical energy.", "sheet.pdf")
    drain_jobs()
    assert client.get(f"/api/documents/{answer}").json()["job"]["stage"] == "references"
    upload("model_answer", "Q1. Plants use light energy to make glucose.", "model.pdf")
    upload("question_paper", "1. What do plants do with light?", "paper.pdf")
    drain_jobs()
    review = client.get("/api/assessment-workspaces/async-references/review").json()
    assert review["workspace"]["questions"][0]["model_answer"] == "Plants use light energy to make glucose."
    assert review["submissions"][0]["evaluation"]["status"] == "provisional"


def test_worker_deadline_retries_once_and_keeps_stored_pages(monkeypatch):
    import subprocess
    from app import worker
    from app.main import save_extracted_page

    upload = upload_answer_sheet(make_pdf("Q1. Saved partial work."), roll="")
    document_id = upload.json()["document"]["id"]
    calls = []

    class StalledChild:
        stopped = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def wait(self, timeout=None):
            if not self.stopped:
                assert timeout == worker.JOB_TIMEOUT
                raise subprocess.TimeoutExpired("worker", timeout)
            return 1

        def poll(self):
            return 1 if self.stopped else None

        def kill(self):
            self.stopped = True
            calls.append("stopped")

    monkeypatch.setattr(worker.subprocess, "Popen", lambda _: StalledChild())
    with TestingSession() as db:
        assert claim_job(db) == document_id
        save_extracted_page(db, document_id, {
            "page_number": 1, "raw_text": "Q1. Saved partial work.", "question_answers": [],
            "source": "pdf_text", "confidence_source": "embedded_pdf_text",
        })
        db.commit()
        worker.record_interruption(db, document_id, worker.run_document_process(document_id))
        assert db.get(ProcessingJob, document_id).state == "queued"
        assert claim_job(db) == document_id
        worker.record_interruption(db, document_id, worker.run_document_process(document_id))
    stored = client.get(f"/api/documents/{document_id}").json()
    assert calls == ["stopped", "stopped"]
    assert stored["job"]["state"] == "needs_attention"
    assert "seven minutes" in stored["job"]["detail"]
    assert stored["pages"][0]["raw_text"] == "Q1. Saved partial work."
    assert client.post(f"/api/documents/{document_id}/process?retry=true").status_code == 202
    drain_jobs()
    assert client.get(f"/api/documents/{document_id}").json()["job"]["state"] == "complete"
