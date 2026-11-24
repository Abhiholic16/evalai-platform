from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.seed import seed_demo_data

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)
with TestingSession() as session:
    seed_demo_data(session)


def override_get_db():
    with TestingSession() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def test_health_and_sample_records():
    assert client.get("/api/health").json() == {"status": "healthy", "project": "EvalAI"}
    response = client.get("/api/students")
    assert response.json()["count"] == 10
    assert response.json()["records"][0]["roll_number"] == "2401"


def test_root_serves_existing_prototype():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "EvalAI · Faculty assessment workspace" in response.text
    assert '<script src="/frontend-integration.js"></script>' in response.text
    script = client.get("/frontend-integration.js")
    assert script.status_code == 200
    assert "Loading student records" in script.text


def test_search_by_name_or_roll_and_class_section_filters():
    assert client.get("/api/students", params={"search": "ananya"}).json()["count"] == 1
    assert client.get("/api/students", params={"search": "2413"}).json()["records"][0]["name"] == "Rohan Das"
    response = client.get("/api/students", params={"class": "BSC1", "section": "B"})
    assert [s["roll_number"] for s in response.json()["records"]] == ["2411", "2412", "2413"]


def test_subject_test_filters_and_student_detail():
    response = client.get("/api/students", params={"subject": "CHEM101", "test": "MID"})
    assert [s["roll_number"] for s in response.json()["records"]] == ["2411", "2413"]
    assert len(client.get("/api/students/2401").json()["assessments"]) == 2
    assert client.get("/api/students/9999").status_code == 404


def test_assessment_filters_and_validation():
    response = client.get("/api/assessments", params={"class": "BSC2", "subject": "ECO201", "test": "SEM1"})
    assert response.json()["count"] == 2
    assert response.json()["records"][0]["student_roll_number"] in {"2421", "2422"}
    assert client.get("/api/assessments", params={"search": "Rohan Das"}).json()["count"] == 1
    assert client.get("/api/students", params={"limit": 0}).status_code == 422
    assert client.get("/api/students", params={"search": ""}).status_code == 422
