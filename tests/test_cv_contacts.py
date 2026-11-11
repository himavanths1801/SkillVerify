"""Tests for deterministic email, phone and GitHub extraction and for contact redaction. All data is fictional."""
import pytest

from skillverify.cv import find_emails, find_github, find_phones
from skillverify.cv.contacts import LABEL_STOPWORDS, MAX_OTHER_LINKS, MAX_REPOS_MENTIONED, RESERVED_PATHS, redact_contacts


# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------
def test_emails_are_lowercased_and_deduplicated():
    text = "Mail: Jane.Doe@Example.com, or jane.doe@example.com; also j+cv@mail.example.co.in."
    assert find_emails(text) == ["jane.doe@example.com", "j+cv@mail.example.co.in"]


def test_emails_from_mailto_links():
    links = ["mailto:Jane@Example.com?subject=Hi", "mailto:not an address", "https://example.com", 5, "MAILTO:b@example.com"]
    assert find_emails("Contact: a@example.com", links) == ["a@example.com", "jane@example.com", "b@example.com"]


def test_no_emails_and_the_ssh_clone_form_is_not_an_email():
    assert find_emails("no address here") == []
    assert find_emails("clone git@github.com:janedoe/repo.git") == []


# --------------------------------------------------------------------------
# phone
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, normalized", [
    ("+91 98765 43210", "+919876543210"),
    ("98765-43210", "9876543210"),
    ("(080) 1234 5678", "08012345678"),
    ("+1 (555) 010-2345", "+15550102345"),
    ("555.010.2345", "5550102345"),
    ("9876543210", "9876543210"),
    ("Phone:9876543210", "9876543210"),
    ("tel +44 20 7946 0958", "+442079460958"),
])
def test_phone_styles(text, normalized):
    phones = find_phones(f"Call me: {text} (evenings)")
    assert [p["normalized"] for p in phones] == [normalized]
    assert text in phones[0]["as_written"] or phones[0]["as_written"] in text


@pytest.mark.parametrize("text", [
    "2021 - 2025", "2021-2025", "B.Tech (2019 2023)", "Class XII: 87.4%", "CGPA 8.7 / 10", "version 3.11.2",
    "v1.2.3.4.5.6.7.8", "build 1.2.3.4.5.6.7.8 done", "15-08-2023", "2023-08-15", "https://example.com/id/919876543210",
    "https://example.com/?id=9876543210", "order 12345", "1234567", "1234567890123456789",
    "Roll 123456789", "Pin 560001", "id-9876543210",
])
def test_things_that_are_not_phones(text):
    assert find_phones(text) == []


def test_phones_are_deduplicated_and_ordered():
    phones = find_phones("A: +91 98765 43210 and again +91-98765-43210. Office (080) 1234 5678.")
    assert [p["normalized"] for p in phones] == ["+919876543210", "08012345678"]


def test_phone_digit_limits():
    assert find_phones("+12 3456 789") != []          # 9 digits
    assert find_phones("+1 234 567") == []             # 7 digits
    assert find_phones("+12 3456 7890 1234 5") != []   # 15 digits
    assert find_phones("+12 3456 7890 1234 56") == []  # 16 digits


def test_phones_from_tel_links():
    phones = find_phones("no number in the text", ["tel:+91 98765 43210", "tel:12", "https://example.com", 7, "TEL:5550102345"])
    assert [p["normalized"] for p in phones] == ["+919876543210", "5550102345"]
    assert find_phones("+91 98765 43210", ["tel:+919876543210"])[0]["as_written"] == "+91 98765 43210"  # text first


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------
def test_redaction_replaces_every_email_and_phone():
    text = "Jane (Jane@Example.com / jane@example.com) +91 98765 43210, office 555.010.2345. Skills: Python"
    emails, phones = find_emails(text), find_phones(text)
    redacted = redact_contacts(text, emails, phones)
    assert redacted == "Jane ([EMAIL] / [EMAIL]) [PHONE], office [PHONE]. Skills: Python"
    assert "example.com" not in redacted and "98765" not in redacted and "010" not in redacted


def test_redaction_without_contacts_changes_nothing():
    assert redact_contacts("Skills: Python", [], []) == "Skills: Python"


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def gh(text, links=None):
    return find_github(text, links)


@pytest.mark.parametrize("text", [
    "https://github.com/janedoe", "http://github.com/janedoe", "github.com/janedoe", "www.github.com/janedoe",
    "https://www.github.com/janedoe/", "(github.com/janedoe)", "See github.com/janedoe.", "github.com/janedoe,",
    "github.com/janedoe?tab=repositories", "github.com/janedoe#readme", "GITHUB.COM/JaneDoe", "<https://github.com/JaneDoe>",
])
def test_profile_urls(text):
    result = gh(f"Code: {text} thanks")
    assert (result["status"], result["username"], result["source"]) == ("found", "janedoe", "text")
    assert result["profile_url"] == "https://github.com/janedoe" and result["repos_mentioned"] == []
    assert result["candidates"] == ["janedoe"]


def test_repo_urls_record_the_repo():
    result = gh("Built it: https://github.com/JaneDoe/churn-predictor.git and github.com/janedoe/dash-board/")
    assert result["username"] == "janedoe" and result["repos_mentioned"] == ["churn-predictor", "dash-board"]
    assert gh("github.com/janedoe/a, github.com/janedoe/a")["repos_mentioned"] == ["a"]
    many = " ".join(f"github.com/janedoe/repo{n}" for n in range(30))
    assert len(gh(many)["repos_mentioned"]) == MAX_REPOS_MENTIONED == 20
    assert gh("github.com/janedoe/bad repo!!")["repos_mentioned"] == ["bad"]
    assert gh("github.com/janedoe/***")["repos_mentioned"] == []


