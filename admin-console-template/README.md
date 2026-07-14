# ASR Admin Console

Central GitHub-native administration for **{{ORGANIZATION}}**.

## What this repository provides

### Announcements

Create an issue with **Publish an ASR announcement**.

- **General** announcements are published to every active repository.
- **Specific** announcements are published only to the selected repositories.
- Every repository receives one managed `[ASR] Announcements` issue.
- Newly created repositories receive the current announcement digest automatically.

Announcement publication is restricted to:

{{EDITOR_TEAMS}}

### Repository creation

Any active member of an organization team can submit **Request a repository**.
The requester must provide the exact team slug and the automation verifies active
membership before creating the repository.

New repositories are automatically provisioned with:

- the organization team-access policy;
- standard labels and repository features;
- the current announcement digest;
- project tracking and automatic progress discovery.

### Incoming projects

- [Open the GitHub Project]({{INCOMING_PROJECT_URL}})
- [Open the generated status dashboard](INCOMING_PROJECTS.md)

Progress is recalculated every **{{SYNC_INTERVAL}} minutes** from repository
existence, issue completion, merged pull requests, releases and recent pushes.

## Security model

All organization teams receive read access to this repository. Write access is
restricted to `release-admins`, `asr-leadership`, and `asr-researchers`.

The automation is operated from the organization `.github` repository so
administrative tokens remain centralized. This repository contains request
forms, generated dashboards and audit records only.

See [Operating model](docs/OPERATING_MODEL.md) and [Security](SECURITY.md).
