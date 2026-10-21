import pytest

import graph


class FakeBound:
    def __init__(self, fail):
        self.fail = fail

    def invoke(self, prompt):
        if self.fail:
            raise RuntimeError("tool_use_failed")
        return "ok"


class FakeLLM:
    def __init__(self, fail_methods):
        self.fail_methods = fail_methods
        self.calls = []

    def with_structured_output(self, schema, method=None):
        self.calls.append(method)
        return FakeBound(method in self.fail_methods)


def test_falls_back_when_first_method_fails(monkeypatch):
    llm = FakeLLM({"json_schema"})
    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    assert graph.structured_invoke(object, "hi") == "ok"
    assert llm.calls == ["json_schema", "function_calling"]


def test_raises_after_all_attempts(monkeypatch):
    llm = FakeLLM({"json_schema", "function_calling"})
    monkeypatch.setattr(graph, "get_llm", lambda: llm)
    with pytest.raises(RuntimeError):
        graph.structured_invoke(object, "hi")
    assert len(llm.calls) == 3
