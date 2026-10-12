"""M2: the MCP catalog — every tool the model can call, inside a budget.

One dispatcher (``mcp_call``) replaces one function schema per remote tool.
The R7d measurement is why: 13 tools cost 13,301 characters *per step*, every
step (docs/features.md 538-541).  The catalog is that cost's replacement, so it
is built bounded here, and its caller regenerates it per step from live state
(agent_loop's volatile tail), never freezes it into a schema.

OpenCode bounds the same thing at INLINE_BUDGET = 2,000 tokens
(packages/core/src/codemode/catalog.ts:50); ~4 chars per token is 8,000.
"""

from types import SimpleNamespace

from ai_coding.core.mcp import build_mcp_catalog


def _tool(name="read_file", description="does a thing"):
    return SimpleNamespace(name=name, description=description)


def test_a_server_and_its_tools_are_listed_by_name():
    text = build_mcp_catalog([("fs", [_tool("read_file"), _tool("write_file")])])

    assert "fs" in text
    assert "read_file" in text
    assert "write_file" in text


def test_nothing_connected_is_an_empty_catalog():
    assert build_mcp_catalog([]) == ""


def test_a_long_description_is_cut_rather_than_dropping_the_tool():
    text = build_mcp_catalog([("fs", [_tool("t", description="y" * 500)])])

    assert "y" * 500 not in text
    assert len(text) < 400


def test_the_budget_keeps_configured_servers_and_drops_the_rest_loudly():
    # Six servers of fifteen verbose tools each: more than 8,000 characters of
    # catalog.
    #
    # M6 changed the allocation policy, and with it two assertions below.  It
    # used to fill servers in config order and drop whole trailing ones, which
    # meant a connected, working server could be invisible to the model behind
    # a panel row reading "Connected ✓" — the operator's work simply not
    # existing for the turn.  The budget is now shared round-robin, so a server
    # gets a tool before another gets its second.  What must still be true is
    # that the budget holds, that every server is represented, and that
    # whatever could not be shown is said.
    servers = [
        (f"s{i}", [_tool(f"t{j}", description="z" * 100) for j in range(15)])
        for i in range(6)
    ]

    text = build_mcp_catalog(servers)

    assert len(text) <= 8_000
    assert "s0" in text
    # Round-robin: the last server listed is no longer the one that loses.
    assert "s5" in text
    # ...and the budget still binds: 6 x 15 tools cannot all fit in 8,000.
    listed = sum(1 for line in text.splitlines() if line.startswith("  t"))
    assert listed < 90


# ---- M6: the catalog carries the server's own argument names ----------------
#
# ``tools/list`` is required to carry ``inputSchema`` and MCPManager keeps it on
# every handle; ``build_mcp_catalog`` used to throw it away, so the model was
# told a tool existed and nothing about how to call it.  These pin the line it
# now renders, and — just as load-bearing — the cases where it must *not*
# render one.


def _schema(properties=None, required=None, defs=None, **extra):
    schema = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    if defs:
        schema["$defs"] = defs
    schema.update(extra)
    return schema


def _with_schema(name, schema, description="does a thing"):
    """A handle shaped like a real one: the remote tool object carries the schema."""
    return SimpleNamespace(
        name=name,
        description=description,
        tool=SimpleNamespace(input_schema=schema),
    )


def test_a_tool_line_carries_the_arguments_the_server_requires():
    text = build_mcp_catalog(
        [("fs", [_with_schema("echo", _schema({"text": {"type": "string"}}, ["text"]))])]
    )

    assert "echo(text: string):" in text


def test_an_optional_argument_is_marked_and_a_required_one_is_not():
    text = build_mcp_catalog(
        [
            (
                "fs",
                [
                    _with_schema(
                        "issue",
                        _schema(
                            {"title": {"type": "string"}, "body": {"type": "string"}},
                            ["title"],
                        ),
                    )
                ],
            )
        ]
    )

    assert "issue(title: string, body?: string):" in text


def test_the_types_the_model_needs_are_the_ones_the_server_declared():
    text = build_mcp_catalog(
        [
            (
                "fs",
                [
                    _with_schema(
                        "t",
                        _schema(
                            {
                                "n": {"type": "integer"},
                                "f": {"type": "number"},
                                "b": {"type": "boolean"},
                                "a": {"type": "array", "items": {"type": "string"}},
                            }
                        ),
                    )
                ],
            )
        ]
    )

    assert "t(n?: integer, f?: number, b?: boolean, a?: Array<string>):" in text


def test_a_nested_object_is_rendered_rather_than_swallowed():
    text = build_mcp_catalog(
        [
            (
                "fs",
                [
                    _with_schema(
                        "t",
                        _schema(
                            {
                                "meta": {
                                    "type": "object",
                                    "properties": {"created": {"type": "string"}},
                                }
                            }
                        ),
                    )
                ],
            )
        ]
    )

    assert "meta?: {created?: string}" in text


