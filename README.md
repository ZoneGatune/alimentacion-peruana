# Biblioteca Tematica

Aplicacion web con backend en Python para operar una biblioteca multimedia como sistema comercial: capta contenido, lo clasifica con categorias jerarquicas, lo ofrece por plataforma/marca, separa accesos por admin, vendedor y gestor de contenido, y expone APIs documentadas para integraciones.

## Stack

- Python 3.11+
- Flask
- PostgreSQL 16 + pgvector
- Gemini (`google-genai`)
- Frontend estatico en `public/`

## Variables de entorno

- `DATABASE_URL`: conexion a PostgreSQL
- `GEMINI_API_KEY`: clave para embeddings y generacion con Gemini
- `SESSION_SECRET`: secreto para las sesiones web
- `ADMIN_USERNAME` y `ADMIN_PASSWORD`: necesarios si quieres poder entrar al panel de administrador
- `PORT`: opcional, por defecto `5000`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`: opcionales, para avisar por correo cuando un contenido se aprueba
- `SMTP_USE_TLS`: opcional, por defecto `true`

## Instalacion

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Si existe un archivo `.env` en la raiz del proyecto, `app.py` lo carga automaticamente al iniciar.

## Flujo de contenido

- Cada aporte pide un correo del remitente y queda guardado para avisarle cuando se apruebe.
- Se aceptan archivos (`pdf`, `docx`, `txt`, `md`, imagenes), paginas web, notas libres y videos publicos de TikTok/Facebook/YouTube.
- Los videos sociales y trailers guardan titulo, descripcion, URL original, fecha publicada y embed para mostrarlos despues.
- Al publicar desde el panel admin, la app intenta enviar un aviso por correo si SMTP esta configurado. Si no lo esta, el contenido se publica igual y el panel lo indica.
- Las categorias son un arbol editable: puedes crear ramas y subramas ilimitadas, por ejemplo `Peliculas > Romantica > Romance asiatico`.
- Las personas que suben contenido tambien pueden proponer categorias o subcategorias nuevas; esas propuestas quedan pendientes hasta que un administrador las apruebe.
- Cada documento puede asociarse a una o varias categorias y esas asociaciones se pueden editar desde el panel admin.
- Si una propuesta no encaja, el administrador puede mover el documento a una categoria existente y luego eliminar la categoria pendiente.
- Cada documento tambien puede asociarse a una o varias plataformas como Netflix, Prime Video, Amazon o Movistar TV.
- La biblioteca comercial ya no es publica: solo entra un perfil con rol `vendedor`.
- El administrador crea primero la empresa y luego asigna perfiles vendedor a esa empresa.
- Existe un rol adicional `gestor_de_contenido`, que puede pertenecer a una empresa o existir solo bajo administracion.
- Cuando entra un gestor de contenido, la captura reutiliza su correo guardado y ya no lo pide en cada subida.
- El modulo comercial queda separado del panel admin y permite filtrar por categoria, plataforma, tendencia y titulo para vender contenido de la biblioteca.
- La interfaz ahora funciona como un SPA con menu lateral, modulos separados y submenus internos para navegar mejor cada bloque: resumen, captura, busqueda inteligente, catalogo comercial, operaciones, taxonomias, accesos y documentacion API.
- Los desarrolladores tienen una seccion propia de documentacion tecnica dentro de la app, alimentada por `/api/docs`.
- En la captura, **Video social** aparece primero y ahora trabaja con **embed HTML** en vez de link directo.

## Estructura

- `app.py` - servidor Flask, API, inicializacion SQL, categorias jerarquicas, empresas, perfiles vendedor, plataformas, modulo comercial, ingestion, videos sociales, avisos de aprobacion y catalogo de endpoints
- `public/` - SPA comercial con menu, modulos, submenus internos y documentacion visual de API
- `requirements.txt` - dependencias Python

## Base de datos

Al iniciar, `app.py` crea la extension `vector` y las tablas `admins`, `documents` y `chunks` si no existen. La base debe permitir `CREATE EXTENSION vector`.

La tabla `documents` tambien guarda:
- correo del remitente
- URL original
- titulo y descripcion externos
- fecha publicada en la fuente
- embed HTML del video
- URL y embed HTML del trailer
- indicador de tendencia
- fecha en que se envio el aviso de aprobacion

Ademas existen:
- `categories`: nodos del arbol jerarquico con `parent_id`, estado de aprobacion y correo del proponente si vino de un envio publico
- `document_categories`: relacion muchos-a-muchos entre documentos y categorias
- `companies`: empresas creadas por el administrador para agrupar vendedores
- `seller_profiles`: perfiles con rol `vendedor` o `gestor_de_contenido`, usuario, correo, clave cifrada y empresa opcional
- `platforms`: marcas o plataformas de streaming / distribucion
- `document_platforms`: relacion muchos-a-muchos entre documentos y plataformas

## API y documentacion

- `GET /api` y `GET /api/docs` devuelven el catalogo estructurado de endpoints.
- La seccion **Documentacion API** dentro de la interfaz muestra autenticacion, grupos de endpoints y ejemplos de uso para desarrolladores.
- El catalogo separa rutas publicas, rutas de vendedor, rutas de gestor de contenido y rutas administrativas para facilitar integraciones externas.
