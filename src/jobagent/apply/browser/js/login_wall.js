() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const passwords = Array.from(document.querySelectorAll('input[type="password"]')).filter(visible);
  if (!passwords.length) return false;
  const fields = Array.from(document.querySelectorAll('input[type="file"], textarea, input[type="tel"]')).filter(visible);
  return fields.length === 0;
}