def test_a_local_ref_is_resolved_because_the_server_sent_a_pointer_not_a_type():
    text = build_mcp_catalog(
        [
            (
                "fs",
                [
                    _with_schema(
                        "t",
                        _schema(
                            {"assignee": {"$ref": "#/$defs/user"}},
                            defs={
                                "user": {
                                    "type": "object",
                                    "properties": {"login": {"type": "string"}},
                                }
                            },
                        ),
                    )
                ],
            )
        ]
    )

    assert "assignee?: {login?: string}" in text


def test_a_self_referential_schema_stops_instead_of_recursing_forever():
    # A server that points at itself is either buggy or hostile. Either way the
    # catalog must still be produced: a hang here takes the whole turn down.
    text = build_mcp_catalog(
        [
            (
                "fs",
                [
                    _with_schema(
                        "loop",
                        _schema(
                            {"next": {"$ref": "#/$defs/node"}},
                            defs={
                                "node": {
                                    "type": "object",
                                    "properties": {"next": {"$ref": "#/$defs/node"}},
                                }
                            },
                        ),
                    )
                ],
            )
        ]
    )

    assert "loop(" in text


def test_a_deeply_nested_schema_is_cut_at_a_fixed_depth():
    schema = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(40):
        schema = {"type": "object", "properties": {"down": schema}}

    text = build_mcp_catalog([("fs", [_with_schema("deep", schema)])])

    assert "deep(" in text
    assert "leaf" not in text


def test_a_signature_wider_than_its_budget_says_it_says_something_was_cut():
    wide = {f"argument_number_{i}": {"type": "string"} for i in range(40)}

    text = build_mcp_catalog([("fs", [_with_schema("wide", _schema(wide))])])

    assert "…" in text
    assert "argument_number_39" not in text


def test_a_tool_whose_server_sent_no_schema_is_listed_exactly_as_before():
    # MCP makes inputSchema required, but a broken server omits it. The line
    # must fall back to name + description, not crash and not print "()".
    text = build_mcp_catalog([("fs", [SimpleNamespace(name="t", description="d")])])

    assert "  t: d" in text


def test_an_empty_schema_is_shown_as_no_arguments():
    text = build_mcp_catalog([("fs", [_with_schema("ping", _schema())])])

    assert "ping(): d" in text


# ---- M6: the budget shares, it does not exclude -----------------------------
#
# A server that is connected and working, and which the model cannot see, is
# the worst outcome the catalog can produce: the panel says "Connected ✓", the
# operator did the work, and the capability simply does not exist for the turn.


def test_a_tight_budget_gives_every_server_something_rather_than_dropping_one():
    servers = [
        (f"s{i}", [_tool(f"t{j}") for j in range(10)])
        for i in range(4)
    ]

    text = build_mcp_catalog(servers, budget_chars=600)

    for name, _handles in servers:
        assert f"\n{name}\n" in text, f"{name} lost its place to a budget with room left"


def test_the_tools_of_one_server_are_not_all_taken_before_the_next_is_touched():
    servers = [(f"s{i}", [_tool(f"t{j}") for j in range(10)]) for i in range(3)]

    text = build_mcp_catalog(servers, budget_chars=600)

    # s0 cannot have taken every slot: a round-robin pass gives s1 and s2 one
    # each before s0 gets a second.
    assert text.count("  t0:") < 10


def test_a_server_with_no_room_left_is_still_named_with_what_it_has():
    servers = [("big", [_tool(f"t{j}") for j in range(10)]), ("one", [_tool("only")])]

    text = build_mcp_catalog(servers, budget_chars=600)

    assert "one" in text
    assert "none shown" not in text or "big (10 tools" in text


def test_the_budget_is_never_exceeded_including_the_notes():
    """Regression: the "none shown" line and the trailing note were added after
    the accounting, so a 300-character budget produced 330 characters."""
    servers = [
        ("big", [_tool(f"big{j}", description="x" * 60) for j in range(10)]),
        ("one", [_tool("only")]),
        ("two", [_tool("solo")]),
    ]

    for budget in range(1, 1_200, 7):
        text = build_mcp_catalog(servers, budget_chars=budget)
        assert len(text) <= budget, f"budget {budget} produced {len(text)} characters"


def test_a_budget_too_small_for_any_server_returns_no_catalog_at_all():
    # The header alone is ~184 characters. Emitting it anyway would mean
    # exceeding the budget and telling the model nothing.
    assert build_mcp_catalog([("a", [_tool("x")])], budget_chars=10) == ""
