import jsonschema
import pytest

from rated_mcp.client import validate_next_url
from rated_mcp.spec import build_tools, is_retired, load_spec, tool_name


@pytest.fixture(scope="module")
def spec():
    return load_spec()


@pytest.fixture(scope="module")
def tools(spec):
    return build_tools(spec, default_page_size=5, max_page_size=50)


def test_every_live_operation_becomes_a_tool(spec, tools):
    live = [
        (method, path)
        for path, item in spec["paths"].items()
        for method, op in item.items()
        if method in ("get", "post", "put", "patch", "delete") and not is_retired(op)
    ]
    assert len(tools) == len(live)
    assert {(t.method, t.path) for t in tools} == set(live)


def test_retired_endpoints_are_skipped(tools):
    paths = {t.path for t in tools}
    assert "/v1/solana/network/geolocation/latency" not in paths
    assert "/v1/solana/validators/{validator_id}/votePerformance" not in paths


def test_names_are_unique_and_valid(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))
    for name in names:
        assert len(name) <= 64
        assert name.replace("_", "").isalnum()


def test_writes_are_flagged(tools):
    writes = {t.name for t in tools if t.is_write}
    assert writes == {
        "private_sets_v1_create_set",
        "private_sets_v1_update_set",
        "private_sets_v1_delete_set",
        "private_sets_v1_add_set_validators",
        "private_sets_v1_remove_set_validators",
        "private_sets_v1_remove_set_validator",
        "eth_v0_self_report_validators",
    }


def test_input_schemas_are_valid_json_schema(tools):
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)


def test_page_size_defaults_are_small(tools):
    paged = [t for t in tools if t.page_size_param]
    assert paged
    for tool in paged:
        prop = tool.input_schema["properties"][tool.page_size_param]
        assert prop["default"] == 5
        assert prop["maximum"] <= 50


def test_naming_examples():
    assert tool_name("get", "/v1/eth/entities/{entity_id}/effectiveness")[0] == "eth_v1_entity_effectiveness"
    assert tool_name("get", "/v1/eth")[0] == "eth_v1_network_overview"
    assert tool_name("get", "/v1/solana/delegators/stakeAccounts/{stake_account}/rewards")[0] == (
        "solana_v1_stake_account_rewards"
    )
    assert tool_name("get", "/v0/eth/network/dailyRewards/{date}")[0] == "eth_v0_network_daily_rewards_by_date"


def test_next_url_validation():
    assert validate_next_url("http://api.rated.network/v1/eth/entities?limit=2&offset=2").startswith("https://")
    with pytest.raises(ValueError):
        validate_next_url("https://evil.example.com/v1/eth/entities")
