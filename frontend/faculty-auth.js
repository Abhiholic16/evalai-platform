/* Same-origin faculty sign-in, signed-cookie session and CSRF header support. */
(function () {
  var nativeFetch = window.fetch.bind(window);
  var csrfToken = null;
  var initialized = false;
  var appRoot = null;
  var panel = null;
  function showPanel(message) {
    if (appRoot) appRoot.style.display = "none";
    panel.hidden = false;
    panel.style.display = "grid";
    var status = panel.querySelector("[data-auth-status]");
    status.textContent = message || "Sign in with the faculty account configured for this workspace.";
  }
  function showApp() {
    panel.hidden = true;
    panel.style.display = "none";
    if (appRoot) appRoot.style.display = "";
  }
  function authError(message) {
    showPanel(message || "Your faculty session expired. Sign in again to continue.");
  }
  window.fetch = async function (input, init) {
    init = init || {};
    var url = typeof input === "string" ? input : input.url;
    var method = (init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
    if (url.indexOf("/api/") === 0 && !url.startsWith("/api/auth/login") && method !== "GET" && method !== "HEAD") {
      init.headers = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
      if (csrfToken) init.headers.set("X-CSRF-Token", csrfToken);
    }
    init.credentials = "same-origin";
    var response = await nativeFetch(input, init);
    if (response.status === 401 && url.indexOf("/api/auth/") !== 0) authError();
    return response;
  };
  var originalXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__evalaiMethod = String(method).toUpperCase();
    this.__evalaiUrl = String(url);
    return originalXhrOpen.apply(this, arguments);
  };
  var originalXhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function () {
    if (this.__evalaiUrl && this.__evalaiUrl.indexOf("/api/") === 0 && this.__evalaiMethod !== "GET" && csrfToken) {
      this.setRequestHeader("X-CSRF-Token", csrfToken);
    }
    var xhr = this;
    xhr.addEventListener("load", function () { if (xhr.status === 401) authError(); });
    return originalXhrSend.apply(this, arguments);
  };
  function makePanel() {
    panel = document.createElement("div");
    panel.id = "facultyAuthPanel";
    panel.hidden = true;
    panel.style.cssText = "position:fixed;inset:0;z-index:10000;background:#f5f7fa;display:none;place-items:center;padding:20px;font:14px Arial,sans-serif;color:#19263c";
    panel.innerHTML = '<form style="width:min(100%,390px);background:white;border:1px solid #e5eaf1;border-radius:14px;padding:28px;box-shadow:0 12px 36px #22314b18"><div style="font-size:22px;font-weight:700;margin-bottom:6px">EvalAI faculty sign-in</div><p style="color:#718097;font-size:13px">Faculty workspace</p><label for="facultyUsername">Faculty username</label><input id="facultyUsername" name="username" autocomplete="username" required style="display:block;width:100%;margin:7px 0 14px;padding:11px;border:1px solid #d6deea;border-radius:7px"><label for="facultyPassword">Password</label><input id="facultyPassword" name="password" type="password" autocomplete="current-password" required style="display:block;width:100%;margin:7px 0 16px;padding:11px;border:1px solid #d6deea;border-radius:7px"><button class="btn primary" type="submit" style="width:100%;padding:12px">Sign in</button><div data-auth-status role="status" style="margin-top:12px;color:#718097;font-size:12px"></div></form>';
    document.body.appendChild(panel);
    panel.querySelector("form").addEventListener("submit", async function (event) {
      event.preventDefault();
      var button = panel.querySelector("button");
      button.disabled = true;
      try {
        var response = await nativeFetch("/api/auth/login", {
          method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: panel.querySelector("[name=username]").value, password: panel.querySelector("[name=password]").value })
        });
        var result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Sign-in failed.");
        csrfToken = result.csrf_token;
        showApp();
        window.location.reload();
      } catch (error) {
        panel.querySelector("[data-auth-status]").textContent = error.message;
        button.disabled = false;
      }
    });
  }
  async function getSession() {
    var response = await nativeFetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" });
    var data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Faculty sign-in is not configured on the server.");
    return data;
  }
  window.bootstrapEvalAI = async function () {
    if (initialized) return false;
    initialized = true;
    appRoot = document.querySelector(".app");
    if (appRoot) appRoot.style.display = "none";
    if (!panel) makePanel();
    try {
      var session = await getSession();
      if (session.authenticated) {
        csrfToken = session.csrf_token;
        showApp();
        addSignOut();
        return true;
      }
      showPanel("Sign in with the faculty account configured for this workspace.");
    } catch (error) { showPanel(error.message); }
    return false;
  };
  function addSignOut() {
    var foot = document.querySelector(".sidefoot");
    if (!foot || document.getElementById("facultySignOut")) return;
    var button = document.createElement("button");
    button.id = "facultySignOut";
    button.className = "btn small";
    button.textContent = "Sign out";
    button.style.marginTop = "12px";
    button.addEventListener("click", async function () {
      await window.fetch("/api/auth/logout", { method: "POST" });
      csrfToken = null;
      showPanel("You have signed out of the faculty workspace.");
    });
    foot.appendChild(button);
  }
  window.getFacultyCSRFToken = function () { return csrfToken; };
})();
