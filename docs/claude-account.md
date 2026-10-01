# Connecting your Claude account

Your servers run under your own Claude account, so connect it before starting
one. Each person connects their own account; nobody else's is used.

## Connecting

1. Start connecting in rcman. It uses the email address you signed in to rcman
   with, unless you give the address of a different Claude account.
2. rcman shows a sign-in link. Open it, sign in to Claude, and allow access.
3. Claude shows a code. Paste it into rcman.

rcman passes the code straight to Claude Code on this machine and keeps no
copy of it — it is not stored or written to any log. If the code is refused,
start again: each code works once.

## Staying connected

A Claude sign-in lasts a limited time. rcman shows the date yours expires, and
warns when it is close — how close is the administrators' setting
`RCMAN_LOGIN_WARNING_DAYS` (see [Configuration](configuration.md)). Once it
has expired, servers stop making progress until you connect again. Connecting
again at any time renews it.

When Claude Code does not say when a sign-in expires, rcman shows no date.

## When something looks unfamiliar

If Claude shows a sign-in screen rcman does not recognise, rcman shows it to
you as it is, and the attempt appears in the audit trail for administrators.
