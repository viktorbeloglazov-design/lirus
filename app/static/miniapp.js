// Мини-приложение в MAX. Работает и в обычном браузере (предпросмотр из панели).
(function () {
  "use strict";
  var W = window.WebApp || null;            // мост MAX; в обычном браузере его может не быть
  var initData = (W && W.initData) || "";
  var state = { data: null, service: "", screen: "home" };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function haptic(kind) { try { if (W && W.HapticFeedback) W.HapticFeedback.impactOccurred(kind || "light"); } catch (e) { /* не везде есть */ } }

  function api(path, body) {
    var headers = { "Content-Type": "application/json" };
    if (initData) headers["Authorization"] = "MaxInitData " + initData;
    return fetch(path, { method: body ? "POST" : "GET", headers: headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); });
  }

  function show(name) {
    ["loading", "error", "home", "book", "done", "list", "chat"].forEach(function (s) { $("screen-" + s).hidden = s !== name; });
    state.screen = name;
    document.querySelectorAll("#tabs button").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-go") === (name === "done" ? "list" : name)); });
    $("tabs").hidden = name === "loading" || name === "error";
    window.scrollTo(0, 0);
    try { if (W && W.BackButton) { if (name === "home") W.BackButton.hide(); else { W.BackButton.show(); W.BackButton.onClick(function () { show("home"); }); } } } catch (e) { /* нет кнопки */ }
    if (name === "chat") { setTimeout(function () { $("chat-input").focus(); }, 50); scrollChat(); }
  }

  function fail(text) { $("error-text").textContent = text; show("error"); }

  // ---------------------------------------------------------------- загрузка
  function load() {
    show("loading");
    api("/max/api/bootstrap").then(function (d) {
      if (!d.ok) { fail(d.error || "Не удалось загрузить данные."); return; }
      state.data = d;
      render(d);
      show("home");
      try { if (W && W.ready) W.ready(); if (W && W.expand) W.expand(); } catch (e) { /* ничего */ }
      var start = (W && W.initDataUnsafe && W.initDataUnsafe.start_param) || "";
      if (start === "book") show("book"); else if (start === "ask") show("chat"); else if (start === "list") show("list");
    }).catch(function () { fail("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз."); });
  }

  function render(d) {
    $("title").textContent = d.title; $("company").textContent = d.company || "";
    $("user-name").textContent = d.user.name || "гость";
    $("about").textContent = d.about; $("hours").textContent = d.hours ? "Часы работы: " + d.hours : "";
    if (d.phone) { $("call").hidden = false; $("call").href = "tel:" + d.phone.replace(/[^\d+]/g, ""); }
    // услуги
    var box = $("services"); box.innerHTML = "";
    d.services.forEach(function (s, i) {
      var lab = el("label", "choice"); var inp = el("input"); inp.type = "radio"; inp.name = "service"; inp.value = s.name;
      var txt = el("span"); txt.appendChild(el("b", null, s.name)); if (s.note) txt.appendChild(el("small", null, s.note));
      lab.appendChild(inp); lab.appendChild(txt); box.appendChild(lab);
      inp.addEventListener("change", function () { state.service = s.name; document.querySelectorAll(".choice").forEach(function (c) { c.classList.toggle("on", c.querySelector("input").checked); }); haptic(); });
    });
    var times = $("f-time"); times.innerHTML = "";
    d.times.forEach(function (t) { var o = el("option", null, t.title); o.value = t.key; times.appendChild(o); });
    $("f-name").value = d.client.name || ""; $("f-phone").value = d.client.phone || ""; $("f-address").value = d.client.address || "";
    var today = new Date(); $("f-date").min = today.toISOString().slice(0, 10);
    // частые вопросы
    var faq = $("faq"); faq.innerHTML = "";
    if (d.faq.length) { $("faq-card").hidden = false; d.faq.forEach(function (f) {
      var q = el("button", "faq-q", f.q); var a = el("div", "faq-a", f.a);
      q.addEventListener("click", function () { a.classList.toggle("open"); });
      faq.appendChild(q); faq.appendChild(a);
    }); }
    renderBookings(d.bookings);
    // история чата
    var chat = $("chat"); chat.innerHTML = "";
    addMsg("hint", "Задайте вопрос про обслуживание бассейна, химию или оборудование. Если помощник не знает ответа — передаст менеджеру.");
    (d.history || []).forEach(function (m) { addMsg(m.role, m.content); });
  }

  function renderBookings(list) {
    var box = $("bookings"); box.innerHTML = "";
    $("count-badge").hidden = !list.length; $("count-badge").textContent = list.length;
    if (!list.length) { box.appendChild(el("p", "muted", "Заявок пока нет. Создайте первую — это займёт минуту.")); return; }
    list.forEach(function (b) {
      var card = el("div", "booking");
      var st = el("span", "st " + (b.status === "Выполнена" ? "done" : b.status === "Отменена" ? "cancelled" : ""), b.status);
      card.appendChild(st); card.appendChild(el("b", null, "№" + b.id + " · " + b.service));
      card.appendChild(el("small", null, "Дата: " + b.date + ", " + b.time.toLowerCase()));
      if (b.comment) card.appendChild(el("small", null, b.comment));
      box.appendChild(card);
    });
  }

  // ------------------------------------------------------------------ запись
  function setErrors(errors) {
    document.querySelectorAll("#book-form .error").forEach(function (e) { e.textContent = ""; e.closest(".field") && e.closest(".field").classList.remove("has-error"); });
    Object.keys(errors || {}).forEach(function (k) {
      var e = document.querySelector('#book-form .error[data-for="' + k + '"]');
      if (e) { e.textContent = errors[k]; var f = e.closest(".field"); if (f) f.classList.add("has-error"); }
    });
    var first = document.querySelector("#book-form .has-error"); if (first) first.scrollIntoView({ block: "center" });
  }

  $("book-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target; var body = {};
    ["name", "phone", "address", "pool", "date", "time", "comment"].forEach(function (k) { body[k] = f.elements[k].value; });
    body.service = state.service;
    var errors = {};
    if (!body.service) errors.service = "Выберите услугу.";
    if (body.name.trim().length < 2) errors.name = "Напишите, как к вам обращаться.";
    if (body.phone.replace(/\D/g, "").length < 10) errors.phone = "Укажите телефон для связи.";
    if (Object.keys(errors).length) { setErrors(errors); haptic("medium"); return; }
    var btn = $("book-submit"); btn.disabled = true; btn.textContent = "Отправляем…";
    api("/max/api/booking", body).then(function (d) {
      btn.disabled = false; btn.textContent = "Отправить заявку";
      if (!d.ok) { setErrors(d.errors || { form: d.error || "Не удалось отправить заявку." }); return; }
      haptic("medium");
      $("done-text").textContent = d.message;
      f.elements.comment.value = ""; f.elements.date.value = "";
      api("/max/api/bookings").then(function (r) { if (r.ok) renderBookings(r.bookings); });
      show("done");
    }).catch(function () { btn.disabled = false; btn.textContent = "Отправить заявку"; setErrors({ form: "Нет связи с сервером. Попробуйте ещё раз." }); });
  });

  // -------------------------------------------------------------------- чат
  function addMsg(role, text) { var m = el("div", "msg " + role, text); $("chat").appendChild(m); scrollChat(); return m; }
  function scrollChat() { setTimeout(function () { window.scrollTo(0, document.body.scrollHeight); }, 30); }

  $("chat-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var inp = $("chat-input"); var text = inp.value.trim();
    if (text.length < 2) return;
    inp.value = ""; addMsg("user", text);
    var typing = addMsg("assistant typing", "Думаю…"); $("chat-send").disabled = true;
    api("/max/api/ask", { text: text }).then(function (d) {
      typing.remove(); $("chat-send").disabled = false;
      if (!d.ok) { addMsg("hint", d.error || "Не удалось получить ответ."); return; }
      addMsg("assistant", d.answer);
      if (d.handoff) addMsg("hint", "Вопрос передан менеджеру. Чтобы с вами точно связались — оставьте заявку с телефоном.");
      inp.focus();
    }).catch(function () { typing.remove(); $("chat-send").disabled = false; addMsg("hint", "Нет связи с сервером."); });
  });

  // ---------------------------------------------------------------- переходы
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-go]"); if (!b) return;
    var to = b.getAttribute("data-go");
    if (to === "list") api("/max/api/bookings").then(function (r) { if (r.ok) renderBookings(r.bookings); });
    show(to);
  });
  $("retry").addEventListener("click", load);
  load();
})();
