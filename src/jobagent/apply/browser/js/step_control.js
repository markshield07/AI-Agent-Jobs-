// The button that ends this page of a multi-page application: the one that
// sends it ("submit") or the one that goes on to the next page ("next"), or
// null. Looked for near the form's fields first (`selectors`), then anywhere.
// The chosen button is marked data-jobagent-step="submit" or "next".
(selectors) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  };
  const words = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim();
  const SUBMIT = /^(submit(\s+(my\s+|your\s+|the\s+)?application)?|send(\s+(my\s+)?application)?|finish(\s+application)?|complete(\s+(my\s+)?application)?)[\s!.›»>→]*$/i;
  const NEXT = /^(next(\s+step|\s+page)?|continue|save\s*(and|&)\s*(continue|next)|proceed|review(\s+(my\s+|your\s+)?application)?)[\s›»>→]*$/i;
  const NOT = /previous|back|cancel|linked\s*in|indeed|sign\s*(in|up)|log\s*in|guest|save\s+(for\s+later|as\s+draft|draft)|next\s+job|withdraw/i;
  document.querySelectorAll('[data-jobagent-step]').forEach((el) => el.removeAttribute('data-jobagent-step'));
  let scope = null;
  for (const sel of selectors || []) {
    let el = null;
    try { el = document.querySelector(sel); } catch (e) { el = null; }
    if (!el) continue;
    // The smallest box holding the field and a button: the form, or the
    // page section around it when the buttons sit outside the form.
    let box = el.form || el.parentElement;
    while (box && box !== document.body && !box.querySelector('button, [role="button"], input[type="submit"]')) box = box.parentElement;
    scope = box;
    break;
  }
  const pick = (root) => {
    const controls = Array.from((root || document).querySelectorAll(
      'button, [role="button"], input[type="submit"], input[type="button"], a'));
    let next = null;
    for (const el of controls) {
      if (!visible(el)) continue;
      const text = words(el);
      if (!text || text.length > 40 || NOT.test(text)) continue;
      if (SUBMIT.test(text)) return { el, kind: 'submit', text };
      if (!next && NEXT.test(text)) next = { el, kind: 'next', text };
    }
    return next;
  };
  const found = (scope && pick(scope)) || pick(document);
  if (!found) return null;
  found.el.setAttribute('data-jobagent-step', found.kind);
  return { kind: found.kind, text: found.text };
}
