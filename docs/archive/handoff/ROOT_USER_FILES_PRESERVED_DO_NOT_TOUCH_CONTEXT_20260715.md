# Root User Files Preserved — DO NOT TOUCH

## Identity

- Protection branch: `main-root-user-files-preserved-DO_NOT_TOUCH-20260715`
- Protected base commit: `12dc8a8ba448881e78ba00c938dca562538b1f89`
- Protected worktree: `<repository-root>`
- Classification: `DO_NOT_TOUCH`
- Pre-context-file dirty-state fingerprint: `535b5cace413e34cfe952d81e71d76b8c2b2e6caf72c82535dd95a70bbcea1f7`

## Purpose

This branch and worktree preserve the user's pre-existing modified and untracked files in place. They are not the delivery source and must not be cleaned, stashed, moved, overwritten, staged, committed, merged, or deleted as part of release work.

## Prohibited Operations

- Do not run `git reset --hard`, `git checkout --`, `git clean`, stash, prune, history rewrite, force push, or bulk cleanup here.
- Do not switch this worktree to `main` unless the user explicitly authorizes a later transition.
- Do not deploy, build a release, or derive delivery evidence from this protected dirty worktree.
- Do not stage or commit any pre-existing user file from this worktree without exact, path-specific user authorization.

## Clean Delivery Source

- Delivery branch: `main`
- Delivery commit: `ef515e71703d1e86ed3ccf3c59df7522447f1f0d`
- Delivery worktree: `<repository-root>-main-clean-delivery-20260715`
- Merge source: `74d13011a87af7cd082cb8e5def4f27d59b033f2`
- Merge tree: `3640e7d1e7588b0a8b22bb2e04d8c7ab37dd7ead`
- Push status at creation: `NOT_PUSHED`

All release verification and any later deployment preparation must run from the clean `main` delivery worktree above.

## Context Document Status

This file is the only new root-worktree file explicitly authorized on 2026-07-15 for branch context. It is committed on the protection branch; `12dc8a8ba448881e78ba00c938dca562538b1f89` remains the recorded protected base, while later commits on this branch are documentation-only context commits. No pre-existing user file is included in those commits.
