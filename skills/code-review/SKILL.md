---
name: code-review
description: Comprehensive automated code review, security vulnerability audit, and architectural sanity analysis.
triggers:
  - "review this code"
  - "audit security"
  - "find bugs"
  - "code quality check"
---

# Code Review & Security Audit Skill

## Overview
Perform a rigorous, senior-level code review focusing on:
1. **Security Vulnerabilities**: Injection attacks (SQL, command, XSS), path traversal, unescaped user input, insecure deserialization, credential leakage.
2. **Correctness & Edge Cases**: Off-by-one errors, null pointer/None dereferencing, race conditions, unhandled exceptions.
3. **Performance & Memory**: Inefficient loops, unbounded queries, memory leaks, unclosed file descriptors/sockets.
4. **Maintainability & Clean Architecture**: DRY violations, high cyclomatic complexity, tight coupling, poor naming.

## Procedure
1. Inspect the target files using `file_read` or `grep`.
2. Categorize findings into:
   - **CRITICAL**: Exploitable security vulnerabilities or immediate crashes.
   - **WARNING**: Sub-optimal performance, unhandled error conditions, or fragility.
   - **SUGGESTION**: Style, idiomatic enhancements, and documentation.
3. Provide surgical code fixes with before-and-after diffs using `file_edit`.
