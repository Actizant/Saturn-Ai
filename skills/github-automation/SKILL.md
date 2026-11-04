---
name: github-automation
description: Autonomous GitHub repository management, Pull Request creation, commit message crafting, and issue triage.
triggers:
  - "create pull request"
  - "open pr"
  - "summarize pr diff"
  - "github issue triage"
---

# GitHub Automation Skill

## Protocols
1. **Pull Request Generation**:
   - Inspect local git branch changes with `git_manager(action='diff')`.
   - Ensure working tree is clean and all tests pass with `test_and_heal`.
   - Format PR title according to Conventional Commits: `<type>(<scope>): <summary>`.
   - Generate structured PR body:
     - **Summary**: Concise bullet points of what changed.
     - **Motivation**: Why this change is necessary.
     - **Testing**: How the change was verified (unit tests, manual screenshots).
     - **Breaking Changes**: Any breaking API or schema changes.
2. **Issue Triage**:
   - Classify incoming bug reports: Reproduction steps, environment details, priority (P0, P1, P2).
   - Match bug tracebacks against workspace symbols using `grep`.
