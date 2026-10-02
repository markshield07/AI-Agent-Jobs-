(root) => {
  // `root` narrows the inventory to one element, e.g. a modal over a page
  // whose own search box would otherwise be read as part of the form.
  const base = (root && document.querySelector(root)) || document;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1');
  const cssPath = (el) => {
    if (!el || el.nodeType !== 1) return null;
    if (el.id) return '#' + esc(el.id);
    const name = el.getAttribute('name');
    const tag = el.tagName.toLowerCase();
    if (name) {
      const same = document.querySelectorAll(tag + '[name="' + name.replace(/"/g, '\\"') + '"]');
      if (same.length === 1) return tag + '[name="' + name.replace(/"/g, '\\"') + '"]';
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 8) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift('#' + esc(cur.id)); break; }
      const sibs = Array.from(cur.parentNode ? cur.parentNode.children : []).filter(c => c.tagName === cur.tagName);
      if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const txt = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  // Radios with no shared name (LinkedIn's 2026 Easy Apply draws its Yes/No
  // questions so) are one question all the same: the smallest box holding
  // this choice and another, or their fieldset or radiogroup. That box is
  // marked, so the filler can find the whole group again by it.
  const GROUP_ATTR = 'data-jobagent-group';
  document.querySelectorAll('[' + GROUP_ATTR + ']').forEach((n) => n.removeAttribute(GROUP_ATTR));
  const CHOICE = 'input[type="radio"], [role="radio"]';
  const isChoice = (el) => (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'radio') || el.getAttribute('role') === 'radio';
  const groupBoxOf = (el) => {
    if (!isChoice(el) || el.getAttribute('name')) return null;
    const set = el.closest('fieldset, [role="radiogroup"]');
    if (set && set.querySelectorAll(CHOICE).length > 1) return set;
    let c = el.parentElement, depth = 0;
    while (c && depth < 6 && c.tagName !== 'FORM' && c.tagName !== 'BODY') {
      if (c.querySelectorAll(CHOICE).length > 1) return c;
      c = c.parentElement; depth++;
    }
    return set;
  };
  let groups = 0;
  const groupOf = (el) => {
    const box = groupBoxOf(el);
    if (!box) return null;
    if (!box.getAttribute(GROUP_ATTR)) box.setAttribute(GROUP_ATTR, 'g' + (++groups));
    return box.getAttribute(GROUP_ATTR);
  };
  // LinkedIn wraps each native choice in a <div role="radio"> whose
  // aria-label is the whole question and whose own text is the option: the
  // native input inside is the control, and the wrapper is not a second one.
  const NATIVE_CHOICE = 'input[type="radio"], input[type="checkbox"]';
  const isWrapper = (n) => {
    const role = n.getAttribute && n.getAttribute('role');
    return (role === 'radio' || role === 'checkbox') && n.tagName !== 'INPUT' && !!n.querySelector(NATIVE_CHOICE);
  };
  // A choice's own words when its <label> is empty: the text of the box that
  // holds it and no other choice ("Yes" beside the circle), else that box's
  // aria-label (LinkedIn's saved resume, labelled by its file name).
  const optionText = (el) => {
    const wrap = el.closest('[role="radio"], [role="checkbox"]');
    if (wrap && wrap !== el && wrap.querySelectorAll(NATIVE_CHOICE).length === 1) {
      const t = txt(wrap) || (wrap.getAttribute('aria-label') || '').trim();
      if (t) return t;
    }
    let c = el.parentElement, depth = 0;
    while (c && depth < 4 && c.tagName !== 'FORM' && c.tagName !== 'BODY') {
      if (c.querySelectorAll(NATIVE_CHOICE + ', [role="radio"]:not(:has(input))').length > 1) break;
      if (txt(c)) return txt(c);
      c = c.parentElement; depth++;
    }
    return '';
  };
  const samePeers = (el) => {
    const box = groupBoxOf(el);
    return box ? Array.from(box.querySelectorAll(CHOICE)) : [];
  };
  const byIds = (ids) => (ids || '').split(/\s+/).filter(Boolean).map(id => txt(document.getElementById(id))).filter(Boolean).join(' ');
  const isHidden = (el) => {
    if (el.type === 'hidden') return true;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return true;
    const r = el.getBoundingClientRect();
    return r.width === 0 && r.height === 0;
  };
  // The boxes a control may live in with its label, innermost first: a
  // fieldset or a div that names itself a field, question or group. Never the
  // form or the body, which would make every label in the form look like this
  // control's. A box is only a candidate while it holds this control and
  // little else: one that wraps several questions (a section) would lend the
  // wrong question's text. The innermost box often holds the control alone
  // (Lever's div.application-field), so the search keeps climbing and the
  // caller takes the first box that actually yields a label.
  const strangers = (c, el) => {
    const name = el.getAttribute('name');
    const peers = samePeers(el);
    return Array.from(c.querySelectorAll('input, select, textarea, [role="radio"]')).filter(n => {
      if (n === el || peers.includes(n) || isWrapper(n)) return false;
      const t = (n.getAttribute('type') || '').toLowerCase();
      if (n.tagName === 'INPUT' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(t)) return false;
      if (name && n.getAttribute('name') === name) return false;  // the rest of this radio group
      return true;
    }).length;
  };
  const containers = (el) => {
    const out = [];
    let c = el.parentElement, depth = 0;
    while (c && depth < 6 && c.tagName !== 'FORM' && c.tagName !== 'BODY') {
      const cls = (typeof c.className === 'string' ? c.className : '') + ' ' + (c.id || '');
      const named = c.tagName === 'FIELDSET' || /field|question|form-group|form-field|input-group|application-|card|row|section/i.test(cls);
      const alone = strangers(c, el) === 0;
      // A box that holds nothing else cannot lend another question's label,
      // whatever it calls itself; a box that names itself a field may hold one
      // stranger (a second input for the same answer).
      if (alone || (named && strangers(c, el) <= 1)) out.push(c);
      c = c.parentElement; depth++;
    }
    if (!out.length) {
      const parent = el.parentElement;
      if (parent && parent.tagName !== 'FORM' && parent.tagName !== 'BODY' && parent.children.length <= 12) out.push(parent);
    }
    return out;
  };
  const container = (el) => containers(el)[0] || null;
  // For a flat form, the label is usually the nearest preceding sibling.
  const precedingSibling = (el) => {
    let s = el.previousElementSibling, steps = 0;
    while (s && steps < 4) {
      if (/^(LABEL|LEGEND|H[1-6]|P|SPAN|DIV)$/.test(s.tagName) && !s.querySelector('input, select, textarea') && txt(s) && txt(s).length <= 300) return txt(s);
      if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(s.tagName)) return '';
      s = s.previousElementSibling; steps++;
    }
    return '';
  };
  const labelOf = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + esc(el.id) + '"]');
      if (l && txt(l)) return txt(l);
    }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.getAttribute('aria-labelledby')) { const t = byIds(el.getAttribute('aria-labelledby')); if (t) return t; }
    const wrap = el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
      if (txt(clone)) return txt(clone);
    }
    for (const c of containers(el)) {
      const cands = Array.from(c.querySelectorAll('label, legend, [class*="label" i], [class*="Label"], h1, h2, h3, h4, h5, p, span, div'))
        .filter(n => n !== el && !n.contains(el) && (n.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && txt(n) && txt(n).length <= 300 && !n.querySelector('input, select, textarea'));
      if (cands.length) return txt(cands[cands.length - 1]);
    }
    const sib = precedingSibling(el);
    if (sib) return sib;
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    return '';
  };
  const legendOf = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg); }
    const box = groupBoxOf(el);
    if (box) {
      if (box.getAttribute('aria-labelledby')) { const t = byIds(box.getAttribute('aria-labelledby')); if (t) return t; }
      if (box.getAttribute('aria-label')) return box.getAttribute('aria-label').trim();
    }
    for (const c of containers(el)) {
      const h = Array.from(c.querySelectorAll('legend, h1, h2, h3, h4, h5, label, [class*="label" i], p, span, div'))
        .filter(n => !n.contains(el) && !n.querySelector('input, select, textarea') && txt(n) && txt(n).length <= 300 && (n.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (h.length) return txt(h[0]);
    }
    return '';
  };
  const ancestorsText = (el) => {
    const out = [];
    let c = el.parentElement, depth = 0;
    while (c && depth < 12 && c.tagName !== 'BODY') {
      const cls = typeof c.className === 'string' ? c.className : '';
      if (c.id) out.push('#' + c.id);
      if (cls) out.push(cls);
      Array.from(c.attributes).forEach(a => { if (a.name.startsWith('data-') && a.value.length < 60) out.push(a.name + '=' + a.value); });
      c = c.parentElement; depth++;
    }
    return out.join(' ').slice(0, 800);
  };
  // What a form shows under a question it will not take empty.
  const REQUIRED_TEXT = /^(this field is required|this question is required|an answer is required|please (make a selection|select an option|enter a valid answer))\.?$/i;
  const isRequired = (el, label, legend) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (/\*/.test(label) || /\(required\)/i.test(label)) return true;
    const c = container(el);
    if (c && /\brequired\b/i.test((typeof c.className === 'string' ? c.className : '') + ' ' + (c.getAttribute('data-required') || ''))) return true;
    // A choice's own label is its option ("Yes"); the question it belongs
    // to carries the star (LinkedIn's "...listed in your resume? *"), or its
    // group says so, or the form has already said it is required.
    const grouped = isChoice(el) || (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'checkbox');
    if (!grouped) return false;
    if (/\*\s*$/.test(legend || '')) return true;
    const box = groupBoxOf(el) || el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (box && (box.getAttribute('aria-required') === 'true' || box.hasAttribute('required'))) return true;
    for (const k of containers(el)) {
      if (Array.from(k.querySelectorAll('p, span, div, [role="alert"]')).some(n => !n.querySelector('input, [role="radio"]') && REQUIRED_TEXT.test(txt(n)))) return true;
    }
    return false;
  };
  const sel = 'input, select, textarea, [role="combobox"], [role="radio"], [role="checkbox"], [role="switch"], [role="textbox"], [contenteditable="true"]';
  const seen = new Set();
  const out = [];
  Array.from(base.querySelectorAll(sel)).forEach((el, i) => {
    if (seen.has(el)) return;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (tag === 'button') return;
    if (isWrapper(el)) return;
    // react-select's required decoy (Greenhouse's 2026 board): a see-through
    // text input out of the tab order and hidden from screen readers, there
    // only for the browser's own required check. The combobox beside it is
    // the question; read as its own field it borrows the "Select..."
    // placeholder for a label and can never be clicked.
    if (tag === 'input' && !['radio', 'checkbox', 'file'].includes(type)
        && el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1') return;
    let hidden = isHidden(el);
    if (hidden && tag === 'input' && (type === 'radio' || type === 'checkbox')) {
      // A choice drawn over its input (a styled circle) is there if what
      // draws it is.
      const shown = el.closest('[role="radio"], [role="checkbox"], label');
      if (shown && !isHidden(shown)) hidden = false;
    }
    if (hidden && !(tag === 'input' && type === 'file')) return;
    const label = labelOf(el);
    const legend = legendOf(el);
    const o = {
      idx: i, tag, type: type || (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : ''),
      role: el.getAttribute('role'), name: el.getAttribute('name'), id: el.id || null,
      label, legend, group: groupOf(el),
      help: byIds(el.getAttribute('aria-describedby')),
      placeholder: el.getAttribute('placeholder'), accept: el.getAttribute('accept'),
      multiple: !!el.multiple || el.getAttribute('aria-multiselectable') === 'true',
      autocomplete: el.getAttribute('autocomplete'),
      aria_haspopup: el.getAttribute('aria-haspopup'), aria_autocomplete: el.getAttribute('aria-autocomplete'),
      contenteditable: el.getAttribute('contenteditable') === 'true' || el.getAttribute('role') === 'textbox',
      classes: typeof el.className === 'string' ? el.className.slice(0, 200) : '',
      required: isRequired(el, label, legend), hidden, selector: cssPath(el),
      context: ancestorsText(el),
      value: (tag === 'input' && (type === 'radio' || type === 'checkbox')) ? el.value : null,
      checked: (tag === 'input' && (type === 'radio' || type === 'checkbox')) ? el.checked : null,
      options: tag === 'select' ? Array.from(el.options).map(op => txt(op) || op.value).filter(t => t && !/^-+$|^select\b|^choose\b|^please select/i.test(t)).slice(0, 300) : [],
      option_label: null,
    };
    if (el.getAttribute('role') === 'radio' || el.getAttribute('role') === 'checkbox') o.value = txt(el) || el.getAttribute('aria-label') || el.getAttribute('value') || '';
    if (tag === 'input' && (type === 'radio' || type === 'checkbox')) {
      let ol = '';
      if (el.id) { const l = document.querySelector('label[for="' + esc(el.id) + '"]'); if (l) ol = txt(l); }
      if (!ol) { const wrap = el.closest('label'); if (wrap) { const clone = wrap.cloneNode(true); clone.querySelectorAll('input').forEach(n => n.remove()); ol = txt(clone); } }
      if (!ol && el.nextElementSibling) ol = txt(el.nextElementSibling);
      if (!ol) ol = optionText(el);
      o.option_label = ol || el.value;
    }
    out.push(o);
  });
  return out;
}
