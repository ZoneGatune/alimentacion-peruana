const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const status = $('#upload-status');

let adminSession = { authenticated: false, username: null };
let sellerSession = { authenticated: false, username: null, fullName: null, role: null, company: null };
let contentManagerSession = { authenticated: false, username: null, fullName: null, email: null, role: null, company: null };
let allDocs = [];
let allCategories = [];
let allPlatforms = [];
let allCompanies = [];
let allSellers = [];
let apiCatalog = null;
let currentView = 'dashboard';
const currentSubViews = {
  dashboard: 'overview',
  commercial: 'intro',
  taxonomy: 'categories',
  access: 'companies',
  api: 'summary',
};

const viewMeta = {
  dashboard: {
    title: 'Resumen ejecutivo',
    description: 'Vista general del sistema, accesos y módulos disponibles.',
  },
  intake: {
    title: 'Captura de contenido',
    description: 'Recepción de archivos, páginas, notas y videos para aprobación.',
  },
  intelligence: {
    title: 'Búsqueda inteligente',
    description: 'Consulta semántica para descubrir contenido y ofrecer alternativas.',
  },
  commercial: {
    title: 'Catálogo comercial',
    description: 'Módulo de ventas y exploración para perfiles vendedor.',
  },
  operations: {
    title: 'Operaciones',
    description: 'Moderación, publicación y administración del contenido cargado.',
  },
  taxonomy: {
    title: 'Catálogo y taxonomías',
    description: 'Gestión de categorías jerárquicas y plataformas comerciales.',
  },
  access: {
    title: 'Empresas y vendedores',
    description: 'Control de acceso comercial por empresa y perfil vendedor.',
  },
  api: {
    title: 'Documentación API',
    description: 'Guía para desarrolladores que integran el sistema vía endpoints.',
  },
};

