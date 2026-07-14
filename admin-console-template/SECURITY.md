# Security policy

## Administrative boundaries

The admin console does not store GitHub tokens. Organization automation runs
from `{{ORGANIZATION}}/.github` using organization-managed Actions secrets.

Write access to this repository is limited to:

{{EDITOR_TEAMS}}

All other organization teams receive read access.

## Reporting a security issue

Do not publish credentials, private research data or vulnerability details in a
public issue. Contact ASR Leadership or `release-admins` through the
organization's approved private channel.

## Repository creation safeguards

Repository requests are accepted only when the submitting user is an active
member of the declared organization team. Names, visibility, templates and
project links are validated before mutation.
