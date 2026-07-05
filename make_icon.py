# -*- coding: utf-8 -*-
"""Generate menq.ico — an azure Armenian 'Մ', slightly slanted right (italic)."""
from PIL import Image, ImageDraw, ImageFont

S = 256
AZURE = (14, 165, 233, 255)   # #0EA5E9
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
font = ImageFont.truetype("C:/Windows/Fonts/sylfaen.ttf", 184)
text = "Մ"

SW = 14                                   # white rim thickness
bbox = d.textbbox((0, 0), text, font=font, stroke_width=SW)
w = bbox[2] - bbox[0]; h = bbox[3] - bbox[1]
# center, then nudge left so the right-leaning shear stays inside the canvas
x = (S - w) / 2 - bbox[0] - 12
y = (S - h) / 2 - bbox[1]
# bold but with an OPEN counter: thin white rim, light azure stroke so the
# two legs of the letter don't merge in the middle
d.text((x, y), text, font=font, fill=(255, 255, 255, 255), stroke_width=SW, stroke_fill=(255, 255, 255, 255))
d.text((x, y), text, font=font, fill=AZURE, stroke_width=6, stroke_fill=AZURE)

# italic: mild shear so the TOP leans to the right (kept small so nothing clips)
shear = 0.15
img = img.transform((S, S), Image.AFFINE, (1, shear, -shear * S * 0.5, 0, 1, 0),
                    resample=Image.BICUBIC)

img.save("C:/Users/Admin/Desktop/MenQ/menq.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("menq.ico written")
