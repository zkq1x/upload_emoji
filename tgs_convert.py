from __future__ import annotations

import gzip
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from lottie import objects
from lottie.objects.shapes import BoundingBox
from lottie.parsers.svg.importer import parse_svg_etree
from lottie.utils.stripper import float_strip

CANVAS = 512
FPS = 60
MAX_SECONDS = 3
MAX_BYTES = 64 * 1024

FATAL_TAGS = {
    "image": "растровое <image>: конвертер его выбросит, а картинки в TGS запрещены",
    "text": "<text>: переведи текст в кривые (Object → Path), иначе он потеряется",
    "foreignObject": "<foreignObject> не переводится в Lottie",
}
WARN_TAGS = {
    "mask": "mask игнорируется конвертером (маски в TGS ещё и запрещены)",
    "filter": "фильтры (тени, размытие) игнорируются",
    "pattern": "pattern-заливки не поддерживаются",
    "animate": "SMIL-анимация поддерживается частично, проверь зацикленность вручную",
    "animateTransform": "SMIL-анимация поддерживается частично, проверь зацикленность вручную",
    "animateMotion": "SMIL-анимация поддерживается частично, проверь зацикленность вручную",
}

FORBIDDEN_LAYER_TYPES = {1: "solid-слой", 2: "image-слой", 5: "text-слой"}
FORBIDDEN_SHAPE_TYPES = {
    "mm": "merge paths",
    "sr": "star shape",
    "gs": "gradient stroke",
    "rp": "repeater",
}


@dataclass
class ConvertResult:
    data: bytes | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None and not self.errors


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _num(value: str | None, default: float = 0.0) -> float:
    try:
        return float(re.sub(r"px$", "", value.strip())) if value else default
    except ValueError:
        return default


def _viewport(root: ET.Element) -> tuple[float, float, float, float]:
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    if len(vb) == 4:
        return tuple(map(float, vb))
    return 0.0, 0.0, _num(root.get("width"), CANVAS), _num(root.get("height"), CANVAS)


def _trivial_clips(root: ET.Element) -> set[str]:
    x0, y0, w, h = _viewport(root)
    trivial = set()
    for el in root.iter():
        if _local(el.tag) != "clipPath" or not el.get("id"):
            continue
        kids = list(el)
        if len(kids) == 1 and _local(kids[0].tag) == "rect" and not kids[0].get("transform"):
            r = kids[0]
            if (_num(r.get("x")) <= x0 and _num(r.get("y")) <= y0
                    and _num(r.get("x")) + _num(r.get("width")) >= x0 + w
                    and _num(r.get("y")) + _num(r.get("height")) >= y0 + h):
                trivial.add(el.get("id"))
    return trivial


def _prescan(root: ET.Element, res: ConvertResult) -> None:
    found: set[str] = set()
    trivial = _trivial_clips(root)
    clipped = 0
    for el in root.iter():
        tag = _local(el.tag)
        if tag in FATAL_TAGS:
            found.add(FATAL_TAGS[tag])
        elif tag in WARN_TAGS:
            res.warnings.append(WARN_TAGS[tag])
        ref = re.fullmatch(r"\s*url\(\s*#([^)\s]+)\s*\)\s*", el.get("clip-path") or "")
        if el.get("clip-path") and not (ref and ref.group(1) in trivial):
            clipped += 1
        for attr in ("mask", "filter"):
            if el.get(attr):
                res.warnings.append(f"атрибут {attr} игнорируется конвертером")
    if clipped:
        res.warnings.append(f"clip-path игнорируется конвертером ({clipped} шт.): "
                            "если обрезка что-то скрывала, это станет видно")
    res.errors += sorted(found)
    res.warnings[:] = list(dict.fromkeys(res.warnings))


_CSS_RULE = re.compile(r"([^{}]+)\{([^}]*)\}")


