"""Async protected and manual approval usage."""

import asyncio
import os

from governance_sdk import Governance


async def main() -> None:
    async with Governance(
        api_key=os.environ["GOVERNANCE_API_KEY"],
        project_id=os.environ["GOVERNANCE_PROJECT_ID"],
        base_url=os.environ["GOVERNANCE_BASE_URL"],
        agent_id=os.environ["GOVERNANCE_AGENT_ID"],
    ) as governance:
        @governance.protect(
            reason="Agent requested a refund",
            on_decision=lambda decision: print("Reviewer:", decision.reviewer_comment),
        )
        async def refund_order(order_id: str, amount_cents: int) -> None:
            await payment_api_refund(order_id, amount_cents)

        await refund_order("ord_42", 2500)

        request = await governance.arequest_approval(
            action="refund_order",
            parameters={"order_id": "ord_43", "amount_cents": 900},
            reason="Customer reported a duplicate charge",
        )
        decision = await request.await_decision(timeout=180)
        if decision.approved:
            print(decision.reviewer_comment)
            # Manual execution remains an explicit application responsibility.


async def payment_api_refund(order_id: str, amount_cents: int) -> None:
    """Replace with the application's asynchronous payment API call."""
    print(f"Refunding {amount_cents} cents for {order_id}")


if __name__ == "__main__":
    asyncio.run(main())

