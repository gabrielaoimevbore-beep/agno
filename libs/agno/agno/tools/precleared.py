import json
from os import getenv
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agno.tools import Toolkit
from agno.utils.log import log_debug, log_error

DEFAULT_API_BASE_URL = "https://precleared.dev"
ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
SENDER_COUNTRIES = ("GB", "US")
VERDICTS = ("ok_to_email", "consent_needed", "blocked", "unknown")
UNEXPECTED_RESPONSE = "Precleared returned an unexpected response. Do not send yet; try again later."

DEFAULT_INSTRUCTIONS = dedent(
    """\
    Before the first cold or unsolicited marketing email to anyone who is not already a customer,
    call check_contact once for that recipient. Check again if what you are offering or where the
    address came from changes. It is not needed for transactional email such as invoices, receipts
    or password resets.
    - ok_to_email: you may send, with a footer that meets footer_text. footer_text describes what
      the footer must contain; it is not wording to paste.
    - consent_needed or blocked: do not send. Follow next_step.
    - unknown: a fact could not be established. Treat it as consent_needed unless you can supply
      the missing fact and check again.
    - An error instead of a verdict means the check could not be made (for example, the daily limit
      was reached). Do not send that email yet; try again later and carry on with your other work.
    Say where the address came from honestly in address_source. Keep the receipt id with the lead.
    """
)