def _inline_css(root: ET.Element, res: ConvertResult) -> None:
    rules: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) != "style" or not el.text:
            continue
        css = re.sub(r"/\*.*?\*/", "", el.text, flags=re.S)
        for selectors, body in _CSS_RULE.findall(css):
            for sel in selectors.split(","):
                sel = sel.strip()
                if re.fullmatch(r"\.[\w-]+", sel):
                    rules[sel[1:]] = rules.get(sel[1:], "") + body.strip().rstrip(";") + ";"
                elif sel:
                    res.warnings.append(f"CSS-селектор {sel!r} не поддерживается (только .class)")
    if not rules:
        return
    for el in root.iter():
        classes = (el.get("class") or "").split()
        inherited = "".join(rules.get(c, "") for c in classes)
        if inherited:
            el.set("style", inherited + (el.get("style") or ""))


def _content_box(anim: objects.Animation) -> BoundingBox:
    box = BoundingBox()
    for layer in anim.layers:
        if not isinstance(layer, objects.ShapeLayer):
            continue
        local = BoundingBox()
        stroke = 0.0
        for shape in layer.shapes:
            local.expand(shape.bounding_box(0))
            for sub in shape.find_all(objects.Stroke):
                stroke = max(stroke, float(sub.width.get_value(0) or 0))
        if local.isnull():
            continue
        half = stroke / 2
        m = layer.transform.to_matrix(0)
        for x, y in ((local.x1 - half, local.y1 - half), (local.x2 + half, local.y1 - half),
                     (local.x1 - half, local.y2 + half), (local.x2 + half, local.y2 + half)):
            p = m.apply(objects.NVector(x, y))
            box.include(p.x, p.y)
    return box


def _fit(anim: objects.Animation, fit: str, padding: float, res: ConvertResult) -> None:
    vw, vh = anim.width, anim.height
    content = _content_box(anim)
    if content.isnull():
        res.errors.append("в SVG нет ни одной фигуры")
        return
    if fit == "content":
        box = BoundingBox(max(content.x1, 0), max(content.y1, 0), min(content.x2, vw), min(content.y2, vh))
        if box.width <= 0 or box.height <= 0:
            res.errors.append("все фигуры лежат за пределами вьюпорта SVG")
            return
    else:
        box = BoundingBox(0, 0, vw, vh)

    avail = CANVAS - 2 * padding
    k = avail / max(box.width, box.height)
    anim.to_precomp()
    tr = anim.layers[0].transform
    tr.anchor_point.value = objects.NVector((box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2)
    tr.position.value = objects.NVector(CANVAS / 2, CANVAS / 2)
    tr.scale.value = objects.NVector(k * 100, k * 100)
    anim.width = anim.height = CANVAS

    cx1 = (content.x1 - (box.x1 + box.x2) / 2) * k + CANVAS / 2
    cy1 = (content.y1 - (box.y1 + box.y2) / 2) * k + CANVAS / 2
    cx2 = (content.x2 - (box.x1 + box.x2) / 2) * k + CANVAS / 2
    cy2 = (content.y2 - (box.y1 + box.y2) / 2) * k + CANVAS / 2
    tol = 1.0
    if cx1 < -tol or cy1 < -tol or cx2 > CANVAS + tol or cy2 > CANVAS + tol:
        msg = (f"объекты выходят за канвас: [{cx1:.0f}, {cy1:.0f}]–[{cx2:.0f}, {cy2:.0f}] "
               f"при канвасе 0–{CANVAS}")
        if fit == "content":
            res.warnings.append(msg + "; часть за вьюпортом SVG будет обрезана")
        else:
            res.errors.append(msg + " (попробуй --fit content)")


def _walk(node, visit) -> None:
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, visit)


