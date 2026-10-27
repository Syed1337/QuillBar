# Changelog

## 1.0.1 — unreleased

- Faster rewrites, especially with Gemini: one connection is reused instead of a new
  TCP+TLS handshake per request, and a per-provider **Thinking** setting is sent as
  `reasoning_effort`. Gemini defaults to Off (fixes #1, idea and measurements by @mreduar in #2).
- Gemini providers saved by 1.0 get the fast default automatically.
- If a model rejects the Thinking setting, Quillbar retries without it instead of failing.
- A connection dropped while the PC slept is retried once instead of showing a network error.

## 1.0 — 2026-10-07

First public release.

- Single-line toolbar with 19 built-in actions and custom buttons
- Global shortcut per button, number keys on the bar, mouse mode, double-tap
- Preview card: streaming, word-level diff, retry, refine
- Smart Reply, Summarize, Key points, Explain
- Per-app rules, personal style, two-way translate, placeholders
- Any OpenAI-compatible provider, model per button, automatic base-URL fixes
- DPAPI-encrypted keys, optional masking of emails/phones/cards, memory-only history
- Start with Windows, single-file app, one-click launcher
