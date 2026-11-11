"""Render demo receipt images with Pillow into demo/receipts/.  Run: python demo/make_receipts.py"""
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "receipts"

RECEIPTS = {
    "office_snacks.png": ("COFFEE & CO", "11/10/2026 10:42 AM", [("Office coffee box (12 cups)", 34.00), ("Assorted office snacks", 30.00)], 0.00),
    "client_dinner.png": ("STEAKHOUSE 21", "11/09/2026 07:55 PM", [("Dinner for two (prix fixe)", 148.00), ("Wine pairing", 38.00)], 0.00),
    "office_supplies.png": ("OFFICE DEPOTISH", "11/08/2026 02:15 PM", [("Printer paper (case)", 42.00), ("Wireless headset", 89.00)], 10.81),
}


@lru_cache
def _font(size: int):
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(name: str, vendor: str, when: str, items: list[tuple[str, float]], tax: float, out_dir: Path = OUT) -> Path:
    font, big = _font(22), _font(30)
    img = Image.new("RGB", (640, 300 + 34 * len(items)), "white")
    d = ImageDraw.Draw(img)
    y = 30
    d.text((40, y), vendor, font=big, fill="black"); y += 50
    d.text((40, y), when, font=font, fill="black"); y += 50
    for label, price in items:
        d.text((40, y), label, font=font, fill="black")
        d.text((500, y), f"${price:.2f}", font=font, fill="black"); y += 34
    subtotal = sum(p for _, p in items)
    y += 16
    for label, value in (("SUBTOTAL", subtotal), ("SALES TAX", tax), ("TOTAL", subtotal + tax)):
        d.text((40, y), label, font=font, fill="black")
        d.text((500, y), f"${value:.2f}", font=font, fill="black"); y += 34
    out_dir.mkdir(exist_ok=True)
    path = out_dir / name
    img.save(path)
    return path


if __name__ == "__main__":
    for fname, args in RECEIPTS.items():
        print(render(fname, *args))
