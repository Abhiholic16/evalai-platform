/* Question-level rubric evaluation and faculty decision workflow for the existing prototype. */
(function () {
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

  function criteriaFromCard(card) {
    return card.querySelector(".question-rubric").value.split(/[\n;]/).map(function (item) {
      return item.trim();
    }).filter(Boolean);
  }

  function getEvaluationQuestions() {
    return Array.prototype.map.call(document.querySelectorAll("[data-assessment-question]"), function (card, index) {
      return {
        question_id: card.dataset.questionId,
        position: index + 1,
        prompt: card.querySelector(".question-prompt").value.trim(),
        model_answer: card.querySelector(".question-model-answer").value.trim(),
        maximum_marks: Number(card.querySelector(".question-maximum").value) || 0,
        rubric_criteria: criteriaFromCard(card)
      };
    }).filter(function (question) { return question.prompt.length > 0; });
  }

  window.setEvaluationQuestions = function (questions) {
    var host = document.getElementById("assessmentQuestionConfig");
    if (!host) return;
    host.replaceChildren();
    (questions || []).forEach(function (question, index) {
      addQuestionCard(question.question_id || "Q" + (index + 1), question);
    });
    if (!questions || !questions.length) addQuestionCard("Q1", { maximum_marks: 10 });
    updateAssessmentTotal();
  };

  function getEvaluationTotalMarks() {
    return getEvaluationQuestions().reduce(function (total, question) { return total + question.maximum_marks; }, 0);
  }

  window.getEvaluationQuestions = getEvaluationQuestions;
  window.getEvaluationTotalMarks = getEvaluationTotalMarks;

  function updateAssessmentTotal() {
    var target = document.getElementById("assessmentTotalMarks");
    if (target) target.textContent = "Assessment total: " + getEvaluationTotalMarks() + " marks";
  }

  function addQuestionCard(questionId, values) {
    values = values || {};
    var card = document.createElement("section");
    card.className = "question-config";
    card.dataset.assessmentQuestion = "true";
    card.dataset.questionId = questionId;
    card.innerHTML = '<div class="recordtop"><h3>Question ' + esc(questionId) + '</h3>' +
      '<button type="button" class="btn small" data-remove-assessment-question>Remove</button></div>' +
      '<label>Question text</label><textarea class="question-prompt" maxlength="2000" placeholder="Enter the question exactly as students receive it">' + esc(values.prompt || "") + '</textarea>' +
      '<label>Model answer / expected concepts</label><textarea class="question-model-answer" maxlength="8000" placeholder="Enter a faculty-approved answer or key concepts">' + esc(values.model_answer || "") + '</textarea>' +
      '<div class="qgrid"><div><label>Maximum marks</label><input class="question-maximum" type="number" min="1" max="500" step="1" value="' + esc(values.maximum_marks || 20) + '"></div><div class="extractmeta" style="align-self:end">Question maximum</div></div>' +
      '<label>Question-specific marking criteria (one per line)</label><textarea class="question-rubric" maxlength="3000" placeholder="For example: identifies the key concept; explains the process; uses relevant evidence">' + esc((values.rubric_criteria || []).join("\n")) + '</textarea>';
    document.getElementById("assessmentQuestionConfig").appendChild(card);
    card.querySelectorAll("input, textarea").forEach(function (input) {
      input.addEventListener("input", updateAssessmentTotal);
    });
    card.querySelector("[data-remove-assessment-question]").addEventListener("click", function () {
      if (document.querySelectorAll("[data-assessment-question]").length <= 1) {
        toast("Keep at least one question in the assessment.");
        return;
      }
      card.remove();
      updateAssessmentTotal();
    });
    updateAssessmentTotal();
  }

  function resetQuestionEditor() {
    document.getElementById("assessmentQuestionConfig").innerHTML = "";
    addQuestionCard("Q1", { maximum_marks: 20 });
  }

  function nextQuestionId() {
    var maximum = 0;
    document.querySelectorAll("[data-assessment-question]").forEach(function (card) {
      var match = /^Q(\d+)$/i.exec(card.dataset.questionId || "");
      if (match) maximum = Math.max(maximum, Number(match[1]));
    });
    return "Q" + (maximum + 1);
  }

  function validateQuestionEditor(event) {
    var questions = getEvaluationQuestions();
    var error = questions.some(function (question) {
      return question.prompt.length < 3 || question.maximum_marks < 1;
    }) ? "Complete the question text and maximum marks for each manually added question." : null;
    if (error) {
      event.preventDefault();
      event.stopImmediatePropagation();
      var status = document.getElementById("assessmentConfigStatus");
      status.textContent = error;
      status.setAttribute("role", "alert");
      status.style.color = "#a74141";
      status.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function latestEvaluationForDocument(assessment, documentId) {
    return (assessment.evaluations || []).find(function (evaluation) { return evaluation.document_id === documentId; }) || null;
  }

  function evaluationActionText(evaluation) {
    if (!evaluation) return "Evaluate answer sheet";
    if (evaluation.status === "approved") return "View faculty-approved marks";
    if (evaluation.status === "rejected") return "Evaluate answer sheet again";
    if (evaluation.status === "needs_manual_review") return "Evaluate answer sheet again";
    return "Review provisional recommendation";
  }

  window.renderEvaluationActions = async function (assessment, documents) {
    if (!assessment._evaluationLoaded && !assessment.reviewPayload) {
      if (assessment._evaluationLoading) return;
      assessment._evaluationLoading = true;
      try {
        var payload = await request("/api/evaluations?assessment_code=" + encodeURIComponent(assessment.id));
        assessment.evaluations = payload;
        assessment._evaluationLoaded = true;
        assessment._evaluationLoading = false;
        if (window.renderQueue) window.renderQueue();
      } catch (error) {
        assessment._evaluationLoading = false;
        toast(error.message);
      }
      return;
    }

    if (assessment.reviewPayload) {
      assessment.evaluations = assessment.reviewPayload.submissions.map(function (item) { return item.evaluation; }).filter(Boolean);
      assessment._evaluationLoaded = true;
    }
    function eligible(evaluation, doc) {
      return evaluation && ["provisional", "edited"].includes(evaluation.status) &&
        (!doc.job || doc.job.state === "complete") &&
        evaluation.ai_proposed.every(function (result) { return !(result.uncertainty_flags || []).length; }) &&
        (evaluation.faculty_edited || evaluation.ai_proposed).every(function (result) { return result.proposed_marks != null; });
    }
    var candidates = documents.map(function (doc) { return { doc: doc, evaluation: latestEvaluationForDocument(assessment, doc.id) }; }).filter(function (item) { return eligible(item.evaluation, item.doc); });
    var header = document.querySelector("#classReview .cardhead");
    var bulk = document.getElementById("approveEligibleSubmissions");
    if (!bulk && header) {
      bulk = document.createElement("button"); bulk.id = "approveEligibleSubmissions"; bulk.className = "btn primary"; header.appendChild(bulk);
    }
    if (bulk) {
      bulk.textContent = "Approve all eligible (" + candidates.length + ")";
      bulk.disabled = !candidates.length;
      bulk.onclick = async function () {
        bulk.disabled = true;
        var approved = 0, failed = 0;
        for (var item of candidates) {
          try { await request("/api/evaluations/" + item.evaluation.id + "/approve", { method: "POST" }); approved++; }
          catch (_) { failed++; }
        }
        toast(approved + " approved. Entries needing attention were skipped." + (failed ? " " + failed + " could not be approved." : ""));
        await window.openAssessment(assessment.id, true);
      };
    }
    documents.forEach(function (documentRecord) {
      if (documentRecord.document_type !== "answer_sheet" || !documentRecord.student_roll_number) return;
      var row = Array.prototype.find.call(document.querySelectorAll("[data-document-row]"), function (candidate) {
        return candidate.dataset.documentRow === documentRecord.student_roll_number;
      });
      if (!row || row.dataset.evaluationActionAdded === "true") return;
      row.dataset.evaluationActionAdded = "true";
      var evaluation = latestEvaluationForDocument(assessment, documentRecord.id);
      if (!evaluation && documentRecord.job && ["queued", "running"].includes(documentRecord.job.state)) return;
      var actionCell = row.cells[5];
      var status = evaluation ? evaluation.status.replaceAll("_", " ") : "Not evaluated";
      if (evaluation && evaluation.status === "approved") {
        var badge = row.cells[4].querySelector(".pill");
        if (badge) { badge.textContent = "Approved"; badge.className = "pill green"; }
      }
      var total = evaluation ? (evaluation.final_approved_total != null ? evaluation.final_approved_total : evaluation.faculty_edited_total != null ? evaluation.faculty_edited_total : evaluation.ai_proposed_total) + " / " + evaluation.total_marks : "";
      var flags = evaluation ? Array.from(new Set(evaluation.ai_proposed.reduce(function (all, result) { return all.concat(result.uncertainty_flags || []); }, []))) : [];
      if (!evaluation) {
        if (documentRecord.job && documentRecord.job.state === "needs_attention") return;
        actionCell.insertAdjacentHTML("beforeend", '<div class="recordmeta" style="margin:6px 0">Evaluation will start automatically.</div>');
        return;
      }
      actionCell.insertAdjacentHTML("beforeend", '<div class="recordmeta" style="margin:6px 0">' + esc(status) + (total ? " · " + esc(total) : "") + '</div>' + (flags.length ? '<div class="extractwarning">⚑ ' + esc(flags.join("; ")) + '</div>' : '') + '<button class="btn small" data-evaluation-action>' + esc(evaluationActionText(evaluation)) + '</button>');
      actionCell.querySelector("[data-evaluation-action]").addEventListener("click", async function (buttonEvent) {
        var button = buttonEvent.currentTarget;
        button.disabled = true;
        var priorText = button.textContent;
          button.textContent = evaluation && evaluation.status !== "rejected" ? "Loading..." : "Reading and evaluating sheet...";
        try {
          var mustEvaluate = !evaluation || evaluation.status === "rejected" || evaluation.status === "needs_manual_review";
          if (mustEvaluate && documentRecord.job) {
            await request("/api/documents/" + documentRecord.id + "/process?retry=true", { method: "POST" });
            await window.openAssessment(assessment.id);
            return;
          }
          var result = !mustEvaluate ?
            await request("/api/evaluations/" + evaluation.id) :
            await request("/api/assessments/" + encodeURIComponent(assessment.id) + "/students/" + encodeURIComponent(documentRecord.student_roll_number) + "/evaluations", { method: "POST" });
          assessment._evaluationLoaded = false;
          if (window.renderQueue) window.renderQueue();
          openEvaluationReview(result.id);
        } catch (error) {
          toast(error.message);
          button.disabled = false;
          button.textContent = priorText;
        }
      });
      if (eligible(evaluation, documentRecord)) {
        var approve = document.createElement("button");
        approve.className = "btn small primary"; approve.textContent = "Approve"; approve.style.marginLeft = "8px";
        approve.onclick = async function () {
          approve.disabled = true;
          try {
            await request("/api/evaluations/" + evaluation.id + "/approve", { method: "POST" });
            toast("Marks approved."); await window.openAssessment(assessment.id, true);
          } catch (error) { approve.disabled = false; toast(error.message); }
        };
        actionCell.appendChild(approve);
      }
    });
  };

  function resultTotal(items) {
    return (items || []).reduce(function (sum, item) { return sum + (item.proposed_marks == null ? 0 : item.proposed_marks); }, 0);
  }

  function lines(value) { return (value || []).join("\n"); }
  function listFromText(value) { return value.split("\n").map(function (item) { return item.trim(); }).filter(Boolean); }

  async function openEvaluationReview(evaluationId) {
    var dialog = document.getElementById("evaluationDialog");
    var body = document.getElementById("evaluationBody");
    body.innerHTML = '<div class="report-empty" role="status">Loading evaluation record...</div>';
    document.getElementById("evaluationTitle").textContent = "Rubric evaluation review";
    dialog.showModal();
    try {
      var evaluation = await request("/api/evaluations/" + evaluationId);
      renderEvaluationReview(evaluation);
    } catch (error) {
      body.innerHTML = '<div class="report-empty" role="alert">' + esc(error.message) + '</div>';
    }
  }

  function renderEvaluationReview(evaluation) {
    var body = document.getElementById("evaluationBody");
    var isEditable = evaluation.status === "provisional" || evaluation.status === "edited";
    var selected = evaluation.status === "approved" ? evaluation.final_approved : (evaluation.faculty_edited || evaluation.ai_proposed);
    var modeLabel = evaluation.provider_mode === "gemini" ? "Gemini model-generated rubric recommendation" :
      evaluation.provider_mode === "openai" ? "Previously stored OpenAI rubric recommendation" :
      evaluation.provider_mode === "demo_simulation" ? "Simulated marks; answer quality was not evaluated" : "Manual review required; no model call was made";
    var aiTotal = evaluation.ai_proposed_total;
    var currentTotal = evaluation.status === "approved" ? evaluation.final_approved_total :
      (evaluation.faculty_edited_total == null ? null : evaluation.faculty_edited_total);
    document.getElementById("evaluationTitle").textContent = evaluation.student_name + " · " + evaluation.assessment_code + " · " + evaluation.status.replaceAll("_", " ");
    var banner = '<div class="evaluation-banner"><b>' + esc(modeLabel) + '</b><br>' + esc(evaluation.status_message) + '<br>All proposed marks are recommendations. Faculty approval is required before a score is recorded as official.</div>';
    var totals = '<div class="evaluation-total">AI proposed: ' + aiTotal + ' / ' + evaluation.total_marks +
      (currentTotal == null ? '' : ' &nbsp;|&nbsp; Faculty-edited: ' + currentTotal + ' / ' + evaluation.total_marks) +
      (evaluation.final_approved_total == null ? '' : ' &nbsp;|&nbsp; Faculty-approved: ' + evaluation.final_approved_total + ' / ' + evaluation.total_marks) + '</div>';
    var questionsHtml = evaluation.question_snapshot.map(function (question) {
      var ai = evaluation.ai_proposed.find(function (item) { return item.question_id === question.question_id; });
      var current = selected.find(function (item) { return item.question_id === question.question_id; }) || ai;
      var sources = evaluation.answer_sources.filter(function (source) { return source.question_id === question.question_id || source.question_id == null; });
      var sourceHtml = sources.length ? sources.map(function (source) {
        return '<div class="evaluation-meta">Answer source: original answer sheet, page ' + source.page_number +
          ' · <button class="btn small" data-eval-source="' + source.document_id + '">Open original</button></div>';
      }).join("") : '<div class="extractwarning">No readable answer was identified for this question. Faculty review is required.</div>';
      return '<section class="evaluation-question" data-evaluation-question="' + esc(question.question_id) + '"><h3>' + esc(question.question_id) + ' · ' + esc(question.prompt) + '</h3>' +
        '<div class="evaluation-meta">Question maximum: ' + question.maximum_marks + ' marks · ' + esc(question.rubric_criteria.join("; ")) + '</div>' + sourceHtml +
        '<div class="evaluation-points">AI proposed: ' + (ai.proposed_marks == null ? 'Not scored' : ai.proposed_marks + ' / ' + ai.maximum_marks) + '</div>' +
        (evaluation.faculty_edited && evaluation.status !== "approved" ? '<div class="evaluation-meta">Current faculty-edited marks: ' + (current.proposed_marks == null ? 'Not scored' : current.proposed_marks + ' / ' + current.maximum_marks) + '</div>' : '') +
        (evaluation.final_approved ? '<div class="evaluation-meta">Faculty-approved final marks: ' + (evaluation.final_approved.find(function (item) { return item.question_id === question.question_id; }).proposed_marks) + ' / ' + question.maximum_marks + '</div>' : '') +
        '<label>Proposed marks</label><input data-eval-marks type="number" min="0" max="' + question.maximum_marks + '" step="1" value="' + (current.proposed_marks == null ? '' : current.proposed_marks) + '" ' + (!isEditable || current.proposed_marks == null ? 'disabled' : '') + '>' +
        '<label>Score rationale</label><textarea data-eval-reasoning ' + (!isEditable ? 'disabled' : '') + '>' + esc(current.reasoning) + '</textarea>' +
        '<label>Strengths (one per line)</label><textarea data-eval-strengths ' + (!isEditable ? 'disabled' : '') + '>' + esc(lines(current.strengths)) + '</textarea>' +
        '<label>Improvement areas (one per line)</label><textarea data-eval-improvements ' + (!isEditable ? 'disabled' : '') + '>' + esc(lines(current.improvement_areas)) + '</textarea>' +
        '<label>Uncertainty flags (one per line)</label><textarea data-eval-flags ' + (!isEditable ? 'disabled' : '') + '>' + esc(lines(current.uncertainty_flags)) + '</textarea>' +
        (evaluation.status === "edited" && ai ? '<details><summary>Original AI rationale and flags</summary><div class="evaluation-meta">' + esc(ai.reasoning) + '</div><div class="evaluation-meta">Strengths: ' + esc(lines(ai.strengths)) + '</div><div class="evaluation-meta">Improvement areas: ' + esc(lines(ai.improvement_areas)) + '</div></details>' : '') +
        '</section>';
    }).join("");
    var decision = evaluation.rejection_reason ? '<div class="extractwarning">Faculty rejection reason: ' + esc(evaluation.rejection_reason) + '</div>' : '';
    var controls = isEditable ? '<div class="extractintro">Save edits before approving. The original model proposal remains stored unchanged. Approval records the final marks as official; rejection leaves them unofficial.</div>' +
      '<div class="evaluation-actions"><button class="btn" id="saveEvaluationEdits">Save faculty edits</button><button class="btn" id="rejectEvaluation">Reject evaluation</button><button class="btn primary" id="approveEvaluation">Approve final marks</button></div><label for="evaluationRejectReason">If rejecting, enter a short reason</label><textarea id="evaluationRejectReason" maxlength="1000" placeholder="Explain what needs to be corrected"></textarea>' : '';
    body.innerHTML = banner + totals + decision + questionsHtml + controls;
    body.querySelectorAll("[data-eval-source]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (window.openStoredAnswerDocument) window.openStoredAnswerDocument(Number(button.dataset.evalSource), "Source answer sheet");
      });
    });
    body.querySelectorAll("[data-eval-marks], [data-eval-reasoning], [data-eval-strengths], [data-eval-improvements], [data-eval-flags]").forEach(function (input) {
      input.addEventListener("input", function () { body.dataset.editsDirty = "true"; });
    });
    var save = document.getElementById("saveEvaluationEdits");
    if (save) save.addEventListener("click", async function () {
      save.disabled = true;
      try {
        var questionResults = Array.prototype.map.call(body.querySelectorAll("[data-evaluation-question]"), function (section) {
          var mark = section.querySelector("[data-eval-marks]").value;
          var ai = evaluation.ai_proposed.find(function (item) { return item.question_id === section.dataset.evaluationQuestion; });
          return {
            question_id: ai.question_id, maximum_marks: ai.maximum_marks,
            proposed_marks: mark === "" ? null : Number(mark),
            reasoning: section.querySelector("[data-eval-reasoning]").value.trim() || "Faculty-edited evaluation.",
            strengths: listFromText(section.querySelector("[data-eval-strengths]").value),
            improvement_areas: listFromText(section.querySelector("[data-eval-improvements]").value),
            uncertainty_flags: listFromText(section.querySelector("[data-eval-flags]").value),
            requires_faculty_review: true
          };
        });
        await request("/api/evaluations/" + evaluation.id + "/edits", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question_results: questionResults })
        });
        toast("Faculty edits saved separately from the AI proposal.");
        openEvaluationReview(evaluation.id);
      } catch (error) { toast(error.message); save.disabled = false; }
    });
    var approve = document.getElementById("approveEvaluation");
    if (approve) approve.addEventListener("click", async function () {
      if (body.dataset.editsDirty === "true") {
        toast("Save your faculty edits before approving the marks.");
        return;
      }
      approve.disabled = true;
      try {
        var result = await request("/api/evaluations/" + evaluation.id + "/approve", { method: "POST" });
        toast("Faculty approved " + result.final_approved_total + " / " + result.total_marks + ". The mark is now official.");
        refreshQueueAfterDecision(evaluation.assessment_code);
        openEvaluationReview(evaluation.id);
      } catch (error) { toast(error.message); approve.disabled = false; }
    });
    var reject = document.getElementById("rejectEvaluation");
    if (reject) reject.addEventListener("click", async function () {
      reject.disabled = true;
      try {
        var reason = document.getElementById("evaluationRejectReason").value.trim();
        if (reason.length < 3) throw new Error("Enter a short rejection reason first.");
        await request("/api/evaluations/" + evaluation.id + "/reject", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: reason })
        });
        toast("Evaluation rejected. It will not be recorded as an official mark.");
        refreshQueueAfterDecision(evaluation.assessment_code);
        openEvaluationReview(evaluation.id);
      } catch (error) { toast(error.message); reject.disabled = false; }
    });
  }

  function refreshQueueAfterDecision(assessmentCode) {
    if (window.getDocumentWorkflowAssessment) {
      var assessment = window.getDocumentWorkflowAssessment();
      if (assessment && assessment.id === assessmentCode) {
        assessment._evaluationLoaded = false;
        assessment._evaluationLoading = false;
        if (window.renderQueue) window.renderQueue();
      }
    }
  }

  window.initEvaluationWorkflow = function () {
    resetQuestionEditor();
    document.getElementById("addAssessmentQuestion").addEventListener("click", function () {
      addQuestionCard(nextQuestionId(), { maximum_marks: 10 });
    });
    document.getElementById("saveSetup").addEventListener("click", validateQuestionEditor, true);
    document.getElementById("closeEvaluation").addEventListener("click", function () {
      document.getElementById("evaluationDialog").close();
    });
    document.getElementById("evaluationDialog").addEventListener("click", function (event) {
      if (event.target === this) this.close();
    });
    var originalStartSetup = window.startSetup;
    window.startSetup = function () {
      resetQuestionEditor();
      originalStartSetup();
    };
  };

  var originalStudentPerformance = window.renderStudentPerformance;
  window.renderStudentPerformance = async function (student) {
    await originalStudentPerformance(student);
    try {
      var payload = await request("/api/evaluations?student_roll_number=" + encodeURIComponent(student.roll));
      var report = document.getElementById("studentReport");
      var existing = document.getElementById("rubricEvaluationHistory");
      if (existing) existing.remove();
      var card = document.createElement("div");
      card.className = "assessment-history";
      card.id = "rubricEvaluationHistory";
      card.innerHTML = '<div class="cardtitle">Rubric evaluation history</div><div class="carddesc">AI scores remain provisional until faculty approval. Simulated marks are not content-evaluated.</div>' +
        (payload.length ? payload.map(function (evaluation) {
          var total = evaluation.final_approved_total != null ? evaluation.final_approved_total : evaluation.ai_proposed_total;
          var totalLabel = evaluation.status === "approved" ? "Faculty-approved official mark" : "Provisional recommendation";
          return '<article class="assessment-record"><div class="recordtop"><div><strong>' + esc(evaluation.assessment_code) + ' · ' + esc(evaluation.status.replaceAll("_", " ")) + '</strong><div class="recordmeta">' + esc(evaluation.provider_mode) + (evaluation.model_name ? ' · ' + esc(evaluation.model_name) : '') + '</div></div><strong>' + total + ' / ' + evaluation.total_marks + '</strong></div><div class="recordmeta">' + totalLabel + '</div><button class="btn small" data-history-evaluation="' + evaluation.id + '">Review question results</button></article>';
        }).join("") : '<div class="report-empty">No rubric evaluations have been created for this student.</div>');
      report.appendChild(card);
      card.querySelectorAll("[data-history-evaluation]").forEach(function (button) {
        button.addEventListener("click", function () { openEvaluationReview(Number(button.dataset.historyEvaluation)); });
      });
    } catch (_) {
      // Existing performance history remains available when evaluation history cannot load.
    }
  };
})();