function setStatus(message, ok) {
  status.textContent = message;
  status.className = 'status ' + (ok === true ? 'ok' : ok === false ? 'err' : '');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function approvedCategories() {
  return allCategories.filter((category) => category.approved !== false);
}

function renderCategoryTags(categories) {
  if (!categories || !categories.length) return '';
  return `<div class="category-tags">${categories.map((category) => `
    <span class="category-chip${category.approved === false ? ' pending' : ''}">
      ${escapeHtml(category.path)}${category.approved === false ? ' · pendiente' : ''}
    </span>`).join('')}</div>`;
}

function renderPlatformTags(platforms) {
  if (!platforms || !platforms.length) return '';
  return `<div class="platform-tags">${platforms.map((platform) => `<span class="platform-chip">${escapeHtml(platform.name)}</span>`).join('')}</div>`;
}

function renderLinks(url) {
  return url
    ? `<div class="links"><a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">Abrir original</a></div>`
    : '';
}

function triggerTikTokEmbeds() {
  if (window.tiktokEmbedLoadContainers) window.tiktokEmbedLoadContainers();
}

function renderVideoEmbed(item) {
  const embed = item.trailer_embed_html || (item.source_type === 'video' ? item.embed_html : '');
  return embed ? `<div class="video-embed">${embed}</div>` : '';
}

async function parseJsonResponse(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Error');
  return data;
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function setCurrentView(view) {
  currentView = view;
  $$('.module-view').forEach((section) => section.classList.toggle('active', section.dataset.view === view));
  $$('.nav-link').forEach((button) => button.classList.toggle('active', button.dataset.view === view));
  $('#current-view-name').textContent = viewMeta[view]?.title || 'Sistema';
  $('#current-view-description').textContent = viewMeta[view]?.description || '';
  activateSubView(view, currentSubViews[view]);
}

function activateSubView(view, subview) {
  const module = document.querySelector(`.module-view[data-view="${view}"]`);
  if (!module) return;
  const buttons = Array.from(module.querySelectorAll(`.module-subnav-link[data-view="${view}"]`));
  if (!buttons.length) return;
  const target = subview || buttons[0].dataset.subview;
  currentSubViews[view] = target;
  buttons.forEach((button) => button.classList.toggle('active', button.dataset.subview === target));
  module.querySelectorAll('[data-subview-section]').forEach((section) => {
    section.classList.toggle('active', section.dataset.subviewSection === target);
  });
}

function updateNavigationVisibility() {
  const sellerEnabled = !!sellerSession.authenticated;
  const adminEnabled = !!adminSession.authenticated;

  $$('.seller-nav').forEach((item) => { item.hidden = !sellerEnabled; });
  $$('.admin-nav').forEach((item) => { item.hidden = !adminEnabled; });

  if ((currentView === 'commercial' && !sellerEnabled) || ((currentView === 'operations' || currentView === 'taxonomy' || currentView === 'access') && !adminEnabled)) {
    setCurrentView('dashboard');
  }
}

function renderDashboard() {
  const stats = $('#dashboard-stats');
  const sessionBox = $('#dashboard-session');
  const modulesBox = $('#dashboard-modules');
  const apiPreview = $('#dashboard-api-preview');
  if (!stats || !sessionBox || !modulesBox || !apiPreview) return;

  const publishedCount = allDocs.filter((doc) => doc.published).length;
  const pendingCount = allDocs.filter((doc) => !doc.published).length;
  const sellerCompany = sellerSession.company?.name || 'Sin empresa';

  stats.innerHTML = [
    { label: 'Categorías', value: allCategories.length, detail: 'Ramas activas para clasificar y vender contenido.' },
    { label: 'Plataformas', value: allPlatforms.length, detail: 'Marcas disponibles para filtrar el catálogo.' },
    { label: 'Pendientes', value: pendingCount, detail: 'Contenido esperando moderación.' },
    { label: 'APIs', value: apiCatalog?.endpointCount || 0, detail: 'Endpoints documentados para integraciones.' },
  ].map((item) => `
    <article class="metric-card">
      <div class="label">${escapeHtml(item.label)}</div>
      <div class="value">${escapeHtml(String(item.value))}</div>
      <div class="detail">${escapeHtml(item.detail)}</div>
    </article>
  `).join('');

  sessionBox.innerHTML = [
    {
      label: 'Administrador',
      detail: adminSession.authenticated ? `Conectado como ${adminSession.username}` : 'Sin sesión administrativa.',
    },
    {
      label: 'Vendedor',
      detail: sellerSession.authenticated
        ? `${sellerSession.fullName || sellerSession.username} · ${sellerCompany}`
        : 'Sin sesión comercial.',
    },
    {
      label: 'Gestor de contenido',
      detail: contentManagerSession.authenticated
        ? `${contentManagerSession.fullName || contentManagerSession.username} · ${contentManagerSession.email || 'Sin correo'}`
        : 'Sin sesión de captura asistida.',
    },
    {
      label: 'Contenido publicado',
      detail: adminSession.authenticated ? `${publishedCount} documentos visibles para venta o consulta.` : 'Disponible al entrar como administrador.',
    },
  ].map((item) => `
    <article class="mini-card">
      <div class="label">${escapeHtml(item.label)}</div>
      <div class="detail">${escapeHtml(item.detail)}</div>
    </article>
  `).join('');

  modulesBox.innerHTML = [
    'Captura de contenido para abastecer la biblioteca.',
    'Búsqueda semántica para encontrar alternativas y material relacionado.',
    'Catálogo comercial exclusivo para vendedores.',
    'Operación administrativa para publicar, categorizar y mejorar contenido.',
  ].map((detail, index) => `
    <article class="mini-card">
      <div class="label">Módulo ${index + 1}</div>
      <div class="detail">${escapeHtml(detail)}</div>
    </article>
  `).join('');

  apiPreview.innerHTML = apiCatalog
    ? `
      <article class="mini-card">
        <div class="label">Base API</div>
        <div class="detail">${escapeHtml(apiCatalog.basePath)} · ${escapeHtml(apiCatalog.description || '')}</div>
      </article>
      <article class="mini-card">
        <div class="label">Módulos API</div>
        <div class="detail">${escapeHtml(String(apiCatalog.groups?.length || 0))} grupos documentados para desarrolladores.</div>
      </article>
    `
    : '<article class="mini-card"><div class="label">API</div><div class="detail">Cargando documentación técnica...</div></article>';
}

function renderApiDocs() {
  const summary = $('#api-docs-summary');
  const auth = $('#api-docs-auth');
  const groups = $('#api-docs-groups');
  const examples = $('#api-docs-examples');
  if (!summary || !auth || !groups || !examples) return;

  if (!apiCatalog) {
    summary.innerHTML = '<article class="mini-card"><div class="detail">No se pudo cargar la documentación de API.</div></article>';
    auth.innerHTML = '';
    groups.innerHTML = '';
    examples.innerHTML = '';
    return;
  }

  summary.innerHTML = [
    { label: 'Sistema', detail: apiCatalog.name || 'API' },
    { label: 'Base path', detail: apiCatalog.basePath || '/api' },
    { label: 'Endpoints', detail: `${apiCatalog.endpointCount || 0} rutas documentadas` },
  ].map((item) => `
    <article class="mini-card">
      <div class="label">${escapeHtml(item.label)}</div>
      <div class="detail">${escapeHtml(item.detail)}</div>
    </article>
  `).join('');

  auth.innerHTML = (apiCatalog.authentication || []).map((item) => `
    <article class="mini-card">
      <div class="label">${escapeHtml(item.role)}</div>
      <div class="detail">${escapeHtml(item.mode)} · ${escapeHtml(item.notes || '')}</div>
    </article>
  `).join('');

  examples.innerHTML = (apiCatalog.examples || []).map((item) => `
    <article class="example-card">
      <h4>${escapeHtml(item.title || 'Ejemplo')}</h4>
      <div class="detail">${escapeHtml(item.language || '')}</div>
      <pre>${escapeHtml(item.code || '')}</pre>
    </article>
  `).join('');

  groups.innerHTML = (apiCatalog.groups || []).map((group) => `
    <article class="api-group">
      <h4>${escapeHtml(group.title)}</h4>
      <p>${escapeHtml(group.description || '')}</p>
      <div class="endpoint-list">
        ${(group.endpoints || []).map((endpoint) => `
          <div class="endpoint-item">
            <div class="endpoint-head">
              <div class="endpoint-code">${escapeHtml(endpoint.method)} ${escapeHtml(endpoint.path)}</div>
              <span class="endpoint-badge ${escapeAttr(endpoint.access || 'publico')}">${escapeHtml(endpoint.access || 'publico')}</span>
            </div>
            <div class="endpoint-description">${escapeHtml(endpoint.description || '')}</div>
            ${endpoint.body ? `<pre class="endpoint-body">${escapeHtml(formatJson(endpoint.body))}</pre>` : ''}
          </div>
        `).join('')}
      </div>
    </article>
  `).join('');
}

async function loadApiDocs() {
  try {
    apiCatalog = await parseJsonResponse(await fetch('/api/docs', { credentials: 'include' }));
  } catch (_error) {
    apiCatalog = null;
  }
  renderApiDocs();
  renderDashboard();
}

function categoryOptions(selectedValue = '') {
  const selected = String(selectedValue || '');
  return ['<option value="">Sin categoria</option>']
    .concat(
      approvedCategories().map((category) => `<option value="${category.id}" ${selected === String(category.id) ? 'selected' : ''}>${escapeHtml(category.path)}</option>`)
    )
    .join('');
}

function platformOptions(selectedValue = '') {
  const selected = String(selectedValue || '');
  return ['<option value="">Sin plataforma</option>']
    .concat(
      allPlatforms.map((platform) => `<option value="${platform.id}" ${selected === String(platform.id) ? 'selected' : ''}>${escapeHtml(platform.name)}</option>`)
    )
    .join('');
}

function companyOptions(selectedValue = '') {
  const selected = String(selectedValue || '');
  return ['<option value="">Sin empresa</option>']
    .concat(
      allCompanies.map((company) => `<option value="${company.id}" ${selected === String(company.id) ? 'selected' : ''}>${escapeHtml(company.name)}</option>`)
    )
    .join('');
}

function populateCategorySelects() {
  $$('.category-select').forEach((select) => {
    const current = select.value;
    select.innerHTML = categoryOptions(current);
  });
  const parentSelect = $('#category-parent');
  if (parentSelect) {
    const current = parentSelect.value;
    parentSelect.innerHTML = '<option value="">Sin categoria padre (raiz)</option>' + allCategories
      .map((category) => `<option value="${category.id}" ${String(category.id) === String(current) ? 'selected' : ''}>${escapeHtml(category.path)}</option>`)
      .join('');
  }
}

function populatePlatformSelects() {
  $$('.platform-select').forEach((select) => {
    const current = select.value;
    select.innerHTML = platformOptions(current);
  });
}

function populateCompanySelects() {
  const select = $('#seller-company');
  if (!select) return;
  const current = select.value;
  select.innerHTML = companyOptions(current);
}

function syncCaptureEmailFields() {
  const managerMode = !!contentManagerSession.authenticated && !!contentManagerSession.email;
  const note = $('#content-manager-note');
  if (note) note.hidden = !managerMode;
  $$('.manager-email-field').forEach((wrapper) => {
    wrapper.hidden = managerMode;
    const input = wrapper.querySelector('input[type="email"]');
    if (!input) return;
    input.required = !managerMode;
    if (managerMode) {
      input.value = contentManagerSession.email || '';
    } else if (input.dataset.autofilled === 'true') {
      input.value = '';
    }
    input.readOnly = managerMode;
    input.dataset.autofilled = managerMode ? 'true' : 'false';
  });
}

function syncSellerRoleFields() {
  const roleSelect = $('#seller-role');
  const emailInput = $('#seller-email');
  const companySelect = $('#seller-company');
  if (!roleSelect || !emailInput || !companySelect) return;
  const isContentManager = roleSelect.value === 'gestor_de_contenido';
  emailInput.required = isContentManager;
  companySelect.required = !isContentManager;
}

async function loadCategories(includePending = false) {
  const url = includePending ? '/api/categories?include_pending=true' : '/api/categories';
  const data = await parseJsonResponse(await fetch(url, { credentials: 'include' }));
  allCategories = data.categories || [];
  populateCategorySelects();
  renderCategoryAdmin();
  renderDashboard();
}

async function loadPlatforms() {
  const data = await parseJsonResponse(await fetch('/api/platforms', { credentials: 'include' }));
  allPlatforms = data.platforms || [];
  populatePlatformSelects();
  renderPlatformAdmin();
  renderDashboard();
}

async function loadTaxonomies(includePendingCategories = false) {
  await Promise.all([loadCategories(includePendingCategories), loadPlatforms()]);
}

async function loadCompanies() {
  if (!adminSession.authenticated) {
    allCompanies = [];
    populateCompanySelects();
    renderCompanyAdmin();
    renderDashboard();
    return;
  }
  const data = await parseJsonResponse(await fetch('/api/companies', { credentials: 'include' }));
  allCompanies = data.companies || [];
  populateCompanySelects();
  renderCompanyAdmin();
  renderDashboard();
}

async function loadSellers() {
  if (!adminSession.authenticated) {
    allSellers = [];
    renderSellerAdmin();
    renderDashboard();
    return;
  }
  const data = await parseJsonResponse(await fetch('/api/sellers', { credentials: 'include' }));
  allSellers = data.sellers || [];
  renderSellerAdmin();
  renderDashboard();
}

function collectFormJson(form, extra = {}) {
  const emailInput = form.querySelector('[name="email"]');
  return {
    email: emailInput ? emailInput.value : '',
    categoryId: form.categoryId.value,
    platformId: form.platformId ? form.platformId.value : '',
    proposedCategoryName: form.proposedCategoryName ? form.proposedCategoryName.value : '',
    proposedCategoryDescription: form.proposedCategoryDescription ? form.proposedCategoryDescription.value : '',
    trailerUrl: form.trailerUrl ? form.trailerUrl.value : '',
    isTrending: !!(form.isTrending && form.isTrending.checked),
    ...extra,
  };
}

async function handle(promise, okMessage) {
  setStatus('Procesando...');
  try {
    const data = await parseJsonResponse(await promise);
    const note = data.published
      ? `${okMessage} y publicado (${data.chunks} fragmentos).`
      : `${okMessage}. Pendiente de aprobación; avisaremos a ${data.submitterEmail} cuando se publique (${data.chunks} fragmentos).`;
    setStatus(note, true);
    if (adminSession.authenticated) await loadDocs();
    if (sellerSession.authenticated) await loadLibrary();
  } catch (error) {
    setStatus('Error: ' + error.message, false);
  }
}

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((item) => item.classList.remove('active'));
    $$('.panel').forEach((item) => item.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('form-' + tab.dataset.tab).classList.add('active');
  });
});

