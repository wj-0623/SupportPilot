# Security Policy

Please do not open a public issue for a suspected vulnerability. Send a private report to the repository owner with the affected endpoint, reproduction steps, impact, and any suggested mitigation.

Never commit `.env`, API keys, customer exports, payment data, passwords, verification codes, or full identity numbers. Rotate any credential that was exposed, even briefly.

The demonstration data is fictional. The application redacts common long payment and identity numbers before storing chat messages, but production deployments must also use a dedicated data-loss-prevention layer and organization-specific retention rules.

