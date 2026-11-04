---
name: api-mock-engine
description: Rapid mock API server generation, synthetic test fixture creation, and client SDK synthesis.
triggers:
  - "create mock server"
  - "generate fake api data"
  - "mock endpoint"
  - "create sdk client"
---

# API Mock Engine & Synthetic Fixture Skill

## Capabilities
1. **Mock API Servers**:
   - Spin up instant lightweight FastAPI or Express servers with simulated latencies, error states (401, 429, 500), and rate-limit responses.
   - Ideal for frontend offline development and end-to-end integration testing.
2. **Realistic Synthetic Data Generation**:
   - Schema-guided generation using Pydantic, Faker, or TypeScript types.
   - Generates realistic UUIDs, timestamps, user profiles, transactional records, and status enums.
3. **Client SDK Synthesis**:
   - Given an OpenAPI spec or JSON payloads, generate fully typed Python `httpx` or TypeScript `fetch` client wrappers with retries and authentication handling.
