import io
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from urllib.parse import parse_qs, quote, unquote, urlparse

import bcrypt
import mammoth
import psycopg
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session
from google import genai
from google.genai import types
from psycopg.rows import dict_row
from pypdf import PdfReader


load_dotenv()

PORT = int(os.getenv("PORT", "5000"))
HOST = "0.0.0.0"
EMBED_MODEL = "gemini-embedding-001"
VISION_MODEL = "gemini-2.5-flash"
TEXT_MODEL = "gemini-2.5-flash"
SIMILARITY_THRESHOLD = 0.55
MAX_FILE_SIZE = 25 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 AlimentacionPeruanaBot/1.0"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

IMAGE_ANALYSIS_PROMPT = """Eres un curador experto de una base de conocimiento multimedia. Analiza esta imagen y extrae, en espanol, toda la informacion util para indexarla y poder recuperarla despues.
Si es comida o nutricion, incluye nombre probable, ingredientes, calorias/macronutrientes aproximados y si ayuda o no a bajar de peso.
Si es una escena de pelicula o serie, describe personajes, accion, tono, escenario, texto visible y cualquier detalle relevante.
Si es una captura de codigo o tecnologia, transcribe el texto/codigo visible y resume el tema tecnico.
Si pertenece a otro tema, describe con el mayor detalle posible.
Devuelve texto claro, exhaustivo y util para busqueda semantica."""

