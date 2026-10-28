window.__arevAudit = function runAudit(doc, win) {
  var findings = [];
  var vw = win.innerWidth;
  var vh = win.innerHeight;
  var MAX_ELS = 3000;

  function push(f) {
    f.axis = f.axis || null;
    f.overflowPx = (f.overflowPx == null) ? null : Math.round(f.overflowPx);
    f.viewportWidth = vw;
    f.persistent = true;
    findings.push(f);
  }

  function shortSelector(el) {
    var segs = [];
    var node = el;
    while (node && node.nodeType === 1 && segs.length < 3) {
      if (node.id) {
        segs.unshift('#' + node.id);
        break;
      }
      var cls = (typeof node.className === 'string' && node.className.trim())
        ? node.className.trim().split(/\s+/)[0]
        : '';
      if (cls) {
        segs.unshift(node.tagName.toLowerCase() + '.' + cls);
      } else {
        var idx = 1, sib = node;
        while ((sib = sib.previousElementSibling)) { if (sib.tagName === node.tagName) idx++; }
        segs.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
      }
      node = node.parentElement;
    }
    return segs.join(' > ');
  }

  function hasDirectText(el) {
    var kids = el.childNodes;
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].nodeType === 3 && kids[i].nodeValue.trim()) return true;
    }
    return false;
  }

  function isControl(el) {
    var tag = el.tagName;
    return tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'SELECT' || el.hasAttribute('data-arev-action');
  }

  function describeControl(el) {
    var label = el.tagName.toLowerCase();
    if (el.hasAttribute('data-arev-action')) label += '[data-arev-action="' + el.getAttribute('data-arev-action') + '"]';
    var txt = (el.textContent || '').trim().slice(0, 30);
    if (txt) label += ' "' + txt + '"';
    return label;
  }

  function outsideDocument(box) {
    var pageLeft = box.left + win.scrollX, pageTop = box.top + win.scrollY;
    var docW = doc.documentElement.scrollWidth, docH = doc.documentElement.scrollHeight;
    return pageLeft + box.width < 0 || pageLeft > docW || pageTop + box.height < 0 || pageTop > docH;
  }

  // Scroll box content does not widen the document, so a control is measured
  // against each scroll box around it.
  function outsideScrollableArea(el, rect) {
    if (!outsideDocument(rect)) return false;
    var box = rect;
    for (var node = el.parentElement; node && node !== doc.body; node = node.parentElement) {
      var ncs = win.getComputedStyle(node);
      if (!/auto|scroll/.test(ncs.overflowX + ' ' + ncs.overflowY)) continue;
      var nr = node.getBoundingClientRect();
      var left = box.left - nr.left - node.clientLeft + node.scrollLeft;
      var top = box.top - nr.top - node.clientTop + node.scrollTop;
      if (left + box.width < 0 || left > node.scrollWidth || top + box.height < 0 || top > node.scrollHeight) return true;
      box = nr;
    }
    return outsideDocument(box);
  }

  function isOpaqueBg(cs) {
    var m = (cs.backgroundColor || '').match(/rgba?\(([^)]+)\)/);
    if (m) {
      var parts = m[1].split(',');
      var alpha = parts.length > 3 ? parseFloat(parts[3]) : 1;
      if (alpha >= 0.95) return true;
    }
    return !!(cs.backgroundImage && cs.backgroundImage !== 'none');
  }

  if (!doc.body) return findings;

  // 1. escaped markup rendered as visible text
  var MARKUP_RE = /<\/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>|&lt;\/?[a-zA-Z]|&amp;lt;/;
  var walker = doc.createTreeWalker(doc.body, 4 /* SHOW_TEXT */, null);
  var tnode, markupHits = 0;
  while ((tnode = walker.nextNode()) && markupHits < 5) {
    var pe = tnode.parentElement;
    if (!pe || pe.closest('pre,code,script,style,textarea')) continue;
    var v = tnode.nodeValue;
    if (!v || !v.trim() || !MARKUP_RE.test(v)) continue;
    push({
      selector: shortSelector(pe),
      kind: 'escaped-markup',
      axis: null,
      overflowPx: null,
      severity: 'severe',
      evidence: 'Visible text renders raw markup instead of HTML: "' + v.trim().slice(0, 60) + '".'
    });
    markupHits++;
  }

  // 2. page-level horizontal overflow
  var dw = doc.documentElement.scrollWidth;
  if (dw - vw > 24) {
    push({
      selector: 'html',
      kind: 'h-overflow',
      axis: 'x',
      overflowPx: dw - vw,
      severity: (dw - vw) > 80 ? 'severe' : 'minor',
      evidence: 'The document is ' + (dw - vw) + 'px wider than the ' + vw + 'px viewport, causing horizontal scroll.'
    });
  }

  // 3 & 4. per-element clipped-text and unreachable-control, plus candidate
  // collection for the occlusion sample
  var all = doc.querySelectorAll('*');
  var n = Math.min(all.length, MAX_ELS);
  var textCandidates = [];

  for (var i = 0; i < n; i++) {
    var el = all[i];
    var tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
    // The review tool's own controls are not part of the artifact's layout.
    if (el.closest('[data-arev-internal]')) continue;
    var cs = win.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var r = el.getBoundingClientRect();

    if (isControl(el)) {
      var reason = null;
      if (r.width === 0 && r.height === 0) {
        reason = 'it renders at zero size';
      } else if (cs.pointerEvents === 'none') {
        reason = 'pointer-events is none on the element itself';
      } else if (outsideScrollableArea(el, r)) {
        reason = 'it sits fully outside the area that its page or scroll box can scroll to';
      }
      if (reason) {
        push({
          selector: shortSelector(el),
          kind: 'unreachable-control',
          axis: null,
          overflowPx: null,
          severity: 'severe',
          evidence: describeControl(el) + ' cannot be reached because ' + reason + '.'
        });
      }
    }

    if (r.width > 0 && r.height > 0 && hasDirectText(el)) {
      var overflowX = el.scrollWidth - el.clientWidth;
      var overflowY = el.scrollHeight - el.clientHeight;
      var hiddenX = cs.overflowX === 'hidden' || cs.overflow === 'hidden';
      var hiddenY = cs.overflowY === 'hidden' || cs.overflow === 'hidden';
      if (hiddenX && overflowX > 8) {
        push({
          selector: shortSelector(el),
          kind: 'clipped-text',
          axis: 'x',
          overflowPx: overflowX,
          severity: overflowX > 24 ? 'severe' : 'minor',
          evidence: overflowX + 'px of text is clipped horizontally because overflow is hidden.'
        });
      } else if (hiddenY && overflowY > 8) {
        push({
          selector: shortSelector(el),
          kind: 'clipped-text',
          axis: 'y',
          overflowPx: overflowY,
          severity: overflowY > 24 ? 'severe' : 'minor',
          evidence: overflowY + 'px of text is clipped vertically because overflow is hidden.'
        });
      }

      if (textCandidates.length < 200 && r.width > 10 && r.height > 8) {
        var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx >= 0 && cx <= vw && cy >= 0 && cy <= vh) textCandidates.push({el: el, cx: cx, cy: cy});
      }
    }
  }

  // 5. occlusion - sample up to 12 evenly spaced visible text blocks
  if (textCandidates.length) {
    var sampleCount = Math.min(12, textCandidates.length);
    var step = textCandidates.length / sampleCount;
    var occluded = [];
    for (var s = 0; s < sampleCount; s++) {
      var c = textCandidates[Math.floor(s * step)];
      var hit = doc.elementFromPoint(c.cx, c.cy);
      if (!hit || hit === c.el || c.el.contains(hit) || hit.contains(c.el)) continue;
      if (hit.closest('[data-arev-internal]')) continue;
      var hcs = win.getComputedStyle(hit);
      if (isOpaqueBg(hcs)) occluded.push(hit);
    }
    if (occluded.length >= 3) {
      push({
        selector: shortSelector(occluded[0]),
        kind: 'occlusion',
        axis: null,
        overflowPx: null,
        severity: 'severe',
        evidence: occluded.length + ' of ' + sampleCount + ' sampled text blocks are hidden behind an opaque overlay.'
      });
    }
  }

  return findings;
};
