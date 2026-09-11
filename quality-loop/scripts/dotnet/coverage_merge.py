#!/usr/bin/env python3
"""Merge multiple Cobertura XML coverage reports into one.

Solution-wide `dotnet test --collect:"XPlat Code Coverage"` writes one
coverage.cobertura.xml per test project; the CRAP and coverage audits merge
them before analysis so every gate sees the whole suite.

Merge rule (deterministic — same inputs ⇒ byte-identical output): packages
keyed by name, classes by (name, filename), methods by (name, signature),
lines by number. A line hit in ANY input counts as covered: per line number
the entry with the greatest hits wins, ties break toward greater branch
coverage, then first-seen. Line/branch rates are recomputed from the merged
lines at every level (method, class, package, root); every other attribute
comes from the first file that declares it.

Coverlet relativizes document paths per test run (longest common prefix of
that run's documents), so the same source file arrives as `Features/X.cs`
from one run and `src/Proj/Features/X.cs` from another. Before merging,
every class filename is canonicalized to its longest super-suffix variant
(segment-boundary suffix): true sub-paths unify, while distinct files that
merely share a basename (e.g. two `AssemblyInfo.cs`) never merge. Without
this, unified classes split into covered + phantom-uncovered duplicates
that inflate totals and fake CRAP/coverage offenders.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

CONDITION_RE = re.compile(r"\((\d+)/(\d+)\)")


def _condition(text: str | None) -> tuple[int, int]:
    """condition-coverage attr -> (covered, valid), for winner ordering."""
    m = CONDITION_RE.search(text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    if (text or "").strip() == "100%":
        return 1, 1
    return 0, 0


def _segments(path: str) -> list[str]:
    return [s for s in path.replace("\\", "/").split("/") if s and s != "."]


def _canonical_filenames(roots: list[ET.Element]) -> dict[str, str]:
    """Map every class filename to its longest super-suffix variant.

    Input-only and deterministic: for filename A that is a segment-boundary
    suffix of a longer filename B, A canonicalizes to the longest such B
    (lexicographically greatest on length ties). Filenames with no longer
    super-suffix map to themselves; distinct files sharing only a basename
    are never unified.
    """
    names = {c.get("filename") for r in roots for c in r.iter("class") if c.get("filename")}
    seg = {n: _segments(n) for n in names}
    canon = {}
    for n in names:
        longer = [m for m in names if m != n
                  and len(seg[m]) > len(seg[n])
                  and seg[m][-len(seg[n]):] == seg[n]]
        canon[n] = max(longer, key=lambda m: (len(seg[m]), m)) if longer else n
    return canon


def _line_key(line: ET.Element) -> tuple[int, int, int]:
    return (int(line.get("hits", "0") or 0), *_condition(line.get("condition-coverage")))


def _strip_ws(el: ET.Element) -> None:
    """Clear whitespace-only text/tails: pretty-printed and compact inputs merge identically."""
    if el.text is not None and not el.text.strip():
        el.text = None
    if el.tail is not None and not el.tail.strip():
        el.tail = None
    for child in el:
        _strip_ws(child)


def _copy(el: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(el))


def _merge_lines(dst: ET.Element, src: ET.Element) -> None:
    """Merge src's <line> children into dst's, winners by _line_key, in place."""
    index = {ln.get("number"): i for i, ln in enumerate(dst.findall("line"))}
    for s in src.findall("line"):
        num = s.get("number")
        if num in index and _line_key(s) > _line_key(dst[index[num]]):
            dst[index[num]] = _copy(s)
        elif num not in index:
            index[num] = len(dst)
            dst.append(_copy(s))


def _lines_el(parent: ET.Element) -> ET.Element:
    lines = parent.find("lines")
    if lines is None:
        lines = ET.SubElement(parent, "lines")
    return lines


def _merge_method(dst_cls: ET.Element, src_method: ET.Element) -> None:
    methods = dst_cls.find("methods")
    if methods is None:
        methods = ET.SubElement(dst_cls, "methods")
    key = (src_method.get("name"), src_method.get("signature") or "")
    for dst_method in methods.findall("method"):
        if (dst_method.get("name"), dst_method.get("signature") or "") == key:
            src_lines = src_method.find("lines")
            if src_lines is not None:
                _merge_lines(_lines_el(dst_method), src_lines)
            return
    methods.append(_copy(src_method))


def _merge_class(dst_pkg: ET.Element, src_cls: ET.Element) -> None:
    classes = dst_pkg.find("classes")
    if classes is None:
        classes = ET.SubElement(dst_pkg, "classes")
    key = (src_cls.get("name"), src_cls.get("filename"))
    for dst_cls in classes.findall("class"):
        if (dst_cls.get("name"), dst_cls.get("filename")) == key:
            src_methods = src_cls.find("methods")
            if src_methods is not None:
                for src_method in src_methods.findall("method"):
                    _merge_method(dst_cls, src_method)
            src_lines = src_cls.find("lines")
            if src_lines is not None:
                _merge_lines(_lines_el(dst_cls), src_lines)
            return
    classes.append(_copy(src_cls))


def _class_lines(dst_cls: ET.Element) -> list[ET.Element]:
    """The lines a class contributes to rates: method lines, else class lines."""
    method_lines = [ln for m in dst_cls.findall("methods/method")
                    for ln in m.findall("lines/line")]
    if method_lines:
        return method_lines
    lines = dst_cls.find("lines")
    return lines.findall("line") if lines is not None else []


def _set_rates(el: ET.Element, lines: list[ET.Element]) -> tuple[int, int, int, int]:
    covered = sum(1 for ln in lines if int(ln.get("hits", "0") or 0) > 0)
    valid = len(lines)
    b_covered = b_valid = 0
    for ln in lines:
        if ln.get("branch") == "True":
            c, v = _condition(ln.get("condition-coverage"))
            b_covered += c
            b_valid += v
    el.set("line-rate", str(covered / valid if valid else 0.0))
    el.set("branch-rate", str(b_covered / b_valid if b_valid else 0.0))
    el.set("lines-covered", str(covered))
    el.set("lines-valid", str(valid))
    el.set("branches-covered", str(b_covered))
    el.set("branches-valid", str(b_valid))
    return covered, valid, b_covered, b_valid


def merge_coverages(paths: list[Path] | tuple[Path, ...]) -> ET.Element:
    """Merge Cobertura files into a single <coverage> element."""
    paths = list(paths)
    if not paths:
        raise ValueError("need at least one coverage file to merge")
    roots = [ET.parse(str(p)).getroot() for p in paths]
    for root in roots:
        _strip_ws(root)
    canon = _canonical_filenames(roots)
    for root in roots:
        for cls in root.iter("class"):
            fn = cls.get("filename")
            if fn in canon:
                cls.set("filename", canon[fn])

    merged = ET.Element("coverage", dict(roots[0].attrib))
    sources = ET.SubElement(merged, "sources")
    for src in sorted({s.text or "" for r in roots for s in r.findall("sources/source")}):
        ET.SubElement(sources, "source").text = src
    packages = ET.SubElement(merged, "packages")

    totals = [0, 0, 0, 0]
    for root in roots:
        src_packages = root.find("packages")
        if src_packages is None:
            continue
        for src_pkg in src_packages.findall("package"):
            dst_pkg = next((p for p in packages.findall("package")
                            if p.get("name") == src_pkg.get("name")), None)
            if dst_pkg is None:
                dst_pkg = _copy(src_pkg)
                # strip stale rates; recomputed below from merged lines
                packages.append(dst_pkg)
            else:
                src_classes = src_pkg.find("classes")
                if src_classes is not None:
                    for src_cls in src_classes.findall("class"):
                        _merge_class(dst_pkg, src_cls)

    for dst_pkg in packages.findall("package"):
        c = v = bc = bv = 0
        for dst_cls in dst_pkg.findall("classes/class"):
            # Per-method rates must be recomputed from the merged lines too.
            # A method's surviving line-rate/branch-rate comes from the first
            # file that declared it (often a test run where it was uncovered);
            # after other runs contribute hits the stale 0 makes the CRAP
            # consumer read a well-covered method as untested.
            for method in dst_cls.findall("methods/method"):
                method_lines = method.find("lines")
                _set_rates(method, method_lines.findall("line") if method_lines is not None else [])
            cc, vv, cbc, vbv = _set_rates(dst_cls, _class_lines(dst_cls))
            c += cc
            v += vv
            bc += cbc
            bv += vbv
        dst_pkg.set("line-rate", str(c / v if v else 0.0))
        dst_pkg.set("branch-rate", str(bc / bv if bv else 0.0))
        dst_pkg.set("lines-covered", str(c))
        dst_pkg.set("lines-valid", str(v))
        dst_pkg.set("branches-covered", str(bc))
        dst_pkg.set("branches-valid", str(bv))
        totals[0] += c
        totals[1] += v
        totals[2] += bc
        totals[3] += bv

    c, v, bc, bv = totals
    merged.set("line-rate", str(c / v if v else 0.0))
    merged.set("branch-rate", str(bc / bv if bv else 0.0))
    merged.set("lines-covered", str(c))
    merged.set("lines-valid", str(v))
    merged.set("branches-covered", str(bc))
    merged.set("branches-valid", str(bv))
    return merged


def write_merged(paths: list[Path] | tuple[Path, ...], out: Path) -> Path:
    """Merge Cobertura files and write the result to `out` (with declaration)."""
    root = merge_coverages(paths)
    ET.indent(root)
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: coverage_merge.py <out.xml> <in1.xml> [<in2.xml> ...]")
    write_merged([Path(p) for p in sys.argv[2:]], Path(sys.argv[1]))