def validate_tgs(data: bytes) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if len(data) > MAX_BYTES:
        errors.append(f"размер {len(data) / 1024:.1f} КБ > 64 КБ")
    try:
        doc = json.loads(gzip.decompress(data))
    except (OSError, ValueError) as e:
        return errors + [f"файл не является gzip-JSON: {e}"], warnings

    if doc.get("tgs") != 1:
        errors.append("нет маркера tgs=1")
    if (doc.get("w"), doc.get("h")) != (CANVAS, CANVAS):
        errors.append(f"канвас {doc.get('w')}×{doc.get('h')}, нужен {CANVAS}×{CANVAS}")
    if doc.get("fr") != FPS:
        errors.append(f"{doc.get('fr')} fps, нужно {FPS}")
    frames = (doc.get("op") or 0) - (doc.get("ip") or 0)
    if frames <= 0:
        errors.append("длительность 0 кадров")
    elif frames > MAX_SECONDS * FPS:
        errors.append(f"длительность {frames / FPS:.2f} с > {MAX_SECONDS} с")
    if doc.get("ddd"):
        errors.append("3D-композиция")

    layers = list(doc.get("layers", []))
    for asset in doc.get("assets", []):
        layers += asset.get("layers", []) or []
        if "p" in asset and "layers" not in asset:
            errors.append("картинка в assets")
    found: set[str] = set()
    for layer in layers:
        name = layer.get("nm") or f"слой {layer.get('ind')}"
        if layer.get("ty") in FORBIDDEN_LAYER_TYPES:
            found.add(f"{FORBIDDEN_LAYER_TYPES[layer['ty']]} ({name})")
        if layer.get("ddd"):
            found.add(f"3D-слой ({name})")
        if layer.get("hasMask") or layer.get("masksProperties"):
            found.add(f"маски ({name})")
        if layer.get("ef"):
            found.add(f"эффекты слоя ({name})")
        if layer.get("ao"):
            found.add(f"auto-orient ({name})")
        if layer.get("sr", 1) != 1:
            found.add(f"time stretching ({name})")
        if "tm" in layer:
            found.add(f"time remapping ({name})")
        if layer.get("tt"):
            warnings.append(f"track matte ({name}) официально не поддерживается")

    has_keyframes = False

    def visit(node: dict) -> None:
        nonlocal has_keyframes
        if isinstance(node.get("ty"), str) and node["ty"] in FORBIDDEN_SHAPE_TYPES:
            found.add(FORBIDDEN_SHAPE_TYPES[node["ty"]])
        if "k" in node and isinstance(node.get("x"), str):
            found.add("expressions")
        if node.get("a") == 1:
            has_keyframes = True

    _walk(layers, visit)
    errors += [f"запрещено в TGS: {f}" for f in sorted(found)]
    if has_keyframes:
        warnings.append("есть анимированные свойства: проверь вручную, что анимация зациклена")
    return errors, warnings


def convert_svg(svg_path: Path, *, duration: float = 1.0, fit: str = "content",
                padding: float = 0.0) -> ConvertResult:
    res = ConvertResult()
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as e:
        res.errors.append(f"SVG не парсится: {e}")
        return res
    _prescan(tree.getroot(), res)
    if res.errors:
        return res
    _inline_css(tree.getroot(), res)

    try:
        anim = parse_svg_etree(tree)
    except Exception as e:
        res.errors.append(f"python-lottie не разобрал SVG: {type(e).__name__}: {e}")
        return res

    frames = round(duration * FPS)
    if not 0 < frames <= MAX_SECONDS * FPS:
        res.errors.append(f"длительность {duration} с вне диапазона (0; {MAX_SECONDS}]")
        return res
    anim.frame_rate = FPS
    anim.in_point, anim.out_point = 0, frames
    for layer in anim.layers:
        layer.in_point, layer.out_point = 0, frames

    _fit(anim, fit, padding, res)
    if res.errors:
        return res

    float_strip(anim)
    lottie_dict = anim.to_dict()
    lottie_dict["tgs"] = 1
    raw = json.dumps(lottie_dict, separators=(",", ":"), ensure_ascii=False).encode()
    data = gzip.compress(raw, compresslevel=9, mtime=0)

    errors, warnings = validate_tgs(data)
    res.errors += errors
    res.warnings += warnings
    res.data = data
    return res
