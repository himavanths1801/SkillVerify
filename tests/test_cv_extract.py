"""Tests for CV file extraction (PDF, DOCX, TXT). Tiny files are built on the fly; every CV is fictional."""
import json
import zipfile

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from skillverify.cv import CVExtractError, extract_text
from skillverify.cv import extract as ex

CV_TEXT = "Jane Example\nData Analyst\nSkills: Python, SQL, Tableau\nGitHub: janedoe\n"


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def make_txt(path, text=CV_TEXT, encoding="utf-8"):
    path.write_bytes(text.encode(encoding))
    return path


def make_pdf(path, pages=(CV_TEXT,), link=None):
    pdf = canvas.Canvas(str(path))
    for number, text in enumerate(pages):
        y = 780
        for line in text.splitlines():
            pdf.drawString(50, y, line)
            y -= 14
        if link and number == 0:
            pdf.drawString(50, 700, "GitHub")
            pdf.linkURL(link, (50, 698, 90, 712), relative=0)
        pdf.showPage()
    pdf.save()
    return path


def add_hyperlink(paragraph, text, url):
    relationship = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relationship)
    run = OxmlElement("w:r")
    content = OxmlElement("w:t")
    content.text = text
    run.append(content)
    link.append(run)
    paragraph._p.append(link)


def make_docx(path, paragraphs=(CV_TEXT,), table=None, header=None, footer=None, link=None):
    document = Document()
    if header:
        document.sections[0].header.paragraphs[0].text = header
    if footer:
        document.sections[0].footer.paragraphs[0].text = footer
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    if link:
        add_hyperlink(document.add_paragraph("Code: "), "GitHub", link)
    document.save(str(path))
    return path


def code_of(path):
    with pytest.raises(CVExtractError) as caught:
        extract_text(path)
    return caught.value.code


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------
def test_txt(tmp_path):
    result = extract_text(make_txt(tmp_path / "cv.txt"))
    assert result == {"text": CV_TEXT.strip(), "links": [], "format": "txt", "pages": None,
                      "characters": len(CV_TEXT.strip()), "truncated": False}


def test_txt_encodings_and_cleaning(tmp_path):
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + CV_TEXT.encode())
    assert extract_text(bom)["text"] == CV_TEXT.strip()
    latin = make_txt(tmp_path / "latin.txt", "José García, Skills: Python, SQL and more text", "latin-1")
    assert "José" in extract_text(latin)["text"]
    messy = make_txt(tmp_path / "messy.txt", "Line one with enough text\x00\x07 here\r\n\r\n\r\n\r\nLine two  \t\nend of cv x")
    cleaned = extract_text(messy)["text"]
    assert "\x00" not in cleaned and "\x07" not in cleaned and "\r" not in cleaned
    assert "\n\n\n" not in cleaned and cleaned.count("\n\n") == 1 and "Line two\n" in cleaned
    assert ex.clean_text("a\tb\x0bc") == "a\tbc"


def test_pdf(tmp_path):
    result = extract_text(make_pdf(tmp_path / "cv.pdf", link="https://github.com/janedoe"))
    assert result["format"] == "pdf" and result["pages"] == 1 and not result["truncated"]
    assert "Data Analyst" in result["text"] and "GitHub" in result["text"]
    assert "github.com" not in result["text"]               # the URL is only in the link annotation
    assert result["links"] == ["https://github.com/janedoe"]


def test_pdf_link_failure_keeps_the_text(tmp_path, monkeypatch):
    def broken(page):
        raise ValueError("bad annotation")

    monkeypatch.setattr(ex, "_pdf_links", broken)
    result = extract_text(make_pdf(tmp_path / "cv.pdf", link="https://github.com/janedoe"))
    assert "Data Analyst" in result["text"] and result["links"] == []


class Obj(dict):
    def get_object(self):
        return self


class Arr(list):
    def get_object(self):
        return self


def test_pdf_links_helper_handles_odd_annotations():
    assert ex._pdf_links({}) == [] and ex._pdf_links({"/Annots": Arr()}) == []
    page = {"/Annots": Arr([
        Obj({"/Subtype": "/Text"}),                                  # no action
        Obj({"/A": Obj({"/S": "/GoTo"})}),                           # an action without a URI
        Obj({"/A": Obj({"/URI": 5})}),                               # a non-text URI
        Obj({"/A": Obj({"/URI": "https://github.com/janedoe"})}),
    ])}
    assert ex._pdf_links(page) == ["https://github.com/janedoe"]


