const $ = (s) => document.querySelector(s);
const status = $('#upload-status');

let me = { authenticated: false, username: null };

document.querySelectorAll('.tab').forEach((t) => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('form-' + t.dataset.tab).classList.add('active');
  });
});

function setStatus(msg, ok) {
  status.textContent = msg;
  status.className = 'status ' + (ok === true ? 'ok' : ok === false ? 'err' : '');
}

async function handle(promise, okMsg) {
  setStatus('Procesando...');
  try {
    const r = await promise;
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Error');
    const note = data.published
      ? `${okMsg} y publicado (${data.chunks} fragmentos)`
      : `${okMsg}. Pendiente de aprobación por un administrador (${data.chunks} fragmentos).`;
    setStatus(note, true);
    if (me.authenticated) loadDocs();
  } catch (e) {
    setStatus('Error: ' + e.message, false);
  }
}

$('#form-file').addEventListener('submit', (e) => {
  e.preventDefault();
  handle(fetch('/api/upload', { method: 'POST', body: new FormData(e.target), credentials: 'include' }), 'Archivo recibido');
});

$('#form-url').addEventListener('submit', (e) => {
  e.preventDefault();
  handle(
    fetch('/api/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ url: e.target.url.value }),
    }),
    'Página recibida'
  );
});

$('#form-text').addEventListener('submit', (e) => {
  e.preventDefault();
  handle(
    fetch('/api/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ title: e.target.title.value, text: e.target.text.value }),
    }),
    'Nota recibida'
  );
});

$('#form-search').addEventListener('submit', async (e) => {
  e.preventDefault();
  const query = e.target.query.value;
  const box = $('#results');
  box.innerHTML = '<p>Buscando...</p>';
  try {
    const r = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, k: 5 }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Error');
    if (!data.results.length) {
      box.innerHTML = '<p>Sin resultados publicados.</p>';
      return;
    }
    const banner = data.generated
      ? '<p class="hint">Resultado generado por IA y agregado a la base.</p>'
      : '';
    box.innerHTML =
      banner +
      data.results
        .map(
          (x) => `
      <div class="result">
        <div class="meta">
          <span><strong>${escapeHtml(x.source_name)}</strong> · ${x.source_type}</span>
          <span>similitud: ${(x.similarity * 100).toFixed(1)}%</span>
        </div>
        <div class="content">${escapeHtml(x.content)}</div>
      </div>`
        )
        .join('');
  } catch (e) {
    box.innerHTML = `<p style="color:#b00020">${e.message}</p>`;
  }
});

// ---------- Auth ----------
async function refreshAuth() {
  const r = await fetch('/api/admin/me', { credentials: 'include' });
  me = await r.json();
  const state = $('#auth-state');
  const btn = $('#auth-btn');
  const adminSection = $('#admin-section');
  if (me.authenticated) {
    state.textContent = `Conectado como ${me.username}`;
    btn.textContent = 'Cerrar sesión';
    adminSection.hidden = false;
    loadDocs();
  } else {
    state.textContent = '';
    btn.textContent = 'Acceso administrador';
    adminSection.hidden = true;
  }
}

$('#auth-btn').addEventListener('click', async () => {
  if (me.authenticated) {
    await fetch('/api/admin/logout', { method: 'POST', credentials: 'include' });
    refreshAuth();
  } else {
    $('#login-modal').hidden = false;
  }
});

$('#login-cancel').addEventListener('click', () => {
  $('#login-modal').hidden = true;
  $('#login-error').textContent = '';
});

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#login-error');
  err.textContent = '';
  try {
    const r = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username: e.target.username.value, password: e.target.password.value }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Error');
    $('#login-modal').hidden = true;
    e.target.reset();
    refreshAuth();
  } catch (ex) {
    err.textContent = ex.message;
  }
});

// ---------- Docs (admin) ----------
let allDocs = [];

async function loadDocs() {
  const r = await fetch('/api/documents', { credentials: 'include' });
  if (r.status === 401) { me = { authenticated: false }; refreshAuth(); return; }
  const data = await r.json();
  allDocs = data.documents;
  renderDocs();
}

