
# EvalAI - Project Instructions

## Project Overview
EvalAI is an Intelligent Assessment and Personalized Learning Feedback Platform for a fictional client, ABC College.

The system helps faculty evaluate handwritten student examination papers using OCR and AI-assisted rubric-based evaluation.

## Business Workflow
1. Faculty uploads question papers, model answers, and marking rubrics.
2. Faculty uploads scanned handwritten answer sheets.
3. OCR/vision extracts question-wise answers.
4. An LLM evaluates answers against the marking rubric.
5. The system generates proposed marks, explanations, and personalized feedback.
6. Unclear answers and uncertain evaluations are flagged.
7. Faculty reviews, edits, and approves all marks.
8. Approved feedback is delivered through email.
9. Faculty can view student performance history and trends.

Students do not log in. Student records come from the college database through a backend API.

## Technology Stack
- Frontend: Existing HTML, CSS, and JavaScript prototype.
- Backend: Python and FastAPI.
- Database: SQLite initially.
- ORM: SQLAlchemy.
- Validation: Pydantic.
- AI: OpenAI API.
- Document processing: PyMuPDF and appropriate OCR/vision tools.
- Testing: Pytest.
- Version control: Git and GitHub.

## Important Development Rules
- Inspect existing files before making changes.
- Preserve the existing frontend design and functionality.
- Do not rebuild the frontend unnecessarily.
- Build in small, independently testable phases.
- Never implement the entire project in one step.
- Do not invent integrations or claim simulated features are real.
- Use fictional sample student records during development.
- Never expose API keys or secrets in source code.
- Use environment variables for configuration.
- Validate uploaded files and API inputs.
- Faculty approval is mandatory before marks become official.
- Add error handling and tests for important features.
- Avoid unnecessary complexity.

## How to Work With Me
I know basic Python and want to understand the engineering.

For every task:
1. Explain what we are building and why.
2. Inspect relevant existing code first.
3. Propose the files that need to be created or modified.
4. Implement only the requested task.
5. Explain important code and decisions.
6. Give me exact commands to run.
7. Tell me how to test the result.
8. Fix errors before moving to another feature.
9. Summarize completed work and remaining limitations.

Do not move to the next development phase without my confirmation.
