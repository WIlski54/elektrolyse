const socket = io();
const progress = new Set();
const chatState = { requestId: null, approved: false, history: [] };
const animations = {};

function qs(selector, root = document) {
  return root.querySelector(selector);
}

function qsa(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[char]);
}

function renderMessage(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
}

function typesetMath(root) {
  if (window.MathJax?.typesetPromise) {
    window.MathJax.typesetPromise([root]).catch(() => {});
  }
}

function updateProgress() {
  const total = window.APP_CONFIG.totalTasks;
  const done = new Set(Array.from(progress).map(item => item.split(':')[0])).size;
  qs('#progress-text').textContent = `${done}/${total} Aufgaben`;
  qs('#progress-fill').style.width = `${Math.round((done / total) * 100)}%`;
}

async function loadProgress() {
  const res = await fetch('/api/progress');
  if (!res.ok) return;
  const data = await res.json();
  data.items.forEach(item => progress.add(`${item.aufgabe_nr}:${item.niveau}`));
  updateProgress();
}

function setupTabs() {
  qsa('.tab-btn').forEach(button => {
    button.addEventListener('click', () => {
      qsa('.tab-btn').forEach(btn => btn.classList.remove('active'));
      qsa('.tab-panel').forEach(panel => panel.classList.remove('active'));
      button.classList.add('active');
      qs(`#${button.dataset.tabTarget}`).classList.add('active');
    });
  });
}

function setupNiveaus() {
  qsa('.niveau-select').forEach(select => {
    updateAnswerSurfaces(select.closest('.task-card'));
    select.addEventListener('change', () => {
      const card = select.closest('.task-card');
      qsa('[data-n]', card).forEach(block => {
        block.classList.toggle('active', block.dataset.n === select.value);
      });
      updateAnswerSurfaces(card);
    });
  });
}

function updateAnswerSurfaces(card) {
  const select = qs('.niveau-select', card);
  if (!select) return;
  qsa('[data-answer-surface]', card).forEach(surface => {
    const levels = (surface.dataset.showFor || '').split(',').map(item => item.trim());
    surface.classList.toggle('active', levels.includes(select.value));
  });
}

function taskQuestion(card) {
  const title = qs('h3', card)?.textContent || '';
  const active = qs('.niveau-content.active', card)?.textContent || '';
  return `${title}: ${active.trim()}`;
}

function getStructuredAnswer(card) {
  const rows = qsa('.comparison-cell', card).map(cell => {
    const entries = qsa('textarea', cell)
      .map(input => `${input.dataset.field}: ${input.value.trim()}`)
      .filter(line => !line.endsWith(':'));
    if (!entries.length) return '';
    return `${cell.dataset.aspect} - ${cell.dataset.system}: ${entries.join(' | ')}`;
  }).filter(Boolean);
  return rows.join('\n');
}

function getAnswer(card) {
  const structured = qs('.structured-answer.active', card);
  if (structured) {
    return getStructuredAnswer(card).trim();
  }
  return (qs('.answer-input.active, .answer-input:not([data-answer-surface])', card)?.value || '').trim();
}

async function saveAnswer(card) {
  const task = card.dataset.taskCard;
  const niveau = qs('.niveau-select', card).value;
  const answer = getAnswer(card);
  const feedback = qs('.feedback-box', card);
  if (!answer) {
    feedback.textContent = 'Bitte schreibe zuerst eine Antwort auf.';
    feedback.classList.add('active');
    return;
  }
  const res = await fetch('/api/answer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({task, niveau, answer})
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    feedback.textContent = data.error || 'Die Antwort konnte nicht gespeichert werden.';
    feedback.classList.add('active');
    return;
  }
  progress.add(`${task}:${niveau}`);
  updateProgress();
  feedback.textContent = 'Gespeichert. Dein Lernstand ist im Lehrer-Dashboard sichtbar.';
  feedback.classList.add('active');
}

async function checkAnswer(card) {
  const answer = getAnswer(card);
  const feedback = qs('.feedback-box', card);
  if (!answer) {
    feedback.textContent = 'Schreibe zuerst eine Antwort, dann kann der KI-Tutor Rueckmeldung geben.';
    feedback.classList.add('active');
    return;
  }
  feedback.textContent = 'Rueckmeldung wird vorbereitet...';
  feedback.classList.add('active');
  const res = await fetch('/api/check-answer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      question: taskQuestion(card),
      answer,
      context: 'Chemie: forschendes Arbeitsblatt zur Elektrolyse, galvanische Zelle im Vergleich.'
    })
  });
  const data = await res.json();
  feedback.innerHTML = `<strong>${escapeHtml(data.feedback || 'Rueckmeldung')}</strong>${data.hint ? `<br>${escapeHtml(data.hint)}` : ''}`;
}

