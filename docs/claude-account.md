# Connecting your Claude account

Your workspaces' servers run Claude Code under your Claude account. You connect
it once, on the **Claude** page, and every workspace of yours uses it: those you
had made before, and those you make afterwards. Until you have connected it,
**Start** in a workspace answers *Connect your Claude account on the Claude
page.*

## Connecting

1. On the **Claude** page, choose **Connect Claude account**.
2. Leave **Claude account email** empty to use the address you sign in to rcman
   with, or give the Claude account's own, and choose **Get sign-in link**.
3. Open **Sign in to Claude**, sign in to Claude there, and paste the code
   Claude shows you into **Code from Claude**. Choose **Connect**.

A code works once. If Claude refuses it, or you wait too long, get a new link
and start again.

The page then shows the account in a card: its address, its state, its
organisation, its plan and when its sign-in expires, as far as Claude says.
Before it expires, the page warns you, as many days ahead as your administrator
chose (`RCMAN_LOGIN_WARNING_DAYS`, see [Configuration](configuration.md));
connect again to renew it.

Connecting another account stops nothing: a server that is running keeps
going, and each server takes the new account's details the next time it
starts.

## Signing out of Claude

**Sign out of Claude** is in the **Sign out** section at the foot of the
**Claude** page while an account is connected.
**Keep servers running until the last one stops** is ticked at first:

- **ticked**: servers that are running keep the account until the last of them
  stops; then you are signed out. Meanwhile no server starts, and the page says
  it is signing out. Connecting again ends the wait, and **Stop my servers and
  sign out now** finishes it at once.
- **not ticked**: your running servers are stopped, and you are signed out at
  once.

## When the sign-in disappears

Your workspaces share one file for the sign-in, and Claude Code in each of them writes it
when it renews its token. Claude Code takes a lock for that, but only among the programs of one
workspace, so two of your workspaces can renew at the same moment, and the file can be left
empty or torn. rcman does not prevent this; it is a known risk it has accepted. What rcman does
is notice: when the **Claude** page finds your account connected but the file holds no sign-in,
and you did not sign out, it says: *Your Claude sign-in disappeared on its own, for example
because two refreshes collided. Connect your Claude account again to start servers.* Choose
**Connect Claude account** and connect again; nothing else of your workspaces is lost.

## Signing out of rcman

Your avatar, the first letter of your address at the right of the top bar on
every page, opens the **Account** menu, which says who you are signed in as.
**Sign out of rcman**, in that menu, ends your rcman session in this browser.
Your servers do not depend on it: with **Keep servers running after sign-out**,
beside it in the menu, ticked, as it is at first, they keep running; untick it
to have them stopped first. Your Claude account stays connected either way.
Signing out ends your workspaces' terminals, in every browser (see
[The terminal](workspaces.md#the-terminal)).

You can change how your sessions' names begin in [Settings](settings.md).
