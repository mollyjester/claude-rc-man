// A stand-in for the browser's WebSocket, which src/test/setup.ts installs for every test:
// each socket the page opens is recorded, with what it sent, and a test plays rcman's end of
// it — opening it, sending it frames and closing it — as the backend's route
// (backend/rcman/routers/terminal.py) would.
export class FakeSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  // Every socket opened since the test began, in order.
  static opened: FakeSocket[] = [];

  readyState = FakeSocket.CONNECTING;
  binaryType: BinaryType = "blob";
  // What the page sent: text frames as text, binary ones as their bytes.
  sent: (string | Uint8Array)[] = [];
  // The code the page closed it with, once it has.
  closedByPage: number | undefined;

  constructor(public url: string) {
    super();
    FakeSocket.opened.push(this);
  }

  send(data: string | ArrayBufferLike | ArrayBufferView | Blob) {
    if (this.readyState !== FakeSocket.OPEN) throw new Error("planted: sent on a socket not open");
    if (typeof data === "string") this.sent.push(data);
    else if (ArrayBuffer.isView(data))
      this.sent.push(
        new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)),
      );
    else if (data instanceof ArrayBuffer) this.sent.push(new Uint8Array(data.slice(0)));
    else throw new Error("planted: a Blob was sent");
  }

  // The page closes it: as a browser's does, it is closing at once, and closed, with its
  // close event, a moment later: 1006 for one that never opened, otherwise the code given,
  // or 1005 for none.
  close(code?: number) {
    if (this.readyState === FakeSocket.CLOSED || this.readyState === FakeSocket.CLOSING) return;
    const wasOpen = this.readyState === FakeSocket.OPEN;
    this.closedByPage = code ?? 1005;
    this.readyState = FakeSocket.CLOSING;
    setTimeout(() => {
      this.readyState = FakeSocket.CLOSED;
      const closing = wasOpen ? (code ?? 1005) : 1006;
      this.dispatchEvent(new CloseEvent("close", { code: closing, wasClean: wasOpen }));
    }, 0);
  }

  // The text frames the page sent, read as JSON.
  frames(): unknown[] {
    return this.sent.filter((d) => typeof d === "string").map((d) => JSON.parse(d as string));
  }

  // The binary frames the page sent, as text.
  typed(): string {
    const decoder = new TextDecoder();
    return this.sent
      .filter((d) => typeof d !== "string")
      .map((d) => decoder.decode(d as Uint8Array))
      .join("");
  }

  // rcman's end: the upgrade accepted.
  accept() {
    this.readyState = FakeSocket.OPEN;
    this.dispatchEvent(new Event("open"));
  }

  // rcman's end: a text frame, given as the object it holds.
  sendText(frame: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(frame) }));
  }

  // rcman's end: a binary frame, as the arraybuffer the page asks for.
  sendBytes(text: string) {
    const bytes = new TextEncoder().encode(text);
    const data = this.binaryType === "arraybuffer" ? bytes.buffer : new Blob([bytes]);
    this.dispatchEvent(new MessageEvent("message", { data }));
  }

  // rcman's end, or the way to it: the socket closed with a code. A browser closes a socket
  // whose upgrade was refused with 1006, before any open.
  serverClose(code: number) {
    this.readyState = FakeSocket.CLOSED;
    this.dispatchEvent(new CloseEvent("close", { code, wasClean: code !== 1006 }));
  }
}

export const sockets = () => FakeSocket.opened;