function setupTasks() {
  qsa('.save-answer').forEach(button => {
    button.addEventListener('click', () => saveAnswer(button.closest('.task-card')));
  });
  qsa('.check-answer').forEach(button => {
    button.addEventListener('click', () => checkAnswer(button.closest('.task-card')));
  });
  qsa('[data-choice-group] button').forEach(button => {
    button.addEventListener('click', () => {
      const group = button.closest('[data-choice-group]');
      qsa('button', group).forEach(btn => btn.classList.remove('selected', 'correct', 'incorrect'));
      button.classList.add('selected', button.dataset.correct === 'true' ? 'correct' : 'incorrect');
    });
  });
  qsa('.app-chip').forEach(button => {
    button.addEventListener('click', () => {
      qsa('.app-chip').forEach(chip => chip.classList.remove('selected'));
      button.classList.add('selected');
    });
  });
}

function makeSvg(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
  return el;
}

function populateParticles() {
  const galvanic = qs('.galvanic-particles');
  const electrolysis = qs('.electrolysis-particles');
  const bubbles = qs('.gas-bubbles');

  function appendIon(parent, cx, cy, text, cls, radius = 17) {
    const ion = makeSvg('g', {class: cls});
    ion.append(makeSvg('circle', {cx, cy, r: radius}));
    const label = makeSvg('text', {x: cx, y: cy + 5, class: 'galvanic-label'});
    label.textContent = text;
    ion.append(label);
    parent.append(ion);
    return ion;
  }

  if (galvanic) {
    [
      [92, 224], [216, 192], [235, 279]
    ].forEach(([cx, cy]) => appendIon(galvanic, cx, cy, 'SO₄²⁻', 'so4-ion so4-mobile'));
    [
      [208, 180], [95, 287]
    ].forEach(([cx, cy]) => appendIon(galvanic, cx, cy, 'Zn²⁺', 'zn2-ion zn2-background'));
    [
      [410, 188], [540, 205], [516, 278], [382, 280]
    ].forEach(([cx, cy], i) => appendIon(galvanic, cx, cy, 'Cu²⁺', `cu2-ion cu2-mobile cu2-${i}`));

    appendIon(galvanic, 156, 232, 'Zn', 'zn-atom zn-surface-atom', 16);
    appendIon(galvanic, 156, 196, 'Zn', 'zn-atom zn-surface-atom alt', 16);
    appendIon(galvanic, 196, 225, 'Zn²⁺', 'zn2-ion zn2-product', 17);
    appendIon(galvanic, 456, 230, 'Cu²⁺', 'cu2-ion cu2-reactant', 17);
    appendIon(galvanic, 472, 236, 'Cu', 'cu-atom cu-product', 18);

    galvanic.append(makeSvg('circle', {cx: 156, cy: 230, r: 27, class: 'reaction-flash zn-flash'}));
    galvanic.append(makeSvg('circle', {cx: 472, cy: 230, r: 27, class: 'reaction-flash cu-flash'}));

    for (let i = 0; i < 8; i += 1) {
      galvanic.append(makeSvg('circle', {cx: 0, cy: 0, r: 6, class: `electron-dot galvanic-electron electron-${i}`}));
    }
  }

  if (electrolysis && bubbles) {
    [
      [150, 182, '+', 'ion-plus electro-cation'],
      [170, 215, '+', 'ion-plus electro-cation'],
      [410, 180, '-', 'ion-minus electro-anion'],
      [390, 214, '-', 'ion-minus electro-anion'],
      [265, 184, '+', 'ion-plus electro-cation'],
      [295, 214, '-', 'ion-minus electro-anion']
    ].forEach(([cx, cy, label, cls]) => {
      const ion = makeSvg('g', {class: `ion ${cls}`});
      ion.append(makeSvg('circle', {cx, cy, r: 12}));
      const text = makeSvg('text', {x: cx - 4, y: cy + 5, class: 'svg-small'});
      text.textContent = label;
      ion.append(text);
      electrolysis.append(ion);
    });
    [[235, 57], [280, 45], [325, 57]].forEach(([cx, cy]) => {
      electrolysis.append(makeSvg('circle', {cx, cy, r: 6, class: 'electron-dot electro-electron'}));
    });
    [[198, 130], [363, 125], [195, 111], [368, 106]].forEach(([cx, cy]) => {
      bubbles.append(makeSvg('circle', {cx, cy, r: 8, class: 'bubble'}));
    });
  }
}

