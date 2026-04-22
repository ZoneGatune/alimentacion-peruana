# Alimentación Peruana

Static landing page for the "Alimentación Peruana" project (Peruvian food).

## Stack
- Node.js 20
- Express 4 serving static files from `public/`

## Run
- Workflow: `Start application` runs `npm start` on port 5000 (host `0.0.0.0`).
- Dev cache headers are disabled when `NODE_ENV !== "production"`.

## Layout
- `server.js` — Express static server
- `public/` — `index.html`, `styles.css`
- `package.json` — npm dependencies and start script

## Deployment
Configured as autoscale: build `npm install`, run `npm start`.
