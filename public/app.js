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

async function loadDocs() {
  const r = await fetch('/api/documents');
  const data = await r.json();
  const ul = $('#docs');
  if (!data.documents.length) {
    ul.innerHTML = '<li><em>Aún no hay documentos.</em></li>';
    return;
  }
  ul.innerHTML = data.documents
    .map(
      (d) => `
    <li>
      <span>
        <strong>${escapeHtml(d.source_name)}</strong>
        <span class="doc-meta"> · ${d.source_type} · ${d.chunks} fragmentos · ${new Date(d.created_at).toLocaleString()}</span>
      </span>
      <button data-id="${d.id}">Eliminar</button>
    </li>`
    )
    .join('');
  ul.querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!confirm('¿Eliminar este documento?')) return;
      await fetch('/api/documents/' + b.dataset.id, { method: 'DELETE' });
      loadDocs();
    })
  );
}

$('#refresh-docs').addEventListener('click', loadDocs);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

loadDocs();
