// What a refused field looks like: marked invalid, and described by the
// server's message, which sits under it.
export function expectRefused(input: HTMLElement, message: string) {
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAccessibleDescription(expect.stringContaining(message));
}
