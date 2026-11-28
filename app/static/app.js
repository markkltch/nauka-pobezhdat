// Наука побеждать · Штаб — interface behaviour: composer, voice, attachments, mobile menu.
(() => {
  const touch = matchMedia('(hover: none) and (pointer: coarse)').matches;

  // a long request (the mentor thinks for 10–30 s): lock the form and say so
  document.addEventListener('submit', e => {
    const f = e.target; if (f.dataset.busy === undefined) return;
    const b = f.querySelector('[type=submit]'); if (b) { b.disabled = true; b.setAttribute('aria-busy', 'true'); }
    const n = f.querySelector('[data-busy-note]'); if (n) n.hidden = false;
  });

  // Computer: Enter sends, Shift+Enter breaks the line. Phone: Enter is a new line, the button sends.
  document.addEventListener('keydown', e => {
    const t = e.target;
    if (e.key !== 'Enter' || touch || e.shiftKey || e.isComposing || !t.matches('textarea[data-enter-sends]')) return;
    e.preventDefault();
    const f = t.form, file = f.querySelector('input[type=file]');
    if (t.value.trim() || (file && file.files.length)) f.requestSubmit();
  });
  document.querySelectorAll('textarea[data-enter-sends]').forEach(t => {
    t.setAttribute('enterkeyhint', touch ? 'enter' : 'send');
    if (touch) t.removeAttribute('autofocus');
    const grow = () => { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight + 2, innerHeight * 0.4) + 'px'; };
    t.addEventListener('input', grow); grow();
  });

  // the text is required unless a file is attached
  document.querySelectorAll('.composer').forEach(f => {
    const t = f.querySelector('textarea'), file = f.querySelector('input[type=file]'), chip = f.querySelector('[data-file-chip]');
    if (!file) return;
    const sync = () => {
      const has = file.files.length > 0;
      t.required = !has;
      if (chip) { chip.hidden = !has; chip.querySelector('span').textContent = has ? file.files[0].name : ''; }
    };
    file.addEventListener('change', () => {
      if (file.files[0] && file.files[0].size > 5 * 1024 * 1024) { alertInline(f, 'Файл больше 5 МБ'); file.value = ''; }
      sync();
    });
    chip?.querySelector('button')?.addEventListener('click', () => { file.value = ''; sync(); t.focus(); });
    sync();
  });

  function alertInline(form, text) {
    let n = form.querySelector('[data-inline-error]');
    if (!n) { n = document.createElement('p'); n.className = 'composer__note composer__note--err'; n.dataset.inlineError = ''; n.setAttribute('role', 'alert'); form.append(n); }
    n.textContent = text; n.hidden = !text;
  }

  // voice: record 16 kHz mono PCM in the browser, the server sends it to SpeechKit
  document.querySelectorAll('[data-voice]').forEach(btn => {
    const form = btn.closest('form'), ta = form.querySelector('textarea'), max = +btn.dataset.max || 30;
    let rec = null;
    if (!navigator.mediaDevices?.getUserMedia) { btn.hidden = true; return; }
    btn.addEventListener('click', async () => {
      if (rec) { rec.stop(); return; }
      alertInline(form, '');
      try { rec = await record(max, s => { btn.dataset.sec = s; }); }
      catch { alertInline(form, 'Нет доступа к микрофону. Разрешите его в настройках браузера.'); return; }
      btn.classList.add('is-rec'); btn.setAttribute('aria-pressed', 'true'); btn.setAttribute('aria-label', 'Остановить запись');
      const pcm = await rec.done; rec = null;
      btn.classList.remove('is-rec'); btn.setAttribute('aria-pressed', 'false'); btn.setAttribute('aria-label', 'Записать голосом'); delete btn.dataset.sec;
      if (pcm.byteLength < 16000) { alertInline(form, 'Запись слишком короткая'); return; }
      btn.disabled = true; btn.classList.add('is-busy'); alertInline(form, 'Распознаю…');
      try {
        const r = await fetch('/voice', { method: 'POST', body: pcm, headers: { 'Content-Type': 'application/octet-stream' } });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || 'Ошибка');
        ta.value = (ta.value.trim() ? ta.value.trim() + ' ' : '') + j.text;
        ta.dispatchEvent(new Event('input')); ta.focus();
        alertInline(form, j.left <= 3 ? `Голосовых в этом месяце осталось: ${j.left}` : '');
      } catch (err) { alertInline(form, err.message); }
      finally { btn.disabled = false; btn.classList.remove('is-busy'); }
    });
  });

  async function record(maxSec, onTick) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = []; let stopped = false, resolve;
    const done = new Promise(r => { resolve = r; });
    proc.onaudioprocess = e => { if (!stopped) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    src.connect(proc); proc.connect(ctx.destination);
    const started = Date.now();
    const tick = setInterval(() => { const s = Math.floor((Date.now() - started) / 1000); onTick(`${s}`); if (s >= maxSec) stop(); }, 250);
    function stop() {
      if (stopped) return; stopped = true; clearInterval(tick);
      proc.disconnect(); src.disconnect(); stream.getTracks().forEach(t => t.stop());
      const rate = ctx.sampleRate; ctx.close();
      resolve(toPcm16k(chunks, rate));
    }
    return { stop, done };
  }

  function toPcm16k(chunks, rate) {
    const len = chunks.reduce((n, c) => n + c.length, 0), all = new Float32Array(len);
    let o = 0; for (const c of chunks) { all.set(c, o); o += c.length; }
    const ratio = rate / 16000, outLen = Math.floor(len / ratio), out = new DataView(new ArrayBuffer(outLen * 2));
    for (let i = 0; i < outLen; i++) {
      const a = Math.floor(i * ratio), b = Math.min(len, Math.floor((i + 1) * ratio));
      let sum = 0; for (let j = a; j < b; j++) sum += all[j];
      const v = Math.max(-1, Math.min(1, sum / Math.max(1, b - a)));
      out.setInt16(i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return out.buffer;
  }

  // the phone top bar draws its hairline only once the page has scrolled
  {
    let raf = 0;
    const mark = () => { raf = 0; document.documentElement.classList.toggle('is-scrolled', scrollY > 4); };
    addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(mark); }, { passive: true });
    mark();
  }

  // phone: the side list opens as a drawer
  const side = document.querySelector('.side'), openers = document.querySelectorAll('[data-menu]');
  if (side && openers.length) {
    const scrim = document.createElement('div'); scrim.className = 'scrim'; document.body.append(scrim);
    const phone = matchMedia('(max-width: 759px)');
    // a closed drawer is out of the tab order and hidden from screen readers
    const sync = () => { side.inert = phone.matches && !document.body.classList.contains('menu-open'); };
    const set = (on, back) => {
      document.body.classList.toggle('menu-open', on);
      openers.forEach(b => b.setAttribute('aria-expanded', on));
      sync();
      if (on) side.querySelector('a, button')?.focus();
      else if (back) openers[0].focus();
    };
    openers.forEach(b => b.addEventListener('click', () => set(!document.body.classList.contains('menu-open'))));
    scrim.addEventListener('click', () => set(false));
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.body.classList.contains('menu-open')) set(false, true); });
    phone.addEventListener('change', sync); sync();
  }

  // show or hide a password
  document.querySelectorAll('[data-pw-toggle]').forEach(btn => btn.addEventListener('click', () => {
    const input = btn.parentElement.querySelector('input'), show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    btn.textContent = show ? 'Скрыть' : 'Показать';
    btn.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
    btn.setAttribute('aria-pressed', String(show));
    input.focus();
  }));

  // examples on the start screen fill the field
  document.querySelectorAll('[data-example]').forEach(b => b.addEventListener('click', () => {
    const t = document.getElementById('say'); if (!t) return;
    t.value = b.dataset.example; t.dispatchEvent(new Event('input')); t.focus();
  }));

  // a разбор's menu: ⋯ opens «Переименовать» and «Удалить»; each opens a dialog that posts to the server
  const I = {
    pen: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.6 2.6l2.8 2.8-7.9 7.9H2.7v-2.8z"/><path d="M9.2 4l2.8 2.8"/></svg>',
    bin: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.8 4.3h10.4M6.3 4.3V2.8h3.4v1.5M4.2 4.3l.7 9h6.2l.7-9M6.7 6.8v4.2M9.3 6.8v4.2"/></svg>',
    team: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="6" cy="5.5" r="2.3"/><path d="M2 13c.5-2.3 2-3.6 4-3.6s3.5 1.3 4 3.6M11 4.5a2 2 0 010 4M12.5 13c-.2-1.4-.8-2.4-1.7-3"/></svg>',
    count: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.7"/><path d="M8 5v3.2l2 1.3"/></svg>'
  };
  const here = () => location.pathname + location.search;
  let pop = null, popFor = null;
  const closePop = (back) => {
    if (!pop) return;
    pop.remove(); pop = null;
    if (popFor) { popFor.setAttribute('aria-expanded', 'false'); if (back) popFor.focus(); }
    popFor = null;
  };
  function dialog(build) {
    const d = document.createElement('dialog'); d.className = 'dlg';
    const f = document.createElement('form'); f.method = 'post'; d.append(f);
    build(f, () => d.close());
    d.addEventListener('close', () => { d.remove(); popFor?.focus(); });
    d.addEventListener('click', e => { if (e.target === d) d.close(); });   // a click on the backdrop
    document.body.append(d); d.showModal();
    return d;
  }
  function el(tag, cls, html) { const n = document.createElement(tag); if (cls) n.className = cls; if (html) n.innerHTML = html; return n; }
  function acts(f, close, okText) {
    const row = el('div', 'dlg__acts');
    const no = el('button', 'btn btn--secondary'); no.type = 'button'; no.textContent = 'Отмена'; no.addEventListener('click', close);
    const ok = el('button', 'btn btn--primary'); ok.type = 'submit'; ok.textContent = okText;
    row.append(no, ok); f.append(row);
    const next = el('input'); next.type = 'hidden'; next.name = 'next'; next.value = here(); f.append(next);
    return ok;
  }
  function askRename(b) {
    const sid = b.dataset.sitMenu;
    dialog((f, close) => {
      f.action = `/app/${sid}/rename`;
      const h = el('h2'); h.textContent = 'Переименовать разбор'; f.append(h);
      const label = el('label'); label.className = 'sr-only'; label.textContent = 'Название'; label.htmlFor = 'dlg-title';
      const i = el('input'); i.type = 'text'; i.name = 'title'; i.id = 'dlg-title'; i.maxLength = 160; i.required = true; i.value = b.dataset.title;
      f.append(label, i);
      acts(f, close, 'Сохранить');
      setTimeout(() => { i.focus(); i.select(); });
    });
  }
  function askDelete(b) {
    const sid = b.dataset.sitMenu;
    dialog((f, close) => {
      f.action = `/app/${sid}/delete`;
      const h = el('h2'); h.textContent = 'Удалить разбор?'; f.append(h);
      const p = el('p'), t = el('b'); t.textContent = `«${b.dataset.title}»`;
      p.append(t, document.createTextNode(' исчезнет вместе с перепиской и планом. Вернуть его будет нельзя.')); f.append(p);
      if (b.dataset.shtab) { const n = el('p', 'dlg__note', I.team); n.append(document.createTextNode('Задачи, которые вы отправили в штаб, останутся у команды.')); f.append(n); }
      const c = el('p', 'dlg__note', I.count); c.append(document.createTextNode('Разбор останется в счётчике этого месяца.')); f.append(c);
      const ok = acts(f, close, 'Удалить');
      setTimeout(() => ok.focus());
    });
  }
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-sit-menu]');
    if (!b) { if (pop && !pop.contains(e.target)) closePop(); return; }
    e.preventDefault();
    if (popFor === b) { closePop(); return; }
    closePop();
    pop = el('div', 'pop'); pop.setAttribute('role', 'menu'); pop.setAttribute('aria-label', 'Действия с разбором');
    const rn = el('button', '', I.pen + '<span>Переименовать</span>'), del = el('button', 'pop__danger', I.bin + '<span>Удалить</span>');
    [rn, del].forEach(x => { x.type = 'button'; x.setAttribute('role', 'menuitem'); });
    rn.addEventListener('click', () => { closePop(); askRename(b); });
    del.addEventListener('click', () => { closePop(); askDelete(b); });
    pop.append(rn, del); document.body.append(pop);
    popFor = b; b.setAttribute('aria-expanded', 'true');
    const r = b.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
    let top = r.bottom + 6; if (top + h > innerHeight - 8) top = Math.max(8, r.top - h - 6);
    pop.style.top = `${top}px`; pop.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
    rn.focus();
  });
  document.addEventListener('keydown', e => {
    if (!pop) return;
    const items = [...pop.querySelectorAll('button')], i = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(true); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
    else if (e.key === 'Tab') closePop();
  }, true);
  addEventListener('resize', () => closePop());
  document.querySelector('.side')?.addEventListener('scroll', () => closePop(), { passive: true });

  // sending like a messenger: no page reload. The message flies from the field into the conversation at once,
  // the mentor «думает» in three dots, and when the server answers, the new part of the conversation rises in
  // place of the dots. The field keeps its focus, so a phone keeps its keyboard. Anything unexpected (a sign-in
  // page, the month's limit, no conversation on the answer page) falls back to an ordinary page load.
  const composer = document.querySelector('form.composer');
  if (composer && window.fetch && window.DOMParser) {
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const ta = composer.querySelector('textarea'), fileIn = composer.querySelector('input[type=file]');
    const sendBtn = composer.querySelector('.composer__send');
    let sending = false;
    // a tap on «отправить» must not take the focus from the field: the phone's keyboard stays open
    sendBtn?.addEventListener('pointerdown', e => { if (document.activeElement === ta) e.preventDefault(); });
    const hhmm = () => new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
    const bubble = (text, file) => {
      const m = el('div', 'msg msg--me');
      if (file) {
        const p = el('p', 'msg__file', '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.8h5.2L12 4.6v9.6H4z"/><path d="M9 1.8v3h3"/></svg><span><b></b><small>Файл</small></span>');
        p.querySelector('b').textContent = file; m.append(p);
      } else { const p = el('p'); p.textContent = text; const t = el('time'); t.textContent = hhmm(); m.append(p, t); }
      return m;
    };
    const typing = () => {
      const m = el('div', 'msg msg--bot msg--typing', '<span class="av av--red" aria-hidden="true"><span class="mark" data-mark="arc"></span></span><div class="bot"><p class="typing" role="status"><span class="typing__dots" aria-hidden="true"><i></i><i></i><i></i></span><span>Наставник думает</span></p></div>');
      setTimeout(() => { const s = m.querySelector('.typing > span:last-child'); if (s) s.textContent = 'Наставник думает — обычно 10–30 секунд'; }, 7000);
      return m;
    };
    // the message leaves the field: from where its text stood in the field to its place in the conversation
    function fly(m, from) {
      if (reduce) return;
      const p = m.querySelector('p'); if (!p) return;
      const to = p.getBoundingClientRect(), cs = getComputedStyle(p);
      const dx = from.x - (to.left + parseFloat(cs.paddingLeft)), dy = from.y - (to.top + parseFloat(cs.paddingTop));
      p.animate([{ transform: `translate(${dx}px, ${dy}px)`, backgroundColor: 'rgb(233 224 207 / 0)' },
                 { transform: 'none', backgroundColor: 'rgb(233 224 207 / 1)' }],
                { duration: 420, easing: 'cubic-bezier(.2,.9,.25,1)' });
      m.querySelector('time')?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 260, delay: 260, fill: 'backwards' });
    }
    const toBottom = () => scrollTo({ top: document.documentElement.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
    // the answer page replaces the head, the side list, the conversation and the field's settings — not the field
    // itself, so the text, the voice button and the focus stay as they are
    function swap(doc) {
      const head = document.querySelector('.talk__head'), nhead = doc.querySelector('.talk__head');
      if (head && nhead) {
        [...head.children].forEach(c => { if (!c.matches('.talk__menu')) c.remove(); });
        [...nhead.children].forEach(c => { if (!c.matches('.talk__menu')) head.append(c); });
      }
      const side = document.querySelector('.side');
      const oldList = side?.querySelector('.list, .side__empty'), newList = doc.querySelector('.side .list, .side .side__empty');
      if (oldList && newList) oldList.replaceWith(newList);
      const oldLabel = side?.querySelector('.side__label'), newLabel = doc.querySelector('.side .side__label');
      if (oldLabel && newLabel) oldLabel.replaceWith(newLabel);
      const feed = document.querySelector('.feed'), nfeed = doc.querySelector('.feed');
      feed.replaceChildren(...nfeed.childNodes);
      const nform = doc.querySelector('form.composer');
      if (!nform) composer.remove();
      else {
        composer.setAttribute('action', nform.getAttribute('action'));
        ta.placeholder = nform.querySelector('textarea').placeholder;
        const l = composer.querySelector('label[for="say"]'), nl = nform.querySelector('label[for="say"]');
        if (l && nl) l.textContent = nl.textContent;
      }
      document.title = doc.title;
      return feed;
    }
    composer.addEventListener('submit', async e => {
      e.preventDefault(); e.stopPropagation();          // not the page-locking «busy» of an ordinary form
      if (sending) return;
      const text = ta.value.trim(), file = fileIn?.files[0];
      if (!text && !file) return;
      sending = true;
      const body = new FormData(composer);
      const feed = document.querySelector('.feed'), end = feed.querySelector('#end');
      const r = ta.getBoundingClientRect(), tcs = getComputedStyle(ta);
      const from = { x: r.left + parseFloat(tcs.paddingLeft), y: r.top + parseFloat(tcs.paddingTop) };
      // the start screen of a new situation steps aside for the conversation
      const start = feed.querySelector('.start');
      if (start) { if (!reduce) await start.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 140 }).finished; start.remove(); }
      const mine = [];
      if (file) mine.push(bubble('', file.name));
      if (text) mine.push(bubble(text));
      const dots = typing();
      mine.concat(dots).forEach(n => end ? end.before(n) : feed.append(n));
      ta.value = ''; ta.dispatchEvent(new Event('input'));
      if (file) { fileIn.value = ''; fileIn.dispatchEvent(new Event('change')); }
      composer.classList.add('is-waiting');
      mine.forEach(m => fly(m, from));
      toBottom();
      try {
        const res = await fetch(composer.action, { method: 'POST', body, credentials: 'same-origin' });
        const url = new URL(res.url), doc = new DOMParser().parseFromString(await res.text(), 'text/html');
        if (!res.ok || !url.pathname.startsWith('/app') || url.searchParams.has('limit') || !doc.querySelector('.feed')) {
          location.assign(res.url); return;
        }
        const failed = url.searchParams.has('err') || url.searchParams.has('ferr');
        if (url.pathname !== location.pathname) history.pushState(null, '', url.pathname);
        const before = document.querySelector('.talk__head')?.getBoundingClientRect().height || 0;
        const f = swap(doc);
        if (failed) {
          // the message was not taken: give it back to the field, show what the page says
          ta.value = text; ta.dispatchEvent(new Event('input'));
          if (file && window.DataTransfer) { const dt = new DataTransfer(); dt.items.add(file); fileIn.files = dt.files; fileIn.dispatchEvent(new Event('change')); }
          const note = [...f.querySelectorAll('.error, .notice')].pop();
          note?.classList.add('is-arriving'); note?.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
        } else {
          // what is new is everything after my last message: it rises in place of the dots
          const me = [...f.querySelectorAll('.msg--me')].pop();
          const fresh = []; for (let n = me?.nextElementSibling; n; n = n.nextElementSibling) if (n.id !== 'end') fresh.push(n);
          fresh.forEach((n, i) => { n.classList.add('is-arriving'); n.style.animationDelay = `${i * 70}ms`; });
          // a short answer: to the end; a long one (a plan): its beginning under the head
          const top = me ? me.getBoundingClientRect().top + scrollY - before - 16 : 0;
          const max = document.documentElement.scrollHeight - innerHeight;
          scrollTo({ top: Math.min(Math.max(top, 0), max), behavior: reduce ? 'auto' : 'smooth' });
        }
      } catch {
        dots.remove(); mine.forEach(m => m.remove());
        ta.value = text; ta.dispatchEvent(new Event('input'));
        alertInline(composer, 'Нет связи с сервером. Сообщение не отправлено — попробуйте ещё раз.');
      } finally {
        sending = false; composer.classList.remove('is-waiting');
      }
    });
    addEventListener('popstate', () => location.reload());
  }

  // «Разбор удалён»: say it once, then take the mark out of the address
  const flash = document.querySelector('[data-flash]');
  if (flash) {
    history.replaceState(null, '', location.pathname);
    setTimeout(() => { flash.style.opacity = '0'; setTimeout(() => flash.remove(), 450); }, 4000);
  }

  // keep the conversation at its end after an answer
  if (location.hash === '#end') document.getElementById('end')?.scrollIntoView({ block: 'end' });
})();
