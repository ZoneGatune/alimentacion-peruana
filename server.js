const express = require('express');
const path = require('path');
const fs = require('fs');
const multer = require('multer');
const { Pool } = require('pg');
const pdfParse = require('pdf-parse');
const mammoth = require('mammoth');
const cheerio = require('cheerio');
const { GoogleGenAI } = require('@google/genai');

const app = express();
const PORT = process.env.PORT || 5000;
const HOST = '0.0.0.0';

const pool = new Pool({ connectionString: process.env.DATABASE_URL });
const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY || '' });

const EMBED_MODEL = 'gemini-embedding-001';
const VISION_MODEL = 'gemini-2.5-flash';

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 25 * 1024 * 1024 },
});

if (process.env.NODE_ENV !== 'production') {
  app.use((req, res, next) => { res.set('Cache-Control', 'no-store'); next(); });
}
app.use(express.json({ limit: '5mb' }));

app.use('/api', (req, res, next) => {
  res.set('Access-Control-Allow-Origin', '*');
  res.set('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS');
  res.set('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

app.use(express.static(path.join(__dirname, 'public')));

function chunkText(text, size = 1200, overlap = 150) {
  const clean = text.replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  const chunks = [];
  let i = 0;
  while (i < clean.length) {
    chunks.push(clean.slice(i, i + size));
    i += size - overlap;
  }
  return chunks;
}

async function embed(text) {
  const res = await ai.models.embedContent({
    model: EMBED_MODEL,
    contents: text,
    config: { outputDimensionality: 768 },
  });
  const values =
    res?.embeddings?.[0]?.values ||
    res?.embedding?.values ||
    null;
  if (!values) throw new Error('No embedding returned by Gemini');
  return values;
}

function toVectorLiteral(values) {
  return '[' + values.map((v) => Number(v).toFixed(6)).join(',') + ']';
}

async function extractFromBuffer(buffer, mimetype, originalName) {
  const lower = (originalName || '').toLowerCase();
  if (mimetype === 'application/pdf' || lower.endsWith('.pdf')) {
    const out = await pdfParse(buffer);
    return out.text || '';
  }
  if (
    mimetype === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ||
    lower.endsWith('.docx')
  ) {
    const out = await mammoth.extractRawText({ buffer });
    return out.value || '';
  }
  if (mimetype === 'text/plain' || lower.endsWith('.txt') || lower.endsWith('.md')) {
    return buffer.toString('utf-8');
  }
  if (mimetype && mimetype.startsWith('image/')) {
    const res = await ai.models.generateContent({
      model: VISION_MODEL,
      contents: [
        {
          inlineData: { data: buffer.toString('base64'), mimeType: mimetype },
        },
        `Eres un nutricionista experto en cocina peruana. Analiza esta imagen y extrae, en español, toda la información útil para una base de datos de alimentación saludable y dietas para bajar de peso. Si ves un plato o alimento, indica:
- Nombre probable del plato (especialmente si es peruano: ceviche, lomo saltado, ají de gallina, quinua, etc.).
- Ingredientes visibles y estimados.
- Macronutrientes aproximados (calorías, proteínas, carbohidratos, grasas) por porción.
- Aporte nutricional, vitaminas y minerales destacados.
- Si es recomendable o no para bajar de peso, y por qué.
- Sugerencias de versiones más saludables o tamaños de porción.
- Cualquier texto visible (etiquetas, recetas, tablas nutricionales) transcrito literalmente.
Si no es comida, describe la imagen normalmente. Sé exhaustivo: este texto se usará para búsqueda semántica.`,
      ],
    });
    return res.text || '';
  }
  throw new Error(`Tipo de archivo no soportado: ${mimetype || lower}`);
}

async function extractFromUrl(url) {
  const r = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 AlimentacionPeruanaBot/1.0' },
  });
  if (!r.ok) throw new Error(`No se pudo descargar (${r.status})`);
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/pdf')) {
    const buf = Buffer.from(await r.arrayBuffer());
    return { text: (await pdfParse(buf)).text || '', name: url };
  }
  const html = await r.text();
  const $ = cheerio.load(html);
  $('script, style, noscript, header, footer, nav, svg').remove();
  const title = $('title').first().text().trim() || url;
  const text = $('body').text().replace(/\s+/g, ' ').trim();
  return { text, name: title };
}

