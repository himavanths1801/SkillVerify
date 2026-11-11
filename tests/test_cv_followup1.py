"""Step 4 follow-up 1: a guessed 'GitHub | LinkedIn' label must never beat or join a real github.com link. Offline."""
import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from reportlab.pdfgen import canvas

from skillverify.cv import extract_text, find_github
from skillverify.cv.contacts import LABEL_STOPWORDS, RESERVED_PATHS

REQUIRED_STOPWORDS = (
    "github link profile account username user handle url website site portfolio linkedin twitter x facebook "
    "instagram youtube medium kaggle leetcode hackerrank hackerearth codeforces codechef geeksforgeeks gitlab "
    "bitbucket stackoverflow behance dribbble email mail phone mobile contact resume cv projects repositories "
    "repos none na n/a").split()


# --------------------------------------------------------------------------
# the reported bug
# --------------------------------------------------------------------------
def test_linkedin_after_a_github_link_is_not_a_username():
    result = find_github("GitHub | LinkedIn", ["https://github.com/ghost"])
    assert (result["status"], result["username"], result["source"]) == ("found", "ghost", "link")
    assert result["candidates"] == ["ghost"] and result["profile_url"] == "https://github.com/ghost"


def test_the_same_header_without_any_link_finds_nothing():
    result = find_github("GitHub | LinkedIn")
    assert (result["status"], result["username"], result["source"]) == ("none", None, None)
    assert result["candidates"] == []


def make_pdf_with_hidden_link(path):
    pdf = canvas.Canvas(str(path))
    pdf.drawString(50, 780, "Jane Example, Data Analyst. Skills: Python and SQL used in many data projects.")
    pdf.drawString(50, 760, "GitHub | LinkedIn")
    pdf.linkURL("https://github.com/ghost", (50, 758, 90, 772), relative=0)  # only the word "GitHub" is linked
    pdf.showPage()
    pdf.save()


def make_docx_with_hidden_link(path):
    document = Document()
    document.add_paragraph("Jane Example, Data Analyst. Skills: Python and SQL used in many data projects.")
    paragraph = document.add_paragraph()
    relationship = paragraph.part.relate_to("https://github.com/ghost", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relationship)
    run = OxmlElement("w:r")
    content = OxmlElement("w:t")
    content.text = "GitHub"
    run.append(content)
    link.append(run)
    paragraph._p.append(link)
    paragraph.add_run(" | LinkedIn")
    document.save(str(path))


@pytest.mark.parametrize("make, name", [(make_pdf_with_hidden_link, "cv.pdf"), (make_docx_with_hidden_link, "cv.docx")])
def test_real_files_with_a_hidden_github_link(tmp_path, make, name):
    make(tmp_path / name)
    extracted = extract_text(tmp_path / name)
    assert "GitHub | LinkedIn" in extracted["text"] and "github.com" not in extracted["text"]
    assert extracted["links"] == ["https://github.com/ghost"]
    result = find_github(extracted["text"], extracted["links"])
    assert (result["status"], result["username"], result["source"]) == ("found", "ghost", "link")
    assert result["candidates"] == ["ghost"]


# --------------------------------------------------------------------------
# label forms that stay valid
# --------------------------------------------------------------------------
def test_a_label_followed_by_another_sites_label_is_still_read():
    result = find_github("GitHub: octocat | LinkedIn: priya-sample")
    assert (result["status"], result["username"], result["source"]) == ("found", "octocat", "label")
    assert result["candidates"] == ["octocat"]


@pytest.mark.parametrize("text", ["GitHub - @octocat", "GitHub | octocat", "GitHub: octocat", "github @octocat"])
def test_labels_without_any_url_are_still_accepted(text):
    result = find_github(text)
    assert (result["status"], result["username"], result["source"]) == ("found", "octocat", "label")


def test_two_distinct_weak_only_candidates_are_ambiguous():
    result = find_github("GitHub: aaa-user\nGitHub - @bbb-user")
    assert result["status"] == "ambiguous" and result["candidates"] == ["aaa-user", "bbb-user"]
    assert result["username"] == "aaa-user" and result["source"] == "label"


# --------------------------------------------------------------------------
# strong beats weak
# --------------------------------------------------------------------------
def test_a_profile_link_beats_a_label():
    result = find_github("GitHub: someone-else", ["https://github.com/octocat"])
    assert (result["status"], result["username"], result["source"]) == ("found", "octocat", "link")
    assert result["candidates"] == ["octocat"]  # the weak label is not even listed


def test_a_profile_url_in_the_text_beats_a_label():
    result = find_github("GitHub: someone-else\nCode: github.com/octocat")
    assert (result["status"], result["username"], result["source"]) == ("found", "octocat", "text")
    assert result["candidates"] == ["octocat"]


