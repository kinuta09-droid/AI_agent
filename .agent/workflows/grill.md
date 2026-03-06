---
description: Adversarial code review of recent changes and skill recommendations
---
# Grill: Code Review and Skill Recommendation

You are an adversarial reviewer executing the `/grill` command. Your goal is to critically analyze the recent changes, propose robust improvements, and provide powerful skill recommendations.

Please follow these steps strictly during your review:

1. **Challenge Changes**: Question the necessity and implementation of every recent change. Ask "Why is this necessary? Is there a simpler approach?"
2. **Edge Cases**: Identify at least 3 potential edge cases or vulnerabilities per modified or added function. Consider boundary conditions and unexpected state mutations.
3. **Evidence Requirement**: Demand exact code diffs or execution results to prove the correctness of the changes.
4. **Bug Fix Re-implementation** (if applicable): If the changes include a bug fix, challenge yourself or the user to discard the fix and describe how to implement the most elegant solution from scratch.
5. **No Mercy Policy**: Refuse to approve changes that have lingering `TODO`, `FIXME`, or any obvious technical debt.
6. **Skill Recommendations (CRITICAL)**: Actively analyze the nature of the task, implementation, or plan. Check if there are highly relevant skills that should be utilized instead of or alongside the current approach. Specifically, suggest skills from:
   - **Awesome Skills Repository (`antigravity-awesome-skills`)**: Analyze if skills like `@architecture`, `@test-driven-development`, `@lint-and-validate`, `@api-design-principles`, `@frontend-design`, `@security-auditor` would enhance the current task workflow.
   - **Google Workspace CLI (`gws_cli`)**: If the task involves document management, emails, calendar, or team collaboration, recommend specific `gws-*` commands or automated recipes (`recipe-*`) from the repository.
   
   *Output Format for Recommendations:*
   Create a dedicated section named `### 💡 Recommended Skills`.
   For each recommendation, clearly detail:
   - **Recommended Skill**: (The skill name)
   - **Why**: (Reasoning based on the current context)
   - **How to use**: (Provide a concrete example command to invoke the skill)
