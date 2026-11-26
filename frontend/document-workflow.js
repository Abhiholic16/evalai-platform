/* Local upload, page extraction, and faculty verification for the existing wizard. */
(function () {
  var activeWorkspace = null;
  var workspacePromise = null;
  var referenceUploadTask = null;
  var queueTimer = null;
  var referenceTimer = null;
  var originalRenderQueue = window.renderQueue;
  var originalStartSetup = window.startSetup;
  var originalStudentPerformance = window.renderStudentPerformance;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  async function request(path, options) {
    var response;
    try { response = await fetch(path, options || {}); }
    catch (_) { throw new Error("The local API is not reachable. Start the backend and reload this page."); }
    var payload = null;
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok) {
      var detail = payload && payload.detail;
      throw new Error(typeof detail === "string" ? detail : "Request failed (" + response.status + ").");
    }
    return payload;
  }

  function uploadForm(form, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/documents");
      xhr.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable && onProgress) onProgress(Math.round(event.loaded / event.total * 100));
      });
      xhr.addEventListener("load", function () {
        var payload = null;
        try { payload = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
        else {
          var detail = payload && payload.detail;
          reject(new Error(typeof detail === "string" ? detail : "Upload failed (" + xhr.status + ")."));
        }
      });
      xhr.addEventListener("error", function () {
        reject(new Error("The local API is not reachable. Start the backend and reload this page."));
      });
      xhr.send(form);
    });
  }

  function criteriaFromForm() {
    return Array.prototype.map.call(document.querySelectorAll("#criteria .criterion"), function (row) {
      return { name: row.children[0].value.trim(), points: Number(row.children[1].value) || 0 };
    }).filter(function (item) { return item.name; });
  }

  function workspacePayload(code) {
    return {
      assessment_code: code || undefined,
      title: document.getElementById("testName").value.trim() || "New assessment",
      class_code: document.getElementById("setupClass").value,
      section: document.getElementById("setupSection").value,
      subject_code: document.getElementById("setupSubject").value,
      test_code: document.getElementById("testType").value,
      test_date: document.getElementById("testDate").value || "",
      question_source: window.getEvaluationQuestions && window.getEvaluationQuestions().length ? "manual" : "question_paper",
      rules: document.getElementById("rules").value,
      marking_mode: document.getElementById("markingMode").value,
      correctness_threshold: (Number(document.getElementById("correctnessThreshold").value) || 0) / 100,
      score_bands: [
        { label: "Full credit", minimum_correctness_percent: Number(document.getElementById("correctnessThreshold").value) || 0, award_percent: 100 },
        { label: "Partial credit", minimum_correctness_percent: Number(document.getElementById("partialBandFloor").value) || 0, award_percent: Number(document.getElementById("partialAwardPercent").value) || 0 },
        { label: "No credit", minimum_correctness_percent: 0, award_percent: 0 }
      ],
      rubric_criteria: criteriaFromForm(),
      questions: window.getEvaluationQuestions && window.getEvaluationQuestions().length ? window.getEvaluationQuestions() : undefined,
      total_marks: window.getEvaluationTotalMarks ? window.getEvaluationTotalMarks() : 0
    };
  }

  async function ensureWorkspace() {
    if (workspacePromise) return workspacePromise;
    var required = [
      ["testName", "Assessment name"], ["setupClass", "Program and year"],
      ["setupSection", "Section"], ["setupSubject", "Subject"], ["testDate", "Test date"]
    ];
    var missing = required.filter(function (entry) { return !document.getElementById(entry[0]).value.trim(); });
    var questions = window.getEvaluationQuestions && window.getEvaluationQuestions();
    if (!document.getElementById("paperFile").files.length && !questions.length) missing.push(["paperFile", "Question paper or manual questions"]);
    if (missing.length) {
      var first = missing[0];
      setStatus("referenceTransferStatus", "Required: " + first[1] + ". Choose a question paper or add at least one manual question.", true);
      document.getElementById(first[0]).focus();
      throw new Error("Complete the required assessment setup fields before continuing.");
    }
    workspacePromise = request("/api/assessment-workspaces", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(workspacePayload(activeWorkspace && activeWorkspace.assessment_code))
    }).then(function (workspace) {
      activeWorkspace = workspace;
      return workspace;
    });
    return workspacePromise;
  }

  function setStatus(id, text, isError) {
    var element = document.getElementById(id);
    if (!element) return;
    element.textContent = text;
    element.setAttribute("role", isError ? "alert" : "status");
    element.style.color = isError ? "#a74141" : "";
  }

  function fileInputEntries() {
    return [
      ["paperFile", "question_paper"], ["modelFile", "model_answer"], ["rubricFile", "rubric"]
    ].map(function (entry) {
      return { file: document.getElementById(entry[0]).files[0], type: entry[1] };
    }).filter(function (entry) { return !!entry.file; });
  }

  async function uploadOne(file, type, workspace, roll, onProgress) {
    var form = new FormData();
    form.append("assessment_code", workspace.assessment_code);
    form.append("document_type", type);
    if (roll) form.append("student_roll_number", roll);
    if (document.getElementById("setup").style.display !== "none" && document.getElementById("syntheticUploads").checked) {
      form.append("synthetic_data", "true");
    }
    form.append("file", file, file.name);
    var uploaded = await uploadForm(form, onProgress);
    return uploaded.document;
  }

  function pending(doc) {
    return doc.job && ["queued", "running"].includes(doc.job.state);
  }

  function jobStatus(doc) {
    if (!doc.job) return doc.status_message;
    var job = doc.job;
    var progress = job.page_count ? " (" + job.pages_done + "/" + job.page_count + " pages)" : "";
    return job.detail + progress;
  }

  async function saveReferenceFiles() {
    setStatus("referenceTransferStatus", "Saving the assessment and uploading references?", false);
    try {
      var workspace = await ensureWorkspace();
      var files = await Promise.all(fileInputEntries().map(function (entry) {
        return uploadOne(entry.file, entry.type, workspace);
      }));
      setStatus("referenceTransferStatus", files.length ? "References saved and queued." : "Assessment setup saved.", false);
      if (window.renderAssessments) window.renderAssessments();
      if (files.length) refreshReferences(workspace.assessment_code);
    } catch (error) {
      workspacePromise = null;
      setStatus("referenceTransferStatus", error.message, true);
    }
  }

  async function refreshReferences(code) {
    clearTimeout(referenceTimer);
    try {
      var review = await request("/api/assessment-workspaces/" + encodeURIComponent(code) + "/review");
      if (!activeWorkspace || activeWorkspace.assessment_code !== code || document.getElementById("setup").style.display === "none") return;
      setStatus("referenceTransferStatus", review.reference_documents.map(function (doc) {
        return doc.original_filename + " ? " + jobStatus(doc);
      }).join(" "), false);
      if (review.reference_documents.some(pending)) {
        referenceTimer = setTimeout(function () { refreshReferences(code); }, 2000);
        return;
      }
      if (review.workspace.questions.length && window.setEvaluationQuestions) {
        var current = window.getEvaluationQuestions();
        if (!current.length) {
          window.setEvaluationQuestions(review.workspace.questions);
        } else {
          var imported = new Map(review.workspace.questions.map(function (q) { return [q.question_id, q]; }));
          var merged = current.map(function (q) {
            var saved = imported.get(q.question_id);
            return saved && !q.model_answer ? Object.assign({}, q, { model_answer: saved.model_answer }) : q;
          });
          if (JSON.stringify(merged) !== JSON.stringify(current)) window.setEvaluationQuestions(merged);
        }
        setStatus("assessmentConfigStatus", "Review the imported questions, answers, and maximum marks.", false);
      } else {
        setStatus("assessmentConfigStatus", "No questions imported. Review the paper or enter questions manually.", true);
      }
    } catch (error) {
      setStatus("referenceTransferStatus", error.message, true);
    }
  }

  function beginReferenceUpload() {
    if (!referenceUploadTask) {
      referenceUploadTask = saveReferenceFiles().finally(function () { referenceUploadTask = null; });
    }
  }

  async function enterDocumentQueue() {
    var button = document.getElementById("openNewQueue");
    button.disabled = true;
    setStatus("queueUploadStatus", "Saving assessment setup and uploading answer sheets?", false);
    try {
      if (referenceUploadTask) await referenceUploadTask;
      workspacePromise = null;
      var workspace = await ensureWorkspace();
      var selections = Array.from(document.querySelectorAll("[data-match]"));
      var results = await Promise.allSettled(batchFiles.map(function (file, index) {
        return uploadOne(file, "answer_sheet", workspace, selections[index] && selections[index].value);
      }));
      var rejected = results.filter(function (result) { return result.status === "rejected"; });
      if (rejected.length) throw new Error(rejected.length + " upload(s) failed. " + rejected[0].reason.message + " Submit again to retry. Saved files are kept.");
      setStatus("queueUploadStatus", "Files saved. You can leave and return from the assessment dashboard.", false);
      await window.openAssessment(workspace.assessment_code);
    } catch (error) {
      setStatus("queueUploadStatus", error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function documentForRoll(assessment, roll) {
    return (assessment.documents || []).find(function (doc) { return doc.student_roll_number === roll; });
  }

  function statusLabel(document) {
    if (!document) return "No upload";
    if (document.document_type === "answer_sheet") {
      if (document.job && ["queued", "running"].includes(document.job.state)) return "Preparing visual evaluation";
      if (document.job && document.job.state === "complete") return "Ready for faculty review";
      if (document.job && document.job.state === "needs_attention") return "Evaluation failed - retry available";
      return "Ready to evaluate visually";
    }
    if (document.job) return jobStatus(document);
    if (document.pages && document.pages.length && document.pages.every(function (page) { return page.faculty_verified; })) return "Faculty verified - text correction saved";
    if (document.status === "extracted") return "Text extracted - faculty review needed";
    if (document.status === "awaiting_provider") return "No OCR provider - manual transcription needed";
    if (document.status === "partial") return "Partial text - review flagged pages";
    if (document.status === "failed") return "Processing failed - retry or inspect original";
    if (document.status === "stored_only") return "Stored - export Office file to PDF for extraction";
    return document.status_message || document.status;
  }

  function renderDocumentQueue(assessment) {
    var list = namesFor(assessment);
    var documents = assessment.documents || [];
    var needsDetails = documents.filter(function (doc) { return !Array.isArray(doc.pages); });
    if (needsDetails.length) {
      document.getElementById("queueRows").innerHTML = '<tr><td colspan="6" class="empty" role="status">Loading stored extraction results…</td></tr>';
      Promise.all(needsDetails.map(function (doc) {
        return request("/api/documents/" + doc.id).then(function (detail) {
          var index = documents.findIndex(function (item) { return item.id === detail.id; });
          if (index >= 0) documents[index] = detail;
        });
      })).then(function () { assessment.documents = documents; renderDocumentQueue(assessment); })
        .catch(function (error) {
          document.getElementById("queueRows").innerHTML = '<tr><td colspan="6" class="empty" role="alert">' + esc(error.message) + '</td></tr>';
        });
      return;
    }
    document.getElementById("queueTitle").textContent = assessment.title + " - Visual evaluation review";
    document.getElementById("queueContext").textContent = (classes[assessment.cls] || assessment.cls) + " · Section " + assessment.section + " · " + (subjects[assessment.subject] || assessment.subject) + " · " + (tests[assessment.test] || assessment.test) + " · " + assessment.date;
    document.getElementById("queueTest").textContent = (tests[assessment.test] || assessment.test).toUpperCase();
    var allDocs = documents;
    var reviewPayload = assessment.reviewPayload;
    var submissions = reviewPayload ? reviewPayload.submissions : [];
    document.getElementById("qStudents").textContent = list.length;
    document.getElementById("qPending").textContent = submissions.filter(function (item) {
      return item.evaluation && ["provisional", "edited"].includes(item.evaluation.status);
    }).length;
    document.getElementById("qFlags").textContent = submissions.filter(function (item) {
      var evaluationFlags = item.evaluation ? item.evaluation.ai_proposed.some(function (result) { return (result.uncertainty_flags || []).length > 0; }) : false;
      return evaluationFlags;
    }).length;
    document.getElementById("qApproved").textContent = submissions.filter(function (item) { return item.evaluation && item.evaluation.status === "approved"; }).length;
    var finalStat = document.querySelector("#classReview .stats .stat:last-child");
    if (finalStat) {
      var finalLine = finalStat.querySelector(".statline");
      var finalNote = finalStat.querySelector(".statnote");
      if (finalLine) finalLine.textContent = "Approved";
      if (finalNote) finalNote.textContent = "Faculty-approved marks";
    }
    var bulk = document.getElementById("bulkApprove");
    bulk.disabled = true;
    bulk.hidden = true;
    var notice = document.querySelector("#classReview .notice span:last-child");
    if (notice) notice.textContent = "Evaluate each original answer sheet visually and review provisional marks. Faculty approval is required before marks become official.";
    var headers = document.querySelectorAll("#classReview .card table thead th");
    ["Roll no.", "Student", "Answer sheet", "Pages", "Status", "Faculty action"].forEach(function (label, index) {
      if (headers[index]) headers[index].textContent = label;
    });
    var cardDesc = document.querySelector("#classReview .card .carddesc");
    if (cardDesc) cardDesc.textContent = "Matched sheets are evaluated automatically. Open a completed recommendation to review question-wise marks and reasons.";
    var reviewSummary = document.getElementById("workspaceReviewSummary");
    if (!reviewSummary) {
      reviewSummary = document.createElement("section");
      reviewSummary.id = "workspaceReviewSummary";
      reviewSummary.className = "card pad";
      var reviewHead = document.querySelector("#classReview .reviewhead");
      if (reviewHead && reviewHead.parentNode) reviewHead.parentNode.insertBefore(reviewSummary, reviewHead.nextSibling);
    }
    var reviewQuestions = assessment.questions || (reviewPayload && reviewPayload.workspace.questions) || [];
    var policyText = (assessment.markingMode === "stepwise" ? "Step-wise marking" : "Holistic / percentage marking") +
      " · correctness threshold " + Math.round((assessment.correctnessThreshold == null ? 0.5 : assessment.correctnessThreshold) * 100) + "%";
    var referenceDocuments = reviewPayload ? reviewPayload.reference_documents || [] : [];
    var unmatched = (reviewPayload && reviewPayload.unmatched_documents) || [];
    clearTimeout(queueTimer);
    if (allDocs.concat(referenceDocuments, unmatched).some(pending)) {
      queueTimer = setTimeout(function checkProgress() {
        if (activeAssessment === assessment && document.getElementById("classReview").style.display !== "none" &&
            !document.querySelector("dialog[open], #workspaceReviewSummary :focus, #queueRows :focus")) {
          window.openAssessment(assessment.id, true);
        } else if (activeAssessment === assessment && document.getElementById("classReview").style.display !== "none") {
          queueTimer = setTimeout(checkProgress, 4000);
        }
      }, 2500);
    }
    reviewSummary.innerHTML = '<div class="cardtitle">Assessment reference</div><div class="carddesc">' + reviewQuestions.length + ' questions · ' + (assessment.totalMarks || 0) + ' total marks · ' + esc(policyText) + '</div>' +
      '<label class="filepick" style="display:inline-flex;margin:10px 0"><strong>Add model answer PDF</strong><small>Optional; attach it now or later</small><input id="lateModelAnswer" type="file" accept=".pdf,application/pdf"></label><div id="lateModelAnswerStatus" class="carddesc" role="status"></div>' +
      '<button type="button" class="btn small" id="refreshAssessmentReview">Refresh status</button> ' +
      '<button type="button" class="btn small" id="editAssessmentReferences">Edit assessment setup</button>' +
      '<label class="filepick"><strong>Add answer sheet PDFs</strong><input id="lateAnswerSheets" type="file" accept=".pdf,application/pdf" multiple></label>' +
      '<div id="lateAnswerSheetStatus" class="carddesc" role="status"></div>' +
      (referenceDocuments.length ? '<div class="recordmeta">' + referenceDocuments.map(function (doc) { return '<div><button type="button" class="btn small" data-open-reference="' + doc.id + '">' + esc(doc.document_type.replaceAll("_", " ") + ": " + doc.original_filename) + '</button></div>'; }).join(" ") + '</div>' : '') +
      (reviewQuestions.length ? '<details><summary>View questions and model answers</summary>' + reviewQuestions.map(function (question) {
        return '<article class="assessment-record"><strong>' + esc(question.question_id) + ' · ' + esc(question.maximum_marks) + ' marks</strong><div class="recordmeta">' + esc(question.prompt) + '</div>' +
          (question.model_answer ? '<div class="recordnote"><b>Model answer</b>' + esc(question.model_answer) + '</div>' : '<div class="extractwarning">No model answer saved for this question.</div>') + '</article>';
      }).join("") + '</details>' : '<div class="report-empty">No questions are saved yet. Upload a readable question paper or add questions manually in assessment setup.</div>');
    var toolbar = document.createElement("div");
    toolbar.className = "reference-toolbar";
    ["refreshAssessmentReview", "editAssessmentReferences"].forEach(function (id) { toolbar.appendChild(document.getElementById(id)); });
    reviewSummary.insertBefore(toolbar, reviewSummary.firstChild);
    var uploads = document.createElement("div");
    uploads.className = "reference-uploads";
    reviewSummary.querySelectorAll("label.filepick").forEach(function (label) {
      label.removeAttribute("style");
      uploads.appendChild(label);
    });
    reviewSummary.insertBefore(uploads, document.getElementById("lateModelAnswerStatus"));
    document.getElementById("refreshAssessmentReview").onclick = function () { window.openAssessment(assessment.id); };
    document.getElementById("editAssessmentReferences").onclick = function () { editWorkspace(reviewPayload.workspace); };
    document.getElementById("lateAnswerSheets").onchange = async function () {
      var files = Array.from(this.files);
      try {
        await Promise.all(files.map(function (file) { return uploadOne(file, "answer_sheet", reviewPayload.workspace); }));
        await window.openAssessment(assessment.id);
      } catch (error) { setStatus("lateAnswerSheetStatus", error.message, true); }
    };
    var lateModelAnswer = document.getElementById("lateModelAnswer");
    if (lateModelAnswer) lateModelAnswer.addEventListener("change", async function () {
      var file = lateModelAnswer.files && lateModelAnswer.files[0];
      if (!file) return;
      setStatus("lateModelAnswerStatus", "Uploading and processing the model answer...", false);
      var form = new FormData();
      form.append("assessment_code", assessment.id);
      form.append("document_type", "model_answer");
      form.append("file", file, file.name);
      try {
        await uploadForm(form);
        await window.openAssessment(assessment.id);
      } catch (error) {
        setStatus("lateModelAnswerStatus", error.message, true);
      }
    });
    reviewSummary.querySelectorAll("[data-open-reference]").forEach(function (button) {
      button.addEventListener("click", function () {
        var doc = referenceDocuments.find(function (item) { return item.id === Number(button.dataset.openReference); });
        if (doc) showStoredDocument(doc.id, doc.original_filename);
      });
    });
    document.getElementById("queueRows").innerHTML = list.map(function (student) {
      var doc = documentForRoll(assessment, student.roll);
      return '<tr data-document-row="' + esc(student.roll) + '"><td>' + esc(student.roll) + '</td><td><span class="primarytext">' + esc(student.name) + '</span></td><td>' +
        (doc ? '<button class="btn small" data-open-stored="' + doc.id + '">Open original</button>' : 'No file') +
        '</td><td>' + (doc && doc.pages.length ? doc.pages.length + ' page(s)' : 'Pending') +
        '</td><td><span class="pill amber">' + esc(statusLabel(doc)) + '</span>' +
        (doc && doc.job && doc.job.match_detail ? '<div class="recordmeta">' + esc(doc.job.match_detail) + '</div>' : "") + '</td><td>' +
        (doc && doc.job && doc.job.state === "needs_attention" ?
          '<div class="extractwarning">' + esc(doc.job.detail || "Evaluation needs attention.") + '</div>' +
          (doc.job.stage === "evaluation" ? '<button class="btn small" data-retry-evaluation="' + doc.id + '">Retry evaluation</button>' : '') :
          (doc ? '' : 'Upload required')) + '</td></tr>';
    }).join("") + unmatched.map(function (doc) {
      var ready = !pending(doc);
      return '<tr><td>' + esc((doc.job && doc.job.detected_roll) || "Pending") + '</td><td>' +
        '<strong>' + esc(doc.original_filename) + '</strong></td><td><button class="btn small" data-open-stored="' + doc.id + '">Open original</button></td><td>' +
        (doc.pages.length ? '<button class="btn small" data-review-extraction="' + doc.id + '">Review pages</button>' : "") +
        '</td><td>' + (ready ? '<span class="recordmeta">Awaiting faculty match</span>' +
        '<select id="match-document-' + doc.id + '" aria-label="Confirm student"><option value="">Choose student</option>' +
        reviewPayload.roster.map(function (student) { return '<option value="' + esc(student.roll_number) + '">' + esc(student.roll_number + " ? " + student.name) + '</option>'; }).join("") +
        '</select> <button class="btn small" data-confirm-match="' + doc.id + '">Confirm match</button>' : "") + '</td></tr>';
    }).join("") || '<tr><td colspan="6" class="empty">No answer sheets uploaded.</td></tr>';
    document.querySelectorAll("[data-retry-evaluation]").forEach(function (button) {
      button.onclick = async function () {
        button.disabled = true;
        button.textContent = "Queuing retry...";
        try {
          await request("/api/documents/" + button.dataset.retryEvaluation + "/process?retry=true", { method: "POST" });
          await window.openAssessment(assessment.id);
        } catch (error) {
          toast(error.message);
          button.disabled = false;
          button.textContent = "Retry evaluation";
        }
      };
    });
    document.querySelectorAll("[data-confirm-match]").forEach(function (button) {
      button.onclick = async function () {
        var id = button.dataset.confirmMatch;
        var roll = document.getElementById("match-document-" + id).value;
        if (!roll) return;
        button.disabled = true;
        try {
          await request("/api/documents/" + id + "/match", {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ student_roll_number: roll })
          });
          await window.openAssessment(assessment.id);
        } catch (error) { toast(error.message); button.disabled = false; }
      };
    });
    document.querySelectorAll("[data-open-stored]").forEach(function (button) {
      button.addEventListener("click", function () {
        var doc = allDocs.concat(referenceDocuments, unmatched).find(function (item) { return item.id === Number(button.dataset.openStored); });
        if (doc) showStoredDocument(doc.id, doc.original_filename);
      });
    });
    document.querySelectorAll("[data-review-extraction]").forEach(function (button) {
      button.addEventListener("click", function () { openExtractionReview(Number(button.dataset.reviewExtraction)); });
    });
    if (window.renderEvaluationActions) window.renderEvaluationActions(assessment, allDocs);
  }

  window.renderQueue = function () {
    if (activeAssessment && activeAssessment._documentWorkflow) return renderDocumentQueue(activeAssessment);
    document.getElementById("bulkApprove").hidden = false;
    return originalRenderQueue();
  };

  window.enterNewQueue = enterDocumentQueue;
  window.getDocumentWorkflowAssessment = function () { return activeAssessment; };

  var previewUrl = null;
  async function showStoredDocument(documentId, filename) {
    document.getElementById("pdfTitle").textContent = filename + " · uploaded original";
    document.getElementById("pdfFrame").style.display = "block";
    var frame = document.getElementById("pdfFrame");
    frame.removeAttribute("src");
    document.getElementById("demoSheet").style.display = "none";
    document.getElementById("pdfDialog").showModal();
    try {
      var response = await fetch("/api/documents/" + documentId + "/file");
      if (!response.ok) throw new Error("Could not load the original document (" + response.status + ").");
      var blob = await response.blob();
      if (!document.getElementById("pdfDialog").open) return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(blob);
      frame.src = previewUrl;
    } catch (error) {
      document.getElementById("pdfTitle").textContent = error.message;
    }
  }
  document.getElementById("pdfDialog").addEventListener("close", function () {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  });

  window.openStoredAnswerDocument = showStoredDocument;

  function confidenceDescription(page) {
    if (page.confidence_source === "model_self_assessment") {
      return "Vision provider estimate: " + page.confidence_label + " (not calibrated; verify manually)";
    }
    if (page.confidence_source === "embedded_pdf_text") return "Digital PDF text layer; OCR confidence does not apply";
    return "Confidence unknown; text requires manual transcription";
  }

  async function openExtractionReview(documentId) {
    var body = document.getElementById("extractionBody");
    var dialog = document.getElementById("extractionDialog");
    body.innerHTML = '<div class="report-empty" role="status">Loading extraction results…</div>';
    document.getElementById("extractionTitle").textContent = "Extracted answer verification";
    dialog.showModal();
    try {
      var documentRecord = await request("/api/documents/" + documentId);
      document.getElementById("extractionTitle").textContent = documentRecord.original_filename + " · page-by-page verification";
      var intro = '<div class="extractintro"><b>' + esc(documentRecord.status_message) + '</b><br>Raw extraction is immutable. Save corrections in the separate faculty-corrected field. No grading or marks are produced here.</div>';
      if (!documentRecord.pages.length) {
        body.innerHTML = intro + '<div class="report-empty">This file is stored, but no page text is available to review. Export Office documents as PDF. Scanned documents need a configured vision provider or manual transcription.</div>';
        return;
      }
      body.innerHTML = intro + documentRecord.pages.map(function (page) {
        var answers = page.question_answers || [];
        var questionHtml = answers.length ? '<div class="carddesc" style="margin-top:8px">Question-wise text found on this page:</div>' + answers.map(function (answer) {
          return '<div class="extractqa"><b>' + esc(answer.question || "Question") + ' · ' + esc(answer.confidence || "unknown") + ' signal</b><br>' + esc(answer.raw_text || answer.text || "") +
            ((answer.uncertain_spans || []).length ? '<br><span class="extractwarning">Uncertain: ' + esc(answer.uncertain_spans.join("; ")) + '</span>' : '') + '</div>';
        }).join("") : '<div class="extractmeta">No question boundaries were confidently identified on this page.</div>';
        var warning = page.needs_review ? '<div class="extractwarning">Manual verification required. ' + esc(confidenceDescription(page)) + '</div>' : '';
        var shownCorrected = page.corrected_text == null ? page.raw_text : page.corrected_text;
        return '<section class="extractpage"><h3>Page ' + page.page_number + ' · ' + esc(page.source) + '</h3><div class="extractmeta">' + esc(confidenceDescription(page)) + '</div>' + warning +
          '<b style="font-size:9px">Raw extracted text · read only</b><div class="extractraw">' + (page.raw_text ? esc(page.raw_text) : 'No text was extracted for this page.') + '</div>' + questionHtml +
          '<label for="corrected-' + page.page_number + '" style="display:block;font-size:9px;font-weight:700;margin-top:10px">Faculty-corrected text · editable</label>' +
          '<textarea id="corrected-' + page.page_number + '" maxlength="20000" aria-label="Faculty corrected text, page ' + page.page_number + '">' + esc(shownCorrected) + '</textarea>' +
          '<div class="extractactions"><button class="btn primary small" data-save-correction="' + page.page_number + '">Save correction</button></div></section>';
      }).join("");
      body.querySelectorAll("[data-save-correction]").forEach(function (button) {
        button.addEventListener("click", async function () {
          button.disabled = true;
          try {
            var pageNumber = Number(button.dataset.saveCorrection);
            await request("/api/documents/" + documentId + "/pages/" + pageNumber, {
              method: "PUT", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ corrected_text: document.getElementById("corrected-" + pageNumber).value })
            });
            button.textContent = "Correction saved";
            toast("Faculty correction saved separately from raw extraction.");
            if (activeAssessment && activeAssessment._documentWorkflow) await window.openAssessment(activeAssessment.id);
          } catch (error) {
            button.disabled = false;
            button.textContent = "Retry save";
            toast(error.message);
          }
        });
      });
    } catch (error) {
      body.innerHTML = '<div class="report-empty" role="alert">' + esc(error.message) + '</div>';
    }
  }

  var connectedRenderStudentPerformance = window.renderStudentPerformance;
  window.renderStudentPerformance = async function (student) {
    await connectedRenderStudentPerformance(student);
    try {
      var payload = await request("/api/documents?student_roll_number=" + encodeURIComponent(student.roll));
      var docs = payload.records;
      var report = document.getElementById("studentReport");
      var existing = document.getElementById("studentDocumentHistory");
      if (existing) existing.remove();
      var card = document.createElement("div");
      card.className = "assessment-history";
      card.id = "studentDocumentHistory";
      card.innerHTML = '<div class="cardtitle">Uploaded documents &amp; extraction</div><div class="carddesc">Stored for this student and assessment. Scanned pages without a configured provider remain blank for manual review.</div>' +
        (docs.length ? docs.map(function (doc) {
          return '<article class="assessment-record"><div class="recordtop"><div><strong>' + esc(doc.original_filename) + '</strong><div class="recordmeta">' + esc(doc.document_type.replaceAll("_", " ")) + ' · ' + esc(doc.assessment_code) + '</div></div><span class="pill amber">' + esc(doc.status) + '</span></div><div class="recordmeta">' + esc(doc.status_message) + '</div><button class="btn small" data-student-document="' + doc.id + '">Review extracted pages</button></article>';
        }).join("") : '<div class="report-empty">No documents uploaded for this student.</div>');
      report.appendChild(card);
      card.querySelector(".cardtitle").textContent = "Original documents";
      card.querySelector(".carddesc").textContent = "View the uploaded originals used for evaluation.";
      card.querySelectorAll(".pill, .assessment-record > .recordmeta").forEach(function (element) { element.remove(); });
      card.querySelectorAll("[data-student-document]").forEach(function (button) {
        button.textContent = "Open original";
        button.addEventListener("click", function () {
          var doc = docs.find(function (item) { return item.id === Number(button.dataset.studentDocument); });
          if (doc) showStoredDocument(doc.id, doc.original_filename);
        });
      });
    } catch (_) {
      // The connected report remains usable if document history is temporarily unavailable.
    }
  };

  function editWorkspace(workspace) {
    window.startSetup();
    activeWorkspace = workspace;
    var fields = {
      testName: workspace.title, setupClass: workspace.class_code, setupSection: workspace.section,
      setupSubject: workspace.subject_code, testType: workspace.test_code, testDate: workspace.test_date,
      rules: workspace.rules, markingMode: workspace.marking_mode,
      correctnessThreshold: Math.round(workspace.correctness_threshold * 100)
    };
    Object.keys(fields).forEach(function (id) {
      var input = document.getElementById(id);
      var value = fields[id] == null ? "" : fields[id];
      if (value && input.tagName === "SELECT" && !Array.from(input.options).some(function (option) { return option.value === value; })) {
        input.add(new Option(value, value));
      }
      input.value = value;
    });
    var partial = (workspace.score_bands || []).find(function (band) { return band.label === "Partial credit"; });
    if (partial) {
      document.getElementById("partialBandFloor").value = partial.minimum_correctness_percent;
      document.getElementById("partialAwardPercent").value = partial.award_percent;
    }
    var criteria = document.getElementById("criteria");
    var template = criteria.querySelector(".criterion");
    if (template) {
      criteria.replaceChildren(...(workspace.rubric_criteria || []).map(function (rule) {
        var row = template.cloneNode(true);
        row.children[0].value = rule.name;
        row.children[1].value = rule.points;
        var remove = row.querySelector("button");
        if (remove) remove.onclick = function () { row.remove(); };
        return row;
      }));
    }
    window.setEvaluationQuestions(workspace.questions);
  }

  window.startSetup = function () {
    clearTimeout(referenceTimer);
    document.getElementById("syntheticUploads").checked = false;
    if (!document.getElementById("testDate").value) document.getElementById("testDate").value = new Date().toISOString().slice(0, 10);
    activeWorkspace = null;
    workspacePromise = null;
    originalStartSetup();
  };

  window.initDocumentWorkflow = function () {
    document.getElementById("saveSetup").addEventListener("click", beginReferenceUpload);
    document.getElementById("closeExtraction").addEventListener("click", function () {
      document.getElementById("extractionDialog").close();
    });
    document.getElementById("extractionDialog").addEventListener("click", function (event) {
      if (event.target === this) this.close();
    });
  };
})();
