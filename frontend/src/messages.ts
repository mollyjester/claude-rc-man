// Every piece of text the interface shows that does not come from the server.
export const m = {
  appName: "rcman",
  // The browser tab's title for a page.
  pageTitle: (page: string) => `${page} – rcman`,
  skipToContent: "Skip to content",
  unexpectedResponse: "rcman answered in a way the page does not understand.",
  unreachable: "The page could not reach rcman. Check the connection, then try again.",
  signedOut: "Your sign-in has ended. Reload the page to sign in again.",
  loading: "Loading…",
  cancel: "Cancel",
  close: "Close",
  // The cross at the top of every dialog, which closes it as Escape does.
  closeDialog: "Close dialog",
  save: "Save",
  saved: "Saved.",

  navDashboard: "Workspaces",
  navClaude: "Claude",
  navSettings: "Settings",
  navDocs: "Help",
  // The sidebar's group of the administrators' pages.
  navAdmin: "Administration",
  // The sidebar's name, and the button that opens it as a drawer on a phone.
  navMain: "Pages",
  navMenu: "Menu",
  navUsers: "People",
  navAudit: "Audit trail",
  navEveryone: "Everyone's workspaces",
  navVersions: "Claude Code versions",
  navMonitor: "Monitor",
  // The Upgrade rcman page's name in the sidebar, a noun as its neighbours' are; not "rcman"
  // alone, which is the top bar's link home.
  navRcman: "rcman release",
  signedInAs: (email: string) => `Signed in as ${email}`,
  // The top bar's menu of the person signed in, behind their avatar: who they are, and
  // signing out of rcman.
  account: "Account",

  // The list's empty state: its title and its one line, over New workspace.
  workspacesEmpty: "No workspaces yet",
  workspacesEmptyBody: "Make one to run a Claude server in it, empty or cloned from GitHub.",
  // The button beside the page's heading, and the dialog it opens.
  newWorkspace: "New workspace",
  // New workspace's words where there is little room, as on a phone; its name stays the above.
  newWorkspaceShort: "New",
  // How many workspaces the list holds, beside New workspace on a phone.
  workspaceCount: (count: number) => (count === 1 ? "1 workspace" : `${count} workspaces`),
  // Over a workspace's panel where the list is not beside it, as on a phone: the way back to the
  // list, and the switcher, which shows the workspace's name and opens the others as a sheet.
  backToWorkspaces: "Back to workspaces",
  switchWorkspace: "Switch workspace",
  switchWorkspaceFrom: (name: string) => `${name}, switch workspace`,
  newWorkspaceName: "Name of the new workspace",
  newWorkspaceNameHelp: "Leave it empty with an address to use the repository's name.",
  newWorkspaceSource: "GitHub address (optional)",
  newWorkspaceSourceHelp: "Cloned into the workspace; without one, it starts empty.",
  create: "Create",
  creating: "Creating…",
  workspacePanelEmpty: "Select a workspace to see its Claude server, or make one.",
  workspaceUnknown: "You have no workspace at this address. Select one from the list.",
  workspaceStates: {
    creating: "Being made",
    ready: "Ready",
    failed: "Failed",
    deleting: "Being deleted",
  } as Record<string, string>,
  // Beside New workspace while rcman is being upgraded, which makes none meanwhile.
  workspacesUpgrading:
    "rcman is being upgraded, so no workspace can be made or rebuilt until the upgrade is done.",
  workspaceCreating: "This workspace is being made. It is ready in a moment.",
  workspaceCloningCancel: "To stop the clone, delete this workspace with its button in the list.",
  workspaceLog: "Steps so far",
  workspaceFailed: "Making this workspace failed:",
  workspaceFailedClone:
    "rcman could not copy this repository. Check the address; a private repository needs a GitHub token in Settings.",
  workspaceFailedMake: "rcman could not make this workspace.",
  detailsTitle: "Details",
  serverUnreadNextStep:
    "This page asks again by itself. If it stays like this, tell an administrator.",
  workspaceDeleting: "This workspace is being deleted.",
  // The trash button of a workspace's row in the list.
  deleteWorkspaceNamed: (name: string) => `Delete workspace ${name}`,
  deleteWorkspaceTitle: (name: string) => `Delete ${name}?`,
  deleteWorkspaceBody:
    "The workspace and everything in it are deleted, and its server is stopped and deleted with it. This cannot be undone.",
  // What the Changes section knows of that would be lost, worked out from its counts.
  deleteWorkspaceUncommitted: (files: number) =>
    files === 1
      ? "One file in it has changes that are not committed."
      : `${files} files in it have changes that are not committed.`,
  deleteWorkspaceUnpushed: (commits: number) =>
    commits === 1
      ? "One commit in it has not been pushed."
      : `${commits} commits in it have not been pushed.`,
  deleteWorkspaceChecking: "Checking for work that is not committed or not pushed…",
  deleteWorkspaceUnchecked:
    "rcman could not check for work that is not committed or not pushed, so some may be lost.",
  deleteWorkspaceConfirm: "Delete",
  // Why a workspace cannot be deleted now, said when its trash button is pressed.
  cannotDeleteTitle: (name: string) => `${name} cannot be deleted now`,
  cannotDeleteCreating:
    "It is still being made. It can be deleted once it is ready, or if making it fails.",
  cannotDeleteDeleting: "It is already being deleted.",
  cannotDeleteState: (state: string) => `It cannot be deleted while it is ${state}.`,
  deleting: "Deleting…",
  workspaceDeleted: (name: string) => `${name} was deleted.`,
  start: "Start",

  // The tabs of a ready workspace's panel.
  workspaceTabs: "Workspace views",
  tabOverview: "Overview",
  tabTerminal: "Terminal",

  // The Terminal tab: how its connection stands, and what ended it.
  terminalConnecting: "Connecting to the workspace's shell…",
  terminalConnected: "Connected to the workspace's shell.",
  terminalReconnect: "Reconnect",
  terminalNotOpened: "The terminal could not be opened.",
  terminalNotLoaded: "The terminal could not be loaded. Reload the page to load it again.",
  reloadPage: "Reload the page",
  terminalLost: "The connection to the terminal was lost.",
  terminalExited: (code: number | null) =>
    code === null ? "The shell ended." : `The shell ended, with exit code ${code}.`,
  // What a terminal's closed frame says, by its reason; one with no words is shown as it is.
  terminalClosed: {
    terminal_replaced:
      "The terminal was opened somewhere else, which took it over. Reconnect to take it back here.",
    workspace_gone: "The workspace is no longer ready, so its terminal was closed.",
    workspace_rebuilding:
      "The workspace is being rebuilt, which closed the terminal. Reconnect once the rebuild is done.",
    signed_out: "You signed out of rcman, which closed the terminal.",
    not_allowed: "You are no longer allowed into rcman, which closed the terminal.",
    service_closing: "rcman is restarting, which closed the terminal.",
  } as Record<string, string>,
  terminalClosedOther: (reason: string) => `The terminal was closed: ${reason}.`,

  serverTitle: "Claude server",
  noServer: "No Claude server runs in this workspace.",
  // While a rebuild replaces the workspace's container, its server is not read.
  serverRebuilding:
    "The workspace's container is being made again, so its server is not running just now. A server that ran is started again once that is done.",

  // The Container card of a ready workspace's Overview, and rebuilding a workspace.
  containerTitle: "Container",
  containerIntro:
    "Made from the repository's devcontainer.json, or from rcman's default image where it has none.",
  containerEmpty:
    "This workspace has no repository, so its container is made from rcman's default image and has nothing to be rebuilt from.",
  devcontainerChanged:
    "This workspace's devcontainer.json has changed since its container was made. Rebuild to apply it.",
  rebuild: "Rebuild",
  rebuildBusy: "Rebuilding…",
  rebuildRunning: "This workspace is being rebuilt. The steps below say how far it is.",
  // Before what the server says of a rebuild that did not finish.
  rebuildFailed: "The last rebuild did not finish:",
  rebuildTitle: (name: string) => `Rebuild ${name}?`,
  rebuildIntro:
    "rcman pulls or builds the image the devcontainer.json in the workspace asks for now, while everything keeps running. Only then is the server stopped, the container made again and the server started again.",
  rebuildIntroFailed:
    "rcman pulls or builds the image the devcontainer.json in the workspace asks for now, then makes the workspace's container again on its existing files.",
  rebuildEndsTitle: "This ends:",
  rebuildEnds: [
    "The terminal, and every process in the workspace, those started with nohup or setsid too.",
    "The containers running inside the workspace.",
    "What is in /tmp, the container's own files, and what the earlier create commands installed outside the workspace's files.",
  ],
  rebuildKeepsTitle: "This is kept:",
  rebuildKeeps: [
    "The files in the workspace: the work folder, your home, Claude's settings and the storage of Podman inside it.",
    "Your Claude sign-in, and git.",
  ],
  rebuildCommands:
    "The repository's lifecycle commands (onCreateCommand, updateContentCommand, postCreateCommand and postStartCommand) run again, so they must be safe to run again.",
  rebuildCommandsFailed:
    "The repository's lifecycle commands run again on the existing files. If the rebuild fails again, the workspace stays failed: there is no earlier state to return to.",
  rebuildMemory:
    "The build runs beside what runs in your workspaces and shares your memory limit. Going over it ends the largest process, which may be a live Claude session.",
  rebuildConfirm: "Rebuild",
  states: {
    starting: "Starting",
    trust_prompt: "Waiting for your answer",
    remote_control_prompt: "Waiting for your answer about Remote Control",
    connected: "Ready to accept sessions",
    ready: "In use",
    stopped: "Stopped",
    trust_declined: "Not started: workspace not trusted",
    logged_out: "Not started: Claude not signed in",
    exited: "Stopped by itself",
    reconnected: "Reconnected, status not shown",
    unrecognised: "Unrecognised screen",
  } as Record<string, string>,
  kinds: {
    running: "Running",
    waiting: "Needs your answer",
    problem: "Has a problem",
    stopped: "Not running",
  },
  nextStep: {
    starting: "Claude is starting. Wait a moment.",
    trust_prompt: "Answer whether you trust this workspace.",
    remote_control_prompt:
      "Answer whether to enable Remote Control. Not now ends the server, and Claude asks again next time.",
    connected: "Open it in claude.ai to start a session.",
    ready: "A session is attached; continue it in claude.ai.",
    stopped: "Resume to pick up its sessions, or forget it.",
    trust_declined: "Start again to answer once more, or forget it.",
    logged_out: "Claude is not signed in in this workspace. Start again once it is, or forget it.",
    exited: "Show the screen to see why it stopped, then resume it.",
    reconnected:
      "Claude says it reconnected but is not showing its status. Show the screen to see what it says.",
    unrecognised: "Show the screen, stop it, and tell an administrator.",
  } as Record<string, string>,
  nextStepUnknown: "rcman does not know this state. Show the screen to see what Claude says.",
  stop: "Stop",
  resume: "Resume",
  startAgain: "Start again",
  forget: "Forget",
  showScreen: "Show screen",
  openSession: "Open in claude.ai",
  sessionName: "Session",
  exitCode: (code: number) => `Exit status ${code}`,
  screenTitle: (name: string) => `Screen of ${name}`,

  trustTitle: "Trust this workspace?",
  trustBody: (name: string) =>
    `Sessions started in ${name} can read, edit and run the files in it, and its own Claude settings apply to them.`,
  trustYes: "Yes, trust it",
  trustNo: "No",

  remoteControlTitle: "Enable Remote Control?",
  remoteControlBody:
    "Claude asks this until Remote Control is enabled for your account. Enabled, the server keeps running here and you pick up its sessions from claude.ai/code or the Claude mobile app. Not now ends the server without starting it, and Claude asks again the next time it starts.",
  enableRc: "Enable",
  notNow: "Not now",

  claudeTitle: "Claude account",
  claudeNotConnected: "Your Claude account is not connected. Connect it to start servers.",
  claudeSignedOutUnexpectedly:
    "Your Claude sign-in disappeared on its own, for example because two refreshes collided. Connect your Claude account again to start servers.",
  // The sheet's title where no account is connected; otherwise the account's address.
  claudeNoAccount: "No Claude account connected",
  // The account's state, as the sheet's pill.
  claudeStates: {
    connected: "Connected",
    expiring: "Expiring soon",
    expired: "Expired",
    signing_out: "Signing out",
  } as Record<string, string>,
  // A fact of the account Claude did not give.
  claudeUnknown: "Not known",
  claudeOrg: "Organisation",
  claudePlan: "Plan",
  claudeExpiresAt: "Sign-in expires",
  claudeExpiring: (when: string) =>
    `Your Claude sign-in expires ${when}. Connect again to renew it.`,
  claudeExpired: "Your Claude sign-in has expired. Connect again to keep your servers working.",
  claudeSigningOut:
    "Signing out: your running servers keep this account until the last one stops, and no new server starts meanwhile.",
  claudeSignOut: "Sign out of Claude",
  claudeSignOutTitle: "Sign out",
  claudeSignOutLine: "Running servers can keep the account until the last one stops.",
  claudeSignOutNow: "Stop my servers and sign out now",
  // On the Claude page: running servers keep the account until the last one stops.
  keepSessions: "Keep servers running until the last one stops",
  // In the top bar's Account menu: signing out of rcman, which never ends a server by itself.
  keepServersOnSignOut: "Keep servers running after sign-out",
  signOut: "Sign out of rcman",
  connect: "Connect Claude account",
  reconnect: "Connect again",
  connectEmail: "Claude account email",
  connectEmailHelp: "Leave it empty to use the address you sign in with.",
  connectGo: "Get sign-in link",
  connectStep: "Open this link, sign in to Claude, and paste the code Claude shows you:",
  connectLink: "Sign in to Claude",
  connectCode: "Code from Claude",
  connectSubmit: "Connect",

  settingsTitle: "Settings",
  prefixLabel: "Session name prefix",
  prefixHelp: "Leave it empty to name sessions after each workspace.",
  settingsPrefixTitle: "Session names",
  settingsPrefixLine: "How your new Remote Control sessions are named in claude.ai.",
  settingsAuthorTitle: "Commit author",
  settingsAuthorLine: "Who commits from rcman and in your workspaces are recorded as.",
  authorNameLabel: "Commit author name",
  authorNameHelp: "Used for commits from rcman and by git in your workspaces.",
  authorEmailLabel: "Commit author email",
  authorEmailHelp: "Leave it empty to use the address you sign in with.",
  authorEffective: (name: string, email: string) => `Commits are recorded as ${name} <${email}>.`,
  authorNotSet: "No commit author is set.",

  settingsGithubTitle: "GitHub token",
  githubTokenLabel: "GitHub token",
  settingsGithubLine: "Lets git and gh in your workspaces reach GitHub as you.",
  githubTokenHelp: "Used by git and gh in all your workspaces; never shown again.",
  githubTokenStored: "A GitHub token is stored.",
  githubTokenNotStored: "No GitHub token is stored.",
  githubTokenSaved: "Token saved.",
  githubTokenRemove: "Remove token",
  githubTokenRemoveTitle: "Remove your GitHub token?",
  githubTokenRemoveBody:
    "git and gh in your workspaces, and in your Claude sessions, can no longer use it, from the next git or gh command on. It still works on GitHub until you revoke it there.",
  githubTokenRemoveConfirm: "Remove",

  usersTitle: "People",
  usersEmail: "Google email",
  usersRole: "Role",
  usersWorkspaces: "Workspaces",
  usersAdmin: "Administrator",
  usersUser: "User",
  usersAdd: "Add person",
  usersAddGo: "Add",
  usersAddAdmin: "Administrator",
  usersAdded: (email: string) => `${email} was added.`,
  usersRemove: "Remove",
  usersRemoveTitle: (email: string) => `Remove ${email}?`,
  usersRemoveBody:
    "Their workspaces are deleted, with everything in them and their servers, and they can no longer sign in. This cannot be undone.",
  usersRemoveConfirm: "Remove",
  usersRemoved: (email: string) => `${email} was removed.`,
  usersMemory: "Memory",
  usersCpu: "CPU",
  usersProcesses: "Processes",
  usersNoLimit: "No limit",
  usersLimits: "Limits",
  usersActions: "Actions",
  usersMoreActions: "More actions",
  usersMoreActionsFor: (email: string) => `More actions for ${email}`,
  limitsTitle: (email: string) => `Limits for ${email}`,
  // One line under each field: what it takes, and that empty is no limit.
  limitsHints: {
    memory: "A whole number then M or G; empty for none.",
    cpu: "Cores, decimals allowed; empty for none.",
    pids: "Threads count too; empty for none.",
  },
  limitsHowTitle: "How limits work",
  limitsHeldToPerson:
    "A workspace is also held to its person's limits: all of a person's workspaces together stay within the person's limits.",
  limitsMemoryHard:
    "A memory limit is hard. When a group goes over it, the kernel ends the largest process in that group, which can be the session of a sibling workspace. So give each workspace a smaller memory limit than its person's.",
  limitsBelowUse:
    "A memory or process limit lower than what is in use now is refused, so that no running work is ended. Raising or removing a limit, and any change to CPU, is always allowed.",
  limitsNotLimited: "Disk and I/O cannot be limited.",
  limitsPersonScope: (email: string) => `All workspaces of ${email}`,
  // The workspace's name, and, while it is not ready, the word for its state.
  limitsWorkspaceScope: (name: string, state: string | null) =>
    state === null ? name : `${name}, ${state}`,
  limitsUsage: (memory: string, tasks: number) =>
    `In use now: ${memory} of memory, ${tasks} processes.`,
  limitsUsageUnknown: "What is in use now could not be read.",
  limitsSaveFor: (scope: string) => `Save limits for ${scope}`,
  limitsSaving: "Saving…",
  limitsCloseWaits: "The dialog can be closed once saving is done.",
  limitsSaved: (scope: string) => `Limits for ${scope} saved.`,

  auditTitle: "Audit trail",
  auditWhen: "When (UTC)",
  auditWho: "Who",
  auditAction: "Action",
  auditTarget: "Target",
  auditResult: "Result",
  auditOlder: "Older",
  auditNoTarget: "None",
  auditFailed: "Failed",
  // Each action docs/audit.md lists, by its code, in plain words; src/audit-guide.test.ts holds
  // the two together. An action it has no words for is shown by its code.
  auditActions: {
    "workspace.create": "Made a workspace",
    "workspace.delete": "Deleted a workspace",
    "workspace.rebuild": "Asked for a workspace to be rebuilt",
    "server.start": "Started a server",
    "server.resume": "Resumed a server",
    "server.stop": "Stopped a server",
    "server.forget": "Forgot a stopped server",
    "server.confirm": "Answered a question a server asked",
    "me.sign_out": "Signed out of rcman",
    "settings.update": "Changed settings",
    "person.add": "Added a person",
    "person.delete": "Removed a person",
    "person.limits": "Set a person's limits",
    "workspace.limits": "Set a workspace's limits",
    "git.stage": "Staged files",
    "git.unstage": "Unstaged files",
    "git.commit": "Committed",
    "git.switch": "Switched branch",
    "git.branch": "Made a branch",
    "git.fetch": "Refreshed remote branches",
    "git.sync": "Synced a branch",
    "git.push": "Pushed",
    "git.commit_push": "Committed and pushed",
    "git.clone": "Made a workspace from a repository",
    "github.token": "Saved a GitHub token",
    "github.forget": "Removed a GitHub token",
    "claude.login": "Started connecting a Claude account",
    "claude.connect": "Connected a Claude account",
    "claude.disconnect": "Signed out of Claude",
    "claude.upgrade": "Rolled out a Claude Code version",
    "rcman.upgrade": "Asked for rcman to be upgraded",
    "monitor.settings": "Changed the monitoring settings",
    "terminal.open": "Opened a terminal",
    "terminal.close": "A terminal closed",
  } as Record<string, string>,
  // The results docs/audit.md lists; any other result is the code of the error the request
  // failed with, shown by its code beside the word for a failure.
  auditResults: {
    ok: "Done",
    unrecognised_screen: "Done, but the server is on a screen rcman does not recognise",
    committed_not_pushed: "Committed, but the push failed",
    pulled_not_pushed: "Pulled, but the push failed",
    terminal_lost: "Its page closed it or went away",
    terminal_replaced: "Another terminal on the workspace replaced it",
    workspace_gone: "Its workspace was deleted or is no longer ready",
    workspace_rebuilding: "Its workspace was being rebuilt",
    signed_out: "Signed out of rcman",
    service_closing: "rcman stopped",
  } as Record<string, string>,
  auditEmpty: "Nothing recorded.",

  attentionTitle: "Needs attention",
  attentionUnrecognised: (servers: string[]) =>
    `Servers on a screen rcman does not recognise: ${servers.join(", ")}.`,

  everyoneTitle: "Everyone's workspaces",
  everyoneIntro:
    "Each person's workspaces and their servers, to look at only. Only the person a workspace belongs to can act on it.",
  everyoneNone: "No workspaces.",
  everyoneWorkspace: "Workspace",
  everyoneNoServer: "No server",

  // The Upgrade rcman page, for administrators: its heading, its button and its dialog are all
  // the same words; its name in the sidebar is navRcman.
  upgradeRcman: "Upgrade rcman",
  upgradeIntro:
    "Installs the newest main of rcman's clone on this server, then restarts rcman. Workspaces and their servers keep running.",
  upgradeFactsTitle: "Release",
  upgradeRelease: "Running release",
  upgradeReleaseUnknown: "Not known: rcman runs from a checkout, not from a release",
  upgradeLast: "Last upgrade",
  upgradeStates: {
    unknown: "None recorded",
    requested: "Asked for",
    running: "Running",
    succeeded: "Succeeded",
    failed: "Failed",
    not_started: "Did not start",
  } as Record<string, string>,
  upgradeStarted: "Started (UTC)",
  upgradeFinished: "Finished (UTC)",
  upgradeFrom: "Upgraded from",
  upgradeTo: "Upgraded to",
  upgradeNotices: "What the installer asks of you",
  // A sentence that ends in a link to the guide, its words then the link's.
  upgradeOff:
    "Upgrading rcman from here is off. An administrator turns it on with the installer, from a shell, as the installation guide says in",
  upgradeOffLink: "Upgrading from the web interface",
  upgradeConfirm: "rcman installs the newest main of its clone, then restarts. While it does:",
  upgradeConsequences: [
    "Workspaces and their Claude servers keep running.",
    "Every open terminal closes. Reconnect once rcman is back.",
    "A git action or a Claude sign-in in progress is cut off. Do it again afterwards.",
    "No workspace can be made and no Claude Code version rolled out.",
  ],
  upgradeRequested: "The upgrade is asked for. Waiting for the server to start it…",
  upgradeRunning: "rcman is being upgraded. It restarts on the way, and this page waits for it…",
  upgradeRestarting: "rcman is restarting. This page waits for it…",
  upgradeSucceeded: "rcman is upgraded. Reload the page to use the new version.",
  upgradeReload: "Reload page",
  upgradeFailed: "The upgrade failed:",
  upgradeFailedUnsaid: "The upgrade failed, and said nothing of why.",
  upgradeNotStarted:
    "The upgrade did not start: nothing on the server acted on the request. Try again, or run the installer from a shell.",
  upgradeRollBack:
    "rcman was not rolled back. To go back to the release before, roll back from a shell, as the installation guide says in",
  upgradeRollBackLink: "Upgrading and rolling back",

  versionsTitle: "Claude Code versions",
  versionsIntro:
    "Every workspace runs Claude Code from the versions installed here. A server keeps the version it started with until its owner stops it and starts it again.",
  versionsFactsTitle: "Versions",
  versionsApproved: "Approved version",
  versionsPrevious: "Previous version",
  versionsTested: "Tested version",
  versionsNone: "None",
  versionsInstalledTitle: "Installed versions",
  versionsVersion: "Version",
  versionsStanding: "Standing",
  versionsStandingApproved: "Approved",
  versionsStandingPrevious: "Previous",
  versionsServersUsing: "Running servers",
  versionsServers: (count: number) => (count === 1 ? "1 server" : `${count} servers`),
  versionsActions: "Action",
  versionsUse: "Use",
  versionsUseVersion: (version: string) => `Use ${version}`,
  versionsDriftTitle: "Servers on another version",
  versionsDriftNone: "Every running server runs the approved version.",
  versionsOwner: "Owner",
  versionsRunning: "Running",
  versionsRolloutTitle: "Roll out a version",
  versionsUpgrade: "Version to roll out",
  versionsUpgradeHelp: "Installed if needed and checked; approved only if the checks pass.",
  versionsUpgradeGo: "Check and roll out",
  versionsUpgradeBusy: "Checking…",
  versionsRolling: (version: string) =>
    `Checking Claude Code ${version}. This can take a few minutes.`,
  versionsUpgraded: (version: string) =>
    `Claude Code ${version} is approved. Servers that start from now on run it.`,
  versionsBusyElsewhere: "Another version is being rolled out. Try again when it is done.",
  versionsRcmanUpgrading:
    "rcman is being upgraded, so no version can be rolled out until the upgrade is done.",
  versionsLatestTitle: "Newest release",
  versionsLatest: (version: string) => `The newest release is ${version}.`,
  versionsLatestApproved: "The approved version is the newest release.",
  versionsLatestUnknown: "rcman could not find out which release is the newest.",
  versionsRollout: "Roll out",
  versionsRolloutVersion: (version: string) => `Roll out ${version}`,
  versionsFailedChecks: "Failed checks:",
  versionsReportMore: (count: number) => `${count} more lines not shown`,

  serverClaudeVersion: (version: string) => `Claude Code ${version}`,
  serverVersionDiffers: (approved: string) =>
    `Claude Code ${approved} is approved now. Stop this server and start it again to use it.`,

  // What an empty field shows of what goes in it; the field's label still names it.
  examples: {
    workspaceName: "my-project",
    address: "https://github.com/owner/repository",
    email: "name@example.com",
  },
  docsTitle: "Help",
  // The button that discloses the help pages' contents where they are folded, as on a phone.
  docsContents: "Help pages",
  docsIntro:
    "How to use rcman: your workspaces and their servers, your Claude account, git and your settings, and, for administrators, installing and running it.",
  // The name of the box a table scrolls in. It is kept apart from the names of the parts of a
  // page, which a person or a test finds a region by.
  tableTitle: "Table",

  changesTitle: "Changes",
  refreshStatus: "Refresh changes",
  notARepository:
    "There is no git repository in this workspace yet. Changes appear here once Claude or you make one.",
  // When the status was last answered, from how many seconds ago.
  checkedAgo: (seconds: number) =>
    seconds < 10
      ? "Checked just now"
      : seconds < 60
        ? `Checked ${seconds} s ago`
        : seconds < 3600
          ? `Checked ${Math.floor(seconds / 60)} min ago`
          : `Checked ${Math.floor(seconds / 3600)} h ago`,
  listHeading: (title: string, count: number) => `${title} (${count})`,
  mergeInProgress:
    "A merge is under way: resolve any conflict, stage the files, and commit to finish it.",
  stagedTitle: "Staged",
  notStagedTitle: "Not staged",
  noFiles: "No files.",
  selectAll: "Select all",
  stageSelected: "Stage selected",
  unstageSelected: "Unstage selected",
  // Each change a file is listed with, by the code the server gives it.
  changes: {
    modified: "Modified",
    type_changed: "Type changed",
    added: "Added",
    deleted: "Deleted",
    renamed: "Renamed",
    copied: "Copied",
    unmerged: "Conflict",
    untracked: "Untracked",
  } as Record<string, string>,
  // The letter each change is shown as, by the same codes; its word above is
  // what it says to assistive technology and in its title.
  changeLetters: {
    modified: "M",
    type_changed: "T",
    added: "A",
    deleted: "D",
    renamed: "R",
    copied: "C",
    unmerged: "!",
    untracked: "U",
  } as Record<string, string>,
  renamedFrom: (path: string) => `from ${path}`,
  submodule: "submodule",
  notSelectable: "Its name is not UTF-8, so rcman cannot stage it.",
  andMore: (count: number) => `and ${count} more`,
  onBranch: (name: string) => `On branch ${name}`,
  detachedAt: (commit: string) => `Not on a branch, at commit ${commit}`,
  noCommitsYet: (name: string) => `On branch ${name}, with no commits yet`,
  tracking: (upstream: string, ahead: number | null, behind: number | null) =>
    ahead === null || behind === null
      ? `Tracking ${upstream}, which is gone`
      : `Tracking ${upstream}: ${ahead} ahead, ${behind} behind`,
  commitMessage: "Commit message",
  commitMessageHelp: "A commit needs a message that says what changed.",
  commit: "Commit",
  // One sentence, its middle the link to Settings.
  commitNeedsAuthor: {
    before: "Commits need an author name: ",
    link: "set yours in Settings",
    after: ".",
  },
  commitNothingStaged: "Nothing is staged. Stage the files to commit first.",
  commitPush: "Commit & Push",
  commitMenu: "More ways to commit",
  push: "Push",
  pushed: (branch: string, upstream: string) => `Pushed ${branch} to ${upstream}.`,
  pushNeedsToken: {
    before: "Pushing to GitHub needs a GitHub token, and none is stored: ",
    link: "add one in Settings",
    after: ".",
  },
  pushNeedsBranch: "You are not on a branch, so there is nothing to push.",
  pushNeedsRemote:
    "This branch tracks no remote branch, and the repository has no remote named origin to push it to.",
  pushNeedsCommit: "This branch has no commits yet, so there is nothing to push.",
  committedNotPushed: (commit: string) => `Committed ${commit}, but the push failed.`,
  branchesTitle: "Branches",
  localBranches: "Local branches",
  remoteBranches: "Remote branches",
  findBranch: "Find a branch",
  noBranches: "None.",
  noBranchMatches: "No branch matches.",
  currentBranch: "(current)",
  tracks: (upstream: string) => `→ ${upstream}`,
  trackedBy: (local: string) => `tracked by ${local}`,
  nameTakenBy: (local: string) => `its name is taken by ${local}`,
  newBranch: "New branch",
  switchTo: (name: string) => `Switch to ${name}`,
  newBranchName: "New branch from here",
  refreshBranches: "Refresh branches",
  sync: "Sync",
  // What a sync did, from its answer: what it pulled, what it pushed, and a push that failed.
  synced: (s: {
    branch: string;
    upstream: string;
    pulled: boolean;
    pushed: boolean;
    upstream_set: boolean;
    push_problem: Record<string, unknown> | null;
  }) => {
    if (s.push_problem !== null)
      return `Pulled ${s.upstream} into ${s.branch}, but the push failed.`;
    if (s.upstream_set) return `Published ${s.branch} to ${s.upstream}, which it now tracks.`;
    if (s.pulled && s.pushed)
      return `Pulled ${s.upstream} into ${s.branch}, and pushed ${s.branch}.`;
    if (s.pulled) return `Pulled ${s.upstream} into ${s.branch}.`;
    if (s.pushed) return `Pushed ${s.branch}.`;
    return `${s.branch} was already up to date with ${s.upstream}.`;
  },
  diverged:
    "This branch and the branch it tracks each have commits the other lacks, so it cannot be synced: merge or rebase it first.",
  gitSaid: "What git said",
  andMoreLines: (count: number) => `and ${count} more lines`,

  // The Monitor page, for administrators (src/Monitor.tsx); docs/monitor.md names its parts in
  // these words, which src/monitor-guide.test.ts holds it to.
  monitorTitle: "Monitor",
  monitorIntro:
    "What the host, each person and each workspace container use now, as rcman last collected it.",
  // The two rounds, by name, in the page's live region and in the guide.
  monitorUsage: "Usage",
  monitorDisk: "Disk",
  // A round in the live region: when it last collected, in UTC, and how often it runs where that
  // is known.
  monitorCollected: (round: string, at: string | null, every: string | null) =>
    `${round}: ${at === null ? "not collected yet" : `collected ${at} UTC`}${every === null ? "" : `, every ${every}`}.`,
  monitorUsageFailed: "The last usage round failed. The figures shown are the last ones read.",
  monitorDiskFailed: "The last disk round failed. The disk figures shown are the last ones read.",
  monitorHost: "Host",
  monitorPeople: "People",
  monitorContainers: "Containers",
  monitorCpu: "CPU",
  monitorMemory: "Memory",
  monitorMemoryWithSwap: "Memory with swap",
  monitorProcesses: "Processes",
  monitorPerson: "Person",
  monitorState: "State",
  monitorFilesystems: "Filesystems",
  monitorNoContainers: "No workspaces.",
  // Beside the host's CPU, a percent of one core.
  monitorCores: (cores: number) =>
    `of one core; ${cores === 1 ? "1 core" : `${cores} cores`} in all`,
  monitorMemoryOf: (total: string) => `used of ${total}`,
  monitorDiskOf: (size: string, free: string) => `used of ${size}, ${free} free`,
  // Under a filesystem's figures: what of rcman's it holds (monitorRoles), as a list.
  monitorFilesystemOf: (folders: string) => `Holds ${folders}`,
  monitorRoles: {
    database: "rcman's database",
    root: "the host's root folder",
    podman: "Podman's storage",
  } as Record<string, string>,
  // A figure rcman could not read, never shown as zero.
  monitorNotKnown: "Not known",
  // A part whose scope is switched off: an empty state that leads to the settings.
  monitorOff: "Not collected",
  monitorOffBody: (scope: string) => `“${scope}” is switched off in the monitoring settings.`,
  monitorOffLink: "Go to monitoring settings",
  monitorSettingsTitle: "Monitoring settings",
  monitorSettingsLine:
    "How often rcman collects, and what. A save applies at once, for every administrator.",
  monitorUsageInterval: "Usage interval (seconds)",
  monitorUsageIntervalHelp: "How often CPU, memory and processes are read. A read costs little.",
  monitorDiskInterval: "Disk interval (seconds)",
  monitorDiskIntervalHelp:
    "How often the disk of every workspace is read. A read walks every file, so keep it rare.",
  monitorCollect: "Collect",
  monitorSave: "Save monitoring settings",
  monitorSaving: "Saving…",
  monitorSaved: "Monitoring settings saved.",
  // Units, after a non-breaking space (src/format.ts): sizes in decimal SI, as Podman gives
  // them, and intervals.
  byteUnits: ["B", "kB", "MB", "GB", "TB", "PB", "EB"],
  timeUnits: { second: "s", minute: "min", hour: "h" },

  problemFieldErrors: "Details:",
  problemCheckFields: "Check the fields marked below.",
  problemServer: "Server:",
  problemLeft: "Not deleted:",
};
