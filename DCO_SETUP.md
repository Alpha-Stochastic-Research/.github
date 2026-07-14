# ASR organization-wide DCO setup

Copy these files to the default branch of:

```text
Alpha-Stochastic-Research/.github
```

```text
DCO.txt
CONTRIBUTING.md
.github/PULL_REQUEST_TEMPLATE.md
```

## Automatic enforcement

The `.github` repository centralizes guidance and templates, but does not itself
execute a DCO check in every repository.

1. Open `https://github.com/apps/dco`.
2. Select **Configure**.
3. Choose **Alpha-Stochastic-Research**.
4. Select **All repositories**.
5. Confirm installation.

## Mandatory organization rule

After the app has run on a Pull Request:

1. Organization **Settings**.
2. **Repository → Rulesets**.
3. Edit the ruleset protecting `main`, `master`, or the default branch.
4. Enable **Require status checks to pass**.
5. Add the check **DCO**.
6. Set enforcement to **Active**.
7. Save.

Recommended companion rules:

```text
Restrict updates
Restrict deletions
Require a pull request before merging
Require status checks to pass: DCO
Require conversation resolution
Block force pushes
```

A bypass can also bypass DCO. Use the `release-admins` bypass only for documented
incident or release-recovery situations.

## Existing local contribution files

Organization defaults apply only when a repository does not already contain its
own contribution guide or Pull Request template. Update or remove local copies
when you want the ASR defaults to appear.

The DCO GitHub App still checks Pull Requests independently of those files.
