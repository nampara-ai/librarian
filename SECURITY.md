# Security Policy

## Supported Versions

Librarian 1.x receives security fixes on `main` and through patch releases when a fix affects a
published release.

## Reporting A Vulnerability

Do not open a public issue for suspected vulnerabilities.

Email security reports to `security@nampara.ai` with:

- affected version or commit SHA
- reproduction steps
- expected impact
- any relevant logs with secrets removed

We will acknowledge reports within 5 business days and coordinate a fix or disclosure plan.

## Secret Handling

- Never commit API keys, `.env` files, private transcripts, provider logs, or eval outputs that
  contain private text.
- Use environment variables for provider credentials.
- Redact `OPENAI_API_KEY`, custom provider keys, `LIBRARIAN_API_KEY`, and `LIBRARIAN_API_KEYS` in
  issues and logs.
- Test data must be synthetic, public-domain, or explicitly approved for open-source use.
- Run `gitleaks detect --source . --redact --verbose` before release candidates.

## Threat Model

See `docs/OPERATIONS.md` for the current operational security guidance covering API imports,
archive policy, provider data flow, logging, SQLite operations, and hosted-mode risks.

## Dependency Security

GitHub Dependabot and CodeQL are enabled for baseline dependency and code scanning. Maintainers
should review dependency alerts before cutting releases.
