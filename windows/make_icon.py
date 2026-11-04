"""Draws the StellAI icon (a four-point star with an orbit) and saves stellai.ico. Run at build time."""
import math
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=(7, 11, 26, 255))

glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse((150, 150, 362, 362), fill=(92, 225, 255, 110))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(40)))

# Orbit: a tilted ellipse
orbit = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(orbit).ellipse((70, 190, 442, 322), outline=(92, 225, 255, 200), width=14)
img.alpha_composite(orbit.rotate(-25, resample=Image.BICUBIC))

# Four-point star
cx = cy = S / 2
pts = []
for i in range(8):
    a = math.pi / 4 * i - math.pi / 2
    r = 150 if i % 2 == 0 else 38
    pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
d.polygon(pts, fill=(235, 250, 255, 255))

out = sys.argv[1] if len(sys.argv) > 1 else "stellai.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out.rsplit(".", 1)[0] + ".png")
print("wrote", out)
