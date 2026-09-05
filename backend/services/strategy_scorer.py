from typing import Any
from database.models import RecoveryCase


def calculate_strategy_scores(case: RecoveryCase, policy: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """
    Deterministic Strategy Scorer:
    STRATEGY_SCORE = Expected Recovery Value - Intervention Cost - Disturbance Penalty - Risk Penalty
    """
    costs = policy.get("channel_costs", {
        "VERIFY": 0,
        "WAIT": 0,
        "RECOVERY_LINK": 2,
        "ESCALATE": 100,
        "STOP": 0
    })

    # Action base recovery probabilities
    action_probs = {
        "WAIT": 0.75 if case.failure_category in {"UNCERTAIN_OR_TEMPORARY", "INSUFFICIENT_FUNDS"} else 0.40,
        "VERIFY": 0.90 if case.failure_category in {"UNCERTAIN_OR_TEMPORARY"} else 0.60,
        "RECOVER": 0.70,
        "ESCALATE": 0.85 if case.amount >= policy.get("minimum_amount_for_human_escalation", 10000) else 0.30,
        "STOP": 0.0,
    }

    # Disturbance penalties
    disturbances = {
        "WAIT": 0,
        "VERIFY": 0,
        "RECOVER": 15 + (case.communication_count * 10),
        "ESCALATE": 25,
        "STOP": 0,
    }

    scores = {}
    for act in ["WAIT", "VERIFY", "RECOVER", "ESCALATE", "STOP"]:
        prob = action_probs.get(act, 0.5)
        cost = costs.get(act, 0)
        dist = disturbances.get(act, 0)

        risk = 0
        if case.retry_count >= policy.get("maximum_automated_attempts", 3) and act == "RECOVER":
            risk = 500
        if case.communication_count >= policy.get("maximum_customer_communications", 3) and act == "RECOVER":
            risk = 500

        expected_gross = int(case.amount * prob * 0.1)
        score = expected_gross - cost - dist - risk
        if case.recovered:
            score = 100 if act == "STOP" else -100

        scores[act] = {
            "score": score,
            "expected_probability": prob,
            "operational_cost": cost,
            "disturbance_penalty": dist,
            "risk_penalty": risk,
        }

    ranked = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    best_candidate = ranked[0][0]
    return scores, best_candidate
