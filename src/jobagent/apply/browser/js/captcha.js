() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 20 && r.height > 20 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const frames = Array.from(document.querySelectorAll('iframe')).filter(visible);
  for (const f of frames) {
    const src = f.src || '';
    if (/recaptcha\/api2\/(bframe|anchor)/i.test(src) && !/size=invisible/i.test(src)) return 'reCAPTCHA';
    if (/hcaptcha\.com/i.test(src)) return 'hCaptcha';
    if (/challenges\.cloudflare\.com/i.test(src)) return 'Cloudflare Turnstile';
  }
  const widgets = Array.from(document.querySelectorAll('.h-captcha, .cf-turnstile, .g-recaptcha:not([data-size="invisible"])')).filter(visible);
  if (widgets.length) return 'captcha widget';
  if (/\b(verify you are human|are you a robot|complete the captcha)\b/i.test(document.body ? document.body.innerText : '')) return 'captcha challenge';
  return null;
}
