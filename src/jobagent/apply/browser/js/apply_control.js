// The control on a posting that leads to its application, best first, or null.
// Marks the chosen element with data-jobagent-apply="1" so it can be clicked
// exactly. Buttons that apply through another site's profile (LinkedIn,
// Indeed...), sign-ins, job alerts and site navigation are passed over.
(args) => {
  const { guestOnly, tried } = args || {};
  const afterApply = (tried || []).length > 0;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'
      && !el.closest('[aria-hidden="true"]') && !el.disabled;
  };
  const words = (el) => (el.innerText || el.value || el.getAttribute('aria-label')
    || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const GUEST = /^(apply|continue|proceed)\s+(as\s+(a\s+)?guest|without\s+(an?\s+)?(account|registering|registration|signing\s+in|logging\s+in))\b|^guest\s+apply\b|^apply\s+manually\b|^continue\s+without\s+(an?\s+)?account\b/i;
  const APPLY = /^(apply(\s+now)?|apply\s+(for|to)\s+(this\s+)?(job|position|role|opening|vacancy)|apply\s+online|apply\s+here|start\s+(your\s+|an?\s+)?application|begin\s+(your\s+|an?\s+)?application|i'?m\s+interested|submit\s+(an?\s+|your\s+)?(application|resume|cv))[\s!.›»>→]*$/i;
  const LOOSE = /^apply\b/i;
  // A notice in a dialog that must be accepted to go on (ADP's privacy notice
  // after Apply). Cookie banners are dealt with before any of this.
  const GO_ON = /^((i\s+)?(consent|agree|accept)(\s+(&|and)\s+(continue|proceed))?|continue(\s+to\s+(the\s+)?application)?|proceed(\s+to\s+(the\s+)?application)?)[\s!.›»>→]*$/i;
  // ADP's notice is an sdf-focus-pane with no dialog role, id "…privacy_dailog".
  const inDialog = (el) => !!el.closest('[role="dialog"], [aria-modal="true"], dialog, .modal, '
    + '[class*="modal" i], [id*="dialog" i], [id*="dailog" i], [class*="dialog" i], sdf-focus-pane');
  // Consent tools and search filters have Apply buttons of their own.
  const aside = (el) => !!el.closest('[id^="onetrust"], #onetrust-consent-sdk, [class*="cookie" i], '
    + '[role="search"], [id*="filter" i], [class*="filter" i]');
  const NOT = /linked\s*in|indeed|google|facebook|glassdoor|seek|ziprecruiter|x\.com|twitter|sign\s*(in|up)|log\s*in|register|create\s+(an?\s+)?account|alert|save|share|refer|filter|similar|search|back\s+to|email\s+me|subscribe|talent\s+(community|network)|how\s+to\s+apply|apply\s+later|previous|next\s+job/i;
  const seen = new Set(tried || []);
  document.querySelectorAll('[data-jobagent-apply]').forEach((el) => el.removeAttribute('data-jobagent-apply'));
  const controls = Array.from(document.querySelectorAll(
    'a, button, [role="button"], input[type="button"], input[type="submit"]'));
  let best = null;
  for (const el of controls) {
    if (!visible(el) || aside(el)) continue;
    const text = words(el);
    if (!text || text.length > 60) continue;
    const href = el.tagName === 'A' ? (el.href || '') : '';
    const key = `${text.toLowerCase()}|${href}`;
    if (seen.has(key)) continue;
    let rank = null;
    if (GUEST.test(text)) rank = 0;
    else if (guestOnly) continue;
    else if (NOT.test(text)) continue;
    else if (APPLY.test(text)) rank = 1;
    else if (afterApply && inDialog(el) && GO_ON.test(text)) rank = 0.5;
    else if (LOOSE.test(text) && text.length <= 30) rank = 2;
    if (rank === null) continue;
    if (!best || rank < best.rank) best = { el, rank, text, href, key,
      target: el.getAttribute('target') || '' };
  }
  if (!best) return null;
  best.el.setAttribute('data-jobagent-apply', '1');
  return { text: best.text, href: best.href, key: best.key, target: best.target, rank: best.rank };
}
