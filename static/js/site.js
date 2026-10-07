// RBG Photography: small enhancements. Everything works without JavaScript too.
(function () {
  document.documentElement.classList.add("js");

  // Mobile menu
  var toggle = document.querySelector(".menu-toggle");
  var nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "Close" : "Menu";
    });
  }

  // Lightbox for portfolio and client galleries (links with class "lb")
  var links = Array.prototype.slice.call(document.querySelectorAll("a.lb"));
  if (links.length) {
    var box = document.createElement("div");
    box.className = "lightbox";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Photo viewer");
    box.innerHTML = '<img alt=""><button class="lb-close" aria-label="Close">Close</button>' +
      '<button class="lb-prev" aria-label="Previous photo">&#8249;</button>' +
      '<button class="lb-next" aria-label="Next photo">&#8250;</button>' +
      '<a class="lb-dl" hidden>Download</a>';
    document.body.appendChild(box);
    var img = box.querySelector("img"), dl = box.querySelector(".lb-dl"), idx = 0, last = null;

    function show(i) {
      idx = (i + links.length) % links.length;
      var a = links[idx], thumb = a.querySelector("img");
      img.src = a.href;
      img.alt = thumb ? thumb.alt : "";
      if (a.dataset.download) { dl.href = a.dataset.download; dl.hidden = false; } else { dl.hidden = true; }
    }
    function close() { box.classList.remove("open"); img.removeAttribute("src"); if (last) last.focus(); }

    links.forEach(function (a, i) {
      a.addEventListener("click", function (e) {
        e.preventDefault(); last = a; show(i); box.classList.add("open"); box.querySelector(".lb-close").focus();
      });
    });
    box.querySelector(".lb-close").addEventListener("click", close);
    box.querySelector(".lb-prev").addEventListener("click", function () { show(idx - 1); });
    box.querySelector(".lb-next").addEventListener("click", function () { show(idx + 1); });
    box.addEventListener("click", function (e) { if (e.target === box) close(); });
    document.addEventListener("keydown", function (e) {
      if (!box.classList.contains("open")) return;
      if (e.key === "Escape") close();
      if (e.key === "ArrowLeft") show(idx - 1);
      if (e.key === "ArrowRight") show(idx + 1);
    });
  }

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

  // Book form: submit in place, fall back to a normal post on any error
  var form = document.getElementById("inquiry-form");
  if (form && window.fetch) {
    var status = form.querySelector(".form-status");
    var params = new URLSearchParams(location.search);
    var preset = params.get("session");
    if (preset) {
      var sel = form.querySelector("select[name=session_type]");
      Array.prototype.forEach.call(sel.options, function (o) { if (o.value === preset) sel.value = preset; });
    }
    var place = params.get("location"), where = form.querySelector("input[name=location]");
    if (place && where && !where.value) where.value = place;

    // Promo codes are checked as soon as they're entered, and again before sending
    var promo = form.querySelector("input[name=promo]"), promoMsg = form.querySelector(".promo-status");
    var checked = { code: "", ok: true };
    function checkPromo() {
      var code = promo ? promo.value.trim() : "";
      if (!code) { checked = { code: "", ok: true }; promoMsg.textContent = ""; promoMsg.className = "promo-status"; return Promise.resolve(true); }
      if (checked.code === code.toUpperCase()) return Promise.resolve(checked.ok);
      promoMsg.textContent = "Checking…"; promoMsg.className = "promo-status";
      return fetch("/api/promo?code=" + encodeURIComponent(code)).then(function (r) { return r.json(); }).then(function (d) {
        checked = { code: code.toUpperCase(), ok: !!d.ok };
        promoMsg.textContent = d.ok ? "\u2713 " + d.offer : d.error;
        promoMsg.className = "promo-status " + (d.ok ? "ok" : "bad");
        return checked.ok;
      }).catch(function () { promoMsg.textContent = ""; return true; });  // the server checks it again anyway
    }
    if (promo) {
      if (params.get("promo")) promo.value = params.get("promo");
      promo.addEventListener("change", checkPromo);
      form.querySelector("[data-promo-check]").addEventListener("click", checkPromo);
      promo.addEventListener("input", function () { if (checked.code !== promo.value.trim().toUpperCase()) { promoMsg.textContent = ""; promoMsg.className = "promo-status"; } });
      if (promo.value) checkPromo();
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (promo && promo.value.trim() && checked.code !== promo.value.trim().toUpperCase()) {
        return checkPromo().then(function (ok) { if (ok) form.requestSubmit ? form.requestSubmit() : send(); else promo.focus(); });
      }
      if (promo && promo.value.trim() && !checked.ok) { promo.focus(); return; }
      send();
    });
    function send() {
      var btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      status.textContent = "Sending…";
      fetch(form.action, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(new FormData(form)).toString()
      }).then(function (r) { return r.json(); }).then(function (data) {
        if (data.ok) { location.href = "/thanks"; return; }
        status.innerHTML = ""; var p = document.createElement("p"); p.className = "form-error";
        p.textContent = data.error || "Something went wrong. Please try again."; status.appendChild(p);
        btn.disabled = false;
      }).catch(function () { form.submit(); });
    }
  }
})();
