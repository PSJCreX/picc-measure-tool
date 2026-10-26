/* PICC 계측 — 문제 단위 진행.
 *
 * 한 문제 = (영상 하나 × 변수 하나). 흐름은
 *
 *   문제 출제  사진 없이 무엇을 잴지만 보여 준다
 *     → 공개   이미지를 미리 받아 둔 뒤 화면에 그린다. 그려진 프레임에서 t0
 *     → 계측   좌클릭 드래그 한 번 (시작점 → 끝점)
 *              Shift+좌드래그 = 화면 이동, 우드래그 = 창 조절(WW/WC),
 *              Tab = 샤프닝 3단계
 *     → drop   손을 뗀 프레임에서 t1.  duration = t1 - t0
 *     → 확인   계측선을 가리지 않는 쪽에 팝업
 *         확정      저장 후 바로 다음 문제
 *         재측정    대기 pool로 되돌리고 바로 다음 문제 (그 자리에서 다시 재지 않음)
 *
 * 시간이 지표이므로 두 가지를 지킨다.
 *   1. 다음 문제의 이미지를 **미리** 받아 둔다. 공개 순간 네트워크를 타면 안 된다.
 *   2. t0는 draw() 직후 requestAnimationFrame 안에서 찍는다 — 실제로 화면에
 *      나타난 시점에 가장 가깝다.
 *
 * 좌표계 (3단계)
 *   ① DICOM 원본 px  — 서버에 저장. 기존 어노테이션과 같은 좌표계
 *   ② PNG px         — ① × renderScale
 *   ③ 캔버스 px      — ② × view.s + view.t
 */
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("measureData").textContent);

  var canvas = document.getElementById("stage");
  var ctx = canvas.getContext("2d");
  var stageQuestion = document.getElementById("stageQuestion");
  var stageMeasure = document.getElementById("stageMeasure");
  var qMain = document.getElementById("qMain");
  var confirmPop = document.getElementById("confirmPop");
  var reviewBar = document.getElementById("reviewBar");

  var MIN_DRAG_PX = 8;          // 이보다 짧으면 오조작으로 보고 무시
  var PHASE = { QUESTION: "question", MEASURE: "measure", CONFIRM: "confirm",
                REVIEW: "review" };

  // ------------------------------------------------------------------ state
  var question = DATA.question;     // 현재 문제
  var answers = DATA.answers || [];
  var phase = PHASE.QUESTION;

  var img = new Image();            // 현재 문제의 원본 렌더
  var imgEnh = null;
  var enhanced = false;
  var imgReady = false;

  var nextQuestion = null;          // 미리 받아 둔 다음 문제
  var nextImg = null;

  var view = { s: 1, tx: 0, ty: 0 };

  // 창 조절(window/level). 방사선과 관례대로 우클릭 드래그로 바꾼다.
  //   좌우 = window width(대비), 상하 = window center(밝기)
  var bright = 100, contrast = 100;
  var mouse = { x: 0, y: 0, inside: false };

  // 샤프닝 3단계. 서버가 만드는 CLAHE 강화(C)와는 별개로 클라이언트에서 건다.
  //
  // 강도만큼이나 **반경**이 중요하다. 반경 1px짜리 언샤프는 2048px 영상에서
  // 픽셀 노이즈만 건드려 육안으로 거의 차이가 없다(표준편차 19.9 → 20.2).
  // 반경을 키워야 뼈 경계 자체가 선다.
  var SHARPEN = [
    null,
    { radius: 2, amount: 1.2 },
    { radius: 5, amount: 2.4 },
    { radius: 9, amount: 3.8 },
  ];
  var sharpenLevel = 0;
  var sharpCache = {};              // "o2" / "e3" → 처리된 canvas

  // 확인 팝업은 계측선 바로 옆에 붙는다. 사용자가 핸들로 옮기면 그 어긋남을
  // 기억해 다음 문제에도 같은 상대 위치로 띄운다.
  var popOffset = { dx: 0, dy: 0 };
  var popDrag = null;

  var drag = null;                  // 계측 드래그 (좌클릭)
  var pan = null;                   // 화면 이동 (Shift + 좌클릭 드래그)
  var win = null;                   // 창 조절 (우클릭 드래그)
  var line = null;                  // [[ox,oy],[ox,oy]] 확정 대기 중인 계측
  var t0 = null, durationMs = null;
  var lastMeasurementId = null;
  var review = null;                // 열람 모드 데이터
  var busy = false;

  // ------------------------------------------------------------- 좌표 변환
  function origToCanvas(x, y) {
    return [x * question.renderScale * view.s + view.tx,
            y * question.renderScale * view.s + view.ty];
  }
  function canvasToOrig(cx, cy) {
    return [(cx - view.tx) / view.s / question.renderScale,
            (cy - view.ty) / view.s / question.renderScale];
  }
  function clampPct(v, lo, hi) {
    return Math.round(Math.max(lo, Math.min(hi, v)));
  }

  function syncWindowReadout() {
    var el = document.getElementById("winReadout");
    if (el) el.textContent = "WW " + contrast + "% · WC " + bright + "%";
  }

  function resetWindow() {
    bright = 100; contrast = 100;
    syncWindowReadout();
    draw();
  }

  function lengthMm(pts) {
    if (!pts || pts.length < 2 || !question.pixelSpacingMm) return null;
    var dx = pts[1][0] - pts[0][0], dy = pts[1][1] - pts[0][1];
    return Math.sqrt(dx * dx + dy * dy) * question.pixelSpacingMm;
  }

  // --------------------------------------------------------------- 캔버스
  function resize() {
    var dpr = window.devicePixelRatio || 1;
    var w = stageMeasure.clientWidth, h = stageMeasure.clientHeight;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function fit() {
    if (!imgReady) return;
    var w = stageMeasure.clientWidth, h = stageMeasure.clientHeight;
    var s = Math.min(w / img.width, h / img.height) * 0.95;
    view.s = s;
    view.tx = (w - img.width * s) / 2;
    view.ty = (h - img.height * s) / 2;
  }

  function zoomAt(cx, cy, factor) {
    var ns = Math.max(0.05, Math.min(40, view.s * factor));
    var k = ns / view.s;
    view.tx = cx - (cx - view.tx) * k;
    view.ty = cy - (cy - view.ty) * k;
    view.s = ns;
    draw();
    if (phase === PHASE.CONFIRM) placeConfirm();
  }

  var ACCENT = "#ff2d55";
  var REVIEW_COLOR = "#39d0ff";

  function baseImage() {
    return enhanced && imgEnh && imgEnh.complete ? imgEnh : img;
  }

  /* 가변 반경 박스 블러 (러닝 섬). 반경이 커져도 O(n)이라 r=9에서도 빠르다.
     단순 컨볼루션이면 19탭 × 2패스라 눈에 띄게 멈춘다. */
  function boxBlur(src, dst, w, h, r) {
    var tmp = new Float32Array(w * h);
    var i, x, y, o, sum;
    var win = 2 * r + 1;

    for (y = 0; y < h; y++) {                     // 가로
      o = y * w;
      sum = src[o] * (r + 1);
      for (i = 1; i <= r; i++) sum += src[o + Math.min(i, w - 1)];
      for (x = 0; x < w; x++) {
        tmp[o + x] = sum / win;
        sum -= src[o + Math.max(x - r, 0)];
        sum += src[o + Math.min(x + r + 1, w - 1)];
      }
    }
    for (x = 0; x < w; x++) {                     // 세로
      sum = tmp[x] * (r + 1);
      for (i = 1; i <= r; i++) sum += tmp[Math.min(i, h - 1) * w + x];
      for (y = 0; y < h; y++) {
        dst[y * w + x] = sum / win;
        sum -= tmp[Math.max(y - r, 0) * w + x];
        sum += tmp[Math.min(y + r + 1, h - 1) * w + x];
      }
    }
  }

  /* 언샤프 마스크. 회색조라 R 채널만 처리하고 G·B에 복사한다. */
  function unsharp(imageData, radius, amount) {
    var w = imageData.width, h = imageData.height, d = imageData.data;
    var n = w * h;
    var src = new Float32Array(n);
    for (var i = 0; i < n; i++) src[i] = d[i * 4];

    var blur = new Float32Array(n);
    boxBlur(src, blur, w, h, radius);

    for (i = 0; i < n; i++) {
      var v = src[i] + amount * (src[i] - blur[i]);
      v = v < 0 ? 0 : (v > 255 ? 255 : v);
      var j = i * 4;
      d[j] = d[j + 1] = d[j + 2] = v;
    }
  }

  function activeImage() {
    var base = baseImage();
    if (!sharpenLevel) return base;
    var key = (enhanced ? "e" : "o") + sharpenLevel;
    if (sharpCache[key]) return sharpCache[key];
    if (!base.complete || !(base.naturalWidth || base.width)) return base;

    var c = document.createElement("canvas");
    c.width = base.naturalWidth || base.width;
    c.height = base.naturalHeight || base.height;
    var cx = c.getContext("2d", { willReadFrequently: true });
    cx.drawImage(base, 0, 0);
    try {
      var cfg = SHARPEN[sharpenLevel];
      var id = cx.getImageData(0, 0, c.width, c.height);
      unsharp(id, cfg.radius, cfg.amount);
      cx.putImageData(id, 0, 0);
    } catch (e) {
      return base;                                  // 실패하면 원본으로
    }
    sharpCache[key] = c;
    return c;
  }

  function setSharpen(level) {
    sharpenLevel = level;
    var btn = document.getElementById("btnSharpen");
    if (btn) {
      btn.textContent = level ? "샤프 " + level : "샤프 OFF";
      btn.classList.toggle("on", level > 0);
    }
    draw();
  }

  function draw() {
    var w = stageMeasure.clientWidth, h = stageMeasure.clientHeight;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0d0d0f";
    ctx.fillRect(0, 0, w, h);
    if (!imgReady) return;

    ctx.save();
    ctx.filter = "brightness(" + bright + "%) contrast(" + contrast + "%)";
    ctx.imageSmoothingEnabled = view.s < 2;
    ctx.drawImage(activeImage(), view.tx, view.ty,
                  img.width * view.s, img.height * view.s);
    ctx.restore();

    var color = phase === PHASE.REVIEW ? REVIEW_COLOR : ACCENT;

    // 드래그 중인 고무줄
    if (drag && drag.moved) {
      drawLine(drag.a, drag.b, color, true);
    } else if (line) {
      drawLine(origToCanvas(line[0][0], line[0][1]),
               origToCanvas(line[1][0], line[1][1]), color, false);
    }

  }

  function drawLine(a, b, color, live) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();

    [a, b].forEach(function (p) {
      ctx.beginPath();
      ctx.arc(p[0], p[1], 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.save();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(p[0], p[1], 7.5, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    });

    var pts = live
      ? [canvasToOrig(a[0], a[1]), canvasToOrig(b[0], b[1])]
      : line;
    var mm = lengthMm(pts);
    if (mm !== null) {
      var mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
      var label = mm.toFixed(1) + " mm";
      ctx.font = "bold 14px ui-monospace, Consolas, monospace";
      var tw = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(0,0,0,.72)";
      ctx.fillRect(mx - tw / 2 - 7, my - 26, tw + 14, 21);
      ctx.fillStyle = color;
      ctx.textBaseline = "top";
      ctx.fillText(label, mx - tw / 2, my - 23);
    }
    ctx.restore();
  }

  /* 안내문의 [[...]] 를 강조 표시로 바꾼다.
     정의의 급소(중간점·내연·상연 종판 …)만 눈에 띄게 하려는 것이라
     서버 문자열에 HTML을 넣지 않고 마커만 쓴다 — 먼저 전부 이스케이프한 뒤
     마커만 태그로 바꾸므로 문구가 바뀌어도 마크업이 깨지지 않는다. */
  function highlight(text) {
    var esc = String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc
      .replace(/\[\[([\s\S]+?)\]\]/g, '<mark class="q-key">$1</mark>')
      .replace(/\n/g, "<br />");
  }

  function setHl(id, text) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = highlight(text);
  }

  /* 배지 문구도 '수평/수직'이 아니라 환자 기준으로 적는다 — 영상의 가로세로로
     읽히면 그 자체가 오해의 출발점이 된다. */
  var AXIS_LABEL = {
    horizontal: "환자 장축에 직각",
    vertical: "환자 장축 방향",
    oblique: "두 점을 잇습니다",
  };
  var AXIS_NOTE_HEAD = {
    horizontal: "측정 방향 — 환자 장축에 직각",
    vertical: "측정 방향 — 환자 장축을 따라",
  };

  function setAxisBadge(axis) {
    var el = document.getElementById("qAxis");
    if (!el) return;
    var label = AXIS_LABEL[axis];
    el.hidden = !label;
    if (!label) return;
    el.textContent = label;
    el.className = "q-axis q-axis--" + axis;
  }

  function setAxisNote(axis, note) {
    var wrap = document.getElementById("qAxisNote");
    if (!wrap) return;
    wrap.hidden = !note;
    if (!note) return;
    document.getElementById("qAxisNoteHead").textContent =
      AXIS_NOTE_HEAD[axis] || "측정 방향";
    setHl("qAxisNoteBody", note);
  }

  /* 브리핑 단계에서만 우측 완료 목록을 접는다. */
  function setBriefing(on) {
    var layout = document.querySelector(".q-layout");
    if (layout) layout.classList.toggle("q-briefing", !!on);
  }

  // ------------------------------------------------------- 문제 출제 화면
  function showQuestionCard() {
    phase = PHASE.QUESTION;
    line = null; drag = null; t0 = null; durationMs = null;
    lastMeasurementId = null;
    stageQuestion.classList.remove("hidden");
    stageMeasure.classList.add("hidden");
    confirmPop.classList.add("hidden");
    reviewBar.classList.add("hidden");

    document.getElementById("qTitle").textContent = question.display;
    setHl("qInstruction", question.instruction);
    setHl("qPointA", question.pointA);
    setHl("qPointB", question.pointB);
    setHl("qCaution", question.caution);
    document.getElementById("qPaperEn").textContent = question.paperEn || "";
    document.getElementById("qPaperWrap").hidden = !question.paperEn;
    setAxisBadge(question.axis);
    setAxisNote(question.axis, question.axisNote);
    document.getElementById("qGuide").src = "/static/graphic/" + question.guide;
    document.getElementById("hudVar").textContent = question.display;

    // 브리핑 동안에는 우측 목록을 접어 화면을 넓게 쓴다 — 정의를 크게 읽히는 게
    // 이 화면의 목적이고, 완료 목록은 계측 중에 보면 된다.
    setBriefing(true);

    var btn = document.getElementById("btnReveal");
    var label = document.getElementById("revealLabel");
    btn.disabled = true;
    label.textContent = "영상 준비 중…";
    document.getElementById("revealSub").textContent = "잠시만 기다려 주세요";

    imgReady = false;
    imgEnh = null;
    enhanced = false;
    sharpCache = {};                 // 영상이 바뀌면 앞 영상의 샤프 결과를 버린다
    syncEnhanceButton();

    // 미리 받아 둔 이미지가 있으면 그걸 쓴다 (네트워크 대기 0)
    if (nextImg && nextImg.dataset.imageId === String(question.imageId)
        && nextImg.complete && nextImg.naturalWidth) {
      img = nextImg;
      nextImg = null;
      onImageReady();
    } else {
      img = new Image();
      img.dataset.imageId = String(question.imageId);
      img.onload = onImageReady;
      img.onerror = function () {
        label.textContent = "영상을 불러오지 못했습니다";
        document.getElementById("revealSub").textContent =
          "새로고침해 주세요";
      };
      img.src = question.pngUrl;
    }
  }

  function onImageReady() {
    imgReady = true;
    var btn = document.getElementById("btnReveal");
    // 버튼을 누르는 순간이 곧 계측 시작이다. '사진 보기'로는 그게 드러나지
    // 않아, 눌러도 되는지 망설이다 시간이 흐르는 경우가 있었다.
    document.getElementById("revealLabel").textContent = "측정 시작";
    document.getElementById("revealSub").textContent =
      "누르는 순간부터 시간이 측정됩니다 (Space)";
    btn.disabled = false;
    btn.focus();
    prefetchNext();
  }

  /* 다음 문제와 그 이미지를 미리 받아 둔다. 공개 순간의 지연을 없애는 핵심. */
  function prefetchNext() {
    fetch("/measure/next", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok || !j.question) { nextQuestion = null; return; }
        nextQuestion = j.question;
        var pre = new Image();
        pre.dataset.imageId = String(j.question.imageId);
        pre.src = j.question.pngUrl;
        nextImg = pre;
      })
      .catch(function () { nextQuestion = null; });
  }

  // ------------------------------------------------------------ 사진 공개
  function reveal() {
    if (phase !== PHASE.QUESTION || !imgReady) return;
    phase = PHASE.MEASURE;
    stageQuestion.classList.add("hidden");
    setBriefing(false);
    stageMeasure.classList.remove("hidden");
    document.getElementById("timerElapsed").textContent = "0.0s";

    resize();
    fit();
    draw();

    // 폴백을 먼저 박아 둔다. 백그라운드 탭이나 자동화처럼 rAF가 늦게/안 뜨는
    // 상황에서 t0가 null로 남으면 소요시간이 통째로 사라진다 — 그건 이 도구가
    // 재야 할 유일한 숫자다.
    t0 = performance.now();

    // 실제로 화면에 나타난 프레임을 기준으로 다시 찍어 정확도를 올린다.
    // drop이 그 전에 일어났다면 이미 폴백 값으로 계산이 끝났으므로 영향 없다.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (phase === PHASE.MEASURE && durationMs === null) t0 = performance.now();
        touchActivity();
      });
    });
  }

  // -------------------------------------------------------------- 마우스
  function localPos(e) {
    var r = canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  canvas.addEventListener("mousedown", function (e) {
    if (phase === PHASE.QUESTION) return;
    var p = localPos(e);

    // 우클릭 드래그 = 창 조절(window/level). 방사선과 뷰어 관례 그대로.
    if (e.button === 2) {
      win = { x0: e.clientX, y0: e.clientY, c0: contrast, b0: bright };
      touchActivity();
      return;
    }
    if (e.button !== 0) return;

    // Shift + 좌클릭 드래그 = 화면 이동
    if (e.shiftKey) {
      pan = { x0: e.clientX, y0: e.clientY, tx0: view.tx, ty0: view.ty };
      touchActivity();
      return;
    }

    if (phase !== PHASE.MEASURE) return;      // 열람 중에는 새로 재지 않는다
    drag = { a: p, b: p, moved: false };
    touchActivity();
  });

  window.addEventListener("mousemove", function (e) {
    if (popDrag) {
      var w = confirmPop.offsetWidth, h = confirmPop.offsetHeight;
      var sw = stageMeasure.clientWidth, sh = stageMeasure.clientHeight;
      var nx = clampNum(popDrag.left0 + (e.clientX - popDrag.x0), 8, sw - w - 8);
      var ny = clampNum(popDrag.top0 + (e.clientY - popDrag.y0), 8, sh - h - 8);
      confirmPop.style.left = nx + "px";
      confirmPop.style.top = ny + "px";
      popOffset.dx = popDrag.offx0 + (nx - popDrag.left0);
      popOffset.dy = popDrag.offy0 + (ny - popDrag.top0);
      return;
    }
    if (win) {
      // 좌우 = window width(대비): 오른쪽으로 갈수록 세게
      // 상하 = window center(밝기): 위로 올리면 어두워지고 아래로 내리면 밝아진다
      var dx = e.clientX - win.x0, dy = e.clientY - win.y0;
      contrast = clampPct(win.c0 + dx * 0.35, 20, 400);
      bright = clampPct(win.b0 + dy * 0.28, 20, 300);
      syncWindowReadout();
      draw();
      return;
    }
    if (pan) {
      view.tx = pan.tx0 + (e.clientX - pan.x0);
      view.ty = pan.ty0 + (e.clientY - pan.y0);
      draw();
      if (phase === PHASE.CONFIRM) placeConfirm();
      return;
    }
    var p = localPos(e);
    mouse.x = p[0]; mouse.y = p[1];
    if (drag) {
      drag.b = p;
      if (Math.hypot(p[0] - drag.a[0], p[1] - drag.a[1]) > 2) drag.moved = true;
      draw();
      touchActivity();
    } else if (mouse.inside && phase === PHASE.MEASURE) {
      draw();
    }
  });

  window.addEventListener("mouseup", function (e) {
    if (popDrag) {
      popDrag = null;
      confirmPop.classList.remove("dragging");
      return;
    }
    if (win) { win = null; return; }
    if (pan) { pan = null; return; }
    if (!drag || phase !== PHASE.MEASURE) { drag = null; return; }

    var p = localPos(e);
    var dist = Math.hypot(p[0] - drag.a[0], p[1] - drag.a[1]);
    if (dist < MIN_DRAG_PX) {
      // 오조작 — 없던 일로 하고 계속 계측 상태를 유지한다(시간은 계속 흐름)
      drag = null;
      draw();
      return;
    }

    // ── 마지막 drop: 여기서 시간 측정 종료 ──
    durationMs = t0 === null ? null : Math.max(0, Math.round(performance.now() - t0));

    var a = canvasToOrig(drag.a[0], drag.a[1]);
    var b = canvasToOrig(p[0], p[1]);
    line = [clampToImage(a), clampToImage(b)];
    drag = null;
    phase = PHASE.CONFIRM;
    draw();
    openConfirm();
  });

  function clampToImage(o) {
    return [Math.max(0, Math.min(question.origW - 1, o[0])),
            Math.max(0, Math.min(question.origH - 1, o[1]))];
  }

  canvas.addEventListener("mouseenter", function () { mouse.inside = true; });
  canvas.addEventListener("mouseleave", function () { mouse.inside = false; draw(); });
  canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  canvas.addEventListener("wheel", function (e) {
    if (phase === PHASE.QUESTION) return;
    e.preventDefault();
    var p = localPos(e);
    zoomAt(p[0], p[1], e.deltaY < 0 ? 1.15 : 1 / 1.15);
    touchActivity();
  }, { passive: false });

  // -------------------------------------------------------------- 확인 팝업
  function openConfirm() {
    var mm = lengthMm(line);
    document.getElementById("cpVar").textContent = question.display;
    document.getElementById("cpMm").textContent = mm === null ? "—" : mm.toFixed(1);
    document.getElementById("cpTime").textContent =
      durationMs === null ? "소요시간 기록 실패" : (durationMs / 1000).toFixed(1) + "초";

    confirmPop.classList.remove("hidden");
    placeConfirm();
    document.getElementById("btnConfirm").focus();
  }

  /* 계측선의 우측 하단 모서리에 붙인다. 화면 밖으로 나가면 반대쪽으로 접는다. */
  function placeConfirm() {
    if (!line) return;
    var GAP = 14;
    var a = origToCanvas(line[0][0], line[0][1]);
    var b = origToCanvas(line[1][0], line[1][1]);
    var right = Math.max(a[0], b[0]);
    var left = Math.min(a[0], b[0]);
    var bottom = Math.max(a[1], b[1]);
    var top = Math.min(a[1], b[1]);

    var w = confirmPop.offsetWidth || 236;
    var h = confirmPop.offsetHeight || 210;
    var sw = stageMeasure.clientWidth, sh = stageMeasure.clientHeight;

    var x = right + GAP + popOffset.dx;
    var y = bottom + GAP + popOffset.dy;

    // 오른쪽/아래로 넘치면 계측선의 반대쪽에 놓는다
    if (x + w > sw - 8) x = left - w - GAP + popOffset.dx;
    if (y + h > sh - 8) y = top - h - GAP + popOffset.dy;

    confirmPop.style.left = clampNum(x, 8, sw - w - 8) + "px";
    confirmPop.style.top = clampNum(y, 8, sh - h - 8) + "px";
  }

  function clampNum(v, lo, hi) {
    if (hi < lo) return lo;
    return Math.max(lo, Math.min(hi, v));
  }

  function closeConfirm() { confirmPop.classList.add("hidden"); }

  function confirmAnswer(thenFinish) {
    if (busy || phase !== PHASE.CONFIRM || !line) return;
    busy = true;
    fetch("/measure/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        image_id: question.imageId,
        variable_key: question.variableKey,
        points: line,
        duration_ms: durationMs,
      }),
    })
      .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (pair) {
        busy = false;
        var ok = pair[0], j = pair[1];
        if (!ok || !j.ok) {
          toast(j && j.message ? j.message : "저장에 실패했습니다.", true);
          return;
        }
        applyProgress(j.progress);
        answers = j.answers || answers;
        renderAnswers();
        if (j.peers) renderPeers(j.peers);
        closeConfirm();
        if (thenFinish) {
          // 방금 계측은 저장된 뒤 종료한다 — 한 일이 사라지지 않게
          window.location.assign("/measure/paused");
          return;
        }
        advance();
      })
      .catch(function () {
        busy = false;
        toast("네트워크 오류 — 저장되지 않았습니다.", true);
      });
  }

  function requestRedoCurrent() {
    // 확정 전 재측정 요청 = 저장하지 않고 대기 pool에 그대로 두면 된다
    if (busy || phase !== PHASE.CONFIRM) return;
    closeConfirm();
    toast("이 문제를 대기 목록으로 남겨 두었습니다. 나중에 다시 출제됩니다.");
    advance();
  }

  // ------------------------------------------------------------ 다음 문제
  function advance() {
    if (nextQuestion) {
      question = nextQuestion;
      nextQuestion = null;
      showQuestionCard();
      return;
    }
    fetch("/measure/next", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { window.location.reload(); return; }
        applyProgress(j.progress);
        if (j.answers) { answers = j.answers; renderAnswers(); }
        if (j.peers) renderPeers(j.peers);
        if (j.done || !j.question) { window.location.assign("/measure"); return; }
        question = j.question;
        showQuestionCard();
      })
      .catch(function () { window.location.reload(); });
  }

  // 이 pool 에서 내가 끝낸 총 건수. 우측 목록은 최근 것만 보여주므로,
  // "몇 개 했나"는 목록 길이가 아니라 이 값으로 말해야 한다.
  var totalDone = (DATA.progress && DATA.progress.done) || 0;
  var LIST_LIMIT = DATA.listLimit || 20;

  // 「내 이력」에서 한 줄을 눌러 들어온 경우. 닫거나 재측정 요청하면 이력으로
  // 되돌려보내야 한다 — 보러 왔던 목록으로 돌아가는 게 자연스럽다.
  var fromHistory = !!DATA.reviewId;

  function applyProgress(p) {
    if (!p) return;
    document.getElementById("progFill").style.width = p.pct + "%";
    document.getElementById("progDone").textContent = p.done;
    document.getElementById("progTotal").textContent = p.total;
    document.getElementById("progPct").textContent = "(" + p.pct + "%)";
    totalDone = p.done;
    syncAnswerMeta();
  }

  // 목록 머리의 건수와 '더 보기' 안내를 현재 상태에 맞춘다.
  function syncAnswerMeta() {
    var countEl = document.getElementById("answerCount");
    if (countEl) {
      // 목록은 최근 LIST_LIMIT 건만 담고 있으므로 총 건수를 따로 보여준다
      countEl.textContent =
        totalDone > answers.length
          ? answers.length + " / " + totalDone
          : String(totalDone);
    }
    var more = document.getElementById("answerMore");
    if (more) more.hidden = !(totalDone > answers.length);
  }

  // -------------------------------------------------------- 계측자 현황
  // 동료의 '건수'만 보여준다 — 계측값은 보이면 안 된다(서로 눈가림 유지).
  // 문제가 넘어갈 때마다 서버가 최신 값을 실어 보내므로 그때마다 다시 그린다.
  function renderPeers(list) {
    var panel = document.getElementById("peerPanel");
    var body = document.getElementById("peerBody");
    if (!panel || !body) return;
    if (!list || list.length < 2) { panel.hidden = true; return; }
    panel.hidden = false;
    body.innerHTML = "";
    list.forEach(function (p) {
      var tr = document.createElement("tr");
      tr.innerHTML = '<td class="pr-name"></td>' +
                     '<td class="num pr-done"></td>' +
                     '<td class="num muted pr-pct"></td>';
      var nameEl = tr.querySelector(".pr-name");
      nameEl.textContent = p.name;
      if (p.is_self) nameEl.style.fontWeight = "700";
      tr.querySelector(".pr-done").textContent = p.done;
      tr.querySelector(".pr-pct").textContent = p.pct + "%";
      body.appendChild(tr);
    });
  }

  // ------------------------------------------------------------ 완료 목록
  function renderAnswers() {
    var ul = document.getElementById("answerList");
    ul.innerHTML = "";
    syncAnswerMeta();
    // 한 줄 = 항목명 · 길이(mm) · 완료시각. 최근 것이 맨 위(서버가 내림차순).
    // 소요시간은 일부러 뺐다 — 자기 속도를 보며 재면 태도가 바뀐다.
    answers.forEach(function (a) {
      var li = document.createElement("li");
      li.className = "answer-item";
      li.tabIndex = 0;
      li.innerHTML =
        '<span class="ai-var"><span class="ai-img"></span><span class="ai-name"></span></span>' +
        '<span class="ai-mm"></span>' +
        '<span class="ai-time">' +
          '<span class="ai-date"></span>' +
          '<span class="ai-clock"></span>' +
        '</span>';
      li.querySelector(".ai-img").textContent = "#" + a.imageId;
      li.querySelector(".ai-name").textContent = a.short;

      var mm = li.querySelector(".ai-mm");
      if (a.valueMm === null) {
        mm.textContent = "—";
      } else {
        mm.textContent = a.valueMm.toFixed(1);
        var u = document.createElement("span");
        u.className = "ai-mm-unit";
        u.textContent = "mm";
        mm.appendChild(u);
      }

      // 날짜를 시각 위에 둔다 — 여러 날에 걸쳐 재므로 시:분:초만으로는
      // 어느 날 것인지 알 수 없다.
      li.querySelector(".ai-date").textContent = a.atDate || "";
      li.querySelector(".ai-clock").textContent = a.atTime || "";
      li.title = "#" + a.imageId + " " + a.filename + " · " + a.display +
                 (a.at ? " · " + a.at : "") +
                 (a.attempt > 1 ? " · " + a.attempt + "번째 시도" : "");
      li.addEventListener("click", function () { openReview(a.id); });
      li.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openReview(a.id); }
      });
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------- 열람 모드
  // 진행 중이던 문제를 버리고 열람으로 넘어간다.
  //
  // 막지 않는 이유: 완료목록을 누른 시점에 이미 그 계측은 오염됐다. 시간은
  // 흘러버렸고 주의도 끊겼다. 억지로 끝내게 하면 '망가진 걸 아는 채로 낸 답'이
  // 자료에 남는다. 그냥 버리는 편이 낫다 — 버린 문제는 저장되지 않았으므로
  // 대기 pool에 그대로 남아 나중에 다시 무작위로 나온다.
  function openReview(measurementId) {
    if (phase === PHASE.MEASURE || phase === PHASE.CONFIRM) {
      abortFlash(function () { doOpenReview(measurementId); });
      return;
    }
    doOpenReview(measurementId);
  }

  var ABORT_FLASH_MS = 420;

  function abortFlash(then) {
    // 진행 중이던 계측을 즉시 무효화한다. 타이머도 같이 버린다.
    line = null; drag = null; t0 = null; durationMs = null;
    confirmPop.classList.add("hidden");

    var el = document.getElementById("abortFlash");
    if (!el) { then(); return; }
    el.hidden = false;
    window.setTimeout(function () {
      el.hidden = true;
      then();
    }, ABORT_FLASH_MS);
  }

  function doOpenReview(measurementId) {
    fetch("/measure/review/" + measurementId, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { toast("불러오지 못했습니다.", true); return; }
        review = j;
        phase = PHASE.REVIEW;
        // 열람용으로 question 자리를 잠시 빌린다 (좌표 변환에 필요)
        question = {
          imageId: j.image.id, variableKey: j.variable.key,
          display: j.variable.display, short: j.variable.short,
          instruction: j.variable.instruction, guide: j.variable.guide,
          filename: j.image.filename, origW: j.image.origW, origH: j.image.origH,
          renderScale: j.image.renderScale, pixelSpacingMm: j.image.pixelSpacingMm,
          pngUrl: j.image.pngUrl,
        };
        line = (j.points || []).map(function (p) { return [+p[0], +p[1]]; });
        if (line.length < 2) line = null;

        stageQuestion.classList.add("hidden");
        setBriefing(false);
        stageMeasure.classList.remove("hidden");
        confirmPop.classList.add("hidden");
        reviewBar.classList.remove("hidden");
        document.getElementById("rbLabel").textContent =
          "#" + j.image.id + " · " + j.variable.display + " · " +
          (j.value_mm === null ? "—" : j.value_mm.toFixed(1) + " mm") +
          (j.duration_ms ? " · " + (j.duration_ms / 1000).toFixed(1) + "초" : "");
        document.getElementById("hudVar").textContent = j.variable.display + " (열람)";
        lastMeasurementId = j.measurement_id;

        // 이미 재측정 요청된 계측에는 그 버튼을 숨긴다 — 눌러도 409 만 돌아온다.
        var rbRedo = document.getElementById("btnReviewRedo");
        if (rbRedo) rbRedo.hidden = (j.status !== "done");
        var rbClose = document.getElementById("btnReviewClose");
        if (rbClose && fromHistory) rbClose.textContent = "이력으로 돌아가기";

        imgReady = false; imgEnh = null; enhanced = false;
        sharpCache = {};
        syncEnhanceButton();

        // 새 Image 객체로 갈아 끼운다. 캐시에 이미 있으면 onload가 안 뜨는
        // 브라우저가 있어 complete 를 먼저 확인한다.
        var nimg = new Image();
        nimg.onload = function () {
          img = nimg; imgReady = true; resize(); fit(); draw();
        };
        nimg.onerror = function () { toast("영상을 불러오지 못했습니다.", true); };
        nimg.src = j.image.pngUrl;
        if (nimg.complete && nimg.naturalWidth) {
          img = nimg; imgReady = true; resize(); fit(); draw();
        }
      })
      .catch(function () { toast("불러오지 못했습니다.", true); });
  }

  function closeReview() {
    review = null;
    lastMeasurementId = null;
    reviewBar.classList.add("hidden");
    if (fromHistory) {
      // 이력에서 보러 왔으므로 이력으로 돌려보낸다
      window.location.assign("/history");
      return;
    }
    // 열람 전 문제로 복귀 — 다음 문제를 새로 받아 온다
    advance();
  }

  function requestRedoSaved() {
    if (busy || !lastMeasurementId) return;
    busy = true;
    fetch("/measure/redo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ measurement_id: lastMeasurementId }),
    })
      .then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (pair) {
        busy = false;
        var ok = pair[0], j = pair[1];
        if (!ok || !j.ok) { toast("처리하지 못했습니다.", true); return; }
        applyProgress(j.progress);
        answers = j.answers || answers;
        renderAnswers();
        if (j.peers) renderPeers(j.peers);
        toast(j.message + " 지금 다시 재지 않고 나중에 무작위로 다시 출제됩니다.");
        reviewBar.classList.add("hidden");
        if (fromHistory) {
          window.location.assign("/history");
          return;
        }
        advance();
      })
      .catch(function () { busy = false; toast("네트워크 오류.", true); });
  }

  // ------------------------------------------------------------ 툴바/토스트
  function syncEnhanceButton() {
    var btn = document.getElementById("btnEnhance");
    btn.textContent = "강화 " + (enhanced ? "ON" : "OFF");
    btn.classList.toggle("on", enhanced);
  }

  function toggleEnhance() {
    if (phase === PHASE.QUESTION) return;
    enhanced = !enhanced;
    syncEnhanceButton();
    if (enhanced && !imgEnh) {
      imgEnh = new Image();
      imgEnh.onload = draw;
      imgEnh.src = "/image/" + question.imageId + "/png?enhanced=1";
    }
    draw();
  }

  var toastTimer = null;
  function toast(msg, isError) {
    var el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.toggle("toast-error", !!isError);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.add("hidden"); }, 3600);
  }

  // 팝업 핸들 — 끌어서 위치를 옮긴다. 옮긴 만큼은 다음 문제에도 이어진다.
  document.getElementById("cpHandle").addEventListener("mousedown", function (e) {
    e.preventDefault();
    var r = confirmPop.getBoundingClientRect();
    var sr = stageMeasure.getBoundingClientRect();
    popDrag = {
      x0: e.clientX, y0: e.clientY,
      left0: r.left - sr.left, top0: r.top - sr.top,
      offx0: popOffset.dx, offy0: popOffset.dy,
    };
    confirmPop.classList.add("dragging");
    touchActivity();
  });

  document.getElementById("btnReveal").addEventListener("click", reveal);
  document.getElementById("btnConfirm").addEventListener("click", function () {
    confirmAnswer(false);
  });
  document.getElementById("btnFinish").addEventListener("click", function () {
    confirmAnswer(true);
  });
  document.getElementById("btnRedo").addEventListener("click", requestRedoCurrent);
  document.getElementById("btnEnhance").addEventListener("click", toggleEnhance);
  document.getElementById("btnSharpen").addEventListener("click", function () {
    setSharpen((sharpenLevel + 1) % 4);
  });
  document.getElementById("btnWinReset").addEventListener("click", resetWindow);
  document.getElementById("btnFit").addEventListener("click", function () { fit(); draw(); });
  document.getElementById("btnReviewRedo").addEventListener("click", requestRedoSaved);
  document.getElementById("btnReviewClose").addEventListener("click", closeReview);

  // -------------------------------------------------------------- 키보드
  window.addEventListener("keydown", function (e) {
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (!document.getElementById("idleOverlay").hidden) return;
    var k = e.key;

    if (phase === PHASE.QUESTION) {
      if (k === " " || k === "Enter") { e.preventDefault(); reveal(); }
    } else if (phase === PHASE.CONFIRM) {
      if (k === "Enter") { e.preventDefault(); confirmAnswer(false); }
      else if (k === "r" || k === "R" || k === "ㄱ") { e.preventDefault(); requestRedoCurrent(); }
      else if (k === "Escape") { e.preventDefault(); confirmAnswer(true); }
    } else if (phase === PHASE.REVIEW) {
      if (k === "Escape") { e.preventDefault(); closeReview(); }
      else if (k === "r" || k === "R" || k === "ㄱ") { e.preventDefault(); requestRedoSaved(); }
    }

    if (phase !== PHASE.QUESTION) {
      if (k === "Tab") {
        // 샤프닝 0 → 1 → 2 → 3 → 0. 브라우저 포커스 이동을 막는다.
        e.preventDefault();
        setSharpen((sharpenLevel + 1) % 4);
      }
      else if (k === "c" || k === "C" || k === "ㅊ") toggleEnhance();
      else if (k === "0") { fit(); draw(); }
      else if (k === "w" || k === "W" || k === "ㅈ") resetWindow();
    }
    touchActivity();
  });

  // ------------------------------------------------------- 무조작 잠금
  // 시간이 지표라 자리를 비운 채 흘러간 문제를 그대로 두면 안 된다.
  // 잠금이 풀리면 **새 문제부터** 다시 시작한다.
  var IDLE_LOCK_MS = DATA.idleLockMs || 60000;
  var PHASE1_MS = DATA.idlePhase1Ms || 10000;
  var lastActivity = performance.now();
  var idlePhase = 0;
  var phase1Timer = null;

  var idleOverlay = document.getElementById("idleOverlay");
  var idleDialog = document.getElementById("idleDialog");

  function touchActivity() {
    if (idlePhase !== 0) return;
    lastActivity = performance.now();
  }

  function openIdle1() {
    if (idlePhase !== 0) return;
    idlePhase = 1;
    idleOverlay.hidden = false;
    idleDialog.classList.remove("idle-dialog--flash");
    document.getElementById("idleMsg").hidden = false;
    document.getElementById("idlePhaseYes").hidden = false;
    document.getElementById("idlePhaseRestart").hidden = true;
    clearTimeout(phase1Timer);
    phase1Timer = setTimeout(openIdle2, PHASE1_MS);
  }

  function openIdle2() {
    idlePhase = 2;
    clearTimeout(phase1Timer);
    document.getElementById("idleMsg").hidden = true;
    document.getElementById("idlePhaseYes").hidden = true;
    document.getElementById("idlePhaseRestart").hidden = false;
    idleDialog.classList.add("idle-dialog--flash");
  }

  document.getElementById("btnIdleYes").addEventListener("click", function () {
    clearTimeout(phase1Timer);
    idlePhase = 0;
    idleOverlay.hidden = true;
    lastActivity = performance.now();
    // 잠긴 동안 흐른 시간이 섞이지 않도록, 계측 중이었다면 그 문제는 버린다
    if (phase === PHASE.MEASURE || phase === PHASE.CONFIRM) {
      line = null; drag = null; t0 = null;
      toast("자리를 비우신 동안의 문제는 대기 목록으로 남겨 두었습니다.");
      advance();
    }
  });
  document.getElementById("btnIdleRestart").addEventListener("click", function () {
    window.location.assign("/measure");
  });

  setInterval(function () {
    if (phase === PHASE.MEASURE && t0 !== null) {
      document.getElementById("timerElapsed").textContent =
        ((performance.now() - t0) / 1000).toFixed(1) + "s";
    }
    if (idlePhase === 0 && performance.now() - lastActivity >= IDLE_LOCK_MS) openIdle1();
  }, 100);

  ["click", "keydown", "wheel", "touchstart", "mousemove"].forEach(function (ev) {
    document.addEventListener(ev, function () {
      if (!idleOverlay.hidden) return;
      touchActivity();
    }, ev === "wheel" || ev === "mousemove" ? { passive: true } : false);
  });

  // ---------------------------------------------------------------- 시작
  window.addEventListener("resize", function () {
    if (phase !== PHASE.QUESTION) { resize(); }
  });

  syncWindowReadout();
  setSharpen(0);
  renderAnswers();
  renderPeers(DATA.peers);
  if (DATA.reviewId) {
    // 이력에서 넘어왔다 — 문제 카드를 건너뛰고 그 계측을 바로 펼친다.
    // 문제는 뒤에 그대로 대기하고, 타이머는 문제 카드를 지나야 돌기 시작한다.
    showQuestionCard();
    doOpenReview(DATA.reviewId);
  } else {
    showQuestionCard();
  }
})();
