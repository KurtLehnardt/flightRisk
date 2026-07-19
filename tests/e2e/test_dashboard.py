"""E2E tests for the Flask dashboard.

Tests route registration without triggering the full pipeline.
"""

import pytest

pytestmark = pytest.mark.e2e


class TestDashboardRoutes:
    """Test Flask routes without full pipeline init."""

    def test_app_import(self):
        """App module can be imported."""
        from amber.dashboard.app import app
        assert app is not None

    def test_index_route_exists(self):
        from amber.dashboard.app import app
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        assert "/" in rules

    def test_status_route_exists(self):
        from amber.dashboard.app import app
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        assert "/api/status" in rules

    def test_static_route_exists(self):
        from amber.dashboard.app import app
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        # Flask always registers /static/<path:filename>
        assert any("/static" in r for r in rules)

    def test_app_has_secret_key(self):
        from amber.dashboard.app import app
        assert app.config["SECRET_KEY"] is not None

    def test_socketio_imported(self):
        from amber.dashboard.app import socketio
        assert socketio is not None
