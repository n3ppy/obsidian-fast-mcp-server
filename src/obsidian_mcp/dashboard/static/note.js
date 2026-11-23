// The note viewer's script: everything a note page does in the browser.
//
// - Math (.math): typeset by KaTeX, loaded only when the note has some.
// - Diagrams (.diagram): drawn by Pele, Obsidian's own Mermaid renderer, loaded only when
//   the note has one. Links to notes inside a diagram go through the dashboard's resolver.
// - Code blocks get a Copy button.
// - A cited range in the address (#L4-L5) highlights those lines' blocks.
//
// Everything is served from this site; the page's Content-Security-Policy allows no
// other scripts and no inline ones. Without this script the note still reads: math and
// diagrams show their source.
(function () {
  "use strict";
  var note = document.querySelector("article[data-note]");
  if (!note) return;
  var assets = note.getAttribute("data-assets") || "";
  var opener = note.getAttribute("data-open") || "";

  function script(path) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = assets + path;
      el.onload = resolve;
      el.onerror = reject;
      document.head.appendChild(el);
    });
  }

  function stylesheet(path) {
    var el = document.createElement("link");
    el.rel = "stylesheet";
    el.href = assets + path;
    document.head.appendChild(el);
  }

  function problem(where, message) {
    var p = document.createElement("p");
    p.className = "render-error";
    p.textContent = message;  // never HTML: the message can quote the note
    where.appendChild(p);
  }

  // -- math ----------------------------------------------------------------------------
  var maths = note.querySelectorAll(".math");
  if (maths.length) {
    stylesheet("vendor/katex/katex.min.css");
    script("vendor/katex/katex.min.js")
      .then(function () { return script("vendor/katex/contrib/mhchem.min.js").catch(function () {}); })
      .then(function () {
        maths.forEach(function (el) {
          var block = el.classList.contains("block");
          var tex = el.textContent;
          try {
            window.katex.render(tex, el, {
              displayMode: block, throwOnError: false, trust: false, strict: "ignore",
              maxExpand: 1000, maxSize: 50, output: "htmlAndMathml"
            });
            el.classList.add("typeset");
          } catch (err) {
            el.textContent = tex;
            el.classList.add("failed");
          }
        });
      })
      .catch(function () { note.classList.add("no-math"); });
  }

  // -- diagrams --------------------------------------------------------------------------
  var diagrams = note.querySelectorAll(".diagram[data-diagram]");
  if (diagrams.length) {
    script("vendor/pele/pele.global.js").then(function () {
      diagrams.forEach(function (el) {
        var source = el.querySelector(".diagram-source");
        var text = source ? source.textContent : "";
        var box = document.createElement("div");
        box.className = "diagram-drawing";
        el.insertBefore(box, source);
        var failed = false;
        try {
          window.pele.mount(box, text, {
            links: true, images: false, linkSchemes: ["https", "http", "mailto"],
            onError: function (err) {
              failed = true;
              box.remove();
              problem(el, "This diagram couldn't be drawn: " + (err && err.message ? err.message : String(err)));
            }
          });
        } catch (err) {
          failed = true;
          box.remove();
          problem(el, "This diagram couldn't be drawn: " + (err && err.message ? err.message : String(err)));
        }
        if (failed) return;
        box.querySelectorAll("a.internal-link[data-href]").forEach(function (a) {
          a.setAttribute("href", opener + encodeURIComponent(a.getAttribute("data-href")));
        });
        box.querySelectorAll("a[href]:not(.internal-link)").forEach(function (a) {
          a.setAttribute("target", "_blank");
          a.setAttribute("rel", "noopener noreferrer");
        });
        el.classList.add("drawn");
      });
    }).catch(function () { note.classList.add("no-diagrams"); });
  }

  // -- copy buttons on code ----------------------------------------------------------------
  if (navigator.clipboard) {
    note.querySelectorAll(".code-block").forEach(function (block) {
      var pre = block.querySelector("pre");
      if (!pre) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy";
      button.textContent = "Copy";
      button.addEventListener("click", function () {
        navigator.clipboard.writeText(pre.innerText).then(function () {
          button.textContent = "Copied";
          setTimeout(function () { button.textContent = "Copy"; }, 1500);
        }, function () {});
      });
      block.appendChild(button);
    });
  }

  // -- cited lines (#L4 or #L4-L5) -----------------------------------------------------------
  function cited() {
    note.querySelectorAll(".cited").forEach(function (el) { el.classList.remove("cited"); });
    var m = /^#L(\d+)(?:-L(\d+))?$/.exec(location.hash);
    if (!m) return;
    var from = +m[1], to = m[2] ? +m[2] : from;
    var hits = Array.prototype.filter.call(note.querySelectorAll("[data-line]"), function (el) {
      var line = +el.getAttribute("data-line");
      return line >= from && line <= to;
    });
    // Mark the outermost blocks only: a list and its first item start on the same line.
    hits = hits.filter(function (el) {
      return !hits.some(function (other) { return other !== el && other.contains(el); });
    });
    hits.forEach(function (el) { el.classList.add("cited"); });
    if (hits.length) hits[0].scrollIntoView({ block: "center" });
  }
  cited();
  window.addEventListener("hashchange", cited);
})();
