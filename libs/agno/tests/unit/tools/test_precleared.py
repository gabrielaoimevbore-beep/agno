"""Unit tests for PreclearedTools class."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agno.tools.precleared import DEFAULT_INSTRUCTIONS, PreclearedTools

OK_RESPONSE = {
    "verdict": "ok_to_email",
    "confidence": "high",
    "reasons": [
        {
            "rule_key": "gb_corporate_ok",
            "text": "Corporate subscribers need no prior consent for B2B marketing email.",
            "source_title": "PECR reg. 22",
            "source_url": "https://www.legislation.gov.uk/uksi/2003/2426/regulation/22",
        }
    ],
    "rules_applied": ["gb_corporate_ok"],
    "footer_text": "Say who you are, include a valid postal address and a simple free way to opt out.",
    "next_step": "Send. Log the receipt against the lead.",
    "receipt": {"id": "cc_rcpt_0123456789ABCDEF"},
}

RECEIPT_RESPONSE = {
    "id": "cc_rcpt_0123456789ABCDEF",
    "verdict": "ok_to_email",
    "rules_applied": ["gb_corporate_ok"],
    "signature": "c2lnbmF0dXJl",
    "algorithm": "ed25519",
}


def _response(status_code=200, json_data=None):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data
    return response


@pytest.fixture
def tools():
    with patch.dict("os.environ", {}, clear=True):
        return PreclearedTools(all=True)


# ============================================================================
# INITIALIZATION TESTS
# ============================================================================


def test_init_defaults():
    with patch.dict("os.environ", {}, clear=True):
        tools = PreclearedTools()
    assert tools.name == "precleared_tools"
    assert tools.api_key is None
    assert tools.sender_country == "GB"
    assert tools.api_base_url == "https://precleared.dev"
    assert tools.timeout == 30
    assert set(tools.functions) == {"check_contact"}
    assert set(tools.async_functions) == {"check_contact"}
    assert tools.instructions == DEFAULT_INSTRUCTIONS
    assert tools.add_instructions is True


def test_init_all_registers_every_tool():
    tools = PreclearedTools(all=True)
    assert set(tools.functions) == {"check_contact", "get_receipt"}
    assert set(tools.async_functions) == {"check_contact", "get_receipt"}


def test_init_reads_api_key_from_env():
    with patch.dict("os.environ", {"PRECLEARED_API_KEY": "pc_live_env"}, clear=True):
        tools = PreclearedTools()
    assert tools.api_key == "pc_live_env"


def test_init_normalises_and_validates_sender_country():
    assert PreclearedTools(sender_country="us").sender_country == "US"
    with pytest.raises(ValueError):
        PreclearedTools(sender_country="FR")


def test_instructions_say_not_to_send_when_the_check_fails():
    assert "Do not send that email yet" in DEFAULT_INSTRUCTIONS


# ============================================================================
# CHECK CONTACT TESTS
# ============================================================================


def test_check_contact_without_key(tools):
    with patch("agno.tools.precleared.httpx.post", return_value=_response(200, OK_RESPONSE)) as mock_post:
        result = json.loads(tools.check_contact("sarah.patel@kestrelfreight.co.uk", address_source="published"))

    assert result["verdict"] == "ok_to_email"
    assert result["receipt"]["id"] == "cc_rcpt_0123456789ABCDEF"
    args, kwargs = mock_post.call_args
    assert args == ("https://precleared.dev/api/v1/check",)
    assert kwargs["json"] == {
        "recipient_email": "sarah.patel@kestrelfreight.co.uk",
        "sender_country": "GB",
        "address_source": "published",
        "existing_customer": False,
    }
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["timeout"] == 30


def test_check_contact_sends_key_and_optional_fields():
    tools = PreclearedTools(api_key="pc_live_test", sender_country="US")
    with patch("agno.tools.precleared.httpx.post", return_value=_response(200, OK_RESPONSE)) as mock_post:
        tools.check_contact(
            "mike@summitroofingco.com",
            address_source="published",
            pitch="lead generation for roofers",
            recipient_country="us",
            existing_customer=False,
        )

    kwargs = mock_post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer pc_live_test"
    assert kwargs["json"]["sender_country"] == "US"
    assert kwargs["json"]["pitch"] == "lead generation for roofers"
    assert kwargs["json"]["recipient_country"] == "US"


def test_check_contact_rejects_bad_address_source(tools):
    with patch("agno.tools.precleared.httpx.post") as mock_post:
        result = json.loads(tools.check_contact("dave@example.co.uk", address_source="guessed"))
    assert "address_source" in result["error"]
    mock_post.assert_not_called()


def test_check_contact_rejects_missing_email(tools):
    with patch("agno.tools.precleared.httpx.post") as mock_post:
        result = json.loads(tools.check_contact("not-an-email", address_source="published"))
    assert "recipient_email" in result["error"]
    mock_post.assert_not_called()


def test_check_contact_daily_limit_passes_error_through(tools):
    body = {
        "error": "daily_limit",
        "message": "Keyless use is over for today.",
        "get_key_url": "https://precleared.dev/get-key",
    }
    with patch("agno.tools.precleared.httpx.post", return_value=_response(429, body)):
        result = json.loads(tools.check_contact("ops@swiftparcel.co.uk", address_source="published"))
    assert result["error"] == "daily_limit"
    assert result["status"] == 429
    assert result["get_key_url"] == "https://precleared.dev/get-key"


def test_check_contact_network_error_hides_key():
    tools = PreclearedTools(api_key="pc_live_secret")
    with patch("agno.tools.precleared.httpx.post", side_effect=httpx.ConnectError("pc_live_secret refused")):
        result = json.loads(tools.check_contact("ops@swiftparcel.co.uk", address_source="published"))
    assert "Precleared request failed" in result["error"]
    assert "pc_live_secret" not in result["error"]


def test_check_contact_non_json_error(tools):
    response = _response(502)
    response.json.side_effect = ValueError("no json")
    with patch("agno.tools.precleared.httpx.post", return_value=response):
        result = json.loads(tools.check_contact("ops@swiftparcel.co.uk", address_source="published"))
    assert "502" in result["error"]


@pytest.mark.parametrize("body", [[], "ok_to_email", None, {"verdict": "maybe"}, {"reasons": []}])
def test_check_contact_unexpected_response_is_an_error(tools, body):
    with patch("agno.tools.precleared.httpx.post", return_value=_response(200, body)):
        result = json.loads(tools.check_contact("ops@swiftparcel.co.uk", address_source="published"))
    assert "verdict" not in result
    assert "unexpected response" in result["error"]


# ============================================================================
# RECEIPT TESTS
# ============================================================================


def test_get_receipt(tools):
    with patch("agno.tools.precleared.httpx.get", return_value=_response(200, RECEIPT_RESPONSE)) as mock_get:
        result = json.loads(tools.get_receipt("cc_rcpt_0123456789ABCDEF"))
    assert result["signature"] == "c2lnbmF0dXJl"
    args, kwargs = mock_get.call_args
    assert args == ("https://precleared.dev/api/v1/receipt",)
    assert kwargs["params"] == {"id": "cc_rcpt_0123456789ABCDEF"}


def test_get_receipt_rejects_bad_id(tools):
    with patch("agno.tools.precleared.httpx.get") as mock_get:
        result = json.loads(tools.get_receipt("12345"))
    assert "cc_rcpt_" in result["error"]
    mock_get.assert_not_called()


def test_get_receipt_not_found(tools):
    with patch("agno.tools.precleared.httpx.get", return_value=_response(404, {"error": "not found"})):
        result = json.loads(tools.get_receipt("cc_rcpt_FFFFFFFFFFFFFFFF"))
    assert result == {"error": "not found", "status": 404}


def test_get_receipt_unexpected_response_is_an_error(tools):
    with patch("agno.tools.precleared.httpx.get", return_value=_response(200, ["cc_rcpt_0123456789ABCDEF"])):
        result = json.loads(tools.get_receipt("cc_rcpt_0123456789ABCDEF"))
    assert "unexpected response" in result["error"]


# ============================================================================
# ASYNC TESTS
# ============================================================================


@pytest.mark.asyncio
@patch("agno.tools.precleared.httpx.AsyncClient")
async def test_acheck_contact(mock_client_class, tools):
    client = mock_client_class.return_value.__aenter__.return_value
    client.post = AsyncMock(return_value=_response(200, OK_RESPONSE))

    result = json.loads(await tools.acheck_contact("sarah.patel@kestrelfreight.co.uk", address_source="published"))

    assert result["verdict"] == "ok_to_email"
    args, kwargs = client.post.call_args
    assert args == ("https://precleared.dev/api/v1/check",)
    assert kwargs["json"]["address_source"] == "published"
    mock_client_class.assert_called_once_with(timeout=30)


@pytest.mark.asyncio
@patch("agno.tools.precleared.httpx.AsyncClient")
async def test_acheck_contact_validation_skips_request(mock_client_class, tools):
    result = json.loads(await tools.acheck_contact("dave@example.co.uk", address_source="guessed"))
    assert "address_source" in result["error"]
    mock_client_class.assert_not_called()


@pytest.mark.asyncio
@patch("agno.tools.precleared.httpx.AsyncClient")
async def test_acheck_contact_unexpected_response_is_an_error(mock_client_class, tools):
    client = mock_client_class.return_value.__aenter__.return_value
    client.post = AsyncMock(return_value=_response(200, {"verdict": "maybe"}))

    result = json.loads(await tools.acheck_contact("sarah.patel@kestrelfreight.co.uk", address_source="published"))

    assert "verdict" not in result
    assert "unexpected response" in result["error"]


@pytest.mark.asyncio
@patch("agno.tools.precleared.httpx.AsyncClient")
async def test_aget_receipt(mock_client_class, tools):
    client = mock_client_class.return_value.__aenter__.return_value
    client.get = AsyncMock(return_value=_response(200, RECEIPT_RESPONSE))

    result = json.loads(await tools.aget_receipt("cc_rcpt_0123456789ABCDEF"))

    assert result["id"] == "cc_rcpt_0123456789ABCDEF"
    assert client.get.call_args.kwargs["params"] == {"id": "cc_rcpt_0123456789ABCDEF"}
