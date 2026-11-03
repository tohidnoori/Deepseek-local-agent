from agent.controller import AgentController


PARSE_SAMPLES = [
    '{"tool_calls": [{"name": "list_files", "arguments": {"path": "."}}], "message": "hi"}',
    '```json\n{"tool_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]}\n```',
    'blah json / Copy / Download\n{"tool_calls": [{"name": "tree", "arguments": {}}], "message": "ok"}',
]


def test_parse_variants():
    ctrl = AgentController.__new__(AgentController)
    for s in PARSE_SAMPLES:
        parsed = ctrl._parse(s)
        assert parsed is not None
        assert isinstance(parsed["tool_calls"], list)


def test_parse_rejects_junk():
    ctrl = AgentController.__new__(AgentController)
    assert ctrl._parse("no json here") is None
    assert ctrl._parse("{\"not\": \"tools\"}") is None
