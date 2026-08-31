from app.services.text import clean_isbn, normalize_title, slugify


def test_title_normalization_is_conservative():
    assert normalize_title("《Python 编程入门》.PDF") == "python 编程入门"
    assert normalize_title("  乡土   中国  ") == "乡土 中国"


def test_isbn_cleanup():
    assert clean_isbn("978-7-111-11111-1") == "9787111111111"


def test_slug_keeps_chinese_and_ascii():
    assert slugify("Python 编程入门") == "python-编程入门"
