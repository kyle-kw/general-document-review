# Repository Guidelines

## Structure

React/TypeScript lives in frontend/src/; frontend/public/ contains public assets. FastAPI modules live in backend/app/ and Python tests in backend/tests/. data/ and .env are local runtime state. Keep API routes under /api/v1.

## Development

From backend/: uv sync --dev; uv run uvicorn app.main:app --reload --port 8000.
From frontend/: npm install; npm run dev. Vite proxies /api to port 8000.

## Style and verification

Use TypeScript strict mode, two spaces, no semicolons, and @/ imports. Use Python type hints and four spaces. Add regression coverage for behavior changes. Run frontend tests, typecheck, build, and backend pytest; test production static serving after a frontend build. Keep npm and uv lockfiles in version control.

## Configuration and delivery

Document new environment variables in .env.example and README.md. Never commit secrets, databases, generated outputs or caches. Commit messages use feat:, fix:, docs:, test:, or chore:. Explain behavior changes and verification in pull requests.
