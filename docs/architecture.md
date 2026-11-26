# EvalAI architecture

EvalAI is a local FastAPI application with a browser-based faculty workspace. The API serves `frontend/index.html`, exposes JSON endpoints, persists prototype data in SQLite, and stores uploads in the configured local upload directory.

## Evaluation flow

1. Faculty configures an assessment with its question paper, model answer, rubric, and marking policy.
2. Answer-sheet PDFs are uploaded and matched to the local roster automatically when possible.
3. Matched submissions enter the background evaluation queue automatically. Unmatched submissions pause for faculty mapping and resume after mapping.
4. The configured evaluation provider grades original answer-sheet pages directly as multimodal input; OCR/transcription is not a grading prerequisite.
5. Results contain question-wise marks, concise reasons, totals, and uncertainty/readability signals.
6. Faculty reviews, edits if needed, and approves marks before feedback is official.

## Boundaries and limitations

- `backend/app/main.py` owns HTTP routes, authentication, validation, and frontend serving.
- `backend/app/worker.py` processes queued work; `backend/app/evaluation.py` owns provider contracts.
- `frontend/` contains the browser client.
- SQLite and `backend/uploads/` are local runtime state, not release artifacts.
- No college SIS/LMS or student login integration is connected.
- Live external evaluation requires explicit provider configuration and consent; automated tests use isolated demo providers.
- Deployment, scaling, and operational monitoring are outside this cleanup.
