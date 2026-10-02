# Git

rcman works with the git repositories in your home folder as you would in a
terminal: as you, with your own git configuration.

## Changes

A file can be listed as one of these changes:

| Change | Shown as | Meaning |
|---|---|---|
| `modified` | Modified | Its content changed |
| `type_changed` | Type changed | It became a link, or stopped being one |
| `added` | Added | It is new in what is staged |
| `deleted` | Deleted | It was deleted |
| `renamed` | Renamed | It was renamed; the name it had is shown too |
| `copied` | Copied | It is a copy of another file, which is shown too |
| `unmerged` | Conflict | A merge left it with a conflict to resolve |
| `untracked` | Untracked | git does not track it yet |