function buildAnimations() {
  if (!qs('.electron-flow') && !qs('.electrolysis-particles')) {
    return;
  }
  if (!qs('.electron-flow')) {
    animations.galvanic = {play(){}, pause(){}, seek(){}};
  } else {
  const electronPath = anime.path('.electron-flow');

  anime.set('.galvanic-electron', {
    translateX: electronPath('x'),
    translateY: electronPath('y'),
    opacity: 0
  });
  anime.set('.zn2-product, .cu-product', {opacity: 0, scale: 0.5, transformOrigin: 'center'});
  anime.set('.cu2-reactant', {opacity: 0, scale: 0.8, transformOrigin: 'center'});
  anime.set('.cu-growth-layer', {scaleX: 0.18, transformOrigin: 'center bottom'});

  animations.galvanic = anime.timeline({autoplay: false, loop: true})
    .add({
      targets: '.zn-flash',
      opacity: [0, .9, 0],
      scale: [0.4, 1.25],
      easing: 'easeOutSine',
      duration: 900
    }, 0)
    .add({
      targets: '.zn-surface-atom',
      translateX: [0, 28],
      translateY: [0, 20],
      opacity: [1, 0],
      scale: [1, .6],
      easing: 'easeInOutSine',
      duration: 1200
    }, 120)
    .add({
      targets: '.zn2-product',
      translateX: [0, 42],
      translateY: [0, 28],
      opacity: [0, 1, 1, 0],
      scale: [.6, 1, 1, .85],
      easing: 'easeInOutSine',
      duration: 2200
    }, 260)
    .add({
      targets: '.zn-electrode-core',
      width: [48, 34],
      x: [132, 139],
      easing: 'easeInOutSine',
      duration: 4200
    }, 0)
    .add({
      targets: '.galvanic-electron',
      opacity: [
        {value: 1, duration: 160},
        {value: 1, duration: 1800},
        {value: 0, duration: 260}
      ],
      translateX: electronPath('x'),
      translateY: electronPath('y'),
      easing: 'linear',
      duration: 2500,
      delay: anime.stagger(210)
    }, 350)
    .add({
      targets: '.electron-flow',
      strokeDashoffset: [100, 0],
      easing: 'linear',
      duration: 2200
    }, 350)
    .add({
      targets: '.cu2-mobile',
      translateX: (el, i) => [-10 - i * 18, -76 - i * 8],
      translateY: (el, i) => [0, i % 2 === 0 ? 28 : -16],
      easing: 'easeInOutSine',
      duration: 2300,
      delay: anime.stagger(120)
    }, 900)
    .add({
      targets: '.cu2-reactant',
      opacity: [0, 1, 1, 0],
      translateX: [42, 0],
      translateY: [-22, 0],
      scale: [.8, 1, 1, .55],
      easing: 'easeInOutSine',
      duration: 1700
    }, 1500)
    .add({
      targets: '.cu-flash',
      opacity: [0, .85, 0],
      scale: [0.5, 1.22],
      easing: 'easeOutSine',
      duration: 900
    }, 2300)
    .add({
      targets: '.cu-product',
      opacity: [0, 1, 1],
      translateX: [18, 0],
      scale: [.55, 1],
      easing: 'easeOutBack',
      duration: 900
    }, 2350)
    .add({
      targets: '.cu-growth-layer',
      scaleX: [0.18, 1.15],
      easing: 'easeInOutSine',
      duration: 4200
    }, 400)
    .add({
      targets: '.so4-mobile',
      translateX: (el, i) => [0, -35 - i * 12],
      translateY: (el, i) => [0, i === 1 ? 16 : -12],
      easing: 'easeInOutSine',
      duration: 2800,
      delay: anime.stagger(160)
    }, 850)
    .add({
      targets: '.ion-guide',
      strokeDashoffset: [40, 0],
      easing: 'linear',
      duration: 1800
    }, 850)
    .add({
      targets: '.zn-electrode-core, .cu-growth-layer, .zn-surface-atom, .zn2-product, .cu2-reactant, .cu-product, .cu2-mobile, .so4-mobile, .reaction-flash, .ion-guide, .electron-flow',
      duration: 900,
      easing: 'easeInOutSine'
    }, 4700);
  }

  if (!qs('.electrolysis-particles')) {
    animations.electrolysis = {play(){}, pause(){}, seek(){}};
    return;
  }
  animations.electrolysis = anime.timeline({autoplay: false, loop: true})
    .add({
      targets: '.electro-cation',
      translateX: (el, i) => i % 2 === 0 ? 185 : 165,
      translateY: (el, i) => i === 4 ? 32 : 0,
      easing: 'easeInOutSine',
      duration: 1400,
      delay: anime.stagger(90)
    })
    .add({
      targets: '.electro-anion',
      translateX: (el, i) => i % 2 === 0 ? -205 : -185,
      translateY: (el, i) => i === 2 ? -28 : 0,
      easing: 'easeInOutSine',
      duration: 1400,
      delay: anime.stagger(90)
    }, 0)
    .add({
      targets: '.electro-electron',
      translateX: -55,
      opacity: [.35, 1],
      easing: 'easeInOutSine',
      duration: 1000
    }, 0)
    .add({
      targets: '.bubble',
      translateY: -26,
      scale: [0.7, 1.25],
      opacity: [.2, .95, .2],
      easing: 'easeOutSine',
      duration: 1400,
      delay: anime.stagger(100)
    }, 0)
    .add({
      targets: '.electro-line',
      strokeDashoffset: [80, 0],
      easing: 'linear',
      duration: 900
    }, 0);
}

