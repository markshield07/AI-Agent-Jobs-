// What surrounds a form: the nearest heading above its first field, and the
// text of the part of the page around its fields (the box holding them all,
// widened while it stays short enough to be about this form alone). Used to
// tell a talent-community sign-up or an email-code check from an application.
// `selectors` are the form's fields.
(selectors) => {
  const txt = (el) => (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
  // The words of a box, less the choices of its selects (a country list alone
  // runs to thousands of characters).
  const words = (root) => {
    if (!root) return '';
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let out = '';
    while (walk.nextNode()) {
      const parent = walk.currentNode.parentElement;
      if (parent && parent.closest('select, option, datalist, script, style')) continue;
      out += ' ' + walk.currentNode.nodeValue;
    }
    return out.replace(/\s+/g, ' ').trim();
  };
  const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const els = [];
  for (const sel of selectors || []) {
    try { const el = document.querySelector(sel); if (el) els.push(el); } catch (e) { /* skip */ }
  }
  if (!els.length) return null;
  const first = els[0];
  let heading = '';
  const HEADINGS = 'h1, h2, h3, h4, legend, [role="heading"]';
  for (let scope = first.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
    const before = Array.from(scope.querySelectorAll(HEADINGS)).filter((h) => shown(h)
      && txt(h) && (h.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING));
    if (before.length) { heading = txt(before[before.length - 1]).slice(0, 120); break; }
  }
  let box = first.parentElement;
  while (box && box !== document.body && !els.every((el) => box.contains(el))) box = box.parentElement;
  let text = words(box);
  for (let up = 0, c = box && box.parentElement; up < 3 && c && c !== document.body; up++, c = c.parentElement) {
    const t = words(c);
    if (t.length > 3000) break;
    text = t;
  }
  return { heading, text: text.slice(0, 4000) };
}
