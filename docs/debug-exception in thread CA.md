-Exception in thread CA-CStage-15:
Traceback (most recent call last):
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/__init__.py", line 277, in _run_c_stage
    self.cache.add_turn(turn_index, l0_text, l1_str, l0_emb, l1_emb)
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/cache.py", line 168, in add_turn
    self._submit_rebuild()
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/cache.py", line 188, in _submit_rebuild
    self._rebuild_future = self._rebuild_executor.submit(self._rebuild_if_dirty)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/i1j/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/concurrent/futures/thread.py", line 167, in submit
    raise RuntimeError('cannot schedule new futures after shutdown')
RuntimeError: cannot schedule new futures after shutdown

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/home/i1j/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/threading.py", line 1045, in _bootstrap_inner
    self.run()
  File "/home/i1j/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/threading.py", line 982, in run
    self._target(*self._args, **self._kwargs)
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/__init__.py", line 331, in _run_c_stage
    self.cache.add_turn(turn_index, "无有效增量", fallback, None, None)
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/cache.py", line 168, in add_turn
    self._submit_rebuild()
  File "/home/i1j/.hermes/profiles/tester/plugins/ca_assembler/ca/cache.py", line 188, in _submit_rebuild
    self._rebuild_future = self._rebuild_executor.submit(self._rebuild_if_dirty)
                           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/i1j/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/concurrent/futures/thread.py", line 167, in submit
    raise RuntimeError('cannot schedule new futures after shutdown')
RuntimeError: cannot schedule new futures after shutdown