(function () {
  var CSS =
    "#feedback-modal{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;align-items:center;justify-content:center;z-index:9999;font-family:system-ui,sans-serif}" +
    "#feedback-modal .fb-box{background:#fff;padding:20px;border-radius:8px;width:420px;max-width:90vw;box-shadow:0 8px 30px rgba(0,0,0,.25)}" +
    "#feedback-modal h3{margin:0 0 10px;font-size:16px}" +
    "#feedback-text{width:100%;height:110px;box-sizing:border-box;font:inherit;padding:8px;border:1px solid #ccc;border-radius:4px}" +
    "#feedback-modal .fb-actions{margin-top:10px;text-align:right}" +
    "#feedback-modal button{font:inherit;padding:6px 14px;border-radius:4px;border:1px solid #ccc;background:#f5f5f5;cursor:pointer;margin-left:6px}" +
    "#feedback-submit{background:#2563eb !important;color:#fff;border-color:#2563eb !important}" +
    "#feedback-toast{position:fixed;bottom:20px;right:20px;background:#111;color:#fff;padding:10px 16px;border-radius:6px;display:none;z-index:10000;font-family:system-ui,sans-serif;max-width:400px}" +
    "#feedback-toast.fb-error{background:#b91c1c}";

  var HTML =
    '<div id="feedback-modal" role="dialog" aria-modal="true">' +
    '<div class="fb-box"><h3>Send feedback</h3>' +
    '<textarea id="feedback-text" placeholder="What is happening?"></textarea>' +
    '<div class="fb-actions"><button type="button" id="feedback-cancel">Cancel</button>' +
    '<button type="button" id="feedback-submit">Send</button></div></div></div>' +
    '<div id="feedback-toast" role="status"></div>';

  var toastTimer;
  function toast(msg, isError) {
    var t = document.getElementById("feedback-toast");
    t.textContent = msg;
    t.className = isError ? "fb-error" : "";
    t.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.style.display = "none"; }, isError ? 8000 : 4000);
  }

  function setModal(open) {
    document.getElementById("feedback-modal").style.display = open ? "flex" : "none";
    if (open) document.getElementById("feedback-text").focus();
  }

  function nextFrame() {
    return new Promise(function (r) { requestAnimationFrame(function () { r(); }); });
  }

  function submit() {
    var textEl = document.getElementById("feedback-text");
    var text = textEl.value.trim();
    if (!text) { textEl.focus(); return; }
    var submitBtn = document.getElementById("feedback-submit");
    submitBtn.disabled = true;
    setModal(false);
    var user = window.APP_USER || {};
    nextFrame()
      .then(function () {
        return html2canvas(document.body, {
          ignoreElements: function (el) { return el.id === "feedback-modal" || el.id === "feedback-toast"; }
        });
      })
      .then(function (canvas) {
        var now = Date.now();
        return fetch("/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "message",
            channel: "C-FEEDBACK",
            user: user.id,
            text: text,
            ts: (now / 1000).toFixed(6),
            files: [{ name: "screenshot.png", mimetype: "image/png", data_url: canvas.toDataURL("image/png") }],
            page_url: location.href,
            client_ts: new Date(now).toISOString()
          })
        });
      })
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (body) {
            throw new Error("HTTP " + res.status + (body ? ": " + body.slice(0, 200) : ""));
          });
        }
        textEl.value = "";
        toast("Thanks — feedback sent", false);
      })
      .catch(function (err) {
        console.error("Feedback failed", err);
        toast("Feedback failed: " + (err && err.message ? err.message : String(err)), true);
      })
      .then(function () { submitBtn.disabled = false; });
  }

  function init() {
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    var wrap = document.createElement("div");
    wrap.innerHTML = HTML;
    while (wrap.firstChild) document.body.appendChild(wrap.firstChild);

    document.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest("#feedback-button")) { e.preventDefault(); setModal(true); }
      else if (t.closest("#feedback-cancel")) setModal(false);
      else if (t.closest("#feedback-submit")) submit();
      else if (t.id === "feedback-modal") setModal(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setModal(false);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
