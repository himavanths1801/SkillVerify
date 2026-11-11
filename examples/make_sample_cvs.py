"""Write the fictional sample CVs as real PDF and Word files, so the real formats can be tested.

Usage (from the project root):
    python examples/make_sample_cvs.py [--out DIR]
Reads examples/sample_cvs/*.txt and writes <name>.pdf and <name>.docx (default folder:
output/sample_cvs/). In both formats a GitHub URL is shown as just the word "GitHub", with the real
URL stored as a hyperlink, exactly like many real CVs. All content is fictional. Needs
`pip install -r requirements-cv.txt`.
"""
import argparse
import re
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = Path(__file__).resolve().parent / "sample_cvs"
DEFAULT_OUT = PROJECT_ROOT / "output" / "sample_cvs"
_GITHUB_URL = re.compile(r"(?:https?://)?github\.com/[^\s)|,;]+", re.IGNORECASE)
_WRAP = 92


def segments(line):
    """Split a line into (text, url) pieces: each github.com URL becomes ("GitHub", full URL)."""
    pieces, position = [], 0
    for match in _GITHUB_URL.finditer(line):
        shown = match.group(0).rstrip(".")  # a sentence-ending period is not part of the URL
        url = shown if shown.lower().startswith("http") else "https://" + shown
        if match.start() > position:
            pieces.append((line[position:match.start()], None))
        pieces.append(("GitHub", url))
        position = match.start() + len(shown)
    if position < len(line):
        pieces.append((line[position:], None))
    return pieces or [("", None)]


def write_pdf(lines, path):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path))
    size, left, y = 10, 50, 800
    for line in lines:
        for wrapped in textwrap.wrap(line, _WRAP) or [""]:
            if y < 50:
                pdf.showPage()
                y = 800
            x = left
            for text, url in segments(wrapped):
                pdf.setFont("Helvetica", size)
                pdf.drawString(x, y, text)
                width = pdf.stringWidth(text, "Helvetica", size)
                if url:
                    pdf.linkURL(url, (x, y - 2, x + width, y + size), relative=0)
                x += width
            y -= 14
    pdf.save()


def add_hyperlink(paragraph, text, url):
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    relationship = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relationship)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.append(underline)
    run.append(properties)
    content = OxmlElement("w:t")
    content.text = text
    run.append(content)
    link.append(run)
    paragraph._p.append(link)


def write_docx(lines, path):
    from docx import Document

    document = Document()
    for line in lines:
        paragraph = document.add_paragraph()
        for text, url in segments(line):
            if url:
                add_hyperlink(paragraph, text, url)
            else:
                paragraph.add_run(text)
    document.save(str(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write fictional sample CVs as PDF and DOCX.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output folder (default: output/sample_cvs)")
    args = parser.parse_args(argv)
    try:
        import docx  # noqa: F401
        import reportlab  # noqa: F401
    except ImportError:
        print("Missing libraries. Run: pip install -r requirements-cv.txt")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for source in sorted(SAMPLES_DIR.glob("*.txt")):
        lines = source.read_text(encoding="utf-8").splitlines()
        for writer, extension in ((write_pdf, ".pdf"), (write_docx, ".docx")):
            target = out / (source.stem + extension)
            writer(lines, target)
            print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
