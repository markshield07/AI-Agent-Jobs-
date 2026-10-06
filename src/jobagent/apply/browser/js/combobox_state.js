// What a react-select combobox holds: {placeholder, chosen, typed}.
// react-select draws an empty box as a placeholder div (its id is
// react-select-<input id>-placeholder, or named in aria-describedby); a choice
// replaces it with the chosen value and empties the input. Text still in the
// input with the menu closed was typed and never chosen. Greenhouse's phone
// Country shows only "+1" once chosen, and keeps the input's value and its
// data-value empty, so neither of those says anything.
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const ids = new Set();
  if (el.id) ids.add('react-select-' + el.id + '-placeholder');
  for (const id of (el.getAttribute('aria-describedby') || '').split(/\s+/)) {
    if (/placeholder/i.test(id)) ids.add(id);
  }
  const placeholder = Array.from(ids).some((id) => document.getElementById(id));
  const box = el.closest('[class*="container" i]') || el.closest('[class*="control" i]')
    || (el.parentElement && el.parentElement.parentElement);
  const value = box && box.querySelector(
    '[class*="single-value" i], [class*="singlevalue" i], [class*="multi-value" i], [class*="multivalue" i]');
  return {
    placeholder,
    chosen: Boolean(value && (value.textContent || '').trim()),
    typed: (el.value || '').trim(),
  };
}
