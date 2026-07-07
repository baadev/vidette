"""Authentication: first-run bootstrap, sessions, scoped API tokens.

Security rules enforced here (docs/architecture/security-model.md): no default credentials;
`auth: none` is explicit and loudly warned; only token *hashes* are stored; password hashing
is stdlib scrypt (no native deps); login errors are uniform (no username oracle).
"""
