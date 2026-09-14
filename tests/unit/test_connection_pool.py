"""
Unit tests for PostgreSQL ThreadedConnectionPool management in OrderManager.
"""

import pytest
from unittest.mock import patch, MagicMock
from execution.order_manager import OrderManager


def test_pool_initialization():
    """Verify that OrderManager initializes ThreadedConnectionPool with configured boundaries."""
    with patch("execution.order_manager.pool.ThreadedConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_pool.getconn.return_value = mock_conn
        mock_pool_cls.return_value = mock_pool

        om = OrderManager()
        assert om.db_pool is not None
        mock_pool_cls.assert_called_once()
        kwargs = mock_pool_cls.call_args[1]
        assert kwargs.get("minconn") == 1
        assert kwargs.get("maxconn") == 10


def test_connection_context_manager_lifecycle():
    """Verify that _get_connection acquires and safely returns connection to pool."""
    with patch("execution.order_manager.pool.ThreadedConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_pool.getconn.return_value = mock_conn
        mock_pool_cls.return_value = mock_pool

        om = OrderManager()
        mock_pool.reset_mock()

        with om._get_connection() as conn:
            assert conn == mock_conn
            mock_pool.getconn.assert_called_once()
            # putconn not called yet while in context
            mock_pool.putconn.assert_not_called()

        # putconn called once context exits
        mock_pool.putconn.assert_called_once_with(mock_conn)


def test_connection_rollback_on_error():
    """Verify that connection rolls back on unhandled error and returns to pool."""
    with patch("execution.order_manager.pool.ThreadedConnectionPool") as mock_pool_cls:
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_pool.getconn.return_value = mock_conn
        mock_pool_cls.return_value = mock_pool

        om = OrderManager()
        mock_pool.reset_mock()
        mock_conn.reset_mock()

        with pytest.raises(ValueError):
            with om._get_connection():
                raise ValueError("Simulated DB error")

        mock_conn.rollback.assert_called_once()
        mock_pool.putconn.assert_called_once_with(mock_conn)
