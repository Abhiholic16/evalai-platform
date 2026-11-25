/* Connect the existing single-file prototype to the local FastAPI service. */
(function () {
  var allStudents = null;
  var studentRequestId = 0;
  var assessmentRequestId = 0;
  var localDocumentAssessments = [];
  var searchTimer = null;
  var localRenderStudentPerformance = window.renderStudentPerformance;

  function apiPath(path) {
    // The normal flow serves the page from FastAPI. This fallback supports opening
    // the HTML file directly while the local API is running.
    return window.location.protocol === "file:" ? "http://127.0.0.1:8000" + path : path;
  }

  async function apiGet(path) {
    var response;
    try {
      response = await fetch(apiPath(path), { headers: { Accept: "application/json" } });
    } catch (_) {
      throw new Error("Could not reach the EvalAI API. Start the backend, then reload this page.");
    }
    if (!response.ok) {
      var message = "The API returned an error (" + response.status + ").";
      try {
        var body = await response.json();
        if (body.detail) message = typeof body.detail === "string" ? body.detail : message;
      } catch (_) {}
      throw new Error(message);
    }
    return response.json();
  }

  function queryString(values) {
    var params = new URLSearchParams();
    Object.keys(values).forEach(function (key) {
      if (values[key] && values[key] !== "all") params.set(key, values[key]);
    });
    return params.toString();
  }

  function toStudent(row) {
    return {
      id: row.id, roll: row.roll_number, name: row.name, cls: row.class_code,
      section: row.section, email: row.email, program: row.program,
      academicYear: row.academic_year, year: row.year, admissionYear: row.admission_year
    };
  }

  async function getAllStudents() {
    if (!allStudents) {
      allStudents = apiGet("/api/students?limit=200").then(function (payload) {
        return payload.records.map(toStudent);
      }).catch(function (error) {
        allStudents = null;
        throw error;
      });
    }
    return allStudents;
  }

  window.fetchCollegeStudentRecords = async function () {
    var records = await getAllStudents();
    students = records.slice();
    return { source: "fictional ABC College SQLite demo data", records: records };
  };

  function assessmentParams(prefix) {
    var dash = prefix === "dash";
    var cls = document.getElementById(prefix + "Class").value;
    var section = document.getElementById(prefix + "Section").value;
    var subject = document.getElementById(prefix + "Subject").value;
    var test = document.getElementById(prefix + "Test").value;
    var params = { class: cls, section: section, subject: subject };
    if (test !== "FALL2026") params.test = test;
    return params;
  }

  function resetResultsFromApi(rows) {
    rows.forEach(function (row) {
      var key = row.assessment_code + "|" + row.student_roll_number;
      var approvedByFaculty = typeof row.review_status === "string" && row.review_status.indexOf("Faculty approved") === 0;
      var rubricSource = row.review_status === "Faculty approved OpenAI evaluation" ?
        "faculty-approved OpenAI rubric evaluation" :
        (row.review_status === "Faculty approved demo evaluation" ? "faculty-approved demo rubric evaluation" : "SQLite demo record");
      if (!results[key]) {
        results[key] = {
          score: row.score, max: row.max_score, status: approvedByFaculty ? "Approved" : "Needs review", flag: false,
          source: rubricSource, reviewStatus: row.review_status
        };
      } else {
        // Keep this page session's faculty review edits while refreshing source marks.
        if (approvedByFaculty || (results[key].source === "SQLite demo record" && results[key].status !== "Approved")) {
          results[key].score = row.score;
          results[key].max = row.max_score;
          if (approvedByFaculty) {
            results[key].status = "Approved";
            results[key].source = rubricSource;
            results[key].reviewStatus = row.review_status;
          }
        }
      }
    });
  }

  window.renderAssessments = async function () {
    var requestId = ++assessmentRequestId;
    var table = document.getElementById("assessmentRows");
    table.innerHTML = '<tr><td colspan="6" class="empty" role="status">Loading assessments from ABC College demo database…</td></tr>';
    try {
      var query = queryString(assessmentParams("dash"));
      var responses = await Promise.all([
        getAllStudents(), apiGet("/api/assessments" + (query ? "?" + query : "")),
        apiGet("/api/assessment-workspaces"), apiGet("/api/documents")
      ]);
      if (requestId !== assessmentRequestId) return;
      students = responses[0].slice();
      var roster = students;
      var rows = responses[1].records;
      resetResultsFromApi(rows);
      var grouped = {};
      rows.forEach(function (row) {
        var id = row.assessment_code;
        if (!grouped[id]) {
          grouped[id] = {
            id: id, title: row.title, cls: row.class_code, section: row.section,
            subject: row.subject_code, test: row.test_code, term: row.term, date: row.date,
            submitted: 0, total: roster.filter(function (s) {
              return s.cls === row.class_code && s.section === row.section;
            }).length, rolls: []
          };
        }
        grouped[id].submitted += 1;
        grouped[id].rolls.push(row.student_roll_number);
      });
      assessments = Object.keys(grouped).map(function (id) {
        var assessment = grouped[id];
        assessment.status = assessment.rolls.every(function (roll) {
          return results[id + "|" + roll] && results[id + "|" + roll].status === "Approved";
        }) ? "Feedback ready" : "Review in progress";
        return assessment;
      });
      localDocumentAssessments = responses[2].filter(function (workspace) {
        return !grouped[workspace.assessment_code];
      }).map(function (workspace) {
        var documents = responses[3].records.filter(function (document) {
          return document.assessment_code === workspace.assessment_code;
        });
        var rolls = documents.filter(function (document) { return document.document_type === "answer_sheet"; })
          .map(function (document) { return document.student_roll_number; }).filter(Boolean);
        return {
          id: workspace.assessment_code, title: workspace.title, cls: workspace.class_code,
          section: workspace.section, subject: workspace.subject_code, test: workspace.test_code,
          term: "Fall 2026", date: workspace.test_date || "Date not set", submitted: rolls.length,
          total: roster.filter(function (s) { return s.cls === workspace.class_code && s.section === workspace.section; }).length,
          status: documents.length ? "Documents uploaded" : "Setup saved", rolls: rolls,
          _documentWorkflow: true, documents: documents
        };
      });
      var dashboardFilters = {
        cls: document.getElementById("dashClass").value, section: document.getElementById("dashSection").value,
        subject: document.getElementById("dashSubject").value, test: document.getElementById("dashTest").value
      };
      var localRows = localDocumentAssessments.filter(function (item) {
        return (dashboardFilters.cls === "all" || dashboardFilters.cls === item.cls) &&
          (dashboardFilters.section === "all" || dashboardFilters.section === item.section) &&
          (dashboardFilters.subject === "all" || dashboardFilters.subject === item.subject) &&
          (dashboardFilters.test === "all" || dashboardFilters.test === item.test);
      });
      assessments = assessments.concat(localRows);
      var list = assessments;
      table.innerHTML = list.length ? list.map(function (assessment) {
        return '<tr><td><span class="primarytext">' + assessment.title + '</span><div class="secondary">' + (tests[assessment.test] || assessment.test) + ' · ' + assessment.date + '</div></td><td>' + (classes[assessment.cls] || assessment.cls) + ' · Section ' + assessment.section + '</td><td>' + (subjects[assessment.subject] || assessment.subject) + '</td><td>' + assessment.submitted + ' of ' + assessment.total + ' students</td><td><span class="pill ' + (assessment.status === "Feedback ready" ? "green" : "amber") + '">' + assessment.status + '</span></td><td><button class="btn primary small" data-open="' + assessment.id + '">Open test review →</button></td></tr>';
      }).join("") : '<tr><td colspan="6" class="empty">No assessments match these filters.</td></tr>';
      document.getElementById("statAssessments").textContent = list.length;
      document.getElementById("statSubmissions").textContent = list.reduce(function (sum, item) { return sum + item.submitted; }, 0);
      document.getElementById("statReview").textContent = list.filter(function (item) { return item.status === "Review in progress"; }).reduce(function (sum, item) { return sum + item.submitted; }, 0);
      document.getElementById("statFeedback").textContent = list.filter(function (item) { return item.status === "Feedback ready"; }).reduce(function (sum, item) { return sum + item.submitted; }, 0);
      table.querySelectorAll("[data-open]").forEach(function (button) {
        button.addEventListener("click", function () { openAssessment(button.dataset.open); });
      });
    } catch (error) {
      if (requestId !== assessmentRequestId) return;
      table.innerHTML = '<tr><td colspan="6" class="empty" role="alert">' + error.message + ' Start the backend and use its local page address.</td></tr>';
      ["statAssessments", "statSubmissions", "statReview", "statFeedback"].forEach(function (id) {
        document.getElementById(id).textContent = "—";
      });
    }
  };

  window.renderPerformance = function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadPerformance, 180);
  };

  async function loadPerformance() {
    var requestId = ++studentRequestId;
    var lookup = document.getElementById("studentLookup");
    var search = document.getElementById("studentSearch").value.trim();
    lookup.innerHTML = '<div class="report-empty" role="status">Loading student records…</div>';
    document.getElementById("studentReport").innerHTML = '<div class="report-empty" role="status">Loading student assessment history…</div>';
    document.getElementById("studentCountTag").textContent = "…";
    try {
      var params = assessmentParams("fb");
      params.search = search;
      var query = queryString(params);
      var payload = await apiGet("/api/students" + (query ? "?" + query : ""));
      if (requestId !== studentRequestId) return;
      var filteredStudents = payload.records.map(toStudent);
      document.getElementById("studentCountTag").textContent = payload.count + " STUDENTS";
      if (!filteredStudents.some(function (student) { return student.roll === selectedStudentRoll; })) {
        selectedStudentRoll = filteredStudents.length ? filteredStudents[0].roll : null;
      }
      lookup.innerHTML = filteredStudents.length ? filteredStudents.map(function (student) {
        return '<div class="lookup-item ' + (student.roll === selectedStudentRoll ? "selected" : "") + '"><div><span class="primarytext">' + student.name + '</span><div class="secondary">Roll ' + student.roll + ' · Section ' + student.section + '</div></div><button class="btn small" data-select-student="' + student.roll + '">Open report →</button></div>';
      }).join("") : '<div class="report-empty">No students match these filters.</div>';
      lookup.querySelectorAll("[data-select-student]").forEach(function (button) {
        button.addEventListener("click", function () {
          selectedStudentRoll = button.dataset.selectStudent;
          renderPerformance();
        });
      });
      var selected = filteredStudents.find(function (student) { return student.roll === selectedStudentRoll; });
      if (selected) await window.renderStudentPerformance(selected);
      else document.getElementById("studentReport").innerHTML = '<div class="report-empty">No student records match this search and filter combination.</div>';
    } catch (error) {
      if (requestId !== studentRequestId) return;
      document.getElementById("studentCountTag").textContent = "0 STUDENTS";
      lookup.innerHTML = '<div class="report-empty" role="alert">' + error.message + ' Start the backend, then reload the page.</div>';
      document.getElementById("studentReport").innerHTML = '<div class="report-empty">Student performance is unavailable until the API reconnects.</div>';
    }
  }

  window.renderStudentPerformance = async function (student) {
    var report = document.getElementById("studentReport");
    report.innerHTML = '<div class="report-empty" role="status">Loading assessment history for ' + student.name + '…</div>';
    try {
      var detail = await apiGet("/api/students/" + encodeURIComponent(student.roll));
      var current = toStudent(detail);
      current.assessments = detail.assessments;
      var requestRows = detail.assessments.map(function (row) {
        var key = row.assessment_code + "|" + current.roll;
        if (!results[key]) results[key] = { score: row.score, max: row.max_score, status: "Needs review", flag: false, source: "SQLite demo record" };
        return row;
      });
      current.assessments = requestRows;
      localRenderStudentPerformance(current);
    } catch (error) {
      report.innerHTML = '<div class="report-empty" role="alert">Could not load this student’s assessment history. ' + error.message + '</div>';
    }
  };

  window.performanceRowsFor = function (student) {
    return (student.assessments || []).filter(function (row) {
      var subject = document.getElementById("fbSubject").value;
      var test = document.getElementById("fbTest").value;
      return (subject === "all" || row.subject_code === subject) &&
        (test === "all" || test === "FALL2026" || row.test_code === test);
    }).map(function (row) {
      var assessment = {
        id: row.assessment_code, title: row.title, cls: row.class_code,
        section: row.section, subject: row.subject_code, test: row.test_code,
        term: row.term, date: row.date, rolls: [student.roll]
      };
      var key = assessment.id + "|" + student.roll;
      if (!results[key]) results[key] = { score: row.score, max: row.max_score, status: row.review_status && row.review_status.indexOf("Faculty approved") === 0 ? "Approved" : "Needs review", flag: false, source: row.review_status === "Faculty approved OpenAI evaluation" ? "faculty-approved OpenAI rubric evaluation" : (row.review_status === "Faculty approved demo evaluation" ? "faculty-approved demo rubric evaluation" : "SQLite demo record"), reviewStatus: row.review_status };
      if (results[key].source === "SQLite demo record" && results[key].status !== "Approved") {
        results[key].score = row.score;
        results[key].max = row.max_score;
      }
      var review = ensureFeedbackData(assessment, student, results[key]);
      if (review.source === "faculty-approved OpenAI rubric evaluation" || review.source === "faculty-approved demo rubric evaluation") {
        review.aiFeedback = "Question-level scoring rationale is shown in the rubric evaluation review. Personalized learner coaching has not been generated.";
        review.strengths = [];
        review.improvements = [];
        review.questionFeedback = [];
        review.approvedFeedback = "Faculty approved the rubric score. Personalized learner coaching has not been generated.";
        review.emailStatus = "Not sent";
      }
      return { a: assessment, r: review, key: key };
    }).sort(function (left, right) { return new Date(left.a.date) - new Date(right.a.date); });
  };
})();
