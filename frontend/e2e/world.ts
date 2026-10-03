import { randomUUID } from "node:crypto";
import { createServer } from "node:net";
import { join } from "node:path";

// This run's world and port, chosen once and kept in the environment: why, and the
// world's life, are in project-knowledge/frontend.md.

// A port the system has free, given back before the backend binds it.
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() =>
        typeof address === "object" && address
          ? resolve(address.port)
          : reject(new Error(`no port in ${String(address)}`)),
      );
    });
  });
}

// Under /tmp, not tmpdir(): a tmux socket's path in it has to stay short.
process.env.RCMAN_E2E_DIR ??= join("/tmp", `rcman-e2e-${randomUUID()}`);
process.env.RCMAN_E2E_PORT ??= String(await freePort());

// The world: each person's home under homes/, and the origins of their repositories
// under origins/, which the world's GitHub serves as https://github.com/<owner>/<name>.git.
export const WORLD: string = process.env.RCMAN_E2E_DIR;
export const PORT: string = process.env.RCMAN_E2E_PORT;
export const ORIGIN = `http://127.0.0.1:${PORT}`;