class PreclearedTools(Toolkit):
    """
    PreclearedTools checks whether a cold marketing email to one recipient is allowed before an
    agent sends it, and fetches the signed receipt for an earlier check.

    Precleared (https://precleared.dev) answers per address under UK PECR and UK GDPR, US CAN-SPAM,
    Canada's CASL, Germany's UWG and France's rules. Each answer has a verdict (ok_to_email,
    consent_needed, blocked or unknown), the rule it rests on with its source, what the email's
    footer must include, a next step and a signed receipt id. Nothing is sent to the recipient.

    An API key is optional. Without one the REST API allows 10 checks a day; a free key from
    https://precleared.dev/get-key allows 25 a day, and paid packs cover more.
    See https://precleared.dev/docs for the API.

    Args:
        api_key (Optional[str]): Precleared API key (pc_live_...). Falls back to the
            PRECLEARED_API_KEY environment variable. Optional.
        sender_country (str): Where the sending business is based, "GB" or "US". Default is "GB".
        enable_check_contact (bool): Enable check_contact. Default is True.
        enable_get_receipt (bool): Enable get_receipt. Default is False.
        all (bool): Enable all tools. Overrides individual flags when True. Default is False.
        api_base_url (str): Base URL of the Precleared API. Default is "https://precleared.dev".
        timeout (int): Timeout in seconds for each request. Default is 30 (a check can take up
            to 25 seconds when a company register is slow).
        instructions (Optional[str]): Override the default toolkit instructions.
        add_instructions (bool): Add the toolkit instructions to the agent. Default is True.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        sender_country: str = "GB",
        enable_check_contact: bool = True,
        enable_get_receipt: bool = False,
        all: bool = False,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout: int = 30,
        instructions: Optional[str] = None,
        add_instructions: bool = True,
        **kwargs,
    ):
        self.api_key: Optional[str] = api_key or getenv("PRECLEARED_API_KEY")
        sender_country = (sender_country or "").strip().upper()
        if sender_country not in SENDER_COUNTRIES:
            raise ValueError(f"sender_country must be one of {', '.join(SENDER_COUNTRIES)}, got {sender_country!r}")
        self.sender_country: str = sender_country
        self.api_base_url: str = api_base_url.rstrip("/")

        tools: List[Any] = []
        async_tools: List[Tuple[Any, str]] = []
        if all or enable_check_contact:
            tools.append(self.check_contact)
            async_tools.append((self.acheck_contact, "check_contact"))
        if all or enable_get_receipt:
            tools.append(self.get_receipt)
            async_tools.append((self.aget_receipt, "get_receipt"))

        super().__init__(
            name="precleared_tools",
            tools=tools,
            async_tools=async_tools,
            instructions=instructions or DEFAULT_INSTRUCTIONS,
            add_instructions=add_instructions,
            timeout=timeout,
            **kwargs,
        )

    # -- pure helpers shared by the sync and async paths -------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "agno"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _check_payload(
        self,
        recipient_email: str,
        address_source: str,
        pitch: Optional[str],
        recipient_country: Optional[str],
        existing_customer: bool,
    ) -> Dict[str, Any]:
        email = (recipient_email or "").strip()
        if not email or "@" not in email:
            raise ValueError("recipient_email must be a full email address")
        source = (address_source or "").strip().lower()
        if source not in ADDRESS_SOURCES:
            raise ValueError(f"address_source must be one of: {', '.join(ADDRESS_SOURCES)}")
        payload: Dict[str, Any] = {
            "recipient_email": email,
            "sender_country": self.sender_country,
            "address_source": source,
            "existing_customer": bool(existing_customer),
        }
        if pitch:
            payload["pitch"] = pitch.strip()
        if recipient_country:
            payload["recipient_country"] = recipient_country.strip().upper()
        return payload

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            return text.replace(self.api_key, "***")
        return text

    def _parse(self, response: Any, expect_verdict: bool = False) -> str:
        """Return Precleared's JSON as a string, or an error object the model can act on."""
        try:
            data = response.json()
        except ValueError:
            return json.dumps({"error": f"Precleared returned status {response.status_code} with no JSON body"})
        if response.status_code != 200:
            body = data if isinstance(data, dict) else {"error": str(data)}
            body.setdefault("error", "request_failed")
            body["status"] = response.status_code
            return json.dumps(body)
        # Only a JSON object (with a known verdict, for a check) counts as an answer. Anything else
        # becomes an error, so the model never reads a stray value as permission to send.
        if not isinstance(data, dict) or (expect_verdict and data.get("verdict") not in VERDICTS):
            return json.dumps({"error": UNEXPECTED_RESPONSE})
        return json.dumps(data)

    # -- tools ---------------------------------------------------------------------------

    def check_contact(
        self,
        recipient_email: str,
        address_source: str,
        pitch: Optional[str] = None,
        recipient_country: Optional[str] = None,
        existing_customer: bool = False,
    ) -> str:
        """Check whether a cold marketing email to one recipient is allowed before sending it.

        Use once per recipient before the first cold or unsolicited marketing email. Not needed for
        transactional email such as invoices, receipts or password resets.

        Args:
            recipient_email (str): The recipient's email address.
            address_source (str): Where the address came from: "published" (on their website, profile
                or a directory), "bought" (a purchased list), "scraped" (collected by a tool), "given"
                (the person gave it to you) or "unknown". It affects the verdict, so do not assume published.
            pitch (Optional[str]): What the email offers, in a few words, used to judge relevance to the
                recipient's role.
            recipient_country (Optional[str]): Two-letter country code of the recipient if known, e.g. "GB".
            existing_customer (bool): True if the recipient has bought from you before.

        Returns:
            str: JSON with verdict (ok_to_email, consent_needed, blocked or unknown), confidence, reasons,
            footer_text (what the footer must include), next_step and receipt, or an error.
        """
        try:
            payload = self._check_payload(recipient_email, address_source, pitch, recipient_country, existing_customer)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        log_debug(f"Precleared check for {payload['recipient_email']}")
        try:
            response = httpx.post(
                f"{self.api_base_url}/api/v1/check", json=payload, headers=self._headers(), timeout=self.timeout
            )
        except httpx.HTTPError as e:
            log_error(f"Precleared request failed: {e}")
            return json.dumps({"error": self._redact(f"Precleared request failed: {e}")})
        return self._parse(response, expect_verdict=True)

    async def acheck_contact(
        self,
        recipient_email: str,
        address_source: str,
        pitch: Optional[str] = None,
        recipient_country: Optional[str] = None,
        existing_customer: bool = False,
    ) -> str:
        """Check whether a cold marketing email to one recipient is allowed before sending it.

        Use once per recipient before the first cold or unsolicited marketing email. Not needed for
        transactional email such as invoices, receipts or password resets.

        Args:
            recipient_email (str): The recipient's email address.
            address_source (str): Where the address came from: "published" (on their website, profile
                or a directory), "bought" (a purchased list), "scraped" (collected by a tool), "given"
                (the person gave it to you) or "unknown". It affects the verdict, so do not assume published.
            pitch (Optional[str]): What the email offers, in a few words, used to judge relevance to the
                recipient's role.
            recipient_country (Optional[str]): Two-letter country code of the recipient if known, e.g. "GB".
            existing_customer (bool): True if the recipient has bought from you before.

        Returns:
            str: JSON with verdict (ok_to_email, consent_needed, blocked or unknown), confidence, reasons,
            footer_text (what the footer must include), next_step and receipt, or an error.
        """
        try:
            payload = self._check_payload(recipient_email, address_source, pitch, recipient_country, existing_customer)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        log_debug(f"Precleared check for {payload['recipient_email']}")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.api_base_url}/api/v1/check", json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            log_error(f"Precleared request failed: {e}")
            return json.dumps({"error": self._redact(f"Precleared request failed: {e}")})
        return self._parse(response, expect_verdict=True)

    def get_receipt(self, receipt_id: str) -> str:
        """Fetch the signed receipt for an earlier check, to show what was checked, when, and the verdict.

        Args:
            receipt_id (str): The receipt id from a check, e.g. "cc_rcpt_0123456789ABCDEF".

        Returns:
            str: JSON with the verdict, rules applied, request and response hashes, the Ed25519 signature
            and the public key needed to verify it, or an error.
        """
        if not receipt_id or not receipt_id.startswith("cc_rcpt_"):
            return json.dumps({"error": "receipt_id must look like cc_rcpt_..."})
        try:
            response = httpx.get(
                f"{self.api_base_url}/api/v1/receipt",
                params={"id": receipt_id},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            log_error(f"Precleared request failed: {e}")
            return json.dumps({"error": self._redact(f"Precleared request failed: {e}")})
        return self._parse(response)

    async def aget_receipt(self, receipt_id: str) -> str:
        """Fetch the signed receipt for an earlier check, to show what was checked, when, and the verdict.

        Args:
            receipt_id (str): The receipt id from a check, e.g. "cc_rcpt_0123456789ABCDEF".

        Returns:
            str: JSON with the verdict, rules applied, request and response hashes, the Ed25519 signature
            and the public key needed to verify it, or an error.
        """
        if not receipt_id or not receipt_id.startswith("cc_rcpt_"):
            return json.dumps({"error": "receipt_id must look like cc_rcpt_..."})
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.api_base_url}/api/v1/receipt", params={"id": receipt_id}, headers=self._headers()
                )
        except httpx.HTTPError as e:
            log_error(f"Precleared request failed: {e}")
            return json.dumps({"error": self._redact(f"Precleared request failed: {e}")})
        return self._parse(response)
