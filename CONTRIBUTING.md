# Contributing to Alpha Stochastic Research

Thank you for contributing to Alpha Stochastic Research (ASR).

ASR repositories contain open-source Python libraries, reproducible research,
scientific documentation, datasets, figures, and working papers.

## Developer Certificate of Origin

ASR uses the [Developer Certificate of Origin 1.1](DCO.txt).

Every human-authored commit submitted to an ASR repository must contain a valid
`Signed-off-by` trailer whose name and email match the commit author.

Create a signed-off commit with:

```bash
git commit -s -m "Brief description of the change"
```

The commit message must contain:

```text
Signed-off-by: Firstname Lastname <email@example.com>
```

The DCO is a certification attached to each commit. It is not a copyright
assignment.

### Fix the latest unsigned commit

```bash
git commit --amend --signoff --no-edit
git push --force-with-lease
```

### Fix several unsigned commits

Use an interactive rebase and amend each unsigned commit:

```bash
git rebase -i origin/main
git commit --amend --signoff --no-edit
git rebase --continue
git push --force-with-lease
```

Replace `origin/main` with `origin/master` when the repository uses `master`.

Do not add another person's sign-off without their authorization.

## Contribution workflow

1. Create a dedicated branch.
2. Keep the change focused.
3. Add or update tests when behavior changes.
4. Update documentation when interfaces or results change.
5. Regenerate figures, tables, notebooks, and papers when source calculations change.
6. Sign off every commit with `git commit -s`.
7. Open a Pull Request.
8. Address automated checks and human review.

## Scientific contributions

Document the research question, assumptions, methodology, data provenance,
parameters, random seeds, validation, limitations, and reproduction instructions.

Do not manually edit generated results to make them agree with a manuscript.
Update the code or source data and regenerate the outputs.

## Software contributions

Use the repository's public Python namespace, preserve compatibility where
appropriate, add regression tests for bug fixes, avoid unnecessary dependencies,
and never commit secrets, credentials, personal data, or confidential material.

ASR Python research packages participate in the shared `asr` namespace and follow
the dependency policy defined by `asr-open-sc`.

## Review

Passing automated checks is necessary but not sufficient. Contributions may
require software, scientific, security, licensing, provenance, editorial, and
release review.

Only authorized maintainers or release administrators may merge into protected
release branches.

## Security

Do not disclose suspected vulnerabilities in a public issue. Follow the
`SECURITY.md` policy of the relevant repository.
