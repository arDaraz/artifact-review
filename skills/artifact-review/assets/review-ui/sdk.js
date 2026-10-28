/*
 * The arev server injects this script into the reviewed artifact. The script
 * sends every request to the parent chrome through postMessage.
 */
(function () {
  "use strict";
  if (window.__arevSdkLoaded) return;
  window.__arevSdkLoaded = true;

  var annotating = false;
  var hoverEl = null;
  var swallowClick = false; // The click that completes a text selection is not an element pick.
  var inlineBoards = Object.create(null);
  var sharedInlineFrame = null;
  var activeInlineBoardId = null;
  var fullscreenBoardId = null;
  var fullscreenOverflow = null;

  function send(msg) {
    msg.arev = true;
    window.parent.postMessage(msg, window.location.origin);
  }


  function cssEscape(s) {
    return window.CSS && CSS.escape
      ? CSS.escape(s)
      : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function selectorFor(el) {
    if (!el || el === document.documentElement) return "html";
    if (el.id) return "#" + cssEscape(el.id);
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && parts.length < 3) {
      if (node.id) {
        parts.unshift("#" + cssEscape(node.id));
        break;
      }
      var tag = node.tagName.toLowerCase();
      var parent = node.parentElement;
      if (parent) {
        var same = Array.prototype.filter.call(parent.children, function (c) {
          return c.tagName === node.tagName;
        });
        if (same.length > 1)
          tag += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
      }
      parts.unshift(tag);
      node = parent;
    }
    return parts.join(" > ");
  }

  function labelFor(el) {
    var text = (
      el.innerText ||
      el.value ||
      el.getAttribute("aria-label") ||
      ""
    ).trim();
    text = text.replace(/\s+/g, " ").slice(0, 80);
    return "<" + el.tagName.toLowerCase() + "> " + text;
  }

  function safeDiagramId(authoredId, index) {
    if (/^[A-Za-z0-9_-]{1,128}$/.test(authoredId || ""))
      return authoredId;
    if (!authoredId) return "arev-mermaid-" + index;
    var hash = 2166136261;
    for (var i = 0; i < authoredId.length; i += 1) {
      hash ^= authoredId.charCodeAt(i);
      hash +=
        (hash << 1) +
        (hash << 4) +
        (hash << 7) +
        (hash << 8) +
        (hash << 24);
    }
    var slug = authoredId
      .replace(/[^A-Za-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 96);
    return (
      (slug || "mermaid") +
      "-" +
      ("00000000" + (hash >>> 0).toString(16)).slice(-8)
    );
  }

  function normalizedText(el) {
    if (!el) return "";
    var clone = el.cloneNode(true);
    Array.prototype.forEach.call(clone.querySelectorAll("br"), function (br) {
      br.parentNode.replaceChild(document.createTextNode(" "), br);
    });
    return (clone.innerText || clone.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function mermaidSvgFor(el) {
    if (!el || !el.closest) return null;
    var svg = el.closest("svg");
    if (!svg) return null;
    // The review tool puts its own SVG icons inside diagram containers. This
    // check keeps the tool from enhancing those icons as diagrams.
    if (svg.closest("[data-arev-internal]")) return null;
    var role = (svg.getAttribute("aria-roledescription") || "").toLowerCase();
    var id = (svg.id || "").toLowerCase();
    var inMermaid = svg.closest(
      ".mermaid,[data-arev-diagram-id],[id^='arev-board-']",
    );
    if (
      !inMermaid &&
      id.indexOf("mermaid") === -1 &&
      role.indexOf("diagram") === -1 &&
      !svg.querySelector("g.nodes")
    )
      return null;
    return svg;
  }

  function mermaidNodeGroup(el) {
    if (!el || el.nodeType !== 1 || !el.closest) return null;
    var group = el.closest("g.node");
    if (!group) {
      var candidate = el.closest("g");
      while (candidate) {
        if (
          candidate.parentElement &&
          candidate.parentElement.matches("g.nodes")
        ) {
          group = candidate;
          break;
        }
        candidate = candidate.parentElement
          ? candidate.parentElement.closest("g")
          : null;
      }
    }
    return group && mermaidSvgFor(group) ? group : null;
  }

  function diagramIdFor(group) {
    var svg = mermaidSvgFor(group);
    if (!svg) return "";
    var holder = svg.closest(
      "[data-arev-diagram-id],.mermaid,[id^='arev-board-']",
    );
    if (holder) {
      var explicit = holder.getAttribute("data-arev-diagram-id");
      if (explicit) return explicit;
      if (holder.id.indexOf("arev-board-") === 0)
        return holder.id.slice("arev-board-".length);
      if (holder.id) return holder.id;
    }
    return svg.id || "mermaid";
  }

  function selectorForMermaidNode(group) {
    var svg = mermaidSvgFor(group);
    var holder = svg
      ? svg.closest(
          "[data-arev-diagram-id],.mermaid,[id^='arev-board-']",
        )
      : null;
    var prefix =
      holder && holder.id
        ? "#" + cssEscape(holder.id) + " "
        : svg && svg.id
          ? "#" + cssEscape(svg.id) + " "
          : "";
    var key = group.getAttribute("data-arev-node-key");
    if (key)
      return prefix + '[data-arev-node-key="' + cssEscape(key) + '"]';
    if (group.id) return "#" + cssEscape(group.id);
    if (group.parentElement && group.parentElement.matches("g.nodes")) {
      var siblings = Array.prototype.filter.call(
        group.parentElement.children,
        function (child) {
          return child.tagName && child.tagName.toLowerCase() === "g";
        },
      );
      return (
        prefix +
        "g.nodes > g:nth-of-type(" +
        (siblings.indexOf(group) + 1) +
        ")"
      );
    }
    return prefix + selectorFor(group);
  }

  /* A re-render changes the counter in a node DOM id. The stable key drops
   * that counter, so annotations survive re-renders. */

  function stableNodeKey(group, svg, index) {
    var key = group.getAttribute("data-id") || "";
    if (!key) {
      var raw = group.id || "";
      // Mermaid 11 prefixes node ids with the svg's render id, which changes
      // on every render pass. Drop that prefix first.
      if (svg && svg.id && raw.indexOf(svg.id + "-") === 0)
        raw = raw.slice(svg.id.length + 1);
      var counted = raw.match(/^(.*[^-])-\d+$/);
      key = counted ? counted[1] : raw;
    }
    return key || "node-" + index;
  }

  function mermaidNodeGroups(svg) {
    // querySelectorAll never repeats an element, even across a selector list.
    return Array.prototype.slice.call(
      svg.querySelectorAll("g.node,g.nodes > g"),
    );
  }

  function allMermaidSvgs() {
    return Array.prototype.filter.call(
      document.querySelectorAll("svg"),
      mermaidSvgFor,
    );
  }

  function mermaidNodeTarget(el) {
    var group = mermaidNodeGroup(el);
    if (!group) return null;
    var labelEl = group.querySelector(
      ".nodeLabel,.label,foreignObject,text,[aria-label]",
    );
    var label =
      normalizedText(labelEl) ||
      group.getAttribute("aria-label") ||
      normalizedText(group);
    var svg = mermaidSvgFor(group);
    var nodeId =
      group.getAttribute("data-arev-node-key") ||
      stableNodeKey(
        group,
        svg,
        svg ? Math.max(0, mermaidNodeGroups(svg).indexOf(group)) : 0,
      );
    return {
      type: "mermaid-node",
      diagramId: diagramIdFor(group),
      nodeId: nodeId || "node",
      label: label.slice(0, 160),
      selector: selectorForMermaidNode(group),
    };
  }

  /* The surrounding context lets the agent find the selected text again
   * after the element re-renders. */
  function textAnchor(sel) {
    var range = sel.getRangeAt(0);
    var exact = sel.toString();
    var container = range.commonAncestorContainer;
    if (container.nodeType !== 1) container = container.parentElement;
    var full = container.innerText || "";
    var idx = full.indexOf(exact);
    return {
      exact: exact.slice(0, 500),
      prefix: idx > 0 ? full.slice(Math.max(0, idx - 40), idx) : "",
      suffix:
        idx >= 0 ? full.slice(idx + exact.length, idx + exact.length + 40) : "",
      selector: selectorFor(container),
    };
  }


  var css = document.createElement("style");
  css.textContent =
    ".arev-hover{outline:2px solid #5b8def!important;outline-offset:2px;cursor:crosshair!important}" +
    ".arev-flash{outline:3px solid #e8a13c!important;outline-offset:2px;transition:outline .2s}" +
    // The max-width:none rule overrides any artifact CSS that caps column
    // width, so the board always matches the diagram width.
    ".arev-inline-board{position:relative;width:100%;max-width:none;margin:0;padding:0 0 8px;display:flex;align-items:center;justify-content:flex-end;background:transparent;border:0;border-radius:8px;box-sizing:border-box}" +
    ".arev-inline-board.arev-inline-active{display:block;padding:0;max-height:calc(100vh - 24px);margin:0;background:#fff;border:1px solid rgba(128,128,128,.4);overflow:hidden}" +
    ".arev-inline-board>iframe{position:absolute;inset:0;display:block;width:100%;height:100%;border:0;background:#fff}" +
    // On hover, the outline grows thicker and changes color, so the change
    // does not rely on color alone.
    ".arev-inline-unlock{box-sizing:border-box;width:32px;height:32px;flex:none;display:inline-flex;align-items:center;justify-content:center;padding:0;border:1.5px solid #4d7ce0;border-radius:6px;background:rgba(77,124,224,.12);color:#4d7ce0;cursor:pointer;transition:border-color 120ms ease-out,border-width 120ms ease-out,background-color 120ms ease-out,color 120ms ease-out}" +
    ".arev-inline-unlock:hover:not(:disabled){border-width:2.5px;border-color:#1f9d6b;background:rgba(31,157,107,.18);color:#1f9d6b}" +
    ".arev-inline-unlock:focus-visible{outline:2px solid #1f9d6b;outline-offset:2px}" +
    ".arev-inline-unlock svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}" +
    ".arev-inline-unlock span{display:none}" +
    ".arev-inline-unlock:disabled{cursor:wait;opacity:.6}" +
    // Any display value overrides the hidden attribute. Without this rule, the
    // hidden overlay keeps catching clicks meant for the canvas.
    ".arev-inline-unlock[hidden]{display:none}" +
    // After the editor mounts, the same button becomes the overlay that
    // covers the frame until the reviewer unlocks the editor.
    ".arev-inline-active .arev-inline-unlock{position:absolute;inset:0;width:100%;height:100%;z-index:2;border:0;border-radius:0;background:rgba(128,128,128,.08);color:inherit;font:600 13px/1.3 sans-serif;transition:none}" +
    ".arev-inline-active .arev-inline-unlock svg{display:none}" +
    ".arev-inline-active .arev-inline-unlock span{display:inline-block;padding:8px 13px;border:1px solid rgba(128,128,128,.5);border-radius:999px;background:rgba(128,128,128,.14)}" +
    // Dark pages need lighter button colors to stay legible.
    "@media (prefers-color-scheme:dark){" +
    ".arev-inline-unlock{border-color:#87a3f4;background:rgba(135,163,244,.12);color:#87a3f4}" +
    ".arev-inline-unlock:hover:not(:disabled){border-color:#5dd3a0;background:rgba(93,211,160,.18);color:#5dd3a0}" +
    ".arev-inline-unlock:focus-visible{outline-color:#5dd3a0}}" +
    // A finger has no hover, so the square grows to the 44px touch minimum.
    "@media (pointer:coarse){.arev-inline-board:not(.arev-inline-active) .arev-inline-unlock{width:44px;height:44px}}" +
    ".arev-inline-board.arev-inline-fullscreen{position:fixed!important;inset:12px!important;width:auto!important;height:auto!important;max-height:none!important;z-index:2147483645!important;border-radius:10px!important;box-shadow:0 12px 48px rgba(0,0,0,.35)}" +
    /* The artifact stylesheet owns the page, so edit-mode styles that must
       stay visible use !important. */
    ".arev-editing{--arev-surface:#fff;--arev-ink:#171a20;--arev-muted:#61687a;--arev-line:#dcdde3;--arev-accent:#3557c0;--arev-accent-soft:#e8edfb;--arev-ok:#186a4b;--arev-ok-soft:#e7f4ee;--arev-cut:#a63d40;--arev-cut-soft:#f8e9ea;--arev-shadow:0 4px 8px rgba(20,24,32,.14)}" +
    "@media (prefers-color-scheme:dark){.arev-editing{--arev-surface:#181c23;--arev-ink:#e8ebf1;--arev-muted:#a0a7b6;--arev-line:#303744;--arev-accent:#87a3f4;--arev-accent-soft:#242d45;--arev-ok:#5dd3a0;--arev-ok-soft:#17362a;--arev-cut:#e38d91;--arev-cut-soft:#3d2629;--arev-shadow:0 4px 8px rgba(0,0,0,.38)}}" +
    ".arev-editing body{cursor:text}" +
    ".arev-editing :is(p,li,h1,h2,h3,h4,h5,h6,blockquote,dd,dt,figcaption,td,th,caption,summary):hover{background:var(--arev-accent-soft);border-radius:5px}" +
    ".arev-mark{border-radius:3px;padding:0 1px}" +
    ".arev-mark-del{background:var(--arev-cut-soft)!important;color:var(--arev-cut)!important;text-decoration:line-through!important}" +
    ".arev-mark-edited{background:var(--arev-ok-soft)!important;color:var(--arev-ok)!important;text-decoration:underline!important;text-underline-offset:3px;text-decoration-thickness:2px}" +
    // A rewritten block shows an "Edited" tag instead of an underline. An
    // underline on whole paragraphs is too distracting.
    ".arev-mark-edited.arev-mark-block{position:relative;display:block;padding:6px 10px;margin:0 -10px;border-radius:5px;text-decoration:none!important}" +
    '.arev-mark-edited.arev-mark-block::after{content:"Edited";position:absolute;top:-9px;right:0;border-radius:999px;padding:1px 7px;background:var(--arev-ok);color:var(--arev-surface);font:650 10px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-decoration:none}' +
    ".arev-mark-edited.arev-mark-block>:first-child{margin-top:0}" +
    ".arev-mark-edited.arev-mark-block>:last-child{margin-bottom:0}" +
    ".arev-editing .arev-mark:hover{outline:2px solid var(--arev-accent);outline-offset:1px;cursor:pointer}" +
    "[data-arev-cut]{position:relative;background:var(--arev-cut-soft)!important;border-radius:5px;color:var(--arev-cut)!important;text-decoration:line-through!important}" +
    '[data-arev-cut]::after{content:"Marked for deletion";position:absolute;top:-9px;right:0;border-radius:999px;padding:1px 7px;background:var(--arev-cut);color:#fff;font:650 10px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-decoration:none}' +
    ".arev-gutter-group{position:absolute;z-index:2147483643;display:flex;gap:4px;margin:0;padding:0}" +
    // Any display value overrides the hidden attribute, so the floating groups
    // restate display:none for [hidden].
    ".arev-gutter-group[hidden],.arev-bar[hidden]{display:none}" +
    ".arev-gutter{box-sizing:border-box;width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--arev-line);border-radius:7px;padding:0;background:var(--arev-surface);color:var(--arev-muted);cursor:pointer;box-shadow:var(--arev-shadow)}" +
    ".arev-gutter:hover:not(:disabled){border-color:var(--arev-accent);background:var(--arev-accent-soft);color:var(--arev-accent)}" +
    ".arev-gutter-cut:hover:not(:disabled){border-color:var(--arev-cut);background:var(--arev-cut-soft);color:var(--arev-cut)}" +
    ".arev-gutter:disabled{cursor:not-allowed;opacity:.52}" +
    ".arev-gutter svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}" +
    ".arev-bar{position:absolute;z-index:2147483644;display:flex;align-items:center;gap:4px;margin:0;border:1px solid var(--arev-line);border-radius:10px;padding:4px;background:var(--arev-surface);color:var(--arev-ink);box-shadow:var(--arev-shadow);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}" +
    ".arev-bar-sep{width:1px;height:20px;margin:0 2px;background:var(--arev-line)}" +
    ".arev-bar-hint{padding:0 6px;color:var(--arev-muted);font-size:11px;white-space:nowrap}" +
    ".arev-btn{box-sizing:border-box;min-height:30px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--arev-line);border-radius:8px;padding:6px 9px;background:var(--arev-surface);color:var(--arev-ink);cursor:pointer;font:600 11px/1 inherit}" +
    ".arev-btn:hover{border-color:var(--arev-accent);background:var(--arev-accent-soft)}" +
    ".arev-btn-primary,.arev-btn-primary:hover{border-color:var(--arev-accent);background:var(--arev-accent);color:#fff}" +
    ".arev-btn:focus-visible,.arev-gutter:focus-visible{outline:2px solid var(--arev-accent);outline-offset:2px}" +
    // The selected text becomes the editor, so the reviewer types over the
    // original words in place.
    ".arev-live-edit{border-radius:4px;padding:0 2px;background:var(--arev-accent-soft)!important;box-shadow:inset 0 0 0 2px var(--arev-accent);caret-color:var(--arev-accent)}" +
    ".arev-live-edit:focus{outline:none}" +
    ".arev-live-block{display:block;padding:6px 10px;margin:0 -10px}" +
    ".arev-live-block>:first-child{margin-top:0}" +
    ".arev-live-block>:last-child{margin-bottom:0}" +
    "@media (pointer:coarse){.arev-gutter{width:44px;height:44px}.arev-btn{min-height:44px}}";
  document.documentElement.appendChild(css);

  function setAnnotate(on) {
    annotating = on;
    if (!on && hoverEl) {
      hoverEl.classList.remove("arev-hover");
      hoverEl = null;
    }
    exploreViewports.forEach(function (viewport) {
      viewport.setFrozen(on);
    });
  }

  document.addEventListener(
    "mouseover",
    function (e) {
      if (!annotating || editing) return;
      if (e.target.closest && e.target.closest("[data-arev-internal]")) {
        if (hoverEl) hoverEl.classList.remove("arev-hover");
        hoverEl = null;
        return;
      }
      if (hoverEl) hoverEl.classList.remove("arev-hover");
      hoverEl = mermaidNodeGroup(e.target) || e.target;
      hoverEl.classList.add("arev-hover");
    },
    true,
  );

  document.addEventListener(
    "mouseup",
    function () {
      if (!annotating || editing) return;
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim()) {
        var anchor = textAnchor(sel);
        var rect = sel.getRangeAt(0).getBoundingClientRect();
        send({
          type: "pick-text",
          anchor: anchor,
          selector: anchor.selector,
          snippet: anchor.exact.slice(0, 120),
          rect: { top: rect.top, left: rect.left, bottom: rect.bottom },
        });
        sel.removeAllRanges();
        swallowClick = true; // The click event for this mouseup has not fired yet.
      }
    },
    true,
  );

  document.addEventListener(
    "click",
    function (e) {
      if (!annotating || editing) return;
      if (e.target.closest && e.target.closest("[data-arev-internal]"))
        return;
      e.preventDefault();
      e.stopPropagation();
      if (swallowClick) {
        swallowClick = false;
        return;
      }
      var target = mermaidNodeTarget(e.target);
      var el = target ? mermaidNodeGroup(e.target) : e.target;
      el.classList.remove("arev-hover");
      var rect = el.getBoundingClientRect();
      var pick = {
        type: "pick-element",
        selector: target ? target.selector : selectorFor(el),
        label: target ? target.label : labelFor(el),
        rect: { top: rect.top, left: rect.left, bottom: rect.bottom },
      };
      if (target) pick.target = target;
      send(pick);
    },
    true,
  );

  document.addEventListener(
    "keydown",
    function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "i") {
        e.preventDefault();
        send({ type: "toggle-annotate" });
      }
    },
    true,
  );

  /* Edit text mode never writes to the file. The chrome keeps each change as
   * a draft for save or send. */

  var BLOCK_SELECTOR =
    "p,li,h1,h2,h3,h4,h5,h6,blockquote,dd,dt,figcaption,td,th,caption,summary";
  var editing = false;
  var editSeq = 0;
  var textEdits = Object.create(null);
  var hoverBlock = null;
  var pendingRange = null;
  var liveEdit = null;
  var activeMark = null;
  var editUi = null;

  function editBlockFor(node) {
    var el = node && node.nodeType === 1 ? node : node && node.parentElement;
    if (!el || !el.closest) return null;
    if (el.closest("[data-arev-internal]")) return null;
    var block = el.closest(BLOCK_SELECTOR);
    if (!block || block.closest("svg") || block.closest("[data-arev-internal]"))
      return null;
    return block.textContent.trim() ? block : null;
  }

  function editBlocksIn(node) {
    if (node.nodeType !== 1) return [];
    var found = Array.prototype.filter.call(
      node.querySelectorAll(BLOCK_SELECTOR),
      function (block) {
        return !block.querySelector(BLOCK_SELECTOR);
      },
    );
    if (found.length) return found;
    var own = editBlockFor(node);
    return own ? [own] : [];
  }

  /* Each cut leaves a CUT_SEAM marker, so the result closes the gap instead
   * of keeping the two spaces around the cut. */
  var CUT_SEAM = "\u0000";

  function resolvedText(block) {
    if (!block) return "";
    if (block.hasAttribute("data-arev-cut")) return "";
    var clone = block.cloneNode(true);
    Array.prototype.forEach.call(
      clone.querySelectorAll(".arev-mark-del,[data-arev-internal]"),
      function (el) {
        el.parentNode.replaceChild(document.createTextNode(CUT_SEAM), el);
      },
    );
    return clone.textContent
      .replace(new RegExp(" " + CUT_SEAM + " ", "g"), " ")
      .replace(new RegExp(CUT_SEAM, "g"), "");
  }

  function editLabel(block) {
    var tag = block.tagName.toLowerCase();
    var kind =
      tag === "li"
        ? "list item"
        : tag.charAt(0) === "h" && tag.length === 2
          ? "heading"
          : tag === "td" || tag === "th"
            ? "table cell"
            : "paragraph";
    var text = normalizedText(block);
    return (
      kind + ' "' + (text.length > 34 ? text.slice(0, 34) + "…" : text) + '"'
    );
  }

  function shortText(value) {
    var text = value.replace(/\s+/g, " ").trim();
    return text.length > 60 ? text.slice(0, 60) + "…" : text;
  }

  /* A drawn SVG icon keeps one stroke weight at any size and needs no font
   * that the artifact might not ship. */
  function drawnIcon(paths) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    paths.forEach(function (d) {
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    });
    return svg;
  }

  function editButton(className, label, contents) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.setAttribute("data-arev-internal", "");
    if (contents) button.appendChild(contents);
    else button.textContent = label;
    button.setAttribute("aria-label", label);
    button.title = label;
    return button;
  }

  function buildEditUi() {
    if (editUi) return editUi;
    var group = document.createElement("div");
    group.className = "arev-gutter-group";
    group.setAttribute("data-arev-internal", "");
    group.hidden = true;
    var pencil = editButton(
      "arev-gutter",
      "Edit this line",
      drawnIcon(["M4 20h4L19 9l-4-4L4 16z", "M14.5 5.5l4 4"]),
    );
    var bin = editButton(
      "arev-gutter arev-gutter-cut",
      "Mark this line for deletion",
      drawnIcon(["M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"]),
    );
    group.append(pencil, bin);

    var selBar = document.createElement("div");
    selBar.className = "arev-bar";
    selBar.setAttribute("data-arev-internal", "");
    selBar.setAttribute("role", "toolbar");
    selBar.setAttribute("aria-label", "Change the selected text");
    selBar.hidden = true;
    var selEdit = editButton("arev-btn", "Edit text");
    var selDelete = editButton("arev-btn", "Delete");
    var separator = document.createElement("span");
    separator.className = "arev-bar-sep";
    separator.setAttribute("aria-hidden", "true");
    var selComment = editButton("arev-btn", "Comment");
    selBar.append(selEdit, selDelete, separator, selComment);

    var editBar = document.createElement("div");
    editBar.className = "arev-bar";
    editBar.setAttribute("data-arev-internal", "");
    editBar.setAttribute("role", "toolbar");
    editBar.setAttribute("aria-label", "Finish this edit");
    editBar.hidden = true;
    var hint = document.createElement("span");
    hint.className = "arev-bar-hint";
    hint.textContent = "Editing in place. ⌘Enter saves, Escape cancels.";
    var cancel = editButton("arev-btn", "Cancel");
    var save = editButton("arev-btn arev-btn-primary", "Save edit");
    editBar.append(hint, cancel, save);

    var undoBar = document.createElement("div");
    undoBar.className = "arev-bar";
    undoBar.setAttribute("data-arev-internal", "");
    undoBar.setAttribute("role", "toolbar");
    undoBar.setAttribute("aria-label", "Change on this text");
    undoBar.hidden = true;
    var undo = editButton("arev-btn", "Undo this edit");
    undoBar.appendChild(undo);

    document.body.append(group, selBar, editBar, undoBar);
    editUi = {
      group: group,
      pencil: pencil,
      bin: bin,
      selBar: selBar,
      editBar: editBar,
      undoBar: undoBar,
    };

    pencil.addEventListener("click", function () {
      if (hoverBlock) openBlockEditor(hoverBlock);
    });
    bin.addEventListener("click", function () {
      cutLine(hoverBlock);
    });
    selEdit.addEventListener("click", openRangeEditor);
    selDelete.addEventListener("click", cutRange);
    selComment.addEventListener("click", commentOnRange);
    cancel.addEventListener("click", cancelEdit);
    save.addEventListener("click", commitEdit);
    undo.addEventListener("click", function () {
      if (activeMark) dropEdit(activeMark.getAttribute("data-arev-edit"));
      activeMark = null;
      hideEditBars();
    });
    return editUi;
  }

  function placeBar(bar, rect, below) {
    bar.hidden = false;
    var top = below
      ? rect.bottom + window.scrollY + 8
      : rect.top + window.scrollY - bar.offsetHeight - 8;
    var maxLeft = Math.max(
      8,
      document.documentElement.clientWidth - bar.offsetWidth - 12,
    );
    bar.style.top = Math.max(4, top) + "px";
    bar.style.left =
      Math.max(8, Math.min(rect.left + window.scrollX, maxLeft)) + "px";
  }

  function hideEditBars() {
    if (!editUi) return;
    editUi.selBar.hidden = true;
    editUi.undoBar.hidden = true;
  }

  function setEditText(on) {
    editing = !!on;
    document.documentElement.classList.toggle("arev-editing", editing);
    if (!editing) {
      cancelEdit();
      hideEditBars();
      if (editUi) editUi.group.hidden = true;
      hoverBlock = null;
      pendingRange = null;
    } else {
      buildEditUi();
    }
  }


  function recordEdit(record) {
    textEdits[record.id] = record;
    send({ type: "text-edit", item: editItem(record) });
  }

  function editItem(record) {
    return {
      kind: "text-edit",
      editId: record.id,
      action: record.action,
      scope: record.scope,
      selector: record.selector,
      label: record.label,
      before: record.before,
      after: record.after,
      blocks: record.blocks,
    };
  }

  function dropEdit(id) {
    var record = textEdits[String(id)];
    if (!record) return;
    delete textEdits[record.id];
    record.restore();
    send({ type: "text-edit-dropped", editId: record.id, qid: record.qid });
  }

  function revertEdit(id) {
    var record = textEdits[String(id)];
    if (!record) return;
    delete textEdits[record.id];
    record.restore();
  }

  function cutLine(block) {
    if (!block) return;
    var existing = block.getAttribute("data-arev-cut");
    if (existing) {
      dropEdit(existing);
      return;
    }
    var id = "te" + ++editSeq;
    var before = resolvedText(block);
    block.setAttribute("data-arev-cut", id);
    recordEdit({
      id: id,
      action: "cut",
      scope: "line",
      selector: selectorFor(block),
      label: editLabel(block),
      before: shortText(before),
      after: "",
      blocks: [{ selector: selectorFor(block), before: before, after: "" }],
      restore: function () {
        block.removeAttribute("data-arev-cut");
      },
    });
    if (editUi) editUi.pencil.disabled = true;
  }

  function cutRange() {
    if (!pendingRange) return;
    var block = editBlockFor(pendingRange.startContainer);
    if (!block) return;
    var id = "te" + ++editSeq;
    var original = pendingRange.toString();
    var before = resolvedText(block);
    var span = document.createElement("span");
    span.className = "arev-mark arev-mark-del";
    span.setAttribute("data-arev-edit", id);
    span.appendChild(pendingRange.extractContents());
    pendingRange.insertNode(span);
    recordEdit({
      id: id,
      action: "cut",
      scope: "range",
      selector: selectorFor(block),
      label: editLabel(block),
      before: shortText(original),
      after: "",
      blocks: [
        { selector: selectorFor(block), before: before, after: resolvedText(block) },
      ],
      restore: function () {
        var parent = span.parentNode;
        if (!parent) return;
        while (span.firstChild) parent.insertBefore(span.firstChild, span);
        parent.removeChild(span);
        parent.normalize();
      },
    });
    finishSelection();
  }

  function commentOnRange() {
    if (!pendingRange) return;
    var selection = window.getSelection();
    var anchor = textAnchor(selection);
    var rect = pendingRange.getBoundingClientRect();
    send({
      type: "pick-text",
      anchor: anchor,
      selector: anchor.selector,
      snippet: anchor.exact.slice(0, 120),
      rect: { top: rect.top, left: rect.left, bottom: rect.bottom },
    });
    finishSelection();
  }

  function finishSelection() {
    hideEditBars();
    pendingRange = null;
    var selection = window.getSelection();
    if (selection) selection.removeAllRanges();
  }


  function fragmentOf(host) {
    var fragment = document.createDocumentFragment();
    Array.prototype.forEach.call(host.childNodes, function (node) {
      fragment.appendChild(node.cloneNode(true));
    });
    return fragment;
  }

  /* A selection across two blocks grows to whole blocks, so the reviewer
   * edits complete lines, not two half sentences. */
  function wholeBlocksIfMultiple(range) {
    var first = editBlockFor(range.startContainer);
    var last = editBlockFor(range.endContainer);
    if (!first || !last || first === last) return range;
    var wide = document.createRange();
    wide.setStartBefore(first);
    wide.setEndAfter(last);
    return wide;
  }

  function openRangeEditor() {
    if (!pendingRange) return;
    var block = editBlockFor(pendingRange.startContainer);
    if (!block) return;
    var range = wholeBlocksIfMultiple(pendingRange);
    var spansBlocks = range !== pendingRange;
    var host = document.createElement(spansBlocks ? "div" : "span");
    host.className = "arev-live-edit" + (spansBlocks ? " arev-live-block" : "");
    host.appendChild(range.extractContents());
    range.insertNode(host);
    var fragment = fragmentOf(host);
    startEditing({
      host: host,
      block: spansBlocks,
      label: editLabel(block),
      scope: spansBlocks ? "lines" : "range",
      // Words picked inside one line belong to that line. Only a multi-block
      // range uses the blocks inside the editor.
      blocks: spansBlocks ? editBlocksIn(host) : [block],
      afterBlocks: spansBlocks
        ? function () {
            return editBlocksIn(host);
          }
        : function () {
            return [block];
          },
      restore: function () {
        if (host.parentNode) host.parentNode.replaceChild(fragment, host);
      },
    });
  }

  /* The block itself becomes the editor, so the paragraph keeps its own tag
   * and style during the edit. */
  function openBlockEditor(block) {
    if (!block || block.hasAttribute("data-arev-cut")) return;
    var done = block.getAttribute("data-arev-edit");
    if (done) dropEdit(done);
    var fragment = fragmentOf(block);
    block.classList.add("arev-live-edit", "arev-live-block");
    startEditing({
      host: block,
      block: true,
      label: editLabel(block),
      scope: "line",
      blocks: [block],
      afterBlocks: function () {
        return [block];
      },
      restore: function () {
        block.classList.remove("arev-mark", "arev-mark-edited", "arev-mark-block");
        block.removeAttribute("data-arev-edit");
        block.textContent = "";
        block.appendChild(fragment);
      },
    });
  }

  function startEditing(plan) {
    buildEditUi();
    // This code hides the other floating controls while an editor is open,
    // because the line handles would act on the wrong line.
    editUi.group.hidden = true;
    editUi.selBar.hidden = true;
    editUi.undoBar.hidden = true;
    plan.original = plan.host.textContent;
    plan.before = plan.blocks.map(resolvedText);
    plan.selector = selectorFor(plan.blocks[0] || plan.host);
    plan.host.contentEditable = "true";
    plan.host.spellcheck = false;
    liveEdit = plan;
    placeBar(editUi.editBar, plan.host.getBoundingClientRect(), true);
    plan.host.focus();
    // The caret goes after the text. A full selection would let the first
    // keystroke delete the words the reviewer wants to keep.
    var caret = document.createRange();
    caret.selectNodeContents(plan.host);
    caret.collapse(false);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(caret);
  }

  function closeEditor(plan) {
    liveEdit = null;
    if (editUi) editUi.editBar.hidden = true;
    plan.host.contentEditable = "false";
    plan.host.removeAttribute("contenteditable");
    plan.host.classList.remove("arev-live-edit", "arev-live-block");
    var selection = window.getSelection();
    if (selection) selection.removeAllRanges();
  }

  function commitEdit() {
    if (!liveEdit) return;
    var plan = liveEdit;
    var changed = plan.host.textContent;
    closeEditor(plan);
    if (!changed.trim() || changed === plan.original) {
      plan.restore();
      return;
    }
    var id = "te" + ++editSeq;
    plan.host.classList.add("arev-mark", "arev-mark-edited");
    if (plan.block) plan.host.classList.add("arev-mark-block");
    plan.host.removeAttribute("data-arev-internal");
    plan.host.setAttribute("data-arev-edit", id);
    var after = plan.afterBlocks();
    recordEdit({
      id: id,
      action: "edit",
      scope: plan.scope,
      selector: plan.selector,
      label: plan.label,
      before: shortText(plan.original),
      after: shortText(changed),
      blocks: plan.blocks.map(function (block, index) {
        return {
          selector: selectorFor(block),
          before: plan.before[index],
          after: after[index] ? resolvedText(after[index]) : "",
        };
      }),
      restore: plan.restore,
    });
  }

  function cancelEdit() {
    if (!liveEdit) return;
    var plan = liveEdit;
    closeEditor(plan);
    plan.restore();
  }


  document.addEventListener(
    "mouseover",
    function (event) {
      if (!editing || liveEdit) return;
      if (event.target.closest && event.target.closest("[data-arev-internal]"))
        return;
      var block = editBlockFor(event.target);
      if (!block) return;
      hoverBlock = block;
      var ui = buildEditUi();
      var rect = block.getBoundingClientRect();
      ui.group.hidden = false;
      // Without a left margin, the handles go above the line, because handles
      // that cover the first words are worse than no handles.
      var beside = rect.left + window.scrollX - ui.group.offsetWidth - 8;
      var above = beside < 4;
      ui.group.style.top =
        rect.top +
        window.scrollY -
        (above ? ui.group.offsetHeight + 4 : 0) +
        "px";
      ui.group.style.left =
        (above ? rect.left + window.scrollX : beside) + "px";
      var cut = block.hasAttribute("data-arev-cut");
      ui.bin.setAttribute(
        "aria-label",
        cut ? "Restore this line" : "Mark this line for deletion",
      );
      ui.bin.title = cut ? "Restore this line" : "Mark this line for deletion";
      ui.pencil.disabled = cut;
    },
    true,
  );

  document.addEventListener("mouseleave", function () {
    if (editUi && !liveEdit) editUi.group.hidden = true;
  });

  document.addEventListener(
    "mouseup",
    function (event) {
      if (!editing || liveEdit) return;
      if (event.target.closest && event.target.closest("[data-arev-internal]"))
        return;
      var selection = window.getSelection();
      if (!selection || selection.isCollapsed || !selection.toString().trim())
        return;
      if (!editBlockFor(selection.anchorNode)) return;
      var ui = buildEditUi();
      hideEditBars();
      pendingRange = selection.getRangeAt(0).cloneRange();
      placeBar(ui.selBar, pendingRange.getBoundingClientRect());
    },
    true,
  );

  document.addEventListener(
    "mousedown",
    function (event) {
      if (!editing || liveEdit) return;
      if (event.target.closest && event.target.closest("[data-arev-internal]"))
        return;
      var mark = event.target.closest
        ? event.target.closest("[data-arev-edit]")
        : null;
      hideEditBars();
      if (!mark) return;
      activeMark = mark;
      placeBar(buildEditUi().undoBar, mark.getBoundingClientRect());
    },
    true,
  );

  /* A click on the highlighted line opens the same editor as the pencil
   * button. */
  document.addEventListener(
    "click",
    function (event) {
      if (!editing) return;
      if (event.target.closest && event.target.closest("[data-arev-internal]"))
        return;
      event.preventDefault();
      event.stopPropagation();
      if (liveEdit) return;
      if (event.target.closest && event.target.closest("[data-arev-edit]"))
        return;
      var selection = window.getSelection();
      if (selection && !selection.isCollapsed && selection.toString().trim())
        return;
      var block = editBlockFor(event.target);
      if (block) openBlockEditor(block);
    },
    true,
  );

  document.addEventListener(
    "keydown",
    function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "e") {
        event.preventDefault();
        send({ type: "toggle-edit-text" });
        return;
      }
      if (!liveEdit) return;
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        commitEdit();
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancelEdit();
      }
    },
    true,
  );

  document.addEventListener("input", function () {
    if (liveEdit && editUi)
      placeBar(editUi.editBar, liveEdit.host.getBoundingClientRect(), true);
  });

  /* A change to an artifact control is feedback. A ticked checkbox or a
   * chosen radio button answers a question without any typing. */

  function controlItem(el, value) {
    var kind = el.matches("[data-arev-action],[data-lavish-action]")
      ? "action"
      : el.type || el.tagName.toLowerCase();
    var label =
      el.labels && el.labels[0]
        ? el.labels[0].innerText.trim()
        : el.getAttribute("data-arev-action") ||
          el.getAttribute("data-lavish-action") ||
          labelFor(el);
    return {
      kind: "control",
      selector: selectorFor(el),
      control: kind,
      value: value,
      label: label.slice(0, 120),
    };
  }

  document.addEventListener(
    "change",
    function (e) {
      if (annotating || editing) return;
      var el = e.target;
      if (el.closest("[data-arev-internal]")) return; // The arev UI is not feedback.
      if (!el.matches("input,select,textarea")) return;
      var value =
        el.type === "checkbox"
          ? String(el.checked)
          : el.type === "radio"
            ? el.value
            : el.value;
      // A radio group gives one answer, so the selector names the group, not
      // the element.
      var item = controlItem(el, value);
      if (el.type === "radio" && el.name)
        item.selector = "input[name=" + cssEscape(el.name) + "]";
      send({ type: "control", item: item });
    },
    true,
  );

  document.addEventListener(
    "click",
    function (e) {
      if (annotating || editing) return;
      if (e.target.closest("[data-arev-internal]")) return; // The arev UI is not feedback.
      var el = e.target.closest(
        "button,[data-arev-action],[data-lavish-action]",
      );
      if (!el) return;
      if (el.matches("[data-arev-action],[data-lavish-action],button")) {
        send({ type: "control", item: controlItem(el, "clicked") });
      }
    },
    true,
  );


  function boardForSource(source) {
    var board = inlineBoards[activeInlineBoardId];
    return board && sharedInlineFrame && sharedInlineFrame.contentWindow === source
      ? board
      : null;
  }

  function postToBoard(board, message) {
    if (!board || !board.iframe || !board.iframe.contentWindow) return;
    board.iframe.contentWindow.postMessage(message, "*");
  }

  function unlockBoard(board, focus) {
    if (!board || board.id !== activeInlineBoardId || !board.iframe) return;
    if (!board.ready) {
      board.wantsUnlock = true;
      return;
    }
    board.iframe.style.pointerEvents = "auto";
    board.unlocked = true;
    board.overlay.hidden = true;
    postToBoard(board, {
      arevFrame: true,
      channel: board.channel,
      type: "unlock",
    });
    if (focus) {
      try {
        board.iframe.focus();
      } catch (err) {}
    }
  }

  function setBoardFullscreen(board, on) {
    if (!board) return;
    if (on && fullscreenBoardId && fullscreenBoardId !== board.id)
      setBoardFullscreen(inlineBoards[fullscreenBoardId], false);
    if (on && !fullscreenBoardId) {
      fullscreenOverflow = document.documentElement.style.overflow;
      document.documentElement.style.overflow = "hidden";
    }
    board.fullscreen = !!on;
    board.host.classList.toggle("arev-inline-fullscreen", !!on);
    board.host.setAttribute(
      "aria-label",
      on
        ? "Diagram editor in fullscreen mode"
        : "Inline editable diagram",
    );
    board.overlay.setAttribute(
      "aria-label",
      on ? "Click to edit fullscreen diagram" : "Click to edit diagram",
    );
    if (on) {
      fullscreenBoardId = board.id;
      unlockBoard(board, true);
    } else if (fullscreenBoardId === board.id) {
      fullscreenBoardId = null;
      document.documentElement.style.overflow = fullscreenOverflow || "";
      fullscreenOverflow = null;
    }
    postToBoard(board, {
      arevFrame: true,
      channel: board.channel,
      type: "fullscreen-state",
      enabled: !!on,
    });
  }

  function restoreBoardSource(board) {
    if (!board || !board.block) return;
    board.block.style.display = board.originalDisplay;
  }

  /* The resting row sits inside the diagram block, so the row shares the
   * block background and border. */
  function placeBoardHost(board) {
    if (!board || !board.block) return;
    if (board.block.firstChild === board.host) return;
    board.block.insertBefore(board.host, board.block.firstChild);
  }

  function liftBoardHost(board) {
    if (!board || !board.block || !board.block.parentNode) return;
    if (board.host.nextElementSibling === board.block) return;
    board.block.parentNode.insertBefore(board.host, board.block);
  }

  function pencilIcon() {
    return drawnIcon(["M4 20h4L19 9l-4-4L4 16z", "M14 6l4 4"]);
  }

  function markBoardReady(board) {
    if (!board || !board.block) return;
    board.ready = true;
    board.host.hidden = false;
    board.block.style.display = "none";
    board.overlay.disabled = false;
    board.overlayLabel.textContent = "Click to edit diagram";
    board.overlay.setAttribute("aria-label", "Click to edit diagram");
    board.overlay.title = "Click to edit diagram";
    if (board.wantsUnlock) unlockBoard(board, true);
  }

  function createSharedInlineFrame() {
    if (sharedInlineFrame) return sharedInlineFrame;
    var iframe = document.createElement("iframe");
    iframe.id = "arev-shared-whiteboard-frame";
    iframe.setAttribute("sandbox", "allow-scripts allow-popups");
    iframe.style.pointerEvents = "none";
    iframe.addEventListener("error", function () {
      var board = inlineBoards[activeInlineBoardId];
      if (!board) return;
      restoreBoardSource(board);
      board.host.classList.remove("arev-inline-active");
      board.host.style.height = "";
      board.overlay.disabled = false;
      board.overlay.hidden = false;
      setOverlayIdle(board);
      placeBoardHost(board);
      send({
        type: "inline-mount-failed",
        id: board.id,
        error: "The inline diagram editor could not be loaded.",
      });
    });
    sharedInlineFrame = iframe;
    return iframe;
  }

  function deactivateInlineBoard(board) {
    if (!board) return;
    if (board.fullscreen) setBoardFullscreen(board, false);
    restoreBoardSource(board);
    board.ready = false;
    board.unlocked = false;
    board.wantsUnlock = false;
    board.iframe = null;
    board.host.classList.remove("arev-inline-active");
    board.host.style.height = "";
    board.host.setAttribute("aria-label", "Diagram editor available");
    board.overlay.disabled = false;
    board.overlay.hidden = false;
    setOverlayIdle(board);
    placeBoardHost(board);
    // The shared frame outlives every board, so a board at rest must not keep
    // the frame. Activation moves and reloads the frame.
    if (sharedInlineFrame && sharedInlineFrame.parentNode === board.host)
      sharedInlineFrame.remove();
  }

  function closeInlineBoard(board) {
    if (!board) return;
    deactivateInlineBoard(board);
    if (activeInlineBoardId === board.id) activeInlineBoardId = null;
  }

  /* The label text stays for the mounted overlay, which shows words instead
   * of the icon. */
  function setOverlayIdle(board) {
    board.overlayLabel.textContent = "Edit diagram";
    board.overlay.setAttribute("aria-label", "Edit diagram");
    board.overlay.title = "Edit diagram";
  }

  function activateInlineBoard(board, focus) {
    if (!board) return;
    if (activeInlineBoardId === board.id && board.iframe) {
      board.wantsUnlock = true;
      unlockBoard(board, focus);
      return;
    }

    // The block grows while Mermaid renders, so the code measures at
    // activation. The 216px holds the frame chrome and Excalidraw toolbar.
    var editorHeight = Math.max(
      300,
      Math.round(board.block.getBoundingClientRect().height) + 216,
    );

    var previous = inlineBoards[activeInlineBoardId];
    if (previous) deactivateInlineBoard(previous);

    // The board host takes the place of the block, and a hidden block cannot
    // hold the host.
    liftBoardHost(board);

    var iframe = createSharedInlineFrame();
    activeInlineBoardId = board.id;
    board.iframe = iframe;
    board.ready = false;
    board.unlocked = false;
    board.wantsUnlock = true;
    board.host.classList.add("arev-inline-active");
    board.host.style.height = editorHeight + "px";
    board.host.setAttribute("aria-label", "Inline editable diagram");
    board.overlay.hidden = false;
    board.overlay.disabled = true;
    board.overlayLabel.textContent = "Loading diagram editor…";
    board.overlay.setAttribute("aria-label", "Loading diagram editor");
    board.overlay.title = "Loading diagram editor";
    iframe.title = "Editable diagram " + board.id;
    iframe.style.pointerEvents = "none";
    board.host.insertBefore(iframe, board.overlay);
    iframe.src =
      "/whiteboard-frame?" +
      (board.frameVersion ? "v=" + board.frameVersion + "&" : "") +
      "diagram=" +
      encodeURIComponent(board.id) +
      "&channel=" +
      encodeURIComponent(board.channel);
    board.host.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function mountInline(message) {
    var id = String(message.id || "");
    var channel = String(message.channel || "");
    var selector = String(message.selector || "");
    var frameVersion = String(message.frameVersion || "");
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(id) || !channel || !selector)
      return;
    if (frameVersion && !/^[0-9a-f]{64}$/.test(frameVersion)) return;
    var block;
    try {
      block = document.querySelector(selector);
    } catch (err) {
      block = null;
    }
    if (!block) {
      send({
        type: "inline-mount-failed",
        id: id,
        error: "The Mermaid source element was not found.",
      });
      return;
    }

    var current = inlineBoards[id];
    if (current) {
      if (activeInlineBoardId === current.id) {
        deactivateInlineBoard(current);
        activeInlineBoardId = null;
      }
      restoreBoardSource(current);
      current.host.remove();
      delete inlineBoards[id];
    }

    var host = document.getElementById("arev-board-" + id);
    var createdHost = !host;
    var originalDisplay = block.hasAttribute("data-arev-original-display")
      ? block.getAttribute("data-arev-original-display")
      : block.style.display;
    try {
      if (!host) {
        host = document.createElement("section");
        host.id = "arev-board-" + id;
      } else {
        host.replaceChildren();
        host.style.cssText = "";
      }
      host.classList.add("arev-inline-board");
      host.setAttribute("data-arev-internal", "");
      host.setAttribute("data-arev-diagram-id", id);
      host.setAttribute("aria-label", "Diagram editor available");
      var overlay = document.createElement("button");
      overlay.type = "button";
      overlay.className = "arev-inline-unlock";
      overlay.setAttribute("data-arev-internal", "");
      overlay.appendChild(pencilIcon());
      var overlayLabel = document.createElement("span");
      overlay.appendChild(overlayLabel);

      var board = {
        id: id,
        channel: channel,
        selector: selector,
        block: block,
        originalDisplay: originalDisplay,
        host: host,
        iframe: null,
        overlay: overlay,
        overlayLabel: overlayLabel,
        frameVersion: frameVersion,
        unlocked: false,
        wantsUnlock: false,
        fullscreen: false,
        ready: false,
      };
      setOverlayIdle(board);
      overlay.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        send({ type: "want-board", id: board.id });
      });

      host.appendChild(overlay);
      placeBoardHost(board);
      inlineBoards[id] = board;
    } catch (err) {
      block.style.display = originalDisplay;
      delete inlineBoards[id];
      if (createdHost && host && host.parentNode)
        host.parentNode.removeChild(host);
      send({
        type: "inline-mount-failed",
        id: id,
        error: err && err.message ? err.message : "Could not mount the editor.",
      });
    }
  }

  // A theme change makes the offline renderer redraw the block and delete its
  // contents. This listener puts each resting row back.
  document.addEventListener("arev:mermaid-rendered", function () {
    Object.keys(inlineBoards).forEach(function (id) {
      if (id !== activeInlineBoardId) placeBoardHost(inlineBoards[id]);
    });
  });

  function focusInline(id) {
    var board = inlineBoards[String(id || "")];
    if (!board) return;
    activateInlineBoard(board, true);
  }

  window.addEventListener("message", function (event) {
    if (!event.data || !event.data.arevFrame) return;
    var board = boardForSource(event.source);
    if (!board || event.data.channel !== board.channel) return;
    if (event.data.type === "ready") markBoardReady(board);
    if (
      event.data.type === "fullscreen" ||
      event.data.type === "request-fullscreen" ||
      event.data.type === "set-fullscreen"
    ) {
      var requested =
        typeof event.data.enabled === "boolean"
          ? event.data.enabled
          : typeof event.data.fullscreen === "boolean"
            ? event.data.fullscreen
            : typeof event.data.on === "boolean"
              ? event.data.on
              : !board.fullscreen;
      setBoardFullscreen(board, requested);
      return;
    }
    if (event.data.type === "close") {
      closeInlineBoard(board);
      return;
    }
    send({
      type: "whiteboard-frame",
      id: board.id,
      channel: board.channel,
      message: event.data,
    });
  });

  document.addEventListener(
    "keydown",
    function (event) {
      // Escape reaches the artifact only when focus is outside the editor
      // frame. Both handlers exit fullscreen first and then close the editor.
      if (event.key !== "Escape") return;
      if (fullscreenBoardId)
        setBoardFullscreen(inlineBoards[fullscreenBoardId], false);
      else if (activeInlineBoardId)
        closeInlineBoard(inlineBoards[activeInlineBoardId]);
    },
    true,
  );


  window.addEventListener("message", function (e) {
    if (e.source !== window.parent || !e.data || !e.data.arev) return;
    if (e.data.type === "set-annotate") setAnnotate(!!e.data.on);
    if (e.data.type === "set-edit-text") setEditText(!!e.data.on);
    if (e.data.type === "text-edit-queued") {
      var queued = textEdits[String(e.data.editId || "")];
      if (queued) queued.qid = e.data.qid;
    }
    if (e.data.type === "revert-text-edit") revertEdit(e.data.editId);
    if (e.data.type === "clear-text-edits") {
      Object.keys(textEdits).forEach(function (id) {
        delete textEdits[id];
      });
    }
    if (e.data.type === "flash") {
      var el = document.querySelector(e.data.selector);
      if (el) {
        el = mermaidNodeGroup(el) || el;
        el.scrollIntoView({ block: "center" });
        el.classList.add("arev-flash");
        setTimeout(function () {
          el.classList.remove("arev-flash");
        }, 1600);
      }
    }
    if (e.data.type === "run-audit") {
      send({
        type: "audit-pass",
        token: e.data.token,
        findings: safeAudit(),
      });
    }
    if (e.data.type === "get-scroll")
      send({ type: "scroll", y: window.scrollY });
    if (e.data.type === "set-scroll") window.scrollTo(0, e.data.y || 0);
    if (e.data.type === "mount-inline") mountInline(e.data);
    if (e.data.type === "focus-inline") focusInline(e.data.id);
    if (e.data.type === "whiteboard-frame") {
      var board = inlineBoards[String(e.data.id || "")];
      if (
        board &&
        e.data.channel === board.channel &&
        e.data.message
      )
        postToBoard(board, e.data.message);
    }
    if (e.data.type === "inject-svg") {
      var block = document.querySelector(e.data.selector);
      if (block) {
        if (inlineBoards[String(e.data.id || "")]) return;
        var host = document.getElementById("arev-board-" + e.data.id);
        if (!host) {
          host = document.createElement("div");
          host.id = "arev-board-" + e.data.id;
          block.setAttribute("data-arev-original-display", block.style.display);
          block.style.display = "none";
          block.parentNode.insertBefore(host, block.nextSibling);
        }
        showBoardSvg(host, e.data.id, e.data.svg);
      }
    }
  });

  /* The Expand button opens the modal Excalidraw editor in the chrome, so
   * nothing heavy mounts inside the artifact. */

  function showBoardSvg(host, id, svg) {
    host.innerHTML = svg;
    host.style.cssText =
      "position:relative;background:#fff;border-radius:6px;" +
      "padding:8px;overflow:auto;border:1px solid #d8dbe0";
    var btn = document.createElement("button");
    btn.textContent = "⛶ Expand";
    btn.setAttribute("data-arev-internal", "");
    btn.style.cssText =
      "position:absolute;right:8px;top:8px;font:12px sans-serif;" +
      "padding:3px 10px;cursor:pointer;border:1px solid #c9cdd3;border-radius:6px;" +
      "background:#fff;color:#333";
    btn.onclick = function (e) {
      e.stopPropagation();
      send({ type: "want-board", id: id });
    };
    host.appendChild(btn);
  }

  /* Mermaid can put two opaque edge labels on one point, so one label hides
   * the other. spreadEdgeLabels pushes the labels apart. */

  function safeBBox(el) {
    try {
      return el.getBBox();
    } catch (err) {
      return null;
    }
  }

  // A diagram inside a display:none subtree measures as nothing at all.
  function laidOut(svg) {
    var box = safeBBox(svg);
    return !!(box && box.width && box.height);
  }

  function labelBox(el) {
    var consolidated = el.transform.baseVal.consolidate();
    var m = consolidated
      ? consolidated.matrix
      : { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
    // The code can rewrite only a pure translation as a new translation.
    if (m.a !== 1 || m.b !== 0 || m.c !== 0 || m.d !== 1) return null;
    var box = safeBBox(el);
    if (!box || box.width < 1 || box.height < 1) return null;
    return {
      el: el,
      // getBBox measures before the element's own translation, so the
      // translation to write back is the moved corner minus ox and oy.
      ox: box.x,
      oy: box.y,
      x: box.x + m.e,
      y: box.y + m.f,
      w: box.width,
      h: box.height,
    };
  }

  // Mermaid frames a drawing with an 8 unit margin. Each push stays inside
  // that frame, so the diagram keeps its visible size.
  function inFrame(value, size, low, span) {
    if (size >= span) return value;
    return Math.min(Math.max(value, low), low + span - size);
  }

  function spreadEdgeLabels(svg) {
    // Measuring labels forces a layout, so the code counts them first and
    // skips a diagram with fewer than two.
    var nodes = svg.querySelectorAll("g.edgeLabel");
    if (nodes.length < 2) return;
    var labels = [];
    Array.prototype.forEach.call(nodes, function (el) {
      var box = labelBox(el);
      if (box) labels.push(box);
    });
    if (labels.length < 2) return;

    var frame = svg.viewBox.baseVal;
    var moved = false;
    // One pass can settle only one overlapping pair, so the pass limit is the
    // label count squared.
    var limit = labels.length * labels.length;
    for (var pass = 0; pass < limit; pass++) {
      var shifted = false;
      for (var i = 0; i < labels.length; i++) {
        for (var j = i + 1; j < labels.length; j++) {
          var a = labels[i];
          var b = labels[j];
          var ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
          var oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
          if (ox <= 0 || oy <= 0) continue;
          // The labels move along the axis with the shorter move, so each
          // label stays close to its own edge.
          var na, nb;
          if (oy <= ox) {
            var dy = oy / 2 + 1;
            var up = a.y + a.h / 2 <= b.y + b.h / 2 ? -1 : 1;
            na = inFrame(a.y + dy * up, a.h, frame.y, frame.height);
            nb = inFrame(b.y - dy * up, b.h, frame.y, frame.height);
            if (na !== a.y || nb !== b.y) shifted = true;
            a.y = na;
            b.y = nb;
          } else {
            var dx = ox / 2 + 1;
            var left = a.x + a.w / 2 <= b.x + b.w / 2 ? -1 : 1;
            na = inFrame(a.x + dx * left, a.w, frame.x, frame.width);
            nb = inFrame(b.x - dx * left, b.w, frame.x, frame.width);
            if (na !== a.x || nb !== b.x) shifted = true;
            a.x = na;
            b.x = nb;
          }
        }
      }
      if (!shifted) break;
      moved = true;
    }
    if (!moved) return;

    labels.forEach(function (label) {
      label.el.setAttribute(
        "transform",
        "translate(" + (label.x - label.ox) + ", " + (label.y - label.oy) + ")",
      );
    });
  }

  // A diagram hidden at boot has no box to measure, so a ResizeObserver waits
  // until the browser lays out the diagram.
  function spreadWhenLaidOut(svg) {
    if (laidOut(svg) || !window.ResizeObserver) return spreadEdgeLabels(svg);
    var observer = new ResizeObserver(function () {
      if (!svg.isConnected) return observer.disconnect();
      if (!laidOut(svg)) return;
      observer.disconnect();
      spreadEdgeLabels(svg);
    });
    observer.observe(svg);
  }

  /* Annotate mode freezes diagram pan and zoom, so element picks stay
   * precise. */

  var exploreViewports = [];

  function createExploreViewport(svg) {
    var initial = null;
    var raw = svg.getAttribute("viewBox");
    if (raw) {
      var parts = raw.trim().split(/[\s,]+/).map(Number);
      if (parts.length === 4 && parts.every(isFinite))
        initial = { x: parts[0], y: parts[1], w: parts[2], h: parts[3] };
    }
    if (!initial) {
      var bbox = null;
      try {
        bbox = svg.getBBox();
      } catch (err) {}
      if (!bbox || !bbox.width || !bbox.height) return null;
      initial = { x: bbox.x, y: bbox.y, w: bbox.width, h: bbox.height };
      svg.setAttribute(
        "viewBox",
        initial.x + " " + initial.y + " " + initial.w + " " + initial.h,
      );
    }

    var view = { x: initial.x, y: initial.y, w: initial.w, h: initial.h };
    var panning = null;

    // Explore mode has no controls, so a native SVG tooltip shows the gesture
    // hint. An existing diagram title stays.
    var hint = null;
    if (!svg.querySelector(":scope > title")) {
      hint = document.createElementNS("http://www.w3.org/2000/svg", "title");
      hint.textContent =
        "Hold Ctrl or Cmd and scroll to zoom, drag to pan, " +
        "double-click to restore the original size.";
      // A screen reader reads a <title> child as the diagram name. The
      // aria-label names the diagram, so the hint becomes the description.
      if (!svg.matches("[aria-label],[aria-labelledby]"))
        svg.setAttribute("aria-label", "Diagram " + diagramIdFor(svg));
    }

    function apply() {
      svg.setAttribute(
        "viewBox",
        view.x + " " + view.y + " " + view.w + " " + view.h,
      );
    }
    function reset() {
      view = { x: initial.x, y: initial.y, w: initial.w, h: initial.h };
      apply();
    }
    function zoomAt(clientX, clientY, factor) {
      var rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      var px = (clientX - rect.left) / rect.width;
      var py = (clientY - rect.top) / rect.height;
      var fx = view.x + view.w * px;
      var fy = view.y + view.h * py;
      var next = Math.min(
        Math.max(view.w * factor, initial.w / 40),
        initial.w * 8,
      );
      var scale = next / view.w;
      view.w = next;
      view.h *= scale;
      view.x = fx - (fx - view.x) * scale;
      view.y = fy - (fy - view.y) * scale;
      apply();
    }

    // Only Ctrl or Cmd with the wheel zooms, so a plain wheel still scrolls
    // the page.
    svg.addEventListener(
      "wheel",
      function (event) {
        if (annotating || !(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        // One mouse notch is deltaY 120. A trackpad sends many small deltas,
        // so the step follows the delta size, not its sign.
        var steps = Math.max(
          -1,
          Math.min(1, event.deltaMode ? event.deltaY : event.deltaY / 120),
        );
        if (!steps) return;
        zoomAt(event.clientX, event.clientY, Math.pow(1.15, steps));
      },
      { passive: false },
    );
    svg.addEventListener("dblclick", function () {
      if (!annotating) reset();
    });
    svg.addEventListener("pointerdown", function (event) {
      if (annotating || event.button !== 0) return;
      // A viewBox write never resizes the client box, so one size read per
      // drag avoids a layout pass on every pointer move.
      var rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      panning = {
        x: event.clientX,
        y: event.clientY,
        vx: view.x,
        vy: view.y,
        width: rect.width,
        height: rect.height,
      };
      if (svg.setPointerCapture) svg.setPointerCapture(event.pointerId);
      svg.style.cursor = "grabbing";
    });
    svg.addEventListener("pointermove", function (event) {
      if (!panning) return;
      view.x =
        panning.vx - ((event.clientX - panning.x) / panning.width) * view.w;
      view.y =
        panning.vy - ((event.clientY - panning.y) / panning.height) * view.h;
      apply();
    });
    function endPan(event) {
      panning = null;
      if (svg.releasePointerCapture) {
        try {
          svg.releasePointerCapture(event.pointerId);
        } catch (err) {}
      }
      svg.style.cursor = annotating ? "" : "grab";
    }
    svg.addEventListener("pointerup", endPan);
    svg.addEventListener("pointercancel", endPan);

    return {
      svg: svg,
      setFrozen: function (frozen) {
        panning = null;
        svg.style.cursor = frozen ? "" : "grab";
        // Vertical touch drags scroll the page, and horizontal drags pan the
        // diagram. A browser scroll fires pointercancel, so endPan still runs.
        svg.style.touchAction = frozen ? "" : "pan-y";
        if (!hint) return;
        if (frozen) hint.remove();
        else svg.insertBefore(hint, svg.firstChild);
      },
    };
  }

  function enhanceMermaidSvgs() {
    exploreViewports = exploreViewports.filter(function (viewport) {
      return viewport.svg.isConnected;
    });
    allMermaidSvgs().forEach(function (svg) {
      // A marked SVG already has its node keys. Re-renders arrive unmarked.
      if (svg.hasAttribute("data-arev-explore")) return;
      spreadWhenLaidOut(svg);
      mermaidNodeGroups(svg).forEach(function (group, index) {
        group.setAttribute(
          "data-arev-node-key",
          stableNodeKey(group, svg, index),
        );
      });
      var viewport = createExploreViewport(svg);
      if (!viewport) return;
      svg.setAttribute("data-arev-explore", "true");
      viewport.setFrozen(annotating);
      exploreViewports.push(viewport);
    });
  }

  document.addEventListener("arev:mermaid-rendered", enhanceMermaidSvgs);


  function captureMermaidSources() {
    var holders = [];
    var nodes = document.querySelectorAll(
      "pre.mermaid, div.mermaid, pre > code.language-mermaid",
    );
    Array.prototype.forEach.call(nodes, function (node) {
      var holder = node;
      if (node.tagName === "CODE") {
        holder = node.closest(".mermaid") || node.parentElement;
      }
      if (!holder || holders.indexOf(holder) !== -1) return;
      holders.push(holder);
      if (
        holder.hasAttribute("data-mermaid-source") ||
        holder.hasAttribute("data-arev-mermaid-source")
      )
        return;
      var sourceNode =
        node.tagName === "CODE"
          ? node
          : holder.querySelector("code.language-mermaid") || holder;
      holder.setAttribute(
        "data-arev-mermaid-source",
        sourceNode.textContent || "",
      );
    });
  }

  // The review rail shows this title. The authored id keeps a generated hash
  // suffix out of the name.
  function diagramTitle(holder, authoredId, index) {
    var figure = holder.closest("figure");
    var caption = figure ? figure.querySelector(":scope > figcaption") : null;
    var title =
      normalizedText(caption) ||
      (holder.getAttribute("aria-label") || "").trim();
    if (title) return title.slice(0, 80);
    var words = String(authoredId || "").replace(/[-_]+/g, " ").trim();
    if (!words) return "Diagram " + (index + 1);
    return (words.charAt(0).toUpperCase() + words.slice(1)).slice(0, 80);
  }

  function findMermaid() {
    var blocks = [];
    var holders = [];
    var nodes = document.querySelectorAll(
      "pre.mermaid, div.mermaid, pre > code.language-mermaid," +
        "[data-mermaid-source],[data-arev-mermaid-source]",
    );
    Array.prototype.forEach.call(nodes, function (node) {
      var holder = node;
      if (node.tagName === "CODE") {
        holder = node.closest(".mermaid") || node.parentElement;
      }
      if (!holder || holders.indexOf(holder) !== -1) return;
      holders.push(holder);
      var index = blocks.length;
      var authoredId = holder.getAttribute("id");
      var id = safeDiagramId(authoredId, index);
      if (!authoredId) holder.id = id;
      holder.setAttribute("data-arev-diagram-id", id);
      var sourceNode =
        node.tagName === "CODE"
          ? node
          : holder.querySelector("code.language-mermaid") || holder;
      var source =
        holder.getAttribute("data-mermaid-source") ||
        holder.getAttribute("data-arev-mermaid-source") ||
        sourceNode.textContent ||
        "";
      blocks.push({
        id: id,
        title: diagramTitle(holder, authoredId, index),
        selector: "#" + cssEscape(holder.id),
        source: source.trim(),
        index: index,
      });
    });
    return blocks;
  }

  // The resting edit control puts an arev SVG icon inside the diagram block,
  // so only an SVG outside the arev UI counts.
  function drawnSvg(holder) {
    var found = null;
    Array.prototype.forEach.call(holder.querySelectorAll("svg"), function (svg) {
      if (!found && !svg.closest("[data-arev-internal]")) found = svg;
    });
    return found;
  }

  function renderMermaidLocally(blocks) {
    var pending = blocks.some(function (block) {
      var holder = document.getElementById(block.id);
      return (
        holder &&
        !drawnSvg(holder) &&
        !holder.getAttribute("data-processed")
      );
    });
    if (!pending) return null;
    // The review server bundles the same pinned Mermaid as the whiteboard, so
    // diagrams render without a CDN or network access.
    return import(window.location.origin + "/mermaid.js").catch(function (err) {
      console.warn("arev: local Mermaid renderer failed to load", err);
    });
  }

  // A block without an SVG means a Mermaid syntax error or a failed renderer,
  // so the reviewer cannot see the diagram.
  function unrenderedMermaidFindings(blocks) {
    var findings = [];
    blocks.forEach(function (block) {
      var holder = document.getElementById(block.id);
      if (!holder || drawnSvg(holder)) return;
      findings.push({
        selector: block.selector,
        kind: "mermaid-render-failed",
        axis: null,
        overflowPx: null,
        viewportWidth: window.innerWidth,
        persistent: true,
        severity: "severe",
        evidence:
          'Diagram "' +
          block.id +
          '" did not render and is showing its Mermaid source as plain text. ' +
          "Check the syntax of: " +
          block.source.split("\n")[0].slice(0, 60),
      });
    });
    return findings;
  }

  function safeAudit() {
    try {
      return window.__arevAudit(document, window) || [];
    } catch (err) {
      /* A crashed audit must never block the review. */
      return [];
    }
  }

  function boot() {
    var mermaidBlocks = findMermaid();
    var rendering = renderMermaidLocally(mermaidBlocks);
    // The audit waits for the drawn diagrams, because unrendered Mermaid
    // source can be wider than the diagram the reviewer will see.
    (rendering || Promise.resolve()).then(function () {
      enhanceMermaidSvgs();
      var findings = safeAudit();
      if (rendering) {
        findings = findings.concat(unrenderedMermaidFindings(mermaidBlocks));
      }
      send({ type: "audit-done", findings: findings });
      send({
        type: "sdk-ready",
        mermaid: mermaidBlocks,
        title: document.title || "",
      });
    });
  }

  // Mermaid start-on-load can replace the source with SVG before boot, so this
  // call saves the authored source while the document parses.
  captureMermaidSources();

  if (document.readyState === "complete") setTimeout(boot, 50);
  else
    window.addEventListener("load", function () {
      setTimeout(boot, 50);
    });
})();
