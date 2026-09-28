el => {
          const txt = (n) => n ? (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim() : '';
          if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l && txt(l)) return txt(l); }
          const wrap = el.closest('label');
          if (wrap) { const c = wrap.cloneNode(true); c.querySelectorAll('input').forEach(n => n.remove()); if (txt(c)) return txt(c); }
          if (el.tagName !== 'INPUT') { const t = txt(el) || el.getAttribute('aria-label'); if (t) return t.trim(); }
          if (el.nextElementSibling && txt(el.nextElementSibling)) return txt(el.nextElementSibling);
          const choice = 'input[type="radio"], input[type="checkbox"]';
          const wrapper = el.closest('[role="radio"], [role="checkbox"]');
          if (wrapper && wrapper !== el && wrapper.querySelectorAll(choice).length === 1) {
            const t = txt(wrapper) || (wrapper.getAttribute('aria-label') || '').trim();
            if (t) return t;
          }
          let c = el.parentElement, depth = 0;
          while (c && depth < 4 && c.tagName !== 'FORM' && c.tagName !== 'BODY') {
            if (c.querySelectorAll(choice + ', [role="radio"]:not(:has(input))').length > 1) break;
            if (txt(c)) return txt(c);
            c = c.parentElement; depth++;
          }
          return el.value || '';
        }
