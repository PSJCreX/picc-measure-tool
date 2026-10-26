/* 다중 파일 업로드 + 진행률 바.
 *
 * fetch()는 업로드 진행률 이벤트를 주지 않으므로 XMLHttpRequest를 쓴다.
 * 파일을 배치로 끊어 보낸다 — 519장을 한 요청에 담으면 타임아웃/메모리 문제가
 * 생기고, 중간에 실패했을 때 어디까지 됐는지 알 수 없다.
 */
(function () {
  "use strict";

  var BATCH_SIZE = 6;         // 한 요청에 담을 파일 수
  var queue = [];             // {file, row, state}
  var running = false;

  var drop = document.getElementById("drop");
  var input = document.getElementById("fileInput");
  var list = document.getElementById("upList");
  var btnUpload = document.getElementById("btnUpload");
  var btnClear = document.getElementById("btnClear");
  var wrapProg = document.getElementById("progressWrap");
  var fill = document.getElementById("progressFill");
  var pct = document.getElementById("progressPct");
  var label = document.getElementById("progressLabel");
  var summary = document.getElementById("summary");
  // ------------------------------------------------------------ 파일 수집

  function addFiles(files) {
    Array.prototype.forEach.call(files, function (f) {
      var row = document.createElement("div");
      row.className = "up-item";
      row.innerHTML =
        '<span class="up-name"></span>' +
        '<span class="up-msg"></span>' +
        '<span class="up-state pending">대기</span>';
      row.querySelector(".up-name").textContent = f.name;
      list.appendChild(row);
      queue.push({ file: f, row: row, state: "pending" });
    });
    list.classList.remove("hidden");
    btnUpload.disabled = queue.length === 0 || running;
    btnClear.disabled = queue.length === 0 || running;
    updateSummary();
  }

  drop.addEventListener("click", function () { input.click(); });
  input.addEventListener("change", function () {
    addFiles(input.files);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) {
      e.preventDefault(); e.stopPropagation();
      drop.classList.add("hot");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) {
      e.preventDefault(); e.stopPropagation();
      drop.classList.remove("hot");
    });
  });
  drop.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });

  btnClear.addEventListener("click", function () {
    if (running) return;
    queue = [];
    list.innerHTML = "";
    list.classList.add("hidden");
    wrapProg.classList.add("hidden");
    btnUpload.disabled = true;
    btnClear.disabled = true;
    updateSummary();
  });

  // --------------------------------------------------------------- 상태

  function setRow(item, state, msg) {
    item.state = state;
    var el = item.row.querySelector(".up-state");
    el.className = "up-state " + state;
    el.textContent = ({
      pending: "대기", sending: "전송 중", processing: "처리 중",
      ok: "등재됨", duplicate: "중복", error: "실패"
    })[state] || state;
    if (msg !== undefined) item.row.querySelector(".up-msg").textContent = msg || "";
  }

  function updateSummary() {
    var n = { ok: 0, duplicate: 0, error: 0 };
    queue.forEach(function (q) { if (n[q.state] !== undefined) n[q.state]++; });
    var done = n.ok + n.duplicate + n.error;
    summary.textContent = queue.length
      ? "총 " + queue.length + "건 · 등재 " + n.ok + " · 중복 " + n.duplicate +
        " · 실패 " + n.error
      : "";
    return { done: done, ok: n.ok, dup: n.duplicate, err: n.error };
  }

  function setProgress(fraction, text) {
    var p = Math.round(fraction * 100);
    fill.style.width = p + "%";
    pct.textContent = p + "%";
    if (text) label.textContent = text;
  }

  // ------------------------------------------------------------- 업로드

  function sendBatch(batch, onDone) {
    var fd = new FormData();
    batch.forEach(function (item) { fd.append("files", item.file, item.file.name); });
    // 어느 pool로 넣을지 — 라디오 선택값. 연습은 연구 산출물에 들어가지 않는다.
    var sel = document.querySelector('input[name="pool"]:checked');
    fd.append("pool", sel ? sel.value : "real");

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/admin/upload", true);
    xhr.withCredentials = true;

    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      batch.forEach(function (item) {
        if (item.state === "pending" || item.state === "sending") {
          setRow(item, e.loaded >= e.total ? "processing" : "sending");
        }
      });
      onDone.progress(e.loaded / e.total);
    };

    xhr.onload = function () {
      var payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch (err) { payload = null; }
      if (xhr.status !== 200 || !payload || !payload.ok) {
        batch.forEach(function (item) {
          setRow(item, "error", "서버 오류 (" + xhr.status + ")");
        });
        onDone.done();
        return;
      }
      // ZIP 하나가 여러 결과를 낼 수 있으므로 파일명으로 매칭한다
      var byName = {};
      batch.forEach(function (item) { byName[item.file.name] = item; });
      var extra = [];
      payload.results.forEach(function (r) {
        var item = byName[r.filename];
        if (item) {
          setRow(item, r.status, r.message);
          delete byName[r.filename];
        } else {
          extra.push(r);
        }
      });
      // ZIP 내부 파일 등 매칭되지 않은 결과는 행을 새로 만든다
      extra.forEach(function (r) {
        var row = document.createElement("div");
        row.className = "up-item";
        row.innerHTML =
          '<span class="up-name"></span><span class="up-msg"></span>' +
          '<span class="up-state"></span>';
        row.querySelector(".up-name").textContent = "└ " + r.filename;
        list.appendChild(row);
        var fake = { row: row, state: "pending", file: { name: r.filename } };
        queue.push(fake);
        setRow(fake, r.status, r.message);
      });
      // 응답에 없는 파일은 원인 불명 실패로 남긴다
      Object.keys(byName).forEach(function (k) {
        setRow(byName[k], "error", "응답 없음");
      });
      onDone.done();
    };

    xhr.onerror = function () {
      batch.forEach(function (item) { setRow(item, "error", "네트워크 오류"); });
      onDone.done();
    };

    xhr.send(fd);
  }

  btnUpload.addEventListener("click", function () {
    if (running || !queue.length) return;
    running = true;
    btnUpload.disabled = true;
    btnClear.disabled = true;
    wrapProg.classList.remove("hidden");

    var pendings = queue.filter(function (q) { return q.state === "pending"; });
    var total = pendings.length;
    var batches = [];
    for (var i = 0; i < pendings.length; i += BATCH_SIZE) {
      batches.push(pendings.slice(i, i + BATCH_SIZE));
    }

    var batchIdx = 0;
    var filesDone = 0;

    function runNext() {
      if (batchIdx >= batches.length) {
        var s = updateSummary();
        setProgress(1, "완료 — 등재 " + s.ok + " · 중복 " + s.dup + " · 실패 " + s.err);
        running = false;
        btnClear.disabled = false;
        return;
      }
      var batch = batches[batchIdx];
      setProgress(
        filesDone / total,
        "업로드 중 " + (filesDone + 1) + "–" +
          Math.min(filesDone + batch.length, total) + " / " + total
      );
      sendBatch(batch, {
        progress: function (frac) {
          setProgress((filesDone + batch.length * frac) / total);
        },
        done: function () {
          filesDone += batch.length;
          batchIdx += 1;
          updateSummary();
          setProgress(filesDone / total);
          runNext();
        }
      });
    }
    runNext();
  });

  window.addEventListener("beforeunload", function (e) {
    if (!running) return;
    e.preventDefault();
    e.returnValue = "";
  });
})();