function renderDocs() {
  const ul = $('#docs');
  const summary = $('#docs-summary');
  const filter = ($('#docs-filter').value || '').toLowerCase();
  const statusF = $('#docs-status').value;
  const docs = allDocs.filter((d) => {
    if (statusF === 'pending' && d.published) return false;
    if (statusF === 'published' && !d.published) return false;
    if (filter && !(d.source_name.toLowerCase().includes(filter) || d.source_type.toLowerCase().includes(filter))) return false;
    return true;
  });
  const pending = allDocs.filter((d) => !d.published).length;
  const published = allDocs.length - pending;
  summary.textContent = allDocs.length
    ? `${published} publicado(s) · ${pending} pendiente(s) de aprobación · ${allDocs.length} total${filter || statusF !== 'all' ? ` · ${docs.length} mostrados` : ''}`
    : '';
  if (!allDocs.length) { ul.innerHTML = '<li><em>Aún no hay contenido cargado.</em></li>'; return; }
  if (!docs.length) { ul.innerHTML = '<li><em>Ningún documento coincide.</em></li>'; return; }
  ul.innerHTML = docs
    .map(
      (d) => `
    <li class="doc ${d.published ? 'published' : 'pending'}" data-id="${d.id}">
      <div class="doc-row">
        <div class="info">
          <strong>${escapeHtml(d.source_name)}</strong>
          <span class="tag tag-${d.source_type}">${d.source_type}</span>
          <span class="tag tag-${d.published ? 'published' : 'pending'}">${d.published ? 'publicado' : 'pendiente'}</span>
          <div class="doc-meta">${d.chunks} fragmento(s) · ${new Date(d.created_at).toLocaleString()}</div>
        </div>
        <div class="doc-actions">
          <button class="view" data-id="${d.id}">Ver</button>
          ${d.published
            ? `<button class="unpublish" data-id="${d.id}">Despublicar</button>`
            : `<button class="publish" data-id="${d.id}">Aprobar y publicar</button>`}
          <button class="delete" data-id="${d.id}">Eliminar</button>
        </div>
      </div>
    </li>`
    )
    .join('');

  ul.querySelectorAll('.delete').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('¿Eliminar este documento?')) return;
    await fetch('/api/documents/' + b.dataset.id, { method: 'DELETE', credentials: 'include' });
    loadDocs();
  }));
  ul.querySelectorAll('.publish').forEach((b) => b.addEventListener('click', async () => {
    await fetch('/api/documents/' + b.dataset.id + '/publish', { method: 'POST', credentials: 'include' });
    loadDocs();
  }));
  ul.querySelectorAll('.unpublish').forEach((b) => b.addEventListener('click', async () => {
    await fetch('/api/documents/' + b.dataset.id + '/unpublish', { method: 'POST', credentials: 'include' });
    loadDocs();
  }));
  ul.querySelectorAll('.view').forEach((b) => b.addEventListener('click', () => toggleView(b)));
}

async function toggleView(btn) {
  const li = btn.closest('.doc');
  const existing = li.querySelector('.doc-content');
  if (existing) { existing.remove(); btn.textContent = 'Ver'; return; }
  btn.textContent = 'Cargando...'; btn.disabled = true;
  try {
    const r = await fetch('/api/documents/' + btn.dataset.id, { credentials: 'include' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Error');
    const div = document.createElement('div');
    div.className = 'doc-content';
    div.innerHTML = data.chunks
      .map((c) => `<div class="chunk"><div class="chunk-label">Fragmento ${c.chunk_index + 1}</div><div>${escapeHtml(c.content)}</div></div>`)
      .join('') || '<em>Sin contenido.</em>';
    li.appendChild(div);
    btn.textContent = 'Ocultar';
  } catch (e) {
    alert('Error: ' + e.message);
    btn.textContent = 'Ver';
  } finally { btn.disabled = false; }
}

$('#refresh-docs').addEventListener('click', loadDocs);
$('#docs-filter').addEventListener('input', renderDocs);
$('#docs-status').addEventListener('change', renderDocs);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

refreshAuth();
