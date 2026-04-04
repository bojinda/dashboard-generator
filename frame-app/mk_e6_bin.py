#!/usr/bin/env python3
import argparse
import math
from PIL import Image

# A reasonable Spectra 6 palette (RGB) – exact panel primaries vary, but this is close enough.
PALETTE = [
    (0, 0, 0),         # 0 black
    (255, 255, 255),   # 1 white
    (0, 128, 0),       # 2 green
    (0, 0, 255),       # 3 blue
    (255, 0, 0),       # 4 red
    (255, 255, 0),     # 5 yellow
]

def nearest_palette_index(r, g, b):
    best_i = 0
    best_d = 10**18
    for i, (pr, pg, pb) in enumerate(PALETTE):
        dr = r - pr
        dg = g - pg
        db = b - pb
        d = dr*dr + dg*dg + db*db
        if d < best_d:
            best_d = d
            best_i = i
    return best_i

def dither_floyd_steinberg(img):
    # img is RGB PIL Image
    w, h = img.size
    px = img.load()

    # Work in float error space
    err = [[[0.0, 0.0, 0.0] for _ in range(w)] for _ in range(h)]

    idx_map = [[0]*w for _ in range(h)]

    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            r = min(255, max(0, int(r + err[y][x][0])))
            g = min(255, max(0, int(g + err[y][x][1])))
            b = min(255, max(0, int(b + err[y][x][2])))

            i = nearest_palette_index(r, g, b)
            idx_map[y][x] = i

            pr, pg, pb = PALETTE[i]
            er = r - pr
            eg = g - pg
            eb = b - pb

            # Distribute error
            if x + 1 < w:
                err[y][x+1][0] += er * 7/16
                err[y][x+1][1] += eg * 7/16
                err[y][x+1][2] += eb * 7/16
            if y + 1 < h:
                if x > 0:
                    err[y+1][x-1][0] += er * 3/16
                    err[y+1][x-1][1] += eg * 3/16
                    err[y+1][x-1][2] += eb * 3/16
                err[y+1][x][0] += er * 5/16
                err[y+1][x][1] += eg * 5/16
                err[y+1][x][2] += eb * 5/16
                if x + 1 < w:
                    err[y+1][x+1][0] += er * 1/16
                    err[y+1][x+1][1] += eg * 1/16
                    err[y+1][x+1][2] += eb * 1/16

    return idx_map

def no_dither_map(img):
    w, h = img.size
    px = img.load()
    idx_map = [[0]*w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            idx_map[y][x] = nearest_palette_index(r, g, b)
    return idx_map

def pack_nibbles(idx_map):
    h = len(idx_map)
    w = len(idx_map[0])
    out = bytearray()

    # 2 pixels per byte: high nibble then low nibble
    for y in range(h):
        x = 0
        while x < w:
            a = idx_map[y][x] & 0x0F
            b = idx_map[y][x+1] & 0x0F if x+1 < w else 0
            out.append((a << 4) | b)
            x += 2
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="input image (png/jpg)")
    ap.add_argument("output", help="output image_data.bin")
    ap.add_argument("--w", type=int, default=1600)
    ap.add_argument("--h", type=int, default=1200)
    ap.add_argument("--rotate", type=int, default=0, help="rotate degrees clockwise (0/90/180/270)")
    ap.add_argument("--dither", action="store_true", help="enable Floyd-Steinberg dithering")
    args = ap.parse_args()

    img = Image.open(args.input).convert("RGB")

    if args.rotate:
        # PIL rotates CCW, so negative for clockwise
        img = img.rotate(-args.rotate, expand=True)

    # Fit/crop to exact size (center crop)
    img_ratio = img.width / img.height
    target_ratio = args.w / args.h

    if img_ratio > target_ratio:
        # too wide
        new_h = args.h
        new_w = int(new_h * img_ratio)
    else:
        # too tall
        new_w = args.w
        new_h = int(new_w / img_ratio)

    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - args.w) // 2
    top = (new_h - args.h) // 2
    img = img.crop((left, top, left + args.w, top + args.h))

    if args.dither:
        idx_map = dither_floyd_steinberg(img)
    else:
        idx_map = no_dither_map(img)

    data = pack_nibbles(idx_map)

    with open(args.output, "wb") as f:
        f.write(data)

    print(f"Wrote {len(data)} bytes to {args.output} (expected ~{args.w*args.h//2} bytes)")

if __name__ == "__main__":
    main()