def test_ssh_form():
    result = gh("git clone git@github.com:JaneDoe/project.git")
    assert result["username"] == "janedoe" and result["repos_mentioned"] == ["project"]


@pytest.mark.parametrize("text", [
    "github.com/about", "github.com/features/copilot", "https://github.com/settings/profile", "github.com/orgs/acme/people",
    "github.com/marketplace", "github.com/sponsors/janedoe", "github.com/login", "github.com/actions/checkout",
    "github.com/topics/python", "github.com/-bad", "github.com/bad_name", "github.com/" + "a" * 40,
    "gist.github.com/janedoe/abc123", "janedoe.github.io", "https://janedoe.github.io/portfolio",
    "mygithub.com/janedoe", "notgithub.com/janedoe", "api.github.com/users/janedoe",
])
def test_not_a_username(text):
    assert gh(f"Links: {text} end")["username"] is None


def test_every_reserved_path_is_skipped():
    for word in RESERVED_PATHS:
        assert gh(f"github.com/{word}")["status"] == "none", word


def test_other_links_gist_and_pages():
    text = "Site: https://janedoe.github.io/portfolio, gist: https://gist.github.com/janedoe/abc123. Also janedoe.github.io."
    result = gh(text)
    assert result["status"] == "none" and result["username"] is None
    assert result["other_links"] == ["https://gist.github.com/janedoe/abc123", "https://janedoe.github.io/portfolio",
                                     "janedoe.github.io"]  # gists first, then Pages sites, each in document order
    capped = gh(" ".join(f"https://user{n}.github.io" for n in range(40)))
    assert len(capped["other_links"]) == MAX_OTHER_LINKS


@pytest.mark.parametrize("text", [
    "GitHub: janedoe", "GitHub : janedoe", "github - @janedoe", "GitHub | janedoe", "GitHub @janedoe", "GitHub – janedoe",
    "GitHub profile: janedoe", "GitHub username - janedoe", "GITHUB:@JaneDoe", "GitHub: janedoe | LinkedIn: jane",
    "GitHub: janedoe, Kaggle: jd", "Links (GitHub: janedoe)",
])
def test_label_forms(text):
    result = gh(text)
    assert (result["username"], result["source"], result["status"]) == ("janedoe", "label", "found")


@pytest.mark.parametrize("text", [
    "GitHub Actions experience", "Experienced with GitHub and GitLab", "GitHub - Version control tools",
    "GitHub: GitHub | LinkedIn: jane", "GitHub: link", "GitHub: profile", "GitHub: github.com/other/x ok",
    "GitHub:", "GitHub - Actions", "using GitHub every day", "GitHub: janedoe/portfolio",
])
def test_things_that_are_not_labels(text):
    result = gh(text)
    assert result["username"] in (None, "other"), text   # the url form in one example is a real (repo) url
    if result["username"] is None:
        assert result["status"] == "none"


def test_label_stopwords_are_lowercase():
    assert all(word == word.lower() for word in LABEL_STOPWORDS) and "github" in LABEL_STOPWORDS


def test_link_annotations_are_the_source_when_the_text_has_no_url():
    result = gh("Code: GitHub\nWeb: Portfolio", ["https://github.com/JaneDoe", "https://example.com"])
    assert (result["username"], result["source"], result["status"]) == ("janedoe", "link", "found")
    assert result["profile_url"] == "https://github.com/janedoe"


def test_a_link_beats_text_for_the_same_user():
    result = gh("see github.com/janedoe", ["https://github.com/janedoe"])
    assert result["source"] == "link" and result["candidates"] == ["janedoe"] and result["status"] == "found"


def test_link_ssh_urls_and_repos_in_links():
    result = gh("no url in text", ["git@github.com:janedoe/tool.git", "https://github.com/janedoe/other-tool"])
    assert result["username"] == "janedoe" and result["repos_mentioned"] == ["tool", "other-tool"]
    assert gh("x", [5, None])["status"] == "none"


def test_ambiguous_profile_beats_repo_url():
    result = gh("My code: github.com/janedoe\nForked github.com/someone-else/cool-lib for a fix, github.com/someone-else/cool-lib")
    assert result["status"] == "ambiguous" and result["username"] == "janedoe"
    assert result["candidates"] == ["janedoe", "someone-else"] and result["repos_mentioned"] == []
    flipped = gh("Forked github.com/someone-else/cool-lib, github.com/someone-else/other\nMy code: github.com/janedoe")
    assert flipped["username"] == "janedoe"  # profile-only wins over more repo mentions


def test_ambiguous_more_mentions_then_first_in_document():
    most = gh("github.com/aaa-user then github.com/bbb-user and github.com/bbb-user again")
    assert most["username"] == "bbb-user" and most["candidates"] == ["aaa-user", "bbb-user"]
    first = gh("github.com/aaa-user then github.com/bbb-user")
    assert first["username"] == "aaa-user" and first["status"] == "ambiguous"
    repos = gh("github.com/aaa-user/r1 and github.com/bbb-user/r2")
    assert repos["username"] == "aaa-user" and repos["repos_mentioned"] == ["r1"]


def test_the_label_counts_as_a_profile_mention():
    result = gh("Forked github.com/someone-else/lib\nGitHub: janedoe")
    assert result["username"] == "janedoe" and result["source"] == "label"


def test_none():
    assert gh("") == {"status": "none", "username": None, "profile_url": None, "source": None,
                      "repos_mentioned": [], "candidates": [], "other_links": []}
    assert gh("No code links here, just GitHub Actions.")["status"] == "none"


def test_invalid_usernames_are_ignored_but_valid_ones_still_found():
    result = gh("github.com/not_valid and github.com/good-one")
    assert result["username"] == "good-one" and result["candidates"] == ["good-one"] and result["status"] == "found"
