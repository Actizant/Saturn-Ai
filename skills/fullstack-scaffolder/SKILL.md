---
name: fullstack-scaffolder
description: Instant production-grade project scaffolding for Next.js, React, FastAPI, Node.js, Tailwind CSS, and SQLite/PostgreSQL.
triggers:
  - "scaffold fullstack app"
  - "create nextjs project"
  - "create fastapi backend"
  - "bootstrap new repo"
---

# Full-Stack Application Scaffolder Skill

## Architecture Blueprints
1. **Frontend (Next.js / Vite + React + Tailwind)**:
   - App Router architecture with typed routes and server/client boundary separation.
   - Component organization: `components/ui/`, `hooks/`, `lib/utils.ts`.
   - Dark/Light mode theme provider with zero-runtime CSS flashes.
2. **Backend (FastAPI / Express / NestJS)**:
   - Layered architecture: Controllers/Routers &rarr; Services/Use-Cases &rarr; Repositories &rarr; Database models.
   - Pydantic v2 or Zod validation schemas.
   - Centralized error handling and structured JSON logging.
3. **Database & Storage**:
   - SQLAlchemy 2.0 (async) or Prisma / Drizzle ORM.
   - Migration management enabled by default.
4. **DevOps & Config**:
   - `.env.example`, `.gitignore`, `Dockerfile`, `docker-compose.yml`.
   - Pre-commit formatting (`ruff`, `prettier`, `eslint`).
