// Generate media/icon.png (128x128): a prism splitting a beam into a spectrum.
// Pure Node (zlib + hand-rolled PNG chunks), no image dependencies.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const here = dirname(fileURLToPath(import.meta.url));
const outFile = join(here, "..", "media", "icon.png");
const SIZE = 128;
const px = Buffer.alloc(SIZE * SIZE * 4);

function blend(x, y, r, g, b, a = 255) {
  if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) return;
  const i = (y * SIZE + x) * 4;
  const ia = a / 255;
  px[i] = Math.round(r * ia + px[i] * (1 - ia));
  px[i + 1] = Math.round(g * ia + px[i + 1] * (1 - ia));
  px[i + 2] = Math.round(b * ia + px[i + 2] * (1 - ia));
  px[i + 3] = Math.max(px[i + 3], a);
}

// Background: dark vertical gradient.
for (let y = 0; y < SIZE; y++) {
  const t = y / SIZE;
  const r = Math.round(22 + 8 * t);
  const g = Math.round(22 + 8 * t);
  const b = Math.round(30 + 14 * t);
  for (let x = 0; x < SIZE; x++) blend(x, y, r, g, b, 255);
}

const A = [40, 24];
const B = [40, 104];
const C = [102, 64];

function sign(p, q, r) {
  return (p[0] - r[0]) * (q[1] - r[1]) - (q[0] - r[0]) * (p[1] - r[1]);
}
function inTriangle(x, y) {
  const p = [x + 0.5, y + 0.5];
  const d1 = sign(p, A, B);
  const d2 = sign(p, B, C);
  const d3 = sign(p, C, A);
  const hasNeg = d1 < 0 || d2 < 0 || d3 < 0;
  const hasPos = d1 > 0 || d2 > 0 || d3 > 0;
  return !(hasNeg && hasPos);
}
function distToSegment(x, y, p, q) {
  const vx = q[0] - p[0];
  const vy = q[1] - p[1];
  const wx = x - p[0];
  const wy = y - p[1];
  const len2 = vx * vx + vy * vy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, (wx * vx + wy * vy) / len2));
  const dx = x - (p[0] + t * vx);
  const dy = y - (p[1] + t * vy);
  return Math.sqrt(dx * dx + dy * dy);
}
function beam(p, q, color, width, alpha = 255) {
  const minX = Math.max(0, Math.floor(Math.min(p[0], q[0]) - width - 2));
  const maxX = Math.min(SIZE - 1, Math.ceil(Math.max(p[0], q[0]) + width + 2));
  const minY = Math.max(0, Math.floor(Math.min(p[1], q[1]) - width - 2));
  const maxY = Math.min(SIZE - 1, Math.ceil(Math.max(p[1], q[1]) + width + 2));
  for (let y = minY; y <= maxY; y++) {
    for (let x = minX; x <= maxX; x++) {
      const d = distToSegment(x + 0.5, y + 0.5, p, q);
      if (d <= width) blend(x, y, color[0], color[1], color[2], alpha);
      else if (d <= width + 1.5) blend(x, y, color[0], color[1], color[2], Math.round(alpha * 0.35));
    }
  }
}

// Glass body of the prism.
for (let y = 0; y < SIZE; y++) {
  for (let x = 0; x < SIZE; x++) {
    if (inTriangle(x, y)) {
      const t = (x - A[0]) / (C[0] - A[0]);
      blend(x, y, Math.round(120 + 60 * t), Math.round(140 + 50 * t), Math.round(190 + 40 * t), 90);
    }
  }
}

// Incoming white beam.
beam([4, 64], [44, 64], [245, 245, 250], 2.2);

// Refracted spectrum fan.
const spectrum = [
  [255, 85, 85],
  [255, 184, 108],
  [241, 250, 140],
  [80, 250, 123],
  [139, 233, 253],
  [189, 147, 249],
];
spectrum.forEach((color, i) => {
  const angle = ((i - (spectrum.length - 1) / 2) * 7 * Math.PI) / 180;
  const end = [124, 64 + Math.tan(angle) * 54];
  beam([72, 64], end, color, 1.8);
});

// Prism edges.
beam(A, B, [230, 235, 255], 1.6);
beam(B, C, [230, 235, 255], 1.6);
beam(C, A, [230, 235, 255], 1.6);

// ---- PNG encoding ----
const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}
const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(SIZE, 0);
ihdr.writeUInt32BE(SIZE, 4);
ihdr[8] = 8; // bit depth
ihdr[9] = 6; // RGBA
const raw = Buffer.alloc(SIZE * (SIZE * 4 + 1));
for (let y = 0; y < SIZE; y++) {
  raw[y * (SIZE * 4 + 1)] = 0; // filter: none
  px.copy(raw, y * (SIZE * 4 + 1) + 1, y * SIZE * 4, (y + 1) * SIZE * 4);
}
const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk("IHDR", ihdr),
  chunk("IDAT", deflateSync(raw, { level: 9 })),
  chunk("IEND", Buffer.alloc(0)),
]);
mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, png);
console.log(`Wrote ${outFile} (${png.length} bytes)`);
