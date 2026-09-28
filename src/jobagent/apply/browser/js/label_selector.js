el => { if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return 'label[for="' + CSS.escape(el.id) + '"]'; }
                  const w = el.closest('label'); return w && w.id ? '#' + CSS.escape(w.id) : null; }
