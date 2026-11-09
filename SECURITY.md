# Security

This public snapshot contains no owner credentials by design. Keep access tokens, cookies, DPAPI files, certificates and runtime session data **out of Git**.

Never upload local environment files or real user datasets. Configure credentials only in your own local environment. If any previously exposed credential is identified, revoke/rotate it with the provider; deleting a Git file does not invalidate a leaked token.
