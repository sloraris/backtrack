# Working on Backtrack

Backtrack is a public, general-purpose project. Write it for anyone who runs Docker Compose stacks, not for one
particular setup. Design notes are in [docs/design.md](docs/design.md). How to run the tests is in the README's
Development section.

## Keep deployment details out

Nothing about a specific deployment belongs in this repo: not in code, comments, tests, docs, examples, commit
messages, branch names, issues or PR descriptions. That covers:

- host, server or NAS names, and the names of other people's or your own stacks and services
- domain names, IP addresses, subnets, VPN or mesh layouts, share or volume paths
- the names of other tools in someone's lab (management planes, proxies, monitoring) that appear only because one
  deployment uses them
- people's names, accounts, email addresses, and secrets of any kind, even when they look fake

Use neutral placeholders instead: `myapp`, `db`, `alpha` and `beta` for machines, `nas.example.com`,
`/mnt/nas/restic`, `192.0.2.0/24` (the documentation range). Describe a bug or a feature by what Backtrack does and
should do, not by the lab where it was found. "A container with a long shutdown hits the 60 s stop timeout" is
fine. Naming the actual service and the host it ran on is not.

## Review before every push and PR

Before you push, open a PR, or file or edit an issue:

1. Read the full diff (`git diff origin/main...HEAD`) and every commit message on the branch
   (`git log origin/main..HEAD`), plus the PR or issue text.
2. Look for the items above. A quick search helps but doesn't replace reading:
   `git diff origin/main...HEAD | grep -nEi '([0-9]{1,3}\.){3}[0-9]{1,3}|\.(dev|lan|local|home|internal)\b|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'`,
   then check every name in the diff that isn't Backtrack's own or a placeholder.
3. If something slipped in, fix it before pushing. Change the files, and reword the commit messages while the
   branch is still local. Once a commit is pushed, don't force-push. Tell the maintainer, who decides what to do.

Plain pushes only. Work on a branch, open a PR, and let the maintainer merge.
