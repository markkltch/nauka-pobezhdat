/* Учение: the lesson player. One page runs the screens of a lesson (word, idea, example, exercises,
   «своими словами»), or a review, a смотр or the entrance review. Answers are checked here at once;
   first-try results go to the server, which keeps the repetition schedule. */
(() => {
  const root = document.querySelector('[data-player]');
  const dataEl = document.getElementById('uc-data');
  if (!root || !dataEl) return;
  const P = JSON.parse(dataEl.textContent);
  const mode = P.mode;
  const $ = (s, r = root) => r.querySelector(s);
  const stageEl = $('[data-stage]');
  const actIn = $('[data-act-in]');
  const act = $('[data-act]');
  const prog = $('[data-prog]');
  const scoreEl = $('[data-score] b');
  const kicker = $('[data-kicker]');
  const started = Date.now();

  const SVG = {
    check: '<path d="M3.5 8.5l3 3 6-7"/>', close: '<path d="M4 4l8 8M12 4l-8 8"/>',
    up: '<path d="M4 10l4-4 4 4"/>', down: '<path d="M4 6l4 4 4-4"/>', book: '<path d="M2.5 3.5h4A1.5 1.5 0 0 1 8 5v8.5A1.5 1.5 0 0 0 6.5 12h-4zM13.5 3.5h-4A1.5 1.5 0 0 0 8 5v8.5A1.5 1.5 0 0 1 9.5 12h4z"/>',
  };
  const icon = (name) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 16 16'); s.setAttribute('aria-hidden', 'true'); s.innerHTML = SVG[name]; return s; };
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) n.append(k.nodeType ? k : document.createTextNode(k));
    return n;
  }
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), credentials: 'same-origin' })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });

  // ---- the queue of screens
  const L = P.lesson;
  const screens = [];
  if (mode === 'lesson') {
    screens.push({ kind: 'quote', seg: 0 }, { kind: 'idea', seg: 1 });
    if (L.example) screens.push({ kind: 'example', seg: 2 });
  }
  P.items.forEach((it) => screens.push({ kind: 'item', item: it, seg: 3 }));
  if (mode === 'lesson' && L.words) screens.push({ kind: 'words', seg: 4 });
  const itemCount = P.items.length;
  const results = {};
  const picks = {};
  const requeued = new Set();
  let pos = 0;
  let right = 0;

  // ---- progress
  const segs = mode === 'lesson' ? 6 : itemCount;
  prog.style.gridTemplateColumns = `repeat(${segs}, 1fr)`;
  for (let i = 0; i < segs; i++) prog.append(el('i'));
  function paint() {
    const bars = prog.children;
    if (mode === 'lesson') {
      const cur = screens[pos] ? screens[pos].seg : 5;
      const doneItems = Object.keys(results).length;
      for (let i = 0; i < 6; i++) {
        bars[i].className = i < cur ? 'on' : (i === cur && cur === 3 && doneItems ? 'half' : (i === cur ? 'half' : ''));
      }
      if (cur === 3 && doneItems >= itemCount) bars[3].className = 'on';
      prog.setAttribute('aria-valuenow', String(Math.round((cur / 6) * 100)));
    } else {
      const doneItems = Object.keys(mode === 'diag' ? picks : results).length;
      for (let i = 0; i < segs; i++) bars[i].className = i < doneItems ? 'on' : (i === doneItems ? 'half' : '');
      prog.setAttribute('aria-valuenow', String(Math.round((doneItems / Math.max(1, segs)) * 100)));
    }
    scoreEl.textContent = mode === 'diag' ? `${Object.keys(picks).length} из ${itemCount}` : String(right);
  }

  // ---- the action bar
  let primary = null;
  function bar({ hint = '', label = 'Дальше', onClick, disabled = false, tone = '', fb = null, extra = null }) {
    act.className = 'uc-act' + (tone ? ` uc-act--${tone}` : '');
    actIn.replaceChildren();
    if (fb) {
      const box = el('div', { class: 'uc-fb' },
        el('span', { class: 'uc-fb__ic' }, icon(tone === 'no' ? 'close' : 'check')),
        el('div', {}, el('b', { text: fb.title }), fb.text ? el('p', { text: fb.text }) : null, fb.small ? el('small', { text: fb.small }) : null));
      actIn.append(box);
    } else if (hint) actIn.append(el('span', { class: 'uc-act__hint', text: hint }));
    if (extra) actIn.append(extra);
    primary = el('button', { type: 'button', class: 'btn btn--primary', onclick: onClick, text: label });
    primary.disabled = disabled;
    if (extra) primary.style.marginLeft = '0';
    actIn.append(primary);
  }

  function next() {
    pos += 1;
    if (pos >= screens.length) return finish();
    render();
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    const h = stageEl.querySelector('h1, h2, blockquote');
    if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
  }

  // ---- screens
  function render() {
    paint();
    const s = screens[pos];
    stageEl.replaceChildren();
    if (s.kind === 'quote') {
      setKicker('Слово Суворова');
      stageEl.append(el('blockquote', { class: 'uc-quote' }, el('p', { text: `«${L.quote_text.replace(/[.!]$/, '')}»` }), el('cite', { text: L.quote_source })));
      bar({ label: 'Дальше', onClick: next, hint: 'Урок: слово, суть, пример, упражнения, приказ' });
    } else if (s.kind === 'idea') {
      setKicker('Суть');
      stageEl.append(el('section', { class: 'uc-idea' },
        el('h2', { class: 'uc-h2', text: L.idea.title }),
        el('ol', {}, L.idea.points.map((p) => el('li', { text: p }))),
        el('span', { class: 'uc-src' }, icon('book'), L.source)));
      bar({ label: 'Дальше', onClick: next });
    } else if (s.kind === 'example') {
      setKicker('Пример');
      const ex = L.example;
      stageEl.append(el('h2', { class: 'uc-label', text: 'Одна и та же мысль, два способа' }),
        el('div', { class: 'uc-pair' },
          el('div', { class: 'uc-ex uc-ex--bad' }, el('span', { class: 'uc-label', style: 'color: #7e0f24', text: 'Так не работает' }), el('span', { text: ex.bad }),
            ex.bad_note ? el('span', { style: 'font-size: 14px; color: #7e0f24', text: ex.bad_note }) : null),
          el('div', { class: 'uc-ex uc-ex--good' }, el('span', { class: 'uc-label', style: 'color: #23452f', text: 'Так работает' }), el('span', { text: ex.good }))));
      bar({ label: 'К упражнениям', onClick: next });
    } else if (s.kind === 'item') {
      renderItem(s.item);
    } else if (s.kind === 'words') {
      renderWords();
    }
  }
  function setKicker(t) {
    if (mode !== 'lesson') return;
    kicker.replaceChildren(el('b', { text: t }), `· ${L.title}`);
  }

  // ---- exercises
  const TYPE = { situation: 'Ситуация', better: 'Что лучше', fix: 'Найди ошибку', quote: 'Собери цитату', order: 'По порядку', match: 'Сопоставь', diag: 'Ситуация' };
  function itemNo(it) { return P.items.indexOf(it) + 1; }
  function renderItem(it) {
    setKicker(`${TYPE[it.type]} · ${itemNo(it)} из ${itemCount}`);
    if (mode !== 'lesson') kicker.replaceChildren(el('b', { text: TYPE[it.type] }), `· ${Math.min(Object.keys(mode === 'diag' ? picks : results).length + 1, itemCount)} из ${itemCount}`);
    ({ situation: options, better: options, diag: options, fix, quote, order, match })[it.type](it);
  }

  function options(it) {
    if (it.scene) stageEl.append(el('p', { class: 'uc-scene', text: it.scene }));
    stageEl.append(el('h1', { class: 'uc-q', text: it.q }));
    const group = el('div', { class: 'uc-opts', role: 'radiogroup', 'aria-label': 'Варианты ответа' });
    let pick = null;
    const btns = it.options.map((t, i) => el('button', { type: 'button', class: 'uc-opt', role: 'radio', 'aria-checked': 'false',
      onclick: () => { pick = i; btns.forEach((b, j) => { b.classList.toggle('is-sel', j === i); b.setAttribute('aria-checked', String(j === i)); }); primary.disabled = false; } },
      el('span', { class: 'uc-opt__k', text: String(i + 1) }), el('span', { text: t })));
    group.append(...btns);
    stageEl.append(group);
    stageEl._keys = (n) => { if (btns[n - 1] && !btns[n - 1].disabled) btns[n - 1].click(); };
    if (it.type === 'diag') {
      bar({ hint: 'Правильного ответа нет: выбирайте, как сделали бы на самом деле', label: 'Дальше', disabled: true,
        onClick: () => { picks[it.id] = pick; next(); } });
      return;
    }
    bar({ hint: 'Выберите вариант', label: 'Проверить', disabled: true, onClick: () => {
      const ok = pick === it.answer;
      btns.forEach((b, j) => { b.disabled = true; b.classList.remove('is-sel'); if (j === it.answer) b.classList.add('is-right'); else if (j === pick) b.classList.add('is-wrong'); else b.classList.add('is-dim'); });
      const why = (it.why || [])[pick] || '';
      verdict(it, ok, ok ? why : `${why} Верно: «${it.options[it.answer]}».`, pick);
    } });
  }

  function fix(it) {
    stageEl.append(el('h1', { class: 'uc-q', text: it.q }));
    const text = el('p', { class: 'uc-text' });
    const btns = [];
    it.parts.forEach((p) => {
      if (!p.tap) { text.append(p.t); return; }
      const b = el('button', { type: 'button', class: 'uc-frag', 'aria-pressed': 'false', text: p.t,
        onclick: () => { const on = b.getAttribute('aria-pressed') !== 'true'; b.setAttribute('aria-pressed', String(on)); b.classList.toggle('is-sel', on); count(); } });
      b._err = !!p.err; btns.push(b); text.append(b);
    });
    stageEl.append(text);
    const need = it.parts.filter((p) => p.err).length;
    const count = () => { const n = btns.filter((b) => b.getAttribute('aria-pressed') === 'true').length; actIn.querySelector('.uc-act__hint').textContent = `Отмечено ${n} из ${need}`; primary.disabled = n === 0; };
    bar({ hint: `Отмечено 0 из ${need}`, label: 'Проверить', disabled: true, onClick: () => {
      let ok = true;
      btns.forEach((b) => { const on = b.getAttribute('aria-pressed') === 'true'; if (on !== b._err) ok = false; b.disabled = true; b.classList.remove('is-sel'); if (b._err) b.classList.add('is-right'); });
      if (it.fixed) stageEl.append(el('div', { class: 'uc-mentor' }, el('span', { class: 'av av--red', 'aria-hidden': 'true', style: 'width:36px;height:36px' }, el('span', { class: 'mark', 'data-mark': 'arc' })),
        el('div', {}, el('p', {}, el('b', { style: 'font-weight:500', text: 'Как сказать иначе: ' }), it.fixed))));
      verdict(it, ok, it.explain || '');
    } });
  }

  function quote(it) {
    stageEl.append(el('h1', { class: 'uc-q', text: 'Соберите слова Суворова' }));
    const line = el('div', { class: 'uc-line', 'data-empty': 'Нажимайте на части по порядку', 'aria-label': 'Собранная фраза' });
    const bank = el('div', { class: 'uc-bank', 'aria-label': 'Части фразы' });
    const chosen = [];
    const sync = () => { primary.disabled = chosen.length !== it.chunks.length; };
    it.shuffled.forEach((t) => {
      const b = el('button', { type: 'button', class: 'uc-chunk', text: t, onclick: () => {
        b.disabled = true; chosen.push(t);
        const c = el('button', { type: 'button', class: 'uc-chunk', text: t, onclick: () => { c.remove(); chosen.splice(chosen.indexOf(t), 1); b.disabled = false; sync(); } });
        line.append(c); sync();
      } });
      bank.append(b);
    });
    stageEl.append(line, bank);
    bar({ hint: 'Части можно вернуть нажатием', label: 'Проверить', disabled: true, onClick: () => {
      const ok = chosen.join(' ') === it.chunks.join(' ');
      line.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      bank.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      verdict(it, ok, ok ? `«${it.text}»` : `Верно: «${it.text}»`);
    } });
  }

  function order(it) {
    stageEl.append(el('h1', { class: 'uc-q', text: it.q }));
    const list = el('ol', { class: 'uc-ordlist' });
    let cur = it.shuffled.slice();
    const draw = () => {
      list.replaceChildren(...cur.map((t, i) => el('li', {}, el('b', { text: String(i + 1) }), el('span', { text: t }),
        el('button', { type: 'button', 'aria-label': `Выше: ${t}`, disabled: i === 0, onclick: () => { [cur[i - 1], cur[i]] = [cur[i], cur[i - 1]]; draw(); list.querySelectorAll('button')[Math.max(0, (i - 1) * 2)].focus(); } }, icon('up')),
        el('button', { type: 'button', 'aria-label': `Ниже: ${t}`, disabled: i === cur.length - 1, onclick: () => { [cur[i + 1], cur[i]] = [cur[i], cur[i + 1]]; draw(); const b = list.querySelectorAll('button')[(i + 1) * 2 + 1]; if (b) b.focus(); } }, icon('down')))));
    };
    draw();
    stageEl.append(list);
    bar({ hint: 'Меняйте порядок стрелками', label: 'Проверить', onClick: () => {
      const ok = cur.join('|') === it.steps.join('|');
      cur = it.steps.slice(); draw(); list.classList.add('is-right');
      list.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      verdict(it, ok, ok ? 'Порядок верный.' : 'Верный порядок — на экране.');
    } });
  }

  function match(it) {
    stageEl.append(el('h1', { class: 'uc-q', text: it.q }));
    const rows = it.pairs.map(([left], i) => {
      const sel = el('select', { 'aria-label': `Пара для: ${left}`, onchange: () => { primary.disabled = selects.some((s) => !s.value); } },
        el('option', { value: '', text: 'Выберите…' }), it.rights.map((r) => el('option', { value: r, text: r })));
      const row = el('div', { class: 'uc-matchrow' }, el('span', { text: left }), sel);
      row._sel = sel; row._right = it.pairs[i][1];
      return row;
    });
    const selects = rows.map((r) => r._sel);
    stageEl.append(el('div', { style: 'display:grid;gap:8px' }, rows));
    bar({ hint: 'Для каждого правила — свой пример', label: 'Проверить', disabled: true, onClick: () => {
      let ok = true;
      rows.forEach((r) => { const good = r._sel.value === r._right; if (!good) ok = false; r.classList.add(good ? 'is-right' : 'is-wrong'); r._sel.disabled = true;
        if (!good) r.append(el('span', { style: 'grid-column:1/-1;font-size:14px;color:#23452f', text: `Верно: ${r._right}` })); });
      verdict(it, ok, ok ? 'Все пары верны.' : 'Посмотрите верные пары на экране.');
    } });
  }

  function verdict(it, ok, text, pick) {
    const first = !(it.id in results);
    if (first) { results[it.id] = { ok, pick: typeof pick === 'number' ? pick : null }; if (ok) right += 1; }
    paint();
    const again = !ok && mode !== 'test' && !requeued.has(it.id);
    if (again) { requeued.add(it.id); const w = screens.findIndex((x, i) => i > pos && x.kind !== 'item'); screens.splice(w === -1 ? screens.length : w, 0, { kind: 'item', item: it, seg: 3 }); }
    bar({ tone: ok ? 'ok' : 'no', label: ok ? 'Дальше' : 'Понятно', onClick: next,
      fb: { title: ok ? 'Верно' : 'Не совсем', text, small: again ? 'Вернёмся к этому в конце. Опыт не снимается.' : (it.source || '') } });
    primary.focus();
  }

  // ---- своими словами
  function renderWords() {
    setKicker('Своими словами');
    const W = L.words;
    const ta = el('textarea', { id: 'uc-say', name: 'say', rows: '4', spellcheck: 'true', maxlength: '600', placeholder: 'Одна-две фразы' });
    stageEl.append(el('h1', { class: 'uc-q', text: W.prompt }),
      el('div', { class: 'uc-write' }, el('label', { for: 'uc-say', class: 'uc-label', text: 'Ваш ответ' }), ta,
        el('div', { class: 'uc-write__bar' }, el('span', { text: 'Ответ видите только вы. Глава штаба его не видит.' }))));
    const skip = el('button', { type: 'button', class: 'btn btn--secondary', style: 'height:52px', text: 'Пропустить', onclick: () => finish() });
    bar({ label: 'Проверить', disabled: true, extra: skip, onClick: () => {
      primary.disabled = true; primary.textContent = 'Наставник читает…';
      post(P.words_url, { lesson: L.id, text: ta.value }).then((fb) => showWords(fb, ta)).catch(() => showWords({ model: W.model }, ta));
    } });
    ta.addEventListener('input', () => { primary.disabled = ta.value.trim().length < 10; });
  }
  function showWords(fb, ta) {
    ta.readOnly = true;
    const box = el('div', { class: 'uc-mentor', role: 'status' }, el('span', { class: 'av av--red', 'aria-hidden': 'true', style: 'width:36px;height:36px' }, el('span', { class: 'mark', 'data-mark': 'arc' })),
      el('div', {},
        fb.ai ? el('p', {}, el('b', { style: 'font-weight:500', text: 'Отзыв наставника. ' }), fb.good || '') : el('p', { text: 'Сравните свой ответ с эталоном: схвачена ли суть?' }),
        fb.ai && fb.add ? el('ul', {}, el('li', { text: fb.add })) : null,
        el('p', { style: 'margin-top:10px;font-size:15px' }, el('b', { style: 'font-weight:500', text: 'Эталон: ' }), `«${fb.model || L.words.model}»`)));
    stageEl.append(box);
    bar({ tone: 'ok', label: 'К приказу', onClick: finish, fb: { title: 'Засчитано', text: 'Остался последний шаг: применить правило в своём деле.' } });
    primary.focus();
  }

  // ---- finish
  let finishing = false;
  function finish() {
    if (finishing) return; finishing = true;
    const seconds = Math.round((Date.now() - started) / 1000);
    const body = mode === 'diag' ? { picks } : { results, seconds };
    if (primary) { primary.disabled = true; primary.textContent = 'Сохраняю…'; }
    post(P.finish, body).then((r) => {
      if (mode === 'lesson') {
        $('[data-body]').hidden = true; act.hidden = true;
        const form = $('[data-order]'); form.hidden = false;
        [...prog.children].forEach((b, i) => { b.className = i < 5 ? 'on' : 'half'; });
        window.scrollTo(0, 0); const h = form.querySelector('h1'); h.setAttribute('tabindex', '-1'); h.focus();
      } else {
        window.location.href = r.next || P.close;
      }
    }).catch(() => {
      finishing = false;
      bar({ tone: 'no', label: 'Повторить', onClick: finish, fb: { title: 'Не сохранилось', text: 'Связь прервалась. Нажмите ещё раз — ответы не потеряются.' } });
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.target.closest('textarea, input, select')) return;
    if (/^[1-9]$/.test(e.key) && stageEl._keys) stageEl._keys(Number(e.key));
    else if (e.key === 'Enter' && primary && !primary.disabled && document.activeElement === document.body) { e.preventDefault(); primary.click(); }
  });

  if (!screens.length) { finish(); return; }
  render();
})();