$('#form-file').addEventListener('submit', (event) => {
  event.preventDefault();
  handle(fetch('/api/upload', {
    method: 'POST',
    body: new FormData(event.target),
    credentials: 'include',
  }), 'Archivo recibido');
});

$('#form-url').addEventListener('submit', (event) => {
  event.preventDefault();
  handle(fetch('/api/url', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(collectFormJson(event.target, { url: event.target.url.value })),
  }), 'Página recibida');
});

$('#form-text').addEventListener('submit', (event) => {
  event.preventDefault();
  handle(fetch('/api/text', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(collectFormJson(event.target, {
      title: event.target.title.value,
      text: event.target.text.value,
    })),
  }), 'Nota recibida');
});

$('#form-video').addEventListener('submit', (event) => {
  event.preventDefault();
  handle(fetch('/api/video', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(collectFormJson(event.target, { embedHtml: event.target.embedHtml.value })),
  }), 'Video recibido');
});

$('#form-search').addEventListener('submit', async (event) => {
  event.preventDefault();
  const box = $('#results');
  box.innerHTML = '<p>Buscando...</p>';
  try {
    const data = await parseJsonResponse(await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: event.target.query.value, k: 5 }),
    }));
    if (!data.results.length) {
      box.innerHTML = '<p>Sin resultados publicados.</p>';
      return;
    }
    box.innerHTML = (data.generated ? '<p class="hint">Resultado generado por IA y agregado a la base.</p>' : '') + data.results.map((item) => `
      <div class="result">
        <div class="meta">
          <span><strong>${escapeHtml(item.source_name)}</strong> · ${item.source_type}</span>
          <span>similitud: ${(item.similarity * 100).toFixed(1)}%</span>
        </div>
        ${renderCategoryTags(item.categories)}
        ${renderPlatformTags(item.platforms)}
        ${item.is_trending ? '<div class="subcontent"><strong>En tendencia</strong></div>' : ''}
        ${item.external_title && item.external_title !== item.source_name ? `<div class="subcontent"><strong>${escapeHtml(item.external_title)}</strong></div>` : ''}
        ${item.external_description ? `<div class="subcontent">${escapeHtml(item.external_description)}</div>` : ''}
        ${item.external_published_at ? `<div class="subcontent">Publicado: ${escapeHtml(formatDate(item.external_published_at))}</div>` : ''}
        <div class="content">${escapeHtml(item.content)}</div>
        ${renderLinks(item.original_url)}
        ${renderVideoEmbed(item)}
      </div>
    `).join('');
    triggerTikTokEmbeds();
  } catch (error) {
    box.innerHTML = `<p style="color:#b00020">${escapeHtml(error.message)}</p>`;
  }
});

