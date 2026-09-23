# Security Policy

## Supported versions

Solomon is pre-1.0 (currently v0.4.0-alpha). There is no long-term
support branch yet -- security fixes land on the latest alpha/beta.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security
vulnerability.**

Instead, use GitHub's private vulnerability reporting for this
repository (Security tab -> "Report a vulnerability"). This reaches the
maintainer(s) privately and creates a private advisory to coordinate a
fix before public disclosure.

Please include:
- A description of the issue and its potential impact.
- Steps to reproduce, or a minimal proof of concept.
- Which component is affected (e.g. a specific adapter, the policy
  engine, the addon manifest validator, the diagnostics export
  redaction logic).

## Scope notes specific to this project

A few areas where a security report is especially valuable, given how
this project is designed:

- **Policy/approval bypass**: anything that lets a task skip the
  Human Approval gate, weaken the autonomy dial's safety floor, or
  proceed past a Token Budget hard-stop without going through approval.
- **Diagnostics export redaction**: any case where `solomon
  diagnostics-export` (default, redacted mode) includes a prompt,
  approval reason, project file path, secret-shaped string, or note
  content it shouldn't.
- **Project isolation**: any case where one project's knowledge,
  usage data, or approval history becomes visible from another
  project's context.
- **Addon manifest validation**: addon *execution* is not implemented
  in this codebase (manifests are discovered/validated only, never
  run) -- but a validator bug that misclassifies a manifest's
  permissions is still worth reporting.

We'll acknowledge reports as promptly as we can and credit reporters
(unless you'd prefer to stay anonymous) once a fix ships.
