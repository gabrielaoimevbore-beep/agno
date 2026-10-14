"""
Precleared Tools
=============================

Demonstrates checking whether a cold marketing email is allowed before an agent sends it.

check_contact returns a verdict for one address (ok_to_email, consent_needed, blocked or
unknown) under UK PECR, US CAN-SPAM, Canada's CASL, Germany's UWG or France's rules, with
the rule and its source, what the email's footer must include, a next step and a signed
receipt id. Nothing is sent to the recipient.

Prerequisites:
- No key is needed for 10 checks a day. For more, get a free key at
  https://precleared.dev/get-key and set:
    export PRECLEARED_API_KEY=<your-api-key>
"""

import json

from agno.agent import Agent
from agno.tools.precleared import PreclearedTools

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# A prospect whose address you found on their company's own website.
prospect_email = "<enter_prospect_email>"

precleared = PreclearedTools(sender_country="GB", all=True)

# ---------------------------------------------------------------------------
# Create Agent
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[precleared],
    instructions="You prepare cold outreach for a UK software company.",
    markdown=True,
)

# ---------------------------------------------------------------------------
# Run Agent
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # The agent should call check_contact before drafting anything.
    agent.print_response(
        f"We found {prospect_email} on their company website. "
        "Can we send them a cold email about our route-planning software? "
        "If yes, draft it with a footer that meets the requirement."
    )

    # ---------------------------------------------------------------------------
    # More Examples
    # ---------------------------------------------------------------------------

    # Enforce the verdict in code as well, so a send step never depends on the
    # model following its instructions.
    result = json.loads(precleared.check_contact(prospect_email, address_source="published"))
    if result.get("verdict") == "ok_to_email":
        print(f"Allowed. Footer must include: {result.get('footer_text')}")
        print(f"Receipt: {result.get('receipt', {}).get('id')}")
    else:
        print(f"Do not send: {result.get('verdict') or result.get('error')}. {result.get('next_step') or ''}")
