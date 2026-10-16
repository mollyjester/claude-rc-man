import { m } from "./messages";

// A workspace's failure told in a sentence. The server writes what went wrong in plain words
// as the first lines of its answer, then "git clone failed, exit code …:" and what git said:
// the sentence is those first lines. A clone with no cause the server knows starts at git's
// line, and is told by the page; so is any failure told in several lines that is not a clone's.
// A failure told in one line is taken to be the server's own sentence. `raw` is whether
// there are words besides the sentence, to be kept apart.
export function failureSentence(
  error: string | null,
  cloned: boolean,
): { sentence: string; raw: boolean } {
  const generic = cloned ? m.workspaceFailedClone : m.workspaceFailedMake;
  const text = (error ?? "").trim();
  if (text === "") return { sentence: generic, raw: false };
  const lines = text.split("\n");
  const git = lines.findIndex((line) => /^git clone failed, exit code \d+:$/.test(line));
  if (git > 0) return { sentence: lines.slice(0, git).join(" "), raw: true };
  if (git === 0) return { sentence: m.workspaceFailedClone, raw: true };
  if (lines.length === 1) return { sentence: text, raw: false };
  return { sentence: generic, raw: true };
}
