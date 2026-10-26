/* 계측 매트릭스 하위 화면 — 영상 위에 계측자들의 선을 겹쳐 본다.
 *
 * 왜 따로 만들었나
 * ----------------
 * `measure.js` 는 계측용이라 드래그·타이머·확인 팝업 같은 것이 얽혀 있다.
 * 여기는 **읽기 전용 열람**이고, 여러 사람의 선을 동시에 겹쳐 그린다는 점이
 * 다르다. 같은 파일에 모드를 섞으면 계측 쪽(가설 2의 시간 측정)을 건드릴
 * 위험이 생기므로 분리했다.
 *
 * 좌표계
 * ------
 *   DICOM 원본 px  --(× renderScale)-->  PNG px  --(× view.s, + view.tx/ty)-->  캔버스 px
 *
 * DB 에는 원본 px 로 저장돼 있다. 환산은 `origToCanvas` 한 곳에서만 한다.
 */
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("detailData").textContent);

  var canvas = document.getElementById("detCanvas");
  var ctx = canvas.getContext("2d");
  var wrap = document.getElementById("canvasWrap");
  var hud = document.getElementById("detHud");

  var img = new Image();
  var imgEnh = null;
  var imgReady = false;
  var enhanced = false;
  var showLabels = true;

  // 필터: null = 전체
  var onlyRater = null;      // 계측자 id ('ai' 도 가능)
  var onlyVar = null;        // 항목 key

  // AI 예측 — 요청 시 추론해 채운다. 사람 계측과 같은 좌표계(DICOM 원본 px).
  var AI_COLOR = "#ffd400";
  var ai = null;             // { results: [{variable_key, points, value_mm, infer_ms}] }

  // 화면 변환 (PNG px → 캔버스 px)
  var view = { s: 1, tx: 0, ty: 0 };

  // ------------------------------------------------------------ 좌표 변환
  function origToCanvas(x, y) {
    var px = x * DATA.renderScale;
    var py = y * DATA.renderScale;
    return [px * view.s + view.tx, py * view.s + view.ty];
  }

  function resize() {
    var r = wrap.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    canvas.style.width = r.width + "px";
    canvas.style.height = r.height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function cssSize() {
    var r = wrap.getBoundingClientRect();
    return [r.width, r.height];
  }

  function fit() {
    if (!imgReady) return;
    var wh = cssSize();
    var s = Math.min(wh[0] / img.width, wh[1] / img.height);
    view.s = s;
    view.tx = (wh[0] - img.width * s) / 2;
    view.ty = (wh[1] - img.height * s) / 2;
  }

  // ------------------------------------------------------------ 그리기
  function visibleItems() {
    var out = [];
    DATA.raters.forEach(function (r) {
      if (onlyRater !== null && r.id !== onlyRater) return;
      r.lines.forEach(function (it) {
        if (onlyVar !== null && it.key !== onlyVar) return;
        out.push({ rater: r, item: it, ai: false });
      });
    });
    // AI 는 점선으로 그려 사람 선과 구분한다 — 색만으로는 헷갈린다
    if (ai && (onlyRater === null || onlyRater === "ai")) {
      ai.results.forEach(function (r) {
        if (onlyVar !== null && r.variable_key !== onlyVar) return;
        out.push({
          rater: { id: "ai", name: "AI", color: AI_COLOR },
          item: {
            key: r.variable_key,
            short: (r.short || r.label || r.variable_key),
            points: r.points,
            value_mm: r.value_mm,
          },
          ai: true,
        });
      });
    }
    return out;
  }

  function draw() {
    var wh = cssSize();
    ctx.clearRect(0, 0, wh[0], wh[1]);
    if (!imgReady) return;

    var src = (enhanced && imgEnh) ? imgEnh : img;
    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(src, view.tx, view.ty, img.width * view.s, img.height * view.s);
    ctx.restore();

    var items = visibleItems();

    // 선은 화면 배율과 무관하게 일정한 굵기로 — 확대해도 두꺼워지지 않게 한다
    items.forEach(function (e) {
      var p = e.item.points;
      if (!p || p.length < 2) return;
      var a = origToCanvas(p[0][0], p[0][1]);
      var b = origToCanvas(p[p.length - 1][0], p[p.length - 1][1]);

      // 어두운 배경에서도 보이게 바깥선을 먼저 깐다
      ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(0,0,0,0.55)";
      ctx.lineWidth = 5;
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();

      ctx.strokeStyle = e.rater.color;
      ctx.lineWidth = 2.5;
      if (e.ai) ctx.setLineDash([9, 5]);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      ctx.setLineDash([]);

      // 끝점
      [a, b].forEach(function (q) {
        ctx.beginPath();
        ctx.arc(q[0], q[1], 4.5, 0, Math.PI * 2);
        ctx.fillStyle = e.rater.color;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "rgba(0,0,0,0.65)";
        ctx.stroke();
      });

      if (showLabels) {
        var mx = (a[0] + b[0]) / 2;
        var my = (a[1] + b[1]) / 2;
        var txt = e.item.short +
          (e.item.value_mm === null ? "" : "  " + e.item.value_mm.toFixed(1) + "mm");
        ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
        var w = ctx.measureText(txt).width;
        // 선 위에 얹되 선을 가리지 않게 살짝 띄운다
        var bx = mx - w / 2 - 5, by = my - 20;
        ctx.fillStyle = "rgba(0,0,0,0.72)";
        ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(bx, by, w + 10, 17, 4); } else { ctx.rect(bx, by, w + 10, 17); }
        ctx.fill();
        ctx.fillStyle = e.rater.color;
        ctx.textBaseline = "middle";
        ctx.fillText(txt, bx + 5, by + 9);
      }
    });

    // HUD — 지금 무엇이 보이고 있는지
    var names = {};
    items.forEach(function (e) { names[e.rater.name] = 1; });
    var who = Object.keys(names);
    hud.textContent = items.length === 0
      ? "표시할 계측이 없습니다"
      : items.length + "개 선 · " + (who.length === 1 ? who[0] : who.length + "명 겹침") +
        (onlyVar ? " · " + onlyVar : "");
  }

  // ------------------------------------------------------------ 확대·이동
  var drag = null;
  canvas.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
  });
  window.addEventListener("mousemove", function (e) {
    if (!drag) return;
    view.tx = drag.tx + (e.clientX - drag.x);
    view.ty = drag.ty + (e.clientY - drag.y);
    draw();
  });
  window.addEventListener("mouseup", function () { drag = null; });

  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    if (!imgReady) return;
    var r = canvas.getBoundingClientRect();
    var cx = e.clientX - r.left, cy = e.clientY - r.top;
    var k = Math.pow(1.0015, -e.deltaY);
    var ns = Math.min(40, Math.max(0.05, view.s * k));
    // 커서 아래 지점을 고정한 채 확대
    view.tx = cx - (cx - view.tx) * (ns / view.s);
    view.ty = cy - (cy - view.ty) * (ns / view.s);
    view.s = ns;
    draw();
  }, { passive: false });

  // ------------------------------------------------------------ 토글
  function setGroupActive(container, btn) {
    container.querySelectorAll(".seg-btn").forEach(function (b) {
      b.classList.toggle("seg-on", b === btn);
    });
  }

  document.querySelectorAll("[data-rater]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var v = btn.getAttribute("data-rater");
      // 'ai' 는 문자열 그대로 둬야 한다 — parseInt("ai") 는 NaN 이고,
      // NaN 은 사람 id 와도 "ai" 와도 같지 않아 **아무것도 그려지지 않는다.**
      onlyRater = (v === "all") ? null : (v === "ai" ? "ai" : parseInt(v, 10));
      setGroupActive(btn.parentElement, btn);
      draw();
    });
  });

  document.querySelectorAll("[data-var]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var v = btn.getAttribute("data-var");
      onlyVar = (v === "all") ? null : v;
      setGroupActive(btn.parentElement, btn);
      syncRowHighlight();
      draw();
    });
  });

  // 우측 표의 한 줄을 눌러도 같은 필터가 걸린다
  function syncRowHighlight() {
    document.querySelectorAll("tr.det-row").forEach(function (tr) {
      tr.classList.toggle("det-row-on", onlyVar !== null &&
        tr.getAttribute("data-var") === onlyVar);
    });
  }
  document.querySelectorAll("tr.det-row").forEach(function (tr) {
    tr.addEventListener("click", function () {
      var k = tr.getAttribute("data-var");
      onlyVar = (onlyVar === k) ? null : k;   // 같은 줄을 다시 누르면 해제
      var group = document.querySelector("[data-var='all']").parentElement;
      var target = group.querySelector("[data-var='" + (onlyVar || "all") + "']");
      if (target) setGroupActive(group, target);
      syncRowHighlight();
      draw();
    });
  });

  document.getElementById("chkLabels").addEventListener("change", function (e) {
    showLabels = e.target.checked;
    draw();
  });

  document.getElementById("btnFit").addEventListener("click", function () {
    fit(); draw();
  });

  var btnEnh = document.getElementById("btnEnhance");
  btnEnh.addEventListener("click", function () {
    if (!imgEnh) {
      // 강화본은 처음 누를 때만 받아 둔다
      var e = new Image();
      e.onload = function () { imgEnh = e; enhanced = true; syncEnh(); draw(); };
      e.onerror = function () { hud.textContent = "강화본을 불러오지 못했습니다"; };
      e.src = DATA.pngEnhUrl;
      return;
    }
    enhanced = !enhanced;
    syncEnh();
    draw();
  });
  function syncEnh() {
    btnEnh.textContent = "강화 " + (enhanced ? "ON" : "OFF");
    btnEnh.classList.toggle("on", enhanced);
  }

  window.addEventListener("keydown", function (e) {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "0") { fit(); draw(); }
    else if (e.key === "c" || e.key === "C") { btnEnh.click(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      // 좌우 화살표로 영상을 넘긴다 (매트릭스로 나가지 않아도 되게)
      var sel = e.key === "ArrowLeft" ? ".det-nav" : ".det-nav:last-of-type";
      var navs = document.querySelectorAll(".det-nav:not(.det-nav-off)");
      var want = e.key === "ArrowLeft" ? "‹" : "›";
      for (var i = 0; i < navs.length; i++) {
        if (navs[i].textContent.trim() === want) {
          e.preventDefault();
          window.location.assign(navs[i].getAttribute("href"));
          return;
        }
      }
    }
    else if (e.key === "Escape") {
      var back = document.querySelector(".det-back");
      if (back) window.location.assign(back.getAttribute("href"));
    }
  });

  window.addEventListener("resize", function () { resize(); fit(); draw(); });

  // ------------------------------------------------- AI 추론 (progress + 표)
  var progWrap = document.getElementById("detProg");
  var progStage = document.getElementById("progStage");
  var progFill = document.getElementById("progFill");
  var progDetail = document.getElementById("progDetail");
  var progList = document.getElementById("progList");
  var btnPredict = document.getElementById("btnPredict");

  // 진행도 배분 — 사용자가 기다리는 전체 시간을 기준으로 잡는다.
  // 모델 로드가 1.5초로 가장 길어 비중을 크게 둔다(실제 체감과 맞게).
  var W = { load: 55, preprocess: 5, warmup: 20, measure: 20 };

  function setProg(pct, stage, detail) {
    progFill.style.width = Math.max(0, Math.min(100, pct)) + "%";
    if (stage !== undefined) progStage.textContent = stage;
    if (detail !== undefined) progDetail.textContent = detail;
  }

  function progRow(label, value) {
    var li = document.createElement("li");
    var a = document.createElement("span"); a.textContent = label;
    var b = document.createElement("b"); b.textContent = value;
    li.appendChild(a); li.appendChild(b);
    progList.appendChild(li);
    progList.scrollTop = progList.scrollHeight;
  }

  function runPredict() {
    if (btnPredict.disabled) return;
    btnPredict.disabled = true;
    progList.innerHTML = "";
    progWrap.hidden = false;
    setProg(0, "시작하는 중…", "");

    var es = new EventSource("/admin/image/" + DATA.imageId + "/predict");
    var base = 0;

    es.addEventListener("progress", function (ev) {
      var p;
      try { p = JSON.parse(ev.data); } catch (e) { return; }

      if (p.stage === "start") {
        setProg(1, "모델 준비", p.device_name + " · torch " + p.torch);
      } else if (p.stage === "load_begin") {
        setProg(1, "모델 로드 중…", "8개 가중치를 GPU 로 올린다");
      } else if (p.stage === "load") {
        setProg(W.load * (p.i / p.n), "모델 로드 중… (" + p.i + "/" + p.n + ")", p.label);
      } else if (p.stage === "load_done") {
        base = W.load;
        setProg(base, "모델 로드 완료", p.n + "개 / " + (p.ms / 1000).toFixed(1) + "s");
        progRow("모델 로드 (측정 제외)", (p.ms / 1000).toFixed(2) + "s");
      } else if (p.stage === "preprocess_begin") {
        setProg(base + 1, "영상 전처리 중…", "DICOM 읽기 → 512 리사이즈·패딩");
      } else if (p.stage === "preprocess_done") {
        base = W.load + W.preprocess;
        setProg(base, "전처리 완료", p.size + " · " + p.ms.toFixed(1) + "ms");
        progRow("전처리 (별도 기록)", p.ms.toFixed(1) + "ms");
      } else if (p.stage === "warmup_begin") {
        setProg(base + 1, "워밍업 중…", "첫 추론의 커널 컴파일을 미리 치운다");
      } else if (p.stage === "warmup") {
        setProg(base + W.warmup * (p.i / p.n),
                "워밍업 중… (" + p.i + "/" + p.n + ")", "측정에서 제외되는 구간");
      } else if (p.stage === "warmup_done") {
        base = W.load + W.preprocess + W.warmup;
        setProg(base, "워밍업 완료", p.n + "회");
      } else if (p.stage === "measure_begin") {
        setProg(base, "측정 시작", p.n + "개 항목");
      } else if (p.stage === "measure") {
        setProg(base + W.measure * ((p.i - 1) / p.n),
                "측정 중… (" + p.i + "/" + p.n + ")", p.label);
      } else if (p.stage === "measured") {
        setProg(base + W.measure * (p.i / p.n),
                "측정 중… (" + p.i + "/" + p.n + ")", p.label);
        progRow(p.label, p.ms.toFixed(2) + "ms" +
          (p.value_mm !== null ? "  ·  " + p.value_mm.toFixed(1) + "mm" : ""));
      } else if (p.stage === "store_done") {
        setProg(100, "저장 완료", p.n + "건");
      } else if (p.stage === "store_failed") {
        progRow("저장 실패", "표시는 됩니다");
      }
    });

    es.addEventListener("result", function (ev) {
      es.close();
      btnPredict.disabled = false;
      var r;
      try { r = JSON.parse(ev.data); } catch (e) { r = { ok: false, error: "응답 해석 실패" }; }
      if (!r.ok) {
        setProg(100, "실패", r.error || "알 수 없는 오류");
        window.setTimeout(function () { progWrap.hidden = true; }, 2600);
        return;
      }
      setProg(100, "완료", "순수 추론 " + r.total_infer_ms.toFixed(1) + "ms");
      applyAI(r);
      // 결과를 잠깐 보여준 뒤 영상으로 — "휘리릭" 뒤에 그림이 뜨도록
      window.setTimeout(function () { progWrap.hidden = true; }, 900);
    });

    es.onerror = function () {
      es.close();
      btnPredict.disabled = false;
      // 추론 환경이 없거나(503) 중간에 끊긴 경우 — 저장된 예측이 있으면 그것으로
      setProg(100, "추론을 받지 못했습니다", "저장된 결과를 찾습니다…");
      loadCached().then(function (had) {
        setProg(100, had ? "저장된 결과를 표시합니다" : "추론 불가",
                had ? "" : "추론 환경(picc-infer)을 확인하세요");
        window.setTimeout(function () { progWrap.hidden = true; }, had ? 900 : 2600);
      });
    };
  }

  function applyAI(r) {
    ai = r;
    var n = (r.results || []).length;
    var seg = document.getElementById("segAI");
    if (seg) {
      seg.disabled = n === 0;
      seg.title = n === 0 ? "먼저 '모델로 계측' 을 실행하세요"
        : ((r.device_name || r.device || "") +
           " · 순수 추론 합계 " + (r.total_infer_ms || 0).toFixed(1) + "ms");
      var cnt = document.getElementById("segAIn");
      if (cnt) cnt.textContent = String(n);
    }
    fillAICells();
    highlight();
    draw();
  }

  function fmtMs(v) {
    // 사람은 수천 ms, AI 는 한 자리 ms — 둘 다 ms 로 읽히게 천단위만 넣는다
    if (v === null || v === undefined || isNaN(v)) return "—";
    return v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(2);
  }

  function fillAICells() {
    var byKey = {};
    (ai ? ai.results : []).forEach(function (x) { byKey[x.variable_key] = x; });

    document.querySelectorAll("tr.det-row").forEach(function (tr) {
      var k = tr.getAttribute("data-var");
      var x = byKey[k];
      var mmCell = tr.querySelector('td[data-cell="mm"][data-rater="ai"]');
      if (mmCell) {
        mmCell.textContent = (x && x.value_mm !== null && x.value_mm !== undefined)
          ? x.value_mm.toFixed(1) : "—";
      }
      var msCell = tr.querySelector('td[data-cell="ms"][data-rater="ai"]');
      if (msCell) msCell.textContent = x ? fmtMs(x.infer_ms) : "—";
    });

    var times = (ai ? ai.results : []).map(function (x) { return x.infer_ms; })
                  .filter(function (v) { return v; }).sort(function (a, b) { return a - b; });
    var med = document.getElementById("aiMedianMs");
    var tot = document.getElementById("aiTotalMs");
    if (med) med.textContent = times.length ? fmtMs(times[Math.floor(times.length / 2)]) : "—";
    if (tot) tot.textContent = times.length
      ? fmtMs(times.reduce(function (a, b) { return a + b; }, 0)) : "—";
  }

  // 강조 규칙
  //  * 시간: 그 항목에서 **가장 짧은** 값 → 굵은 빨강
  //  * 계측값: **다른 값들과 가장 동떨어진** 값 → 굵은 파랑
  //
  // '동떨어짐'은 그 줄 값들의 **중앙값과의 거리**로 본다. 평균을 쓰면 이상치가
  // 평균을 끌고 가 자기 거리를 줄여 버린다(이상치가 자기를 숨긴다).
  // 값이 2개 이하면 '동떨어짐'을 말할 수 없으므로 표시하지 않는다.
  //
  // 시간은 사람(초)과 AI(ms) 단위가 다르므로 **ms 로 환산해** 비교한다.
  function highlight() {
    // 길이 표와 시간 표가 따로이므로 data-kind 로 구분해 처리한다
    document.querySelectorAll('tr.det-row[data-kind="mm"]').forEach(function (tr) {
      var cells = tr.querySelectorAll('td[data-cell="mm"]');
      cells.forEach(function (c) { c.classList.remove("det-far"); });
      var vals = [];
      cells.forEach(function (c) {
        var v = parseFloat(c.textContent.replace(/,/g, ""));
        if (!isNaN(v)) vals.push({ v: v, el: c });
      });
      // 셋 이상일 때만 '동떨어짐' 을 말할 수 있다
      if (vals.length < 3) return;
      var srt = vals.map(function (x) { return x.v; }).sort(function (a, b) { return a - b; });
      var mid = srt.length % 2
        ? srt[(srt.length - 1) / 2]
        : (srt[srt.length / 2 - 1] + srt[srt.length / 2]) / 2;
      var worst = -1, pick = [];
      vals.forEach(function (x) {
        var d = Math.abs(x.v - mid);
        if (d > worst + 1e-9) { worst = d; pick = [x]; }
        else if (Math.abs(d - worst) < 1e-9) { pick.push(x); }
      });
      if (worst > 1e-9) pick.forEach(function (x) { x.el.classList.add("det-far"); });
    });

    document.querySelectorAll('tr.det-row[data-kind="ms"]').forEach(function (tr) {
      var cells = tr.querySelectorAll('td[data-cell="ms"]');
      cells.forEach(function (c) { c.classList.remove("det-fast"); });
      var times = [];
      cells.forEach(function (c) {
        var v = parseFloat(c.textContent.replace(/,/g, ""));
        if (!isNaN(v)) times.push({ ms: v, el: c });     // 단위가 이미 ms 로 통일됨
      });
      if (times.length < 2) return;
      var best = Math.min.apply(null, times.map(function (x) { return x.ms; }));
      times.forEach(function (x) {
        if (Math.abs(x.ms - best) < 1e-9) x.el.classList.add("det-fast");
      });
    });
  }

  btnPredict.addEventListener("click", runPredict);

  // 저장된 예측을 쓰는 경로 — 추론 환경이 없을 때의 폴백
  function loadCached() {
    return fetch("/admin/image/" + DATA.imageId + "/prediction",
                 { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok && j.cached && j.results && j.results.length) {
          applyAI(j);
          return true;
        }
        return false;
      })
      .catch(function () { return false; });
  }

  // ------------------------------------------------------------ 시작
  highlight();          // 사람 값만으로도 '가장 빠른 시간' 은 표시된다
  resize();

  // 영상에 들어오면 **바로 AI 계측을 시작한다.**
  // 저장된 값을 먼저 띄우지 않는다 — 그러면 숫자가 한 번 떴다가 다시 바뀌어
  // 무엇이 지금 측정한 값인지 헷갈린다. 추론 환경이 없을 때만 저장분을 쓴다.
  window.setTimeout(function () {
    if (typeof EventSource === "undefined") { loadCached(); return; }
    runPredict();
  }, 150);   // 영상이 먼저 뜨도록 한 틱 양보한다

  img.onload = function () {
    imgReady = true;
    fit();
    draw();
  };
  img.onerror = function () { hud.textContent = "영상을 불러오지 못했습니다"; };
  img.src = DATA.pngUrl;
  // 캐시에 이미 있으면 onload 가 안 뜨는 브라우저가 있다
  if (img.complete && img.naturalWidth) {
    imgReady = true; fit(); draw();
  }
})();
