import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

// What the build copies from public/ as it is: the installed app's manifest and its icons.
const PUBLIC = resolve(__dirname, "..", "public");
const INDEX = readFileSync(resolve(__dirname, "..", "index.html"), "utf-8");

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

// A PNG's width and height, from its IHDR chunk, which comes first; null for a file that is no PNG.
function pngSize(path: string): [number, number] | null {
  const bytes = readFileSync(path);
  if (!PNG_SIGNATURE.every((b, i) => bytes[i] === b)) return null;
  if (bytes.toString("latin1", 12, 16) !== "IHDR") return null;
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

const inPublic = (href: string) => resolve(PUBLIC, `.${href}`);

type Icon = { src: string; sizes: string; type: string; purpose?: string };

test("every icon the manifest names is a PNG of the size it states, one of them maskable", () => {
  const manifest = JSON.parse(readFileSync(resolve(PUBLIC, "manifest.webmanifest"), "utf-8"));
  const icons: Icon[] = manifest.icons;
  expect(icons.length).toBeGreaterThan(0);
  for (const icon of icons) {
    expect(icon.type, icon.src).toBe("image/png");
    const [width, height] = icon.sizes.split("x").map(Number);
    expect(pngSize(inPublic(icon.src)), icon.src).toEqual([width, height]);
  }
  expect(icons.some((icon) => icon.purpose === "maskable" && icon.sizes === "512x512")).toBe(true);
});

test("index.html links the manifest and an apple-touch-icon of 180 pixels", () => {
  const manifest = INDEX.match(/<link rel="manifest"[^>]*>/)?.[0];
  expect(manifest).toBe('<link rel="manifest" href="/manifest.webmanifest" />');
  expect(existsSync(inPublic("/manifest.webmanifest"))).toBe(true);
  const touch = INDEX.match(/<link rel="apple-touch-icon"[^>]*href="([^"]+)"/)?.[1];
  expect(touch).toBeDefined();
  expect(pngSize(inPublic(touch as string))).toEqual([180, 180]);
});
