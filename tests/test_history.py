import sys
import types
import warnings

from advisor import history


def test_yfinance_warnings_are_hidden_but_ours_are_not(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", types.ModuleType("yfinance"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        history._yfinance()
        exec(compile('import warnings\nwarnings.warn("from yfinance", DeprecationWarning)', "utils.py", "exec"),
             {"__name__": "yfinance.utils"})
        exec(compile('import warnings\nwarnings.warn("from advisor", DeprecationWarning)', "x.py", "exec"),
             {"__name__": "advisor.x"})
    assert [str(w.message) for w in caught] == ["from advisor"]
