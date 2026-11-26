/* Faculty feedback review and delivery controls, backed by the local API. */
(function () {
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  async function request(path, options) {
    var response;
    try { response = await fetch(path, options || {}); }
    catch (_) { throw new Error("The local API is not reachable. Start the backend and try again."); }
    var body = null;
    try { body = await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(body && body.detail || "Request failed (" + response.status + ").");
    return body;
  }
  function jsonOptions(method, body) {
    return { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }
  function ensureDialog() {
    if (document.getElementById("feedbackReviewDialog")) return;
    var dialog = document.createElement("dialog");
    dialog.id = "feedbackReviewDialog";
    dialog.className = "modal";
    dialog.innerHTML = '<div class="modalhead"><div><h2 id="feedbackReviewTitle">Feedback review</h2><div class="carddesc">Edit the student-facing message. Approved marks stay fixed.</div></div><button class="btn" id="closeFeedbackReview">Close</button></div><div id="feedbackReviewBody"></div>';
    document.body.appendChild(dialog);
    document.getElementById("closeFeedbackReview").addEventListener("click", function () { dialog.close(); });
    dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });
  }
  function valuesForEditor(content) {
    return '<label>Overall performance summary</label><textarea id="fbOverallSummary" maxlength="2000">' + esc(content.overall_summary) + '</textarea>' +
      '<label>Overall strengths (one per line)</label><textarea id="fbOverallStrengths">' + esc(content.overall_strengths.join("\n")) + '</textarea>' +
      '<label>Areas to improve (one per line)</label><textarea id="fbOverallImprovements">' + esc(content.improvement_areas.join("\n")) + '</textarea>' +
      content.questions.map(function (question) {
        return '<section class="evaluation-question" data-feedback-question="' + esc(question.question_id) + '"><h3>' + esc(question.question_id) + ' · ' + esc(question.prompt) + '</h3><div class="evaluation-meta">Approved score: ' + question.marks + ' / ' + question.maximum_marks + '</div>' +
          '<label>Strengths (one per line)</label><textarea data-feedback-strengths>' + esc(question.strengths.join("\n")) + '</textarea>' +
          '<label>Missing concepts (one per line)</label><textarea data-feedback-missing>' + esc(question.missing_concepts.join("\n")) + '</textarea>' +
          '<label>Improvement suggestions (one per line)</label><textarea data-feedback-suggestions>' + esc(question.suggestions.join("\n")) + '</textarea></section>';
      }).join("") + '<div class="evaluation-actions"><button class="btn" id="saveFeedbackDraft">Save edits</button><button class="btn primary" id="approveFeedback">Approve feedback</button></div>';
  }
  function lines(selector) {
    return document.querySelector(selector).value.split("\n").map(function (line) { return line.trim(); }).filter(Boolean);
  }
  async function reviewFeedback(record) {
    ensureDialog();
    var dialog = document.getElementById("feedbackReviewDialog");
    var body = document.getElementById("feedbackReviewBody");
    document.getElementById("feedbackReviewTitle").textContent = record.student_name + " · " + record.assessment_code + " feedback";
    body.innerHTML = '<div class="evaluation-banner">' + (record.content.demo_result ? '<b>SIMULATED SOURCE SCORE.</b> These marks came from demo placeholders, not answer-quality evaluation.<br>' : '') +
      'Feedback is based on faculty-approved marks. Email delivery will remain simulated unless SMTP is explicitly configured.</div>' +
      '<div class="evaluation-total">Approved marks: ' + record.content.score + ' / ' + record.content.maximum_marks + '</div>' + valuesForEditor(record.content);
    dialog.showModal();
    body.querySelectorAll("#saveFeedbackDraft, #approveFeedback").forEach(function (button) {
      button.addEventListener("click", async function () {
        button.disabled = true;
        try {
          var content = JSON.parse(JSON.stringify(record.content));
          content.overall_summary = document.getElementById("fbOverallSummary").value.trim();
          content.overall_strengths = lines("#fbOverallStrengths");
          content.improvement_areas = lines("#fbOverallImprovements");
          body.querySelectorAll("[data-feedback-question]").forEach(function (section, index) {
            content.questions[index].strengths = section.querySelector("[data-feedback-strengths]").value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
            content.questions[index].missing_concepts = section.querySelector("[data-feedback-missing]").value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
            content.questions[index].suggestions = section.querySelector("[data-feedback-suggestions]").value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
          });
          var saved = record.status === "draft" ? await request("/api/feedback/" + record.id, jsonOptions("PUT", { content: content })) : record;
          if (button.id === "approveFeedback") saved = await request("/api/feedback/" + record.id + "/approve", { method: "POST" });
          else toast("Feedback edits saved as a draft.");
          if (saved.status === "approved") toast("Feedback approved. You can download its report or deliver it.");
          dialog.close();
          if (window.renderStudentPerformance && window.selectedFeedbackStudent) window.renderStudentPerformance(window.selectedFeedbackStudent);
        } catch (error) { toast(error.message); button.disabled = false; }
      });
    });
  }
  function deliveryLabel(record) {
    if (record.delivery_status === "sent") return "Accepted by configured SMTP server";
    if (record.delivery_status === "failed") return "Failed · retry available";
    if (record.delivery_status === "simulated") return "Simulated · email was not sent";
    return record.delivery_attempts ? "Pending" : "Not delivered";
  }
  async function renderFeedbackForStudent(student) {
    window.selectedFeedbackStudent = student;
    var report = document.getElementById("studentReport");
    var old = document.getElementById("personalizedFeedbackHistory");
    if (old) old.remove();
    var card = document.createElement("div");
    card.className = "assessment-history";
    card.id = "personalizedFeedbackHistory";
    card.innerHTML = '<div class="cardtitle">Personalized feedback &amp; delivery</div><div class="carddesc">Feedback can only be prepared from faculty-approved evaluation results. ABC College records are fictional demo data.</div><div class="report-empty" role="status">Loading feedback history…</div>';
    report.appendChild(card);
    try {
      var responses = await Promise.all([
        request("/api/evaluations?student_roll_number=" + encodeURIComponent(student.roll)),
        request("/api/feedback?student_roll_number=" + encodeURIComponent(student.roll))
      ]);
      var feedbackByEvaluation = {};
      responses[1].forEach(function (feedback) { feedbackByEvaluation[feedback.evaluation_id] = feedback; });
      var approved = responses[0].filter(function (evaluation) { return evaluation.status === "approved"; });
      card.innerHTML = '<div class="cardtitle">Personalized feedback &amp; delivery</div><div class="carddesc">Draft and approved feedback is stored per student and assessment. Simulation never sends an email.</div>' +
        (approved.length ? approved.map(function (evaluation) {
          var feedback = feedbackByEvaluation[evaluation.id];
          if (!feedback) return '<article class="assessment-record"><div class="recordtop"><strong>' + esc(evaluation.assessment_code) + '</strong><strong>' + evaluation.final_approved_total + ' / ' + evaluation.total_marks + '</strong></div><div class="recordmeta">Faculty-approved source evaluation</div><button class="btn primary small" data-prepare-feedback="' + evaluation.id + '">Prepare feedback draft</button></article>';
          var controls = feedback.status === "draft" ? '<button class="btn small" data-review-feedback="' + feedback.id + '">Preview and edit</button>' :
            '<button class="btn small" data-download-feedback="' + feedback.id + '">Download PDF report</button><button class="btn primary small" data-deliver-feedback="' + feedback.id + '">' + (feedback.delivery_status === "failed" ? "Retry email" : feedback.delivery_status === "sent" ? "Already sent" : "Deliver feedback") + '</button>';
          return '<article class="assessment-record"><div class="recordtop"><div><strong>' + esc(feedback.assessment_code) + '</strong><div class="recordmeta">' + esc(feedback.status) + (feedback.content.demo_result ? ' · approved demo placeholder marks' : ' · faculty-approved marks') + '</div></div><strong>' + feedback.content.score + ' / ' + feedback.content.maximum_marks + '</strong></div><div class="recordnote"><b>Performance summary</b>' + esc(feedback.content.overall_summary) + '</div><div class="recordmeta"><b>Strengths:</b> ' + esc(feedback.content.overall_strengths.join("; ") || "No strengths recorded") + '</div><div class="recordmeta"><b>Areas to improve:</b> ' + esc(feedback.content.improvement_areas.join("; ") || "No specific areas recorded") + '</div><div class="recordmeta">Email: ' + esc(deliveryLabel(feedback)) + ' · ' + feedback.delivery_attempts + ' attempt(s) · ' + esc(feedback.student_email) + '</div><div class="evaluation-actions">' + controls + (feedback.status === "approved" && feedback.delivery_status === "sent" ? '<span class="recordmeta">Duplicate sending is blocked.</span>' : '') + '</div>' +
            (feedback.delivery_attempts ? '<details><summary>Email delivery history</summary><div data-delivery-history="' + feedback.id + '">Loading attempts…</div></details>' : '') + '</article>';
        }).join("") : '<div class="report-empty">No faculty-approved evaluations are available for feedback.</div>');
      card.querySelectorAll("[data-prepare-feedback]").forEach(function (button) {
        button.addEventListener("click", async function () {
          button.disabled = true;
          try { var feedback = await request("/api/evaluations/" + button.dataset.prepareFeedback + "/feedback", { method: "POST" }); await renderFeedbackForStudent(student); reviewFeedback(feedback); }
          catch (error) { toast(error.message); button.disabled = false; }
        });
      });
      card.querySelectorAll("[data-review-feedback]").forEach(function (button) { button.addEventListener("click", function () { reviewFeedback(feedbackById(responses[1], button.dataset.reviewFeedback)); }); });
      card.querySelectorAll("[data-download-feedback]").forEach(function (button) { button.addEventListener("click", function () { downloadPdf(button.dataset.downloadFeedback); }); });
      card.querySelectorAll("[data-deliver-feedback]").forEach(function (button) { button.addEventListener("click", function () { deliver(button, student); }); });
      card.querySelectorAll("[data-delivery-history]").forEach(function (container) { loadDeliveryHistory(container); });
    } catch (error) { card.innerHTML += '<div class="report-empty" role="alert">' + esc(error.message) + '</div>'; }
  }
  function feedbackById(records, id) { return records.find(function (record) { return String(record.id) === String(id); }); }
  async function downloadPdf(id) {
    try {
      var response = await fetch("/api/feedback/" + id + "/pdf");
      if (!response.ok) { var payload = await response.json(); throw new Error(payload.detail || "PDF download failed."); }
      var blob = await response.blob();
      var link = document.createElement("a");
      link.href = URL.createObjectURL(blob); link.download = "EvalAI_Feedback_Report.pdf"; link.click();
      setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    } catch (error) { toast(error.message); }
  }
  async function deliver(button, student) {
    if (button.textContent === "Already sent") return;
    button.disabled = true;
    try {
      var result = await request("/api/feedback/" + button.dataset.deliverFeedback + "/deliver", { method: "POST" });
      toast(result.delivery_message);
      await renderFeedbackForStudent(student);
    } catch (error) { toast(error.message); button.disabled = false; }
  }
  async function loadDeliveryHistory(container) {
    try {
      var history = await request("/api/feedback/" + container.dataset.deliveryHistory + "/deliveries");
      container.innerHTML = history.length ? history.map(function (item) {
        return '<div class="recordmeta">' + esc(item.status) + ' · ' + esc(item.provider_mode) + ' · ' + esc(new Date(item.attempted_at).toLocaleString()) + ' · ' + esc(item.message) + '</div>';
      }).join("") : 'No attempts yet.';
    } catch (_) { container.textContent = "Delivery history is unavailable."; }
  }
  var previousRender = window.renderStudentPerformance;
  window.renderStudentPerformance = async function (student) {
    await previousRender(student);
    await renderFeedbackForStudent(student);
  };
  window.initFeedbackWorkflow = function () { ensureDialog(); };
})();
