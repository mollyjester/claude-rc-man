// What the browser tests run a command of the whole stack with (RCMAN_STACK_EXEC): the command,
// as JSON, sent over the unix socket the suite listens on for the browser's container
// (tests/container/test_stack_in_podman.py), which runs it in the stack's container if it is
// one the stack takes. What it printed is printed here, and its exit code is this one's.
// Usage: node stack_exec.mjs SOCKET ARGV...
import { connect } from "node:net";

const [path, ...argv] = process.argv.slice(2);
const socket = connect(path, () => socket.end(JSON.stringify(argv)));
const chunks = [];
socket.on("data", (chunk) => chunks.push(chunk));
socket.on("end", () => {
  const answer = JSON.parse(Buffer.concat(chunks).toString());
  process.stdout.write(answer.output);
  process.exit(answer.code);
});
socket.on("error", (error) => {
  process.stderr.write(`${error}\n`);
  process.exit(1);
});
