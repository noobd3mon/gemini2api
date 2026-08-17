"""Regenerate gemini_web2api.py from the gemini_web2api/ package.

The single-file copy exists for standalone use (no pip install). It is GENERATED:
edit the package modules, then run `python build_single_file.py` and commit both.
This replaces the manual keep-in-sync step that previously let the two copies
diverge (different inner sizes, no stream retry, missing models).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PKG = ROOT / "gemini_web2api"
OUT = ROOT / "gemini_web2api.py"

HTTPX_BLOCK_RE = re.compile(
    r"try:\n    import httpx\n    HAS_HTTPX = True\nexcept ImportError:\n    HAS_HTTPX = False")
IMPORT_RE = re.compile(r"^(?:import .*|from \w+(?:\.\w+)* import .*)$", re.MULTILINE)

MODULES = [
    ("config.py", "config"),
    ("models.py", "models"),
    ("gemini.py", "gemini protocol"),
    ("multimodal.py", "multimodal upload"),
    ("tools.py", "tool calling"),
    ("server.py", "http server"),
]


def read(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8")


def strip_relative_imports(src: str) -> str:
    """Drop `from .x import y` lines (incl. parenthesized multi-line ones).

    In the merged file the imported names resolve directly from the same
    namespace, so the import statements must not remain at all.
    """
    out = []
    skip_depth = 0
    for line in src.splitlines(keepends=True):
        if skip_depth > 0:
            skip_depth += line.count("(") - line.count(")")
            if skip_depth <= 0:
                skip_depth = 0
            continue
        if re.match(r"^\s*from \.\S* import ", line):
            skip_depth = line.count("(") - line.count(")")
            continue
        out.append(line)
    return "".join(out)


def prepare_module(name: str) -> str:
    """Return a module's source with docstring and top-level imports removed.

    Imports are collected once and placed at the top of the merged file; the
    module docstring would otherwise end up as a stray string literal.
    """
    src = read(name)
    src = re.sub(r'\A""".*?"""\s*', '', src, flags=re.DOTALL)
    src = IMPORT_RE.sub("", src)
    return strip_relative_imports(src)


def collect_imports(sources) -> tuple:
    """Dedupe stdlib import lines (and the httpx try/except block) in order."""
    imports = []
    httpx_block = None
    for src in sources:
        m = HTTPX_BLOCK_RE.search(src)
        if m and httpx_block is None:
            httpx_block = m.group(0)
        for line in IMPORT_RE.findall(src):
            if line not in imports:
                imports.append(line)
    return imports, httpx_block


def build() -> None:
    init = read("__init__.py")
    version = re.search(r'__version__ = "([^"]+)"', init).group(1)

    module_srcs = [prepare_module(name) for name, _label in MODULES]
    main_src = strip_relative_imports(read("__main__.py"))
    main_body = main_src[main_src.index("def main():"):]

    imports, httpx_block = collect_imports([read(name) for name, _label in MODULES] + [read("__main__.py")])

    parts = [
        '"""gemini-web2api standalone single-file server. GENERATED FILE - do not edit.',
        "",
        "This file is built from the gemini_web2api/ package by build_single_file.py.",
        "Edit the package modules instead, then rerun the build script and commit.",
        '"""',
        "",
    ]
    parts.extend(imports)
    parts.append("")
    if httpx_block:
        parts.append(httpx_block)
        parts.append("")
    parts.append(f'__version__ = "{version}"')
    parts.append("")
    for src, label in zip(module_srcs, (label for _n, label in MODULES)):
        parts.append(f"# --- {label} (from gemini_web2api/) ---")
        parts.append("")
        parts.append(src.rstrip())
        parts.append("")
    parts.append("# --- entry point (from gemini_web2api/__main__.py) ---")
    parts.append("")
    parts.append(main_body.rstrip())
    parts.append("")

    out = "\n".join(parts)
    assert "from ." not in out, "relative import survived the transform"
    # Repo .py files are CRLF; write with newline translation.
    with open(OUT, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(out)
    print(f"wrote {OUT.name} ({len(out.splitlines())} lines, CRLF)")
    subprocess.run([sys.executable, "-m", "py_compile", str(OUT)], check=True)
    print("py_compile OK")


if __name__ == "__main__":
    build()
