el => {
          const txt = (n) => n ? (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim() : '';
          if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l && txt(l)) return txt(l); }
          const wrap = el.closest('label');
          if (wrap) { const c = wrap.cloneNode(true); c.querySelectorAll('input').forEach(n => n.remove()); if (txt(c)) return txt(c); }
          if (el.nextElementSibling && txt(el.nextElementSibling)) return txt(el.nextElementSibling);
          return el.value || '';
        }
