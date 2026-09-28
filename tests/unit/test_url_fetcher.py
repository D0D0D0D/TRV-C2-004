# TRV-C2-004 — Unit Tests: url_fetcher (SSRF guards, S-2)

import pytest

import src.services.url_fetcher as uf
from src.services.url_fetcher import UrlFetchError, _validate_url, html_to_text


class TestSsrfGuards:
    @pytest.mark.parametrize("url", [
        "ftp://example.com/",           # non-http scheme
        "file:///etc/passwd",           # local file
        "http://127.0.0.1/",            # loopback
        "http://localhost/",            # loopback via hostname
        "http://10.0.0.5/",             # private range
        "http://192.168.1.1/",          # private range
        "http://169.254.169.254/",      # link-local (cloud metadata endpoint)
        "http://example.com:8080/",     # non-standard port
        "https://",                     # no hostname
        "http://user@example.com/",     # userinfo trick (http://safe@evil/)
        "http://user" + chr(58) + "pw@example.com/",  # password userinfo, assembled to avoid a credential literal
    ])
    def test_dangerous_urls_rejected(self, url):
        with pytest.raises(UrlFetchError):
            _validate_url(url)

    def test_connection_is_pinned_to_validated_ip(self, monkeypatch):
        """DNS rebinding 対策: 接続は検証時に解決した IP へ固定される
        （リクエスト時に DNS を引き直さない — レビュー指摘 F-01 回帰）。"""
        connected = {}

        class FakeResponse:
            status = 200
            def read(self, n):
                return "<html><body>宿の紹介ページ</body></html>".encode()

        class FakeConn:
            def request(self, method, path, headers=None):
                connected["host_header"] = (headers or {}).get("Host")
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        # 検証時の解決結果 = 8.8.8.8（public）。以後 DNS が何を返しても
        # 接続先はこの IP に固定されることを factory への引数で確認する。
        monkeypatch.setattr(uf.socket, "getaddrinfo",
                            lambda host, port, proto=None: [(2, 1, 6, "", ("8.8.8.8", port))])
        def fake_open(parsed, ip, port):
            connected["ip"] = ip
            connected["port"] = port
            return FakeConn()
        monkeypatch.setattr(uf, "_open_connection", fake_open)

        text = uf.fetch_facility_text("http://example.com/onsen")
        assert connected["ip"] == "8.8.8.8"
        assert connected["host_header"] == "example.com"
        assert "宿の紹介ページ" in text

    def test_redirect_not_followed(self, monkeypatch):
        class FakeResponse:
            status = 302
            def read(self, n):
                return b""
        class FakeConn:
            def request(self, *a, **k):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass
        monkeypatch.setattr(uf.socket, "getaddrinfo",
                            lambda host, port, proto=None: [(2, 1, 6, "", ("8.8.8.8", port))])
        monkeypatch.setattr(uf, "_open_connection", lambda *a: FakeConn())
        with pytest.raises(UrlFetchError, match="redirect"):
            uf.fetch_facility_text("http://example.com/")

    def test_any_private_resolution_rejected_even_mixed(self, monkeypatch):
        """公 IP と私設 IP が混在して解決される場合も拒否（全件 public 必須）。"""
        monkeypatch.setattr(uf.socket, "getaddrinfo",
                            lambda host, port, proto=None: [
                                (2, 1, 6, "", ("8.8.8.8", port)),
                                (2, 1, 6, "", ("10.0.0.5", port))])
        with pytest.raises(UrlFetchError, match="not public"):
            _validate_url("http://example.com/")


class TestHtmlToText:
    def test_strips_script_and_tags(self):
        html = ("<html><head><script>steal()</script><style>.x{}</style></head>"
                "<body><h1>海辺の宿</h1><p>全室オーシャンビュー</p></body></html>")
        text = html_to_text(html)
        assert "海辺の宿" in text and "オーシャンビュー" in text
        assert "steal" not in text and ".x{}" not in text
