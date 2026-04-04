#!/usr/bin/env python3
import argparse
import os
import requests
from PIL import Image

W, H = 1600, 1200

# You may need to reorder this list after the 1-time calibration.
# Index in this list becomes the 4-bit pixel value.
PALETTE = [
    ("white",  (255, 255, 255)),
    ("black",  (0, 0, 0)),
    ("red",    (255, 0, 0)),
    ("yellow", (255, 255, 0)),
    ("blue",   (0, 0, 255)),
    ("green",  (0, 255, 0)),
]

def build_palette_image():
    pal_img = Image.new("P", (1, 1))
    pal = []
    for _, rgb in PALETTE:
        pal.extend(rgb)
    pal.extend([0,0,0] * (256 - len(PALETTE)))
    pal_img.putpalette(pal)
    return pal_img

def fit(img: Image.Image, rotate: int) -> Image.Image:
    img = img.convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)  # clockwise

    # cover-fit then center-crop
    scale = max(W / img.width, H / img.height)
    nw, nh = int(img.width * scale), int(img.height * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - W) // 2
    top = (nh - H) // 2
    return img.crop((left, top, left + W, top + H))

def to_indices(img: Image.Image, dither: bool) -> Image.Image:
    pal_img = build_palette_image()
    return img.quantize(
        palette=pal_img,
        dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE,
    )

def pack_4bpp(p_img: Image.Image) -> bytes:
    pix = list(p_img.getdata())
    out = bytearray((W * H) // 2)
    j = 0
    for i in range(0, len(pix), 2):
        a = pix[i] & 0x0F
        b = pix[i+1] & 0x0F
        out[j] = (a << 4) | b
        j += 1
    return bytes(out)

def upload(frame_host: str, bin_path: str, timeout: int):
    url = f"http://{frame_host}/upload"
    with open(bin_path, "rb") as f:
        files = {"data": ("image_data.bin", f, "application/octet-stream")}
        r = requests.post(url, files=files, timeout=timeout)
    r.raise_for_status()
    return r.text.strip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default="192.168.0.224")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default="/tmp/image_data.bin")
    ap.add_argument("--rotate", type=int, default=90, choices=[0,90,180,270])
    ap.add_argument("--no-dither", action="store_true")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    img = Image.open(args.inp)
    img = fit(img, rotate=args.rotate)
    p = to_indices(img, dither=not args.no_dither)
    data = pack_4bpp(p)

    with open(args.out, "wb") as f:
        f.write(data)

    print(f"Wrote {args.out} ({len(data)} bytes; expected 960000)")

    if args.upload:
        resp = upload(args.frame, args.out, args.timeout)
        print("Upload response:", resp)

if __name__ == "__main__":
    main()
