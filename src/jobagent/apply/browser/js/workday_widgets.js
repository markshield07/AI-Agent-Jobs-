(root) => {
  // Workday draws three kinds of question with controls the inventory does
  // not read as what they are: a dropdown that is a <button aria-haspopup>
  // opening a listbox, a "prompt" (a search box that picks items into
  // pills, e.g. How did you hear about us?), and a date split into month,
  // day and year spin buttons. Each comes back as one widget, and the
  // controls inside it are marked as covered so the inventory's own reading
  // of them (a text box called "Search", three called "Month" and so on)
  // can be dropped.
  const base = (root && document.querySelector(root)) || document;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1');
  const txt = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  document.querySelectorAll('[data-jobagent-widget]').forEach((el) => el.removeAttribute('data-jobagent-widget'));
  document.querySelectorAll('[data-jobagent-covered]').forEach((el) => el.removeAttribute('data-jobagent-covered'));
  let n = 0;
  const selectorFor = (el) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) return '#' + esc(el.id);
    n += 1;
    el.setAttribute('data-jobagent-widget', String(n));
    return '[data-jobagent-widget="' + n + '"]';
  };
  const box = (el) => el.closest('[data-automation-id^="formField-"]') || el.closest('fieldset') || el.parentElement;
  const labelOf = (el, b) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + esc(el.id) + '"]');
      if (l && txt(l)) return txt(l);
    }
    if (b) {
      const l = b.querySelector('label, legend');
      if (l && txt(l)) return txt(l);
    }
    const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map((id) => txt(document.getElementById(id))).filter(Boolean).join(' ');
    if (by) return by;
    return (el.getAttribute('aria-label') || '').trim();
  };
  const requiredOf = (el, b, label) => el.getAttribute('aria-required') === 'true' || /\*/.test(label)
    || !!(b && b.querySelector('abbr[title="required" i], [data-automation-id="requiredIndicator"], [aria-required="true"]'));
  const context = (el) => {
    const out = [];
    let c = el.parentElement, depth = 0;
    while (c && depth < 14 && c.tagName !== 'BODY') {
      const id = c.getAttribute('data-automation-id');
      if (id) out.push(id);
      if (c.id) out.push('#' + c.id);
      c = c.parentElement; depth += 1;
    }
    return out.join(' ').slice(0, 600);
  };
  const keyOf = (el, b, fallback) => el.id || (b && b.getAttribute('data-automation-id')) || fallback;
  const out = [];

  base.querySelectorAll('button[aria-haspopup="listbox"]').forEach((el, i) => {
    if (!visible(el)) return;
    const b = box(el);
    const label = labelOf(el, b);
    const shown = txt(el);
    out.push({ widget: 'dropdown', key: keyOf(el, b, 'dropdown-' + i), selector: selectorFor(el),
      label, required: requiredOf(el, b, label), context: context(el),
      value: /^(select one|select|choose one)$/i.test(shown) ? '' : shown });
  });

  const prompts = new Set();
  base.querySelectorAll('[data-automation-id="multiselectInputContainer"], [data-automation-id="multiSelectContainer"], input[data-automation-id="searchBox"]').forEach((el) => {
    const container = el.tagName === 'INPUT'
      ? (el.closest('[data-automation-id="multiselectInputContainer"], [data-automation-id="multiSelectContainer"]') || el.parentElement)
      : el;
    if (prompts.has(container)) return;
    prompts.add(container);
    const input = container.querySelector('input[data-automation-id="searchBox"], input[type="text"], input:not([type])');
    if (!input || !visible(input)) return;
    const b = box(container);
    const label = labelOf(input, b);
    const scope = b || container;
    const picked = Array.from(scope.querySelectorAll('[data-automation-id="selectedItem"]')).map(txt).filter(Boolean);
    container.setAttribute('data-jobagent-covered', '1');
    out.push({ widget: 'prompt', key: keyOf(input, b, 'prompt-' + out.length), selector: selectorFor(input),
      label, required: requiredOf(input, b, label), context: context(input), value: picked.join(', ') });
  });

  const dates = new Set();
  base.querySelectorAll('[data-automation-id*="dateSectionMonth"], [data-automation-id*="dateSectionYear"]').forEach((part) => {
    const wrapper = part.closest('[data-automation-id="dateInputWrapper"]') || part.parentElement;
    if (!wrapper || dates.has(wrapper)) return;
    dates.add(wrapper);
    const find = (name) => {
      const el = wrapper.querySelector('input[data-automation-id*="' + name + '"], [data-automation-id*="' + name + '"] input');
      return el && visible(el) ? el : null;
    };
    const month = find('dateSectionMonth'), day = find('dateSectionDay'), year = find('dateSectionYear');
    if (!month && !year) return;
    const first = month || year;
    const b = box(wrapper);
    let label = labelOf(first, b);
    if (/^(month|day|year)$/i.test(label)) label = b ? txt(b.querySelector('label, legend')) : '';
    const values = [month, day, year].map((el) => el ? (el.value || '').trim() : '');
    wrapper.setAttribute('data-jobagent-covered', '1');
    out.push({ widget: 'date', key: keyOf(wrapper, b, 'date-' + out.length), selector: selectorFor(wrapper),
      label, required: requiredOf(first, b, label), context: context(wrapper),
      parts: [month ? selectorFor(month) : null, day ? selectorFor(day) : null, year ? selectorFor(year) : null],
      value: values.filter(Boolean).length === [month, day, year].filter(Boolean).length && values.some(Boolean)
        ? values.filter(Boolean).join('/') : '' });
  });

  // A file input with a file already on Workday's side (the resume uploaded
  // on the first page shows again under My Experience): its name, by the
  // input's selector, so it is not uploaded twice.
  const uploaded = {};
  base.querySelectorAll('input[type="file"]').forEach((el) => {
    const b = el.closest('[data-automation-id^="formField-"]') || el.closest('[data-automation-id*="attachments" i]') || el.parentElement;
    const done = b && b.querySelector('[data-automation-id="file-upload-successful"], [data-automation-id="fileName"], [data-automation-id="file-upload-item"]');
    if (done && txt(done) && el.id) uploaded['#' + esc(el.id)] = txt(done);
  });

  return { widgets: out, uploaded };
}
