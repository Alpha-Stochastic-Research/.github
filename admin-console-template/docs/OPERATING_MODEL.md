# Operating model

## Reconciliation

The organization workflow runs every {{SYNC_INTERVAL}} minutes and is
idempotent. It creates missing resources and updates only managed state.

## Announcement lifecycle

1. An authorized editor opens an announcement request.
2. The central workflow validates the author and payload.
3. The issue receives `asr-announcement-active`.
4. One generated digest issue is reconciled in every applicable repository.
5. Closing the source announcement removes it from future digests.

## Repository-request lifecycle

1. A team member opens a repository request and declares a team slug.
2. Membership is verified against GitHub Teams.
3. The repository is created from the selected ASR template or as a blank repo.
4. Team permissions, labels, topics and feature flags are applied.
5. The request is closed with a permanent audit comment.

## Incoming projects

The GitHub Project and `INCOMING_PROJECTS.md` contain the same calculated
signals. `data/repository-project-links.json` supplies explicit links when a
project title cannot be matched to a repository candidate automatically.

The GitHub Projects public API does not expose collaborator-role management.
A one-time setup issue records the exact access configuration required in the
GitHub UI: organization base role **Read**, with **Write** granted only to the
three editor teams.
