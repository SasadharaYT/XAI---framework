"""Create a manual request; application code owns execution."""

import os

from governance_sdk import Governance


def main() -> None:
    with Governance(
        api_key=os.environ["GOVERNANCE_API_KEY"],
        project_id=os.environ["GOVERNANCE_PROJECT_ID"],
        base_url=os.environ["GOVERNANCE_BASE_URL"],
    ) as governance:
        request = governance.request_approval(
            agent_id=os.environ["GOVERNANCE_AGENT_ID"],
            action="delete_customer",
            parameters={"customer_id": 123},
            reason="Customer requested account deletion",
            metadata={"ticket_id": "SUP-184"},
        )
        decision = request.wait(timeout=180)
        print(decision.status, decision.reviewer_comment)
        if decision.approved:
            # The SDK does not call this manually controlled operation.
            print("Application may now execute the exact approved customer_id")


if __name__ == "__main__":
    main()