function setupAnimationControls() {
  qsa('[data-action]').forEach(button => {
    button.addEventListener('click', () => {
      const timeline = animations[button.dataset.for];
      if (!timeline) return;
      if (button.dataset.action === 'play') timeline.play();
      if (button.dataset.action === 'pause') timeline.pause();
      if (button.dataset.action === 'reset') {
        timeline.pause();
        timeline.seek(0);
      }
    });
  });
}

function addMessage(role, text) {
  const box = qs('#chat-messages');
  const item = document.createElement('div');
  item.className = `message ${role}`;
  item.innerHTML = renderMessage(text);
  box.append(item);
  typesetMath(item);
  box.scrollTop = box.scrollHeight;
}

function setChatApproved(requestId) {
  chatState.requestId = requestId || chatState.requestId;
  chatState.approved = true;
  qs('#chat-status').textContent = 'KI-Hilfe ist freigegeben.';
  qs('#chat-input').placeholder = 'Frage zur Elektrolyse stellen...';
  qs('#chat-form button').disabled = false;
  qs('#request-ki').disabled = true;
}

function setupChat() {
  const widget = qs('#chat-widget');
  qs('#chat-toggle').addEventListener('click', () => widget.classList.add('open'));
  qs('#chat-close').addEventListener('click', () => widget.classList.remove('open'));
  qs('#request-ki').addEventListener('click', async () => {
    const input = qs('#chat-input');
    const reason = input.value.trim() || 'Ich moechte die KI-Hilfe zur Elektrolyse nutzen.';
    const res = await fetch('/api/ki-anfrage', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason})
    });
    const data = await res.json();
    if (data.ok) {
      chatState.requestId = data.request_id;
      qs('#chat-status').textContent = 'Anfrage gesendet. Nach der Freigabe kannst du deine Frage senden.';
    }
  });
  qs('#chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = qs('#chat-input');
    const text = input.value.trim();
    if (!text || !chatState.approved) return;
    input.value = '';
    addMessage('user', text);
    chatState.history.push({role: 'user', content: text});
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, request_id: chatState.requestId, history: chatState.history})
    });
    const data = await res.json();
    if (!res.ok) {
      addMessage('assistant', data.error || 'Die KI-Hilfe ist noch nicht freigegeben.');
      return;
    }
    addMessage('assistant', data.answer);
    chatState.history.push({role: 'assistant', content: data.answer});
  });
}

socket.on('ki_decision', data => {
  if (data.status === 'genehmigt') {
    setChatApproved(data.request_id);
    addMessage('assistant', 'Ich bin jetzt freigegeben. Wobei soll ich dir beim Denken helfen?');
  } else {
    qs('#chat-status').textContent = 'Die KI-Hilfe wurde gerade nicht freigegeben.';
  }
});

socket.on('session_reset', data => {
  document.body.innerHTML = `
    <main class="login-shell">
      <section class="login-card">
        <p class="eyebrow">Sitzung beendet</p>
        <h1>Die Unterrichtssitzung wurde zurueckgesetzt.</h1>
        <p>${escapeHtml(data.message || 'Bitte melde dich neu an, wenn die Lehrkraft eine neue Runde startet.')}</p>
        <a class="btn-primary" href="/">Neu anmelden</a>
      </section>
    </main>`;
  document.body.className = 'login-page';
});

document.addEventListener('DOMContentLoaded', () => {
  setupTabs();
  setupNiveaus();
  setupTasks();
  setupChat();
  populateParticles();
  buildAnimations();
  setupAnimationControls();
  loadProgress();
});
