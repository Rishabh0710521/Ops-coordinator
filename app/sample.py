TRANSCRIPT = """Priya (Product): Thanks everyone. We need to settle the plan for the Rewards app pilot in the Lucknow and Kanpur stores.
Arjun (Engineering): Backend work is the points ledger and the coupon service. I can own the ledger, roughly two weeks.
Priya: Launch date, I told the board 4 December. That is the number I'm holding.
Karan (Finance): Budget ceiling for the pilot is 3,50,000 rupees. Anything above 2,00,000 needs my sign-off before we commit spend.
Meera (Design): Design handoff for the app screens will be done by Friday 13 November. The loyalty card screen is the main piece.
Sana (QA): I need at least five working days of testing on a feature-complete build.
Arjun: Honestly I thought we'd said 11 December for launch, the coupon service depends on the POS vendor.
Priya: We'll sort that out offline. Does the POS vendor have an API ready?
Arjun: They promised sandbox credentials but I have no date.
Karan: Store staff need training before the pilot starts. Nobody has picked who runs that.
Meera: Marketing wants posters in the stores. I assume that's not on this team.
Priya: Posters are out of scope for this plan. Let's keep to the app and the pilot.
Sana: Anything that collects customer data needs a security review before we go live, that's company policy.
Priya: Agreed. Security review has to finish before launch, someone from engineering should book it.
Karan: I also want a weekly status note to leadership from now until launch.
Priya: Fine. Karan, you send that out, every Friday.
Arjun: Coupon service is three weeks, not two, once the POS integration is in. Ledger stays at two.
Priya: Okay, let's reconvene Monday."""

RULES = {
    "company": "Brightleaf Foods",
    "roster": [
        {"name": "Priya", "role": "Product Lead"},
        {"name": "Arjun", "role": "Backend Engineer"},
        {"name": "Ishaan", "role": "Frontend Engineer"},
        {"name": "Meera", "role": "Designer"},
        {"name": "Karan", "role": "Finance and Operations"},
        {"name": "Sana", "role": "QA Lead"},
    ],
    "params": {
        "planning_start": "2026-11-02",
        "latest_deadline": "2026-12-11",
        "max_tasks_per_owner": 3,
        "budget_signoff_threshold_inr": 200000,
        "launch_keywords": ["launch", "go live", "go-live"],
        "gate_keywords": ["security review"],
    },
    "rules": [
        {"id": "R1", "text": "Owners must come from the roster. Never assign work to anyone outside it."},
        {"id": "R2", "text": "Any owner or deadline the transcript does not state must be labelled as a recommendation."},
        {"id": "R3", "text": "Deadlines must fall on a working day, Monday to Friday."},
        {"id": "R4", "text": "No person may own more than max_tasks_per_owner tasks."},
        {"id": "R5", "text": "A security review task must be finished before any launch task starts."},
        {"id": "R6", "text": "No deadline may fall before planning_start or after latest_deadline, and none may fall after the launch date the team has agreed."},
        {"id": "R7", "text": "Spend above budget_signoff_threshold_inr needs an explicit finance sign-off task before the spend is committed."},
    ],
}
