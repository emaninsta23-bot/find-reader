#!/usr/bin/env python3
"""
Find Reader — Ebook to Audiobook Converter
Supports: PDF, EPUB, DOCX, TXT  →  MP3
Voice: Microsoft Edge TTS (free, high-quality neural voices)
"""

import asyncio
import edge_tts
import os
import sys
import re
import tempfile
import subprocess
import argparse
from pathlib import Path


VOICES = {
    "aria":   "en-US-AriaNeural",       # Female, warm & expressive
    "jenny":  "en-US-JennyNeural",      # Female, friendly
    "guy":    "en-US-GuyNeural",        # Male, confident
    "ryan":   "en-GB-RyanNeural",       # Male, British
    "sonia":  "en-GB-SoniaNeural",      # Female, British
    "natasha":"en-AU-NatashaNeural",    # Female, Australian
}
DEFAULT_VOICE = "aria"


# ── Text Extraction ──────────────────────────────────────────────────────────

def extract_pdf(path: str) -> str:
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    doc.close()
    return "\n".join(pages)


def extract_epub(path: str) -> str:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(path, options={"ignore_ncx": True})
    chapters = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        chapters.append(soup.get_text(separator="\n"))
    return "\n\n".join(chapters)


def extract_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def extract_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def extract_text(path: str) -> str:
    ext = Path(path).suffix.lower()
    extractors = {
        ".pdf": extract_pdf,
        ".epub": extract_epub,
        ".docx": extract_docx,
        ".doc": extract_docx,
        ".txt": extract_txt,
    }
    if ext not in extractors:
        raise ValueError(f"Unsupported format '{ext}'. Supported: PDF, EPUB, DOCX, TXT")
    return extractors[ext](path)


# ── Text Cleaning ────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    # Fix hyphenated line breaks (e.g. "impor-\ntant" → "important")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Remove lone page numbers
    text = re.sub(r"\n[ \t]*\d{1,4}[ \t]*\n", "\n", text)
    # Collapse 3+ blank lines to a paragraph break
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Normalize weird whitespace
    text = re.sub(r"[ \t]+", " ", text)
    # Remove lines that are just punctuation/symbols with no words
    text = re.sub(r"\n[^a-zA-Z0-9\n]{0,10}\n", "\n", text)
    return text.strip()


# ── Chunking ─────────────────────────────────────────────────────────────────

def split_into_chunks(text: str, max_chars: int = 2500) -> list[str]:
    """Split text into chunks at paragraph or sentence boundaries."""
    paragraphs = re.split(r"\n\n+", text)
    chunks = []
    current_parts = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If a single paragraph exceeds max, split it at sentence boundaries
        if len(para) > max_chars:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sentence in sentences:
                if current_len + len(sentence) > max_chars and current_parts:
                    chunks.append(" ".join(current_parts))
                    current_parts = [sentence]
                    current_len = len(sentence)
                else:
                    current_parts.append(sentence)
                    current_len += len(sentence)
        elif current_len + len(para) > max_chars and current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = [para]
            current_len = len(para)
        else:
            current_parts.append(para)
            current_len += len(para)

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return [c for c in chunks if c.strip()]


# ── Audio Generation ─────────────────────────────────────────────────────────

async def chunk_to_mp3(text: str, path: str, voice: str, rate: str, pitch: str):
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(path)


async def convert(input_path: str, output_path: str, voice: str,
                  rate: str = "+0%", pitch: str = "+0Hz"):

    print(f"\n  Book   : {Path(input_path).name}")
    print(f"  Voice  : {voice}")
    print(f"  Output : {output_path}\n")

    print("Extracting text...")
    text = extract_text(input_path)
    text = clean_text(text)
    print(f"  {len(text):,} characters extracted")

    chunks = split_into_chunks(text)
    total = len(chunks)
    print(f"  {total} audio segments to generate\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_files = []

        for i, chunk in enumerate(chunks):
            pct = (i + 1) / total * 100
            bar = ("█" * int(pct / 5)).ljust(20)
            print(f"\r  [{bar}] {pct:5.1f}%  segment {i+1}/{total}", end="", flush=True)

            chunk_path = os.path.join(tmpdir, f"seg_{i:05d}.mp3")
            await chunk_to_mp3(chunk, chunk_path, voice, rate, pitch)
            chunk_files.append(chunk_path)

        print(f"\n\nMerging {total} segments...")

        list_file = os.path.join(tmpdir, "segments.txt")
        with open(list_file, "w") as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")

        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", output_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print("ffmpeg error:", result.stderr)
            sys.exit(1)

    size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    print(f"\nDone!  Saved to: {output_path}  ({size_mb:.1f} MB)\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def list_voices_cmd():
    voices = await edge_tts.list_voices()
    en_voices = [v for v in voices if v["Locale"].startswith("en-")]
    print(f"\n{'Name':<45} {'Gender':<8} {'Locale'}")
    print("-" * 65)
    for v in en_voices:
        print(f"{v['ShortName']:<45} {v['Gender']:<8} {v['Locale']}")
    print(f"\nTotal: {len(en_voices)} English voices\n")
    print("Shortcut names you can use with -v:")
    for name, full in VOICES.items():
        print(f"  {name:<10} → {full}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Convert ebooks/PDFs to audiobooks using Microsoft neural voices",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  python converter.py book.pdf
  python converter.py book.epub -v sonia
  python converter.py book.pdf -v en-US-GuyNeural -o my_audiobook.mp3
  python converter.py --list-voices
        """
    )
    parser.add_argument("input", nargs="?", help="Input file (PDF, EPUB, DOCX, TXT)")
    parser.add_argument("-o", "--output", help="Output MP3 path (default: <bookname>_audiobook.mp3)")
    parser.add_argument("-v", "--voice", default=DEFAULT_VOICE,
                        help=f"Voice name or shortcut (default: {DEFAULT_VOICE})\n"
                             f"Shortcuts: {', '.join(VOICES.keys())}")
    parser.add_argument("--rate", default="+0%",
                        help="Speaking rate, e.g. +10%% faster, -10%% slower (default: +0%%)")
    parser.add_argument("--pitch", default="+0Hz",
                        help="Pitch adjustment, e.g. +5Hz higher (default: +0Hz)")
    parser.add_argument("--list-voices", action="store_true",
                        help="List all available English voices and exit")

    args = parser.parse_args()

    if args.list_voices:
        asyncio.run(list_voices_cmd())
        return

    if not args.input:
        parser.print_help()
        return

    if not os.path.exists(args.input):
        print(f"Error: File not found: {args.input}")
        sys.exit(1)

    # Resolve voice shortcut
    voice = VOICES.get(args.voice.lower(), args.voice)

    output = args.output or (Path(args.input).stem + "_audiobook.mp3")

    asyncio.run(convert(args.input, output, voice, args.rate, args.pitch))


if __name__ == "__main__":
    main()
