"""Unit tests for the Infomed RCM scraper."""

from infomed.main import (
    load_progress,
    sanitize_filename,
    save_output_urls,
    save_progress,
)


def test_sanitize_filename():
    """Verify filename sanitization cleans invalid path characters."""
    assert sanitize_filename("4083_Dulcolax gotas") == "4083_Dulcolax_gotas"
    assert (
        sanitize_filename("603863_Evacol (solution/drops)")
        == "603863_Evacol_solution_drops"
    )
    assert sanitize_filename("   invalid/file\\name?:*  ") == "invalid_file_name"


def test_progress_save_and_load(tmp_path, monkeypatch):
    """Test saving and loading execution progress including downloaded files."""
    test_progress_file = str(tmp_path / "test_progress.json")
    monkeypatch.setattr("infomed.main.PROGRESS_FILE", test_progress_file)

    atcs = {"REF_ATC_1", "REF_ATC_2"}
    urls = {
        "https://extranet.infarmed.pt/doc1.pdf",
        "https://extranet.infarmed.pt/doc2.pdf",
    }
    downloaded_files = {"rcm_100.pdf", "rcm_101.pdf"}

    save_progress(atcs, urls, downloaded_files)

    loaded = load_progress()
    assert loaded["processed_atcs"] == atcs
    assert loaded["urls"] == urls
    assert loaded["downloaded_files"] == downloaded_files


def test_save_output_urls(tmp_path, monkeypatch):
    """Test saving output URLs to text file."""
    test_output_file = str(tmp_path / "test_urls.txt")
    monkeypatch.setattr("infomed.main.OUTPUT_FILE", test_output_file)

    urls = {"http://example.com/rcm1.pdf", "http://example.com/rcm2.pdf"}
    save_output_urls(urls)

    with open(test_output_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]

    assert sorted(lines) == sorted(list(urls))