def test_docx_paragraphs_tables_header_footer_and_links(tmp_path):
    path = make_docx(tmp_path / "cv.docx", ("Jane Example", "Skills: Python"),
                     table=[["Education", "B.Sc. Statistics, Lakeview University"], ["Projects", "Churn Predictor"]],
                     header="jane@example.com | +1 555 010 2345", footer="Page footer text", link="https://github.com/janedoe")
    result = extract_text(path)
    text = result["text"]
    assert result["format"] == "docx" and result["pages"] is None
    assert text.startswith("jane@example.com")                     # headers come first
    assert "Skills: Python" in text and "Education | B.Sc. Statistics, Lakeview University" in text
    assert "Projects | Churn Predictor" in text and text.endswith("Page footer text")
    assert "Code: GitHub" in text and result["links"] == ["https://github.com/janedoe"]
    assert text.index("Skills: Python") < text.index("Education |")  # document order


def test_docx_merged_cells_and_linked_headers_are_not_repeated(tmp_path):
    document = Document()
    document.sections[0].header.paragraphs[0].text = "Header once only"
    document.add_paragraph("Body text with enough words to count as a real CV page")
    table = document.add_table(rows=1, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Merged cell"
    table.cell(0, 2).text = "Other"
    document.add_section()
    document.add_paragraph("Second section text")
    path = tmp_path / "cv.docx"
    document.save(str(path))
    text = extract_text(path)["text"]
    assert text.count("Header once only") == 1 and text.count("Merged cell") == 1 and "Merged cell | Other" in text


# --------------------------------------------------------------------------
# refused files
# --------------------------------------------------------------------------
def test_unsupported_types(tmp_path):
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0 old word")
    assert code_of(tmp_path / "old.doc") == "unsupported_file"
    with pytest.raises(CVExtractError) as caught:
        extract_text(tmp_path / "old.doc")
    assert ".docx" in caught.value.message and "PDF" in caught.value.message
    for name in ("picture.png", "noextension", "cv.rtf"):
        (tmp_path / name).write_bytes(b"data")
        assert code_of(tmp_path / name) == "unsupported_file"


def test_extension_and_content_must_agree(tmp_path):
    (tmp_path / "fake.pdf").write_bytes(b"this is plain text, not a PDF")
    (tmp_path / "fake.docx").write_bytes(b"this is plain text, not a zip")
    assert code_of(tmp_path / "fake.pdf") == "unsupported_file" and code_of(tmp_path / "fake.docx") == "unsupported_file"
    make_pdf(tmp_path / "real.pdf")
    (tmp_path / "pdf_as.txt").write_bytes((tmp_path / "real.pdf").read_bytes())
    assert code_of(tmp_path / "pdf_as.txt") == "unsupported_file"
    make_docx(tmp_path / "real.docx")
    (tmp_path / "docx_as.txt").write_bytes((tmp_path / "real.docx").read_bytes())
    assert code_of(tmp_path / "docx_as.txt") == "unsupported_file"


def test_unreadable_files(tmp_path, monkeypatch):
    assert code_of(tmp_path / "missing.pdf") == "unreadable_file"
    (tmp_path / "folder.pdf").mkdir()
    assert code_of(tmp_path / "folder.pdf") == "unreadable_file"
    (tmp_path / "empty.txt").write_bytes(b"")
    assert code_of(tmp_path / "empty.txt") == "unreadable_file"
    make_txt(tmp_path / "ok.txt")

    def boom(self):
        raise OSError("denied")

    monkeypatch.setattr(type(tmp_path), "read_bytes", boom)
    assert code_of(tmp_path / "ok.txt") == "unreadable_file"


def test_corrupt_files(tmp_path):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4\nthis is not really a pdf at all, just garbage " * 3)
    assert code_of(tmp_path / "bad.pdf") == "unreadable_file"
    (tmp_path / "bad.docx").write_bytes(b"PK\x03\x04 not a real zip archive")
    assert code_of(tmp_path / "bad.docx") == "unreadable_file"
    with zipfile.ZipFile(tmp_path / "zip_only.docx", "w") as archive:
        archive.writestr("hello.txt", "just a zip, not a Word document")
    assert code_of(tmp_path / "zip_only.docx") == "unreadable_file"


def test_pypdf_log_noise_is_silenced(tmp_path, capfd):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4\nthis is not really a pdf at all, just garbage " * 3)
    assert code_of(tmp_path / "bad.pdf") == "unreadable_file"
    captured = capfd.readouterr()
    assert "EOF marker" not in captured.err and "EOF marker" not in captured.out


def test_encrypted_pdf(tmp_path):
    plain = make_pdf(tmp_path / "plain.pdf")
    writer = PdfWriter()
    writer.append(PdfReader(str(plain)))
    writer.encrypt(user_password="secret", owner_password="owner", algorithm="RC4-128")
    with open(tmp_path / "locked.pdf", "wb") as handle:
        writer.write(handle)
    assert code_of(tmp_path / "locked.pdf") == "encrypted_file"
    open_writer = PdfWriter()
    open_writer.append(PdfReader(str(plain)))
    open_writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")  # opens with an empty password
    with open(tmp_path / "openable.pdf", "wb") as handle:
        open_writer.write(handle)
    assert "Data Analyst" in extract_text(tmp_path / "openable.pdf")["text"]


def test_a_pdf_that_cannot_even_be_decrypted_is_reported_as_encrypted(tmp_path, monkeypatch):
    import pypdf

    def cannot(self, password):
        raise RuntimeError("missing crypto library")

    plain = make_pdf(tmp_path / "plain.pdf")
    writer = PdfWriter()
    writer.append(PdfReader(str(plain)))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    with open(tmp_path / "locked.pdf", "wb") as handle:
        writer.write(handle)
    monkeypatch.setattr(pypdf.PdfReader, "decrypt", cannot)
    assert code_of(tmp_path / "locked.pdf") == "encrypted_file"


def test_scanned_or_empty_cvs_have_no_text(tmp_path):
    pdf = canvas.Canvas(str(tmp_path / "scan.pdf"))
    pdf.rect(50, 50, 200, 200, fill=1)  # an "image": no text layer
    pdf.showPage()
    pdf.save()
    with pytest.raises(CVExtractError) as caught:
        extract_text(tmp_path / "scan.pdf")
    assert caught.value.code == "no_text" and "OCR" in caught.value.message
    make_txt(tmp_path / "tiny.txt", "Jane\n\n  \t Doe")
    assert code_of(tmp_path / "tiny.txt") == "no_text"
    make_docx(tmp_path / "blank.docx", ("", "   "))
    assert code_of(tmp_path / "blank.docx") == "no_text"


def test_too_large(tmp_path, monkeypatch):
    path = make_txt(tmp_path / "cv.txt")
    monkeypatch.setattr(ex, "MAX_FILE_BYTES", 10)
    assert code_of(path) == "too_large"


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------
def test_page_cap(tmp_path):
    pages = [f"Page marker {n:02d} with some text" for n in range(1, 23)]
    result = extract_text(make_pdf(tmp_path / "long.pdf", pages))
    assert result["pages"] == 22                       # the real page count is reported ...
    assert "Page marker 20" in result["text"] and "Page marker 21" not in result["text"]  # ... but only 20 are read


def test_docx_zip_bomb_guard(tmp_path, monkeypatch):
    path = make_docx(tmp_path / "cv.docx")
    monkeypatch.setattr(ex, "MAX_DOCX_UNCOMPRESSED_BYTES", 10)
    assert code_of(path) == "unreadable_file"


def test_truncation(tmp_path):
    long_text = "Python and SQL skills. " * 2000
    result = extract_text(make_txt(tmp_path / "long.txt", long_text))
    assert result["truncated"] is True and len(result["text"]) == ex.MAX_CV_CHARS == result["characters"]
    exact = extract_text(make_txt(tmp_path / "exact.txt", "x" * ex.MAX_CV_CHARS))
    assert exact["truncated"] is False


def test_links_are_deduplicated_cleaned_and_capped(monkeypatch):
    raw = ["https://a.example/x", "https://a.example/x", 5, None, "  https://b.example/\x00y  ", "", "https://c.example"]
    assert ex._unique_links(raw) == ["https://a.example/x", "https://b.example/y", "https://c.example"]
    monkeypatch.setattr(ex, "MAX_LINKS", 2)
    assert ex._unique_links([f"https://x.example/{n}" for n in range(10)]) == ["https://x.example/0", "https://x.example/1"]


def test_the_result_never_contains_the_file_path(tmp_path):
    folder = tmp_path / "secret_folder_name"
    folder.mkdir()
    path = make_pdf(folder / "jane_private_cv.pdf", link="https://github.com/janedoe")
    result = extract_text(path)
    dumped = json.dumps(result)
    assert "secret_folder_name" not in dumped and "jane_private_cv" not in dumped
    with pytest.raises(CVExtractError) as caught:
        extract_text(folder / "missing_private.pdf")
    assert "secret_folder_name" not in caught.value.message and "missing_private" not in caught.value.message


def test_the_path_can_be_a_string(tmp_path):
    assert extract_text(str(make_txt(tmp_path / "cv.txt")))["format"] == "txt"


def test_the_error_carries_a_code_and_a_message():
    error = CVExtractError("no_text", "nothing here")
    assert (error.code, error.message, str(error)) == ("no_text", "nothing here", "nothing here")