async function loadLibrary() {
  const section = $('#seller-library-section');
  const box = $('#library-results');
  const locked = $('#commercial-locked');
  if (!section || !box) return;
  if (!sellerSession.authenticated) {
    section.hidden = true;
    if (locked) locked.hidden = false;
    box.innerHTML = '';
    return;
  }

  section.hidden = false;
  if (locked) locked.hidden = true;
  const form = $('#form-library');
  if (!form) {
    box.innerHTML = '';
    return;
  }
  const params = new URLSearchParams();
  if (form.query.value.trim()) params.set('q', form.query.value.trim());
  if (form.categoryId.value) params.set('category_id', form.categoryId.value);
  if (form.platformId.value) params.set('platform_id', form.platformId.value);
  if (form.trending.checked) params.set('trending', 'true');
  box.innerHTML = '<p>Cargando biblioteca comercial...</p>';
  try {
    const data = await parseJsonResponse(await fetch(`/api/library?${params.toString()}`, { credentials: 'include' }));
    if (!data.items.length) {
      box.innerHTML = '<p>No hay contenido para ese filtro.</p>';
      return;
    }
    box.innerHTML = data.items.map((item) => `
      <article class="library-item">
        <div class="meta">
          <span><strong>${escapeHtml(item.external_title || item.source_name)}</strong></span>
          <span>${item.is_trending ? 'Tendencia' : item.source_type}</span>
        </div>
        ${renderCategoryTags(item.categories)}
        ${renderPlatformTags(item.platforms)}
        ${item.external_description ? `<div class="subcontent">${escapeHtml(item.external_description)}</div>` : ''}
        ${item.external_published_at ? `<div class="subcontent">Disponible desde: ${escapeHtml(formatDate(item.external_published_at))}</div>` : ''}
        <div class="content">${escapeHtml(item.excerpt || item.source_name)}</div>
        ${renderLinks(item.original_url || item.trailer_url)}
        ${renderVideoEmbed(item)}
      </article>
    `).join('');
    triggerTikTokEmbeds();
  } catch (error) {
    box.innerHTML = `<p style="color:#b00020">${escapeHtml(error.message)}</p>`;
  }
}

$('#form-library').addEventListener('submit', async (event) => {
  event.preventDefault();
  await loadLibrary();
});

async function refreshSessions() {
  const [adminResponse, sellerResponse, contentManagerResponse] = await Promise.all([
    fetch('/api/admin/me', { credentials: 'include' }),
    fetch('/api/seller/me', { credentials: 'include' }),
    fetch('/api/content-manager/me', { credentials: 'include' }),
  ]);

  adminSession = await adminResponse.json();
  sellerSession = await sellerResponse.json();
  contentManagerSession = await contentManagerResponse.json();

  $('#auth-state').textContent = adminSession.authenticated ? `Admin: ${adminSession.username}` : '';
  $('#auth-btn').textContent = adminSession.authenticated ? 'Cerrar sesión admin' : 'Acceso administrador';
  $('#content-manager-auth-state').textContent = contentManagerSession.authenticated
    ? `Gestor: ${contentManagerSession.fullName || contentManagerSession.username}`
    : '';
  $('#content-manager-auth-btn').textContent = contentManagerSession.authenticated ? 'Cerrar sesión gestor' : 'Acceso gestor';
  $('#seller-auth-state').textContent = sellerSession.authenticated
    ? `Vendedor: ${sellerSession.fullName || sellerSession.username} · ${sellerSession.company?.name || 'Sin empresa'}`
    : '';
  $('#seller-auth-btn').textContent = sellerSession.authenticated ? 'Cerrar sesión vendedor' : 'Acceso vendedor';

  updateNavigationVisibility();
  syncCaptureEmailFields();
  $('#admin-section').hidden = !adminSession.authenticated;
  $('#categories-section').hidden = !adminSession.authenticated;
  $('#platforms-section').hidden = !adminSession.authenticated;
  $('#companies-section').hidden = !adminSession.authenticated;
  $('#sellers-section').hidden = !adminSession.authenticated;

  await loadTaxonomies(adminSession.authenticated);

  if (adminSession.authenticated) {
    await Promise.all([loadDocs(), loadCompanies(), loadSellers()]);
  } else {
    allDocs = [];
    allCompanies = [];
    allSellers = [];
    renderDocs();
    populateCompanySelects();
    renderCompanyAdmin();
    renderSellerAdmin();
  }

  await loadLibrary();
  renderDashboard();
}

$('#auth-btn').addEventListener('click', async () => {
  if (adminSession.authenticated) {
    await fetch('/api/admin/logout', { method: 'POST', credentials: 'include' });
    await refreshSessions();
  } else {
    $('#login-modal').hidden = false;
  }
});

$('#content-manager-auth-btn').addEventListener('click', async () => {
  if (contentManagerSession.authenticated) {
    await fetch('/api/content-manager/logout', { method: 'POST', credentials: 'include' });
    await refreshSessions();
  } else {
    $('#content-manager-login-modal').hidden = false;
  }
});

$('#seller-auth-btn').addEventListener('click', async () => {
  if (sellerSession.authenticated) {
    await fetch('/api/seller/logout', { method: 'POST', credentials: 'include' });
    await refreshSessions();
  } else {
    $('#seller-login-modal').hidden = false;
  }
});

$('#login-cancel').addEventListener('click', () => {
  $('#login-modal').hidden = true;
  $('#login-error').textContent = '';
});

