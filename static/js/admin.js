// RBG Photography admin: photo uploads (one file per request), confirmations, copy buttons.
(function () {
  // Ask before destructive actions
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
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

    if (input) input.addEventListener("change", function () { upload(input.files); input.value = ""; });
    if (zone.classList.contains("dropzone")) {
      ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("over"); }); });
      ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("over"); }); });
      zone.addEventListener("drop", function (e) { upload(e.dataTransfer.files); });
    }
  });
})();
