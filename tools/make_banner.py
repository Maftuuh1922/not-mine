"""Compose assets/banner.png and assets/social-preview.png (1280x640).

Dev tool only (needs Pillow; the CLI itself is stdlib-only). Background art
assets/banner-art.jpg was AI-generated without text; the text is drawn here.
Run from the repo root: python3 tools/make_banner.py
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
bg = Image.open('assets/banner-art.jpg').convert('RGB')
W, H = 1280, 640
s = H / bg.height
bg = bg.resize((round(bg.width * s), H), Image.LANCZOS)
x0 = bg.width - W
bg = bg.crop((x0, 0, x0 + W, H))
# darken left side a bit for legibility
grad = Image.new('L', (W, H))
gd = ImageDraw.Draw(grad)
for x in range(W):
    gd.line([(x, 0), (x, H)], fill=int(150 * max(0, 1 - x / 760)))
bg = Image.composite(Image.new('RGB', (W, H), (5, 7, 14)), bg, grad)
d = ImageDraw.Draw(bg)
B = '/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf'
R = '/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf'
M = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
fT, fS, fM, fK = ImageFont.truetype(B, 132), ImageFont.truetype(R, 33), ImageFont.truetype(M, 22), ImageFont.truetype(M, 18)
X = 70
# kicker
d.text((X, 100), "AGENT SKILL  ·  ANY MODEL  ·  STDLIB PYTHON", font=fK, fill=(255, 92, 92))
# title with glow
glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
ImageDraw.Draw(glow).text((X, 150), "Not Mine", font=fT, fill=(255, 40, 60, 170))
glow = glow.filter(ImageFilter.GaussianBlur(14))
bg.paste(glow, (0, 0), glow)
d = ImageDraw.Draw(bg)
d.text((X, 150), "Not ", font=fT, fill=(245, 246, 250))
nw = d.textlength("Not ", font=fT)
d.text((X + nw, 150), "Mine", font=fT, fill=(255, 64, 76))
y = 320
for line in ["Your agent is about to kill a process", "that isn't its. Not Mine stops it."]:
    d.text((X, y), line, font=fS, fill=(214, 218, 230))
    y += 44
# terminal pill
y += 34
cmd = "$ notmine check port 8000"
res = "  NOT MINE  exit 10"
pw = d.textlength(cmd + res, font=fM) + 36
d.rounded_rectangle([X, y, X + pw, y + 46], radius=10, fill=(14, 17, 28), outline=(70, 76, 96), width=1)
d.text((X + 18, y + 11), cmd, font=fM, fill=(160, 220, 170))
d.text((X + 18 + d.textlength(cmd, font=fM), y + 11), res, font=fM, fill=(255, 92, 92))
bg.save('assets/banner.png', optimize=True)
bg.save('assets/social-preview.png', optimize=True)
print(bg.size)