$('#content-manager-login-cancel').addEventListener('click', () => {
  $('#content-manager-login-modal').hidden = true;
  $('#content-manager-login-error').textContent = '';
});

$('#seller-login-cancel').addEventListener('click', () => {
  $('#seller-login-modal').hidden = true;
  $('#seller-login-error').textContent = '';
});

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await parseJsonResponse(await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username: event.target.username.value, password: event.target.password.value }),
    }));
    $('#login-modal').hidden = true;
    $('#login-error').textContent = '';
    event.target.reset();
    await refreshSessions();
  } catch (error) {
    $('#login-error').textContent = error.message;
  }
});

$('#seller-login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await parseJsonResponse(await fetch('/api/seller/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username: event.target.username.value, password: event.target.password.value }),
    }));
    $('#seller-login-modal').hidden = true;
    $('#seller-login-error').textContent = '';
    event.target.reset();
    await refreshSessions();
  } catch (error) {
    $('#seller-login-error').textContent = error.message;
  }
});

$('#content-manager-login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await parseJsonResponse(await fetch('/api/content-manager/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username: event.target.username.value, password: event.target.password.value }),
    }));
    $('#content-manager-login-modal').hidden = true;
    $('#content-manager-login-error').textContent = '';
    event.target.reset();
    await refreshSessions();
  } catch (error) {
    $('#content-manager-login-error').textContent = error.message;
  }
});

async function loadDocs() {
  const response = await fetch('/api/documents', { credentials: 'include' });
  if (response.status === 401) {
    adminSession = { authenticated: false };
    await refreshSessions();
    return;
  }
  const data = await response.json();
  allDocs = data.documents || [];
  renderDocs();
  renderDashboard();
}

function docMatchesFilter(doc, filter) {
  return (
    doc.source_name.toLowerCase().includes(filter) ||
    String(doc.external_title || '').toLowerCase().includes(filter) ||
    String(doc.submitter_email || '').toLowerCase().includes(filter) ||
    String(doc.original_url || '').toLowerCase().includes(filter) ||
    (doc.categories || []).some((category) => category.path.toLowerCase().includes(filter)) ||
    (doc.platforms || []).some((platform) => platform.name.toLowerCase().includes(filter))
  );
}

function renderDocMeta(doc) {
  const parts = [`${doc.chunks} fragmento(s)`, formatDate(doc.created_at)];
  if (doc.submitter_email) parts.push(`correo: ${doc.submitter_email}`);
  if (doc.is_trending) parts.push('tendencia');
  if (doc.approval_notified_at) {
    parts.push(`aviso enviado: ${formatDate(doc.approval_notified_at)}`);
  } else if (doc.submitter_email && doc.published) {
    parts.push('aviso pendiente o sin SMTP');
  }
  return parts.join(' · ');
}

function renderDocumentFields(doc) {
  const items = [
    doc.submitter_email ? `<div><strong>Correo:</strong> ${escapeHtml(doc.submitter_email)}</div>` : '',
    doc.external_title ? `<div><strong>Título externo:</strong> ${escapeHtml(doc.external_title)}</div>` : '',
    doc.external_description ? `<div><strong>Descripción:</strong> ${escapeHtml(doc.external_description)}</div>` : '',
    doc.external_published_at ? `<div><strong>Fecha fuente:</strong> ${escapeHtml(formatDate(doc.external_published_at))}</div>` : '',
    doc.is_trending ? '<div><strong>Tendencia:</strong> Sí</div>' : '',
    doc.original_url ? `<div class="doc-links"><a href="${escapeAttr(doc.original_url)}" target="_blank" rel="noopener noreferrer">Abrir original</a></div>` : '',
  ].filter(Boolean);
  return items.length ? `<div class="doc-fields">${items.join('')}</div>` : '';
}

function renderCategoryEditor(doc) {
  if (!allCategories.length) return '<div class="doc-fields"><em>No hay categorias creadas.</em></div>';
  const selected = new Set((doc.categories || []).map((category) => Number(category.id)));
  return `
    <div class="doc-fields">
      <strong>Categorías</strong>
      <div class="category-picker" data-kind="category-picker" data-document-id="${doc.id}">
        ${allCategories.map((category) => `
          <label class="category-option">
            <input type="checkbox" data-category-id="${category.id}" value="${category.id}" ${selected.has(Number(category.id)) ? 'checked' : ''} />
            <span>${escapeHtml(category.path)}${category.approved === false ? ' · pendiente' : ''}</span>
          </label>
        `).join('')}
      </div>
      <button class="save-doc-categories" data-id="${doc.id}">Guardar categorías</button>
    </div>
  `;
}

function renderPlatformEditor(doc) {
  if (!allPlatforms.length) return '<div class="doc-fields"><em>No hay plataformas creadas.</em></div>';
  const selected = new Set((doc.platforms || []).map((platform) => Number(platform.id)));
  return `
    <div class="doc-fields">
      <strong>Plataformas</strong>
      <div class="category-picker" data-kind="platform-picker" data-document-id="${doc.id}">
        ${allPlatforms.map((platform) => `
          <label class="category-option">
            <input type="checkbox" data-platform-id="${platform.id}" value="${platform.id}" ${selected.has(Number(platform.id)) ? 'checked' : ''} />
            <span>${escapeHtml(platform.name)}</span>
          </label>
        `).join('')}
      </div>
      <button class="save-doc-platforms" data-id="${doc.id}">Guardar plataformas</button>
    </div>
  `;
}

function renderLibraryMetadataEditor(doc) {
  return `
    <div class="doc-fields">
      <strong>Biblioteca comercial</strong>
      <label class="checkbox-row"><input type="checkbox" class="doc-trending" ${doc.is_trending ? 'checked' : ''} /> En tendencia</label>
      <input type="url" class="doc-trailer-url" value="${escapeAttr(doc.trailer_url || '')}" placeholder="URL del trailer (TikTok/Facebook/YouTube)" />
      <button class="save-doc-library" data-id="${doc.id}">Guardar biblioteca</button>
    </div>
  `;
}

