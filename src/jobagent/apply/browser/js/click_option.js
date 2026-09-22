([wanted, mode]) => {
  const norm = (s) => (s || '').toLowerCase().replace(/[^\w\s+]/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const want = norm(wanted);
  const lists = Array.from(document.querySelectorAll('[role="listbox"], [class*="menu-list" i], [class*="MenuList"], [class*="__menu"], ul[class*="option" i], [class*="options" i], [class*="dropdown" i] ul, [class*="listbox" i]')).filter(visible);
  let options = [];
  for (const lb of lists) {
    options = Array.from(lb.querySelectorAll('[role="option"], [class*="option" i], li')).filter(visible);
    if (options.length) break;
  }
  if (!options.length) options = Array.from(document.querySelectorAll('[role="option"]')).filter(visible);
  const texts = options.map(o => norm(o.innerText || o.textContent));
  let idx = texts.findIndex(t => t === want);
  if (idx < 0 && mode !== 'exact') idx = texts.findIndex(t => t.startsWith(want) || want.startsWith(t) && t.length > 2);
  if (idx < 0 && mode !== 'exact') idx = texts.findIndex(t => new RegExp('\\b' + want.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b').test(t));
  if (idx < 0) return false;
  const el = options[idx];
  el.scrollIntoView({block: 'nearest'});
  for (const type of ['mousedown', 'mouseup', 'click']) el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  return true;
}
