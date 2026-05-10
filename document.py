"""Document model: layers, composite, snapshots for undo/redo."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PIL import Image


@dataclass
class Layer:
    image: Image.Image
    name: str = "Layer"
    visible: bool = True
    opacity: int = 255  # 0–255

    def __post_init__(self) -> None:
        self.image = self.image.convert("RGBA")


@dataclass
class Document:
    layers: List[Layer] = field(default_factory=list)
    active_index: int = 0
    width: int = 0
    height: int = 0

    @classmethod
    def new_blank(cls, width: int, height: int, bg: Tuple[int, int, int, int] = (255, 255, 255, 255)) -> Document:
        base = Image.new("RGBA", (width, height), bg)
        return cls(layers=[Layer(base, "Background")], active_index=0, width=width, height=height)

    @classmethod
    def from_image(cls, img: Image.Image, name: str = "Background") -> Document:
        img = img.convert("RGBA")
        w, h = img.size
        return cls(layers=[Layer(img.copy(), name)], active_index=0, width=w, height=h)

    @property
    def active_layer(self) -> Optional[Layer]:
        if not self.layers or self.active_index < 0 or self.active_index >= len(self.layers):
            return None
        return self.layers[self.active_index]

    def composite(self) -> Image.Image:
        if not self.layers:
            return Image.new("RGBA", (max(1, self.width), max(1, self.height)), (0, 0, 0, 0))
        w = max((lyr.image.width for lyr in self.layers), default=self.width)
        h = max((lyr.image.height for lyr in self.layers), default=self.height)
        self.width, self.height = w, h
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for lyr in self.layers:
            if not lyr.visible:
                continue
            im = lyr.image
            if im.size != (w, h):
                im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                im.paste(lyr.image, (0, 0))
            if lyr.opacity < 255:
                alpha = im.split()[3]
                alpha = alpha.point(lambda p: int(p * lyr.opacity / 255))
                im = im.convert("RGBA")
                im.putalpha(alpha)
            out = Image.alpha_composite(out, im)
        return out

    def snapshot(self) -> dict:
        return {
            "layers": [
                {
                    "name": lyr.name,
                    "image": lyr.image.copy(),
                    "visible": lyr.visible,
                    "opacity": lyr.opacity,
                }
                for lyr in self.layers
            ],
            "active_index": self.active_index,
            "width": self.width,
            "height": self.height,
        }

    def restore(self, snap: dict) -> None:
        self.layers = []
        for item in snap["layers"]:
            lyr = Layer(item["image"].copy(), item["name"])
            lyr.visible = item["visible"]
            lyr.opacity = item["opacity"]
            self.layers.append(lyr)
        self.active_index = snap["active_index"]
        self.width = snap["width"]
        self.height = snap["height"]


def document_from_snapshot(snap: dict) -> Document:
    doc = Document()
    doc.restore(snap)
    return doc