def test_a_label_for_the_same_user_changes_nothing():
    result = find_github("GitHub: octocat", ["https://github.com/OctoCat"])
    assert result["username"] == "octocat" and result["source"] == "link" and result["candidates"] == ["octocat"]


def test_a_repo_only_url_is_not_a_profile_claim_and_does_not_silence_an_explicit_label():
    result = find_github("Forked github.com/someone-else/lib\nGitHub: octocat")
    assert result["username"] == "octocat" and result["source"] == "label"
    assert result["candidates"] == ["someone-else", "octocat"]  # in document order
    assert result["status"] == "ambiguous" and result["repos_mentioned"] == []


def test_strong_candidates_are_still_ambiguous_with_the_existing_ranking():
    profile_beats_repo = find_github("Forked github.com/aaa-user/lib\nMine: github.com/bbb-user", ["GitHub: nobody"])
    assert profile_beats_repo["status"] == "ambiguous" and profile_beats_repo["username"] == "bbb-user"
    assert profile_beats_repo["candidates"] == ["aaa-user", "bbb-user"]
    more_mentions = find_github("github.com/aaa-user then github.com/bbb-user and github.com/bbb-user again")
    assert more_mentions["username"] == "bbb-user" and more_mentions["status"] == "ambiguous"
    first_in_document = find_github("github.com/aaa-user then github.com/bbb-user")
    assert first_in_document["username"] == "aaa-user" and first_in_document["candidates"] == ["aaa-user", "bbb-user"]
    with_label = find_github("GitHub: label-user\ngithub.com/aaa-user and github.com/bbb-user")
    assert with_label["candidates"] == ["aaa-user", "bbb-user"] and "label-user" not in with_label["candidates"]


# --------------------------------------------------------------------------
# the stop-list and the "next field" rule
# --------------------------------------------------------------------------
def test_every_required_word_is_in_the_stop_list_and_the_old_ones_are_kept():
    assert set(REQUIRED_STOPWORDS) <= LABEL_STOPWORDS
    assert {"github", "link", "profile", "url", "here", "website", "page", "account", "repo", "repository",
            "repositories"} <= LABEL_STOPWORDS
    assert all(word == word.lower() for word in LABEL_STOPWORDS)


@pytest.mark.parametrize("word", ["linkedin", "LinkedIn", "TWITTER", "leetcode", "Portfolio", "email", "Gitlab", "n/a",
                                  "N/A", "na", "x", "X", "cv", "resume", "kaggle", "projects", "none", "user", "mobile"])
@pytest.mark.parametrize("separator", [": ", " | ", " - ", " @"])
def test_stop_listed_words_after_a_github_label_are_rejected(word, separator):
    result = find_github(f"GitHub{separator}{word}")
    assert result["username"] is None and result["status"] == "none" and result["candidates"] == []


@pytest.mark.parametrize("word", sorted(REQUIRED_STOPWORDS))
def test_each_stop_list_word_is_rejected(word):
    assert find_github(f"GitHub: {word}")["username"] is None


@pytest.mark.parametrize("text", [
    "GitHub | Twitter.com", "GitHub | LinkedIn: linkedin.com/in/x", "GitHub | Medium.com/@x", "GitHub: octocat.com",
    "GitHub | CodeChef : my-handle", "GitHub | Mystery: x", "GitHub | Mystery : x", "GitHub | Mystery.com",
])
def test_a_token_that_is_the_label_of_the_next_field_is_rejected(text):
    assert find_github(text)["username"] is None


def test_the_next_field_rule_applies_even_when_the_word_is_not_a_known_site():
    assert find_github("GitHub | SomeNewSite: priya")["username"] is None
    assert find_github("GitHub | octocat: x")["username"] is None
    assert find_github("GitHub | octocat | SomeNewSite: priya")["username"] == "octocat"


@pytest.mark.parametrize("text", ["GitHub Actions experience", "Experienced with GitHub and GitLab", "GitHub Actions",
                                  "GitHub - Version control tools", "GitHub:", "using GitHub every day"])
def test_plain_mentions_still_give_nothing(text):
    assert find_github(text)["username"] is None


def test_validation_and_lowercasing_still_apply_to_labels():
    assert find_github("GitHub: Octo-Cat")["username"] == "octo-cat"
    assert find_github("GitHub: not_valid")["username"] is None
    assert find_github("GitHub: " + "a" * 40)["username"] is None
    for word in RESERVED_PATHS:
        assert find_github(f"GitHub: {word}")["username"] is None, word


def test_the_result_shape_is_unchanged():
    result = find_github("GitHub | LinkedIn", ["https://github.com/ghost/portfolio"])
    assert set(result) == {"status", "username", "profile_url", "source", "repos_mentioned", "candidates", "other_links"}
    assert result["repos_mentioned"] == ["portfolio"] and result["source"] == "link"
