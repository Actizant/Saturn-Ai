---
name: git-workflow
description: Professional Git version control workflow, conventional commits, branch management, and conflict resolution.
triggers:
  - "git commit"
  - "create branch"
  - "resolve merge conflicts"
  - "git status check"
---

# Git Workflow & Conventional Commits Skill

## Commit Conventions
Follow the Conventional Commits specification:
- `feat:` A new feature for the user
- `fix:` A bug fix
- `docs:` Documentation only changes
- `style:` Formatting, missing semi-colons, no code change
- `refactor:` A code change that neither fixes a bug nor adds a feature
- `perf:` A code change that improves performance
- `test:` Adding missing tests or correcting existing tests
- `chore:` Changes to build process, dependencies, or auxiliary tools

## Standard Workflow
1. Run `git_manager(action='status')` and `git_manager(action='diff')` to review modified files.
2. Ensure no secrets (API keys, `.env`, tokens) or unintended files are staged.
3. Commit changes using `git_manager(action='commit', message='...')`.
4. If a branch is requested, create and checkout using `git_manager(action='new_branch', branch='...')`.
