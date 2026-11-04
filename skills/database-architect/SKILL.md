---
name: database-architect
description: SQL & NoSQL database schema design, migrations, index optimization, and relational modeling.
triggers:
  - "design database"
  - "create sql schema"
  - "optimize query"
  - "migration"
---

# Database Architect Skill

## Capabilities
1. **Schema Design**:
   - Normalize to 3NF where appropriate; denormalize intentionally for read-heavy workloads.
   - Primary keys: UUIDv7 or BIGSERIAL/AUTO_INCREMENT.
   - Foreign key constraints with proper `ON DELETE CASCADE / SET NULL` policies.
2. **Indexing & Optimization**:
   - B-tree indexes for equality and range queries.
   - Compound indexes with cardinality awareness (most selective column first).
   - GIN / GiST indexes for JSONB, array, and full-text search.
3. **ORM & Migrations**:
   - SQLAlchemy / Alembic for Python.
   - Prisma / Drizzle / TypeORM for TypeScript/JavaScript.
   - Zero-downtime migration guidelines (adding nullable columns first, backfilling, then enforcing constraints).
