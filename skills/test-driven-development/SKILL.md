---
name: test-driven-development
description: Automated test-driven development (TDD), test suite generation, and self-healing verification loops.
triggers:
  - "write tests"
  - "tdd"
  - "run tests and fix"
  - "self-heal code"
---

# Test-Driven Development & Self-Healing Skill

## Overview
Ensures robust, verifiable software by driving implementation through rigorous unit and integration tests.

## Protocol
1. **Analyze Requirements**: Identify the inputs, expected outputs, invariants, and edge cases (empty inputs, maximum bounds, error types).
2. **Generate Test Suite**:
   - Python: `pytest` / `unittest` with mock fixtures and parameterized test cases.
   - JavaScript/TypeScript: `vitest` / `jest`.
   - Rust: `cargo test`.
   - Go: `go test`.
3. **Execute & Verify**:
   - Run the test suite using `test_and_heal` or `bash`.
   - If tests fail, parse the stack trace and traceback.
   - Do NOT rewrite entire files. Use `file_edit` to surgically patch the minimal faulty logic.
   - Re-run the tests until 100% pass rate is achieved.
