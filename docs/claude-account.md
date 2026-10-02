# Connecting your Claude account

Your servers run under your own Claude account, so connect it before starting
one. Each person connects their own account; nobody else's is used.

## Connecting

1. On the Servers page, choose **Connect Claude account**, then **Get sign-in
   link**. rcman uses the email address you signed in to rcman with, unless
   you type the address of a different Claude account first.
2. Open **Sign in to Claude**, sign in, and allow access.
3. Claude shows a code. Paste it into **Code from Claude** and choose
   **Connect**.

rcman passes the code straight to Claude Code on this machine and keeps no
copy of it — it is not stored or written to any log. If the code is refused,
rcman shows why and offers a new sign-in link: each code works once.

## Staying connected

A Claude sign-in lasts a limited time. The Servers page shows the date yours
expires, and warns when it is close — how close is the administrators' setting
`RCMAN_LOGIN_WARNING_DAYS` (see [Configuration](configuration.md)). Once it
has expired, servers stop making progress until you connect again. Connecting
again at any time renews it.

When Claude Code does not say when a sign-in expires, rcman shows no date.

## When something looks unfamiliar

If Claude shows a sign-in screen rcman does not recognise, rcman shows it to
you as it is, and the attempt appears in the audit trail for administrators.

You can also change the prefix of your sessions' names in [Settings](settings.md).
