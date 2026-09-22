() => {
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el); return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const lists = Array.from(document.querySelectorAll('[role="listbox"], [class*="menu-list" i], [class*="MenuList"], [class*="__menu"], ul[class*="option" i], [class*="options" i], [class*="dropdown" i] ul, [class*="listbox" i]')).filter(visible);
  for (const lb of lists) {
    const opts = Array.from(lb.querySelectorAll('[role="option"], [class*="option" i], li')).filter(visible).map(txt).filter(Boolean);
    if (opts.length) return Array.from(new Set(opts)).slice(0, 200);
  }
  // A menu with no class or role the list above names: take the options
  // themselves, wherever they are, as long as they are on screen.
  const loose = Array.from(document.querySelectorAll('[role="option"]')).filter(visible).map(txt).filter(Boolean);
  if (loose.length) return Array.from(new Set(loose)).slice(0, 200);
  return [];
}