async function saveDocumentCategories(button) {
  const picker = button.closest('.doc-content').querySelector('[data-kind="category-picker"]');
  const categoryIds = Array.from(picker.querySelectorAll('input[data-category-id]:checked')).map((input) => Number(input.value));
  await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}/categories`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ categoryIds }),
  }));
  await loadDocs();
  await loadLibrary();
}

async function saveDocumentPlatforms(button) {
  const picker = button.closest('.doc-content').querySelector('[data-kind="platform-picker"]');
  const platformIds = Array.from(picker.querySelectorAll('input[data-platform-id]:checked')).map((input) => Number(input.value));
  await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}/platforms`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ platformIds }),
  }));
  await loadDocs();
  await loadLibrary();
}

async function saveDocumentLibraryMetadata(button) {
  const content = button.closest('.doc-content');
  await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}/library-metadata`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      isTrending: content.querySelector('.doc-trending').checked,
      trailerUrl: content.querySelector('.doc-trailer-url').value,
    }),
  }));
  await loadDocs();
  await loadLibrary();
}

function renderDocs() {
  const list = $('#docs');
  if (!list) return;

  const filter = ($('#docs-filter').value || '').toLowerCase();
  const statusFilter = $('#docs-status').value;
  const docs = allDocs.filter((doc) => {
    if (statusFilter === 'pending' && doc.published) return false;
    if (statusFilter === 'published' && !doc.published) return false;
    if (filter && !docMatchesFilter(doc, filter)) return false;
    return true;
  });

  const pending = allDocs.filter((doc) => !doc.published).length;
  const published = allDocs.length - pending;
  $('#docs-summary').textContent = allDocs.length
    ? `${published} publicado(s) · ${pending} pendiente(s) · ${allDocs.length} total${filter || statusFilter !== 'all' ? ` · ${docs.length} mostrados` : ''}`
    : '';

  if (!allDocs.length) {
    list.innerHTML = '<li><em>Aún no hay contenido cargado.</em></li>';
    return;
  }
  if (!docs.length) {
    list.innerHTML = '<li><em>Ningún documento coincide.</em></li>';
    return;
  }

  list.innerHTML = docs.map((doc) => `
    <li class="doc ${doc.published ? 'published' : 'pending'}" data-id="${doc.id}">
      <div class="doc-row">
        <div class="info">
          <strong>${escapeHtml(doc.source_name)}</strong>
          <span class="tag tag-${doc.source_type}">${doc.source_type}</span>
          <span class="tag tag-${doc.published ? 'published' : 'pending'}">${doc.published ? 'publicado' : 'pendiente'}</span>
          ${renderCategoryTags(doc.categories)}
          ${renderPlatformTags(doc.platforms)}
          <div class="doc-meta">${escapeHtml(renderDocMeta(doc))}</div>
        </div>
        <div class="doc-actions">
          <button class="view" data-id="${doc.id}">Ver</button>
          ${doc.published ? `<button class="unpublish" data-id="${doc.id}">Despublicar</button>` : `<button class="publish" data-id="${doc.id}">Aprobar y publicar</button>`}
          <button class="delete" data-id="${doc.id}">Eliminar</button>
        </div>
      </div>
    </li>
  `).join('');

  list.querySelectorAll('.delete').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm('¿Eliminar este documento?')) return;
      try {
        await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}`, { method: 'DELETE', credentials: 'include' }));
        await loadDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });

  list.querySelectorAll('.publish').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        const data = await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}/publish`, {
          method: 'POST',
          credentials: 'include',
        }));
        if (data.notification && !data.notification.sent && data.notification.reason) {
          alert('Documento publicado, pero el aviso por correo no se envió: ' + data.notification.reason);
        }
        await loadDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });

  list.querySelectorAll('.unpublish').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}/unpublish`, {
          method: 'POST',
          credentials: 'include',
        }));
        await loadDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });

  list.querySelectorAll('.view').forEach((button) => button.addEventListener('click', () => toggleView(button)));
}

async function toggleView(button) {
  const li = button.closest('.doc');
  const existing = li.querySelector('.doc-content');
  if (existing) {
    existing.remove();
    button.textContent = 'Ver';
    return;
  }
  button.textContent = 'Cargando...';
  button.disabled = true;
  try {
    const data = await parseJsonResponse(await fetch(`/api/documents/${button.dataset.id}`, { credentials: 'include' }));
    const div = document.createElement('div');
    div.className = 'doc-content';
    div.innerHTML = `
      ${renderDocumentFields(data.document)}
      ${renderCategoryEditor(data.document)}
      ${renderPlatformEditor(data.document)}
      ${renderLibraryMetadataEditor(data.document)}
      ${renderVideoEmbed(data.document)}
      ${(data.chunks || []).map((chunk) => `
        <div class="chunk">
          <div class="chunk-label">Fragmento ${chunk.chunk_index + 1}</div>
          <div>${escapeHtml(chunk.content)}</div>
        </div>
      `).join('') || '<em>Sin contenido.</em>'}
    `;
    li.appendChild(div);
    const saveCategories = div.querySelector('.save-doc-categories');
    const savePlatforms = div.querySelector('.save-doc-platforms');
    const saveLibrary = div.querySelector('.save-doc-library');
    if (saveCategories) saveCategories.addEventListener('click', () => saveDocumentCategories(saveCategories));
    if (savePlatforms) savePlatforms.addEventListener('click', () => saveDocumentPlatforms(savePlatforms));
    if (saveLibrary) saveLibrary.addEventListener('click', () => saveDocumentLibraryMetadata(saveLibrary));
    button.textContent = 'Ocultar';
    triggerTikTokEmbeds();
  } catch (error) {
    alert('Error: ' + error.message);
    button.textContent = 'Ver';
  } finally {
    button.disabled = false;
  }
}

function resetCategoryForm(parentId = '') {
  $('#category-id').value = '';
  $('#category-name').value = '';
  $('#category-description').value = '';
  $('#category-parent').value = parentId ? String(parentId) : '';
  $('#category-submit').textContent = 'Crear categoría';
  $('#category-cancel').hidden = true;
}

