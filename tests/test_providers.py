from app.providers import registry, url_hash


def test_recognizes_baidu_and_normalizes_tracking():
    parsed = registry.recognize("http://pan.baidu.com/s/abc123/?utm_source=x&pwd=8k2a")
    assert parsed.provider_code == "baidu"
    assert parsed.share_id == "abc123"
    assert parsed.normalized_url == "https://pan.baidu.com/s/abc123?pwd=8k2a"


def test_quark_link_recognition():
    parsed = registry.recognize("https://pan.quark.cn/s/qwerty")
    assert parsed.provider_code == "quark"
    assert parsed.share_id == "qwerty"


def test_duplicate_identity_ignores_extract_code():
    first = url_hash("https://pan.baidu.com/s/abc123?pwd=1111")
    second = url_hash("https://pan.baidu.com/s/abc123?pwd=2222")
    assert first == second


def test_rejects_unsupported_provider():
    try:
        registry.recognize("https://example.com/share/a")
    except ValueError as exc:
        assert "暂不支持" in str(exc)
    else:
        raise AssertionError("应拒绝未配置网盘")
