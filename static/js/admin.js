
// Photo editor dialog: crop a new photo to a fixed shape, or pick a photo's focus point.
var photoTool = (function () {
  var dlg, stage, img, frame, dot, title, hint, saveBtn, state = {};

  function build() {
    dlg = document.createElement("dialog");
    dlg.className = "photo-tool";
    dlg.innerHTML =
      '<h2></h2><p class="muted small hint"></p>' +
      '<div class="pt-stage"><img alt=""><div class="pt-frame"><span class="pt-handle" aria-hidden="true"></span></div>' +
      '<span class="pt-dot" aria-hidden="true"></span></div>' +
      '<div class="row-actions"><button type="button" class="btn small pt-save">Save</button>' +
      '<button type="button" class="btn ghost small pt-cancel">Cancel</button></div>';
    document.body.appendChild(dlg);
    stage = dlg.querySelector(".pt-stage"); img = stage.querySelector("img");
    frame = stage.querySelector(".pt-frame"); dot = stage.querySelector(".pt-dot");
    title = dlg.querySelector("h2"); hint = dlg.querySelector(".hint"); saveBtn = dlg.querySelector(".pt-save");
    dlg.querySelector(".pt-cancel").addEventListener("click", close);
    dlg.addEventListener("cancel", function () { cleanup(); });
    saveBtn.addEventListener("click", function () { if (state.onSave) state.onSave(); });

    // Drag inside the frame to move it, drag the corner handle to resize it.
    stage.addEventListener("pointerdown", function (e) {
      if (state.mode === "focus") { setDot(e); stage.setPointerCapture(e.pointerId); state.drag = "dot"; return; }
      if (e.target.classList.contains("pt-handle")) state.drag = "size";
      else if (e.target === frame) state.drag = "move";
      else return;
      e.preventDefault();
      stage.setPointerCapture(e.pointerId);
      state.start = { x: e.clientX, y: e.clientY, f: Object.assign({}, state.f) };
    });
    stage.addEventListener("pointermove", function (e) {
      if (!state.drag) return;
      if (state.drag === "dot") return setDot(e);
      var dx = e.clientX - state.start.x, dy = e.clientY - state.start.y, f = state.start.f;
      var W = img.clientWidth, H = img.clientHeight, a = state.aspect;
      if (state.drag === "move") {
        state.f.x = Math.min(Math.max(0, f.x + dx), W - f.w);
        state.f.y = Math.min(Math.max(0, f.y + dy), H - f.h);
      } else {
        var w = Math.max(40, Math.max(f.w + dx, (f.h + dy) * a));
        w = Math.min(w, W - f.x, (H - f.y) * a);
        state.f.w = w; state.f.h = w / a;
      }
      drawFrame();
    });
    ["pointerup", "pointercancel"].forEach(function (ev) { stage.addEventListener(ev, function () { state.drag = null; }); });
  }

  function setDot(e) {
    var r = img.getBoundingClientRect();
    state.xy = [Math.min(100, Math.max(0, (e.clientX - r.left) / r.width * 100)),
                Math.min(100, Math.max(0, (e.clientY - r.top) / r.height * 100))];
    drawDot();
  }
  function drawDot() {
    dot.style.left = (img.offsetLeft + img.clientWidth * state.xy[0] / 100) + "px";
    dot.style.top = (img.offsetTop + img.clientHeight * state.xy[1] / 100) + "px";
  }
  function drawFrame() {
    var f = state.f;
    frame.style.left = (img.offsetLeft + f.x) + "px"; frame.style.top = (img.offsetTop + f.y) + "px";
    frame.style.width = f.w + "px"; frame.style.height = f.h + "px";
  }
  function cleanup() { if (state.url) URL.revokeObjectURL(state.url); state = {}; }
  function close() { dlg.close(); cleanup(); }

  function open(mode, src, ready) {
    if (!dlg) build();
    state = { mode: mode };
    dlg.classList.toggle("focus-mode", mode === "focus");
    saveBtn.disabled = false; saveBtn.textContent = "Save";
    img.onload = function () { ready(); };
    img.onerror = function () { alert("That photo couldn't be opened. Try a JPEG or PNG."); close(); };
    img.src = src;
    dlg.showModal();
  }

  return {
    // Let Rachel frame `file` at width x height, then hand back a JPEG File of that size.
    crop: function (file, width, height, name, done) {
      var url = URL.createObjectURL(file);
      open("crop", url, function () {
        state.url = url;
        var W = img.clientWidth, H = img.clientHeight, a = width / height;
        var w = Math.min(W, H * a);
        state.aspect = a;
        state.f = { w: w, h: w / a, x: (W - w) / 2, y: (H - w / a) / 2 };
        drawFrame();
        var small = img.naturalWidth * (w / W) < width * 0.6;
        hint.textContent = "Drag the box to choose what shows, and drag its corner to zoom in. " +
          (small ? "This photo is on the small side and may look soft; a larger original works best." : "");
      });
      title.textContent = "Crop " + name;
      state.onSave = function () {
        var k = img.naturalWidth / img.clientWidth, f = state.f;
        var sx = f.x * k, sy = f.y * k, sw = f.w * k, sh = f.h * k;
        var outW = Math.round(Math.min(width, sw)), outH = Math.round(outW * height / width);
        var c = document.createElement("canvas");
        c.width = outW; c.height = outH;
        var g = c.getContext("2d");
        g.imageSmoothingQuality = "high";
        g.drawImage(img, sx, sy, sw, sh, 0, 0, outW, outH);
        saveBtn.disabled = true; saveBtn.textContent = "Saving…";
        c.toBlob(function (blob) {
          close();
          done(new File([blob], name, { type: "image/jpeg" }));
        }, "image/jpeg", 0.9);
      };
    },
    // Pick the spot that stays in view when screens trim the photo.
    focus: function (src, xy, name, done) {
      open("focus", src, function () { drawDot(); });
      state.xy = xy;
      title.textContent = "Focus point for " + name;
      hint.textContent = "Tap the part of the photo that should always stay in view, like a face. " +
        "Phones and wide screens trim the edges around it.";
      state.onSave = function () {
        saveBtn.disabled = true; saveBtn.textContent = "Saving…";
        var body = new URLSearchParams({ name: name, x: state.xy[0].toFixed(1), y: state.xy[1].toFixed(1) });
        fetch("/admin/photos/focus", { method: "POST", body: body })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (!d.ok) throw new Error(d.error); close(); done(); })
          .catch(function (err) { alert("That didn't save: " + err.message); saveBtn.disabled = false; saveBtn.textContent = "Save"; });
      };
    }
  };
})();

