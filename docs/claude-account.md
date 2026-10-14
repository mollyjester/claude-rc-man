# Connecting your Claude account

Your workspaces' servers run Claude Code under your Claude account. You connect
it once, on the **Claude** page, and every workspace of yours uses it: those you
had made before, and those you make afterwards. Until you have connected it,
**Start** in a workspace answers *Connect your Claude account on the Claude
page.*

## Connecting

1. On the **Claude** page, choose **Connect Claude account**.
2. Leave the email empty to use the address you sign in to rcman with, or give
   the Claude account's own, and choose **Get sign-in link**.
3. Open **Sign in to Claude**, sign in to Claude there, and paste the code
   Claude shows you into **Code from Claude**. Choose **Connect**.

A code works once. If Claude refuses it, or you wait too long, get a new link
and start again.

The page then shows which account is connected, and when its sign-in expires,
if Claude says. Before it expires, the page warns you, as many days ahead as
your administrator chose (`RCMAN_LOGIN_WARNING_DAYS`, see
[Configuration](configuration.md)); connect again to renew it.

Connecting another account stops nothing: a server that is running keeps
going, and each server takes the new account's details the next time it
starts.

## Signing out of Claude

**Sign out of Claude** is on the **Claude** page while an account is connected.
**Keep sessions running** is ticked at first:

- **ticked**: servers that are running keep the account until the last of them
  stops; then you are signed out. Meanwhile no server starts, and the page says
  it is signing out. Connecting again ends the wait, and **Stop my servers and
  sign out now** finishes it at once.
- **not ticked**: your running servers are stopped, and you are signed out at
  once.

## Signing out of rcman

**Sign out**, beside your name at the top of every page, ends your rcman
session in this browser. Your servers do not depend on it: with **Keep sessions
running** ticked, as it is at first, they keep running; untick it to have them
stopped first. Your Claude account stays connected either way.

You can change how your sessions' names begin in [Settings](settings.md).
