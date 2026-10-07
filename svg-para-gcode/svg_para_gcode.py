"""Converte geometria SVG em trajetos de centro de ferramenta para fresadora."""
from __future__ import annotations

import argparse
import io
import math
from pathlib import Path as FilePath
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

try:
    from svgelements import SVG, Shape, Path, Move, Line, Close, CubicBezier, QuadraticBezier, Arc
except ImportError:
    raise SystemExit("Instale a dependencia: python -m pip install -r requirements.txt")


@dataclass
class Config:
    depth: float
    stepdown: float
    feed: float
    plunge: float
    safe_z: float = 5.0
    rpm: float | None = None
    spindle_wait: float = 2.0
    tolerance: float = 0.05
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def validate(self):
        for name, value in vars(self).items():
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name}: informe um numero finito.")
        if self.depth >= 0:
            raise ValueError("A profundidade deve ser negativa; Z=0 e a superficie da peca.")
        if abs(self.depth) < 0.0001 or self.stepdown < 0.0001 or self.safe_z < 0.0001:
            raise ValueError("Profundidade, passo Z e altura segura devem ter magnitude minima de 0.0001 mm.")
        for name in ("stepdown", "feed", "plunge", "safe_z", "tolerance", "scale"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} deve ser maior que zero.")
        if self.tolerance < 0.001:
            raise ValueError("Tolerancia minima: 0.001 mm.")
        if self.feed < 0.001 or self.plunge < 0.001:
            raise ValueError("Avancos devem ser pelo menos 0.001 mm/min.")
        if self.rpm is not None and self.rpm < 1:
            raise ValueError("RPM deve ser pelo menos 1.")
        if self.spindle_wait < 0:
            raise ValueError("Espera do spindle nao pode ser negativa.")
        if math.ceil(abs(self.depth) / self.stepdown) > 10000:
            raise ValueError("Mais de 10000 passadas: confira profundidade e passo Z.")


def point(p, factor):
    result = complex(float(p.x) * factor, float(p.y) * factor)
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise ValueError("O SVG contem coordenadas invalidas.")
    return result


def distance_to_segment(p, a, b):
    delta = b - a
    if abs(delta) == 0:
        return abs(p - a)
    t = max(0.0, min(1.0, ((p - a).conjugate() * delta).real / abs(delta) ** 2))
    return abs(p - (a + t * delta))


def bezier_points(controls, tolerance, level=0):
    # A curva fica no fecho convexo dos controles. Subdivisao de De Casteljau.
    if max(distance_to_segment(p, controls[0], controls[-1]) for p in controls[1:-1]) <= tolerance:
        return [controls[-1]]
    if level >= 24:
        raise ValueError("Curva complexa demais; simplifique o SVG ou aumente a tolerancia.")
    rows = [controls]
    while len(rows[-1]) > 1:
        rows.append([(a + b) / 2 for a, b in zip(rows[-1], rows[-1][1:])])
    left = [row[0] for row in rows]
    right = [row[-1] for row in reversed(rows)]
    return bezier_points(left, tolerance, level + 1) + bezier_points(right, tolerance, level + 1)


def flatten(segment, factor, tolerance):
    if isinstance(segment, (Line, Close)):
        return [point(segment.end, factor)]
    if isinstance(segment, CubicBezier):
        controls = [segment.start, segment.control1, segment.control2, segment.end]
    elif isinstance(segment, QuadraticBezier):
        controls = [segment.start, segment.control, segment.end]
    elif isinstance(segment, Arc):
        # Limite conservador para a segunda derivada da elipse, inclusive apos escala.
        radius = (abs(point(segment.prx, factor) - point(segment.center, factor))
                  + abs(point(segment.pry, factor) - point(segment.center, factor)))
        count = max(1, math.ceil(abs(segment.sweep) * math.sqrt(radius / (8 * tolerance))))
        if count > 100000:
            raise ValueError("Arco exige pontos demais; confira escala e tolerancia.")
        return [point(segment.point(i / count), factor) for i in range(1, count + 1)]
    else:
        raise ValueError(f"Segmento nao suportado: {type(segment).__name__}")
    return bezier_points([point(p, factor) for p in controls], tolerance)


