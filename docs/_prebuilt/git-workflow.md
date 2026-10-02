
# 🚀 Git Workflow & Best Practices

A concise, opinionated workflow for the **Frontend**, **Controller**, and **Core** teams working on NEBULA Platform.

Following these conventions minimises merge conflicts, preserves history, and accelerates delivery.

---

## 1. Branch Model

```
main     ──▶ production (always deployable)
│
├─ develop            ──▶ integration branch
│   ├─ feature/<scope>-<desc>
│   ├─ bugfix/<scope>-<desc>
│   ├─ hotfix/<scope>-<desc>
│   └─ release/<version>
```

### 1.1 Branch Types

| Branch        | Purpose                                                                 |
|---------------|-------------------------------------------------------------------------|
| `main`        | Stable, released code. **Direct commits are blocked.**                  |
| `develop`     | Integration of completed work before a release.                         |
| `feature/*`   | New functionality.                                                      |
| `bugfix/*`    | Non‑critical fixes.                                                     |
| `hotfix/*`    | **Critical** production patches.                                        |
| `release/*`   | Final hardening + version bump before shipping.                         |

> **Rule of thumb:** _Every change flows through a Pull Request (PR) — never push to `main` or `develop` directly._

---

## 2. Naming Conventions

| Type      | Pattern                                   | Example                             |
|-----------|-------------------------------------------|-------------------------------------|
| Feature   | `feature/<component>-<desc>`              | `feature/frontend-login-page`       |
| Bugfix    | `bugfix/<component>-<desc>`               | `bugfix/controller-null-pointer`    |
| Hotfix    | `hotfix/<component>-<desc>`               | `hotfix/core-memory-leak`           |
| Release   | `release/<major>.<minor>.<patch>`         | `release/1.4.0`                     |

---

## 3. Conventional Commits

Format: `<type>(<scope>): <short summary>`

| Type        | Use for …                           | Example                                           |
|-------------|-------------------------------------|---------------------------------------------------|
| `feat`      | New feature                         | `feat(controller): add /scenarios endpoint`       |
| `fix`       | Bug fix                             | `fix(frontend): correct form validation`          |
| `docs`      | Docs only                           | `docs(core): explain rule engine config`          |
| `refactor`  | Internal restructuring              | `refactor(core): extract auth helpers`            |
| `test`      | Add/modify tests                    | `test(frontend): snapshot for Button`             |
| `chore`     | Build, tooling, release, cleaning   | `chore: removing logs`                            |

_References to issues_: add `Closes #123` in the commit body.

---

## 4. End‑to‑End Workflow

1. **Create an issue** describing scope, acceptance criteria, and dependencies.
2. **Branch from `develop`** following the naming rules.
3. **Commit early & often** using Conventional Commits.
4. **Push & open a PR** to `develop`.
   - Assign at least **one reviewer**.
   - CI must pass (lint, tests, build).
5. **Review & merge** when approved. Delete the remote branch.

### 4.1 Releasing

For planned releases, follow this workflow to ensure stability:

- **When to release**: Based on sprint cycles or feature readiness
- **Key points**:
  - Release branches isolate stabilization work
  - All changes must be regression tested
  - Coordinates deployment across components


1. Branch `release/x.y.z` from `develop`.
2. Bump version, update CHANGELOG, run full regression tests.
3. Open PR `release/x.y.z` → `main`; merge when green.
4. Tag `vX.Y.Z` on `main` (`git tag v1.2.0 && git push origin v1.2.0`).
5. Merge `main` back into `develop` to keep history linear.

### 4.2 Hotfixing

For critical production issues that need immediate fixes:

- **When to use**: Only for urgent production bugs that can't wait for the next release
- **Key points**:
  - Hotfixes bypass `develop` and go directly to `main`
  - Must be thoroughly tested despite urgency
  - Requires careful coordination with the team
  - Should be small, focused changes targeting only the critical issue


1. Branch `hotfix/...` from `main`.
2. Fix, test, and open PR **to `main`**.
3. Tag `vX.Y.Z-hotfix` (or patch bump) after merge.
4. **Back‑merge** `main` into `develop`.

---

## 5. Code Review Checklist

> Use this quick checklist before approving a PR.

- [ ] Code builds & CI is green.
- [ ] No obvious security, performance, or accessibility issues.
- [ ] Docs, API spec, and configs updated.
- [ ] Commit history is clean (squash/rebase as needed).

---

## 6. House‑Keeping Commands

| Task                 | Command                                         |
|----------------------|-------------------------------------------------|
| Delete local branch  | `git branch -d <branch>`                        |
| Delete remote branch | `git push origin --delete <branch>`             |
| Sync `develop`       | `git pull --rebase origin develop`              |
| Inspect history      | `git log --oneline --graph --decorate --all`    |

---

## 7. Additional Tips

- Keep PRs **small** (< 1000 LOC) to speed up review.
- Use **Draft PRs** to gather feedback early.

---

## 8. Resources

- [Conventional Commits](https://www.conventionalcommits.org)
- [Git Branching Model](https://nvie.com/posts/a-successful-git-branching-model/)
