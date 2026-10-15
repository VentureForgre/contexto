import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Fallback until lane A's contexto.model is on main; remove together with _model_stub.py.
if importlib.util.find_spec("contexto.model") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _model_stub

    sys.modules["contexto.model"] = _model_stub
