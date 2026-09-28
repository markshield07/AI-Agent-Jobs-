(selectors) => {
  const FLAG = 'data-jobagent-form';
  Array.from(document.querySelectorAll('[' + FLAG + ']')).forEach(el => el.removeAttribute(FLAG));
  const counts = new Map();
  (selectors || []).forEach(sel => {
    let el = null;
    try { el = document.querySelector(sel); } catch (e) { return; }
    if (!el) return;
    const form = el.closest('form');
    if (!form) return;
    counts.set(form, (counts.get(form) || 0) + 1);
  });
  let best = null, most = 0;
  counts.forEach((n, form) => { if (n > most) { most = n; best = form; } });
  if (!best) return false;
  best.setAttribute(FLAG, '1');
  return true;
}
