---
name: docker-devops
description: Production-grade containerization, multi-stage Dockerfiles, Docker Compose orchestration, and CI/CD pipelines.
triggers:
  - "dockerize"
  - "create dockerfile"
  - "docker compose"
  - "setup github actions"
---

# Docker & DevOps Engineering Skill

## Best Practices
1. **Multi-Stage Builds**:
   - Separate build-time dependencies from minimal runtime environments (e.g., `python:3.11-slim`, `node:20-alpine`, `scratch`, `distroless`).
   - Drastically reduces image attack surface and final image size.
2. **Layer Caching**:
   - Copy dependency manifests (`package.json`, `requirements.txt`, `Cargo.toml`) before copying source code.
3. **Security**:
   - Run as non-root user (`USER appuser`).
   - Do NOT bake secrets or `.env` files into Docker images.
4. **Compose & Networking**:
   - Configure health checks (`HEALTHCHECK CMD curl -f ...`).
   - Define volume persistence for stateful databases.
