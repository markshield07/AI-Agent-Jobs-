// The calendar a date box opened (react-datepicker, jQuery UI and the like).
// step "lists": mark its year and month lists (data-jobagent-picker="year" /
// "month") and return whether both were found. step "day": mark the given day
// of the month on show (data-jobagent-picker="day"), skipping the days of the
// months either side, and return whether it was found. `selector` is the box.
({ selector, step, day }) => {
  const input = document.querySelector(selector);
  if (!input) return false;
  const shown = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const MONTHS = /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i;
  const isYears = (s) => s.options.length >= 5
    && Array.from(s.options).filter((o) => /^\d{4}$/.test((o.text || '').trim())).length >= s.options.length - 1;
  const isMonths = (s) => Array.from(s.options).filter((o) => MONTHS.test((o.text || '').trim())).length >= 12;
  // The calendar: the nearest box around the date box that holds a year list,
  // else a page-wide one (jQuery UI keeps its calendar at the end of the body).
  let box = null;
  for (let c = input.parentElement, up = 0; c && c !== document.body && up < 6; c = c.parentElement, up++) {
    if (Array.from(c.querySelectorAll('select')).some((s) => shown(s) && isYears(s))) { box = c; break; }
  }
  box = box || document.querySelector('#ui-datepicker-div, .react-datepicker, [class*="datepicker-dropdown" i]');
  if (!box) return false;
  if (step === 'lists') {
    document.querySelectorAll('[data-jobagent-picker]').forEach((el) => el.removeAttribute('data-jobagent-picker'));
    const lists = Array.from(box.querySelectorAll('select')).filter(shown);
    const years = lists.find(isYears);
    const months = lists.find(isMonths);
    if (!years || !months) return false;
    years.setAttribute('data-jobagent-picker', 'year');
    months.setAttribute('data-jobagent-picker', 'month');
    return true;
  }
  const OUTSIDE = /outside|other-month|disabled/i;
  const want = String(day || 1);
  const days = Array.from(box.querySelectorAll('[role="option"], [role="gridcell"], td a, td button, [class*="__day" i], .day'))
    .filter((el) => shown(el) && (el.innerText || el.textContent || '').trim() === want
      && !OUTSIDE.test(el.className || '') && !OUTSIDE.test((el.parentElement && el.parentElement.className) || '')
      && el.getAttribute('aria-disabled') !== 'true');
  if (!days.length) return false;
  days[0].setAttribute('data-jobagent-picker', 'day');
  return true;
}
