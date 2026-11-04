---
name: security-auditor
description: Hardcoded secrets detection, OWASP Top 10 auditing, dependency CVE scanner, and safe API boundary analysis.
triggers:
  - "scan for secrets"
  - "security audit"
  - "owasp check"
  - "find leaked api keys"
---

# Security Auditor & Vulnerability Scanner Skill

## Core Audit Vectors
1. **Secrets & Credentials Leakage**:
   - High-entropy strings, JWT tokens, AWS access keys (`AKIA...`), OpenAI/Gemini API keys, private keys (`-----BEGIN PRIVATE KEY-----`). <!-- tripwire: allow -->
   - `.env` files mistakenly tracked by Git.
2. **Injection Attacks**:
   - SQL Injection: String concatenation in queries instead of parameterized statements.
   - Command Injection: Unescaped inputs passed into `os.system`, `subprocess.Popen(..., shell=True)`.
   - Cross-Site Scripting (XSS): Unsanitized `dangerouslySetInnerHTML`, raw innerHTML manipulation.
3. **Dependency Vulnerability Scans**:
   - Python: Run `pip audit` or check known CVE databases.
   - Node: Run `npm audit --production`.
4. **Remediation Protocol**:
   - Replace raw concatenation with parameterized queries or prepared statements.
   - Move sensitive credentials into environment variables loaded via `.env` with strict gitignore rules.
