"""pytest configuration.

The v1 test modules are unittest-based, import removed dependencies (faiss, langchain, ...) and are
deleted together with the v1 module they cover (spec sez. 0.5). Until then they are not collected.
"""

collect_ignore = [
    "test_data_fetch.py",
    "test_data_preprocessing.py",
    "test_monitoring.py",
    "test_sentiment_analysis.py",
    "test_trading_logic.py",
    "test_vector_storage.py",
]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never read the developer's real ``.env`` (it holds a real API key)."""
    from modules.config import CoinbaseConfig

    monkeypatch.setitem(CoinbaseConfig.model_config, "env_file", None)
