// Визуальный редактор сценариев (аналог редактора n8n). Без внешних библиотек.
(function () {
  "use strict";

  var root = document.getElementById("wf-editor");
  if (!root) return;

  var SVGNS = "http://www.w3.org/2000/svg";
  var NODE_W = 210, NODE_MIN_H = 64, NOTE_W = 260, NOTE_H = 140;

  var S = {
    id: root.dataset.id,
    csrf: root.dataset.csrf,
    publicUrl: (root.dataset.publicUrl || "").replace(/\/$/, ""),
    localUrl: (root.dataset.localUrl || "").replace(/\/$/, ""),
    wf: null,
    types: {},
    groups: [],
    lookup: { connections: [], workflows: [], variables: [] },
    view: { x: 80, y: 80, k: 1 },
    selected: new Set(),
    selectedEdge: -1,
    dirty: false,
    history: [],
    future: [],
    exec: null,
    panelNode: null,
    panelTab: "params",
    readonly: false,
    lastInput: null,
    clipboard: null,
    pendingFrom: null
  };

  var $ = function (id) { return document.getElementById(id); };
  var svg = $("wf-canvas"), viewport = $("wf-viewport"), gNodes = $("wf-nodes"), gEdges = $("wf-edges");
  var tempEdge = $("wf-temp-edge"), selectBox = $("wf-select-box");

  // ------------------------------------------------------------- помощники

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }

  function s(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") el.textContent = attrs[k];
      else el.setAttribute(k, attrs[k]);
    });
    return el;
  }

  function api(method, url, body) {
    var opts = { method: method, headers: { "X-CSRF-Token": S.csrf }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || ("Ошибка " + r.status));
        return data;
      });
    });
  }

  function uid() { return "n" + Math.random().toString(16).slice(2, 10); }
  function clone(x) { return JSON.parse(JSON.stringify(x)); }
  function nodeById(id) { return S.wf.nodes.find(function (n) { return n.id === id; }); }
  function typeOf(n) { return S.types[n.type]; }

  function outputsOf(n) {
    var t = typeOf(n);
    if (!t) return ["выход"];
    if (t.key === "switch") {
      var rules = (n.params && n.params.rules) || [];
      return rules.map(function (r, i) { return r.output || ("Правило " + (i + 1)); }).concat(["иначе"]);
    }
    return t.outputs;
  }
  function inputsOf(n) { var t = typeOf(n); return t ? t.inputs : 1; }
  function isNote(n) { return n.type === "note"; }
  function nodeH(n) {
    if (isNote(n)) return NOTE_H;
    return Math.max(NODE_MIN_H, Math.max(outputsOf(n).length, inputsOf(n)) * 26 + 14);
  }
  function nodeW(n) { return isNote(n) ? NOTE_W : NODE_W; }

  function portPos(n, kind, idx) {
    var count = kind === "in" ? inputsOf(n) : outputsOf(n).length;
    var hh = nodeH(n);
    var y = n.position[1] + hh * (idx + 1) / (count + 1);
    return { x: n.position[0] + (kind === "in" ? 0 : nodeW(n)), y: y };
  }

  function edgePath(a, b) {
    var dx = Math.max(40, Math.abs(b.x - a.x) / 2);
    if (b.x < a.x + 20) dx = 80;
    return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " " + (b.x - dx) + "," + b.y + " " + b.x + "," + b.y;
  }

  function status(text, kind, sticky) {
    var el = $("wf-status");
    el.textContent = "";
    el.className = "wf-status " + (kind || "");
    if (typeof text === "string") el.textContent = text; else if (text) el.appendChild(text);
    el.hidden = !text;
    clearTimeout(status._t);
    if (text && !sticky) status._t = setTimeout(function () { el.hidden = true; }, 6000);
  }

  function banner(text, kind) {
    var el = $("wf-banner");
    el.textContent = "";
    if (!text) { el.hidden = true; return; }
    el.className = "wf-banner " + (kind || "");
    if (typeof text === "string") el.textContent = text; else el.appendChild(text);
    el.hidden = false;
  }

  function markDirty() {
    if (S.readonly) return;
    S.dirty = true;
    $("wf-saved").textContent = "Не сохранено";
    $("wf-saved").classList.add("dirty");
  }

  function pushHistory() {
    if (S.readonly) return;
    S.history.push(JSON.stringify({ nodes: S.wf.nodes, connections: S.wf.connections }));
    if (S.history.length > 100) S.history.shift();
    S.future = [];
  }

  function restore(snap) {
    var d = JSON.parse(snap);
    S.wf.nodes = d.nodes; S.wf.connections = d.connections;
    S.selected.clear(); S.selectedEdge = -1;
    if (S.panelNode && !nodeById(S.panelNode)) closePanel();
    markDirty(); render(); if (S.panelNode) renderPanel();
  }

  function undo() {
    if (!S.history.length) return;
    S.future.push(JSON.stringify({ nodes: S.wf.nodes, connections: S.wf.connections }));
    restore(S.history.pop());
  }
  function redo() {
    if (!S.future.length) return;
    S.history.push(JSON.stringify({ nodes: S.wf.nodes, connections: S.wf.connections }));
    restore(S.future.pop());
  }

  // --------------------------------------------------------------- отрисовка

  function applyView() {
    viewport.setAttribute("transform", "translate(" + S.view.x + "," + S.view.y + ") scale(" + S.view.k + ")");
  }

  function runOf(nodeId) {
    if (!S.exec || !S.exec.run_data) return null;
    var runs = S.exec.run_data[nodeId];
    return runs && runs.length ? runs[runs.length - 1] : null;
  }

  function render() {
    gNodes.textContent = "";
    gEdges.textContent = "";
    S.wf.connections.forEach(function (e, i) {
      var a = nodeById(e.from), b = nodeById(e.to);
      if (!a || !b) return;
      var p1 = portPos(a, "out", e.out), p2 = portPos(b, "in", e.in);
      var d = edgePath(p1, p2);
      var run = runOf(a.id);
      var cls = "wf-edge" + (i === S.selectedEdge ? " selected" : "");
      if (run && run.outputs && run.outputs[e.out] && run.outputs[e.out].count) cls += " done";
      var hit = s("path", { d: d, class: "wf-edge-hit", "data-edge": i });
      var path = s("path", { d: d, class: cls, "marker-end": "url(#wf-arrow)", "data-edge": i });
      gEdges.appendChild(path);
      gEdges.appendChild(hit);
      if (run && run.outputs && run.outputs[e.out]) {
        var mid = { x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 - 8 };
        var c = run.outputs[e.out].count;
        gEdges.appendChild(s("text", { x: mid.x, y: mid.y, class: "wf-edge-count", "text-anchor": "middle",
                                       text: c + " " + plural(c, "элемент", "элемента", "элементов") }));
      }
    });
    S.wf.nodes.forEach(function (n) { gNodes.appendChild(renderNode(n)); });
    applyView();
  }

  function plural(n, a, b, c) {
    var m = n % 10, mm = n % 100;
    if (m === 1 && mm !== 11) return a;
    if (m >= 2 && m <= 4 && (mm < 10 || mm >= 20)) return b;
    return c;
  }

  function renderNode(n) {
    var t = typeOf(n);
    var g = s("g", { class: "wf-node" + (S.selected.has(n.id) ? " selected" : "") + (n.disabled ? " disabled" : "") +
                            (isNote(n) ? " note" : "") + (!t ? " unknown" : ""),
                     transform: "translate(" + n.position[0] + "," + n.position[1] + ")", "data-node": n.id });
    var w = nodeW(n), hh = nodeH(n);
    if (isNote(n)) {
      g.appendChild(s("rect", { width: w, height: hh, rx: 8, class: "wf-note-bg" }));
      var fo = s("foreignObject", { x: 10, y: 8, width: w - 20, height: hh - 16 });
      var div = document.createElement("div");
      div.className = "wf-note-text";
      div.textContent = (n.params && n.params.text) || "";
      fo.appendChild(div);
      g.appendChild(fo);
      return g;
    }
    var run = runOf(n.id);
    var running = S.exec && S.exec.running && S.exec.current_node === n.id;
    var stateCls = running ? " running" : run ? (" " + run.status) : "";
    g.appendChild(s("rect", { width: w, height: hh, rx: 10, class: "wf-node-bg" + stateCls }));
    g.appendChild(s("rect", { x: 0, y: 0, width: 6, height: hh, rx: 3, fill: (t && t.color) || "#94a3b8", class: "wf-node-stripe" }));
    g.appendChild(s("rect", { x: 14, y: hh / 2 - 17, width: 34, height: 34, rx: 8, fill: (t && t.color) || "#94a3b8", class: "wf-node-iconbg" }));
    g.appendChild(s("text", { x: 31, y: hh / 2 + 5, "text-anchor": "middle", class: "wf-node-icon", text: (t && t.icon) || "?" }));
    g.appendChild(s("title", { text: n.name + (t ? " — " + t.title : "") }));
    g.appendChild(s("text", { x: 58, y: hh / 2 - 3, class: "wf-node-title", text: clip(n.name, 16) }));
    g.appendChild(s("text", { x: 58, y: hh / 2 + 14, class: "wf-node-sub",
                              text: clip(t ? t.title : "не поддерживается", 21) + (n.disabled ? " · откл." : "") }));
    if (t && t.trigger) g.appendChild(s("path", { d: "M-2," + (hh / 2 - 8) + " l-8,8 l8,8", class: "wf-trigger-mark" }));
    for (var i = 0; i < inputsOf(n); i++) {
      var pin = portPos({ position: [0, 0], type: n.type, params: n.params }, "in", i);
      g.appendChild(s("circle", { cx: pin.x, cy: pin.y, r: 7, class: "wf-port in", "data-port": "in", "data-node": n.id, "data-idx": i }));
      if (inputsOf(n) > 1) g.appendChild(s("text", { x: 10, y: pin.y - 9, class: "wf-port-label", text: "вход " + (i + 1) }));
    }
    var outs = outputsOf(n);
    outs.forEach(function (label, idx) {
      var p = portPos({ position: [0, 0], type: n.type, params: n.params }, "out", idx);
      g.appendChild(s("circle", { cx: p.x, cy: p.y, r: 7, class: "wf-port out", "data-port": "out", "data-node": n.id, "data-idx": idx }));
      if (label && outs.length > 1) g.appendChild(s("text", { x: p.x + 11, y: p.y + 4, class: "wf-port-label", text: clip(label, 12) }));
    });
    if (run) {
      var badge = run.status === "success" ? "✓" : run.status === "error" ? "!" : "…";
      g.appendChild(s("circle", { cx: w - 12, cy: 12, r: 9, class: "wf-badge " + run.status }));
      g.appendChild(s("text", { x: w - 12, y: 16, "text-anchor": "middle", class: "wf-badge-text", text: badge }));
      var runs = S.exec.run_data[n.id].length;
      if (runs > 1) g.appendChild(s("text", { x: w - 26, y: 16, "text-anchor": "end", class: "wf-node-sub", text: "×" + runs }));
    }
    return g;
  }

  function clip(text, max) {
    text = String(text || "");
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // ----------------------------------------------------------- координаты

  function toWorld(ev) {
    var r = svg.getBoundingClientRect();
    return { x: (ev.clientX - r.left - S.view.x) / S.view.k, y: (ev.clientY - r.top - S.view.y) / S.view.k };
  }

  function centerWorld() {
    var r = svg.getBoundingClientRect();
    return { x: (r.width / 2 - S.view.x) / S.view.k, y: (r.height / 2 - S.view.y) / S.view.k };
  }

  function fit() {
    if (!S.wf.nodes.length) { S.view = { x: 80, y: 80, k: 1 }; applyView(); return; }
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    S.wf.nodes.forEach(function (n) {
      minX = Math.min(minX, n.position[0]); minY = Math.min(minY, n.position[1]);
      maxX = Math.max(maxX, n.position[0] + nodeW(n) + 90); maxY = Math.max(maxY, n.position[1] + nodeH(n));
    });
    var r = svg.getBoundingClientRect();
    var k = Math.min(1.2, Math.max(0.25, Math.min((r.width - 120) / (maxX - minX || 1), (r.height - 120) / (maxY - minY || 1))));
    S.view.k = k;
    S.view.x = (r.width - (maxX - minX) * k) / 2 - minX * k;
    S.view.y = (r.height - (maxY - minY) * k) / 2 - minY * k;
    applyView();
  }

  function zoom(factor, cx, cy) {
    var r = svg.getBoundingClientRect();
    if (cx === undefined) { cx = r.width / 2; cy = r.height / 2; }
    var k = Math.min(2.5, Math.max(0.2, S.view.k * factor));
    S.view.x = cx - (cx - S.view.x) * (k / S.view.k);
    S.view.y = cy - (cy - S.view.y) * (k / S.view.k);
    S.view.k = k;
    applyView();
  }

  // --------------------------------------------------------------- мышь

  var drag = null;

  svg.addEventListener("mousedown", function (ev) {
    if (ev.button !== 0) return;
    svg.focus();
    var t = ev.target;
    var port = t.closest && t.closest("[data-port]");
    var nodeEl = t.closest && t.closest("g[data-node]");
    var edgeEl = t.closest && t.closest("[data-edge]");
    var w = toWorld(ev);
    if (port && !S.readonly && port.dataset.port === "out") {
      var n = nodeById(port.dataset.node);
      drag = { kind: "connect", from: n.id, out: +port.dataset.idx, start: portPos(n, "out", +port.dataset.idx), moved: false };
      ev.preventDefault();
      return;
    }
    if (port && !S.readonly && port.dataset.port === "in") {
      // потянуть связь от входа — снять её и переподключить
      var nid = port.dataset.node, idx = +port.dataset.idx;
      var ei = S.wf.connections.findIndex(function (e) { return e.to === nid && e.in === idx; });
      if (ei >= 0) {
        pushHistory();
        var e = S.wf.connections.splice(ei, 1)[0];
        var fromN = nodeById(e.from);
        drag = { kind: "connect", from: e.from, out: e.out, start: portPos(fromN, "out", e.out), moved: true };
        markDirty(); render();
        ev.preventDefault();
      }
      return;
    }
    if (nodeEl) {
      var id = nodeEl.dataset.node;
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
        if (S.selected.has(id)) S.selected.delete(id); else S.selected.add(id);
      } else if (!S.selected.has(id)) {
        S.selected.clear(); S.selected.add(id);
      }
      S.selectedEdge = -1;
      var starts = {};
      S.selected.forEach(function (sid) { var sn = nodeById(sid); if (sn) starts[sid] = sn.position.slice(); });
      drag = { kind: "move", id: id, origin: w, starts: starts, moved: false };
      render();
      ev.preventDefault();
      return;
    }
    if (edgeEl) {
      S.selectedEdge = +edgeEl.dataset.edge;
      S.selected.clear();
      render();
      return;
    }
    S.selectedEdge = -1;
    if (ev.shiftKey) {
      drag = { kind: "box", origin: w, screen: { x: ev.clientX, y: ev.clientY } };
    } else {
      if (S.selected.size) { S.selected.clear(); render(); }
      drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, vx: S.view.x, vy: S.view.y };
      svg.classList.add("panning");
    }
  });

  window.addEventListener("mousemove", function (ev) {
    if (!drag) return;
    var w = toWorld(ev);
    if (drag.kind === "pan") {
      S.view.x = drag.vx + ev.clientX - drag.sx;
      S.view.y = drag.vy + ev.clientY - drag.sy;
      applyView();
    } else if (drag.kind === "move" && !S.readonly) {
      var dx = w.x - drag.origin.x, dy = w.y - drag.origin.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; pushHistory(); }
      if (drag.moved) {
        Object.keys(drag.starts).forEach(function (id) {
          var n = nodeById(id);
          n.position = [Math.round((drag.starts[id][0] + dx) / 10) * 10, Math.round((drag.starts[id][1] + dy) / 10) * 10];
        });
        render();
      }
    } else if (drag.kind === "connect") {
      drag.moved = true;
      tempEdge.setAttribute("d", edgePath(drag.start, w));
    } else if (drag.kind === "box") {
      var r = svg.getBoundingClientRect();
      var x1 = Math.min(drag.screen.x, ev.clientX) - r.left, y1 = Math.min(drag.screen.y, ev.clientY) - r.top;
      selectBox.setAttribute("x", x1); selectBox.setAttribute("y", y1);
      selectBox.setAttribute("width", Math.abs(ev.clientX - drag.screen.x));
      selectBox.setAttribute("height", Math.abs(ev.clientY - drag.screen.y));
      selectBox.removeAttribute("hidden");
      drag.end = w;
    }
  });

  window.addEventListener("mouseup", function (ev) {
    if (!drag) return;
    var d = drag;
    drag = null;
    svg.classList.remove("panning");
    if (d.kind === "connect") {
      tempEdge.setAttribute("d", "");
      var target = document.elementFromPoint(ev.clientX, ev.clientY);
      var port = target && target.closest && target.closest("[data-port='in']");
      var nodeEl = target && target.closest && target.closest("g[data-node]");
      if (!port && nodeEl && nodeEl.dataset.node !== d.from) {
        var tn = nodeById(nodeEl.dataset.node);
        if (tn && inputsOf(tn) > 0) port = { dataset: { node: tn.id, idx: firstFreeInput(tn) } };
      }
      if (port) {
        connect(d.from, d.out, port.dataset.node, +port.dataset.idx);
      } else if (!d.moved || !nodeEl) {
        S.pendingFrom = { from: d.from, out: d.out, at: toWorld(ev) };
        openPicker();
      }
    } else if (d.kind === "move") {
      if (d.moved) markDirty();
      else if (!(ev.shiftKey || ev.ctrlKey || ev.metaKey)) openPanel(d.id);
    } else if (d.kind === "box") {
      selectBox.setAttribute("hidden", "");
      if (d.end) {
        var x1 = Math.min(d.origin.x, d.end.x), x2 = Math.max(d.origin.x, d.end.x);
        var y1 = Math.min(d.origin.y, d.end.y), y2 = Math.max(d.origin.y, d.end.y);
        S.wf.nodes.forEach(function (n) {
          if (n.position[0] >= x1 && n.position[0] + nodeW(n) <= x2 && n.position[1] >= y1 && n.position[1] + nodeH(n) <= y2) S.selected.add(n.id);
        });
        render();
      }
    }
  });

  function firstFreeInput(n) {
    for (var i = 0; i < inputsOf(n); i++) {
      if (!S.wf.connections.some(function (e) { return e.to === n.id && e.in === i; })) return i;
    }
    return 0;
  }

  function connect(from, out, to, inp) {
    if (from === to) return;
    var tn = nodeById(to);
    if (!tn || inputsOf(tn) === 0) { status("У этого узла нет входа: это узел запуска.", "warn"); return; }
    if (S.wf.connections.some(function (e) { return e.from === from && e.out === out && e.to === to && e.in === inp; })) return;
    pushHistory();
    S.wf.connections.push({ from: from, out: out, to: to, in: inp });
    markDirty(); render();
  }

  svg.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var r = svg.getBoundingClientRect();
    if (ev.ctrlKey || Math.abs(ev.deltaY) >= 30 || ev.deltaMode) {
      zoom(ev.deltaY < 0 ? 1.12 : 1 / 1.12, ev.clientX - r.left, ev.clientY - r.top);
    } else {
      S.view.x -= ev.deltaX; S.view.y -= ev.deltaY; applyView();
    }
  }, { passive: false });

  svg.addEventListener("dblclick", function (ev) {
    var nodeEl = ev.target.closest && ev.target.closest("g[data-node]");
    if (nodeEl) { openPanel(nodeEl.dataset.node); return; }
    if (!S.readonly) { S.pendingFrom = { at: toWorld(ev) }; openPicker(); }
  });

  // ------------------------------------------------------------ клавиатура

  function typing(ev) {
    var t = ev.target;
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  document.addEventListener("keydown", function (ev) {
    var mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key.toLowerCase() === "s" || mod && ev.code === "KeyS") { ev.preventDefault(); save(); return; }
    if (mod && ev.key === "Enter") { ev.preventDefault(); run(); return; }
    if (ev.key === "Escape") {
      if (!$("wf-picker").hidden) closePicker(); else if (!$("wf-settings").hidden) $("wf-settings").hidden = true; else closePanel();
      return;
    }
    if (typing(ev) || S.readonly) return;
    if ((ev.key === "Delete" || ev.key === "Backspace")) { ev.preventDefault(); deleteSelected(); }
    else if (mod && (ev.code === "KeyZ") && !ev.shiftKey) { ev.preventDefault(); undo(); }
    else if (mod && (ev.code === "KeyY" || (ev.code === "KeyZ" && ev.shiftKey))) { ev.preventDefault(); redo(); }
    else if (mod && ev.code === "KeyC") copySelected();
    else if (mod && ev.code === "KeyV") paste();
    else if (mod && ev.code === "KeyA") { ev.preventDefault(); S.wf.nodes.forEach(function (n) { S.selected.add(n.id); }); render(); }
    else if (mod && ev.code === "KeyD") { ev.preventDefault(); copySelected(); paste(); }
    else if (ev.key === "Tab") { ev.preventDefault(); S.pendingFrom = null; openPicker(); }
    else if (ev.key === "1") fit();
    else if (ev.key === "d" || ev.key === "в") toggleDisabled();
  });

  function deleteSelected() {
    if (S.selectedEdge >= 0) {
      pushHistory();
      S.wf.connections.splice(S.selectedEdge, 1);
      S.selectedEdge = -1;
      markDirty(); render();
      return;
    }
    if (!S.selected.size) return;
    pushHistory();
    S.wf.nodes = S.wf.nodes.filter(function (n) { return !S.selected.has(n.id); });
    S.wf.connections = S.wf.connections.filter(function (e) { return !S.selected.has(e.from) && !S.selected.has(e.to); });
    if (S.panelNode && S.selected.has(S.panelNode)) closePanel();
    S.selected.clear();
    markDirty(); render();
  }

  function toggleDisabled() {
    if (!S.selected.size) return;
    pushHistory();
    S.selected.forEach(function (id) { var n = nodeById(id); if (n) n.disabled = !n.disabled; });
    markDirty(); render();
  }

  function copySelected() {
    if (!S.selected.size) return;
    var nodes = S.wf.nodes.filter(function (n) { return S.selected.has(n.id); });
    var edges = S.wf.connections.filter(function (e) { return S.selected.has(e.from) && S.selected.has(e.to); });
    S.clipboard = clone({ nodes: nodes, edges: edges });
    try { localStorage.setItem("wf-clipboard", JSON.stringify(S.clipboard)); } catch (e) { /* нет хранилища */ }
    status("Скопировано узлов: " + nodes.length);
  }

  function paste() {
    var clip = S.clipboard;
    if (!clip) { try { clip = JSON.parse(localStorage.getItem("wf-clipboard") || "null"); } catch (e) { clip = null; } }
    if (!clip || !clip.nodes || !clip.nodes.length) return;
    pushHistory();
    var map = {};
    S.selected.clear();
    clip.nodes.forEach(function (n) {
      var copy = clone(n);
      map[n.id] = copy.id = uid();
      copy.name = uniqueName(n.name);
      copy.position = [n.position[0] + 40, n.position[1] + 40];
      S.wf.nodes.push(copy);
      S.selected.add(copy.id);
    });
    clip.edges.forEach(function (e) { S.wf.connections.push({ from: map[e.from], out: e.out, to: map[e.to], in: e.in }); });
    markDirty(); render();
  }

  function uniqueName(base) {
    var names = new Set(S.wf.nodes.map(function (n) { return n.name; }));
    if (!names.has(base)) return base;
    var i = 2;
    base = base.replace(/ \d+$/, "");
    while (names.has(base + " " + i)) i++;
    return base + " " + i;
  }

  // ------------------------------------------------------ выбор нового узла

  function openPicker() {
    if (S.readonly) return;
    $("wf-picker").hidden = false;
    var inp = $("wf-picker-search");
    inp.value = "";
    renderPicker("");
    setTimeout(function () { inp.focus(); }, 10);
  }
  function closePicker() { $("wf-picker").hidden = true; S.pendingFrom = null; }

  function renderPicker(q) {
    var list = $("wf-picker-list");
    list.textContent = "";
    q = q.trim().toLowerCase();
    var first = null;
    S.groups.forEach(function (g) {
      var items = g.nodes.filter(function (t) {
        if (S.pendingFrom && S.pendingFrom.from && t.trigger) return false;
        return !q || (t.title + " " + t.description + " " + t.key + " " + g.title).toLowerCase().indexOf(q) >= 0;
      });
      if (!items.length) return;
      list.appendChild(h("div", { class: "wf-picker-group", text: g.title }));
      items.forEach(function (t) {
        var btn = h("button", { type: "button", class: "wf-picker-item", onclick: function () { addNode(t.key); } }, [
          h("span", { class: "wf-picker-icon", text: t.icon }),
          h("span", {}, [h("b", { text: t.title }), h("small", { text: t.description })])
        ]);
        btn.querySelector(".wf-picker-icon").style.background = t.color;
        if (!first) first = btn;
        list.appendChild(btn);
      });
    });
    if (!first) list.appendChild(h("p", { class: "muted", text: "Ничего не найдено. Для любого сервиса с API подойдёт «HTTP-запрос»." }));
  }

  $("wf-picker-search").addEventListener("input", function (ev) { renderPicker(ev.target.value); });
  $("wf-picker-search").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { var b = $("wf-picker-list").querySelector(".wf-picker-item"); if (b) b.click(); }
  });

  function defaultsFor(t) {
    var p = {};
    t.params.forEach(function (x) { if (x.kind !== "notice") p[x.name] = clone(x.default === undefined ? null : x.default); });
    return p;
  }

  function addNode(key) {
    var t = S.types[key];
    var pending = S.pendingFrom;
    closePicker();
    pushHistory();
    var pos;
    if (pending && pending.from) {
      var src = nodeById(pending.from);
      pos = [src.position[0] + 280, src.position[1] + pending.out * 90];
    } else if (pending && pending.at) {
      pos = [pending.at.x, pending.at.y];
    } else {
      var c = centerWorld();
      var last = S.wf.nodes.filter(function (n) { return !isNote(n); }).slice(-1)[0];
      pos = last ? [last.position[0] + 280, last.position[1]] : [c.x - 100, c.y - 30];
    }
    var n = { id: uid(), type: key, name: uniqueName(t.title), position: [Math.round(pos[0] / 10) * 10, Math.round(pos[1] / 10) * 10],
              params: defaultsFor(t), disabled: false, settings: {} };
    S.wf.nodes.push(n);
    if (pending && pending.from && t.inputs > 0) S.wf.connections.push({ from: pending.from, out: pending.out, to: n.id, in: 0 });
    else if (!pending && t.inputs > 0 && S.selected.size === 1) {
      var sel = nodeById(Array.from(S.selected)[0]);
      if (sel && outputsOf(sel).length) {
        n.position = [sel.position[0] + 280, sel.position[1]];
        S.wf.connections.push({ from: sel.id, out: 0, to: n.id, in: 0 });
      }
    }
    S.selected.clear(); S.selected.add(n.id);
    markDirty(); render();
    openPanel(n.id);
  }

  // ------------------------------------------------------------ панель узла

  function openPanel(id) {
    S.panelNode = id;
    $("wf-panel").hidden = false;
    if (!S.selected.has(id)) { S.selected.clear(); S.selected.add(id); render(); }
    renderPanel();
    ensureVisible(nodeById(id));
  }

  function ensureVisible(n) {
    if (!n) return;
    var r = svg.getBoundingClientRect();
    var x1 = n.position[0] * S.view.k + S.view.x, x2 = (n.position[0] + nodeW(n) + 60) * S.view.k + S.view.x;
    var y1 = n.position[1] * S.view.k + S.view.y, y2 = (n.position[1] + nodeH(n)) * S.view.k + S.view.y;
    if (x2 > r.width) S.view.x -= x2 - r.width + 20;
    if (x1 < 0) S.view.x += -x1 + 40;
    if (y2 > r.height) S.view.y -= y2 - r.height + 20;
    if (y1 < 0) S.view.y += -y1 + 40;
    applyView();
  }
  function closePanel() { S.panelNode = null; $("wf-panel").hidden = true; }

  document.querySelectorAll(".wf-tab").forEach(function (b) {
    b.addEventListener("click", function () { S.panelTab = b.dataset.tab; renderPanel(); });
  });
  $("wf-panel-close").addEventListener("click", closePanel);

  var nameInput = $("wf-node-name");
  nameInput.addEventListener("change", function () {
    var n = nodeById(S.panelNode);
    if (!n) return;
    var newName = nameInput.value.trim();
    if (!newName || newName === n.name) { nameInput.value = n.name; return; }
    if (S.wf.nodes.some(function (x) { return x.id !== n.id && x.name === newName; })) {
      status("Узел с таким названием уже есть.", "warn"); nameInput.value = n.name; return;
    }
    pushHistory();
    renameRefs(n.name, newName);
    n.name = newName;
    markDirty(); render();
  });

  function renameRefs(oldName, newName) {
    var pats = ["$('" + oldName + "')", '$("' + oldName + '")', "$node['" + oldName + "']", '$node["' + oldName + '"]'];
    var reps = ["$('" + newName + "')", '$("' + newName + '")', "$node['" + newName + "']", '$node["' + newName + '"]'];
    function walk(v) {
      if (typeof v === "string") { pats.forEach(function (p, i) { v = v.split(p).join(reps[i]); }); return v; }
      if (Array.isArray(v)) return v.map(walk);
      if (v && typeof v === "object") { var o = {}; Object.keys(v).forEach(function (k) { o[k] = walk(v[k]); }); return o; }
      return v;
    }
    S.wf.nodes.forEach(function (n) { n.params = walk(n.params || {}); });
  }

  function renderPanel() {
    var n = nodeById(S.panelNode);
    if (!n) { closePanel(); return; }
    nameInput.value = n.name;
    nameInput.disabled = S.readonly;
    document.querySelectorAll(".wf-tab").forEach(function (b) { b.classList.toggle("active", b.dataset.tab === S.panelTab); });
    var body = $("wf-panel-body");
    body.textContent = "";
    var t = typeOf(n);
    if (S.panelTab === "params") renderParams(body, n, t);
    else if (S.panelTab === "data") renderData(body, n);
    else renderNodeSettings(body, n);
    $("wf-run-to").hidden = S.readonly || isNote(n);
  }

  function visible(p, params) {
    if (!p.show_if) return true;
    return Object.keys(p.show_if).every(function (k) {
      var v = params[k];
      return p.show_if[k].some(function (x) { return x === v || String(x) === String(v); });
    });
  }

  function onParamChange(n, name, value, rerender) {
    if (S.readonly) return;
    if (!onParamChange.pushed) { pushHistory(); onParamChange.pushed = true; setTimeout(function () { onParamChange.pushed = false; }, 800); }
    n.params = n.params || {};
    n.params[name] = value;
    markDirty();
    if (rerender) { render(); renderPanel(); }
  }

  function renderParams(body, n, t) {
    if (!t) {
      body.appendChild(h("div", { class: "wf-error-box" }, [
        h("b", { text: "Этот узел не поддерживается." }),
        h("p", { text: "Он перенесён из n8n (" + ((n.params || {}).n8n_type || n.type) + "). Замените его подходящим узлом — часто подходит «HTTP-запрос» или «Код» — или отключите." })
      ]));
      body.appendChild(h("pre", { class: "wf-json", text: JSON.stringify((n.params || {}).original || n.params, null, 2) }));
      return;
    }
    body.appendChild(h("p", { class: "wf-desc", text: t.description }));
    if (n.type === "webhook") body.appendChild(webhookBlock(n));
    var params = Object.assign(defaultsFor(t), n.params || {});
    t.params.forEach(function (p) {
      if (!visible(p, params)) return;
      body.appendChild(paramField(n, p, params[p.name], t));
    });
    if (n.type === "schedule") body.appendChild(schedulePreview(n));
    if (n.type === "code") body.appendChild(h("p", { class: "hint", text: "Пример: for item in items: item['итого'] = item['цена'] * item['кол-во'] … return items. Код выполняется на сервере в отдельном процессе." }));
    if (!isNote(n)) body.appendChild(exprHelp());
  }

  function exprHelp() {
    return h("details", { class: "wf-expr-help" }, [
      h("summary", { text: "Как подставлять данные (выражения)" }),
      h("ul", {}, [
        h("li", {}, [h("code", { text: "{{ $json.поле }}" }), " — поле текущего элемента"]),
        h("li", {}, [h("code", { text: "{{ $('Имя узла').item.json.поле }}" }), " — поле из другого узла"]),
        h("li", {}, [h("code", { text: "{{ $now.toFormat('dd.MM.yyyy') }}" }), " — сегодняшняя дата"]),
        h("li", {}, [h("code", { text: "{{ $vars.ИМЯ }}" }), " — общая переменная"]),
        h("li", {}, [h("code", { text: "{{ $json.сумма * 1.2 }}" }), " — вычисления; ",
          h("code", { text: "{{ $json.имя.toUpperCase() }}" }), " — методы строк"]),
        h("li", { text: "На вкладке «Данные» щёлкните по полю входа — выражение вставится в последнее поле, где был курсор." })
      ])
    ]);
  }

  function paramField(n, p, value, t) {
    var wrap = h("div", { class: "field wf-field" });
    var label = h("label", { text: p.label + (p.required ? " *" : "") });
    wrap.appendChild(label);
    var ro = S.readonly;
    var input;
    if (p.kind === "boolean") {
      input = h("input", { type: "checkbox", checked: !!value, disabled: ro });
      input.addEventListener("change", function () { onParamChange(n, p.name, input.checked, true); });
      wrap.textContent = "";
      wrap.appendChild(h("label", { class: "check" }, [input, " " + p.label]));
    } else if (p.kind === "select") {
      input = h("select", { disabled: ro }, p.options.map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
      input.value = value === null || value === undefined ? "" : String(value);
      input.addEventListener("change", function () { onParamChange(n, p.name, input.value, true); });
      wrap.appendChild(input);
    } else if (p.kind === "connection") {
      var conns = S.lookup.connections.filter(function (c) { return !p.conn_types.length || p.conn_types.indexOf(c.type) >= 0; });
      input = h("select", { disabled: ro }, [h("option", { value: "", text: conns.length ? "— выберите —" : "— нет подходящих подключений —" })]
        .concat(conns.map(function (c) { return h("option", { value: String(c.id), text: c.name + (c.status === "error" ? " (ошибка связи)" : "") }); })));
      input.value = value ? String(value) : (conns.length === 1 ? String(conns[0].id) : "");
      if (!value && conns.length === 1) { n.params = n.params || {}; n.params[p.name] = String(conns[0].id); }
      input.addEventListener("change", function () { onParamChange(n, p.name, input.value, false); });
      wrap.appendChild(input);
      if (!conns.length) wrap.appendChild(h("div", { class: "hint" }, ["Сначала добавьте подключение в разделе ", h("a", { href: "/connections", target: "_blank", text: "«Подключения»" }), "."]));
    } else if (p.kind === "workflow") {
      var wfs = S.lookup.workflows.filter(function (w) { return String(w.id) !== String(S.id); });
      input = h("select", { disabled: ro }, [h("option", { value: "", text: "— выберите сценарий —" })]
        .concat(wfs.map(function (w) { return h("option", { value: String(w.id), text: w.name }); })));
      input.value = value ? String(value) : "";
      input.addEventListener("change", function () { onParamChange(n, p.name, input.value, false); });
      wrap.appendChild(input);
    } else if (p.kind === "list") {
      wrap.appendChild(listEditor(n, p, Array.isArray(value) ? value : []));
    } else {
      var multi = ["text", "code", "json"].indexOf(p.kind) >= 0;
      input = multi ? h("textarea", { rows: p.kind === "code" ? 14 : p.kind === "json" ? 7 : 4, spellcheck: "false", disabled: ro })
                    : h("input", { type: "text", spellcheck: "false", disabled: ro, placeholder: p.placeholder || "" });
      if (p.kind === "code" || p.kind === "json") input.classList.add("mono");
      input.value = value === null || value === undefined ? "" : (typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));
      if (p.placeholder) input.setAttribute("placeholder", p.placeholder);
      input.addEventListener("input", function () {
        var v = input.value;
        if (p.kind === "number" && v.trim() !== "" && !/\{\{/.test(v) && !isNaN(Number(v.replace(",", ".")))) v = Number(v.replace(",", "."));
        onParamChange(n, p.name, v, false);
        updatePreview(n, input, previewEl);
      });
      input.addEventListener("focus", function () { S.lastInput = { el: input, node: n.id, name: p.name }; });
      if (p.kind === "code") input.addEventListener("keydown", tabInTextarea);
      wrap.appendChild(input);
      var previewEl = h("div", { class: "wf-preview", hidden: true });
      wrap.appendChild(previewEl);
      updatePreview(n, input, previewEl);
    }
    if (p.hint) wrap.appendChild(h("div", { class: "hint", text: p.hint }));
    return wrap;
  }

  function tabInTextarea(ev) {
    if (ev.key !== "Tab") return;
    ev.preventDefault();
    var el = ev.target, st = el.selectionStart;
    el.value = el.value.slice(0, st) + "    " + el.value.slice(el.selectionEnd);
    el.selectionStart = el.selectionEnd = st + 4;
    el.dispatchEvent(new Event("input"));
  }

  function listEditor(n, p, rows) {
    var box = h("div", { class: "wf-list" });
    var listPreview = h("div", { class: "wf-preview", hidden: true });
    var ro = S.readonly;
    function commit() { onParamChange(n, p.name, clone(rows), n.type === "switch"); }
    rows.forEach(function (row, ri) {
      var line = h("div", { class: "wf-list-row" });
      p.columns.forEach(function (col) {
        var el;
        if (col.kind === "select") {
          el = h("select", { disabled: ro, title: col.label }, col.options.map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
          el.value = row[col.name] || col.options[0][0];
          el.addEventListener("change", function () { row[col.name] = el.value; commit(); });
        } else {
          el = h("input", { type: "text", placeholder: col.placeholder || col.label, title: col.label, disabled: ro, spellcheck: "false" });
          el.value = row[col.name] === undefined || row[col.name] === null ? "" : String(row[col.name]);
          el.addEventListener("input", function () {
            row[col.name] = el.value; onParamChange(n, p.name, clone(rows), false); updatePreview(n, el, listPreview);
          });
          el.addEventListener("change", function () { if (n.type === "switch") commit(); });
          el.addEventListener("focus", function () { S.lastInput = { el: el, node: n.id, name: p.name, row: ri, col: col.name }; });
        }
        line.appendChild(el);
      });
      if (!ro) line.appendChild(h("button", { type: "button", class: "ghost small", title: "Удалить строку", text: "✕",
                                              onclick: function () { rows.splice(ri, 1); commit(); renderPanel(); } }));
      box.appendChild(line);
    });
    if (p.columns.length) {
      box.appendChild(h("div", { class: "wf-list-head" }, p.columns.map(function (c) { return h("small", { text: c.label }); })));
      box.insertBefore(box.lastChild, box.firstChild);
    }
    box.appendChild(listPreview);
    if (!ro) box.appendChild(h("button", { type: "button", class: "ghost small", text: "＋ Добавить строку", onclick: function () {
      var r = {};
      p.columns.forEach(function (c) { r[c.name] = c.kind === "select" ? c.options[0][0] : ""; });
      rows.push(r); commit(); renderPanel();
    } }));
    return box;
  }

  // --- предпросмотр выражений на данных последнего запуска
  function inputItemsFor(n) {
    var e = S.wf.connections.find(function (c) { return c.to === n.id && c.in === 0; });
    if (!e) {
      var own = runOf(n.id);
      return own && own.outputs && own.outputs[0] ? own.outputs[0].items : [];
    }
    var run = runOf(e.from);
    return run && run.outputs && run.outputs[e.out] ? run.outputs[e.out].items : [];
  }

  var previewTimer = null;
  function updatePreview(n, input, el) {
    if (!/\{\{[\s\S]*\}\}/.test(input.value || "")) { el.hidden = true; return; }
    clearTimeout(previewTimer);
    previewTimer = setTimeout(function () {
      var items = inputItemsFor(n).slice(0, 1);
      api("POST", "/api/expression-preview", { expr: input.value, items: items.length ? items : [{}],
                                                execution_id: S.exec && S.exec.id })
        .then(function (r) {
          el.hidden = false;
          el.className = "wf-preview " + (r.ok ? "ok" : "err");
          el.textContent = r.ok ? ("Результат: " + (typeof r.value === "string" ? r.value : JSON.stringify(r.value))).slice(0, 400)
                                : r.error;
          if (r.ok && !items.length) el.textContent += " (данных для проверки нет — выполните сценарий)";
        }).catch(function () { el.hidden = true; });
    }, 350);
  }

  function webhookBlock(n) {
    var path = ((n.params || {}).path || "").replace(/^\/+/, "");
    var base = S.publicUrl || S.localUrl;
    var prod = base + "/webhook/" + (path || "…");
    var test = base + "/webhook-test/" + (path || "…");
    var box = h("div", { class: "wf-webhook" }, [
      h("div", {}, [h("small", { text: "Рабочий адрес (сценарий включён):" }), copyLine(prod)]),
      h("div", {}, [h("small", { text: "Тестовый адрес (один вызов после кнопки ниже):" }), copyLine(test)])
    ]);
    if (!S.readonly) {
      box.appendChild(h("button", { type: "button", class: "ghost small", text: "Ждать тестовый вызов (2 минуты)", onclick: function () { listen(n); } }));
    }
    if (!S.publicUrl) box.appendChild(h("p", { class: "hint", text: "Внешний адрес панели не указан в «Настройках» — снаружи адрес будет другим." }));
    return box;
  }

  function copyLine(text) {
    return h("div", { class: "wf-copy" }, [h("code", { text: text }),
      h("button", { type: "button", class: "ghost small", text: "Копировать", onclick: function (ev) {
        var b = ev.target;
        (navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () {
          b.textContent = "Скопировано";
        }).catch(function () {
          var ta = h("textarea", { value: text }); document.body.appendChild(ta); ta.select();
          try { document.execCommand("copy"); b.textContent = "Скопировано"; } catch (e) { /* ничего */ }
          document.body.removeChild(ta);
        });
      } })]);
  }

  function schedulePreview(n) {
    var el = h("div", { class: "wf-preview ok", text: "…" });
    api("POST", "/api/cron-preview", { params: n.params || {} }).then(function (r) { el.textContent = r.text; })
      .catch(function () { el.hidden = true; });
    return el;
  }

  function renderNodeSettings(body, n) {
    var st = n.settings = n.settings || {};
    var ro = S.readonly;
    function flag(key, label, hint, rerender) {
      var cb = h("input", { type: "checkbox", checked: !!st[key], disabled: ro });
      cb.addEventListener("change", function () { pushHistory(); st[key] = cb.checked; markDirty(); if (rerender) renderPanel(); render(); });
      var wrap = h("div", { class: "field" }, [h("label", { class: "check" }, [cb, " " + label])]);
      if (hint) wrap.appendChild(h("div", { class: "hint", text: hint }));
      body.appendChild(wrap);
    }
    function num(key, label, def) {
      var inp = h("input", { type: "text", value: st[key] === undefined ? def : st[key], disabled: ro, inputmode: "numeric" });
      inp.addEventListener("input", function () { st[key] = Number(inp.value) || def; markDirty(); });
      body.appendChild(h("div", { class: "field" }, [h("label", { text: label }), inp]));
    }
    var dis = h("input", { type: "checkbox", checked: !!n.disabled, disabled: ro });
    dis.addEventListener("change", function () { pushHistory(); n.disabled = dis.checked; markDirty(); render(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { class: "check" }, [dis, " Узел отключён"]),
      h("div", { class: "hint", text: "Отключённый узел пропускается: данные проходят через него без изменений." })]));
    flag("retry", "Повторять при ошибке", "Полезно для сервисов, которые иногда отвечают «слишком много запросов».", true);
    if (st.retry) { num("max_tries", "Сколько раз пробовать", 3); num("wait_ms", "Пауза между попытками, миллисекунд", 1000); }
    flag("continue_on_fail", "Не останавливать сценарий при ошибке", "Вместо остановки узел выдаст элемент с полем «ошибка».");
    flag("always_output", "Всегда выдавать хотя бы один элемент", "Чтобы сценарий шёл дальше, даже если узел ничего не нашёл.");
    flag("execute_once", "Выполнить только для первого элемента");
    var notes = h("textarea", { rows: 3, disabled: ro, placeholder: "Заметка для себя: что делает узел" });
    notes.value = n.notes || "";
    notes.addEventListener("input", function () { n.notes = notes.value; markDirty(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { text: "Заметка" }), notes]));
  }

  // ------------------------------------------------------------ данные узла

  function renderData(body, n) {
    var run = runOf(n.id);
    if (!S.exec) {
      body.appendChild(h("p", { class: "muted", text: "Данных пока нет. Нажмите «Выполнить» — здесь появятся входные и выходные данные этого узла." }));
      return;
    }
    if (!run) {
      body.appendChild(h("p", { class: "muted", text: "В последнем запуске этот узел не выполнялся (до него не дошли данные)." }));
    }
    var inItems = inputItemsFor(n);
    body.appendChild(h("h3", { class: "wf-data-title", text: "Вход (" + inItems.length + ")" }));
    body.appendChild(h("p", { class: "hint", text: "Щёлкните по названию поля — выражение вставится в поле параметра, где был курсор." }));
    body.appendChild(itemsView(inItems, true));
    if (!run) return;
    if (run.status === "error") {
      body.appendChild(h("div", { class: "wf-error-box" }, [h("b", { text: "Ошибка: " + run.error }),
        run.hint ? h("p", { text: "Что делать: " + run.hint }) : null,
        run.details ? h("pre", { class: "wf-json", text: run.details }) : null]));
    }
    if (run.logs && run.logs.length) {
      body.appendChild(h("h3", { class: "wf-data-title", text: "Журнал узла" }));
      body.appendChild(h("pre", { class: "wf-json", text: run.logs.join("\n") }));
    }
    (run.outputs || []).forEach(function (o, i) {
      var names = outputsOf(n);
      body.appendChild(h("h3", { class: "wf-data-title", text: "Выход" + (names.length > 1 ? " «" + names[i] + "»" : "") + " (" + o.count + ")" +
                                 (run.ms !== undefined ? " · " + run.ms + " мс" : "") }));
      body.appendChild(itemsView(o.items, false));
      if (o.count > o.items.length) body.appendChild(h("p", { class: "hint", text: "Показаны первые " + o.items.length + " из " + o.count + "." }));
    });
  }

  var dataMode = "table";
  function itemsView(items, clickable) {
    var box = h("div", { class: "wf-items" });
    if (!items || !items.length) { box.appendChild(h("p", { class: "muted", text: "Нет элементов." })); return box; }
    var sw = h("div", { class: "wf-mode" }, ["table", "json"].map(function (m) {
      return h("button", { type: "button", class: "ghost small" + (dataMode === m ? " active" : ""), text: m === "table" ? "Таблица" : "JSON",
                           onclick: function () { dataMode = m; renderPanel(); } });
    }));
    box.appendChild(sw);
    if (dataMode === "json") { box.appendChild(h("pre", { class: "wf-json", text: JSON.stringify(items, null, 2) })); return box; }
    var cols = [];
    items.forEach(function (it) { Object.keys(it || {}).forEach(function (k) { if (cols.indexOf(k) < 0 && cols.length < 30) cols.push(k); }); });
    var table = h("table", { class: "table wf-table" });
    table.appendChild(h("thead", {}, [h("tr", {}, cols.map(function (c) {
      return h("th", {}, [clickable ? h("button", { type: "button", class: "link", text: c, title: "Вставить {{ $json." + c + " }}",
                                                    onclick: function () { insertExpr(fieldExpr(c)); } }) : c]);
    }))]));
    var tb = h("tbody");
    items.slice(0, 50).forEach(function (it) {
      tb.appendChild(h("tr", {}, cols.map(function (c) {
        var v = it ? it[c] : undefined;
        var text = v === undefined ? "" : v === null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v);
        return h("td", { text: clip(text, 120), title: text.length > 120 ? text.slice(0, 2000) : null });
      })));
    });
    table.appendChild(tb);
    box.appendChild(h("div", { class: "wf-table-wrap" }, [table]));
    return box;
  }

  function fieldExpr(key) {
    return /^[A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*$/.test(key) ? "{{ $json." + key + " }}" : "{{ $json['" + key.replace(/'/g, "\\'") + "'] }}";
  }

  function insertExpr(text) {
    var li = S.lastInput;
    if (!li || !document.body.contains(li.el) || S.readonly) {
      status("Сначала поставьте курсор в поле параметра на вкладке «Параметры», затем вернитесь и щёлкните по полю.", "warn");
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text);
      return;
    }
    var el = li.el, st = el.selectionStart || el.value.length;
    el.value = el.value.slice(0, st) + text + el.value.slice(el.selectionEnd || st);
    el.dispatchEvent(new Event("input"));
    status("Вставлено: " + text);
  }

  // ---------------------------------------------------------- настройки сценария

  function openSettings() {
    var body = $("wf-settings-body");
    body.textContent = "";
    var st = S.wf.settings = S.wf.settings || {};
    var ro = S.readonly;
    var sel = h("select", { disabled: ro }, [h("option", { value: "", text: "— не выбран —" })].concat(
      S.lookup.workflows.filter(function (w) { return String(w.id) !== String(S.id); })
        .map(function (w) { return h("option", { value: String(w.id), text: w.name }); })));
    sel.value = st.error_workflow_id ? String(st.error_workflow_id) : "";
    sel.addEventListener("change", function () { st.error_workflow_id = sel.value; markDirty(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { text: "Сценарий для ошибок" }), sel,
      h("div", { class: "hint", text: "Если этот сценарий упадёт, запустится выбранный (он должен начинаться с узла «При ошибке в сценарии»). Например — прислать сообщение админу." })]));
    var to = h("input", { type: "text", value: st.timeout_minutes || 30, disabled: ro, inputmode: "numeric" });
    to.addEventListener("input", function () { st.timeout_minutes = Number(to.value) || 30; markDirty(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { text: "Остановить, если выполняется дольше, минут" }), to]));
    var ss = h("input", { type: "checkbox", checked: st.save_success !== false, disabled: ro });
    ss.addEventListener("change", function () { st.save_success = ss.checked; markDirty(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { class: "check" }, [ss, " Сохранять данные успешных запусков"]),
      h("div", { class: "hint", text: "Для частых сценариев (каждую минуту) можно выключить, чтобы не раздувать базу. Ошибки сохраняются всегда." })]));
    var note = h("textarea", { rows: 3, disabled: ro });
    note.value = st.note || "";
    note.addEventListener("input", function () { st.note = note.value; markDirty(); });
    body.appendChild(h("div", { class: "field" }, [h("label", { text: "Описание сценария" }), note]));
    body.appendChild(h("p", {}, [h("a", { class: "button ghost small", href: "/workflows/" + S.id + "/export", text: "Скачать сценарий (.json)" })]));
    $("wf-settings").hidden = false;
  }

  // ------------------------------------------------------ сохранение и запуск

  function payload() {
    return { name: $("wf-name").value.trim() || "Сценарий", nodes: S.wf.nodes, connections: S.wf.connections, settings: S.wf.settings };
  }

  function save() {
    if (S.readonly) return Promise.resolve();
    $("wf-saved").textContent = "Сохраняем…";
    return api("PUT", "/api/workflows/" + S.id, payload()).then(function (r) {
      S.dirty = false;
      $("wf-saved").textContent = "Сохранено";
      $("wf-saved").classList.remove("dirty");
      document.title = payload().name;
      if (r.warning) { banner(r.warning, "warn"); setActiveUi(false); }
      return r;
    }).catch(function (e) {
      $("wf-saved").textContent = "Не сохранено";
      status("Не удалось сохранить: " + e.message, "error", true);
      throw e;
    });
  }

  function setActiveUi(on) {
    var b = $("wf-active");
    b.classList.toggle("on", on);
    b.querySelector("span").textContent = on ? "вкл" : "выкл";
    S.wf.active = on;
  }

  $("wf-active").addEventListener("click", function () {
    if (S.readonly) return;
    var want = !S.wf.active;
    (S.dirty ? save() : Promise.resolve()).then(function () {
      return api("POST", "/api/workflows/" + S.id + "/activate", { active: want });
    }).then(function (r) {
      setActiveUi(r.active);
      banner("");
      status(r.active ? "Сценарий включён — он будет запускаться сам." : "Сценарий выключен.", "ok");
    }).catch(function (e) { banner(e.message, "error"); });
  });

  var pollTimer = null;

  function run(stopAt) {
    if (S.readonly) { location.href = "/workflows/" + S.id; return; }
    if (S.exec && S.exec.running) return;
    var body = { workflow: payload() };
    if (stopAt) body.stop_at = stopAt;
    var sel = Array.from(S.selected).map(nodeById).find(function (n) { return n && typeOf(n) && typeOf(n).trigger; });
    if (sel) body.trigger_id = sel.id;
    $("wf-run").disabled = true;
    status("Запускаем…", "", true);
    api("POST", "/api/workflows/" + S.id + "/run", body).then(function (r) {
      S.exec = { id: r.execution_id, running: true, run_data: {} };
      poll();
    }).catch(function (e) {
      $("wf-run").disabled = false;
      status("Не удалось запустить: " + e.message, "error", true);
    });
  }

  function stopButton() {
    return h("button", { type: "button", class: "ghost small", text: "Остановить", onclick: function () {
      api("POST", "/api/executions/" + S.exec.id + "/stop", {});
    } });
  }

  function poll() {
    clearTimeout(pollTimer);
    api("GET", "/api/executions/" + S.exec.id).then(function (r) {
      S.exec = r;
      render();
      if (S.panelNode) renderPanel();
      if (r.running) {
        var cur = r.current_node && nodeById(r.current_node);
        status(h("span", {}, ["Выполняется" + (cur ? ": " + cur.name : "") + "… ", stopButton()]), "", true);
        pollTimer = setTimeout(poll, 600);
        return;
      }
      $("wf-run").disabled = false;
      var secs = r.finished_at && r.started_at ? (r.finished_at - r.started_at).toFixed(1) : "?";
      if (r.status === "success") status("Выполнено за " + secs + " с. Щёлкните по узлу, чтобы посмотреть данные.", "ok");
      else if (r.status === "error") {
        var errNode = S.wf.nodes.find(function (n) { return n.name === r.error_node; });
        var msg = h("span", {}, ["Ошибка" + (r.error_node ? " в узле «" + r.error_node + "»" : "") + ": " + r.error + (r.error_hint ? " Что делать: " + r.error_hint : "") + " "]);
        if (errNode) msg.appendChild(h("button", { type: "button", class: "ghost small", text: "Открыть узел", onclick: function () { S.panelTab = "data"; openPanel(errNode.id); } }));
        status(msg, "error", true);
      } else status("Выполнение остановлено.", "warn");
    }).catch(function (e) {
      status("Связь с сервером потеряна: " + e.message, "error", true);
      pollTimer = setTimeout(poll, 2000);
    });
  }

  function listen(n) {
    (S.dirty ? save() : Promise.resolve()).then(function () {
      return api("POST", "/api/workflows/" + S.id + "/listen", { node_id: n.id });
    }).then(function (r) {
      status("Ждём тестовый вызов на /webhook-test/" + r.path + " … Отправьте запрос в течение 2 минут.", "", true);
      var until = Date.now() + 120000;
      (function wait() {
        api("GET", "/api/workflows/" + S.id + "/latest?since=" + r.since).then(function (x) {
          if (x.execution_id) { S.exec = { id: x.execution_id, running: true, run_data: {} }; poll(); return; }
          if (Date.now() < until) setTimeout(wait, 1200); else status("Тестовый вызов не пришёл за 2 минуты.", "warn");
        });
      })();
    }).catch(function (e) { status(e.message, "error", true); });
  }

  // ------------------------------------------------------------ загрузка

  function loadExecution(exId) {
    return api("GET", "/api/executions/" + exId).then(function (r) {
      S.exec = r;
      if (r.workflow_snapshot && r.workflow_snapshot.nodes) {
        S.wf.nodes = r.workflow_snapshot.nodes;
        S.wf.connections = r.workflow_snapshot.connections;
        S.readonly = true;
        root.classList.add("readonly");
        var when = new Date(r.started_at * 1000).toLocaleString("ru-RU");
        var st = { success: "успешно", error: "ошибка", cancelled: "остановлено", running: "выполняется" }[r.status] || r.status;
        banner(h("span", {}, ["Просмотр выполнения №" + r.id + " от " + when + " — " + st + (r.error ? ": " + r.error : "") + ". Здесь сценарий в том виде, в каком он был при запуске. ",
          h("a", { href: "/workflows/" + S.id, text: "Открыть текущую версию для правки" })]), r.status === "error" ? "error" : "info");
        if (r.running) poll();
      }
    });
  }

  Promise.all([api("GET", "/api/node-types"), api("GET", "/api/workflows/" + S.id), api("GET", "/api/lookup")]).then(function (res) {
    S.groups = res[0].groups;
    S.groups.forEach(function (g) { g.nodes.forEach(function (t) { S.types[t.key] = t; }); });
    S.wf = res[1];
    S.lookup = res[2];
    var ex = root.dataset.execution;
    return (ex ? loadExecution(ex) : Promise.resolve()).then(function () {
      render(); fit();
      if (!S.wf.nodes.length) openPicker();
    });
  }).catch(function (e) { banner("Не удалось загрузить сценарий: " + e.message, "error"); });

  $("wf-name").addEventListener("input", markDirty);
  $("wf-save").addEventListener("click", function () { save(); });
  $("wf-run").addEventListener("click", function () { run(); });
  $("wf-run-to").addEventListener("click", function () { if (S.panelNode) run(S.panelNode); });
  $("wf-add").addEventListener("click", function () { S.pendingFrom = null; openPicker(); });
  $("wf-zoom-in").addEventListener("click", function () { zoom(1.2); });
  $("wf-zoom-out").addEventListener("click", function () { zoom(1 / 1.2); });
  $("wf-fit").addEventListener("click", fit);
  $("wf-undo").addEventListener("click", undo);
  $("wf-redo").addEventListener("click", redo);
  $("wf-settings-btn").addEventListener("click", openSettings);
  document.querySelectorAll(".wf-modal [data-close]").forEach(function (b) {
    b.addEventListener("click", function () { b.closest(".wf-modal").hidden = true; S.pendingFrom = null; });
  });
  document.querySelectorAll(".wf-modal").forEach(function (m) {
    m.addEventListener("mousedown", function (ev) { if (ev.target === m) { m.hidden = true; S.pendingFrom = null; } });
  });
  window.addEventListener("beforeunload", function (ev) {
    if (S.dirty) { ev.preventDefault(); ev.returnValue = ""; }
  });
  window.addEventListener("resize", applyView);
})();
