# Security

The VPS is a trusted intermediary: it sees the complete context and Anthropic
authentication headers so it can forward the original request. Use a server you
control. This is not end-to-end encryption between Claude Code and Anthropic.

The public relay requires a separate random bearer token over verified HTTPS.
The installer generates a self-signed certificate with the VPS IPv4 SAN and
copies that certificate over SSH as the local trust anchor. Never disable TLS
verification or use the relay token as an Anthropic credential. The certificate
lasts 365 days; replace it and distribute the new trust anchor before expiry.

Private credentials live outside the checkout in `~/.config/claude-proxy/`
(or `$XDG_CONFIG_HOME/claude-proxy/`) and `/etc/claude-proxy/`. Logs contain byte
counts, not request bodies or headers. Context chunks are kept in a bounded
in-memory cache, not on disk. Clients sharing a relay token share that cache;
use separate deployments for mutually untrusted users. The loopback listener
is not authenticated, so other processes/users on the client machine may use
it. Secure both hosts and their local accounts accordingly.

The service uses a fixed upstream origin, does not follow redirects, and checks
frame sizes, checksums and request hashes. It does not automatically replay
requests after uncertain network delivery. A cache-miss retry occurs before
any upstream request. Body limits do not constitute multi-tenant rate limiting
or protection against a trusted client deliberately consuming resources.

To report a vulnerability, use this repository's GitHub private vulnerability
reporting if available. Otherwise open an issue requesting a private contact
without including exploit details, tokens, private keys, or request captures.
No external security audit has been performed.
