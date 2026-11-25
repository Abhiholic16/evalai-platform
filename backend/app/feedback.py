"""Approved-result feedback generation, PDF rendering, and email providers."""
from email.message import EmailMessage
import os
import smtplib
from typing import Protocol

import pymupdf


def generate_feedback(evaluation) -> dict:
    """Create a transparent, personalized summary from faculty-approved question results."""
    if evaluation.status != "approved" or not evaluation.final_approved:
        raise ValueError("Feedback requires a faculty-approved evaluation.")
    questions = []
    strengths = []
    improvements = []
    for question in evaluation.question_snapshot:
        result = next((item for item in evaluation.final_approved if item["question_id"] == question["question_id"]), None)
        if result is None or result.get("proposed_marks") is None:
            raise ValueError("Feedback cannot be generated while an approved question result is missing.")
        q_strengths = list(result.get("strengths") or [])
        q_improvements = list(result.get("improvement_areas") or [])
        strengths.extend(q_strengths)
        improvements.extend(q_improvements)
        questions.append({
            "question_id": question["question_id"],
            "prompt": question["prompt"],
            "marks": result["proposed_marks"],
            "maximum_marks": result["maximum_marks"],
            "strengths": q_strengths,
            "missing_concepts": q_improvements,
            "suggestions": q_improvements or ["Continue applying the concepts shown in this answer."],
        })
    total = sum(item["marks"] for item in questions)
    maximum = sum(item["maximum_marks"] for item in questions)
    percent = round(total / maximum * 100) if maximum else 0
    if percent >= 80:
        summary = "Strong result: the approved answers show good command of the assessed material."
    elif percent >= 60:
        summary = "Developing well: review the concepts identified below to strengthen understanding."
    else:
        summary = "Use this result as a guide: revisit the listed concepts and discuss questions with your instructor."
    return {
        "assessment_code": evaluation.assessment_code,
        "score": total,
        "maximum_marks": maximum,
        "percentage": percent,
        "overall_summary": summary,
        "overall_strengths": list(dict.fromkeys(strengths)),
        "improvement_areas": list(dict.fromkeys(improvements)),
        "questions": questions,
        "source": "faculty_approved_evaluation",
        "demo_result": evaluation.provider_mode == "demo_simulation",
    }


def render_feedback_pdf(student_name: str, roll_number: str, content: dict, fictional: bool = True) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    lines = [
        "ABC College | Faculty Feedback Report",
        "FICTIONAL DEMO STUDENT RECORD" if fictional else "Confidential student feedback",
        f"Student: {student_name} ({roll_number})",
        f"Assessment: {content['assessment_code']}",
        f"Approved score: {content['score']} / {content['maximum_marks']} ({content['percentage']}%)",
        "", "Overall performance", content["overall_summary"], "",
        "Overall strengths: " + ("; ".join(content["overall_strengths"]) or "No strengths recorded."),
        "Areas to improve: " + ("; ".join(content["improvement_areas"]) or "No specific areas were recorded."), "",
    ]
    for question in content["questions"]:
        lines.extend([
            f"{question['question_id']}: {question['prompt']}",
            f"Marks: {question['marks']} / {question['maximum_marks']}",
            "Strengths: " + ("; ".join(question["strengths"]) or "None recorded."),
            "Missing concepts: " + ("; ".join(question["missing_concepts"]) or "None recorded."),
            "Suggestions: " + "; ".join(question["suggestions"]), "",
        ])
    if content.get("demo_result"):
        lines.extend(["Notice: The approved source marks came from a simulated placeholder evaluation, not answer-quality assessment."])
    y = 55
    for index, line in enumerate(lines):
        if y > 770:
            page = document.new_page()
            y = 55
        page.insert_text((50, y), line[:180], fontsize=14 if index == 0 else 10)
        y += 20 if index == 0 else 15
    data = document.tobytes()
    document.close()
    return data


class EmailProvider(Protocol):
    mode: str
    def send(self, recipient: str, subject: str, body: str, attachment: bytes) -> str: ...


class SimulationEmailProvider:
    mode = "simulation"
    def send(self, recipient: str, subject: str, body: str, attachment: bytes) -> str:
        return "Simulated only. No email was sent."


class SMTPEmailProvider:
    mode = "smtp"
    def __init__(self):
        self.host = os.getenv("EVALAI_SMTP_HOST", "").strip()
        self.port = int(os.getenv("EVALAI_SMTP_PORT", "587"))
        self.username = os.getenv("EVALAI_SMTP_USERNAME", "")
        self.password = os.getenv("EVALAI_SMTP_PASSWORD", "")
        self.sender = os.getenv("EVALAI_EMAIL_FROM", "").strip()
        if not self.host or not self.sender:
            raise ValueError("SMTP is selected but EVALAI_SMTP_HOST and EVALAI_EMAIL_FROM are required.")

    def send(self, recipient: str, subject: str, body: str, attachment: bytes) -> str:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        message.add_attachment(attachment, maintype="application", subtype="pdf", filename="feedback-report.pdf")
        with smtplib.SMTP(self.host, self.port, timeout=30) as server:
            server.starttls()
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)
        return "Email accepted by the configured SMTP server."


def configured_email_provider() -> EmailProvider:
    mode = os.getenv("EVALAI_EMAIL_PROVIDER", "simulation").strip().lower()
    if mode == "simulation":
        return SimulationEmailProvider()
    if mode == "smtp":
        return SMTPEmailProvider()
    raise ValueError("EVALAI_EMAIL_PROVIDER must be 'simulation' or 'smtp'.")