// RBG Photography admin: photo uploads (one file per request), confirmations, copy buttons.
(function () {
  // Ask before destructive actions
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  document.querySelectorAll("button[data-confirm]").forEach(function (b) {
    b.addEventListener("click", function (e) { if (!confirm(b.dataset.confirm)) e.preventDefault(); });
  });

  // Number pickers (adults and kids): add minus and plus buttons around each number box
  Array.prototype.forEach.call(document.querySelectorAll(".headcount input[type=number]"), function (input) {
    var wrap = document.createElement("span"), name = input.labels && input.labels[0] ? input.labels[0].textContent.toLowerCase() : "";
    wrap.className = "stepper";
    input.parentNode.insertBefore(wrap, input);
    function button(step, text, label) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = text; b.setAttribute("aria-label", label);
      b.addEventListener("click", function () {
        var v = (parseInt(input.value, 10) || 0) + step;
        input.value = Math.max(Number(input.min) || 0, Math.min(Number(input.max) || 30, v));
      });
      return b;
    }
    wrap.appendChild(button(-1, "\u2212", "Fewer " + name));
    wrap.appendChild(input);
    wrap.appendChild(button(1, "+", "More " + name));
  });

  // Template picker on the email screen reloads with the chosen template
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  // Copy share text
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.dataset.copy);
      t.select();
      (navigator.clipboard ? navigator.clipboard.writeText(t.value) : Promise.reject()).catch(function () {
        document.execCommand("copy");
      }).finally(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy message"; }, 1800); });
    });
  });

  // Uploads: any element with data-upload holds a file input (and maybe accepts drops)
  document.querySelectorAll("[data-upload]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var box = zone.querySelector(".progress") || (zone.closest("figcaption") || zone.parentNode).querySelector(".progress");

    function say(html) { if (box) box.innerHTML = html; }

    function upload(files) {
      files = Array.prototype.slice.call(files).filter(function (f) { return /^image\//.test(f.type) || /\.(jpe?g|png|webp)$/i.test(f.name); });
      if (!files.length) return;
      var done = 0, failed = [];
      function next(i) {
        if (i >= files.length) {
          if (failed.length) {
            say('<span class="err">' + failed.map(function (m) { return m.replace(/</g, "&lt;"); }).join("<br>") + "</span>" +
                (done ? "<br>" + done + " uploaded. Refresh to see them." : ""));
          } else { say("Done. Refreshing…"); location.reload(); }
          return;
        }
        var f = files[i];
        say("Uploading " + (i + 1) + " of " + files.length + "…");
        var sep = zone.dataset.upload.indexOf("?") >= 0 ? "&" : "?";
        var url = zone.dataset.upload + (zone.dataset.upload.indexOf("name=") >= 0 ? "" : sep + "name=" + encodeURIComponent(f.name));
        fetch(url, { method: "POST", body: f, headers: { "Content-Type": f.type || "application/octet-stream" } })
          .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Upload failed (" + r.status + ")" }; }); })
          .then(function (d) { if (d.ok) done++; else failed.push(f.name + ": " + d.error); })
          .catch(function () { failed.push(f.name + ": the connection dropped"); })
          .then(function () { next(i + 1); });
      }
      next(0);
    }

    if (input) input.addEventListener("change", function () {
      var files = input.files, shape = (zone.dataset.crop || "").split("x");
      var name = decodeURIComponent((zone.dataset.upload.match(/name=([^&]+)/) || [])[1] || "photo.jpg");
      if (shape.length === 2 && files.length === 1 && /\.jpe?g$/i.test(name)) {
        photoTool.crop(files[0], +shape[0], +shape[1], name, function (f) { upload([f]); });
      } else upload(files);
      input.value = "";
    });
    if (zone.classList.contains("dropzone")) {
      ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("over"); }); });
      ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("over"); }); });
      zone.addEventListener("drop", function (e) { upload(e.dataTransfer.files); });
    }
  });
})();

// Admin sidebar on small screens
(function () {
  var btn = document.querySelector(".side-toggle"), side = document.getElementById("sidebar");
  if (!btn || !side) return;
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    var open = side.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", function (e) {
    if (side.classList.contains("open") && !side.contains(e.target)) { side.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); }
  });
})();

// Focus point buttons on the Site photos page
document.querySelectorAll("[data-focus]").forEach(function (b) {
  b.addEventListener("click", function () {
    photoTool.focus(b.dataset.src, b.dataset.xy.split(",").map(Number), b.dataset.focus, function () { location.reload(); });
  });
});
