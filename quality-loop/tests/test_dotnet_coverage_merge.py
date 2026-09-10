"""Tests for scripts/dotnet/coverage_merge.py — multi-project Cobertura merging.

Covers the per-line winner rule (greatest hits, then branch coverage, then
first-seen), structural union (packages/classes/methods/sources), rate
recomputation, determinism, idempotence, and the write_merged file output —
all with synthetic XML, no dotnet needed.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.dotnet import coverage_merge as cm


def line(number: int, hits: int, branch: str = "False", condition: str | None = None) -> str:
    cond = f' condition-coverage="{condition}"' if condition else ""
    return f'<line number="{number}" hits="{hits}" branch="{branch}"{cond} />'


def method_xml(name: str, lines: str, complexity: int = 1) -> str:
    return (f'<method name="{name}" signature="()" complexity="{complexity}\">'
            f"<lines>{lines}</lines></method>")


def class_xml(name: str, filename: str, methods: str) -> str:
    return (f'<class name="{name}" filename="{filename}" line-rate="0" branch-rate="0\">'
            f"<methods>{methods}</methods><lines /></class>")


def doc(packages: str, sources: str = "<source>/repo</source>") -> str:
    return (f'<coverage line-rate="0" branch-rate="0" version="1.9\" timestamp="1\" '
            f'lines-covered="0" lines-valid="0" branches-covered="0" branches-valid="0\">'
            f"<sources>{sources}</sources><packages>{packages}</packages></coverage>")


def pkg(name: str, classes: str) -> str:
    return f'<package name="{name}" line-rate="0" branch-rate="0\"><classes>{classes}</classes></package>'


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def lines_of(root: ET.Element, cls: str) -> dict[str, ET.Element]:
    for c in root.findall("packages/package/classes/class"):
        if c.get("name") == cls:
            return {ln.get("number"): ln for m in c.findall("methods/method")
                    for ln in m.findall("lines/line")}
    raise AssertionError(f"class {cls} missing")


def test_merge_disjoint_classes_union(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs", method_xml("M", line(1, 1))))))
    b = write(tmp_path, "b.xml", doc(pkg("Lib", class_xml("B", "b.cs", method_xml("N", line(2, 0))))))
    merged = cm.merge_coverages([a, b])
    assert {c.get("name") for c in merged.findall("packages/package/classes/class")} == {"A", "B"}
    assert merged.find("packages/package").get("name") == "Lib"


def test_merge_winner_is_greatest_hits(tmp_path):
    verbo = method_xml("M", line(1, 0) + line(2, 3))
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs", verbo))))
    b = write(tmp_path, "b.xml", doc(pkg("Lib", class_xml("A", "a.cs",
          method_xml("M", line(1, 5) + line(2, 1))))))
    merged = cm.merge_coverages([a, b])
    got = lines_of(merged, "A")
    assert got["1"].get("hits") == "5"  # hit in ANY input counts
    assert got["2"].get("hits") == "3"


def test_merge_tie_breaks_toward_branch_coverage(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs",
          method_xml("M", line(1, 2, "True", "50% (1/2)"))))))
    b = write(tmp_path, "b.xml", doc(pkg("Lib", class_xml("A", "a.cs",
          method_xml("M", line(1, 2, "True", "100% (2/2)"))))))
    merged = cm.merge_coverages([a, b])
    assert lines_of(merged, "A")["1"].get("condition-coverage") == "100% (2/2)"


def test_merge_recomputes_rates(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs",
          method_xml("M", line(1, 1) + line(2, 0))))))
    merged = cm.merge_coverages([a])
    cls = merged.find("packages/package/classes/class")
    assert cls.get("lines-covered") == "1" and cls.get("lines-valid") == "2"
    assert cls.get("line-rate") == str(1 / 2)
    assert merged.get("lines-covered") == "1" and merged.get("branch-rate") == "0.0"


def test_merge_sources_union_sorted(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", ""), "<source>/z</source>"))
    b = write(tmp_path, "b.xml", doc(pkg("Lib", ""), "<source>/a</source><source>/z</source>"))
    merged = cm.merge_coverages([a, b])
    assert [s.text for s in merged.findall("sources/source")] == ["/a", "/z"]


def test_merge_deterministic_and_idempotent(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs", method_xml("M", line(1, 1))))))
    b = write(tmp_path, "b.xml", doc(pkg("Lib", class_xml("A", "a.cs", method_xml("M", line(1, 0) + line(2, 4))))))
    once = ET.tostring(cm.merge_coverages([a, b]))
    assert ET.tostring(cm.merge_coverages([a, b])) == once  # same inputs ⇒ same bytes
    mfile = cm.write_merged([a, b], tmp_path / "merged.xml")
    again = ET.tostring(cm.merge_coverages([mfile, a]))
    assert again == once  # merging the merge changes nothing


def test_merge_empty_raises():
    try:
        cm.merge_coverages([])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_write_merged_declaration_and_roundtrip(tmp_path):
    a = write(tmp_path, "a.xml", doc(pkg("Lib", class_xml("A", "a.cs", method_xml("M", line(1, 1))))))
    out = cm.write_merged([a], tmp_path / "out.xml")
    text = out.read_text()
    assert text.startswith("<?xml")
    assert ET.parse(str(out)).getroot().tag == "coverage"