function renderCategoryAdmin() {
  const list = $('#category-list');
  if (!list) return;
  if (!allCategories.length) {
    list.innerHTML = '<li><em>No hay categorías creadas.</em></li>';
    return;
  }

  list.innerHTML = allCategories.map((category) => `
    <li class="category-item" data-id="${category.id}">
      <div class="category-item-row">
        <div class="category-item-info">
          <strong>${escapeHtml(category.path)}</strong>
          ${category.approved === false ? '<span class="tag tag-pending">pendiente</span>' : '<span class="tag tag-published">aprobada</span>'}
          ${category.description ? `<div class="category-item-desc">${escapeHtml(category.description)}</div>` : ''}
          ${category.created_by_submitter_email ? `<div class="category-item-desc">Propuesta por: ${escapeHtml(category.created_by_submitter_email)}</div>` : ''}
        </div>
        <div class="category-item-actions">
          <button class="ghost small category-child" data-id="${category.id}">Subcategoría</button>
          ${category.approved === false ? `<button class="small approve category-approve" data-id="${category.id}">Aprobar</button>` : ''}
          <button class="ghost small category-edit" data-id="${category.id}">Editar</button>
          <button class="small delete category-delete" data-id="${category.id}">Eliminar</button>
        </div>
      </div>
    </li>
  `).join('');

  list.querySelectorAll('.category-child').forEach((button) => {
    button.addEventListener('click', () => {
      resetCategoryForm(button.dataset.id);
      $('#category-name').focus();
    });
  });

  list.querySelectorAll('.category-edit').forEach((button) => {
    button.addEventListener('click', () => {
      const category = allCategories.find((item) => String(item.id) === button.dataset.id);
      if (!category) return;
      $('#category-id').value = category.id;
      $('#category-name').value = category.name;
      $('#category-description').value = category.description || '';
      $('#category-parent').value = category.parent_id || '';
      $('#category-submit').textContent = 'Guardar cambios';
      $('#category-cancel').hidden = false;
    });
  });

  list.querySelectorAll('.category-approve').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        const data = await parseJsonResponse(await fetch(`/api/categories/${button.dataset.id}/approve`, {
          method: 'POST',
          credentials: 'include',
        }));
        allCategories = data.categories || [];
        populateCategorySelects();
        renderCategoryAdmin();
        renderDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });

  list.querySelectorAll('.category-delete').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm('¿Eliminar esta categoría? Solo se puede si no tiene hijos ni documentos asociados.')) return;
      try {
        const data = await parseJsonResponse(await fetch(`/api/categories/${button.dataset.id}`, {
          method: 'DELETE',
          credentials: 'include',
        }));
        allCategories = data.categories || [];
        populateCategorySelects();
        renderCategoryAdmin();
        renderDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });
}

$('#category-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const categoryId = $('#category-id').value;
  try {
    const data = await parseJsonResponse(await fetch(categoryId ? `/api/categories/${categoryId}` : '/api/categories', {
      method: categoryId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        name: $('#category-name').value,
        description: $('#category-description').value,
        parentId: $('#category-parent').value,
      }),
    }));
    allCategories = data.categories || [];
    populateCategorySelects();
    renderCategoryAdmin();
    renderDocs();
    await loadLibrary();
    resetCategoryForm();
  } catch (error) {
    alert('Error: ' + error.message);
  }
});

$('#category-cancel').addEventListener('click', () => resetCategoryForm());

function resetPlatformForm() {
  $('#platform-id').value = '';
  $('#platform-name').value = '';
  $('#platform-description').value = '';
  $('#platform-submit').textContent = 'Crear plataforma';
  $('#platform-cancel').hidden = true;
}

function renderPlatformAdmin() {
  const list = $('#platform-list');
  if (!list) return;
  if (!allPlatforms.length) {
    list.innerHTML = '<li><em>No hay plataformas creadas.</em></li>';
    return;
  }
  list.innerHTML = allPlatforms.map((platform) => `
    <li class="category-item">
      <div class="category-item-row">
        <div class="category-item-info">
          <strong>${escapeHtml(platform.name)}</strong>
          ${platform.description ? `<div class="category-item-desc">${escapeHtml(platform.description)}</div>` : ''}
        </div>
        <div class="category-item-actions">
          <button class="ghost small platform-edit" data-id="${platform.id}">Editar</button>
          <button class="small delete platform-delete" data-id="${platform.id}">Eliminar</button>
        </div>
      </div>
    </li>
  `).join('');

  list.querySelectorAll('.platform-edit').forEach((button) => {
    button.addEventListener('click', () => {
      const platform = allPlatforms.find((item) => String(item.id) === button.dataset.id);
      if (!platform) return;
      $('#platform-id').value = platform.id;
      $('#platform-name').value = platform.name;
      $('#platform-description').value = platform.description || '';
      $('#platform-submit').textContent = 'Guardar cambios';
      $('#platform-cancel').hidden = false;
    });
  });

  list.querySelectorAll('.platform-delete').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm('¿Eliminar esta plataforma? Solo se puede si no está asociada a documentos.')) return;
      try {
        const data = await parseJsonResponse(await fetch(`/api/platforms/${button.dataset.id}`, {
          method: 'DELETE',
          credentials: 'include',
        }));
        allPlatforms = data.platforms || [];
        populatePlatformSelects();
        renderPlatformAdmin();
        renderDocs();
        await loadLibrary();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });
}

$('#platform-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const platformId = $('#platform-id').value;
  try {
    const data = await parseJsonResponse(await fetch(platformId ? `/api/platforms/${platformId}` : '/api/platforms', {
      method: platformId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        name: $('#platform-name').value,
        description: $('#platform-description').value,
      }),
    }));
    allPlatforms = data.platforms || [];
    populatePlatformSelects();
    renderPlatformAdmin();
    renderDocs();
    await loadLibrary();
    resetPlatformForm();
  } catch (error) {
    alert('Error: ' + error.message);
  }
});

$('#platform-cancel').addEventListener('click', () => resetPlatformForm());

function resetCompanyForm() {
  $('#company-id').value = '';
  $('#company-name').value = '';
  $('#company-description').value = '';
  $('#company-submit').textContent = 'Crear empresa';
  $('#company-cancel').hidden = true;
}

