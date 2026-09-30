// Обозреватель 1С: поиск объектов и предпросмотр записей.
(function () {
  "use strict";
  var root = document.getElementById("onec");
  if (!root) return;
  var conn = root.dataset.conn;
  var current = "";

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = text;
    if (cls) e.className = cls;
    return e;
  }

  document.getElementById("onec-search").addEventListener("input", function (ev) {
    var q = ev.target.value.trim().toLowerCase();
    document.querySelectorAll(".onec-group").forEach(function (g) {
      var shown = 0;
      g.querySelectorAll("li").forEach(function (li) {
        var name = li.querySelector("button").dataset.entity.toLowerCase();
        var ok = !q || name.indexOf(q) >= 0;
        li.hidden = !ok;
        if (ok) shown++;
      });
      g.hidden = shown === 0;
      if (q && shown) g.open = true;
    });
  });

  document.querySelectorAll(".onec-entity").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".onec-entity.active").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      current = b.dataset.entity;
      document.getElementById("onec-empty").hidden = true;
      document.getElementById("onec-panel").hidden = false;
      document.getElementById("onec-entity-name").textContent = current;
      document.getElementById("onec-filter").value = "";
      document.getElementById("onec-select").value = "";
      load();
    });
  });

  document.getElementById("onec-load").addEventListener("click", load);
  document.getElementById("onec-copy").addEventListener("click", function (ev) {
    var b = ev.target, text = document.getElementById("onec-entity-name").textContent;
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text);
    else { var ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); } catch (e) { /* */ } document.body.removeChild(ta); }
    b.textContent = "Скопировано";
    setTimeout(function () { b.textContent = "Копировать имя"; }, 1500);
  });

  function load() {
    var entity = document.getElementById("onec-entity-name").textContent;
    var params = new URLSearchParams({
      entity: entity,
      filter: document.getElementById("onec-filter").value,
      select: document.getElementById("onec-select").value,
      top: document.getElementById("onec-top").value || "20"
    });
    var status = document.getElementById("onec-status");
    var box = document.getElementById("onec-result");
    status.textContent = "Загружаем из 1С…";
    box.textContent = "";
    fetch("/api/onec/" + conn + "/preview?" + params.toString(), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) {
          status.textContent = "";
          var err = el("div", d.error, "flash flash-error");
          box.appendChild(err);
          if (d.action) box.appendChild(el("div", "Что делать: " + d.action, "todo"));
          return;
        }
        status.textContent = "Записей: " + d.records.length + " · " + d.ms + " мс";
        if (!d.records.length) { box.appendChild(el("p", "Записей нет (или все отфильтрованы).", "muted")); return; }
        var cols = [];
        d.records.forEach(function (r) { Object.keys(r).forEach(function (k) { if (cols.indexOf(k) < 0 && cols.length < 40) cols.push(k); }); });
        var wrap = el("div", null, "onec-table-wrap");
        var t = el("table", null, "table onec-table");
        var tr = el("tr");
        cols.forEach(function (c) { tr.appendChild(el("th", c)); });
        var thead = el("thead"); thead.appendChild(tr); t.appendChild(thead);
        var tb = el("tbody");
        d.records.forEach(function (r) {
          var row = el("tr");
          cols.forEach(function (c) {
            var v = r[c];
            var text = v === undefined ? "" : v === null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
            var td = el("td", text.length > 80 ? text.slice(0, 79) + "…" : text);
            if (text.length > 80) td.title = text.slice(0, 2000);
            row.appendChild(td);
          });
          tb.appendChild(row);
        });
        t.appendChild(tb); wrap.appendChild(t); box.appendChild(wrap);
      })
      .catch(function () { status.textContent = "Нет связи с панелью. Обновите страницу."; });
  }
})();
