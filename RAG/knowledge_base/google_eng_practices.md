# Code Review Standards and Change Size Philosophy

*Original summary written for this demo corpus. Not a reproduction of any published standard.*

## The Standard of Review

The purpose of code review is to keep the overall health of the codebase improving over
time. The operative bar is not perfection: a reviewer should approve a change once it
definitely improves the codebase's health, even if it is not flawless. Holding changes
hostage to an imagined ideal implementation stalls progress and teaches authors to batch
work into larger, riskier changes.

The counterweight is that a change which degrades health should not be approved on the
grounds that it is urgent or nearly done. Urgency argues for a smaller, safer change, not
a looser bar.

Reviewers should distinguish between requirements and preferences. Correctness, security,
test coverage, and clarity are requirements. Stylistic preferences that the style guide
does not mandate should be marked explicitly as optional so the author can decide. Where
a reviewer and author disagree on substance, the resolution is technical facts and
established principles, not seniority or persistence.

Review latency matters. A change should receive a response within one business day; long
review turnaround is one of the most reliable causes of oversized changes, because authors
keep working while they wait.

## Small Changes

Small changes are reviewed faster, reviewed more thoroughly, and are less likely to
introduce defects. They are easier to roll back, easier to bisect, and easier to reason
about in isolation. A change is the right size when it is self-contained, does one
conceptual thing, and includes the tests for that thing.

Signals that a change is too large: it touches many unrelated modules, it mixes a
refactor with a behavior change, or a reviewer cannot hold its full effect in their head.
The remedy is to split — land pure refactors separately from semantic changes, and
sequence dependent pieces so each is independently correct and independently revertible.

A large diff that is mostly mechanical (a rename, a generated file) is not the same risk
as a large diff of hand-written logic. Assess review burden by the volume of genuinely new
logic, not by raw line count. Conversely, a small diff in authentication, permissions, or
query construction can carry more risk than a thousand-line rename.

## Testing Expectations

Changes should include tests at the level that makes the behavior verifiable, and those
tests should fail if the behavior regresses. Tests asserting only that code runs without
raising provide little protection. For a bug fix, add a test that reproduces the original
bug — otherwise nothing prevents its return.

Integration tests earn their cost where components meet: authentication flows, database
access layers, and public API contracts. When a change alters a security-relevant path or
a cross-service contract, the absence of an integration test is itself a review finding
worth blocking on.

Continuous integration passing is a floor, not evidence of adequacy. Ask what the tests
would have caught, not merely whether they are green.