def read_paths(filename, cfg):
    cfg.validate()
    data = FilePath(filename).read_bytes()
    if len(data) > 10_000_000:
        raise ValueError("SVG maior que 10 MB; simplifique o arquivo.")
    # Rejeita DTD/entidades, inclusive documentos UTF-16/32.
    if b"<!DOCTYPE" in data.replace(b"\x00", b"").upper() or b"<!ENTITY" in data.replace(b"\x00", b"").upper():
        raise ValueError("SVG com DTD ou entidades nao e aceito.")
    root = ET.fromstring(data)
    tag = lambda node: node.tag.rsplit("}", 1)[-1]
    if tag(root) != "svg":
        raise ValueError("O arquivo precisa ter uma raiz SVG.")
    unsupported = {"text", "image", "use", "symbol", "clipPath", "mask", "filter", "style",
                   "foreignObject", "pattern", "switch", "animate", "animateTransform", "set"}
    for node in root.iter():
        if tag(node) in unsupported:
            raise ValueError(f"Elemento <{tag(node)}> nao suportado. Converta em caminhos simples no editor SVG.")
        if tag(node) == "svg" and node is not root:
            raise ValueError("SVGs aninhados nao sao suportados; converta em um unico desenho.")
        for attr in ("clip-path", "mask", "filter"):
            if node.get(attr, "none") != "none" or attr in node.get("style", ""):
                raise ValueError(f"Recurso {attr} nao suportado; aplique a geometria no editor SVG.")
    viewbox = root.get("viewBox", "").replace(",", " ").split()
    for key, index in (("width", 2), ("height", 3)):
        if key not in root.attrib:
            if len(viewbox) != 4:
                raise ValueError("Informe width/height ou viewBox no SVG para definir a escala.")
            root.set(key, viewbox[index])
        if "%" in root.get(key):
            raise ValueError("O tamanho principal do SVG deve ser absoluto, sem porcentagens.")
    svg = SVG.parse(io.StringIO(ET.tostring(root, encoding="unicode")), ppi=96,
                    reify=True, on_error="raise")
    factor = 25.4 / 96 * cfg.scale
    paths = []
    total = 0
    for element in svg.elements():
        if not isinstance(element, Shape):
            continue
        if element.values.get("visibility") in ("hidden", "collapse"):
            continue
        if float(element.values.get("opacity", 1)) == 0:
            continue
        current = []
        for segment in Path(element):
            if isinstance(segment, Move):
                if len(current) > 1:
                    paths.append(current)
                current = [point(segment.end, factor)]
                continue
            if not current:
                current = [point(segment.start, factor)]
            for p in flatten(segment, factor, cfg.tolerance):
                if abs(p - current[-1]) > 1e-9:
                    current.append(p)
                    total += 1
                    if total > 1_000_000:
                        raise ValueError("O desenho excede um milhao de segmentos.")
        if len(current) > 1:
            paths.append(current)
    if not paths:
        raise ValueError("Nenhum trajeto utilizavel encontrado no SVG.")
    min_x = min(p.real for path in paths for p in path)
    max_y = max(p.imag for path in paths for p in path)
    # Origem no canto inferior esquerdo do desenho; Y cresce para cima na CNC.
    return [[complex(p.real - min_x + cfg.offset_x, max_y - p.imag + cfg.offset_y)
             for p in path] for path in paths]


