"""Browser regression with an isolated real API, SQLite DB, and worker."""
from contextlib import ExitStack
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time

import httpx
import pytest

playwright = pytest.importorskip("playwright.sync_api")

BACKEND = Path(__file__).resolve().parents[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)


def test_batch_survives_closing_browser_page(tmp_path):
    fixtures = tmp_path / "fixtures"
    runpy.run_path(str(BACKEND / "tests" / "fixtures" / "pipeline_pdfs.py"))["generate_pipeline_pdfs"](fixtures)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "EVALAI_DATABASE_URL": f"sqlite:///{(tmp_path / 'browser.sqlite3').as_posix()}",
        "EVALAI_UPLOAD_DIR": str(tmp_path / "uploads"),
        "EVALAI_OCR_PROVIDER": "demo", "EVALAI_EVALUATION_PROVIDER": "demo", "EVALAI_ALLOW_EXTERNAL": "0",
        "EVALAI_FACULTY_USERNAME": "fictional-faculty", "EVALAI_FACULTY_PASSWORD": "fictional-browser-test-123",
        "EVALAI_SESSION_SECRET": "fictional-secret-for-browser-tests-only",
        "EVALAI_COOKIE_SECURE": "false",
    }
    env.pop("GEMINI_API_KEY", None)
    with playwright.sync_playwright() as runtime, ExitStack() as cleanup:
        try:
            browser = runtime.chromium.launch(channel="msedge" if os.name == "nt" else None, headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" not in str(exc) and "not found" not in str(exc):
                raise
            pytest.skip("Playwright's browser is not installed")
        cleanup.callback(browser.close)

        def start(*module_args):
            log = cleanup.enter_context((tmp_path / f"{module_args[0].replace('.', '-')}.log").open("ab"))
            process = subprocess.Popen([sys.executable, "-m", *module_args], cwd=BACKEND, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            cleanup.callback(stop, process)
            return process

        server = start("uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port))
        with httpx.Client(trust_env=False) as http:
            for _ in range(100):
                assert server.poll() is None
                try:
                    if http.get(origin + "/api/health").is_success:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Temporary backend failed to start")
        subprocess.run([sys.executable, "-c", """
from app.database import SessionLocal
from app.models import Student
with SessionLocal() as db:
    for i in range(1, 5):
                db.add(Student(roll_number=["251081011", "251081068", "251081046", "251081001"][i - 1], name=f"Fictional Student {i}", email=f"student{i}@example.test",
                       class_code="ENGG2", section="A", program="Engineering", year="Year 2",
                       academic_year="2026-27", admission_year="2025"))
    db.commit()
"""], cwd=BACKEND, env=env, check=True)

        context = browser.new_context()
        # The verification browser uses only the localhost application.
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        page.locator("#facultyUsername").fill(env["EVALAI_FACULTY_USERNAME"])
        page.locator("#facultyPassword").fill(env["EVALAI_FACULTY_PASSWORD"])
        page.get_by_role("button", name="Sign in").click()
        page.locator(".app").wait_for(state="visible")
        page.locator(".nav [data-view='setup']").click()
        page.locator("#testName").fill("Fictional pipeline browser test")
        page.locator("#setupClass").select_option("ENGG2")
        page.locator("#setupSection").select_option("A")
        page.locator("#paperFile").set_input_files(fixtures / "question-paper.pdf")
        page.locator("#modelFile").set_input_files(fixtures / "model-answer.pdf")
        page.locator("#saveSetup").click()
        playwright.expect(page.locator("#setupContext")).not_to_contain_text("undefined")
        page.locator("#answerPdfs").set_input_files([fixtures / f"answer-{i}.pdf" for i in range(1, 5)])
        page.locator("#previewMatches").click()
        playwright.expect(page.locator("#openNewQueue")).to_be_enabled()
        page.locator("#openNewQueue").click()
        playwright.expect(page.locator("#queueRows")).to_contain_text("answer-1.pdf")
        playwright.expect(page.locator("#queueRows")).not_to_contain_text("Retry processing")
        code = page.evaluate("activeAssessment.id")
        page.close()

        worker = start("app.worker")
        for _ in range(140):
            assert worker.poll() is None
            cookies = {item["name"]: item["value"] for item in context.cookies()}
            with httpx.Client(trust_env=False, cookies=cookies) as http:
                response = http.get(origin + f"/api/assessment-workspaces/{code}/review")
                assert response.status_code == 200
                submissions = response.json()["submissions"]
                if len(submissions) == 4 and all(item["evaluation"] for item in submissions):
                    break
            time.sleep(0.15)
        else:
            pytest.fail(f"Worker did not finish all four synthetic submissions. See {tmp_path}.")
        assert all(item["evaluation"]["status"] == "provisional" for item in submissions)
        assert all(item["answer_sheets"][0]["job"]["match_method"] == "first_page" for item in submissions)

        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        row = page.locator("#assessmentRows tr").filter(has_text="Fictional pipeline browser test")
        playwright.expect(row).to_contain_text("Ready for faculty review")
        row.get_by_role("button", name="Open test review").click()
        playwright.expect(page.locator("#queueRows")).to_contain_text("251081001")
        playwright.expect(page.get_by_role("button", name="Review provisional recommendation")).to_have_count(4)
        page.get_by_role("button", name="Review provisional recommendation").first.click()
        dialog = page.locator("#evaluationDialog")
        playwright.expect(dialog).to_be_visible()
        playwright.expect(dialog).to_contain_text("Simulated marks")
        playwright.expect(dialog).to_contain_text("Faculty approval is required")
        assert page.evaluate("async code => (await (await fetch('/api/assessments?limit=100')).json()).records.filter(r => r.assessment_code === code).length", code) == 0
        dialog.get_by_role("button", name="Approve final marks").click()
        playwright.expect(dialog).to_contain_text("Faculty-approved final marks")
        assert page.evaluate("async code => (await (await fetch('/api/assessments?limit=100')).json()).records.filter(r => r.assessment_code === code).length", code) == 1
        assert not errors
