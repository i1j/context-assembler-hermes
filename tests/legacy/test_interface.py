import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from plugins.context_engine.ca_assembler import is_available


def test_plugin_is_available_initially():
    assert is_available() is True
