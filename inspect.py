"""Inspect patent DOCX files before translation.

Usage:
    uv run python inspect.py ./data
    uv run python inspect.py ./data/document.docx
"""

from __future__ import annotations

import importlib.util
import sys
import sysconfig

if __name__ == "inspect":
    _stdlib_inspect = sysconfig.get_path("stdlib") + "/inspect.py"
    _spec = importlib.util.spec_from_file_location("inspect", _stdlib_inspect)
    if _spec is None or _spec.loader is None:
        raise ImportError("cannot load standard-library inspect module")
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    globals().update(_module.__dict__)
else:
    import argparse
    from pathlib import Path

    from docx import Document

    from translate.client import ClientConfig, LLMClient
    from translate.agent.docx_utils import (
        extract_all_text,
        has_drawing,
        has_math,
        iter_all_paragraphs,
    )
    from translate.agent.nodes.chunk_abstract import chunk_abstract
    from translate.agent.nodes.chunk_body import chunk_body
    from translate.agent.nodes.chunk_claims import chunk_claims
    from translate.agent.nodes.classify import classify


def _docx_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".docx" else []
    if path.is_dir():
        return sorted(p for p in path.rglob("*.docx") if not p.name.startswith("~$"))
    return []


def _preview(text: str, limit: int = 96) -> str:
    text = text.replace("\n", " ⏎ ").replace("\t", " ⇥ ")
    return text[:limit] + ("..." if len(text) > limit else "")


def inspect_docx(path: Path, *, max_rows: int, client=None) -> bool:
    print(f"\n== {path} ==")
    try:
        doc = Document(path)
    except Exception as exc:
        print(f"ERROR: cannot open DOCX: {type(exc).__name__}: {exc}")
        return False

    top_level = len(doc.paragraphs)
    paragraphs = list(iter_all_paragraphs(doc))
    print(f"top-level paragraphs : {top_level}")
    print(f"all paragraphs       : {len(paragraphs)}")
    if top_level == 0 and paragraphs:
        print("note: text appears to be table-wrapped; recursive extraction is required and enabled.")
    if not paragraphs:
        print("ERROR: no paragraphs found.")
        return False

    state = {"doc": doc, "font": "Times New Roman", "progress": lambda _: None}
    if client is not None:
        state["client"] = client
    try:
        state.update(classify(state))
        state.update(chunk_body(state))
        state.update(chunk_abstract(state))
        state.update(chunk_claims(state))
    except Exception as exc:
        print(f"ERROR: classifier/chunker failed: {type(exc).__name__}: {exc}")
        return False

    records = state["records"]
    counts: dict[str, int] = {}
    for r in records:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    sections = sorted({r.section for r in records if r.section})
    print("record kinds         : " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"sections             : {sections or 'none'}")
    print(
        "chunks               : "
        f"body={len(state.get('chunks_body', []))}, "
        f"abstract={len(state.get('chunks_abstract', []))}, "
        f"claims={len(state.get('chunks_claims', []))}"
    )

    warnings: list[str] = []
    if not sections:
        warnings.append("no section headers detected")
    if counts.get("text", 0) == 0:
        warnings.append("no translatable text records detected")
    if any(has_drawing(p) and extract_all_text(p).strip() for p in paragraphs):
        warnings.append("mixed text+drawing paragraphs detected")
    if any(has_math(p) and extract_all_text(p, math_as_placeholder=True).strip() for p in paragraphs):
        warnings.append("math-bearing text paragraphs detected")
    if state.get("chunks_claims") and not any(r.kind == "claim_header" for r in records):
        warnings.append("claims detected without standalone claim headers")
    if warnings:
        print("warnings             : " + "; ".join(warnings))

    print("\nfirst records:")
    for r in records[:max_rows]:
        flags = []
        if has_math(r.para):
            flags.append("math")
        if has_drawing(r.para):
            flags.append("drawing")
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        print(
            f"  [{r.index:>4}] {r.kind:<14s} "
            f"section={str(r.section or '-'):<32s}{flag_text} "
            f"{_preview(r.raw or '')!r}"
        )

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect DOCX parsing/chunking readiness.")
    parser.add_argument("path", type=Path, help="A .docx file or a directory containing .docx files")
    parser.add_argument("--max-rows", type=int, default=30, help="Rows to print per document")
    parser.add_argument(
        "--llm-classify",
        action="store_true",
        help="Use the same LLM fallback paragraph classifier as the translation pipeline",
    )
    parser.add_argument("--model", default=None, help="Model for --llm-classify (default: .env/ClientConfig)")
    parser.add_argument("--base-url", default=None, help="API base URL for --llm-classify")
    parser.add_argument("--api-key", default=None, help="API key for --llm-classify")
    args = parser.parse_args()

    paths = _docx_paths(args.path)
    if not paths:
        raise SystemExit(f"No .docx files found at {args.path}")

    client = None
    if args.llm_classify:
        env = ClientConfig.from_env()
        config = ClientConfig(
            model=args.model or env.model,
            base_url=args.base_url or env.base_url,
            api_key=args.api_key or env.api_key,
            temperature=env.temperature,
            max_tokens=env.max_tokens,
        )
        client = LLMClient(config)

    ok = True
    for path in paths:
        ok = inspect_docx(path, max_rows=args.max_rows, client=client) and ok
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
