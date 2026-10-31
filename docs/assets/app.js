(function () {
  "use strict";
  var root = document.documentElement.getAttribute("data-root") || "";
  var html = document.documentElement;
  var body = document.body;

  function $(sel, el) { return (el || document).querySelector(sel); }
  function $$(sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); }

  /* Theme */
  var stored = null;
  try { stored = localStorage.getItem("miot-docs-theme"); } catch (e) {}
  var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var theme = stored || (prefersDark ? "dark" : "light");
  html.setAttribute("data-theme", theme);
  function syncThemeIcon() {
    var sun = $(".js-theme-sun"), moon = $(".js-theme-moon");
    if (!sun || !moon) return;
    var dark = html.getAttribute("data-theme") === "dark";
    sun.style.display = dark ? "none" : "";
    moon.style.display = dark ? "" : "none";
  }
  syncThemeIcon();
  $$(".js-theme-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
      html.setAttribute("data-theme", next);
      try { localStorage.setItem("miot-docs-theme", next); } catch (e) {}
      syncThemeIcon();
    });
  });

  /* Mobile sidebar */
  var sidebar = $(".sidebar");
  var mask = $(".sidebar-mask");
  function closeSidebar() {
    if (sidebar) sidebar.classList.remove("open");
    if (mask) mask.classList.remove("open");
    $$(".js-mobile-menu").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }
  $$(".js-mobile-menu").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!sidebar) return;
      var open = !sidebar.classList.contains("open");
      sidebar.classList.toggle("open", open);
      if (mask) mask.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });
  if (mask) mask.addEventListener("click", closeSidebar);
  $$(".sidebar a").forEach(function (a) { a.addEventListener("click", closeSidebar); });

  /* Code copy */
  $$(".content pre, .code-card pre").forEach(function (pre) {
    var code = pre.querySelector("code") || pre;
    var btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.type = "button";
    btn.textContent = "复制";
    btn.addEventListener("click", function () {
      var text = code.innerText;
      var done = function () {
        btn.textContent = "已复制";
        setTimeout(function () { btn.textContent = "复制"; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
      } else {
        fallbackCopy(text, done);
      }
    });
    pre.appendChild(btn);
  });
  function fallbackCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (e) {}
    document.body.removeChild(ta);
  }

  /* Sidebar active */
  var page = body.getAttribute("data-page") || "";
  $$(".sidebar-link").forEach(function (a) {
    var href = (a.getAttribute("href") || "").split("#")[0];
    var target = href.split("/").pop().replace(".html", "");
    if (target === page) a.classList.add("active");
  });

  /* Outline + scroll spy */
  var outline = $(".outline");
  var headings = $$(".content h2, .content h3");
  if (outline) {
    var outlineLinks = [];
    headings.forEach(function (h) {
      if (!h.id) return;
      var a = document.createElement("a");
      a.href = "#" + h.id;
      a.textContent = h.textContent.replace(/\s+$/, "");
      if (h.tagName === "H3") a.className = "h3";
      a.dataset.target = h.id;
      outline.appendChild(a);
      outlineLinks.push(a);
    });
    var setActive = function (id) {
      outlineLinks.forEach(function (a) { a.classList.toggle("active", a.dataset.target === id); });
    };
    var onScroll = function () {
      var current = headings.length ? headings[0].id : "";
      var top = window.scrollY + 110;
      headings.forEach(function (h) { if (h.offsetTop <= top) current = h.id; });
      setActive(current);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* Search */
  var overlay = $(".search-overlay");
  var input = $(".search-input");
  var results = $(".search-results");
  var index = null;
  var selected = 0;
  var rendered = [];

  function openSearch() {
    if (!overlay) return;
    overlay.classList.add("open");
    document.body.style.overflow = "hidden";
    if (input) { input.value = ""; input.focus(); }
    renderResults("");
  }
  function closeSearch() {
    if (!overlay) return;
    overlay.classList.remove("open");
    document.body.style.overflow = "";
    selected = 0;
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function renderResults(q) {
    if (!results) return;
    q = (q || "").trim().toLowerCase();
    var items = (index || []);
    rendered = [];
    items.forEach(function (item) {
      var hay = (item.title + " " + item.desc + " " + (item.headings || []).join(" ") + " " + (item.keywords || "")).toLowerCase();
      if (q && hay.indexOf(q) < 0) return;
      rendered.push(item);
    });
    if (!rendered.length) {
      results.innerHTML = '<div class="search-empty">没有找到相关内容，换个关键词试试。</div>';
      return;
    }
    var html = "";
    rendered.slice(0, 10).forEach(function (item, i) {
      html += '<a class="search-item' + (i === selected ? " sel" : "") + '" href="' + root + item.url + '">' +
        '<div class="s-title">' + esc(item.title) + '</div>' +
        '<div class="s-desc">' + esc(item.desc) + '</div></a>';
    });
    results.innerHTML = html;
  }
  if (overlay) {
    if (!index) {
      fetch(root + "assets/search-index.json").then(function (r) { return r.json(); }).then(function (data) {
        index = data || [];
        if (overlay.classList.contains("open")) renderResults(input ? input.value : "");
      }).catch(function () { index = []; });
    }
    $$(".js-search-open").forEach(function (b) { b.addEventListener("click", openSearch); });
    $$(".js-search-close").forEach(function (b) { b.addEventListener("click", closeSearch); });
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closeSearch(); });
    if (input) {
      input.addEventListener("input", function () { selected = 0; renderResults(input.value); });
      input.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") {
          e.preventDefault(); selected = Math.min(selected + 1, Math.min(rendered.length, 10) - 1);
          renderResults(input.value);
        } else if (e.key === "ArrowUp") {
          e.preventDefault(); selected = Math.max(0, selected - 1);
          renderResults(input.value);
        } else if (e.key === "Enter") {
          if (rendered[selected]) window.location.href = root + rendered[selected].url;
        } else if (e.key === "Escape") {
          closeSearch();
        }
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeSearch();
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); openSearch();
      } else if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
        e.preventDefault(); openSearch();
      }
    });
  }
})();
