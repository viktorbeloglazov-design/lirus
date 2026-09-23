// Небольшие удобства интерфейса. Панель работает и без них.
(function () {
  "use strict";

  // Подтверждение опасных действий
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      e.preventDefault();
      return;
    }
    // Долгие действия: блокируем кнопку и показываем, что идёт работа
    var busy = form.getAttribute("data-busy");
    var submitter = e.submitter;
    var busyButton = submitter && submitter.getAttribute("data-busy-button");
    if (busy || busyButton) {
      var buttons = form.querySelectorAll("button[type=submit]");
      window.setTimeout(function () {
        buttons.forEach(function (b) { b.disabled = true; });
      }, 0);
      if (submitter) {
        submitter.classList.add("is-busy");
        submitter.textContent = busyButton || "Подождите…";
      }
      if (busy) {
        var note = document.createElement("div");
        note.className = "busy-note";
        note.textContent = busy;
        form.appendChild(note);
      }
    }
  });

  document.addEventListener("click", function (e) {
    var t = e.target;
    // Показать / скрыть секрет
    var id = t.getAttribute && t.getAttribute("data-toggle-secret");
    if (id) {
      var input = document.getElementById(id);
      if (input) {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        t.textContent = show ? "Скрыть" : "Показать";
      }
    }
    // Копирование в буфер
    var text = t.getAttribute && t.getAttribute("data-copy");
    if (text) {
      var done = function () { t.textContent = "Скопировано"; setTimeout(function () { t.textContent = "Копировать"; }, 1500); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); done(); } catch (err) { /* ничего */ }
        document.body.removeChild(ta);
      }
    }
    if (t.hasAttribute && t.hasAttribute("data-back")) {
      history.back();
    }
  });

  // Ожидание перезапуска
  var wait = document.querySelector("[data-wait-restart]");
  if (wait) {
    var started = Date.now();
    var poll = function () {
      fetch("/health", { cache: "no-store" }).then(function (r) {
        if (r.ok && Date.now() - started > 3000) { window.location.href = "/"; return; }
        setTimeout(poll, 1500);
      }).catch(function () { setTimeout(poll, 1500); });
    };
    setTimeout(poll, 3000);
  }
})();
