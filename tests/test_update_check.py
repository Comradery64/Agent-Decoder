"""Tests for update_check.py — the PyPI-based version check backing the
About panel's update banner."""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from agent_decoder.models import AppConfig
from agent_decoder.update_check import fetch_latest_version, is_newer


class TestIsNewer:
    def test_newer_patch_version(self):
        assert is_newer("0.3.2", "0.3.1.3") is True

    def test_same_version_is_not_newer(self):
        assert is_newer("0.3.1.3", "0.3.1.3") is False

    def test_older_version_is_not_newer(self):
        assert is_newer("0.3.0", "0.3.1.3") is False

    def test_unparsable_latest_fails_closed(self):
        assert is_newer("not-a-version", "0.3.1.3") is False

    def test_unparsable_current_fails_closed(self):
        assert is_newer("0.3.2", "not-a-version") is False


class TestFetchLatestVersion:
    async def test_returns_version_on_success(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"info": {"version": "0.4.0"}})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await fetch_latest_version()

        assert result == "0.4.0"

    async def test_returns_none_on_request_error(self):
        import httpx as _httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=_httpx.RequestError("timeout"))

            result = await fetch_latest_version()

        assert result is None

    async def test_returns_none_on_http_error_status(self):
        import httpx as _httpx

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=_httpx.HTTPStatusError(
                "404", request=MagicMock(), response=MagicMock(status_code=404)
            )
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await fetch_latest_version()

        assert result is None

    async def test_returns_none_on_malformed_json(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"unexpected": "shape"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=mock_client
            )
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)

            result = await fetch_latest_version()

        assert result is None


class TestUpdateCheckEndpoint:
    def _make_app(self, tmp_path):
        from agent_decoder.server import create_app

        return create_app(AppConfig(), tmp_path / "test.db")

    async def test_reports_update_available(self, tmp_path, monkeypatch):
        from agent_decoder import __version__

        monkeypatch.setattr(
            "agent_decoder.server.fetch_latest_version",
            AsyncMock(return_value="999.0.0"),
        )
        app = self._make_app(tmp_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/api/update-check")
        assert r.status_code == 200
        body = r.json()
        assert body["current_version"] == __version__
        assert body["latest_version"] == "999.0.0"
        assert body["update_available"] is True

    async def test_reports_no_update_when_current(self, tmp_path, monkeypatch):
        from agent_decoder import __version__

        monkeypatch.setattr(
            "agent_decoder.server.fetch_latest_version",
            AsyncMock(return_value=__version__),
        )
        app = self._make_app(tmp_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/api/update-check")
        assert r.json()["update_available"] is False

    async def test_reports_no_update_on_fetch_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agent_decoder.server.fetch_latest_version",
            AsyncMock(return_value=None),
        )
        app = self._make_app(tmp_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.get("/api/update-check")
        assert r.status_code == 200
        assert r.json()["update_available"] is False

    async def test_caches_result_across_requests(self, tmp_path, monkeypatch):
        """A second request within the cache window must not call
        fetch_latest_version again — PyPI-friendliness is the whole point."""
        mock_fetch = AsyncMock(return_value="999.0.0")
        monkeypatch.setattr("agent_decoder.server.fetch_latest_version", mock_fetch)
        app = self._make_app(tmp_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r1 = await c.get("/api/update-check")
            r2 = await c.get("/api/update-check")
        assert r1.json() == r2.json()
        mock_fetch.assert_called_once()
