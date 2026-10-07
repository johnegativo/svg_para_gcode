import tempfile
import unittest
from pathlib import Path

from svg_para_gcode import Config, generate_gcode, read_paths, bezier_points, distance_to_segment


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(depth=-2.5, stepdown=1, feed=300, plunge=100)

    def parse(self, body, attrs='width="40mm" height="20mm" viewBox="0 0 40 20"'):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "drawing.svg"
            file.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>', encoding="utf-8")
            return read_paths(file, self.cfg)

    def test_mm_scale_transform_and_y(self):
        path = self.parse('<g transform="translate(2 3) scale(2)"><path d="M0 0 L10 0 L10 5"/></g>')[0]
        self.assertAlmostEqual(path[0].real, 0)
        self.assertAlmostEqual(path[0].imag, 10, delta=0.0001)
        self.assertAlmostEqual(path[-1].real, 20, delta=0.0001)
        self.assertAlmostEqual(path[-1].imag, 0)

    def test_px_units(self):
        path = self.parse('<path d="M0 0 L96 0"/>', 'width="96" height="96"')[0]
        self.assertAlmostEqual(path[-1].real, 25.4)

    def test_subpaths_and_closure(self):
        paths = self.parse('<path d="M0 0 L10 0 L10 10 Z M20 0 l5 0"/>')
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0][0], paths[0][-1])
        self.assertEqual(len(paths[1]), 2)

    def test_circle_and_beziers(self):
        paths = self.parse('<circle cx="10" cy="10" r="5"/><path d="M20 0 Q30 20 40 0 C35 5 25 5 20 0"/>')
        self.assertEqual(len(paths), 2)
        self.assertGreater(len(paths[0]), 12)
        self.assertLess(abs(paths[0][0] - paths[0][-1]), 1e-8)
        self.assertGreater(len(paths[1]), 10)

    def test_bezier_error_and_collinear_reversal(self):
        controls = [0j, 10+20j, 20-20j, 30+0j]
        points = [controls[0]] + bezier_points(controls, 0.05)
        for i in range(1001):
            t = i / 1000
            p = (1-t)**3*controls[0] + 3*(1-t)**2*t*controls[1] + 3*(1-t)*t*t*controls[2] + t**3*controls[3]
            self.assertLessEqual(min(distance_to_segment(p, a, b) for a, b in zip(points, points[1:])), 0.05)
        self.assertGreater(len(bezier_points([0j, 20+0j, -20+0j, 1+0j], 0.05)), 2)

    def test_all_xy_rapids_are_retracted_and_depth_limited(self):
        paths = self.parse('<path d="M0 0 L10 0 M20 0 L30 0"/>')
        code = generate_gcode(paths, self.cfg)
        lines = code.splitlines()
        depths = []
        for i, line in enumerate(lines):
            if line.startswith("G0 X"):
                self.assertEqual(lines[i-1], "G0 Z5.0000")
            if line.startswith("G1 Z"):
                depths.append(float(line.split()[1][1:]))
        self.assertEqual(depths, [-1, -1, -2, -2, -2.5, -2.5])
        self.assertIn("M0", lines)
        self.assertEqual(lines[-3:], ["G0 Z5.0000", "M5", "M2"])

    def test_spindle_and_offsets(self):
        self.cfg.rpm = 12000
        self.cfg.offset_x = 7
        self.cfg.offset_y = 9
        paths = self.parse('<path d="M0 0 L10 0"/>')
        code = generate_gcode(paths, self.cfg)
        self.assertIn("M3 S12000\nG4 P2.000", code)
        self.assertIn("G0 X7.0000 Y9.0000", code)
        self.assertNotIn("\nM0\n", code)

    def test_hidden_paths(self):
        paths = self.parse('<g display="none"><path d="M0 0 L100 0"/></g><path d="M0 0 L2 0"/>')
        self.assertEqual(len(paths), 1)
        self.assertAlmostEqual(paths[0][-1].real, 2, delta=0.0001)

    def test_circle_chord_error(self):
        path = self.parse('<circle cx="10" cy="10" r="5"/>')[0]
        center = complex((min(p.real for p in path) + max(p.real for p in path))/2,
                         (min(p.imag for p in path) + max(p.imag for p in path))/2)
        for a, b in zip(path, path[1:]):
            self.assertLessEqual(abs(abs((a+b)/2 - center) - 5), self.cfg.tolerance + 0.0001)

    def test_viewbox_only(self):
        path = self.parse('<path d="M0 0 L96 0"/>', 'viewBox="0 0 96 96"')[0]
        self.assertAlmostEqual(path[-1].real, 25.4)

    def test_invalid_settings(self):
        for name, value in [("depth", 1), ("feed", 0), ("safe_z", -1), ("stepdown", 0), ("scale", float("nan"))]:
            with self.subTest(name=name):
                cfg = Config(-1, 1, 300, 100)
                setattr(cfg, name, value)
                with self.assertRaises(ValueError):
                    cfg.validate()

    def test_unsupported_and_empty_fail(self):
        for body in ['<text>ola</text>', '<image href="a.png"/>', '<path style="clip-path:url(#a)" d="M0 0 L1 1"/>', '']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)


if __name__ == "__main__":
    unittest.main()
