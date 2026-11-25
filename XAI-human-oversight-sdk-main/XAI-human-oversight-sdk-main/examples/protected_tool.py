"""Protect a synchronous tool call and surface its reviewer decision."""

import os

from governance_sdk import ApprovalRejectedError, Governance


def main() -> None:
    with Governance(
        api_key=os.environ["GOVERNANCE_API_KEY"],
        project_id=os.environ["GOVERNANCE_PROJECT_ID"],
        base_url=os.environ["GOVERNANCE_BASE_URL"],
    ) as governance:
        @governance.protect(
            agent_id=os.environ["GOVERNANCE_AGENT_ID"],
            reason="Support agent requested account removal",
            on_decision=lambda decision: print("Reviewer:", decision.reviewer_comment),
        )
        def delete_customer(customer_id: int) -> str:
            return f"deleted {customer_id}"

        try:
            print(delete_customer(123))
        except ApprovalRejectedError as exc:
            print("Not approved:", exc.comment)


if __name__ == "__main__":
    main()