FALLBACK_PROMPT_TEMPLATE = """Eres un asistente experto en organizar conocimiento de distintos temas.
La siguiente consulta no se encontro en la base de datos local: "{query}".
Responde en espanol con informacion clara y util sobre el tema.
Si es nutricion o alimentacion, incluye beneficios, riesgos y recomendaciones practicas.
Si es cine o escenas de peliculas, incluye contexto, genero, subgenero y rasgos distintivos.
Si es tecnologia o programacion, explica conceptos, ejemplos y aplicaciones practicas.
Si pertenece a otra categoria, responde igual con la mejor informacion disponible.
Devuelve solo texto plano, sin markdown."""

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS admins (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS companies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS companies_name_unique_idx
    ON companies (LOWER(name));

CREATE TABLE IF NOT EXISTS seller_profiles (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'vendedor',
    password_hash TEXT NOT NULL,
    company_id BIGINT REFERENCES companies(id) ON DELETE RESTRICT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE seller_profiles ALTER COLUMN company_id DROP NOT NULL;
ALTER TABLE seller_profiles ADD COLUMN IF NOT EXISTS email TEXT;

CREATE INDEX IF NOT EXISTS seller_profiles_company_idx ON seller_profiles(company_id);
CREATE INDEX IF NOT EXISTS seller_profiles_role_idx ON seller_profiles(role);

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    published BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS submitter_email TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS original_url TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_title TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_description TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_published_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS embed_html TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS approval_notified_at TIMESTAMPTZ;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS trailer_url TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS trailer_embed_html TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_trending BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY,
    parent_id BIGINT REFERENCES categories(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE categories ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS created_by_submitter_email TEXT;

UPDATE categories
SET approved_at = COALESCE(approved_at, NOW())
WHERE approved = TRUE AND approved_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS categories_unique_branch_name_idx
    ON categories (COALESCE(parent_id, 0), LOWER(name));
CREATE INDEX IF NOT EXISTS categories_parent_idx ON categories(parent_id);

CREATE TABLE IF NOT EXISTS document_categories (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    PRIMARY KEY (document_id, category_id)
);

CREATE TABLE IF NOT EXISTS platforms (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS platforms_name_unique_idx
    ON platforms (LOWER(name));

CREATE TABLE IF NOT EXISTS document_platforms (
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    platform_id BIGINT NOT NULL REFERENCES platforms(id) ON DELETE RESTRICT,
    PRIMARY KEY (document_id, platform_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(768) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);
"""

CATEGORY_CTE = """
WITH RECURSIVE category_tree AS (
    SELECT
        c.id,
        c.parent_id,
        c.name,
        c.description,
        c.approved,
        c.approved_at,
        c.created_by_submitter_email,
        c.created_at,
        c.updated_at,
        c.name::text AS path,
        0 AS depth
    FROM categories c
    WHERE c.parent_id IS NULL

    UNION ALL

    SELECT
        c.id,
        c.parent_id,
        c.name,
        c.description,
        c.approved,
        c.approved_at,
        c.created_by_submitter_email,
        c.created_at,
        c.updated_at,
        ct.path || ' > ' || c.name AS path,
        ct.depth + 1 AS depth
    FROM categories c
    JOIN category_tree ct ON c.parent_id = ct.id
)
"""

app = Flask(__name__, static_folder="public", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE
app.secret_key = os.getenv("SESSION_SECRET", "dev-secret-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("NODE_ENV") == "production"


def database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL no configurada.")
    return value


def db_connection():
    return psycopg.connect(database_url(), row_factory=dict_row)


def ai_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY no configurada.")
    return genai.Client(api_key=api_key)


def request_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT}


def normalize_optional_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def normalize_username(value: str | None) -> str | None:
    text = normalize_optional_text(value)
    return text.lower() if text else None


def clean_email(value: str | None) -> str:
    email = (value or "").strip().lower()
    if not email:
        raise RuntimeError("Falta correo del remitente.")
    if not EMAIL_RE.match(email):
        raise RuntimeError("El correo del remitente no es valido.")
    return email


def clean_optional_email(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if not EMAIL_RE.match(text):
        raise RuntimeError("El correo no es valido.")
    return text


def resolve_submitter_email(value: str | None) -> str:
    explicit_email = clean_optional_email(value)
    if explicit_email:
        return explicit_email
    manager_email = clean_optional_email(session.get("content_manager_email"))
    if manager_email:
        return manager_email
    raise RuntimeError("Falta correo del remitente.")


def parse_optional_int(value, field_name: str):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field_name} invalido.") from exc


def parse_external_datetime(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_category_ids(payload) -> list[int]:
    raw_ids = None
    if hasattr(payload, "get"):
        raw_ids = payload.get("categoryIds")
        if raw_ids is None:
            raw_single = payload.get("categoryId")
            raw_ids = [] if raw_single in (None, "") else [raw_single]
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]

    result: list[int] = []
    seen: set[int] = set()
    for raw_id in raw_ids:
        if raw_id in (None, ""):
            continue
        try:
            category_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Categoria invalida.") from exc
        if category_id not in seen:
            result.append(category_id)
            seen.add(category_id)
    return result


def parse_platform_ids(payload) -> list[int]:
    raw_ids = None
    if hasattr(payload, "get"):
        raw_ids = payload.get("platformIds")
        if raw_ids is None:
            raw_single = payload.get("platformId")
            raw_ids = [] if raw_single in (None, "") else [raw_single]
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]

    result: list[int] = []
    seen: set[int] = set()
    for raw_id in raw_ids:
        if raw_id in (None, ""):
            continue
        try:
            platform_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Plataforma invalida.") from exc
        if platform_id not in seen:
            result.append(platform_id)
            seen.add(platform_id)
    return result


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    clean = " ".join(text.split()).strip()
    if not clean:
        return []
    chunks: list[str] = []
    index = 0
    step = max(size - overlap, 1)
    while index < len(clean):
        chunks.append(clean[index:index + size])
        index += step
    return chunks


def embed(text: str) -> list[float]:
    result = ai_client().models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=768),
    )
    values = None
    if getattr(result, "embeddings", None):
        values = result.embeddings[0].values
    elif getattr(result, "embedding", None):
        values = result.embedding.values
    if not values:
        raise RuntimeError("No embedding returned by Gemini")
    return [float(value) for value in values]


def to_vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.6f}" for value in values) + "]"


def extract_pdf(buffer: bytes) -> str:
    reader = PdfReader(io.BytesIO(buffer))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_docx(buffer: bytes) -> str:
    return mammoth.extract_raw_text(io.BytesIO(buffer)).value or ""


def extract_from_buffer(buffer: bytes, mimetype: str | None, original_name: str | None) -> str:
    lower = (original_name or "").lower()
    if mimetype == "application/pdf" or lower.endswith(".pdf"):
        return extract_pdf(buffer)
    if (
        mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or lower.endswith(".docx")
    ):
        return extract_docx(buffer)
    if mimetype == "text/plain" or lower.endswith(".txt") or lower.endswith(".md"):
        return buffer.decode("utf-8", errors="replace")
    if mimetype and mimetype.startswith("image/"):
        response = ai_client().models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=buffer, mime_type=mimetype),
                IMAGE_ANALYSIS_PROMPT,
            ],
        )
        return response.text or ""
    raise RuntimeError(f"Tipo de archivo no soportado: {mimetype or lower}")


def fetch_page(url: str) -> tuple[requests.Response, BeautifulSoup]:
    response = requests.get(url, headers=request_headers(), timeout=30)
    if not response.ok:
        raise RuntimeError(f"No se pudo descargar ({response.status_code})")
    return response, BeautifulSoup(response.text, "html.parser")


def soup_meta_content(soup: BeautifulSoup, key: str) -> str | None:
    for attr_name in ("property", "name"):
        tag = soup.find("meta", attrs={attr_name: key})
        content = tag.get("content") if tag else None
        if content and content.strip():
            return content.strip()
    return None


def extract_text_from_soup(soup: BeautifulSoup) -> str:
    soup_copy = BeautifulSoup(str(soup), "html.parser")
    for tag_name in ("script", "style", "noscript", "header", "footer", "nav", "svg"):
        for node in soup_copy.find_all(tag_name):
            node.decompose()
    return " ".join(soup_copy.get_text(" ").split()).strip()


def detect_video_provider(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if "tiktok.com" in host:
        return "tiktok"
    if host == "fb.watch" or "facebook.com" in host:
        return "facebook"
    if host in {"youtube.com", "m.youtube.com", "youtu.be"}:
        return "youtube"
    return None


def extract_video_url_from_embed(embed_html: str) -> str:
    soup = BeautifulSoup(embed_html, "html.parser")

    blockquote = soup.find("blockquote")
    if blockquote:
        cite = normalize_optional_text(blockquote.get("cite"))
        if cite:
            return cite
        link = blockquote.find("a", href=True)
        if link and normalize_optional_text(link.get("href")):
            return normalize_optional_text(link.get("href")) or ""

    iframe = soup.find("iframe")
    if iframe:
        src = normalize_optional_text(iframe.get("src"))
        if src:
            parsed = urlparse(src)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if "facebook.com" in host:
                href = parse_qs(parsed.query).get("href", [None])[0]
                if href:
                    return unquote(href)
            if host in {"youtube.com", "m.youtube.com", "youtube-nocookie.com"} and parsed.path.startswith("/embed/"):
                video_id = parsed.path.split("/embed/", 1)[1].split("/", 1)[0]
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"
            if "tiktok.com" in host or host == "youtu.be":
                return src

    anchor = soup.find("a", href=True)
    if anchor and normalize_optional_text(anchor.get("href")):
        return normalize_optional_text(anchor.get("href")) or ""

    raise RuntimeError("No se pudo resolver la URL original desde el embed.")


def sanitize_video_embed_html(embed_html: str) -> str:
    soup = BeautifulSoup(embed_html, "html.parser")
    iframe = soup.find("iframe")
    if iframe:
        return str(iframe)
    blockquote = soup.find("blockquote")
    if blockquote:
        return str(blockquote)
    raise RuntimeError("El embed debe contener un iframe o blockquote valido.")


def fetch_tiktok_oembed(url: str) -> dict:
    response = requests.get(
        "https://www.tiktok.com/oembed",
        params={"url": url},
        headers=request_headers(),
        timeout=30,
    )
    if not response.ok:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def build_facebook_embed(url: str) -> str:
    encoded_url = quote(url, safe="")
    return (
        '<iframe src="https://www.facebook.com/plugins/video.php?href='
        f'{encoded_url}&show_text=false&width=560" '
        'width="560" height="315" style="border:none;overflow:hidden" '
        'scrolling="no" frameborder="0" allowfullscreen="true" '
        'allow="autoplay; clipboard-write; encrypted-media; picture-in-picture; web-share"></iframe>'
    )


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0] or None
    if host in {"youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            parts = [part for part in parsed.path.split("/") if part]
            return parts[-1] if parts else None
    return None


def build_youtube_embed(url: str) -> str:
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise RuntimeError("No se pudo identificar el video de YouTube.")
    return (
        f'<iframe src="https://www.youtube.com/embed/{quote(video_id, safe="")}" '
        'title="Trailer" width="560" height="315" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
        'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>'
    )


def extract_video_submission(url: str) -> dict:
    provider = detect_video_provider(url)
    if not provider:
        raise RuntimeError("Solo se permiten videos publicos de TikTok, Facebook o YouTube.")

    response, soup = fetch_page(url)
    title = (
        soup_meta_content(soup, "og:title")
        or soup_meta_content(soup, "twitter:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else "")
        or url
    )
    description = (
        soup_meta_content(soup, "og:description")
        or soup_meta_content(soup, "twitter:description")
        or ""
    )
    published_at = parse_external_datetime(
        soup_meta_content(soup, "article:published_time")
        or soup_meta_content(soup, "og:updated_time")
        or soup_meta_content(soup, "video:release_date")
    )
    embed_html = None

    if provider == "tiktok":
        oembed = fetch_tiktok_oembed(url)
        embed_html = oembed.get("html")
        title = oembed.get("title") or title
    elif provider == "facebook":
        embed_html = build_facebook_embed(url)
    elif provider == "youtube":
        embed_html = build_youtube_embed(url)

    if not embed_html:
        raise RuntimeError("No se pudo obtener un embed valido para este video.")

    index_text = "\n".join(
        value
        for value in [
            "Video social indexado.",
            f"Proveedor: {provider}.",
            f"Titulo: {title}",
            f"Descripcion: {description}" if description else "",
            f"URL original: {url}",
            f"Fecha publicada: {published_at.isoformat()}" if published_at else "",
            f"Fuente resuelta: {response.url}",
        ]
        if value
    )

    return {
        "source_type": "video",
        "source_name": title,
        "text": index_text,
        "original_url": url,
        "external_title": title,
        "external_description": description,
        "external_published_at": published_at,
        "embed_html": embed_html,
    }


def extract_video_submission_from_embed(embed_html: str) -> dict:
    if not normalize_optional_text(embed_html):
        raise RuntimeError("Falta el embed del video.")
    url = extract_video_url_from_embed(embed_html)
    video = extract_video_submission(url)
    provided_embed = sanitize_video_embed_html(embed_html)
    if provided_embed:
        video["embed_html"] = provided_embed
    return video


def extract_from_url(url: str) -> tuple[str, str]:
    response = requests.get(url, headers=request_headers(), timeout=30)
    if not response.ok:
        raise RuntimeError(f"No se pudo descargar ({response.status_code})")
    content_type = response.headers.get("content-type", "")
    if "application/pdf" in content_type:
        return extract_pdf(response.content), url
    soup = BeautifulSoup(response.text, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else url
    text = extract_text_from_soup(soup)
    return text, title


def fetch_category_rows(conn, include_pending: bool = False) -> list[dict]:
    rows = conn.execute(
        CATEGORY_CTE
        + """
        SELECT id, parent_id, name, description, approved, approved_at, created_by_submitter_email, created_at, updated_at, path, depth
        FROM category_tree
        WHERE (%s OR approved = TRUE)
        ORDER BY path
        """,
        (include_pending,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_category_tree(category_rows: list[dict]) -> list[dict]:
    nodes = {
        row["id"]: {
            **row,
            "children": [],
        }
        for row in category_rows
    }
    roots: list[dict] = []
    for row in category_rows:
        node = nodes[row["id"]]
        parent_id = row["parent_id"]
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


def ensure_category_ids_exist(conn, category_ids: list[int]) -> None:
    if not category_ids:
        return
    found_rows = conn.execute(
        "SELECT id FROM categories WHERE id = ANY(%s)",
        (category_ids,),
    ).fetchall()
    found_ids = {row["id"] for row in found_rows}
    missing = [str(category_id) for category_id in category_ids if category_id not in found_ids]
    if missing:
        raise RuntimeError("Categoria no encontrada: " + ", ".join(missing))


def set_document_categories(cur, document_id: int, category_ids: list[int]) -> None:
    cur.execute("DELETE FROM document_categories WHERE document_id = %s", (document_id,))
    for category_id in category_ids:
        cur.execute(
            "INSERT INTO document_categories (document_id, category_id) VALUES (%s, %s)",
            (document_id, category_id),
        )


def fetch_document_categories_map(conn, document_ids: list[int], include_pending: bool = False) -> dict[int, list[dict]]:
    if not document_ids:
        return {}
    rows = conn.execute(
        CATEGORY_CTE
        + """
        SELECT
            dc.document_id,
            ct.id,
            ct.parent_id,
            ct.name,
            ct.description,
            ct.approved,
            ct.path,
            ct.depth
        FROM document_categories dc
        JOIN category_tree ct ON ct.id = dc.category_id
        WHERE dc.document_id = ANY(%s)
          AND (%s OR ct.approved = TRUE)
        ORDER BY ct.path
        """,
        (document_ids, include_pending),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for row in rows:
        result.setdefault(row["document_id"], []).append(
            {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "name": row["name"],
                "description": row["description"],
                "approved": row["approved"],
                "path": row["path"],
                "depth": row["depth"],
            }
        )
    return result


def attach_categories(conn, items: list[dict], document_id_key: str = "id", include_pending: bool = False) -> list[dict]:
    document_ids = []
    for item in items:
        document_id = item.get(document_id_key)
        if document_id is not None:
            document_ids.append(document_id)
    category_map = fetch_document_categories_map(conn, document_ids, include_pending=include_pending)
    for item in items:
        document_id = item.get(document_id_key)
        item["categories"] = category_map.get(document_id, [])
    return items


def fetch_company_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, description, created_at, updated_at
        FROM companies
        ORDER BY LOWER(name)
        """
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_company_ids_exist(conn, company_ids: list[int]) -> None:
    if not company_ids:
        return
    found_rows = conn.execute(
        "SELECT id FROM companies WHERE id = ANY(%s)",
        (company_ids,),
    ).fetchall()
    found_ids = {row["id"] for row in found_rows}
    missing = [str(company_id) for company_id in company_ids if company_id not in found_ids]
    if missing:
        raise RuntimeError("Empresa no encontrada: " + ", ".join(missing))


def ensure_company_name_available(conn, name: str, exclude_id: int | None = None) -> None:
    if exclude_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM companies
            WHERE LOWER(name) = LOWER(%s)
            """,
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM companies
            WHERE LOWER(name) = LOWER(%s)
              AND id <> %s
            """,
            (name, exclude_id),
        ).fetchone()
    if row:
        raise RuntimeError("Ya existe una empresa con ese nombre.")


def fetch_seller_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            sp.id,
            sp.username,
            sp.full_name,
            sp.email,
            sp.role,
            sp.active,
            sp.company_id,
            c.name AS company_name,
            sp.created_at,
            sp.updated_at
        FROM seller_profiles sp
        LEFT JOIN companies c ON c.id = sp.company_id
        ORDER BY LOWER(COALESCE(c.name, '')), LOWER(sp.full_name), LOWER(sp.username)
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_platform_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, description, created_at, updated_at
        FROM platforms
        ORDER BY LOWER(name)
        """
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_platform_ids_exist(conn, platform_ids: list[int]) -> None:
    if not platform_ids:
        return
    found_rows = conn.execute(
        "SELECT id FROM platforms WHERE id = ANY(%s)",
        (platform_ids,),
    ).fetchall()
    found_ids = {row["id"] for row in found_rows}
    missing = [str(platform_id) for platform_id in platform_ids if platform_id not in found_ids]
    if missing:
        raise RuntimeError("Plataforma no encontrada: " + ", ".join(missing))


def set_document_platforms(cur, document_id: int, platform_ids: list[int]) -> None:
    cur.execute("DELETE FROM document_platforms WHERE document_id = %s", (document_id,))
    for platform_id in platform_ids:
        cur.execute(
            "INSERT INTO document_platforms (document_id, platform_id) VALUES (%s, %s)",
            (document_id, platform_id),
        )


def fetch_document_platforms_map(conn, document_ids: list[int]) -> dict[int, list[dict]]:
    if not document_ids:
        return {}
    rows = conn.execute(
        """
        SELECT dp.document_id, p.id, p.name, p.description
        FROM document_platforms dp
        JOIN platforms p ON p.id = dp.platform_id
        WHERE dp.document_id = ANY(%s)
        ORDER BY LOWER(p.name)
        """,
        (document_ids,),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for row in rows:
        result.setdefault(row["document_id"], []).append(
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
            }
        )
    return result


def attach_platforms(conn, items: list[dict], document_id_key: str = "id") -> list[dict]:
    document_ids = []
    for item in items:
        document_id = item.get(document_id_key)
        if document_id is not None:
            document_ids.append(document_id)
    platform_map = fetch_document_platforms_map(conn, document_ids)
    for item in items:
        document_id = item.get(document_id_key)
        item["platforms"] = platform_map.get(document_id, [])
    return items


def build_trailer_metadata(trailer_url: str | None) -> tuple[str | None, str | None]:
    cleaned = normalize_optional_text(trailer_url)
    if not cleaned:
        return None, None
    trailer = extract_video_submission(cleaned)
    return cleaned, trailer["embed_html"]


def validate_category_parent(conn, category_id: int | None, parent_id: int | None) -> None:
    if parent_id is None:
        return
    parent = conn.execute("SELECT id FROM categories WHERE id = %s", (parent_id,)).fetchone()
    if not parent:
        raise RuntimeError("La categoria padre no existe.")
    if category_id is None:
        return
    if parent_id == category_id:
        raise RuntimeError("Una categoria no puede ser su propia rama padre.")
    descendants = conn.execute(
        """
        WITH RECURSIVE descendants AS (
            SELECT id FROM categories WHERE parent_id = %s
            UNION ALL
            SELECT c.id
            FROM categories c
            JOIN descendants d ON c.parent_id = d.id
        )
        SELECT id FROM descendants
        """,
        (category_id,),
    ).fetchall()
    if parent_id in {row["id"] for row in descendants}:
        raise RuntimeError("No puedes mover una categoria dentro de una de sus subcategorias.")


def ensure_category_name_available(conn, parent_id: int | None, name: str, exclude_id: int | None = None) -> None:
    if exclude_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM categories
            WHERE COALESCE(parent_id, 0) = COALESCE(%s, 0)
              AND LOWER(name) = LOWER(%s)
            """,
            (parent_id, name),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM categories
            WHERE COALESCE(parent_id, 0) = COALESCE(%s, 0)
              AND LOWER(name) = LOWER(%s)
              AND id <> %s
            """,
            (parent_id, name, exclude_id),
        ).fetchone()
    if row:
        raise RuntimeError("Ya existe una categoria con ese nombre en la misma rama.")


def ensure_platform_name_available(conn, name: str, exclude_id: int | None = None) -> None:
    if exclude_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM platforms
            WHERE LOWER(name) = LOWER(%s)
            """,
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM platforms
            WHERE LOWER(name) = LOWER(%s)
              AND id <> %s
            """,
            (name, exclude_id),
        ).fetchone()
    if row:
        raise RuntimeError("Ya existe una plataforma con ese nombre.")


def ensure_seller_username_available(conn, username: str, exclude_id: int | None = None) -> None:
    if exclude_id is None:
        row = conn.execute(
            """
            SELECT id
            FROM seller_profiles
            WHERE LOWER(username) = LOWER(%s)
            """,
            (username,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id
            FROM seller_profiles
            WHERE LOWER(username) = LOWER(%s)
              AND id <> %s
            """,
            (username, exclude_id),
        ).fetchone()
    if row:
        raise RuntimeError("Ya existe un vendedor con ese usuario.")


def create_category_record(
    conn,
    *,
    parent_id: int | None,
    name: str,
    description: str | None,
    approved: bool,
    created_by_submitter_email: str | None = None,
):
    approved_at = datetime.now(timezone.utc) if approved else None
    return conn.execute(
        """
        INSERT INTO categories (
            parent_id,
            name,
            description,
            approved,
            approved_at,
            created_by_submitter_email
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, parent_id, name, description, approved, approved_at, created_by_submitter_email, created_at, updated_at
        """,
        (parent_id, name, description, approved, approved_at, created_by_submitter_email),
    ).fetchone()


def resolve_submission_categories(conn, payload, submitter_email: str) -> list[int]:
    category_ids = parse_category_ids(payload)
    proposed_name = normalize_optional_text(payload.get("proposedCategoryName")) if hasattr(payload, "get") else None
    proposed_description = normalize_optional_text(payload.get("proposedCategoryDescription")) if hasattr(payload, "get") else None
    proposed_parent_id = parse_optional_int(payload.get("proposedCategoryParentId"), "proposedCategoryParentId") if hasattr(payload, "get") else None

    if not proposed_name:
        ensure_category_ids_exist(conn, category_ids)
        return category_ids

    if proposed_parent_id is None and category_ids:
        proposed_parent_id = category_ids[0]

    validate_category_parent(conn, None, proposed_parent_id)
    ensure_category_name_available(conn, proposed_parent_id, proposed_name)
    proposal = create_category_record(
        conn,
        parent_id=proposed_parent_id,
        name=proposed_name,
        description=proposed_description,
        approved=False,
        created_by_submitter_email=submitter_email,
    )
    return [proposal["id"]]


def ingest(
    source_type: str,
    source_name: str,
    text: str,
    submitter_email: str | None = None,
    original_url: str | None = None,
    external_title: str | None = None,
    external_description: str | None = None,
    external_published_at=None,
    embed_html: str | None = None,
    category_ids: list[int] | None = None,
    platform_ids: list[int] | None = None,
    trailer_url: str | None = None,
    trailer_embed_html: str | None = None,
    is_trending: bool = False,
) -> dict:
    category_ids = category_ids or []
    platform_ids = platform_ids or []
    chunks = chunk_text(text)
    if not chunks:
        raise RuntimeError("No se extrajo texto del contenido.")
    published = source_type == "ai"
    with db_connection() as conn:
        ensure_category_ids_exist(conn, category_ids)
        ensure_platform_ids_exist(conn, platform_ids)
        with conn.cursor() as cur:
            doc_row = cur.execute(
                """
                INSERT INTO documents (
                    source_type,
                    source_name,
                    published,
                    submitter_email,
                    original_url,
                    external_title,
                    external_description,
                    external_published_at,
                    embed_html,
                    trailer_url,
                    trailer_embed_html,
                    is_trending
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    source_type,
                    source_name,
                    published,
                    submitter_email,
                    original_url,
                    external_title,
                    external_description,
                    external_published_at,
                    embed_html,
                    trailer_url,
                    trailer_embed_html,
                    is_trending,
                ),
            ).fetchone()
            document_id = doc_row["id"]
            set_document_categories(cur, document_id, category_ids)
            set_document_platforms(cur, document_id, platform_ids)
            for chunk_index, content in enumerate(chunks):
                vector = embed(content)
                cur.execute(
                    """
                    INSERT INTO chunks (document_id, chunk_index, content, embedding)
                    VALUES (%s, %s, %s, %s::vector)
                    """,
                    (document_id, chunk_index, content, to_vector_literal(vector)),
                )
    return {
        "documentId": document_id,
        "chunks": len(chunks),
        "published": published,
        "submitterEmail": submitter_email,
        "categoryIds": category_ids,
        "platformIds": platform_ids,
        "isTrending": is_trending,
    }


def search_by_vector(vector: list[float], k: int | str | None) -> list[dict]:
    try:
        limit = int(k or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 20)
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                c.id,
                c.content,
                c.chunk_index,
                d.id AS document_id,
                d.source_type,
                d.source_name,
                d.original_url,
                d.external_title,
                d.external_description,
                d.external_published_at,
                d.embed_html,
                d.trailer_url,
                d.trailer_embed_html,
                d.is_trending,
                1 - (c.embedding <=> %s::vector) AS similarity
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.published = TRUE
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (to_vector_literal(vector), to_vector_literal(vector), limit),
        ).fetchall()
        results = [dict(row) for row in rows]
        attach_categories(conn, results, document_id_key="document_id")
        attach_platforms(conn, results, document_id_key="document_id")
    return results


def ask_gemini_about_topic(query: str) -> str:
    prompt = FALLBACK_PROMPT_TEMPLATE.format(query=query)
    models = [TEXT_MODEL, "gemini-2.0-flash", "gemini-flash-latest"]
    last_error = None
    client = ai_client()
    for model in models:
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            if response.text:
                return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("Sin respuesta de IA")


def semantic_search(query: str, k: int | str | None) -> dict:
    vector = embed(query)
    results = search_by_vector(vector, k)
    generated = False
    best = float(results[0]["similarity"]) if results else 0.0
    if best < SIMILARITY_THRESHOLD:
        try:
            ai_text = ask_gemini_about_topic(query)
            if ai_text.strip():
                ingest("ai", f"Generado por IA: {query}", ai_text)
                results = search_by_vector(vector, k)
                generated = True
        except Exception as exc:  # noqa: BLE001
            app.logger.error("Fallback IA fallo: %s", exc)
    return {"results": results, "generated": generated}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def is_bcrypt_hash(value: str) -> bool:
    return value.startswith("$2a$") or value.startswith("$2b$") or value.startswith("$2y$")


def ensure_seed_admin() -> None:
    username = os.getenv("ADMIN_USERNAME")
    password = os.getenv("ADMIN_PASSWORD")
    if not username or not password:
        return
    password_hash = hash_password(password)
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO admins (username, password_hash)
            VALUES (%s, %s)
            ON CONFLICT (username)
            DO UPDATE SET password_hash = EXCLUDED.password_hash
            """,
            (username, password_hash),
        )
    app.logger.info('Admin "%s" listo.', username)


def ensure_default_platforms() -> None:
    defaults = [
        ("Netflix", "Catalogo de Netflix"),
        ("Prime Video", "Catalogo de Prime Video"),
        ("Amazon", "Catalogo o compra en Amazon"),
        ("Movistar TV", "Catalogo de Movistar TV"),
        ("Disney+", "Catalogo de Disney+"),
        ("Max", "Catalogo de Max"),
    ]
    with db_connection() as conn:
        for name, description in defaults:
            conn.execute(
                """
                INSERT INTO platforms (name, description)
                VALUES (%s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, description),
            )


def admin_seed_configured() -> bool:
    return bool(os.getenv("ADMIN_USERNAME") and os.getenv("ADMIN_PASSWORD"))


def verify_admin_password(admin: dict, password: str) -> bool:
    stored_password = admin["password_hash"]
    if is_bcrypt_hash(stored_password):
        return bcrypt.checkpw(password.encode("utf-8"), stored_password.encode("utf-8"))

    if password != stored_password:
        return False

    upgraded_hash = hash_password(password)
    with db_connection() as conn:
        conn.execute(
            "UPDATE admins SET password_hash = %s WHERE id = %s",
            (upgraded_hash, admin["id"]),
        )
    admin["password_hash"] = upgraded_hash
    return True


def verify_seller_password(seller: dict, password: str) -> bool:
    stored_password = seller["password_hash"]
    if not is_bcrypt_hash(stored_password):
        return False
    return bcrypt.checkpw(password.encode("utf-8"), stored_password.encode("utf-8"))


def normalize_profile_role(value: str | None) -> str:
    role = normalize_optional_text(value) or "vendedor"
    role = role.lower().replace(" ", "_")
    if role not in {"vendedor", "gestor_de_contenido"}:
        raise RuntimeError("Rol no valido.")
    return role


def profile_role_label(role: str) -> str:
    return "gestor de contenido" if role == "gestor_de_contenido" else role


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USERNAME")))


def send_approval_notification(document: dict) -> dict:
    email = document.get("submitter_email")
    if not email:
        return {"attempted": False, "sent": False, "reason": "Documento sin correo de contacto."}
    if not smtp_configured():
        return {
            "attempted": False,
            "sent": False,
            "reason": "SMTP no configurado. El documento se publico, pero no se envio aviso por correo.",
        }

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() not in {"0", "false", "no"}
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM_EMAIL") or username

    message = EmailMessage()
    message["Subject"] = "Tu contenido fue aprobado"
    message["From"] = from_email
    message["To"] = email

    title = document.get("external_title") or document.get("source_name")
    source_type = document.get("source_type")
    original_url = document.get("original_url")
    body_lines = [
        "Hola,",
        "",
        "Tu contenido fue aprobado en la base de conocimiento.",
        f"Tipo: {source_type}",
        f"Titulo: {title}",
    ]
    if original_url:
        body_lines.append(f"URL original: {original_url}")
    body_lines += [
        "",
        "Gracias por tu aporte.",
    ]
    message.set_content("\n".join(body_lines))

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(message)
        return {"attempted": True, "sent": True}
    except Exception as exc:  # noqa: BLE001
        return {"attempted": True, "sent": False, "reason": str(exc)}


def require_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if session.get("admin_id"):
            return view_func(*args, **kwargs)
        return jsonify({"error": "No autenticado."}), 401

    return wrapped


def require_seller(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        seller_id = session.get("seller_id")
        if seller_id and session.get("seller_role") == "vendedor":
            with db_connection() as conn:
                seller = conn.execute(
                    """
                    SELECT
                        sp.id,
                        sp.username,
                        sp.full_name,
                        sp.role,
                        sp.active,
                        sp.company_id,
                        c.name AS company_name
                    FROM seller_profiles sp
                    JOIN companies c ON c.id = sp.company_id
                    WHERE sp.id = %s
                    """,
                    (seller_id,),
                ).fetchone()
            if seller and seller["active"] and seller["role"] == "vendedor":
                session["seller_username"] = seller["username"]
                session["seller_full_name"] = seller["full_name"]
                session["seller_role"] = seller["role"]
                session["seller_company_id"] = seller["company_id"]
                session["seller_company_name"] = seller["company_name"]
                return view_func(*args, **kwargs)
            clear_seller_session()
        return jsonify({"error": "Acceso solo para vendedores autorizados."}), 401

    return wrapped


def require_content_manager(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        profile_id = session.get("content_manager_id")
        if profile_id and session.get("content_manager_role") == "gestor_de_contenido":
            with db_connection() as conn:
                profile = conn.execute(
                    """
                    SELECT
                        sp.id,
                        sp.username,
                        sp.full_name,
                        sp.email,
                        sp.role,
                        sp.active,
                        sp.company_id,
                        c.name AS company_name
                    FROM seller_profiles sp
                    LEFT JOIN companies c ON c.id = sp.company_id
                    WHERE sp.id = %s
                    """,
                    (profile_id,),
                ).fetchone()
            if profile and profile["active"] and profile["role"] == "gestor_de_contenido":
                session["content_manager_username"] = profile["username"]
                session["content_manager_full_name"] = profile["full_name"]
                session["content_manager_email"] = profile["email"]
                session["content_manager_role"] = profile["role"]
                session["content_manager_company_id"] = profile["company_id"]
                session["content_manager_company_name"] = profile["company_name"]
                return view_func(*args, **kwargs)
            clear_content_manager_session()
        return jsonify({"error": "Acceso solo para gestores de contenido autorizados."}), 401

    return wrapped


def clear_admin_session() -> None:
    for key in ("admin_id", "admin_username"):
        session.pop(key, None)


def clear_seller_session() -> None:
    for key in ("seller_id", "seller_username", "seller_full_name", "seller_role", "seller_company_id", "seller_company_name"):
        session.pop(key, None)


def clear_content_manager_session() -> None:
    for key in (
        "content_manager_id",
        "content_manager_username",
        "content_manager_full_name",
        "content_manager_email",
        "content_manager_role",
        "content_manager_company_id",
        "content_manager_company_name",
    ):
        session.pop(key, None)


@app.before_request
def api_preflight():
    if request.path.startswith("/api") and request.method == "OPTIONS":
        return ("", 204)
    return None


@app.after_request
def add_headers(response):
    if os.getenv("NODE_ENV") != "production":
        response.headers["Cache-Control"] = "no-store"
    if request.path.startswith("/api"):
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.errorhandler(413)
def payload_too_large(_error):
    if request.path.startswith("/api"):
        return jsonify({"error": "El archivo supera el limite de 25 MB."}), 413
    return ("Payload Too Large", 413)


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/admin/login")
def admin_login():
    try:
        data = request.get_json(silent=True) or {}
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "Faltan datos."}), 400
        if admin_seed_configured():
            ensure_seed_admin()
        with db_connection() as conn:
            admin_count = conn.execute("SELECT COUNT(*) AS total FROM admins").fetchone()["total"]
            if admin_count == 0:
                return (
                    jsonify(
                        {
                            "error": (
                                "No hay ningun administrador configurado. Define ADMIN_USERNAME y "
                                "ADMIN_PASSWORD en el archivo .env y reinicia la aplicacion."
                            )
                        }
                    ),
                    503,
                )
            admin = conn.execute(
                "SELECT * FROM admins WHERE username = %s",
                (username,),
            ).fetchone()
        if not admin:
            return jsonify({"error": "Credenciales invalidas."}), 401
        valid = verify_admin_password(admin, password)
        if not valid:
            return jsonify({"error": "Credenciales invalidas."}), 401
        session["admin_id"] = admin["id"]
        session["admin_username"] = admin["username"]
        return jsonify({"ok": True, "username": admin["username"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/admin/logout")
def admin_logout():
    clear_admin_session()
    return jsonify({"ok": True})


@app.get("/api/admin/me")
def admin_me():
    if session.get("admin_id"):
        return jsonify({"authenticated": True, "username": session.get("admin_username")})
    return jsonify({"authenticated": False})


@app.post("/api/seller/login")
def seller_login():
    try:
        data = request.get_json(silent=True) or {}
        username = normalize_username(data.get("username"))
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "Faltan datos."}), 400
        with db_connection() as conn:
            seller = conn.execute(
                """
                SELECT
                    sp.*,
                    c.name AS company_name
                FROM seller_profiles sp
                LEFT JOIN companies c ON c.id = sp.company_id
                WHERE LOWER(sp.username) = LOWER(%s)
                """,
                (username,),
            ).fetchone()
        if not seller or not seller["active"] or seller["role"] != "vendedor":
            return jsonify({"error": "Credenciales invalidas."}), 401
        if not verify_seller_password(seller, password):
            return jsonify({"error": "Credenciales invalidas."}), 401
        clear_seller_session()
        session["seller_id"] = seller["id"]
        session["seller_username"] = seller["username"]
        session["seller_full_name"] = seller["full_name"]
        session["seller_role"] = seller["role"]
        session["seller_company_id"] = seller["company_id"]
        session["seller_company_name"] = seller["company_name"]
        return jsonify(
            {
                "ok": True,
                "username": seller["username"],
                "fullName": seller["full_name"],
                "role": seller["role"],
                "company": {"id": seller["company_id"], "name": seller["company_name"]},
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/seller/logout")
def seller_logout():
    clear_seller_session()
    return jsonify({"ok": True})


@app.get("/api/seller/me")
def seller_me():
    seller_id = session.get("seller_id")
    if seller_id and session.get("seller_role") == "vendedor":
        with db_connection() as conn:
            seller = conn.execute(
                """
                SELECT
                    sp.id,
                    sp.username,
                    sp.full_name,
                    sp.email,
                    sp.role,
                    sp.active,
                    sp.company_id,
                    c.name AS company_name
                FROM seller_profiles sp
                LEFT JOIN companies c ON c.id = sp.company_id
                WHERE sp.id = %s
                """,
                (seller_id,),
            ).fetchone()
        if seller and seller["active"] and seller["role"] == "vendedor":
            session["seller_username"] = seller["username"]
            session["seller_full_name"] = seller["full_name"]
            session["seller_role"] = seller["role"]
            session["seller_company_id"] = seller["company_id"]
            session["seller_company_name"] = seller["company_name"]
            return jsonify(
                {
                    "authenticated": True,
                    "id": seller["id"],
                    "username": seller["username"],
                    "fullName": seller["full_name"],
                    "role": seller["role"],
                    "company": {
                        "id": seller["company_id"],
                        "name": seller["company_name"],
                    },
                }
            )
        clear_seller_session()
    return jsonify({"authenticated": False})


@app.post("/api/content-manager/login")
def content_manager_login():
    try:
        data = request.get_json(silent=True) or {}
        username = normalize_username(data.get("username"))
        password = data.get("password")
        if not username or not password:
            return jsonify({"error": "Faltan datos."}), 400
        with db_connection() as conn:
            profile = conn.execute(
                """
                SELECT
                    sp.*,
                    c.name AS company_name
                FROM seller_profiles sp
                LEFT JOIN companies c ON c.id = sp.company_id
                WHERE LOWER(sp.username) = LOWER(%s)
                """,
                (username,),
            ).fetchone()
        if not profile or not profile["active"] or profile["role"] != "gestor_de_contenido":
            return jsonify({"error": "Credenciales invalidas."}), 401
        if not verify_seller_password(profile, password):
            return jsonify({"error": "Credenciales invalidas."}), 401
        clear_content_manager_session()
        session["content_manager_id"] = profile["id"]
        session["content_manager_username"] = profile["username"]
        session["content_manager_full_name"] = profile["full_name"]
        session["content_manager_email"] = profile["email"]
        session["content_manager_role"] = profile["role"]
        session["content_manager_company_id"] = profile["company_id"]
        session["content_manager_company_name"] = profile["company_name"]
        return jsonify(
            {
                "ok": True,
                "id": profile["id"],
                "username": profile["username"],
                "fullName": profile["full_name"],
                "email": profile["email"],
                "role": profile["role"],
                "company": {"id": profile["company_id"], "name": profile["company_name"]} if profile["company_id"] else None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/content-manager/logout")
def content_manager_logout():
    clear_content_manager_session()
    return jsonify({"ok": True})


@app.get("/api/content-manager/me")
def content_manager_me():
    profile_id = session.get("content_manager_id")
    if profile_id and session.get("content_manager_role") == "gestor_de_contenido":
        with db_connection() as conn:
            profile = conn.execute(
                """
                SELECT
                    sp.id,
                    sp.username,
                    sp.full_name,
                    sp.email,
                    sp.role,
                    sp.active,
                    sp.company_id,
                    c.name AS company_name
                FROM seller_profiles sp
                LEFT JOIN companies c ON c.id = sp.company_id
                WHERE sp.id = %s
                """,
                (profile_id,),
            ).fetchone()
        if profile and profile["active"] and profile["role"] == "gestor_de_contenido":
            session["content_manager_username"] = profile["username"]
            session["content_manager_full_name"] = profile["full_name"]
            session["content_manager_email"] = profile["email"]
            session["content_manager_role"] = profile["role"]
            session["content_manager_company_id"] = profile["company_id"]
            session["content_manager_company_name"] = profile["company_name"]
            return jsonify(
                {
                    "authenticated": True,
                    "id": profile["id"],
                    "username": profile["username"],
                    "fullName": profile["full_name"],
                    "email": profile["email"],
                    "role": profile["role"],
                    "company": {"id": profile["company_id"], "name": profile["company_name"]} if profile["company_id"] else None,
                }
            )
        clear_content_manager_session()
    return jsonify({"authenticated": False})


@app.get("/api/companies")
@require_admin
def list_companies():
    try:
        with db_connection() as conn:
            companies = fetch_company_rows(conn)
        return jsonify({"companies": companies})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/companies")
@require_admin
def create_company():
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        if not name:
            return jsonify({"error": "Falta nombre de empresa."}), 400
        with db_connection() as conn:
            ensure_company_name_available(conn, name)
            company = conn.execute(
                """
                INSERT INTO companies (name, description)
                VALUES (%s, %s)
                RETURNING id, name, description, created_at, updated_at
                """,
                (name, description),
            ).fetchone()
            companies = fetch_company_rows(conn)
        return jsonify({"ok": True, "company": company, "companies": companies})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/companies/<int:company_id>")
@require_admin
def update_company(company_id: int):
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        if not name:
            return jsonify({"error": "Falta nombre de empresa."}), 400
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM companies WHERE id = %s", (company_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Empresa no encontrada."}), 404
            ensure_company_name_available(conn, name, exclude_id=company_id)
            conn.execute(
                """
                UPDATE companies
                SET name = %s, description = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (name, description, company_id),
            )
            companies = fetch_company_rows(conn)
        return jsonify({"ok": True, "companies": companies})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/companies/<int:company_id>")
@require_admin
def delete_company(company_id: int):
    try:
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM companies WHERE id = %s", (company_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Empresa no encontrada."}), 404
            sellers_count = conn.execute(
                "SELECT COUNT(*) AS total FROM seller_profiles WHERE company_id = %s",
                (company_id,),
            ).fetchone()["total"]
            if sellers_count:
                return jsonify({"error": "No puedes eliminar una empresa con perfiles asignados."}), 409
            conn.execute("DELETE FROM companies WHERE id = %s", (company_id,))
            companies = fetch_company_rows(conn)
        return jsonify({"ok": True, "companies": companies})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/sellers")
@require_admin
def list_sellers():
    try:
        with db_connection() as conn:
            sellers = fetch_seller_rows(conn)
        return jsonify({"sellers": sellers})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/sellers")
@require_admin
def create_seller():
    try:
        data = request.get_json(silent=True) or {}
        username = normalize_username(data.get("username"))
        full_name = normalize_optional_text(data.get("fullName"))
        email = clean_optional_email(data.get("email"))
        role = normalize_profile_role(data.get("role"))
        password = data.get("password")
        company_id = parse_optional_int(data.get("companyId"), "companyId")
        active = parse_bool(data.get("active"))
        if not username or not full_name or not password:
            return jsonify({"error": "Faltan datos del perfil."}), 400
        with db_connection() as conn:
            if company_id is not None:
                ensure_company_ids_exist(conn, [company_id])
            if role == "vendedor" and company_id is None:
                return jsonify({"error": "El vendedor debe pertenecer a una empresa."}), 400
            if role == "gestor_de_contenido" and not email:
                return jsonify({"error": "El gestor de contenido debe tener un correo guardado."}), 400
            ensure_seller_username_available(conn, username)
            seller = conn.execute(
                """
                INSERT INTO seller_profiles (username, full_name, email, role, password_hash, company_id, active)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (username, full_name, email, role, hash_password(password), company_id, active),
            ).fetchone()
            sellers = fetch_seller_rows(conn)
        return jsonify({"ok": True, "sellerId": seller["id"], "sellers": sellers})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/sellers/<int:seller_id>")
@require_admin
def update_seller(seller_id: int):
    try:
        data = request.get_json(silent=True) or {}
        username = normalize_username(data.get("username"))
        full_name = normalize_optional_text(data.get("fullName"))
        email = clean_optional_email(data.get("email"))
        role = normalize_profile_role(data.get("role"))
        company_id = parse_optional_int(data.get("companyId"), "companyId")
        active = parse_bool(data.get("active"))
        password = normalize_optional_text(data.get("password"))
        if not username or not full_name:
            return jsonify({"error": "Faltan datos del perfil."}), 400
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM seller_profiles WHERE id = %s", (seller_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Perfil no encontrado."}), 404
            if company_id is not None:
                ensure_company_ids_exist(conn, [company_id])
            if role == "vendedor" and company_id is None:
                return jsonify({"error": "El vendedor debe pertenecer a una empresa."}), 400
            if role == "gestor_de_contenido" and not email:
                return jsonify({"error": "El gestor de contenido debe tener un correo guardado."}), 400
            ensure_seller_username_available(conn, username, exclude_id=seller_id)
            if password:
                conn.execute(
                    """
                    UPDATE seller_profiles
                    SET username = %s,
                        full_name = %s,
                        email = %s,
                        role = %s,
                        company_id = %s,
                        active = %s,
                        password_hash = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (username, full_name, email, role, company_id, active, hash_password(password), seller_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE seller_profiles
                    SET username = %s,
                        full_name = %s,
                        email = %s,
                        role = %s,
                        company_id = %s,
                        active = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (username, full_name, email, role, company_id, active, seller_id),
                )
            sellers = fetch_seller_rows(conn)
        return jsonify({"ok": True, "sellers": sellers})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/sellers/<int:seller_id>")
@require_admin
def delete_seller(seller_id: int):
    try:
        with db_connection() as conn:
            deleted = conn.execute("DELETE FROM seller_profiles WHERE id = %s RETURNING id", (seller_id,)).fetchone()
            if not deleted:
                return jsonify({"error": "Perfil no encontrado."}), 404
            sellers = fetch_seller_rows(conn)
        return jsonify({"ok": True, "sellers": sellers})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/categories")
def list_categories():
    try:
        include_pending = request.args.get("include_pending") == "true" and bool(session.get("admin_id"))
        with db_connection() as conn:
            categories = fetch_category_rows(conn, include_pending=include_pending)
        return jsonify({"categories": categories, "tree": build_category_tree(categories)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/categories")
@require_admin
def create_category():
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        parent_id = parse_optional_int(data.get("parentId"), "parentId")
        if not name:
            return jsonify({"error": "Falta nombre de categoria."}), 400
        with db_connection() as conn:
            validate_category_parent(conn, None, parent_id)
            ensure_category_name_available(conn, parent_id, name)
            category = create_category_record(
                conn,
                parent_id=parent_id,
                name=name,
                description=description,
                approved=True,
            )
            categories = fetch_category_rows(conn, include_pending=True)
        return jsonify({"ok": True, "category": category, "categories": categories, "tree": build_category_tree(categories)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/categories/<int:category_id>")
@require_admin
def update_category(category_id: int):
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        parent_id = parse_optional_int(data.get("parentId"), "parentId")
        if not name:
            return jsonify({"error": "Falta nombre de categoria."}), 400
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM categories WHERE id = %s", (category_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Categoria no encontrada."}), 404
            validate_category_parent(conn, category_id, parent_id)
            ensure_category_name_available(conn, parent_id, name, exclude_id=category_id)
            conn.execute(
                """
                UPDATE categories
                SET parent_id = %s, name = %s, description = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (parent_id, name, description, category_id),
            )
            categories = fetch_category_rows(conn, include_pending=True)
        return jsonify({"ok": True, "categories": categories, "tree": build_category_tree(categories)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/categories/<int:category_id>")
@require_admin
def delete_category(category_id: int):
    try:
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM categories WHERE id = %s", (category_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Categoria no encontrada."}), 404
            child_count = conn.execute(
                "SELECT COUNT(*) AS total FROM categories WHERE parent_id = %s",
                (category_id,),
            ).fetchone()["total"]
            if child_count:
                return jsonify({"error": "No puedes eliminar una categoria que tiene subcategorias."}), 409
            document_count = conn.execute(
                "SELECT COUNT(*) AS total FROM document_categories WHERE category_id = %s",
                (category_id,),
            ).fetchone()["total"]
            if document_count:
                return jsonify({"error": "No puedes eliminar una categoria asociada a documentos."}), 409
            conn.execute("DELETE FROM categories WHERE id = %s", (category_id,))
            categories = fetch_category_rows(conn, include_pending=True)
        return jsonify({"ok": True, "categories": categories, "tree": build_category_tree(categories)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/categories/<int:category_id>/approve")
@require_admin
def approve_category(category_id: int):
    try:
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM categories WHERE id = %s", (category_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Categoria no encontrada."}), 404
            conn.execute(
                """
                UPDATE categories
                SET approved = TRUE, approved_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (category_id,),
            )
            categories = fetch_category_rows(conn, include_pending=True)
        return jsonify({"ok": True, "categories": categories, "tree": build_category_tree(categories)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/platforms")
def list_platforms():
    try:
        with db_connection() as conn:
            platforms = fetch_platform_rows(conn)
        return jsonify({"platforms": platforms})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/platforms")
@require_admin
def create_platform():
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        if not name:
            return jsonify({"error": "Falta nombre de plataforma."}), 400
        with db_connection() as conn:
            ensure_platform_name_available(conn, name)
            platform = conn.execute(
                """
                INSERT INTO platforms (name, description)
                VALUES (%s, %s)
                RETURNING id, name, description, created_at, updated_at
                """,
                (name, description),
            ).fetchone()
            platforms = fetch_platform_rows(conn)
        return jsonify({"ok": True, "platform": platform, "platforms": platforms})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/platforms/<int:platform_id>")
@require_admin
def update_platform(platform_id: int):
    try:
        data = request.get_json(silent=True) or {}
        name = normalize_optional_text(data.get("name"))
        description = normalize_optional_text(data.get("description"))
        if not name:
            return jsonify({"error": "Falta nombre de plataforma."}), 400
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM platforms WHERE id = %s", (platform_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Plataforma no encontrada."}), 404
            ensure_platform_name_available(conn, name, exclude_id=platform_id)
            conn.execute(
                """
                UPDATE platforms
                SET name = %s, description = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (name, description, platform_id),
            )
            platforms = fetch_platform_rows(conn)
        return jsonify({"ok": True, "platforms": platforms})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/platforms/<int:platform_id>")
@require_admin
def delete_platform(platform_id: int):
    try:
        with db_connection() as conn:
            existing = conn.execute("SELECT id FROM platforms WHERE id = %s", (platform_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Plataforma no encontrada."}), 404
            document_count = conn.execute(
                "SELECT COUNT(*) AS total FROM document_platforms WHERE platform_id = %s",
                (platform_id,),
            ).fetchone()["total"]
            if document_count:
                return jsonify({"error": "No puedes eliminar una plataforma asociada a documentos."}), 409
            conn.execute("DELETE FROM platforms WHERE id = %s", (platform_id,))
            platforms = fetch_platform_rows(conn)
        return jsonify({"ok": True, "platforms": platforms})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/upload")
def upload():
    try:
        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"error": "No se recibio archivo."}), 400
        submitter_email = resolve_submitter_email(request.form.get("email"))
        with db_connection() as conn:
            category_ids = resolve_submission_categories(conn, request.form, submitter_email)
            platform_ids = parse_platform_ids(request.form)
            ensure_platform_ids_exist(conn, platform_ids)
        trailer_url, trailer_embed_html = build_trailer_metadata(request.form.get("trailerUrl"))
        is_trending = parse_bool(request.form.get("isTrending"))
        text = extract_from_buffer(uploaded.read(), uploaded.mimetype, uploaded.filename)
        return jsonify(
            ingest(
                "file",
                uploaded.filename or "archivo",
                text,
                submitter_email=submitter_email,
                category_ids=category_ids,
                platform_ids=platform_ids,
                trailer_url=trailer_url,
                trailer_embed_html=trailer_embed_html,
                is_trending=is_trending,
            )
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Upload error")
        return jsonify({"error": str(exc)}), 500


@app.post("/api/url")
def create_from_url():
    try:
        data = request.get_json(silent=True) or {}
        url = data.get("url")
        submitter_email = resolve_submitter_email(data.get("email"))
        if not url:
            return jsonify({"error": "Falta url."}), 400
        with db_connection() as conn:
            category_ids = resolve_submission_categories(conn, data, submitter_email)
            platform_ids = parse_platform_ids(data)
            ensure_platform_ids_exist(conn, platform_ids)
        trailer_url, trailer_embed_html = build_trailer_metadata(data.get("trailerUrl"))
        is_trending = parse_bool(data.get("isTrending"))
        provider = detect_video_provider(url)
        if provider:
            video = extract_video_submission(url)
            return jsonify(
                ingest(
                    video["source_type"],
                    video["source_name"],
                    video["text"],
                    submitter_email=submitter_email,
                    original_url=video["original_url"],
                    external_title=video["external_title"],
                    external_description=video["external_description"],
                    external_published_at=video["external_published_at"],
                    embed_html=video["embed_html"],
                    category_ids=category_ids,
                    platform_ids=platform_ids,
                    trailer_url=video["original_url"],
                    trailer_embed_html=video["embed_html"],
                    is_trending=is_trending,
                )
            )
        text, name = extract_from_url(url)
        return jsonify(
            ingest(
                "url",
                f"{name} ({url})",
                text,
                submitter_email=submitter_email,
                original_url=url,
                external_title=name,
                category_ids=category_ids,
                platform_ids=platform_ids,
                trailer_url=trailer_url,
                trailer_embed_html=trailer_embed_html,
                is_trending=is_trending,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/video")
def create_from_video():
    try:
        data = request.get_json(silent=True) or {}
        embed_html = normalize_optional_text(data.get("embedHtml") or data.get("embed"))
        submitter_email = resolve_submitter_email(data.get("email"))
        if not embed_html:
            return jsonify({"error": "Falta el embed del video."}), 400
        with db_connection() as conn:
            category_ids = resolve_submission_categories(conn, data, submitter_email)
            platform_ids = parse_platform_ids(data)
            ensure_platform_ids_exist(conn, platform_ids)
        is_trending = parse_bool(data.get("isTrending"))
        video = extract_video_submission_from_embed(embed_html)
        return jsonify(
            ingest(
                video["source_type"],
                video["source_name"],
                video["text"],
                submitter_email=submitter_email,
                original_url=video["original_url"],
                external_title=video["external_title"],
                external_description=video["external_description"],
                external_published_at=video["external_published_at"],
                embed_html=video["embed_html"],
                category_ids=category_ids,
                platform_ids=platform_ids,
                trailer_url=video["original_url"],
                trailer_embed_html=video["embed_html"],
                is_trending=is_trending,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/text")
def create_from_text():
    try:
        data = request.get_json(silent=True) or {}
        title = data.get("title") or "Nota sin titulo"
        text = data.get("text")
        submitter_email = resolve_submitter_email(data.get("email"))
        if not text:
            return jsonify({"error": "Falta texto."}), 400
        with db_connection() as conn:
            category_ids = resolve_submission_categories(conn, data, submitter_email)
            platform_ids = parse_platform_ids(data)
            ensure_platform_ids_exist(conn, platform_ids)
        trailer_url, trailer_embed_html = build_trailer_metadata(data.get("trailerUrl"))
        is_trending = parse_bool(data.get("isTrending"))
        return jsonify(
            ingest(
                "text",
                title,
                text,
                submitter_email=submitter_email,
                external_title=title,
                category_ids=category_ids,
                platform_ids=platform_ids,
                trailer_url=trailer_url,
                trailer_embed_html=trailer_embed_html,
                is_trending=is_trending,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/search")
def search_get():
    try:
        query = request.args.get("q") or request.args.get("query")
        if not query:
            return jsonify({"error": "Falta el parametro q."}), 400
        payload = semantic_search(str(query), request.args.get("k"))
        return jsonify({"query": query, **payload})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/search")
def search_post():
    try:
        data = request.get_json(silent=True) or {}
        query = data.get("query")
        if not query:
            return jsonify({"error": "Falta query."}), 400
        payload = semantic_search(str(query), data.get("k"))
        return jsonify({"query": query, **payload})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/library")
@require_seller
def library_browse():
    try:
        query = normalize_optional_text(request.args.get("q"))
        category_id = parse_optional_int(request.args.get("category_id"), "category_id")
        platform_id = parse_optional_int(request.args.get("platform_id"), "platform_id")
        trending_only = parse_bool(request.args.get("trending"))
        term = f"%{query}%" if query else None
        with db_connection() as conn:
            if category_id is not None:
                ensure_category_ids_exist(conn, [category_id])
            if platform_id is not None:
                ensure_platform_ids_exist(conn, [platform_id])
            rows = conn.execute(
                """
                SELECT
                    d.id,
                    d.source_type,
                    d.source_name,
                    d.original_url,
                    d.external_title,
                    d.external_description,
                    d.external_published_at,
                    d.embed_html,
                    d.trailer_url,
                    d.trailer_embed_html,
                    d.is_trending,
                    excerpt.content AS excerpt,
                    d.created_at
                FROM documents d
                LEFT JOIN LATERAL (
                    SELECT content
                    FROM chunks
                    WHERE document_id = d.id
                    ORDER BY chunk_index
                    LIMIT 1
                ) excerpt ON TRUE
                WHERE d.published = TRUE
                  AND (%s IS NULL OR EXISTS (
                        SELECT 1 FROM document_categories dc
                        WHERE dc.document_id = d.id AND dc.category_id = %s
                  ))
                  AND (%s IS NULL OR EXISTS (
                        SELECT 1 FROM document_platforms dp
                        WHERE dp.document_id = d.id AND dp.platform_id = %s
                  ))
                  AND (%s = FALSE OR d.is_trending = TRUE)
                  AND (
                        %s IS NULL
                        OR d.source_name ILIKE %s
                        OR COALESCE(d.external_title, '') ILIKE %s
                        OR COALESCE(d.external_description, '') ILIKE %s
                        OR COALESCE(excerpt.content, '') ILIKE %s
                  )
                ORDER BY d.is_trending DESC, d.created_at DESC
                LIMIT 60
                """,
                (category_id, category_id, platform_id, platform_id, trending_only, term, term, term, term, term, term),
            ).fetchall()
            items = [dict(row) for row in rows]
            attach_categories(conn, items)
            attach_platforms(conn, items)
        return jsonify({"items": items})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/documents")
@require_admin
def list_documents():
    try:
        with db_connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    d.id,
                    d.source_type,
                    d.source_name,
                    d.submitter_email,
                    d.original_url,
                    d.external_title,
                    d.external_description,
                    d.external_published_at,
                    d.trailer_url,
                    d.trailer_embed_html,
                    d.is_trending,
                    d.approval_notified_at,
                    d.created_at,
                    d.published,
                    COUNT(c.id)::int AS chunks
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                GROUP BY d.id
                ORDER BY d.published ASC, d.created_at DESC
                """
            ).fetchall()
            documents = [dict(row) for row in rows]
            attach_categories(conn, documents, include_pending=True)
            attach_platforms(conn, documents)
        return jsonify({"documents": documents})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/documents/<int:document_id>")
@require_admin
def document_detail(document_id: int):
    try:
        with db_connection() as conn:
            document = conn.execute(
                "SELECT * FROM documents WHERE id = %s",
                (document_id,),
            ).fetchone()
            if not document:
                return jsonify({"error": "No encontrado."}), 404
            document_payload = dict(document)
            attach_categories(conn, [document_payload], include_pending=True)
            attach_platforms(conn, [document_payload])
            chunks = conn.execute(
                """
                SELECT id, chunk_index, content
                FROM chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                (document_id,),
            ).fetchall()
        return jsonify({"document": document_payload, "chunks": chunks})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.put("/api/documents/<int:document_id>/categories")
@require_admin
def update_document_categories(document_id: int):
    try:
        data = request.get_json(silent=True) or {}
        category_ids = parse_category_ids(data)
        with db_connection() as conn:
            document = conn.execute("SELECT id FROM documents WHERE id = %s", (document_id,)).fetchone()
            if not document:
                return jsonify({"error": "No encontrado."}), 404
            ensure_category_ids_exist(conn, category_ids)
            with conn.cursor() as cur:
                set_document_categories(cur, document_id, category_ids)
            categories = fetch_document_categories_map(conn, [document_id]).get(document_id, [])
        return jsonify({"ok": True, "categories": categories})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.put("/api/documents/<int:document_id>/platforms")
@require_admin
def update_document_platforms(document_id: int):
    try:
        data = request.get_json(silent=True) or {}
        platform_ids = parse_platform_ids(data)
        with db_connection() as conn:
            document = conn.execute("SELECT id FROM documents WHERE id = %s", (document_id,)).fetchone()
            if not document:
                return jsonify({"error": "No encontrado."}), 404
            ensure_platform_ids_exist(conn, platform_ids)
            with conn.cursor() as cur:
                set_document_platforms(cur, document_id, platform_ids)
            platforms = fetch_document_platforms_map(conn, [document_id]).get(document_id, [])
        return jsonify({"ok": True, "platforms": platforms})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/documents/<int:document_id>/library-metadata")
@require_admin
def update_document_library_metadata(document_id: int):
    try:
        data = request.get_json(silent=True) or {}
        is_trending = parse_bool(data.get("isTrending"))
        trailer_url, trailer_embed_html = build_trailer_metadata(data.get("trailerUrl"))
        with db_connection() as conn:
            updated = conn.execute(
                """
                UPDATE documents
                SET is_trending = %s,
                    trailer_url = %s,
                    trailer_embed_html = %s
                WHERE id = %s
                RETURNING id, is_trending, trailer_url, trailer_embed_html
                """,
                (is_trending, trailer_url, trailer_embed_html, document_id),
            ).fetchone()
        if not updated:
            return jsonify({"error": "No encontrado."}), 404
        return jsonify({"ok": True, "document": updated})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/documents/<int:document_id>/publish")
@require_admin
def publish_document(document_id: int):
    try:
        with db_connection() as conn:
            document = conn.execute(
                """
                UPDATE documents
                SET published = TRUE
                WHERE id = %s
                RETURNING *
                """,
                (document_id,),
            ).fetchone()
        if not document:
            return jsonify({"error": "No encontrado."}), 404

        notification = send_approval_notification(document)
        if notification.get("sent"):
            notified_at = datetime.now(timezone.utc)
            with db_connection() as conn:
                conn.execute(
                    "UPDATE documents SET approval_notified_at = %s WHERE id = %s",
                    (notified_at, document_id),
                )
            notification["notifiedAt"] = notified_at.isoformat()
        return jsonify({"ok": True, "notification": notification})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/documents/<int:document_id>/unpublish")
@require_admin
def unpublish_document(document_id: int):
    try:
        with db_connection() as conn:
            updated = conn.execute(
                "UPDATE documents SET published = FALSE WHERE id = %s RETURNING id",
                (document_id,),
            ).fetchone()
        if not updated:
            return jsonify({"error": "No encontrado."}), 404
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/documents/<int:document_id>")
@require_admin
def delete_document(document_id: int):
    try:
        with db_connection() as conn:
            deleted = conn.execute("DELETE FROM documents WHERE id = %s RETURNING id", (document_id,)).fetchone()
        if not deleted:
            return jsonify({"error": "No encontrado."}), 404
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


def build_api_catalog() -> dict:
    groups = [
        {
            "id": "public-content",
            "title": "Contenido y captura publica",
            "description": "Endpoints para aportar contenido, taxonomias publicas y busqueda semantica.",
            "endpoints": [
                {"method": "GET", "path": "/api/search?q=texto&k=5", "access": "publico", "description": "Busqueda semantica sobre contenido publicado."},
                {"method": "POST", "path": "/api/search", "access": "publico", "description": 'Busqueda semantica con JSON.', "body": {"query": "texto", "k": 5}},
                {"method": "GET", "path": "/api/categories", "access": "publico", "description": "Lista publica de categorias aprobadas."},
                {"method": "GET", "path": "/api/platforms", "access": "publico", "description": "Lista publica de plataformas y marcas."},
                {"method": "POST", "path": "/api/upload", "access": "publico", "description": "Carga archivo para aprobacion.", "contentType": "multipart/form-data"},
                {"method": "POST", "path": "/api/url", "access": "publico", "description": "Captura una pagina o URL de video.", "body": {"url": "https://ejemplo.com", "email": "autor@ejemplo.com", "categoryId": 1}},
                {"method": "POST", "path": "/api/text", "access": "publico", "description": "Guarda una nota libre.", "body": {"title": "Mi nota", "text": "Contenido", "email": "autor@ejemplo.com"}},
                {"method": "POST", "path": "/api/video", "access": "publico", "description": "Registra video social a partir del embed HTML.", "body": {"embedHtml": "<iframe src=\"https://www.youtube.com/embed/demo\"></iframe>", "email": "autor@ejemplo.com"}},
            ],
        },
        {
            "id": "seller-module",
            "title": "Modulo comercial vendedor",
            "description": "API para perfiles vendedor vinculados a una empresa.",
            "endpoints": [
                {"method": "POST", "path": "/api/seller/login", "access": "vendedor", "description": "Inicia sesion vendedor.", "body": {"username": "karla.rojas", "password": "******"}},
                {"method": "POST", "path": "/api/seller/logout", "access": "vendedor", "description": "Cierra sesion vendedor."},
                {"method": "GET", "path": "/api/seller/me", "access": "vendedor", "description": "Devuelve el perfil vendedor autenticado."},
                {"method": "GET", "path": "/api/library", "access": "vendedor", "description": "Explora la biblioteca comercial por categoria, plataforma, tendencia y titulo."},
            ],
        },
        {
            "id": "content-manager-module",
            "title": "Gestor de contenido",
            "description": "API para gestores que capturan material sin repetir correo en cada carga.",
            "endpoints": [
                {"method": "POST", "path": "/api/content-manager/login", "access": "gestor", "description": "Inicia sesion gestor de contenido.", "body": {"username": "gestor.demo", "password": "******"}},
                {"method": "POST", "path": "/api/content-manager/logout", "access": "gestor", "description": "Cierra sesion del gestor."},
                {"method": "GET", "path": "/api/content-manager/me", "access": "gestor", "description": "Devuelve el perfil gestor autenticado."},
            ],
        },
        {
            "id": "admin-auth",
            "title": "Autenticacion administrativa",
            "description": "Sesion del administrador para operar el sistema comercial.",
            "endpoints": [
                {"method": "POST", "path": "/api/admin/login", "access": "admin", "description": "Inicia sesion admin.", "body": {"username": "admin", "password": "******"}},
                {"method": "POST", "path": "/api/admin/logout", "access": "admin", "description": "Cierra sesion admin."},
                {"method": "GET", "path": "/api/admin/me", "access": "admin", "description": "Estado de la sesion admin."},
            ],
        },
        {
            "id": "admin-content",
            "title": "Operacion de contenido",
            "description": "Moderacion, publicacion y enriquecimiento del contenido.",
            "endpoints": [
                {"method": "GET", "path": "/api/documents", "access": "admin", "description": "Lista todo el contenido cargado."},
                {"method": "GET", "path": "/api/documents/:id", "access": "admin", "description": "Detalle de documento, metadata y fragmentos."},
                {"method": "PUT", "path": "/api/documents/:id/categories", "access": "admin", "description": "Reasignar categorias.", "body": {"categoryIds": [1, 2]}},
                {"method": "PUT", "path": "/api/documents/:id/platforms", "access": "admin", "description": "Reasignar plataformas.", "body": {"platformIds": [1, 3]}},
                {"method": "PATCH", "path": "/api/documents/:id/library-metadata", "access": "admin", "description": "Actualizar tendencia y trailer.", "body": {"isTrending": True, "trailerUrl": "https://www.youtube.com/watch?v=demo"}},
                {"method": "POST", "path": "/api/documents/:id/publish", "access": "admin", "description": "Publicar e intentar aviso por correo."},
                {"method": "POST", "path": "/api/documents/:id/unpublish", "access": "admin", "description": "Despublicar documento."},
                {"method": "DELETE", "path": "/api/documents/:id", "access": "admin", "description": "Eliminar documento."},
            ],
        },
        {
            "id": "admin-taxonomy",
            "title": "Taxonomias y catalogo",
            "description": "Mantenimiento de categorias y plataformas de negocio.",
            "endpoints": [
                {"method": "POST", "path": "/api/categories", "access": "admin", "description": "Crear categoria."},
                {"method": "PATCH", "path": "/api/categories/:id", "access": "admin", "description": "Editar categoria."},
                {"method": "POST", "path": "/api/categories/:id/approve", "access": "admin", "description": "Aprobar categoria propuesta."},
                {"method": "DELETE", "path": "/api/categories/:id", "access": "admin", "description": "Eliminar categoria sin hijos ni documentos."},
                {"method": "POST", "path": "/api/platforms", "access": "admin", "description": "Crear plataforma."},
                {"method": "PATCH", "path": "/api/platforms/:id", "access": "admin", "description": "Editar plataforma."},
                {"method": "DELETE", "path": "/api/platforms/:id", "access": "admin", "description": "Eliminar plataforma."},
            ],
        },
        {
            "id": "admin-access",
            "title": "Empresas y vendedores",
            "description": "Control de acceso comercial por empresa y perfil vendedor.",
            "endpoints": [
                {"method": "GET", "path": "/api/companies", "access": "admin", "description": "Listar empresas."},
                {"method": "POST", "path": "/api/companies", "access": "admin", "description": "Crear empresa.", "body": {"name": "Distribuidora Andina", "description": "Cliente premium"}},
                {"method": "PATCH", "path": "/api/companies/:id", "access": "admin", "description": "Editar empresa."},
                {"method": "DELETE", "path": "/api/companies/:id", "access": "admin", "description": "Eliminar empresa sin vendedores asignados."},
                {"method": "GET", "path": "/api/sellers", "access": "admin", "description": "Listar perfiles vendedor."},
                {"method": "POST", "path": "/api/sellers", "access": "admin", "description": "Crear vendedor o gestor de contenido.", "body": {"fullName": "Karla Rojas", "username": "karla.rojas", "email": "karla@empresa.com", "role": "vendedor", "password": "******", "companyId": 1, "active": True}},
                {"method": "PATCH", "path": "/api/sellers/:id", "access": "admin", "description": "Editar vendedor."},
                {"method": "DELETE", "path": "/api/sellers/:id", "access": "admin", "description": "Eliminar perfil de acceso."},
            ],
        },
    ]
    return {
        "name": "Sistema comercial de biblioteca multimedia",
        "description": "API para capturar, moderar, vender y documentar contenido multimedia con categorias, plataformas, empresas y perfiles vendedor.",
        "version": "1.0",
        "basePath": "/api",
        "authentication": [
            {"role": "publico", "mode": "sin autenticacion", "notes": "Consulta publica y envio de contenido para aprobacion."},
            {"role": "vendedor", "mode": "cookie de sesion", "notes": "Acceso al modulo comercial mediante /api/seller/login."},
            {"role": "gestor", "mode": "cookie de sesion", "notes": "Captura asistida mediante /api/content-manager/login, reutilizando el correo del perfil."},
            {"role": "admin", "mode": "cookie de sesion", "notes": "Operacion y mantenimiento del sistema mediante /api/admin/login."},
        ],
        "examples": [
            {"title": "Buscar contenido", "language": "bash", "code": 'curl "http://127.0.0.1:5000/api/search?q=netflix&k=5"'},
            {"title": "Login vendedor", "language": "javascript", "code": "await fetch('/api/seller/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ username: 'karla.rojas', password: '******' }) });"},
            {"title": "Login gestor de contenido", "language": "javascript", "code": "await fetch('/api/content-manager/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ username: 'gestor.demo', password: '******' }) });"},
            {"title": "Explorar biblioteca comercial", "language": "javascript", "code": "await fetch('/api/library?platform_id=1&trending=true', { credentials: 'include' });"},
        ],
        "groups": groups,
        "endpointCount": sum(len(group["endpoints"]) for group in groups),
    }


@app.get("/api")
def api_index():
    return jsonify(build_api_catalog())


@app.get("/api/docs")
def api_docs():
    return jsonify(build_api_catalog())


@app.get("/<path:path>")
def static_files(path: str):
    full_path = os.path.join(app.static_folder or "", path)
    if os.path.isfile(full_path):
        return send_from_directory(app.static_folder, path)
    return app.send_static_file("index.html")


def initialize() -> None:
    with db_connection() as conn:
        conn.execute(SCHEMA_SQL)
    ensure_seed_admin()
    ensure_default_platforms()


if __name__ == "__main__":
    initialize()
    app.run(host=HOST, port=PORT)