function renderCompanyAdmin() {
  const list = $('#company-list');
  if (!list) return;
  if (!allCompanies.length) {
    list.innerHTML = '<li><em>No hay empresas creadas.</em></li>';
    return;
  }
  list.innerHTML = allCompanies.map((company) => `
    <li class="category-item">
      <div class="category-item-row">
        <div class="category-item-info">
          <strong>${escapeHtml(company.name)}</strong>
          ${company.description ? `<div class="category-item-desc">${escapeHtml(company.description)}</div>` : ''}
        </div>
        <div class="category-item-actions">
          <button class="ghost small company-edit" data-id="${company.id}">Editar</button>
          <button class="small delete company-delete" data-id="${company.id}">Eliminar</button>
        </div>
      </div>
    </li>
  `).join('');

  list.querySelectorAll('.company-edit').forEach((button) => {
    button.addEventListener('click', () => {
      const company = allCompanies.find((item) => String(item.id) === button.dataset.id);
      if (!company) return;
      $('#company-id').value = company.id;
      $('#company-name').value = company.name;
      $('#company-description').value = company.description || '';
      $('#company-submit').textContent = 'Guardar cambios';
      $('#company-cancel').hidden = false;
    });
  });

  list.querySelectorAll('.company-delete').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm('¿Eliminar esta empresa? Solo se puede si no tiene vendedores asignados.')) return;
      try {
        const data = await parseJsonResponse(await fetch(`/api/companies/${button.dataset.id}`, {
          method: 'DELETE',
          credentials: 'include',
        }));
        allCompanies = data.companies || [];
        populateCompanySelects();
        renderCompanyAdmin();
        await loadSellers();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });
}

$('#company-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const companyId = $('#company-id').value;
  try {
    const data = await parseJsonResponse(await fetch(companyId ? `/api/companies/${companyId}` : '/api/companies', {
      method: companyId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        name: $('#company-name').value,
        description: $('#company-description').value,
      }),
    }));
    allCompanies = data.companies || [];
    populateCompanySelects();
    renderCompanyAdmin();
    await loadSellers();
    resetCompanyForm();
  } catch (error) {
    alert('Error: ' + error.message);
  }
});

$('#company-cancel').addEventListener('click', () => resetCompanyForm());

function resetSellerForm() {
  $('#seller-id').value = '';
  $('#seller-role').value = 'vendedor';
  $('#seller-full-name').value = '';
  $('#seller-username').value = '';
  $('#seller-email').value = '';
  $('#seller-password').value = '';
  $('#seller-company').value = '';
  $('#seller-active').checked = true;
  $('#seller-submit').textContent = 'Crear perfil';
  $('#seller-cancel').hidden = true;
  syncSellerRoleFields();
}

function renderSellerAdmin() {
  const list = $('#seller-list');
  if (!list) return;
  if (!allSellers.length) {
    list.innerHTML = '<li><em>No hay perfiles de acceso creados.</em></li>';
    return;
  }
  list.innerHTML = allSellers.map((seller) => `
    <li class="category-item">
      <div class="category-item-row">
        <div class="category-item-info">
          <strong>${escapeHtml(seller.full_name)}</strong>
          <span class="tag tag-published">${escapeHtml(seller.role === 'gestor_de_contenido' ? 'gestor de contenido' : seller.role)}</span>
          ${seller.active ? '' : '<span class="tag tag-pending">inactivo</span>'}
          <div class="category-item-desc">Usuario: ${escapeHtml(seller.username)}</div>
          ${seller.email ? `<div class="category-item-desc">Correo: ${escapeHtml(seller.email)}</div>` : ''}
          <div class="category-item-desc">Empresa: ${escapeHtml(seller.company_name || 'Sin empresa')}</div>
        </div>
        <div class="category-item-actions">
          <button class="ghost small seller-edit" data-id="${seller.id}">Editar</button>
          <button class="small delete seller-delete" data-id="${seller.id}">Eliminar</button>
        </div>
      </div>
    </li>
  `).join('');

  list.querySelectorAll('.seller-edit').forEach((button) => {
    button.addEventListener('click', () => {
      const seller = allSellers.find((item) => String(item.id) === button.dataset.id);
      if (!seller) return;
      $('#seller-id').value = seller.id;
      $('#seller-role').value = seller.role;
      $('#seller-full-name').value = seller.full_name;
      $('#seller-username').value = seller.username;
      $('#seller-email').value = seller.email || '';
      $('#seller-password').value = '';
      $('#seller-company').value = seller.company_id || '';
      $('#seller-active').checked = !!seller.active;
      $('#seller-submit').textContent = 'Guardar cambios';
      $('#seller-cancel').hidden = false;
      syncSellerRoleFields();
    });
  });

  list.querySelectorAll('.seller-delete').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm('¿Eliminar este perfil vendedor?')) return;
      try {
        const data = await parseJsonResponse(await fetch(`/api/sellers/${button.dataset.id}`, {
          method: 'DELETE',
          credentials: 'include',
        }));
        allSellers = data.sellers || [];
        renderSellerAdmin();
      } catch (error) {
        alert('Error: ' + error.message);
      }
    });
  });
}

$('#seller-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const sellerId = $('#seller-id').value;
  const password = $('#seller-password').value;
  if (!sellerId && !password) {
    alert('Debes definir una contraseña para el vendedor.');
    return;
  }
  try {
    const data = await parseJsonResponse(await fetch(sellerId ? `/api/sellers/${sellerId}` : '/api/sellers', {
      method: sellerId ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        role: $('#seller-role').value,
        fullName: $('#seller-full-name').value,
        username: $('#seller-username').value,
        email: $('#seller-email').value,
        password,
        companyId: $('#seller-company').value,
        active: $('#seller-active').checked,
      }),
    }));
    allSellers = data.sellers || [];
    renderSellerAdmin();
    resetSellerForm();
  } catch (error) {
    alert('Error: ' + error.message);
  }
});

$('#seller-cancel').addEventListener('click', () => resetSellerForm());
$('#seller-role').addEventListener('change', syncSellerRoleFields);

$('#refresh-docs').addEventListener('click', loadDocs);
$('#docs-filter').addEventListener('input', renderDocs);
$('#docs-status').addEventListener('change', renderDocs);

$$('.nav-link').forEach((button) => {
  button.addEventListener('click', () => setCurrentView(button.dataset.view));
});

$$('.module-subnav-link').forEach((button) => {
  button.addEventListener('click', () => activateSubView(button.dataset.view, button.dataset.subview));
});

$$('.go-view').forEach((button) => {
  button.addEventListener('click', () => setCurrentView(button.dataset.goView));
});

setCurrentView('dashboard');
syncSellerRoleFields();
loadApiDocs();
refreshSessions();
