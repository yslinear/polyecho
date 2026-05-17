/*
 * Build-free PNG icon generator (Node stdlib only: zlib + Buffer).
 *
 * Emits a solid on-brand background with a centred lettermark "P" drawn as
 * filled rectangles (a tiny built-in 5x7-ish glyph scaled up) — no canvas,
 * no fonts, no deps. Produces VALID 8-bit RGBA PNGs the manifest references:
 *   public/icons/icon-192.png  (192x192)
 *   public/icons/icon-512.png  (512x512, also serves as the maskable icon)
 *
 * Run once at authoring time; committed PNGs are the source of truth so the
 * Docker build never needs to run this. Re-run only to retune the brand color.
 *
 * Colors are the DESIGN.md tokens converted to sRGB:
 *   bg  = --primary  hsl(205 80% 41%)  -> #1481bd
 *   fg  = --primary-foreground near-white tinted -> #f6fafd
 */

import { deflateSync } from "node:zlib";
import { writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(__dirname, "..", "public", "icons");

const BG = [0x14, 0x81, 0xbd, 0xff]; // primary
const FG = [0xf6, 0xfa, 0xfd, 0xff]; // on-primary

// 7 rows x 5 cols bitmap of "P" (1 = foreground).
const GLYPH = [
  [1, 1, 1, 1, 0],
  [1, 0, 0, 0, 1],
  [1, 0, 0, 0, 1],
  [1, 1, 1, 1, 0],
  [1, 0, 0, 0, 0],
  [1, 0, 0, 0, 0],
  [1, 0, 0, 0, 0],
];

function crc32(buf) {
  let c = ~0;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return (~c) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crc]);
}

function makePng(size) {
  const cols = 5;
  const rows = 7;
  // Glyph occupies ~56% of the canvas, centred.
  const cell = Math.floor((size * 0.56) / Math.max(cols, rows));
  const gW = cell * cols;
  const gH = cell * rows;
  const offX = Math.floor((size - gW) / 2);
  const offY = Math.floor((size - gH) / 2);

  // Raw image: each row prefixed with filter byte 0.
  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    const rowStart = y * (stride + 1);
    raw[rowStart] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      let px = BG;
      const gx = Math.floor((x - offX) / cell);
      const gy = Math.floor((y - offY) / cell);
      if (
        gx >= 0 &&
        gx < cols &&
        gy >= 0 &&
        gy < rows &&
        GLYPH[gy][gx] === 1
      ) {
        px = FG;
      }
      const p = rowStart + 1 + x * 4;
      raw[p] = px[0];
      raw[p + 1] = px[1];
      raw[p + 2] = px[2];
      raw[p + 3] = px[3];
    }
  }

  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;
  const idat = deflateSync(raw, { level: 9 });
  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

mkdirSync(OUT_DIR, { recursive: true });
writeFileSync(join(OUT_DIR, "icon-192.png"), makePng(192));
writeFileSync(join(OUT_DIR, "icon-512.png"), makePng(512));
console.log("wrote icon-192.png + icon-512.png to", OUT_DIR);
