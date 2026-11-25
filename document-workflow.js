/* Local upload, page extraction, and faculty verification for the existing wizard. */
(function () {
  var activeWorkspace = null;
  var workspacePromise = null;
  var referenceUploadTask = null;
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
      rules: document.getElementById("rules").value,
      rubric_criteria: criteriaFromForm(),
      questions: window.getEvaluationQuestions ? window.getEvaluationQuestions() : [],
      total_marks: window.getEvaluationTotalMarks ? window.getEvaluationTotalMarks() : 0
    };
  }

  async function ensureWorkspace() {
    if (workspacePromise) return workspacePromise;
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
    form.append("file", file, file.name);
    var uploaded = await uploadForm(form, onProgress);
    var doc = uploaded.document;
    if (doc.status === "uploaded" || (doc.status === "failed" && !uploaded.duplicate)) {
      if (onProgress) onProgress(100, "Processing pages...");
      doc = await request("/api/documents/" + doc.id + "/process", { method: "POST" });
    }
    return { document: doc, duplicate: uploaded.duplicate, originalName: file.name };
  }

  async function saveReferenceFiles() {
    setStatus("referenceTransferStatus", "Saving the assessment and its reference documents...", false);
    try {
      var workspace = await ensureWorkspace();
      var files = fileInputEntries();
      var outcomes = [];
      for (var i = 0; i < files.length; i += 1) {
        setStatus("referenceTransferStatus", "Uploading and processing " + (i + 1) + " of " + files.length + ": " + files[i].file.name, false);
        outcomes.push(await uploadOne(files[i].file, files[i].type, workspace, null, function (percent, status) {
          setStatus("referenceTransferStatus", status || ("Uploading and processing " + (i + 1) + " of " + files.length + ": " + files[i].file.name + " (" + percent + "%)"), false);
        }));
      }
      var summary = files.length ? outcomes.map(function (outcome) {
        return outcome.originalName + ": " + outcome.document.status_message;
      }).join(" ") : "Assessment setup saved. Add reference documents now or continue to student answer sheets.";
      setStatus("referenceTransferStatus", summary, false);
      if (window.renderAssessments) window.renderAssessments();
    } catch (error) {
      workspacePromise = null;
      setStatus("referenceTransferStatus", error.message, true);
    }
  }

  function beginReferenceUpload() {
    if (!referenceUploadTask) {
      referenceUploadTask = saveReferenceFiles().finally(function () { referenceUploadTask = null; });
    }
  }

  function newAssessmentFromWorkspace(workspace, docs) {
    var roster = students.filter(function (student) {
      return student.cls === workspace.class_code && student.section === workspace.section;
    });
    return {
      id: workspace.assessment_code, title: workspace.title, cls: workspace.class_code,
      section: workspace.section, subject: workspace.subject_code, test: workspace.test_code,
      term: "Fall 2026", date: workspace.test_date || "Date not set", submitted: docs.length,
      totalMarks: workspace.total_marks, questions: workspace.questions || [],
      total: roster.length, status: "Documents uploaded", rolls: docs.map(function (doc) {
        return doc.document.student_roll_number;
      }), _documentWorkflow: true, documents: docs.map(function (doc) { return doc.document; })
    };
  }

  async function enterDocumentQueue() {
    var button = document.getElementById("openNewQueue");
    button.disabled = true;
    setStatus("queueUploadStatus", "Saving assessment setup and uploading answer sheets...", false);
    try {
      var workspace = await ensureWorkspace();
      if (referenceUploadTask) await referenceUploadTask;
      var jobs = [];
      document.querySelectorAll("[data-match]").forEach(function (select, index) {
        if (select.value) jobs.push({ file: batchFiles[index], roll: select.value });
      });
      if (!jobs.length) throw new Error("Match at least one answer sheet to a student first.");
      var docs = [];
      for (var i = 0; i < jobs.length; i += 1) {
        setStatus("answerUploadStatus", "Uploading and processing answer sheet " + (i + 1) + " of " + jobs.length + ": " + jobs[i].file.name, false);
        var outcome = await uploadOne(jobs[i].file, "answer_sheet", workspace, jobs[i].roll, function (percent, status) {
          setStatus("queueUploadStatus", status || ("Uploading answer sheet " + (i + 1) + " of " + jobs.length + ": " + jobs[i].file.name + " (" + percent + "%)"), false);
        });
        docs.push(outcome);
        fileByRoll[workspace.assessment_code + "|" + jobs[i].roll] = jobs[i].file;
      }
      var assessment = newAssessmentFromWorkspace(workspace, docs);
      assessment.rolls.forEach(function (roll) {
        results[assessment.id + "|" + roll] = { score: null, max: null, status: "Extraction pending", flag: true };
      });
      assessments = assessments.filter(function (item) { return item.id !== assessment.id; });
      assessments.unshift(assessment);
      activeAssessment = assessment;
      setStatus("queueUploadStatus", docs.map(function (item) {
        return item.document.original_filename + ": " + item.document.status_message;
      }).join(" "), docs.some(function (item) { return item.document.status === "failed"; }));
      if (window.renderAssessments) window.renderAssessments();
      window.renderQueue();
      window.go("classReview");
    } catch (error) {
      setStatus("queueUploadStatus", error.message, true);
      button.disabled = false;
    }
  }

  function documentForRoll(assessment, roll) {
    return (assessment.documents || []).find(function (doc) { return doc.student_roll_number === roll; });
  }

  function statusLabel(document) {
    if (!document) return "Not uploaded";
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
    document.getElementById("queueTitle").textContent = assessment.title + " - Answer extraction review";
    document.querySelector("#classReview .pagehead .sub").textContent = "Review uploaded answer sheets and verify extracted text. This workflow does not generate marks.";
    document.getElementById("queueContext").textContent = (classes[assessment.cls] || assessment.cls) + " · Section " + assessment.section + " · " + (subjects[assessment.subject] || assessment.subject) + " · " + (tests[assessment.test] || assessment.test) + " · " + assessment.date;
    document.getElementById("queueTest").textContent = (tests[assessment.test] || assessment.test).toUpperCase();
    var allDocs = documents;
    document.getElementById("qStudents").textContent = list.length;
    document.getElementById("qPending").textContent = allDocs.filter(function (doc) { return !doc.pages.length || doc.pages.some(function (page) { return page.needs_review; }); }).length;
    document.getElementById("qFlags").textContent = allDocs.filter(function (doc) { return !doc.pages.length || doc.pages.some(function (page) { return page.needs_review; }); }).length;
    document.getElementById("qApproved").textContent = allDocs.filter(function (doc) { return doc.pages.length && doc.pages.every(function (page) { return page.faculty_verified; }); }).length;
    var finalStat = document.querySelector("#classReview .stats .stat:last-child");
    finalStat.querySelector(".statline").innerHTML = 'Faculty verified <span class="staticon">✓</span>';
    finalStat.querySelector(".statnote").textContent = "Extraction reviewed";
    var bulk = document.getElementById("bulkApprove");
    bulk.disabled = true;
    bulk.hidden = true;
    var notice = document.querySelector("#classReview .notice span:last-child");
    notice.textContent = "This queue is for text extraction and faculty verification only. OCR estimates are not calibrated, unreadable text stays blank, and no marks are generated.";
    var headers = document.querySelectorAll("#classReview .card table thead th");
    ["Roll no.", "Student", "Answer sheet", "Extraction", "Status", "Faculty action"].forEach(function (label, index) {
      if (headers[index]) headers[index].textContent = label;
    });
    document.querySelector("#classReview .card .carddesc").textContent = "Inspect the original file alongside raw extracted text. Faculty corrections are stored separately and never replace the original extraction.";
    document.getElementById("queueRows").innerHTML = list.map(function (student) {
      var doc = documentForRoll(assessment, student.roll);
      return '<tr data-document-row="' + esc(student.roll) + '"><td>' + esc(student.roll) + '</td><td><span class="primarytext">' + esc(student.name) + '</span></td><td>' +
        (doc ? '<button class="btn small" data-open-stored="' + doc.id + '">Open original ↗</button>' : 'No file') +
        '</td><td>' + (doc && doc.pages.length ? doc.pages.length + ' page(s)' : 'No text generated') +
        '</td><td><span class="pill amber">' + esc(statusLabel(doc)) + '</span></td><td>' +
        (doc ? (doc.status === "failed" ? '<button class="btn small" data-retry-extraction="' + doc.id + '">Retry extraction</button> ' : '') + '<button class="btn primary small" data-review-extraction="' + doc.id + '">Review extracted text →</button>' : 'Upload required') + '</td></tr>';
    }).join("") || '<tr><td colspan="6" class="empty">No matched answer sheets.</td></tr>';
    document.querySelectorAll("[data-open-stored]").forEach(function (button) {
      button.addEventListener("click", function () {
        var doc = allDocs.find(function (item) { return item.id === Number(button.dataset.openStored); });
        if (doc) showStoredDocument(doc.id, doc.original_filename);
      });
    });
    document.querySelectorAll("[data-review-extraction]").forEach(function (button) {
      button.addEventListener("click", function () { openExtractionReview(Number(button.dataset.reviewExtraction)); });
    });
    document.querySelectorAll("[data-retry-extraction]").forEach(function (button) {
      button.addEventListener("click", async function () {
        button.disabled = true;
        try {
          var detail = await request("/api/documents/" + button.dataset.retryExtraction + "/process?retry=true", { method: "POST" });
          var item = assessment.documents.find(function (doc) { return doc.id === detail.id; });
          if (item) Object.assign(item, detail);
          renderDocumentQueue(assessment);
        } catch (error) { toast(error.message); button.disabled = false; }
      });
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

  function showStoredDocument(documentId, filename) {
    document.getElementById("pdfTitle").textContent = filename + " · uploaded original";
    document.getElementById("pdfFrame").style.display = "block";
    document.getElementById("pdfFrame").src = "/api/documents/" + documentId + "/file";
    document.getElementById("demoSheet").style.display = "none";
    document.getElementById("pdfDialog").showModal();
  }

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
            if (activeAssessment && activeAssessment._documentWorkflow) {
              var updated = await request("/api/documents?assessment_code=" + encodeURIComponent(activeAssessment.id));
              activeAssessment.documents = updated.records;
              renderDocumentQueue(activeAssessment);
            }
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
      card.innerHTML = '<div class="cardtitle">Uploaded documents &amp; extraction</div><div class="carddesc">Stored under this fictional student and their assessment. Scanned pages without a configured provider remain blank for manual review.</div>' +
        (docs.length ? docs.map(function (doc) {
          return '<article class="assessment-record"><div class="recordtop"><div><strong>' + esc(doc.original_filename) + '</strong><div class="recordmeta">' + esc(doc.document_type.replaceAll("_", " ")) + ' · ' + esc(doc.assessment_code) + '</div></div><span class="pill amber">' + esc(doc.status) + '</span></div><div class="recordmeta">' + esc(doc.status_message) + '</div><button class="btn small" data-student-document="' + doc.id + '">Review extracted pages</button></article>';
        }).join("") : '<div class="report-empty">No documents uploaded for this student.</div>');
      report.appendChild(card);
      card.querySelectorAll("[data-student-document]").forEach(function (button) {
        button.addEventListener("click", function () { openExtractionReview(Number(button.dataset.studentDocument)); });
      });
    } catch (_) {
      // The connected report remains usable if document history is temporarily unavailable.
    }
  };

  window.startSetup = function () {
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
