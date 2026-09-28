(selector) => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!visible(el)) continue;
    let t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t && el.getAttribute('aria-invalid') === 'true') {
      const l = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
      t = (l ? (l.innerText || '').trim() : (el.name || el.id || 'a field')) + ' is invalid';
    }
    if (t && t.length <= 300 && !out.includes(t)) out.push(t);
    if (out.length >= 8) break;
  }
  return out;
}
