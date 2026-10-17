"""Command line interface (for power users and automation; artists should use the GUI)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import convert
from .core.plugin import codecs, load_plugins


def _out(s: str = "") -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"))


def cmd_apps(_a) -> int:
    for d in convert.detect_all():
        i = d.install
        if i:
            _out(f"[{d.adapter.id}] {i.label}{' (running)' if i.running else ''}\n    exe:  {i.exe}\n    data: {i.data_dir}")
        else:
            _out(f"[{d.adapter.id}] {d.adapter.name}: {d.note}")
    return 0


def cmd_formats(_a) -> int:
    for c in codecs():
        rw = ("R" if c.can_read else "-") + ("W" if c.can_write else "-")
        _out(f"{c.id:10} {rw}  {' '.join(c.extensions):28} {c.name}")
    return 0


def cmd_list(a) -> int:
    o = convert.open_file(Path(a.file))
    if o.error:
        _out(o.error)
        return 1
    _out(f"{o.info.format_name}  v{o.info.version}  ({len(o.refs)} brushes)")
    for i, r in enumerate(o.refs):
        _out(f"{i:4}  {r.group + ' / ' if r.group else ''}{r.name}  {r.detail}")
    return 0


def cmd_diagnose(a) -> int:
    info = convert.diagnose(Path(a.file))
    if info is None:
        _out("无法识别的文件")
        return 1
    _out(f"{info.format_name}  版本 {info.version}")
    for k, v in info.details.items():
        _out(f"  {k}: {v}")
    for p in info.problems:
        _out(f"  ! {p}")
    for i, f in enumerate(info.fixes):
        _out(f"  fix {i}: {f.title} — {f.description}")
    if a.fix is not None:
        files = info.fixes[a.fix].apply(Path(a.file), Path(a.out or Path(a.file).parent))
        for f in files:
            _out(f"→ {f}")
    return 0


def _parse_indices(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return [i for i in out if 0 <= i < n]


def cmd_convert(a) -> int:
    o = convert.open_file(Path(a.file))
    if o.error:
        _out(o.error)
        return 1
    refs = o.refs if not a.only else [o.refs[i] for i in _parse_indices(a.only, len(o.refs))]
    brushes, errors = convert.load_brushes(refs)
    for e in errors:
        _out(e)
    files = convert.export(brushes, a.to, Path(a.out))
    for b in brushes:
        for n in b.notes:
            _out(f"  [{b.name}] {n}")
    for f in files:
        _out(f"→ {f}")
    return 0 if files else 1


def cmd_install(a) -> int:
    o = convert.open_file(Path(a.file))
    if o.error:
        _out(o.error)
        return 1
    targets = [d for d in convert.detect_all() if d.adapter.id == a.app and d.install]
    if not targets:
        _out(f"{a.app} not found")
        return 1
    refs = o.refs if not a.only else [o.refs[i] for i in _parse_indices(a.only, len(o.refs))]
    brushes, _errors = convert.load_brushes(refs)
    res = convert.install(targets[0], brushes)
    _out(res.message)
    return 0 if res.ok else 1


def main(argv=None) -> int:
    load_plugins()
    p = argparse.ArgumentParser(prog="brush-bridge-cli", description="Convert brushes between painting apps.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apps", help="detect installed painting apps").set_defaults(fn=cmd_apps)
    sub.add_parser("formats", help="list supported formats").set_defaults(fn=cmd_formats)
    s = sub.add_parser("list", help="list brushes in a file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("diagnose", help="check a file for import problems")
    s.add_argument("file")
    s.add_argument("--fix", type=int, help="apply fix number N")
    s.add_argument("--out", help="output folder for --fix")
    s.set_defaults(fn=cmd_diagnose)
    s = sub.add_parser("convert", help="convert a brush file to another format")
    s.add_argument("file")
    s.add_argument("--to", required=True, help="target format id (see 'formats')")
    s.add_argument("-o", "--out", required=True, help="output file")
    s.add_argument("--only", help="brush indices, e.g. 0,3,5-8")
    s.set_defaults(fn=cmd_convert)
    s = sub.add_parser("install", help="install brushes from a file into an app")
    s.add_argument("file")
    s.add_argument("--app", required=True, help="app id (see 'apps')")
    s.add_argument("--only", help="brush indices, e.g. 0,3,5-8")
    s.set_defaults(fn=cmd_install)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
