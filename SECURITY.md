# Security policy

The management console currently assumes a trusted local or LAN deployment and does not provide user authentication. Do not expose it directly to the public Internet.

- Bind it to a trusted interface or place it behind an authenticated reverse proxy.
- Keep the bundled frontend and API on the same origin. Set `WEB_CORS_ORIGINS` only for explicitly trusted separate frontends.
- Store credentials in `.env`, the system settings store, or referenced environment variables. Never commit live secrets to plugin manifests.
- Treat database files, call history, chat logs, memory records and downloaded media as private runtime data.

When reporting a vulnerability, use a private repository security advisory when available. Do not include live API keys, chat content or personal data in a public issue.