def generate_gcode(paths, cfg):
    cfg.validate()
    if not paths or any(len(path) < 2 for path in paths):
        raise ValueError("Forneca trajetos com pelo menos dois pontos.")
    if any(not math.isfinite(p.real) or not math.isfinite(p.imag) for path in paths for p in path):
        raise ValueError("Trajeto com coordenadas invalidas.")
    count = math.ceil(abs(cfg.depth) / cfg.stepdown)
    if sum(len(path) + 4 for path in paths) * count > 2_000_000:
        raise ValueError("G-code excederia dois milhoes de linhas; simplifique os parametros.")
    xs = [p.real for path in paths for p in path]
    ys = [p.imag for path in paths for p in path]
    lines = ["(SVG PARA GCODE - centro da ferramenta, sem compensacao)",
             "(G54: Z0 na superficie; XY relativo ao canto inferior esquerdo)",
             f"(Limites X {min(xs):.4f} a {max(xs):.4f}; Y {min(ys):.4f} a {max(ys):.4f} mm)",
             "G21", "G90", "G17", "G94", "G40", "G49", "G54", "M5",
             f"G0 Z{cfg.safe_z:.4f}"]
    if cfg.rpm is not None:
        lines.extend([f"M3 S{cfg.rpm:.0f}", f"G4 P{cfg.spindle_wait:.3f}"])
    else:
        lines.extend(["(Ligue o spindle manualmente antes de continuar)", "M0"])
    for index in range(1, count + 1):
        z = max(cfg.depth, -index * cfg.stepdown)
        lines.append(f"(Passada {index}/{count} - Z {z:.4f})")
        for path in paths:
            lines.extend([f"G0 Z{cfg.safe_z:.4f}",
                          f"G0 X{path[0].real:.4f} Y{path[0].imag:.4f}",
                          f"G1 Z{z:.4f} F{cfg.plunge:.3f}"])
            for p in path[1:]:
                lines.append(f"G1 X{p.real:.4f} Y{p.imag:.4f} F{cfg.feed:.3f}")
            lines.append(f"G0 Z{cfg.safe_z:.4f}")
    lines.extend(["M5", "M2"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=FilePath, help="Arquivo SVG de entrada")
    parser.add_argument("-o", "--saida", type=FilePath, required=True, help="G-code de saida (.nc)")
    parser.add_argument("--profundidade", type=float, required=True, help="Z final negativo, em mm")
    parser.add_argument("--passo-z", type=float, required=True, help="Profundidade maxima por passada, mm")
    parser.add_argument("--avanco", type=float, required=True, help="Avanco XY em mm/min")
    parser.add_argument("--mergulho", type=float, required=True, help="Avanco Z em mm/min")
    parser.add_argument("--z-seguro", type=float, default=5, help="Altura de retracao acima de Z0, mm (5)")
    parser.add_argument("--rpm", type=float, help="Aciona spindle com M3; sem esta opcao usa parada M0")
    parser.add_argument("--espera-spindle", type=float, default=2, help="Espera apos M3, segundos (2)")
    parser.add_argument("--tolerancia", type=float, default=0.05, help="Erro de aproximacao das curvas, mm (0.05)")
    parser.add_argument("--escala", type=float, default=1, help="Fator de escala (1)")
    parser.add_argument("--offset-x", type=float, default=0, help="Deslocamento X em mm")
    parser.add_argument("--offset-y", type=float, default=0, help="Deslocamento Y em mm")
    parser.add_argument("--sobrescrever", action="store_true", help="Permite substituir uma saida existente")
    args = parser.parse_args()
    try:
        if args.svg.resolve() == args.saida.resolve():
            raise ValueError("A saida nao pode substituir o SVG de entrada.")
        cfg = Config(args.profundidade, args.passo_z, args.avanco, args.mergulho,
                     args.z_seguro, args.rpm, args.espera_spindle, args.tolerancia,
                     args.escala, args.offset_x, args.offset_y)
        paths = read_paths(args.svg, cfg)
        code = generate_gcode(paths, cfg)
        with args.saida.open("w" if args.sobrescrever else "x", encoding="ascii", newline="\n") as output:
            output.write(code)
        print(f"Criado: {args.saida.resolve()} | {len(paths)} trajetos | {len(code.splitlines())} linhas")
    except (OSError, ValueError, ET.ParseError, TypeError, ZeroDivisionError, OverflowError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
