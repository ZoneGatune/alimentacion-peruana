const $ = (s) => document.querySelector(s);
const status = $('#upload-status');

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
    setStatus(`${okMsg} (${data.chunks} fragmentos)`, true);
    loadDocs();
  } catch (e) {
    setStatus('Error: ' + e.message, false);
  }
}

$('#form-file').addEventListener('submit', (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  handle(fetch('/api/upload', { method: 'POST', body: fd }), 'Archivo guardado');
});

$('#form-url').addEventListener('submit', (e) => {
  e.preventDefault();
  const url = e.target.url.value;
  handle(
    fetch('/api/url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    }),
    'Página guardada'
  );
});

$('#form-text').addEventListener('submit', (e) => {
  e.preventDefault();
  const body = { title: e.target.title.value, text: e.target.text.value };
  handle(
    fetch('/api/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
    'Nota guardada'
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
      box.innerHTML = '<p>Sin resultados.</p>';
      return;
    }
    box.innerHTML = data.results
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

let allDocs = [];

async function loadDocs() {
  const r = await fetch('/api/documents');
  const data = await r.json();
  allDocs = data.documents;
  renderDocs();
}

function renderDocs() {
  const ul = $('#docs');
  const summary = $('#docs-summary');
  const filter = ($('#docs-filter').value || '').toLowerCase();
  const docs = allDocs.filter(
    (d) => !filter || d.source_name.toLowerCase().includes(filter) || d.source_type.toLowerCase().includes(filter)
  );
  const totalChunks = allDocs.reduce((s, d) => s + d.chunks, 0);
  summary.textContent = allDocs.length
    ? `${allDocs.length} documento(s) · ${totalChunks} fragmento(s) en total${filter ? ` · ${docs.length} coinciden` : ''}`
    : '';
  if (!allDocs.length) {
    ul.innerHTML = '<li><em>Aún no hay contenido cargado.</em></li>';
    return;
  }
  if (!docs.length) {
    ul.innerHTML = '<li><em>Ningún documento coincide con el filtro.</em></li>';
    return;
  }
  ul.innerHTML = docs
    .map(
      (d) => `
    <li class="doc" data-id="${d.id}">
      <div class="doc-row">
        <div class="info">
          <strong>${escapeHtml(d.source_name)}</strong>
          <span class="tag tag-${d.source_type}">${d.source_type}</span>
          <div class="doc-meta">${d.chunks} fragmento(s) · ${new Date(d.created_at).toLocaleString()}</div>
        </div>
        <div class="doc-actions">
          <button class="view" data-id="${d.id}">Ver</button>
          <button class="delete" data-id="${d.id}">Eliminar</button>
        </div>
      </div>
    </li>`
    )
    .join('');

  ul.querySelectorAll('.delete').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('¿Eliminar este documento?')) return;
      await fetch('/api/documents/' + b.dataset.id, { method: 'DELETE' });
      loadDocs();
    })
  );
  ul.querySelectorAll('.view').forEach((b) =>
    b.addEventListener('click', () => toggleView(b))
  );
}

async function toggleView(btn) {
  const li = btn.closest('.doc');
  const existing = li.querySelector('.doc-content');
  if (existing) {
    existing.remove();
    btn.textContent = 'Ver';
    return;
  }
  btn.textContent = 'Cargando...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/documents/' + btn.dataset.id);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Error');
    const html = data.chunks
      .map(
        (c) => `<div class="chunk">
          <div class="chunk-label">Fragmento ${c.chunk_index + 1}</div>
          <div>${escapeHtml(c.content)}</div>
        </div>`
      )
      .join('');
    const div = document.createElement('div');
    div.className = 'doc-content';
    div.innerHTML = html || '<em>Sin contenido.</em>';
    li.appendChild(div);
    btn.textContent = 'Ocultar';
  } catch (e) {
    alert('Error: ' + e.message);
    btn.textContent = 'Ver';
  } finally {
    btn.disabled = false;
  }
}

$('#refresh-docs').addEventListener('click', loadDocs);
$('#docs-filter').addEventListener('input', renderDocs);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

loadDocs();
