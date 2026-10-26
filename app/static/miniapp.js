// Мини-приложение в MAX. Работает и в обычном браузере (предпросмотр из панели).
(function () {
  "use strict";
  var W = window.WebApp || null;            // мост MAX; в обычном браузере его может не быть
  var initData = (W && W.initData) || "";
  var state = { data: null, service: "", screen: "home", cart: {}, category: "", menu: [] };
  var SCREENS = ["loading", "error", "home", "shop", "cart", "book", "done", "list", "chat", "botchat", "contacts", "faq"];
  var GO = { shop: "shop", services: "book", chat: "botchat", assistant: "chat", orders: "list", contacts: "contacts", faq: "faq" };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function haptic(kind) { try { if (W && W.HapticFeedback) W.HapticFeedback.impactOccurred(kind || "light"); } catch (e) { /* не везде есть */ } }
  function money(n) { return (Math.round(n * 100) / 100).toLocaleString("ru-RU") + " ₽"; }

  function api(path, body) {
    var headers = { "Content-Type": "application/json" };
    if (initData) headers["Authorization"] = "MaxInitData " + initData;
    return fetch(path, { method: body ? "POST" : "GET", headers: headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); });
  }

  function show(name) {
    SCREENS.forEach(function (s) { $("screen-" + s).hidden = s !== name; });
    state.screen = name;
    var tabName = name === "done" ? "list" : name === "cart" ? "shop" : name;
    document.querySelectorAll("#tabs button").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-go") === tabName); });
    $("tabs").hidden = name === "loading" || name === "error";
    window.scrollTo(0, 0);
    try { if (W && W.BackButton) { if (name === "home") W.BackButton.hide(); else { W.BackButton.show(); W.BackButton.onClick(function () { show(name === "cart" ? "shop" : "home"); }); } } } catch (e) { /* нет кнопки */ }
    if (name === "chat") { setTimeout(function () { $("chat-input").focus(); }, 50); scrollChat(); }
    if (name === "cart") renderCart();
    if (name === "list") api("/max/api/bookings").then(function (r) { if (r.ok) renderBookings(r.bookings); });
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
      if (GO[start]) show(GO[start]); else if (start === "book") show("book");
    }).catch(function () { fail("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз."); });
  }

  function render(d) {
    $("title").textContent = d.title; $("company").textContent = d.company || "";
    $("user-name").textContent = d.user.name || "гость";
    $("about").textContent = d.about;
    var tel = d.phone ? "tel:" + d.phone.replace(/[^\d+]/g, "") : "";
    if (d.phone) { $("call").hidden = false; $("call").href = tel; $("c-call").hidden = false; $("c-call").href = tel; }
    // меню главного экрана и вкладки
    state.menu = d.menu.length ? d.menu : [{ type: "services", title: "Записаться на сервис" }, { type: "orders", title: "Мои заявки" }];
    var menu = $("menu"); menu.innerHTML = "";
    var tabs = $("tabs"); tabs.innerHTML = "";
    var home = el("button", "active", "Главная"); home.setAttribute("data-go", "home"); tabs.appendChild(home);
    state.menu.forEach(function (m, i) {
      var b = el("button", "big" + (i === 0 ? " primary" : ""), m.title);
      if (m.type === "link") { b.addEventListener("click", function () { openLink(m.param); }); }
      else { b.setAttribute("data-go", GO[m.type] || "home"); }
      if (m.type === "orders") { var badge = el("span", "badge"); badge.id = "count-badge"; badge.hidden = true; b.appendChild(badge); }
      menu.appendChild(b);
      if (i < 3 && m.type !== "link") { var t = el("button", null, m.title); t.setAttribute("data-go", GO[m.type]); tabs.appendChild(t); }
      var titles = { shop: "shop-title", services: "book-title", orders: "list-title", contacts: "contacts-title", faq: "faq-title" };
      if (titles[m.type]) $(titles[m.type]).textContent = m.title;
    });
    tabs.style.gridTemplateColumns = "repeat(" + tabs.children.length + ", 1fr)";
    // услуги для записи
    var box = $("services"); box.innerHTML = "";
    d.services.forEach(function (s) {
      var lab = el("label", "choice"); var inp = el("input"); inp.type = "radio"; inp.name = "service"; inp.value = s.name;
      var txt = el("span"); txt.appendChild(el("b", null, s.name)); if (s.note) txt.appendChild(el("small", null, s.note));
      lab.appendChild(inp); lab.appendChild(txt); box.appendChild(lab);
      inp.addEventListener("change", function () { state.service = s.name; document.querySelectorAll("#services .choice").forEach(function (c) { c.classList.toggle("on", c.querySelector("input").checked); }); haptic(); });
    });
    var times = $("f-time"); times.innerHTML = "";
    d.times.forEach(function (t) { var o = el("option", null, t.title); o.value = t.key; times.appendChild(o); });
    ["f", "o"].forEach(function (p) { $(p + "-name").value = d.client.name || ""; $(p + "-phone").value = d.client.phone || ""; $(p + "-address").value = d.client.address || ""; });
    $("f-date").min = new Date().toISOString().slice(0, 10);
    // магазин
    $("shop-note").textContent = d.shop_note || "";
    renderShop();
    // контакты, частые вопросы
    $("c-phone").textContent = d.phone ? "Телефон: " + d.phone : "";
    $("c-hours").textContent = d.hours ? "Часы работы: " + d.hours : "";
    $("c-about").textContent = d.about;
    $("botchat-hours").textContent = d.hours ? "Отвечаем в рабочее время: " + d.hours : "";
    if (d.bot_username) { $("botchat-link").hidden = false; $("botchat-link").href = "https://max.ru/" + d.bot_username; }
    var faq = $("faq"); faq.innerHTML = "";
    if (!d.faq.length) faq.appendChild(el("p", "muted", "Пока нет вопросов — задайте свой в чате."));
    d.faq.forEach(function (f) {
      var q = el("button", "faq-q", f.q); var a = el("div", "faq-a", f.a);
      q.addEventListener("click", function () { a.classList.toggle("open"); });
      faq.appendChild(q); faq.appendChild(a);
    });
    renderBookings(d.bookings);
    var chat = $("chat"); chat.innerHTML = "";
    addMsg("hint", "Задайте вопрос про обслуживание бассейна, химию или оборудование. Если помощник не знает ответа — передаст менеджеру.");
    (d.history || []).forEach(function (m) { addMsg(m.role, m.content); });
  }

  function openLink(url) {
    try { if (W && W.openLink) { W.openLink(url); return; } } catch (e) { /* откроем обычно */ }
    window.open(url, "_blank", "noopener");
  }

  // ------------------------------------------------------------------ магазин
  function renderShop() {
    var d = state.data; var cats = [];
    d.products.forEach(function (p) { if (p.category && cats.indexOf(p.category) < 0) cats.push(p.category); });
    var chips = $("categories"); chips.innerHTML = "";
    if (cats.length > 1) {
      [""].concat(cats).forEach(function (c) {
        var b = el("button", "chip" + (state.category === c ? " on" : ""), c || "Всё");
        b.addEventListener("click", function () { state.category = c; renderShop(); });
        chips.appendChild(b);
      });
    }
    var list = $("products"); list.innerHTML = "";
    var shown = d.products.filter(function (p) { return !state.category || p.category === state.category; });
    if (!shown.length) list.appendChild(el("p", "muted", "Товаров пока нет — напишите нам в чат."));
    shown.forEach(function (p) {
      var row = el("div", "product");
      var info = el("div", "info"); info.appendChild(el("b", null, p.name)); if (p.note) info.appendChild(el("small", null, p.note));
      row.appendChild(info);
      row.appendChild(el("span", "price", p.price ? money(p.price) : "по запросу"));
      var q = el("div", "qty"); var minus = el("button", null, "−"); var num = el("span", null, String(state.cart[p.key] || 0)); var plus = el("button", null, "+");
      minus.type = plus.type = "button";
      minus.addEventListener("click", function () { changeQty(p.key, -1); num.textContent = String(state.cart[p.key] || 0); });
      plus.addEventListener("click", function () { changeQty(p.key, 1); num.textContent = String(state.cart[p.key] || 0); });
      q.appendChild(minus); q.appendChild(num); q.appendChild(plus); row.appendChild(q);
      list.appendChild(row);
    });
    updateCartBar();
  }

  function changeQty(key, delta) {
    var n = (state.cart[key] || 0) + delta;
    if (n <= 0) delete state.cart[key]; else state.cart[key] = Math.min(n, 999);
    haptic(); updateCartBar();
  }

  function cartItems() {
    return state.data.products.filter(function (p) { return state.cart[p.key]; }).map(function (p) { return { key: p.key, name: p.name, price: p.price, qty: state.cart[p.key] }; });
  }

  function updateCartBar() {
    var items = cartItems(); var count = 0, total = 0, unknown = false;
    items.forEach(function (i) { count += i.qty; total += i.price * i.qty; if (!i.price) unknown = true; });
    $("cart-bar").hidden = !items.length;
    $("cart-summary").textContent = count + " шт. · " + (total ? money(total) + (unknown ? " + по запросу" : "") : "цена по запросу");
  }

  function renderCart() {
    var box = $("cart-items"); box.innerHTML = "";
    var items = cartItems(); var total = 0;
    if (!items.length) { box.appendChild(el("p", "muted", "Корзина пуста.")); return; }
    items.forEach(function (i) {
      total += i.price * i.qty;
      var line = el("div", "cart-line"); line.appendChild(el("span", null, i.name + " × " + i.qty));
      line.appendChild(el("span", null, i.price ? money(i.price * i.qty) : "по запросу")); box.appendChild(line);
    });
    var t = el("div", "cart-total"); t.appendChild(el("span", null, "Итого")); t.appendChild(el("span", null, total ? money(total) : "уточнит менеджер")); box.appendChild(t);
  }

  document.querySelectorAll("input[name=delivery]").forEach(function (r) {
    r.addEventListener("change", function () {
      $("d-delivery").classList.toggle("on", r.value === "delivery" && r.checked);
      $("d-pickup").classList.toggle("on", r.value === "pickup" && r.checked);
      $("o-address-field").hidden = r.value === "pickup" && r.checked;
    });
  });

  function setErrors(formId, errors) {
    document.querySelectorAll("#" + formId + " .error").forEach(function (e) { e.textContent = ""; var f = e.closest(".field"); if (f) f.classList.remove("has-error"); });
    Object.keys(errors || {}).forEach(function (k) {
      var e = document.querySelector("#" + formId + ' .error[data-for="' + k + '"]');
      if (e) { e.textContent = errors[k]; var f = e.closest(".field"); if (f) f.classList.add("has-error"); }
    });
    var first = document.querySelector("#" + formId + " .has-error"); if (first) first.scrollIntoView({ block: "center" });
  }

  $("order-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target;
    var body = { name: f.elements.name.value, phone: f.elements.phone.value, address: f.elements.address.value,
                 comment: f.elements.comment.value, delivery: f.elements.delivery.value, items: cartItems() };
    var errors = {};
    if (!body.items.length) errors.items = "Корзина пуста — добавьте хотя бы один товар.";
    if (body.name.trim().length < 2) errors.name = "Напишите, как к вам обращаться.";
    if (body.phone.replace(/\D/g, "").length < 10) errors.phone = "Укажите телефон для подтверждения.";
    if (body.delivery === "delivery" && body.address.trim().length < 5) errors.address = "Укажите адрес доставки или выберите самовывоз.";
    if (Object.keys(errors).length) { setErrors("order-form", errors); haptic("medium"); return; }
    var btn = $("order-submit"); btn.disabled = true; btn.textContent = "Отправляем…";
    api("/max/api/order", body).then(function (d) {
      btn.disabled = false; btn.textContent = "Отправить заказ";
      if (!d.ok) { setErrors("order-form", d.errors || { form: d.error || "Не удалось отправить заказ." }); return; }
      haptic("medium"); state.cart = {}; renderShop();
      $("done-title").textContent = "Заказ принят"; $("done-text").textContent = d.message; show("done");
    }).catch(function () { btn.disabled = false; btn.textContent = "Отправить заказ"; setErrors("order-form", { form: "Нет связи с сервером. Попробуйте ещё раз." }); });
  });

  // ------------------------------------------------------------------ заявки
  function renderBookings(list) {
    var box = $("bookings"); box.innerHTML = "";
    var badge = $("count-badge"); if (badge) { badge.hidden = !list.length; badge.textContent = list.length; }
    if (!list.length) { box.appendChild(el("p", "muted", "Заказов и заявок пока нет.")); return; }
    list.forEach(function (b) {
      var card = el("div", "booking");
      var cls = /Выполнен/.test(b.status) ? "done" : /Отмен/.test(b.status) ? "cancelled" : "";
      card.appendChild(el("span", "st " + cls, b.status));
      if (b.kind === "order") {
        card.appendChild(el("b", null, "Заказ №" + b.id + (b.total ? " · " + money(b.total) : "")));
        var items = el("div", "items"); b.items.forEach(function (i) { items.appendChild(el("small", null, i.name + " × " + i.qty)); }); card.appendChild(items);
        card.appendChild(el("small", null, b.delivery === "pickup" ? "Самовывоз" : "Доставка"));
      } else {
        card.appendChild(el("b", null, "Заявка №" + b.id + " · " + b.service));
        card.appendChild(el("small", null, "Дата: " + b.date + ", " + b.time.toLowerCase()));
      }
      if (b.comment) card.appendChild(el("small", null, b.comment));
      box.appendChild(card);
    });
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
    if (Object.keys(errors).length) { setErrors("book-form", errors); haptic("medium"); return; }
    var btn = $("book-submit"); btn.disabled = true; btn.textContent = "Отправляем…";
    api("/max/api/booking", body).then(function (d) {
      btn.disabled = false; btn.textContent = "Отправить заявку";
      if (!d.ok) { setErrors("book-form", d.errors || { form: d.error || "Не удалось отправить заявку." }); return; }
      haptic("medium");
      $("done-title").textContent = "Заявка принята"; $("done-text").textContent = d.message;
      f.elements.comment.value = ""; f.elements.date.value = "";
      show("done");
    }).catch(function () { btn.disabled = false; btn.textContent = "Отправить заявку"; setErrors("book-form", { form: "Нет связи с сервером. Попробуйте ещё раз." }); });
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

  $("botchat-open").addEventListener("click", function () {
    try { if (W && W.close && initData) { W.close(); return; } } catch (e) { /* не внутри MAX */ }
    if (state.data && state.data.bot_username) openLink("https://max.ru/" + state.data.bot_username);
    else alert("Закройте приложение и напишите боту в чат.");
  });

  // ---------------------------------------------------------------- переходы
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-go]"); if (!b) return;
    show(b.getAttribute("data-go"));
  });
  $("retry").addEventListener("click", load);
  load();
})();