async function ingest(sourceType, sourceName, text) {
  const chunks = chunkText(text);
  if (chunks.length === 0) throw new Error('No se extrajo texto del contenido.');

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const docRes = await client.query(
      'INSERT INTO documents (source_type, source_name) VALUES ($1, $2) RETURNING id',
      [sourceType, sourceName]
    );
    const docId = docRes.rows[0].id;
    for (let idx = 0; idx < chunks.length; idx++) {
      const vec = await embed(chunks[idx]);
      await client.query(
        'INSERT INTO chunks (document_id, chunk_index, content, embedding) VALUES ($1, $2, $3, $4::vector)',
        [docId, idx, chunks[idx], toVectorLiteral(vec)]
      );
    }
    await client.query('COMMIT');
    return { documentId: docId, chunks: chunks.length };
  } catch (e) {
    await client.query('ROLLBACK');
    throw e;
  } finally {
    client.release();
  }
}

app.post('/api/upload', upload.single('file'), async (req, res) => {
  try {
    if (!req.file) return res.status(400).json({ error: 'No se recibió archivo.' });
    const text = await extractFromBuffer(req.file.buffer, req.file.mimetype, req.file.originalname);
    const result = await ingest('file', req.file.originalname, text);
    res.json(result);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/url', async (req, res) => {
  try {
    const { url } = req.body || {};
    if (!url) return res.status(400).json({ error: 'Falta url.' });
    const { text, name } = await extractFromUrl(url);
    const result = await ingest('url', name + ' (' + url + ')', text);
    res.json(result);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/text', async (req, res) => {
  try {
    const { title, text } = req.body || {};
    if (!text) return res.status(400).json({ error: 'Falta texto.' });
    const result = await ingest('text', title || 'Nota sin título', text);
    res.json(result);
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

async function semanticSearch(query, k) {
  const vec = await embed(query);
  const limit = Math.min(Math.max(parseInt(k) || 5, 1), 20);
  const sql = `
    SELECT c.id, c.content, c.chunk_index,
           d.id AS document_id, d.source_type, d.source_name,
           1 - (c.embedding <=> $1::vector) AS similarity
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    ORDER BY c.embedding <=> $1::vector
    LIMIT $2`;
  const r = await pool.query(sql, [toVectorLiteral(vec), limit]);
  return r.rows;
}

app.get('/api/search', async (req, res) => {
  try {
    const query = req.query.q || req.query.query;
    if (!query) return res.status(400).json({ error: 'Falta el parámetro q.' });
    const results = await semanticSearch(String(query), req.query.k);
    res.json({ query, results });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

app.post('/api/search', async (req, res) => {
  try {
    const { query, k } = req.body || {};
    if (!query) return res.status(400).json({ error: 'Falta query.' });
    const results = await semanticSearch(query, k);
    res.json({ query, results });
  } catch (e) {
    console.error(e);
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/documents', async (_req, res) => {
  try {
    const r = await pool.query(`
      SELECT d.id, d.source_type, d.source_name, d.created_at,
             COUNT(c.id)::int AS chunks
      FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
      GROUP BY d.id ORDER BY d.created_at DESC`);
    res.json({ documents: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.delete('/api/documents/:id', async (req, res) => {
  try {
    await pool.query('DELETE FROM documents WHERE id = $1', [req.params.id]);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api', (_req, res) => {
  res.json({
    name: 'Alimentación Peruana — API de búsqueda semántica',
    endpoints: {
      'GET  /api/search?q=texto&k=5': 'Búsqueda semántica (recomendado para integraciones).',
      'POST /api/search': 'Body JSON: { "query": "texto", "k": 5 }',
      'POST /api/text': 'Body JSON: { "title": "...", "text": "..." } — guarda una nota.',
      'POST /api/url': 'Body JSON: { "url": "https://..." } — descarga e indexa una página.',
      'POST /api/upload': 'multipart/form-data con campo "file" (PDF, DOCX, imagen, txt, md).',
      'GET  /api/documents': 'Lista todos los documentos guardados.',
      'DELETE /api/documents/:id': 'Elimina un documento y sus fragmentos.',
    },
    cors: 'habilitado para todos los orígenes',
    response_format: {
      results: [
        { id: 1, document_id: 1, source_type: 'file|url|text', source_name: '...', content: '...', similarity: 0.87 },
      ],
    },
  });
});

app.listen(PORT, HOST, () => {
  console.log(`Vector DB server running at http://${HOST}:${PORT}`);
});
