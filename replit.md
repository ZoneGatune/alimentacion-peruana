# Alimentacion Peruana

Aplicacion web con backend Flask y frontend estatico para construir una base nutricional de comida peruana con embeddings y busqueda semantica.

## Stack

- Python 3.11
- Flask
- PostgreSQL 16 con pgvector
- Gemini para embeddings, vision y respuestas fallback

## Run

- Instalar dependencias con `pip install -r requirements.txt`
- Iniciar con `python app.py` en el puerto 5000

## Layout

- `app.py` - backend Flask y API
- `public/` - `index.html`, `styles.css`, `app.js`
- `requirements.txt` - dependencias Python

## Deployment

Configurado para ejecutar `python app.py`.
