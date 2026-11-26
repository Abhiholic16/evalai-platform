/* Connect the existing single-file prototype to the local FastAPI service. */
(function () {
  var allStudents = null;
  var studentRequestId = 0;
  var assessmentRequestId = 0;
  var localDocumentAssessments = [];
  var searchTimer = null;
  var localRenderStudentPerformance = window.renderStudentPerformance;
  window.ensureFeedbackData = function (assessment, student, result) {
    result.aiFeedback = "See saved evaluation results below.";
    result.strengths = []; result.improvements = []; result.questionFeedback = [];
    result.approvedFeedback = "See saved feedback below.";
    result.emailStatus = "See delivery history";
    return result;
  };

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

  function controlValue(id) {
    var control = document.getElementById(id);
    if (!control) throw new Error("The page is missing its " + id + " control. Reload the faculty workspace.");
    return control.value;
  }

  function toStudent(row) {
    return {
      id: row.id, roll: row.roll_number, name: row.name, cls: row.class_code,
      section: row.section, email: row.email, program: row.program,
      academicYear: row.academic_year, year: row.year, branch: row.branch, semester: row.semester, admissionYear: row.admission_year
    };
  }

  function populateWorkspaceFilters(workspaces) {
    var labels = {}, testLabels = {};
    workspaces.forEach(function (workspace) {
      if (workspace.subject_code) {
        var titleParts = (workspace.title || "").split(" - ");
        labels[workspace.subject_code] = titleParts.length > 1 ?
          titleParts.slice(1).join(" - ").replace(/\s*\(MOCK TEST\)\s*$/i, "") : workspace.subject_code;
      }
      if (workspace.test_code) testLabels[workspace.test_code] = workspace.test_code;
    });
    ["dashSubject", "fbSubject"].forEach(function (id) {
      var select = document.getElementById(id);
      if (!select) return;
      var selected = select.value;
      select.replaceChildren(new Option("All subjects", "all"));
      Object.keys(labels).sort(function (a, b) { return labels[a].localeCompare(labels[b]); }).forEach(function (code) {
        select.add(new Option(labels[code], code));
      });
      select.value = Object.prototype.hasOwnProperty.call(labels, selected) ? selected : "all";
    });
    Object.keys(labels).forEach(function (code) { subjects[code] = labels[code]; });
    ["dashTest", "fbTest"].forEach(function (id) {
      var select = document.getElementById(id);
      if (!select) return;
      var selected = select.value;
      var retainFall = id === "fbTest";
      select.replaceChildren(new Option(id === "fbTest" ? "All terms and assessments" : "All tests", "all"));
      if (retainFall) select.add(new Option("Fall 2026", "FALL2026"));
      Object.keys(testLabels).sort().forEach(function (code) { select.add(new Option(testLabels[code], code)); });
      select.value = selected === "FALL2026" && retainFall || Object.prototype.hasOwnProperty.call(testLabels, selected) ? selected : "all";
    });
  }

  async function getAllStudents() {
    if (!allStudents) {
      allStudents = apiGet("/api/students?limit=200").then(function (payload) {
        var roster = payload.records.map(toStudent);
        installRosterSelectors(roster);
        return roster;
      }).catch(function (error) {
        allStudents = null;
        throw error;
      });
    }
    return allStudents;
  }

  function installRosterSelectors(roster) {
    ["setup", "fb"].forEach(function (prefix) {
      var year = document.getElementById(prefix + "Class");
      if (!year || document.getElementById(prefix + "Department")) return;
      var label = document.querySelector('label[for="' + year.id + '"]');
      if (label) label.textContent = "Year";
      Array.from(year.options).forEach(function (option) {
        var student = roster.find(function (s) { return s.cls === option.value; });
        if (student) option.textContent = student.year;
        else if (option.value === "all") option.textContent = "All years";
      });
      var wrapper = document.createElement("div");
      wrapper.className = prefix === "setup" ? "field" : "filter";
      var departmentLabel = document.createElement("label");
      departmentLabel.htmlFor = prefix + "Department";
      departmentLabel.textContent = "Department";
      var department = document.createElement("select");
      department.id = prefix + "Department";
      if (prefix === "fb") department.add(new Option("All departments", "all"));
      Array.from(new Set(roster.map(function (s) { return s.branch; }).filter(Boolean))).sort().forEach(function (branch) { department.add(new Option(branch, branch)); });
      wrapper.append(departmentLabel, department);
      year.parentNode.parentNode.insertBefore(wrapper, year.parentNode);
      department.addEventListener("change", function () {
        var matching = roster.filter(function (s) { return department.value === "all" || s.branch === department.value; });
        Array.from(year.options).forEach(function (option) { option.disabled = option.value !== "all" && !matching.some(function (s) { return s.cls === option.value; }); });
        if (year.selectedOptions[0] && year.selectedOptions[0].disabled) year.value = matching.length ? matching[0].cls : "all";
        if (prefix === "fb") window.renderPerformance();
      });
    });
  }

  window.fetchCollegeStudentRecords = async function () {
    var records = await getAllStudents();
    students = records.slice();
    return { source: "local prototype roster", records: records };
  };

  function assessmentParams(prefix) {
    var cls = controlValue(prefix + "Class");
    var section = controlValue(prefix + "Section");
    var subject = controlValue(prefix + "Subject");
    var test = controlValue(prefix + "Test");
    var params = { class: cls, section: section, subject: subject };
    if (test !== "FALL2026") params.test = test;
    return params;
  }

  function resetResultsFromApi(rows) {
    rows.forEach(function (row) {
      var key = row.assessment_code + "|" + row.student_roll_number;
      var approvedByFaculty = typeof row.review_status === "string" && row.review_status.indexOf("Faculty approved") === 0;
      var rubricSource = row.review_status === "Faculty approved Gemini evaluation" ?
        "faculty-approved Gemini rubric evaluation" :
        (row.review_status === "Faculty approved OpenAI evaluation" ? "previously faculty-approved OpenAI rubric evaluation" :
        (row.review_status === "Faculty approved demo evaluation" ? "faculty-approved demo rubric evaluation" : "Stored assessment record"));
      if (!results[key]) {
        results[key] = {
          score: row.score, max: row.max_score, status: approvedByFaculty ? "Approved" : "Needs review", flag: false,
          source: rubricSource, reviewStatus: row.review_status
        };
      } else {
        // Keep this page session's faculty review edits while refreshing source marks.
        if (approvedByFaculty || (results[key].source === "Stored assessment record" && results[key].status !== "Approved")) {
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

  var assessmentTimer;
  window.renderAssessments = async function () {
    clearTimeout(assessmentTimer);
    var requestId = ++assessmentRequestId;
    var table = document.getElementById("assessmentRows");
    table.innerHTML = '<tr><td colspan="6" class="empty" role="status">Loading assessments…</td></tr>';
    try {
      var query = queryString(assessmentParams("dash"));
      var responses = await Promise.all([
        getAllStudents(), apiGet("/api/assessments" + (query ? "?" + query : "")),
        apiGet("/api/assessment-workspaces"), apiGet("/api/documents")
      ]);
      if (requestId !== assessmentRequestId) return;
      populateWorkspaceFilters(responses[2]);
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
      localDocumentAssessments = responses[2].map(function (workspace) {
        var documents = responses[3].records.filter(function (document) {
          return document.assessment_code === workspace.assessment_code;
        });
        var rolls = documents.filter(function (document) { return document.document_type === "answer_sheet"; })
          .map(function (document) { return document.student_roll_number; }).filter(Boolean);
        var jobs = documents.map(function (doc) { return doc.job; }).filter(Boolean);
        var remaining = jobs.filter(function (job) { return job.state === "queued" || job.state === "running"; }).length;
        var attention = jobs.filter(function (job) { return job.state === "needs_attention"; }).length;
        var completed = jobs.filter(function (job) { return job.stage === "evaluation" && job.state === "complete"; }).length;
        var prior = grouped[workspace.assessment_code];
        var priorStatus = prior && assessments.find(function (item) { return item.id === workspace.assessment_code; }).status;
        var status = remaining ? remaining + " of " + jobs.length + " files queued or processing" :
          attention ? attention + " file(s) need attention" :
          priorStatus === "Feedback ready" && prior.submitted >= rolls.length ? priorStatus :
          completed ? "Ready for faculty review" : priorStatus ||
          (documents.length ? "Documents uploaded" : "Setup saved");
        return {
          id: workspace.assessment_code, title: workspace.title, cls: workspace.class_code,
          section: workspace.section, subject: workspace.subject_code, test: workspace.test_code,
          term: "Fall 2026", date: workspace.test_date || "Date not set", submitted: rolls.length,
          total: roster.filter(function (s) { return s.cls === workspace.class_code && s.section === workspace.section; }).length,
          status: status, rolls: Array.from(new Set(rolls)),
          _documentWorkflow: true, documents: documents
        };
      });
      var dashboardFilters = {
        cls: controlValue("dashClass"), section: controlValue("dashSection"),
        subject: controlValue("dashSubject"), test: controlValue("dashTest")
      };
      var localRows = localDocumentAssessments.filter(function (item) {
        return (dashboardFilters.cls === "all" || dashboardFilters.cls === item.cls) &&
          (dashboardFilters.section === "all" || dashboardFilters.section === item.section) &&
          (dashboardFilters.subject === "all" || dashboardFilters.subject === item.subject) &&
          (dashboardFilters.test === "all" || dashboardFilters.test === item.test);
      });
      var workspaceCodes = new Set(responses[2].map(function (workspace) { return workspace.assessment_code; }));
      assessments = assessments.filter(function (item) { return !workspaceCodes.has(item.id); }).concat(localRows);
      if (responses[3].records.some(function (doc) { return doc.job && ["queued", "running"].includes(doc.job.state); })) {
        assessmentTimer = setTimeout(function () {
          if (document.getElementById("dashboard").style.display !== "none") window.renderAssessments();
        }, 3000);
      }
      var list = assessments;
      table.innerHTML = list.length ? list.map(function (assessment) {
        return '<tr><td><span class="primarytext">' + assessment.title + '</span><div class="secondary">' + (tests[assessment.test] || assessment.test) + ' - ' + assessment.date + '</div></td><td>' + (classes[assessment.cls] || assessment.cls) + ' - Section ' + assessment.section + '</td><td>' + (subjects[assessment.subject] || assessment.subject) + '</td><td>' + assessment.submitted + ' of ' + assessment.total + ' students</td><td><span class="pill ' + (assessment.status === "Feedback ready" ? "green" : "amber") + '">' + assessment.status + '</span></td><td><button class="btn primary small" data-open="' + assessment.id + '">Open test review →</button></td></tr>';
      }).join("") : '<tr><td colspan="6" class="empty">No assessments match these filters.</td></tr>';
      document.getElementById("statAssessments").textContent = list.length;
      document.getElementById("statSubmissions").textContent = list.reduce(function (sum, item) { return sum + item.submitted; }, 0);
      document.getElementById("statReview").textContent = list.filter(function (item) { return item.status === "Review in progress"; }).reduce(function (sum, item) { return sum + item.submitted; }, 0);
      document.getElementById("statFeedback").textContent = list.filter(function (item) { return item.status === "Feedback ready"; }).reduce(function (sum, item) { return sum + item.submitted; }, 0);
      table.querySelectorAll("[data-open]").forEach(function (button) {
        var actionCell = button.parentNode;
        actionCell.classList.add("table-actions");
        if (!localDocumentAssessments.some(function (item) { return String(item.id) === button.dataset.open; })) return;
        if (actionCell.querySelector("[data-delete-assessment]")) return;
        var remove = document.createElement("button");
        remove.className = "btn small danger";
        remove.textContent = "Delete";
        remove.dataset.deleteAssessment = button.dataset.open;
        var menu = document.createElement("details");
        menu.className = "assessment-menu";
        var trigger = document.createElement("summary");
        trigger.textContent = "⋮";
        trigger.setAttribute("aria-label", "Assessment actions for " + button.closest("tr").querySelector(".primarytext").textContent);
        menu.appendChild(trigger);
        menu.appendChild(remove);
        menu.addEventListener("toggle", function () {
          if (menu.open) table.querySelectorAll(".assessment-menu[open]").forEach(function (other) { if (other !== menu) other.open = false; });
        });
        actionCell.appendChild(menu);
      });
      table.querySelectorAll("[data-open]").forEach(function (button) {
        button.addEventListener("click", function () { openAssessment(button.dataset.open); });
      });
      table.querySelectorAll("[data-delete-assessment]").forEach(function (button) {
        button.addEventListener("click", async function () {
          var title = button.closest("tr").querySelector(".primarytext").textContent;
          if (!window.confirm('Delete "' + title + '"? This permanently removes its questions, uploads, processing jobs, evaluations, and feedback. The roster and other assessments stay unchanged.')) return;
          button.disabled = true;
          try {
            var response = await fetch("/api/assessment-workspaces/" + encodeURIComponent(button.dataset.deleteAssessment), { method: "DELETE" });
            if (!response.ok) { var body = await response.json().catch(function () { return {}; }); throw new Error(body.detail || "Delete failed."); }
            toast("Assessment deleted."); window.renderAssessments();
          } catch (error) { button.disabled = false; toast(error.message); }
        });
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

  // Open the queue from the same workspace payload that owns its questions,
  // reference files, submissions, and evaluations. The dashboard's historical
  // Assessment rows are a different table and may legitimately be absent.
  window.openAssessment = async function (assessmentCode, refreshOnly) {
    try {
      var payload = await apiGet("/api/assessment-workspaces/" + encodeURIComponent(assessmentCode) + "/review");
      if (refreshOnly && (!activeAssessment || activeAssessment.id !== assessmentCode || document.getElementById("classReview").style.display === "none")) return;
      var workspace = payload.workspace;
      var assessment = assessments.find(function (item) { return item.id === assessmentCode; });
      if (!assessment) {
        assessment = {
          id: workspace.assessment_code, title: workspace.title, cls: workspace.class_code,
          section: workspace.section, subject: workspace.subject_code, test: workspace.test_code,
          date: workspace.test_date || "Date not set", term: "", rolls: [], submitted: 0, total: 0
        };
      }
      assessment.id = workspace.assessment_code;
      assessment.title = workspace.title;
      assessment.cls = workspace.class_code;
      assessment.section = workspace.section;
      assessment.subject = workspace.subject_code;
      assessment.test = workspace.test_code;
      assessment.date = workspace.test_date || "Date not set";
      assessment.questions = workspace.questions || [];
      assessment.totalMarks = workspace.total_marks || 0;
      assessment.markingMode = workspace.marking_mode;
      assessment.correctnessThreshold = workspace.correctness_threshold;
      assessment.scoreBands = workspace.score_bands || [];
      assessment.reviewPayload = payload;
      assessment.rolls = payload.submissions.map(function (submission) { return submission.student.roll_number; });
      assessment.submitted = assessment.rolls.length;
      assessment.total = students.filter(function (student) {
        return student.cls === workspace.class_code && student.section === workspace.section;
      }).length;
      assessment.documents = payload.submissions.reduce(function (all, submission) {
        return all.concat(submission.answer_sheets || []);
      }, []);
      assessment._documentWorkflow = true;
      if (!assessments.some(function (item) { return item.id === assessmentCode; })) assessments.unshift(assessment);
      activeAssessment = assessment;
      window.renderQueue();
      if (!refreshOnly) window.go("classReview");
    } catch (error) {
      var rows = document.getElementById("queueRows");
      if (rows) rows.innerHTML = '<tr><td colspan="6" class="empty" role="alert">' + String(error.message).replace(/[&<>"']/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]; }) + '</td></tr>';
      if (!refreshOnly) window.go("classReview");
    }
  };

  async function loadPerformance() {
    var requestId = ++studentRequestId;
    var lookup = document.getElementById("studentLookup");
    var search = controlValue("studentSearch").trim();
    lookup.innerHTML = '<div class="report-empty" role="status">Loading student records…</div>';
    document.getElementById("studentReport").innerHTML = '<div class="report-empty" role="status">Loading student assessment history…</div>';
    document.getElementById("studentCountTag").textContent = "…";
    try {
      // Subject and test filters describe assessment history, not roster membership.
      // Workspace-only students have no Assessment rows yet and must remain searchable.
      var params = {
        class: controlValue("fbClass"), section: controlValue("fbSection"), search: search
      };
      var query = queryString(params);
      var payload = await apiGet("/api/students" + (query ? "?" + query : ""));
      if (requestId !== studentRequestId) return;
      var filteredStudents = payload.records.map(toStudent);
      var departmentFilter = document.getElementById("fbDepartment");
      if (departmentFilter && departmentFilter.value !== "all") filteredStudents = filteredStudents.filter(function (student) { return student.branch === departmentFilter.value; });
      document.getElementById("studentCountTag").textContent = filteredStudents.length + " STUDENTS";
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
        if (!results[key]) results[key] = { score: row.score, max: row.max_score, status: "Needs review", flag: false, source: "Stored assessment record" };
        return row;
      });
      current.assessments = requestRows;
      localRenderStudentPerformance(current);
      report.querySelectorAll(".assessment-history").forEach(function (section) {
        if (section.querySelector(".topicchips")) section.remove();
      });
      report.querySelectorAll(".recordcols, article.assessment-record > details, .deliver").forEach(function (element) { element.remove(); });
      report.querySelectorAll(".assessment-history > .carddesc").forEach(function (element) { element.textContent = "Saved assessment marks. Evaluation results and feedback are shown below when available."; });
    } catch (error) {
      report.innerHTML = '<div class="report-empty" role="alert">Could not load this student’s assessment history. ' + error.message + '</div>';
    }
  };

  window.performanceRowsFor = function (student) {
    return (student.assessments || []).filter(function (row) {
      var subject = controlValue("fbSubject");
      var test = controlValue("fbTest");
      return (subject === "all" || row.subject_code === subject) &&
        (test === "all" || test === "FALL2026" || row.test_code === test);
    }).map(function (row) {
      var assessment = {
        id: row.assessment_code, title: row.title, cls: row.class_code,
        section: row.section, subject: row.subject_code, test: row.test_code,
        term: row.term, date: row.date, rolls: [student.roll]
      };
      var key = assessment.id + "|" + student.roll;
      if (!results[key]) results[key] = { score: row.score, max: row.max_score, status: row.review_status && row.review_status.indexOf("Faculty approved") === 0 ? "Approved" : "Needs review", flag: false, source: row.review_status === "Faculty approved Gemini evaluation" ? "faculty-approved Gemini rubric evaluation" : (row.review_status === "Faculty approved OpenAI evaluation" ? "previously faculty-approved OpenAI rubric evaluation" : (row.review_status === "Faculty approved demo evaluation" ? "faculty-approved demo rubric evaluation" : "Stored assessment record")), reviewStatus: row.review_status };
      if (results[key].source === "Stored assessment record" && results[key].status !== "Approved") {
        results[key].score = row.score;
        results[key].max = row.max_score;
      }
      var review = ensureFeedbackData(assessment, student, results[key]);
      if (review.source === "faculty-approved Gemini rubric evaluation" || review.source === "previously faculty-approved OpenAI rubric evaluation" || review.source === "faculty-approved demo rubric evaluation") {
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
