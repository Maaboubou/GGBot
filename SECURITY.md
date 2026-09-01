# Security policy

The management console currently assumes a trusted local or Tailnet deployment and does not provide its own user login. Its default `WEB_HOST=127.0.0.1` accepts local connections only. Use Tailscale Serve for Tailnet access and do not expose port 8888 directly to the public Internet.

- Keep inbound port 8888 closed. Tailscale Serve can proxy the loopback listener to authenticated members of the same Tailnet; this project does not enable Funnel.
- Keep the bundled frontend and API on the same origin. Set `WEB_CORS_ORIGINS` only for explicitly trusted separate frontends.
- Store credentials in `.env`, the system settings store, or referenced environment variables. Never commit live secrets to plugin manifests.
- Treat GitHub Skills as executable third-party code. The console validates and installs their files without executing scripts or dependency installers; review and pin a commit before enabling a Skill or installing its declared dependencies.
- Treat database files, call history, chat logs, memory records and downloaded media as private runtime data.

When reporting a vulnerability, use a private repository security advisory when available. Do not include live API keys, chat content or personal data in a public issue.
